"""Session-only SQLite state; only knowledge revocations survive a restart."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from ..contracts import DiagnosisRequest, TelemetryBatch, TelemetrySnapshot
from ..knowledge.common import citation_is_revoked
from ..util import canonical, digest, iso, utcnow

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
LOG = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    pass


class IdempotencyConflict(ValueError):
    pass


class TelemetryConflictError(ValueError):
    pass


class Repository:
    """Keep logs, jobs and reports in RAM; attach a knowledge-only state file."""

    def __init__(self, knowledge_path: Path | None = None):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA temp_store=MEMORY")
            if knowledge_path is not None:
                knowledge_path.parent.mkdir(parents=True, exist_ok=True)
            self.db.execute("ATTACH DATABASE ? AS knowledge", (str(knowledge_path) if knowledge_path else ":memory:",))
            self.db.executescript("""
              CREATE TABLE ingest_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL);
              CREATE TABLE telemetry_batches(namespace TEXT, batch_id TEXT, payload TEXT, revision INTEGER,received_at TEXT,
                PRIMARY KEY(namespace,batch_id));
              CREATE TABLE observations(namespace TEXT, sample_id TEXT, host_id TEXT, observed_at TEXT,
                received_at TEXT, revision INTEGER, metric TEXT, payload TEXT, identity TEXT,
                PRIMARY KEY(namespace,sample_id));
              CREATE INDEX observation_window ON observations(namespace,host_id,observed_at,metric);
              CREATE TABLE events(namespace TEXT,event_id TEXT,host_id TEXT,observed_at TEXT,received_at TEXT,
                revision INTEGER,payload TEXT,identity TEXT,PRIMARY KEY(namespace,event_id));
              CREATE INDEX event_window ON events(namespace,host_id,observed_at);
              CREATE INDEX event_revision ON events(namespace,revision);
              CREATE TABLE quarantine(id INTEGER PRIMARY KEY,created_at TEXT,reason TEXT,payload TEXT);
              CREATE TABLE source_health(source TEXT PRIMARY KEY,updated_at TEXT,payload TEXT);
              CREATE TABLE counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE metric_distributions(name TEXT PRIMARY KEY,count INTEGER NOT NULL,total REAL NOT NULL,
                minimum REAL NOT NULL,maximum REAL NOT NULL,buckets TEXT NOT NULL);
              CREATE TABLE jobs(job_id TEXT PRIMARY KEY,request TEXT,request_hash TEXT,idempotency_key TEXT,
                caller TEXT,created_at TEXT,started_at TEXT,status,stage,pinned,error,report_id,
                diagnostics TEXT NOT NULL DEFAULT '{}');
              CREATE INDEX job_idempotency ON jobs(caller,idempotency_key,created_at);
              CREATE TABLE reports(report_id TEXT PRIMARY KEY,job_id TEXT UNIQUE,created_at TEXT,payload TEXT);
              CREATE TABLE IF NOT EXISTS knowledge.revocations(key TEXT PRIMARY KEY,epoch INTEGER,created_at TEXT);
              CREATE TABLE IF NOT EXISTS knowledge.control(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
              INSERT OR IGNORE INTO knowledge.control VALUES('revocation_epoch',0);
            """)
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def count(self, name: str, amount=1):
        with self.transaction() as db:
            db.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                       (name, amount))

    def metrics(self):
        with self.lock:
            result = dict(self.db.execute("SELECT name,value FROM counters"))
            result["queue"] = dict(self.db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
            result["distributions"] = {r[0]: {"count": r[1], "sum": r[2], "min": r[3], "max": r[4],
                                             "buckets": json.loads(r[5])}
                                       for r in self.db.execute("SELECT * FROM metric_distributions")}
            return result

    def observe(self, name, value):
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid_metric_value")
        # Fixed buckets avoid unbounded per-request samples in the database.
        boundaries = (0, .1, .5, 1, 5, 10, 50, 100, 500, 1000, 5000, 10000, 30000, 60000)
        bucket = next((str(x) for x in boundaries if value <= x), "+Inf")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM metric_distributions WHERE name=?", (name,)).fetchone()
            counts = json.loads(row[5]) if row else {}
            counts[bucket] = counts.get(bucket, 0) + 1
            db.execute("INSERT OR REPLACE INTO metric_distributions VALUES(?,?,?,?,?,?)", (
                name, row[1] + 1 if row else 1, row[2] + value if row else value,
                min(row[3], value) if row else value, max(row[4], value) if row else value, canonical(counts)))

    def health(self, source: str, payload: dict):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO source_health VALUES(?,?,?)", (source, iso(utcnow()), canonical(payload)))

    def source_health(self):
        with self.lock:
            return {r["source"]: {"updated_at": r["updated_at"], **json.loads(r["payload"])}
                    for r in self.db.execute("SELECT * FROM source_health")}

    def quarantine(self, reason: str, payload: str):
        with self.transaction() as db:
            db.execute("INSERT INTO quarantine(created_at,reason,payload) VALUES(?,?,?)",
                       (iso(utcnow()), reason[:500], payload[:262144]))

    def append(self, batch: TelemetryBatch, namespace="live", received_at=None) -> dict:
        return self._append(batch.model_dump(mode="json"), namespace, received_at)

    def append_snapshot(self, snapshot: TelemetrySnapshot, namespace="live", received_at=None):
        value = snapshot.model_dump(mode="json")
        self.health(f"{namespace}/{snapshot.host_id}", snapshot.source_health)
        return self._append(value, namespace, received_at)

    def _append(self, value, namespace, received_at):
        received = iso(received_at or utcnow())
        identity = {k: value[k] for k in ("host_id", "boot_id", "collector_session_id", "netns_id")}
        bid = digest({**identity, "stream": value.get("stream_id", "snapshot"),
                      "sequence": value.get("sequence", value.get("captured_at"))})
        text = canonical(value)
        identity_json = canonical(identity)
        try:
            with self.transaction() as db:
                previous = db.execute("SELECT payload,revision FROM telemetry_batches WHERE namespace=? AND batch_id=?",
                                      (namespace, bid)).fetchone()
                if previous:
                    if previous["payload"] != text:
                        raise TelemetryConflictError("batch_id_payload_conflict")
                    return {"revision": previous["revision"], "inserted": 0, "duplicates": 1}
                rev = db.execute("INSERT INTO ingest_revisions(created_at) VALUES(?)", (iso(utcnow()),)).lastrowid
                inserted = duplicates = 0
                for table, field, items in [("observations", "sample_id", value.get("observations", [])),
                                             ("events", "event_id", value.get("events", []))]:
                    for item in items:
                        previous = db.execute(f"SELECT payload,identity FROM {table} WHERE namespace=? AND {field}=?",
                                              (namespace, item[field])).fetchone()
                        payload = canonical(item)
                        if previous:
                            if previous["payload"] != payload or previous["identity"] != identity_json:
                                raise TelemetryConflictError(f"{field}_payload_conflict")
                            duplicates += 1
                            continue
                        common = (namespace, item[field], identity["host_id"], iso(item["observed_at"]), received, rev)
                        if table == "observations":
                            db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
                                       (*common, item["metric"], payload, identity_json))
                        else:
                            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)", (*common, payload, identity_json))
                        inserted += 1
                db.execute("INSERT INTO telemetry_batches VALUES(?,?,?,?,?)", (namespace, bid, text, rev, received))
            self.count("telemetry_inserted", inserted)
            self.count("telemetry_duplicates", duplicates)
            return {"revision": rev, "inserted": inserted, "duplicates": duplicates}
        except ValueError as exc:
            self.quarantine(str(exc), text)
            raise

    def revision(self):
        with self.lock:
            return self.db.execute("SELECT COALESCE(MAX(id),0) FROM ingest_revisions").fetchone()[0]

    def namespace_bounds(self, namespace):
        with self.lock:
            row = self.db.execute("SELECT MIN(received_at),MAX(received_at) FROM (SELECT received_at FROM observations WHERE namespace=? UNION ALL SELECT received_at FROM events WHERE namespace=?)",
                                  (namespace, namespace)).fetchone()
            return tuple(row)

    def window(self, request: DiagnosisRequest, revision: int, received_at):
        params = (request.data_namespace, request.host_id, iso(request.window_start), iso(request.window_end),
                  revision, iso(received_at))
        with self.lock:
            rows = self.db.execute("""SELECT payload,identity FROM observations WHERE namespace=? AND host_id=?
                AND observed_at>=? AND observed_at<=? AND revision<=? AND received_at<=?
                ORDER BY observed_at,sample_id""", params).fetchall()
            events = self.db.execute("""SELECT payload,identity FROM events WHERE namespace=? AND host_id=?
                AND observed_at>=? AND observed_at<? AND revision<=? AND received_at<=?
                ORDER BY observed_at,event_id""", params).fetchall()
            # A batch can be printed after the selected sample window ends.
            # Its raw log still supports those samples; fetch only provenance
            # from their pinned ingestion revisions, never later measurements.
            references = self.db.execute("""SELECT payload,identity FROM events
                WHERE namespace=? AND host_id=? AND revision IN (
                    SELECT revision FROM observations WHERE namespace=? AND host_id=?
                    AND observed_at>=? AND observed_at<=? AND revision<=? AND received_at<=?
                    UNION SELECT revision FROM events WHERE namespace=? AND host_id=?
                    AND observed_at>=? AND observed_at<? AND revision<=? AND received_at<=?)
                AND json_extract(payload,'$.event_type')='service_log'
                AND json_extract(payload,'$.details.format')='telemetry_v1'
                ORDER BY observed_at,event_id""", (*params[:2], *params, *params)).fetchall()
        def unpack(row):
            return {**json.loads(row["payload"]), "_identity": json.loads(row["identity"])}
        observations = [unpack(r) for r in rows]
        events = list({value["event_id"]: value for value in map(unpack, [*events, *references])}.values())
        if request.interfaces:
            def permitted(item):
                return item["scope"]["kind"] == "host_netns" or item["scope"].get("interface") in request.interfaces
            observations = list(filter(permitted, observations))
            events = list(filter(permitted, events))
        return observations, events

    def export_batches(self, namespace):
        with self.lock:
            rows = self.db.execute("SELECT payload,received_at FROM telemetry_batches WHERE namespace=? ORDER BY revision", (namespace,)).fetchall()
        records = []
        for row in rows:
            payload = json.loads(row["payload"])
            records.append({"snapshot" if "captured_at" in payload else "batch": payload,
                            "received_at": row["received_at"]})
        return records

    def submit(self, request: DiagnosisRequest, pinned: dict, capacity: int, key=None, caller="local", request_hash=None):
        now = utcnow()
        value = request.model_dump(mode="json")
        request_json = canonical(value)
        request_hash = request_hash or digest(value)
        with self.transaction() as db:
            if key:
                found = db.execute("SELECT * FROM jobs WHERE caller=? AND idempotency_key=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                                   (caller, key, iso(now - timedelta(hours=24)))).fetchone()
                if found:
                    if found["request_hash"] != request_hash:
                        raise IdempotencyConflict("idempotency_key_content_mismatch")
                    return self._job(found)
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            if queued >= capacity:
                raise QueueFullError("diagnosis_queue_full")
            job_id = str(uuid.uuid4())
            db.execute("INSERT INTO jobs(job_id,request,request_hash,idempotency_key,caller,created_at,started_at,status,stage,pinned,error,report_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                job_id, request_json, request_hash, key, caller, iso(now), None, "queued", "queued",
                canonical(pinned), None, None))
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job(row)

    @staticmethod
    def _job(row):
        value = dict(row)
        for key in ("request", "pinned", "diagnostics"):
            value[key] = json.loads(value[key])
        value.pop("request_hash", None)
        return value

    def job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job(row) if row else None

    def cancel_requested(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return row is None or row[0] in {"cancelling", "cancelled"}

    def record_diagnostics(self, job_id, **sections):
        """Bounded receipts survive failed/cancelled jobs; never store prompts or raw model text."""
        with self.transaction() as db:
            row = db.execute("SELECT diagnostics FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("diagnostic_job_missing")
            data = canonical({**json.loads(row[0]), **sections})
            if len(data.encode()) > 65536:
                raise ValueError("job_diagnostics_too_large")
            db.execute("UPDATE jobs SET diagnostics=? WHERE job_id=?", (data, job_id))

    def pinned_generations(self):
        with self.lock:
            pinned = (json.loads(row[0]) for row in self.db.execute(
                "SELECT pinned FROM jobs WHERE status IN ('queued','running','cancelling')"))
            return {value["generation_id"] for value in pinned if value.get("generation_id")}

    def claim(self, queue_timeout):
        with self.transaction() as db:
            cutoff = iso(utcnow() - timedelta(seconds=queue_timeout))
            db.execute("UPDATE jobs SET status='timed_out',stage='finished',error='queue_timeout' WHERE status='queued' AND created_at<?", (cutoff,))
            while True:
                row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if not row:
                    return None
                try:
                    job = self._job(row)
                except (ValueError, TypeError, AttributeError) as exc:
                    # A corrupt payload must not roll back to the head of the queue
                    # on every claim and starve otherwise valid work.
                    db.execute("UPDATE jobs SET status='failed',stage='finished',error='invalid_job_payload' WHERE job_id=?",
                               (row["job_id"],))
                    LOG.warning("invalid_job_payload", extra={"job_id": row["job_id"],
                                "error_type": type(exc).__name__, "stage": "claim"})
                    continue
                job.update(status="running", stage="evidence", started_at=iso(utcnow()))
                db.execute("UPDATE jobs SET status='running',stage='evidence',started_at=? WHERE job_id=? AND status='queued'",
                           (job["started_at"], job["job_id"]))
                return job

    def stage(self, job_id, stage):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET stage=? WHERE job_id=? AND status='running'", (stage, job_id))

    def cancel(self, job_id):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET status=CASE status WHEN 'queued' THEN 'cancelled' ELSE 'cancelling' END WHERE job_id=? AND status IN ('queued','running')", (job_id,))
        return self.job(job_id)

    def finish_error(self, job_id, status, error):
        assert status in TERMINAL - {"succeeded"}
        with self.transaction() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN status='cancelling' THEN 'cancelled' ELSE ? END,stage='finished',error=? WHERE job_id=? AND status IN ('queued','running','cancelling')", (status, error, job_id))

    def commit_report(self, job_id, report: dict, epoch: int, deadline_check):
        with self.transaction() as db:
            deadline_check()
            if db.execute("SELECT value FROM control WHERE key='revocation_epoch'").fetchone()[0] != epoch:
                raise ValueError("revocation_changed_during_diagnosis")
            row = db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row[0] != "running":
                raise InterruptedError("cancelled_or_finished")
            db.execute("INSERT INTO reports VALUES(?,?,?,?)", (report["report_id"], job_id, iso(utcnow()), canonical(report)))
            db.execute("UPDATE jobs SET status='succeeded',stage='finished',report_id=? WHERE job_id=?", (report["report_id"], job_id))

    def report(self, report_id):
        with self.lock:
            row = self.db.execute("SELECT payload FROM reports WHERE report_id=?", (report_id,)).fetchone()
        if not row:
            return None
        report = json.loads(row[0])
        _, revoked = self.revocations()
        for citation in report["citations"]:
            citation["revoked"] = citation_is_revoked(citation, revoked)
        return report

    def revoke(self, key):
        with self.transaction() as db:
            db.execute("UPDATE control SET value=value+1 WHERE key='revocation_epoch'")
            epoch = db.execute("SELECT value FROM control WHERE key='revocation_epoch'").fetchone()[0]
            db.execute("INSERT OR REPLACE INTO revocations VALUES(?,?,?)", (key, epoch, iso(utcnow())))
        return epoch

    def revocations(self):
        with self.lock:
            epoch = self.db.execute("SELECT value FROM control WHERE key='revocation_epoch'").fetchone()[0]
            return epoch, {r[0] for r in self.db.execute("SELECT key FROM revocations")}

    def prune(self, window_seconds):
        """Bound the live window and recent results without losing active inputs."""
        with self.transaction() as db:
            cutoff = iso(utcnow() - timedelta(seconds=window_seconds))
            for row in db.execute("SELECT request FROM jobs WHERE status IN ('queued','running','cancelling')"):
                request = json.loads(row[0])
                if request["data_namespace"] == "live" and request.get("window_start"):
                    cutoff = min(cutoff, iso(request["window_start"]))
            db.execute("DELETE FROM observations WHERE namespace='live' AND received_at<?", (cutoff,))
            db.execute("DELETE FROM events WHERE namespace='live' AND received_at<?", (cutoff,))
            db.execute("DELETE FROM telemetry_batches WHERE namespace='live' AND revision NOT IN (SELECT revision FROM observations UNION SELECT revision FROM events)")
            db.execute("DELETE FROM quarantine WHERE created_at<?", (cutoff,))
            db.execute("""DELETE FROM jobs WHERE job_id IN (
                SELECT job_id FROM jobs WHERE status IN ('succeeded','failed','cancelled','timed_out')
                ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET 64)""")
            db.execute("DELETE FROM reports WHERE job_id NOT IN (SELECT job_id FROM jobs)")
            db.execute("""DELETE FROM ingest_revisions WHERE id < (SELECT MAX(id) FROM ingest_revisions)
                AND id NOT IN (SELECT revision FROM telemetry_batches)""")
