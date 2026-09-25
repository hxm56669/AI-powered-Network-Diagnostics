"""Pinned local BGE-M3 dense CLS encoder and explicitly supplied test vectors."""
from __future__ import annotations

import re
import json
import multiprocessing as mp
import socket
import time
import resource
import weakref
from contextlib import nullcontext
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from threading import Lock, RLock

import numpy as np

from ..knowledge.common import KnowledgeError, digest
from ..knowledge.tokenization import ConservativeTokenizer, ModelTokenizer
from .scheduling import gpu_slot


class EmbeddingError(KnowledgeError):
    pass


class EmbeddingTimeout(EmbeddingError):
    pass


class ModelWorkerError(KnowledgeError):
    pass


class ModelStageTimeout(ModelWorkerError):
    pass


def _remaining(deadline, budget=None):
    remaining = budget.check() if budget is not None else float("inf")
    if deadline is not None:
        stage = deadline - time.monotonic()
        if stage <= 0:
            raise ModelStageTimeout("model_stage_timeout")
        remaining = min(remaining, stage)
    return remaining


def _receive_exact(connection, size, deadline=None, budget=None):
    result = bytearray()
    while len(result) < size:
        remaining = _remaining(deadline, budget)
        connection.settimeout(min(.05, remaining) if deadline is not None or budget is not None else None)
        try:
            data = connection.recv(min(65536, size - len(result)))
        except socket.timeout:
            continue
        if not data:
            raise ModelWorkerError("model_worker_disconnected")
        result.extend(data)
    return bytes(result)


def receive_message(connection, deadline=None, budget=None):
    size = int.from_bytes(_receive_exact(connection, 8, deadline, budget), "big")
    if size > 8 * 1024 * 1024:
        raise ModelWorkerError("model_message_too_large")
    return json.loads(_receive_exact(connection, size, deadline, budget))


def send_message(connection, value, deadline=None, budget=None):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > 8 * 1024 * 1024:
        raise ModelWorkerError("model_message_too_large")
    frame = memoryview(len(data).to_bytes(8, "big") + data)
    while frame:
        remaining = _remaining(deadline, budget)
        connection.settimeout(min(.05, remaining) if deadline is not None or budget is not None else None)
        try:
            sent = connection.send(frame)
        except socket.timeout:
            continue
        if sent == 0:
            raise ModelWorkerError("model_worker_disconnected")
        frame = frame[sent:]


MODEL_WORKERS = weakref.WeakSet()


def model_resources(device):
    import torch
    return {"rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "cuda_allocated_peak_bytes": torch.cuda.max_memory_allocated(device) if str(device).startswith("cuda") else 0,
            "cuda_reserved_peak_bytes": torch.cuda.max_memory_reserved(device) if str(device).startswith("cuda") else 0}


def kill_model_workers():
    # Emergency deadline path immediately followed by os._exit, not normal
    # resource cleanup. ModelProcess.close handles joining during normal shutdown.
    for worker in list(MODEL_WORKERS):
        process = worker.process
        try:
            if process is not None and process.is_alive():
                process.kill()
        except (ValueError, OSError):
            pass


