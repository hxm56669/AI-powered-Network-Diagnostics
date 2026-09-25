"""Pinned cross-encoder in a bounded, killable worker. Scores are not probabilities."""
from __future__ import annotations

import math
from collections.abc import Mapping

from ..knowledge.common import KnowledgeError
from .embedding import (ModelProcess, ModelStageTimeout, ModelWorkerError, receive_message,
                        require_revision, send_message, model_resources)


class RerankerError(KnowledgeError):
    pass


class RerankerTimeout(RerankerError):
    pass


class BGEReranker:
    def __init__(self, config: Mapping):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        model_id = config.get("model", "BAAI/bge-reranker-v2-m3")
        if model_id != "BAAI/bge-reranker-v2-m3":
            raise RerankerError("unsupported_reranker_model")
        revision = require_revision(config.get("revision", ""))
        kwargs = {"revision": revision, "trust_remote_code": False,
                  "local_files_only": config.get("local_files_only", True)}
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, **kwargs)
        self.device = config.get("device", "cpu")
        dtype = torch.float32 if self.device == "cpu" else torch.float16
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_id, torch_dtype=dtype, **kwargs).to(self.device).eval()
        self.max_tokens = int(config.get("max_pair_tokens", 768))
        self.query_tokens = min(int(config.get("max_query_tokens", 192)), self.max_tokens // 2)
        self.batch_size = int(config.get("batch_size", 8))

    def score(self, query: str, candidates: list[dict]) -> list[dict]:
        import torch

        original_q = self.tokenizer.encode(query, add_special_tokens=False)
        query_ids = original_q[:self.query_tokens]
        query_tokens = set(query_ids)
        features, trace = [], []
        for candidate in candidates:
            meta = candidate.get("metadata", {})
            prefix = candidate.get("title", "") + "\n" + meta.get("safety_context", "") + "\n"
            prefix_ids = self.tokenizer.encode(prefix, add_special_tokens=False)
            room = self.max_tokens - len(query_ids) - self.tokenizer.num_special_tokens_to_add(pair=True)
            if len(prefix_ids) >= room:
                raise RerankerError("rerank_safety_context_exceeds_budget")
            body = candidate.get("body_text", candidate["text"])
            body_ids = self.tokenizer.encode(body, add_special_tokens=False)
            remaining = room - len(prefix_ids)
            # Seek the earliest query token match, keeping safety text intact.
            match = next((i for i, token in enumerate(body_ids) if token in query_tokens), 0)
            start = max(0, min(match - remaining // 3, len(body_ids) - remaining))
            passage_ids = prefix_ids + body_ids[start:start + remaining]
            features.append(self.tokenizer.prepare_for_model(
                query_ids, pair_ids=passage_ids, add_special_tokens=True, truncation=False,
                return_attention_mask=True))
            trace.append({"query_truncated": len(original_q) > len(query_ids),
                          "passage_truncated": len(body_ids) > remaining,
                          "body_token_start": start, "pair_tokens": len(features[-1]["input_ids"])})
        scores = []
        for start in range(0, len(features), self.batch_size):
            inputs = self.tokenizer.pad(features[start:start + self.batch_size],
                                        padding=True, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                scores.extend(self.model(**inputs).logits.float().reshape(-1).cpu().tolist())
        if len(scores) != len(candidates) or not all(math.isfinite(x) for x in scores):
            raise RerankerError("invalid_reranker_scores")
        return [{"score": score, "truncation": item} for score, item in zip(scores, trace)]


def _worker(connection, config):
    try:
        backend = BGEReranker(config)
        send_message(connection, ["ready", {"__identity__": {"model": config.get("model", "BAAI/bge-reranker-v2-m3"),
                                             "revision": config["revision"]}, "resources": model_resources(backend.device)}])
        while True:
            message = receive_message(connection)
            if message is None:
                break
            query, candidates = message
            try:
                result = backend.score(query, candidates)
                send_message(connection, ["ok", {"__model_result__": result, "resources": model_resources(backend.device)}])
            except Exception as exc:
                send_message(connection, ["error", f"{type(exc).__name__}: {exc}"])
    except (OSError, ModelWorkerError):
        pass
    except Exception as exc:
        try:
            send_message(connection, ["error", f"{type(exc).__name__}: {exc}"])
        except (BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class BoundedReranker(ModelProcess):
    """One in-flight job, no hidden queue; timeout terminates the actual inference process."""
    def __init__(self, config: Mapping):
        require_revision(config.get("revision", ""))
        super().__init__(config, _worker)
        self.timeout = float(config.get("timeout_ms", 2000)) / 1000

    def warmup(self, budget=None):
        try:
            return super().warmup(budget)
        except ModelStageTimeout as exc:
            raise RerankerTimeout("reranker_load_timeout") from exc
        except ModelWorkerError as exc:
            raise RerankerError(str(exc)) from exc

    def score(self, query: str, candidates: list[dict], *, budget=None) -> list[dict]:
        try:
            return self.request([query, candidates], self.timeout, budget)
        except ModelStageTimeout as exc:
            raise RerankerTimeout("rerank_timeout") from exc
        except ModelWorkerError as exc:
            raise RerankerError(str(exc)) from exc


class FixtureReranker:
    def __init__(self, scores: dict):
        self.scores = scores

    def score(self, query: str, candidates: list[dict], *, budget=None) -> list[dict]:
        if budget is not None:
            budget.check()
        result = []
        for candidate in candidates:
            key = candidate["chunk_id"] if candidate["chunk_id"] in self.scores else candidate["doc_id"]
            if key not in self.scores:
                raise RerankerError(f"fixture_rerank_score_missing: {key}")
            score = float(self.scores[key])
            if not math.isfinite(score):
                raise RerankerError("invalid_fixture_rerank_score")
            result.append({"score": score, "truncation": {}, "fixture": True})
        return result

    def close(self):
        pass


def make_reranker(settings: Mapping, budget=None):
    config = settings.get("reranker", {})
    if not config.get("enabled", False):
        return None
    if "fixture_scores" in config:
        if settings.get("profile") != "offline-fixture":
            raise RerankerError("fixture_provider_requires_offline_fixture_profile")
        return FixtureReranker(config["fixture_scores"])
    result = BoundedReranker(config)
    result.warmup(budget)
    return result
