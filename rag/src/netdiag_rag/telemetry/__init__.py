"""Capture the diagnostic service console; persist raw logs and support deterministic replay."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from pathlib import Path

from ..contracts import Event, Scope, TelemetryBatch, TelemetrySnapshot
from ..storage import TelemetryConflictError
from ..util import atomic_write, canonical, digest, timestamp, utcnow

STRUCTURED_LOG_PREFIX = "NETDIAG_TELEMETRY "
# The C++ exporter caps JSON batches at 256 KiB, excluding the console prefix.
MAX_LOG_LINE_BYTES = 256 * 1024 + len(STRUCTURED_LOG_PREFIX)


def import_replay(repository, path: Path, replay_id: str, preserve_received_time=True):
    if not replay_id or any(c in replay_id for c in "/\\\x00"):
        raise ValueError("invalid_replay_id")
    namespace = "replay:" + replay_id
    source_text = path.read_text()
    # Validate the entire file before writing: malformed input does not leave a half-import.
    records = []
    for number, line in enumerate(source_text.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if "batch" in value:
            payload, model = value["batch"], TelemetryBatch
        elif "snapshot" in value:
            payload, model = value["snapshot"], TelemetrySnapshot
        else:
            payload, model = value, TelemetryBatch
        record = model.model_validate(payload)
        received = value.get("received_at")
        if preserve_received_time and not received:
            raise ValueError(f"line_{number}:received_at_required")
        received = timestamp(received) if received else utcnow()
        records.append((record, received))
    inserted = duplicates = 0
    for record, received in records:
        method = repository.append if isinstance(record, TelemetryBatch) else repository.append_snapshot
        result = method(record, namespace, received)
        inserted += result["inserted"]
        duplicates += result["duplicates"]
    repository.health(namespace, {"status": "replay", "records": len(records),
                                  "source_sha256": digest(source_text),
                                  "received_time": "preserved" if preserve_received_time else "synthetic"})
    return {"namespace": namespace, "records": len(records), "inserted": inserted, "duplicates": duplicates}


def export_replay(repository, namespace: str, path: Path):
    records = repository.export_batches(namespace)
    data = "".join(canonical(r) + "\n" for r in records).encode()
    atomic_write(path, data)
    from ..util import atomic_json
    import hashlib
    manifest = {"records": len(records), "sha256": hashlib.sha256(data).hexdigest(), "namespace": namespace}
    atomic_json(path.with_suffix(path.suffix + ".manifest.json"), manifest)
    return manifest


# Parse the C++ console directly: no intermediate, lossy emoji/log formatter.
_NUMBER = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_LOG_PATTERNS = [(priority, re.compile(pattern)) for priority, pattern in [
    (3, rf"RTT_MONITOR:\s*(?P<interface>\S+)\s*\|\s*RTT:\s*(?P<rtt>{_NUMBER})ms\s*\|\s*Quality:\s*\S+\s*\|\s*Using:\s*\S+\s*\|\s*Target:\s*(?P<target>\S+)"),
    (3, rf"RSSI_MONITOR:\s*(?P<interface>\S+)\s*\|\s*RSSI:\s*(?P<rssi>{_NUMBER})dBm"),
    (3, rf"TCP_LOSS_MONITOR:\s*interface=(?P<interface>\S+)\s+rate=(?P<tcp_rate>{_NUMBER})%\s+delta_sent=(?P<delta_sent>\d+)\s+delta_retrans=(?P<delta_retrans>\d+)"),
    (3, rf"TRAFFIC_MONITOR:\s*Total=(?P<traffic>{_NUMBER})MB/s,\s*Flows=(?P<flows>\d+),\s*PPS=(?P<pps>{_NUMBER}),\s*Interface=(?P<interface>\S+)"),
    (3, rf"网络质量(?:变化|稳定):\s*\S+\s*\(\s*分数:\s*(?P<score>{_NUMBER})(?:,\s*接口:\s*(?P<interface>[^)\s]+))?\s*\)"),
    (1, rf"\b(?:ACTIVE|INACTIVE):\s*(?P<interface>\S+)\s*\|\s*RTT:\s*(?P<rtt>{_NUMBER})ms\s*\|\s*Quality:\s*\S+\s*\|\s*RSSI:\s*(?P<rssi>{_NUMBER})dBm\s*\|\s*TCP Loss:\s*(?P<tcp_rate>{_NUMBER})%(?:\s*\([^)]*\))?(?:\s*\|\s*Traffic:\s*(?P<traffic>{_NUMBER})MB/s)?"),
    (2, rf"\[weak_mgr\]\s+iface=(?P<interface>\S+)\s+rtt=(?P<rtt>{_NUMBER})ms"),
    (2, rf"\[weak_mgr\]\s+iface=(?P<interface>\S+)\s+rssi=(?P<rssi>{_NUMBER})"),
    (2, rf"\[weak_mgr\]\s+iface=(?P<interface>\S+)\s+tcp_loss_rate=(?P<tcp_rate>{_NUMBER})"),
]]
_LOG_VALUES = {
    "rtt": ("icmp_rtt_ms", "ms", 0, math.inf),
    "rssi": ("wifi_rssi_dbm", "dBm", -150, 0),
    "tcp_rate": ("tcp_retransmission_log", "%", 0, math.inf),
    "traffic": ("traffic_log", "MB/s", 0, math.inf),
    "score": ("quality_score", "score", 0, 100),
}


def parse_log(line):
    """Return logged values, never invented probe attempts or cumulative counters."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
    for priority, pattern in _LOG_PATTERNS:
        match = pattern.search(clean)
        if not match:
            continue
        fields = match.groupdict()
        interface = fields.get("interface")
        target = fields.get("target")
        scope = Scope(kind="interface_target" if target else "interface" if interface else "host_netns",
                      interface=interface, target=target)
        facts = []
        for field, (metric, unit, low, high) in _LOG_VALUES.items():
            if fields.get(field) is None:
                continue
            value = float(fields[field])
            valid = math.isfinite(value) and low <= value <= high
            facts.append({"metric": metric, "unit": unit, "value": value if valid else None,
                          "status": "ok" if valid else "unavailable", "priority": priority})
        # Keep deltas as logged metadata, not as counters to differentiate again.
        metadata = {k: float(fields[k]) for k in ("delta_sent", "delta_retrans", "flows", "pps")
                    if fields.get(k) is not None and math.isfinite(float(fields[k]))}
        return scope, facts, metadata
    return Scope(kind="host_netns"), [], {}