class ModelProcess:
    """One bounded request over deadline-aware IPC; kills inference on cancel/timeout.

    Socket framing bounds sends and complete receives, unlike poll()+Connection.recv()
    which can block on a partial/large message after the first bytes become readable.
    """
    def __init__(self, config, worker):
        self.config = dict(config)
        self.worker = worker
        self.process = None
        self.connection = None
        self.lock = Lock()
        self._state_lock = Lock()
        self._closed = False
        self.failures = 0
        self.max_failures = int(config.get("max_failures", 2))
        self.identity = None
        self.resources = {}
        MODEL_WORKERS.add(self)

    def warmup(self, budget=None):
        deadline = time.monotonic() + float(self.config.get("load_timeout_s", 120))
        with gpu_slot(self.config.get("device", "cpu"), "online", lambda: _remaining(deadline, budget)):
            return self._warmup(budget, deadline)

    def _warmup(self, budget, deadline):
        if self._closed:
            raise ModelWorkerError("model_worker_closed")
        if self.process is not None and self.process.is_alive():
            return self.identity
        if self.failures >= self.max_failures:
            raise ModelWorkerError("model_circuit_open")
        _remaining(deadline, budget)
        context = mp.get_context("spawn")
        parent, child = socket.socketpair()
        try:
            with self._state_lock:
                if self._closed:
                    raise ModelWorkerError("model_worker_closed")
                self.connection = parent
                self.process = context.Process(target=self.worker, args=(child, {**self.config, "_managed_gpu": True}), daemon=True)
                self.process.start()
            child.close()
            status, payload = receive_message(parent, deadline, budget)
            if status != "ready":
                raise ModelWorkerError(f"model_load_failed: {payload}")
            if not isinstance(payload, dict) or not {"__identity__", "resources"} <= payload.keys():
                raise ModelWorkerError("model_worker_invalid_response")
            self.identity = payload["__identity__"]
            self.resources = payload["resources"]
            return self.identity
        except BaseException:
            child.close()
            self.failures += 1
            self.close(retryable=True)
            parent.close()
            raise

    def request(self, payload, timeout_s, budget=None):
        if not self.lock.acquire(blocking=False):
            raise ModelWorkerError("model_worker_busy")
        try:
            with self._state_lock:
                if self._closed:
                    raise ModelWorkerError("model_worker_closed")
                ready = self.process is not None and self.process.is_alive()
                if not ready and self.process is not None:
                    self.failures += 1  # Unexpected exits also consume the restart allowance.
            if not ready:
                self.close(retryable=True)
                # Loading has its own limit and consumes the caller's job budget.
                # The existing failure cap prevents repeated restarts indefinitely.
                self.warmup(budget)
            deadline = time.monotonic() + timeout_s
            with gpu_slot(self.config.get("device", "cpu"), "online", lambda: _remaining(deadline, budget)):
                return self._request(payload, _remaining(deadline, budget), budget)
        finally:
            self.lock.release()

    def _request(self, payload, timeout_s, budget=None):
        try:
            deadline = time.monotonic() + timeout_s
            _remaining(deadline, budget)
            with self._state_lock:
                connection = self.connection
                if connection is None or self.process is None or not self.process.is_alive():
                    raise ModelWorkerError("model_worker_unavailable")
            send_message(connection, payload, deadline, budget)
            status, response = receive_message(connection, deadline, budget)
            _remaining(deadline, budget)
            if status != "ok":
                raise ModelWorkerError(f"model_inference_failed: {response}")
            self.resources = response["resources"]
            return response["__model_result__"]
        except (TimeoutError, InterruptedError, ModelStageTimeout):
            self.failures += 1
            self.close(retryable=True)
            raise
        except (OSError, EOFError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.failures += 1
            self.close(retryable=True)
            raise ModelWorkerError("model_worker_transport_failed") from exc

    def close(self, *, retryable=False):
        with self._state_lock:
            self._closed = self._closed or not retryable
            connection, process = self.connection, self.process
            self.connection = self.process = None
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            if process is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=.2)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=.2)
                if not process.is_alive():
                    process.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def normalized(vectors, dimension: int) -> np.ndarray:
    value = np.asarray(vectors, dtype=np.float32)
    if value.ndim != 2 or value.shape[1] != dimension or not np.isfinite(value).all():
        raise EmbeddingError("invalid_embedding_shape_or_values")
    norms = np.linalg.norm(value.astype(np.float64), axis=1, keepdims=True)
    if np.any(norms <= 1e-12):
        raise EmbeddingError("zero_embedding_vector")
    return np.ascontiguousarray(value / norms, dtype=np.float32)


def require_revision(revision: str) -> str:
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EmbeddingError("model_requires_immutable_40_character_revision")
    return revision


