from __future__ import annotations

import asyncio
import hmac
import logging
import os
import threading
import resource
from contextlib import asynccontextmanager
from collections import Counter
from datetime import timedelta

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .contracts import DiagnosisRequest
from .diagnosis import DiagnosisService
from .settings import Settings
from .storage import IdempotencyConflict, QueueFullError, Repository, TERMINAL
from .telemetry import LogSource, import_replay
from .util import Budget, digest, iso, owner_lock, timestamp, utcnow
from .maintenance import disk_status, require_capacity
from .lifecycle import ShutdownDeadline

LOG = logging.getLogger(__name__)


class BodyLimitMiddleware:
    """Bound actual HTTP bytes, including chunked bodies, before route execution."""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            if len(body) + len(part) > 65536:
                response = JSONResponse(status_code=413, content={"detail": "request_body_too_large"})
                return await response(scope, receive, send)
            body.extend(part)
            if not message.get("more_body", False):
                break
        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class Runtime:
    def __init__(self, settings: Settings, repository: Repository):
        self.settings = settings
        self.repository = repository
        self.service = DiagnosisService(settings, repository)
        self.stop = asyncio.Event()
        self.active = None
        self.index_error = None
        self.index_lock = threading.RLock()
        self.preparing = Counter()
        self.index_recovery = None
        self.operations = set()
        self.disk = disk_status(settings.model_dump(mode="json"))

    async def call(self, function, *args):
        # Client cancellation must not hide a still-running native/threaded operation.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self.operations.add(task)
        def completed(result):
            self.operations.discard(result)
            if not result.cancelled():
                result.exception()  # Consume failures even if the HTTP caller disconnected.
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    def maintain(self):
        from .indexing import collect_generations
        self.repository.prune(self.settings.window.max_seconds + self.settings.window.allowed_lateness_seconds)
        with self.index_lock:
            pinned = self.repository.pinned_generations() | set(self.preparing)
            result = collect_generations(self.settings.model_dump(mode="json"), pinned)
        self.repository.count("index_generations_collected", len(result["removed"]))
        self.disk = disk_status(self.settings.model_dump(mode="json"))
        if self.disk["status"] != "ok":
            LOG.warning("disk_capacity_" + self.disk["status"])

    async def maintenance_loop(self):
        while not self.stop.is_set():
            try:
                await asyncio.to_thread(self.maintain)
            except Exception as exc:
                LOG.warning("maintenance_failed", extra={"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(self.stop.wait(), self.settings.maintenance.interval_s)
            except TimeoutError:
                pass

    def snapshot(self, generation=None, budget=None):
        if budget is None:
            self.index_lock.acquire()
        else:
            while not self.index_lock.acquire(timeout=min(.05, budget.check())):
                budget.check()
        try:
            return self._snapshot(generation, budget)
        finally:
            self.index_lock.release()

    def _snapshot(self, generation=None, budget=None):
        from .indexing import load_snapshot, recover_current, SnapshotError
        active_pointer = generation is None
        if generation is None:
            pointer = self.settings.data_dir / "indexes" / "CURRENT"
            try:
                if pointer.is_file() and not pointer.is_symlink():
                    generation = pointer.read_text().strip()
            except (OSError, ValueError):
                pass  # Let checked loading and recovery handle a damaged pointer.
        if generation in self.service.retrievers:
            if active_pointer:
                self.index_error = None
            return self.service.retrievers[generation].snapshot
        try:
            snap = load_snapshot(self.settings.model_dump(mode="json"), generation_id=generation)
        except (SnapshotError, OSError, ValueError):
            if not active_pointer:
                raise
            snap = recover_current(self.settings.model_dump(mode="json"))
            self.index_recovery = snap.recovery
            if snap.recovery:
                self.repository.count("index_recoveries")
                LOG.warning("index_recovered", extra={"generation_id": snap.generation_id})
        retriever = self.service.prepare(snap, budget)
        if active_pointer:
            self.index_error = None
        return retriever.snapshot

    def submit(self, request: DiagnosisRequest, key=None):
        # Pinning a job and retiring old model instances share the publication lock.
        with self.index_lock:
            return self._submit(request, key)

    def _submit(self, request: DiagnosisRequest, key=None):
        if self.stop.is_set():
            raise RuntimeError("service_stopping")
        # Idempotency compares the caller's normalized payload, before clock-based
        # defaults. A retry without windows reuses the first job's pinned window;
        # explicitly supplying a window is a different payload, even if equivalent.
        original_hash = digest(request.model_dump(mode="json"))
        req = request.model_copy(deep=True)
        now = utcnow()
        if req.report_type == "incident_diagnosis":
            req.window_end = req.window_end or now - timedelta(seconds=self.settings.window.allowed_lateness_seconds)
            req.window_start = req.window_start or req.window_end - timedelta(seconds=self.settings.window.default_seconds)
            duration = (req.window_end-req.window_start).total_seconds()
            if not self.settings.window.min_seconds <= duration <= self.settings.window.max_seconds:
                raise ValueError("window_outside_configured_bounds")
            if req.window_end > now:
                raise ValueError("future_window")
            if req.data_namespace.startswith("replay:"):
                lo, hi = self.repository.namespace_bounds(req.data_namespace)
                if lo is None:
                    raise ValueError("replay_namespace_not_found")
                received = req.as_of_received_at or timestamp(hi)
                # A virtual query can occur after the final input; never before the replay begins.
                if received < timestamp(lo):
                    raise ValueError("replay_cutoff_precedes_first_record")
            else:
                received = now
        else:
            received = now
        cutoff = req.knowledge_cutoff or now
        if cutoff > now:
            raise ValueError("future_knowledge_cutoff")
        try:
            snap = self.snapshot()
        except (FileNotFoundError, ValueError, RuntimeError, ImportError) as exc:
            self.index_error = type(exc).__name__
            raise RuntimeError("knowledge_index_unavailable") from exc
        epoch, _ = self.repository.revocations()
        pinned = {"ingest_revision": self.repository.revision(), "as_of_received_at": iso(received),
                  "knowledge_cutoff": iso(cutoff), "generation_id": snap.manifest["generation_id"],
                  "config_hash": self.settings.fingerprint, "revocation_epoch": epoch}
        pinned["rule_set_hash"] = self.service.rule_engine.fingerprint
        return self.repository.submit(req, pinned, self.settings.service.queue_capacity, key=key, request_hash=original_hash)

    def reap(self):
        """Release old model instances and trim the session's completed results."""
        self.repository.prune(self.settings.window.max_seconds + self.settings.window.allowed_lateness_seconds)
        with self.index_lock:
            pinned = self.repository.pinned_generations()
            pinned.update(self.preparing)
            pointer = self.settings.data_dir / "indexes" / "CURRENT"
            if pointer.is_file():
                pinned.add(pointer.read_text().strip())
            for generation in list(self.service.retrievers):
                if generation not in pinned:
                    retriever = self.service.retrievers.pop(generation)
                    retriever.close()

    def publish(self, label):
        from .indexing import load_snapshot, publish_index
        # Loading a new model must not block requests against CURRENT.
        snapshot = load_snapshot(self.settings.model_dump(mode="json"), generation_id=label)
        generation = snapshot.generation_id
        with self.index_lock:
            self.preparing[generation] += 1
        try:
            retriever = self.service.prepare(snapshot)
            identity = retriever.embedding.identity if retriever.embedding is not None else None
            with self.index_lock:
                manifest = publish_index(self.settings.model_dump(mode="json"), generation,
                                         prepared_embedding_identity=identity)
                self.index_error = None
                return manifest
        finally:
            with self.index_lock:
                self.preparing[generation] -= 1
                if not self.preparing[generation]:
                    del self.preparing[generation]
                self.reap()

    async def worker(self):
        while not self.stop.is_set():
            job = None
            try:
                job = self.repository.claim(self.settings.service.queue_timeout_s)
                if job is not None:
                    self.active = job["job_id"]
                    try:
                        elapsed = (utcnow() - timestamp(job["created_at"])).total_seconds()
                        seconds = max(0.0, min(self.settings.service.pipeline_timeout_s,
                                              self.settings.service.job_timeout_s - elapsed))
                        budget = Budget(seconds, lambda: self.stop.is_set() or
                                        self.repository.cancel_requested(job["job_id"]))
                        # The total deadline includes queue time. Expired work must
                        # not load a model or enter the diagnosis pipeline.
                        budget.check()
                        generation = job["pinned"].get("generation_id")
                        snap = await asyncio.to_thread(self.snapshot, generation, budget) if generation else None
                        await asyncio.to_thread(self.service.run, job, snap, budget)
                        self.repository.count("diagnoses_succeeded")
                    except InterruptedError:
                        self.repository.finish_error(job["job_id"], "cancelled", "cancelled")
                        self.repository.count("diagnoses_cancelled")
                        LOG.info("diagnosis_cancelled", extra={"job_id": job["job_id"]})
                    except TimeoutError:
                        self.repository.finish_error(job["job_id"], "timed_out", "deadline_exceeded")
                        self.repository.count("diagnoses_timed_out")
                        LOG.warning("diagnosis_timed_out", extra={"job_id": job["job_id"]})
                    except Exception as exc:
                        LOG.exception("diagnosis_failed", extra={"job_id": job["job_id"]})
                        self.repository.finish_error(job["job_id"], "failed", type(exc).__name__ + ":" + str(exc)[:200])
                        self.repository.count("diagnoses_failed")
                    finally:
                        self.active = None
                        await asyncio.to_thread(self.reap)
                    continue
            except Exception:
                # Claim, error persistence and resource cleanup can also fail.
                # Keep the worker alive, log the failure and retry without spinning.
                LOG.exception("diagnosis_worker_iteration_failed", extra={"job_id": self.active or
                              (job.get("job_id") if job else None)})
                self.active = None
            try:
                await asyncio.wait_for(self.stop.wait(), .1)
            except TimeoutError:
                pass


def create_app(settings: Settings | None = None, *, replays=()):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        from .providers.generation import generation_key
        generation_key(settings.generation)
        with owner_lock(settings.data_dir):
            require_capacity(settings.model_dump(mode="json"))
            repository = Repository(settings.data_dir / "knowledge.sqlite")
            try:
                for path, replay_id in replays:
                    import_replay(repository, path, replay_id)
            except BaseException:
                repository.close()
                raise
            runtime = Runtime(settings, repository)
            app.state.runtime = runtime
            try:
                await asyncio.to_thread(runtime.snapshot)
            except (FileNotFoundError, ValueError, RuntimeError, ImportError) as exc:
                runtime.index_error = type(exc).__name__
            worker = asyncio.create_task(runtime.worker())
            maintenance = asyncio.create_task(runtime.maintenance_loop())
            telemetry = asyncio.create_task(LogSource(settings.telemetry, repository).run()) if settings.telemetry.source == "logs" else None
            try:
                yield
            finally:
                with ShutdownDeadline(settings.service.shutdown_timeout_s):
                    runtime.stop.set()
                    if telemetry:
                        telemetry.cancel()
                        await asyncio.gather(telemetry, return_exceptions=True)
                    await worker
                    await maintenance
                    if runtime.operations:
                        await asyncio.gather(*runtime.operations, return_exceptions=True)
                    runtime.service.close()
                    repository.close()

    app = FastAPI(title="Network Diagnosis RAG", version="0.1.0", lifespan=lifespan)

    app.add_middleware(BodyLimitMiddleware)

    def runtime():
        return app.state.runtime

    def authorize_admin(token):
        expected = os.environ.get(settings.service.admin_token_env, "")
        if not expected or not token or not hmac.compare_digest(expected, token):
            raise HTTPException(403, "admin_token_required")

    @app.post("/v1/diagnoses", status_code=202)
    async def submit(request: DiagnosisRequest, idempotency_key: str | None = Header(default=None)):
        if idempotency_key and len(idempotency_key) > 128:
            raise HTTPException(422, "idempotency_key_too_long")
        try:
            result = await runtime().call(runtime().submit, request, idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except QueueFullError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"job_id": result["job_id"], "status": result["status"], "status_url": "/v1/diagnoses/" + result["job_id"]}

    @app.get("/v1/diagnoses/{job_id}")
    def get_job(job_id: str):
        value = runtime().repository.job(job_id)
        if not value:
            raise HTTPException(404, "job_not_found")
        if value["report_id"]:
            value["report"] = runtime().repository.report(value["report_id"])
        return value

    @app.post("/v1/diagnoses/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = runtime().repository.cancel(job_id)
        if not job:
            raise HTTPException(404, "job_not_found")
        return JSONResponse(status_code=200 if job["status"] in TERMINAL else 202,
                            content={"job_id": job_id, "status": job["status"]})

    @app.get("/v1/reports/{report_id}")
    def report(report_id: str):
        result = runtime().repository.report(report_id)
        if result is None:
            raise HTTPException(404, "report_not_found")
        return result

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        value = capabilities()
        ready = value["index_ready"] and value["generation_credentials_present"] and value["disk"]["status"] != "blocked"
        return JSONResponse(status_code=200 if ready else 503, content=value)

    @app.get("/v1/capabilities")
    def capabilities():
        rt = runtime()
        pointer = settings.data_dir / "indexes" / "CURRENT"
        generation = pointer.read_text().strip() if pointer.is_file() else None
        retriever = rt.service.retrievers.get(generation)
        from .providers.generation import generation_key, ProviderUnavailable
        from .providers.decision import decision_capabilities
        try:
            generation_key(settings.generation)
            credentials_present = True
        except ProviderUnavailable:
            credentials_present = False
        return {"profile": settings.profile, "index_ready": retriever is not None and rt.index_error is None,
                "index_error": rt.index_error, "embedding": settings.embedding.provider,
                "generation_id": generation,
                "actual_retrieval_mode": retriever.mode if retriever else None,
                "retrieval_degradations": retriever.degradations if retriever else [],
                "generation": settings.generation.provider,
                "generation_credentials_present": credentials_present,
                "decision": decision_capabilities(settings.decision), "report_schema_version": 2,
                "index_recovery": rt.index_recovery, "disk": rt.disk,
                "telemetry_source": settings.telemetry.source, "source_health": rt.repository.source_health(),
                "runtime_storage": "memory", "history_persistent": False}

    @app.get("/v1/metrics")
    def metrics():
        rt = runtime()
        with rt.index_lock:
            retrievers = list(rt.service.retrievers.values())
            indexes = {retriever.snapshot.generation_id: {
                name: value for name, value in retriever.snapshot.manifest.get("build", {}).items()
                if name != "elapsed_ms"} for retriever in retrievers}
        resources = {name + ":" + str(i): getattr(provider, "resources", {})
                     for i, retriever in enumerate(retrievers)
                     for name, provider in (("embedding", retriever.embedding), ("reranker", retriever.reranker))
                     if provider is not None}
        return {**rt.repository.metrics(), "disk": rt.disk, "indexes": indexes,
                "resources": {"service_rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                              "models": resources}}

    @app.post("/v1/admin/indexes/publish")
    async def publish(payload: dict, x_admin_token: str | None = Header(default=None)):
        authorize_admin(x_admin_token)
        label = payload.get("generation_id") or payload.get("label")
        if not isinstance(label, str):
            raise HTTPException(422, "generation_id_or_label_required")
        try:
            # Preload and warm the candidate while CURRENT still points to the
            # usable generation. Publication follows successful preparation.
            manifest = await runtime().call(runtime().publish, label)
            return manifest
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/admin/knowledge/revoke")
    def revoke(payload: dict, x_admin_token: str | None = Header(default=None)):
        authorize_admin(x_admin_token)
        key = payload.get("key")
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise HTTPException(422, "invalid_document_or_revision_id")
        return {"revocation_epoch": runtime().repository.revoke(key)}

    return app