async def _console_lines(stream):
    """Bound memory even for a broken producer that never emits a newline."""
    pending = bytearray()
    truncated = False
    while data := await stream.read(8192):
        parts = data.split(b"\n")
        for i, part in enumerate(parts):
            space = MAX_LOG_LINE_BYTES - len(pending)
            pending.extend(part[:space])
            truncated |= len(part) > space
            if i < len(parts) - 1:
                yield pending.decode("utf-8", errors="replace").rstrip("\r"), truncated
                pending.clear()
                truncated = False
    if pending or truncated:
        yield pending.decode("utf-8", errors="replace").rstrip("\r"), truncated


class LogSource:
    def __init__(self, settings, repository):
        self.settings = settings
        self.repository = repository
        self.process = None

    async def run(self):
        settings, repository = self.settings, self.repository
        session = "capture-" + uuid.uuid4().hex
        identity = dict(host_id=settings.host_id, boot_id="unknown", collector_session_id=session,
                        netns_id="unknown")
        health = {"host_id": settings.host_id, "collector_session_id": session,
                  "executable": str(settings.executable), "lines": 0, "parsed_lines": 0,
                  "rejected_lines": 0, "timestamp_source": "capture_time"}
        repository.health("logs", {**health, "status": "starting"})
        # Shield process creation so cancellation cannot orphan a just-started child.
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            str(settings.executable), *settings.args, cwd=settings.executable.parent,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))

        async def consume(stream):
            nonlocal identity
            async for line, truncated in _console_lines(stream):
                if not line.strip():
                    continue
                received = utcnow()
                health["lines"] += 1
                parse_error = None
                if line.startswith(STRUCTURED_LOG_PREFIX):
                    try:
                        if truncated:
                            raise ValueError("structured_log_truncated")
                        batch = TelemetryBatch.model_validate_json(line[len(STRUCTURED_LOG_PREFIX):])
                    except ValueError as exc:
                        health["rejected_lines"] += 1
                        parse_error = type(exc).__name__
                        repository.quarantine("invalid_structured_log:" + str(exc), line)
                    else:
                        batch_identity = {key: getattr(batch, key) for key in identity}
                        # Stable provenance makes an identical logged batch idempotent.
                        reference = Event(event_id="log-" + digest({**batch_identity, "stream": batch.stream_id,
                                                                   "sequence": batch.sequence}),
                            event_type="service_log", observed_at=batch.emitted_at,
                            monotonic_ns=max(item.monotonic_ns for item in [*batch.observations, *batch.events]),
                            scope=Scope(kind="host_netns"), source="console_log_v1",
                            details={"format": "telemetry_v1", "raw": line, "timestamp_source": "service_time",
                                     "sample_ids": [item.sample_id for item in batch.observations],
                                     "event_ids": [item.event_id for item in batch.events]})
                        try:
                            repository.append(batch.model_copy(update={"events": [*batch.events, reference]}),
                                              received_at=received)
                        except TelemetryConflictError:
                            # The repository already quarantined this batch atomically.
                            # Storage failures still escape; bad input must not stop both pipes.
                            health["rejected_lines"] += 1
                            repository.health("logs", {**health, "status": "capturing"})
                            continue
                        identity = batch_identity
                        health.update(host_id=batch.host_id, collector_session_id=batch.collector_session_id,
                                      timestamp_source="service_time")
                        health["parsed_lines"] += 1
                        repository.health("logs", {**health, "status": "capturing"})
                        continue
                scope, facts, metadata = (parse_log(line) if not truncated and parse_error is None
                                          else (Scope(kind="host_netns"), [], {}))
                health["parsed_lines"] += bool(facts)
                event = Event(event_id=f"{session}:{health['lines']}", event_type="service_log",
                              observed_at=received, monotonic_ns=time.monotonic_ns(), scope=scope,
                              source="console_log_v1", details={"raw": line, "line_number": health["lines"],
                              "timestamp_source": "capture_time", "truncated": truncated,
                              "facts": facts, "metadata": metadata, "parse_error": parse_error})
                repository.append(TelemetryBatch(**identity, stream_id="console", sequence=health["lines"],
                                                 emitted_at=received, events=[event]), received_at=received)
                repository.health("logs", {**health, "status": "capturing"})
        readers = []
        try:
            self.process = await asyncio.shield(launch)
            health["pid"] = self.process.pid
            repository.health("logs", {**health, "status": "capturing"})
            # Separate pipes prevent stderr writes from corrupting a large JSON
            # line on stdout; both consumers append to the same local repository.
            readers = [asyncio.create_task(consume(stream)) for stream in (self.process.stdout, self.process.stderr)]
            await asyncio.gather(*readers)
            code = await self.process.wait()
            repository.health("logs", {**health, "status": "exited" if code == 0 else "failed", "exit_code": code})
        except asyncio.CancelledError:
            repository.health("logs", {**health, "status": "stopped"})
            raise
        except Exception as exc:
            repository.health("logs", {**health, "status": "failed", "error": type(exc).__name__,
                                      "detail": str(exc)[:500]})
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            # launch may finish after cancellation, but must still be reaped.
            try:
                self.process = await launch
            except Exception:
                pass
            if self.process is not None:
                if self.process.returncode is None:
                    try:
                        self.process.terminate()
                    except ProcessLookupError:
                        pass
                    # Drain in bounded chunks: wait alone can block on a full pipe,
                    # while communicate would retain all shutdown output in memory.
                    async def drain_and_wait():
                        async def drain(stream):
                            while await stream.read(8192):
                                pass
                        await asyncio.gather(drain(self.process.stdout), drain(self.process.stderr))
                        await self.process.wait()

                    try:
                        await asyncio.wait_for(drain_and_wait(), timeout=1)
                    except TimeoutError:
                        try:
                            self.process.kill()
                        except ProcessLookupError:
                            pass
                        await drain_and_wait()
                await self.process.wait()