def bge_identity(config: Mapping) -> dict:
    revision = require_revision(config.get("revision", ""))
    model = config.get("model", "BAAI/bge-m3")
    if model != "BAAI/bge-m3":
        raise EmbeddingError("unsupported_embedding_model")
    dimension = int(config.get("dimension", 1024))
    if dimension != 1024:
        raise EmbeddingError("bge_m3_dimension_must_be_1024")
    dtype = config.get("dtype", "float32")
    if dtype not in {"float32", "float16", "bfloat16"}:
        raise EmbeddingError("unsupported_embedding_dtype")
    if config.get("device", "cpu") == "cpu" and dtype != "float32":
        raise EmbeddingError("cpu_embedding_requires_float32")
    return {"provider": "bge", "model": model, "revision": revision, "dimension": dimension,
            "normalize": True, "dtype": dtype, "max_input_tokens": int(config.get("max_input_tokens", 512)),
            "pooling": "cls", "quality_evaluation_eligible": True}


class FixtureEmbedding:
    """No text-to-vector heuristics: every requested vector must be in the fixture."""
    def __init__(self, config: Mapping):
        import json
        from pathlib import Path

        vectors = config.get("fixture_vectors")
        if vectors is None and config.get("fixture_path"):
            vectors = json.loads(Path(config["fixture_path"]).read_text())
        if not isinstance(vectors, dict) or not vectors:
            raise EmbeddingError("fixture_embedding_requires_explicit_vectors")
        self.dimension = int(config.get("dimension", len(next(iter(vectors.values())))))
        self._vectors = {k: normalized([v], self.dimension)[0] for k, v in vectors.items()}
        self.tokenizer = ConservativeTokenizer()
        self.identity = {"provider": "fixture", "model": "explicit-test-vectors",
                         "revision": digest(vectors), "dimension": self.dimension,
                         "normalize": True, "quality_evaluation_eligible": False}

    def encode(self, texts: Sequence[str], keys: Sequence[str] | None = None, *, budget=None) -> np.ndarray:
        if budget is not None:
            budget.check()
        result = []
        for i, text in enumerate(texts):
            key = text if text in self._vectors else (keys[i] if keys is not None else text)
            if key not in self._vectors:
                raise EmbeddingError(f"fixture_vector_missing: {key[:100]}")
            result.append(self._vectors[key])
        return np.asarray(result, dtype=np.float32).reshape((-1, self.dimension))

    def close(self):
        pass


class BGEEmbedding:
    """The dense branch of BGE-M3 is normalized XLM-R CLS, not mean pooling."""
    def __init__(self, config: Mapping):
        identity = bge_identity(config)
        revision = identity["revision"]
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise EmbeddingError("bge_requires_models_extra") from exc
        model_id = identity["model"]
        self.dimension = identity["dimension"]
        self.device = str(config.get("device", "cpu"))
        dtype_name = identity["dtype"]
        kwargs = {"revision": revision, "local_files_only": config.get("local_files_only", True),
                  "trust_remote_code": False}
        self._tokenizer = AutoTokenizer.from_pretrained(model_id, **kwargs)
        self._gpu_managed = config.get("_managed_gpu", False)
        with self._slot():
            self._model = AutoModel.from_pretrained(model_id, torch_dtype=getattr(torch, dtype_name),
                                                    **kwargs).to(self.device).eval()
        if self._model.config.hidden_size != self.dimension:
            raise EmbeddingError("embedding_model_dimension_mismatch")
        self.tokenizer = ModelTokenizer(self._tokenizer, f"{model_id}@{revision}")
        self.max_tokens = identity["max_input_tokens"]
        self.batch_size = int(config.get("batch_size", 8))
        self.cache_size = max(0, int(config.get("cache_size", 256)))
        self._cache = OrderedDict()
        self._lock = RLock()
        self.identity = identity

    def _slot(self):
        return nullcontext() if self._gpu_managed else gpu_slot(self.device, "offline")

    def encode(self, texts: Sequence[str], keys=None) -> np.ndarray:
        import torch

        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        with self._lock:
            output = []
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start:start + self.batch_size])
                pending = list(dict.fromkeys(text for text in batch if text not in self._cache))
                values = {}
                if pending:
                    if any(self.tokenizer.count(t, True) > self.max_tokens for t in pending):
                        raise EmbeddingError("embedding_input_exceeds_token_budget")
                    with self._slot():
                        inputs = self._tokenizer(pending, padding=True, truncation=False,
                                                  return_tensors="pt").to(self.device)
                        with torch.inference_mode():
                            encoded = self._model(**inputs).last_hidden_state[:, 0].float().cpu().numpy()
                    values = dict(zip(pending, normalized(encoded, self.dimension)))
                resolved = {text: self._cache[text] if text in self._cache else values[text]
                            for text in batch}
                for text in batch:
                    value = resolved[text]
                    output.append(value)
                    if self.cache_size:
                        self._cache.pop(text, None)
                        self._cache[text] = value
                        while len(self._cache) > self.cache_size:
                            self._cache.popitem(last=False)
                del values
            # Building the matrix from rows isolates the output from cached vectors.
            return np.ascontiguousarray(output, dtype=np.float32)

    def close(self):
        with self._lock:
            self._cache.clear()
            self._model = None
            if self.device.startswith("cuda"):
                import torch
                torch.cuda.empty_cache()


def make_embedding(settings: Mapping):
    config = settings.get("embedding", {})
    provider = config.get("provider", "none")
    if provider == "none":
        return None
    if provider == "fixture":
        if settings.get("profile") != "offline-fixture":
            raise EmbeddingError("fixture_provider_requires_offline_fixture_profile")
        return FixtureEmbedding(config)
    if provider == "bge":
        if settings.get("profile") == "lexical-only":
            raise EmbeddingError("lexical_only_profile_cannot_load_dense_model")
        return BGEEmbedding(config)
    raise EmbeddingError(f"unknown_embedding_provider: {provider}")


def configured_embedding_identity(settings: Mapping):
    """Read the configured identity without loading real model weights."""
    config = settings.get("embedding", {})
    if config.get("provider", "none") == "bge":
        if settings.get("profile") == "lexical-only":
            raise EmbeddingError("lexical_only_profile_cannot_load_dense_model")
        return bge_identity(config)
    provider = make_embedding(settings)  # Only none/explicit fixture; never a real model.
    return provider.identity if provider is not None else None


def _embedding_worker(connection, config):
    try:
        backend = BGEEmbedding(config)
        send_message(connection, ["ready", {"__identity__": backend.identity, "resources": model_resources(backend.device)}])
        while True:
            texts = receive_message(connection)
            if texts is None:
                break
            try:
                result = backend.encode(texts).tolist()
                send_message(connection, ["ok", {"__model_result__": result, "resources": model_resources(backend.device)}])
            except Exception as exc:
                send_message(connection, ["error", f"{type(exc).__name__}: {exc}"])
    except (OSError, ModelWorkerError):
        pass
    except Exception as exc:
        try:
            send_message(connection, ["error", f"{type(exc).__name__}: {exc}"])
        except OSError:
            pass
    finally:
        connection.close()


class BoundedEmbedding(ModelProcess):
    def __init__(self, config):
        require_revision(config.get("revision", ""))
        super().__init__(config, _embedding_worker)
        self.dimension = int(config.get("dimension", 1024))
        self.timeout = float(config.get("timeout_ms", 2000)) / 1000

    def warmup(self, budget=None):
        try:
            return super().warmup(budget)
        except ModelStageTimeout as exc:
            raise EmbeddingTimeout("embedding_load_timeout") from exc
        except ModelWorkerError as exc:
            raise EmbeddingError(str(exc)) from exc

    def encode(self, texts, keys=None, *, budget=None):
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        try:
            values = self.request(list(texts), self.timeout, budget)
            result = normalized(values, self.dimension)
            if result.shape[0] != len(texts):
                raise EmbeddingError("query_embedding_count_mismatch")
            return result
        except ModelStageTimeout as exc:
            raise EmbeddingTimeout("embedding_timeout") from exc
        except ModelWorkerError as exc:
            raise EmbeddingError(str(exc)) from exc


def make_online_embedding(settings: Mapping, budget=None):
    if settings.get("embedding", {}).get("provider", "none") != "bge":
        return make_embedding(settings)
    if settings.get("profile") == "lexical-only":
        raise EmbeddingError("lexical_only_profile_cannot_load_dense_model")
    bge_identity(settings["embedding"])
    result = BoundedEmbedding(settings["embedding"])
    result.warmup(budget)
    return result
