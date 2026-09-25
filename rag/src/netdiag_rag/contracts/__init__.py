"""Versioned contracts shared by log capture, replay, HTTP and evaluation."""
from __future__ import annotations

import math
import ipaddress
import re
from typing import Any, Literal

from pydantic import AwareDatetime, Field, StrictFloat, StrictInt, field_validator, model_serializer, model_validator

from ..settings import StrictModel

Status = Literal["ok", "timeout", "permission_denied", "unavailable", "invalid", "not_applicable"]
Metric = Literal["icmp_rtt_ms", "icmp_probe_result", "tcp_segments", "netdev_bytes",
                 "wifi_rssi_dbm", "flow_top_bytes_per_s", "quality_score", "quality_level"]
METRIC_UNITS = {"icmp_rtt_ms": "ms", "icmp_probe_result": "result", "tcp_segments": "segments",
                "netdev_bytes": "bytes", "wifi_rssi_dbm": "dBm", "flow_top_bytes_per_s": "B/s",
                "quality_score": "score", "quality_level": "level"}
LOG_METRIC_UNITS = {"tcp_retransmission_log": "%", "traffic_log": "MB/s"}


class Scope(StrictModel):
    kind: Literal["host_netns", "interface", "interface_target", "flow_subset"]
    interface: str | None = None
    ifindex: int | None = Field(default=None, ge=1)
    interface_generation: int | None = Field(default=None, ge=1)
    target: str | None = None
    direction: str | None = None
    protocol: str | None = None
    top_n: int | None = Field(default=None, gt=0)
    hook: str | None = None

    @model_validator(mode="after")
    def valid_scope(self):
        if self.kind != "host_netns" and not self.interface:
            raise ValueError("interface_required")
        if self.kind == "host_netns" and any(v is not None for v in
                (self.interface, self.ifindex, self.interface_generation, self.target)):
            raise ValueError("host_scope_cannot_claim_interface")
        if self.kind == "interface_target" and not self.target:
            raise ValueError("probe_target_required")
        if self.kind == "flow_subset" and (self.direction not in ("tx", "rx") or not self.protocol
                or not self.top_n or not self.hook or not self.ifindex or not self.interface_generation):
            raise ValueError("flow_scope_requires_direction_protocol_topn_hook_identity")
        return self


class Observation(StrictModel):
    sample_id: str = Field(min_length=1, max_length=1024)
    metric: Metric
    scope: Scope
    source: str = Field(min_length=1)
    observed_at: AwareDatetime
    monotonic_ns: int = Field(ge=0)
    interval_ms: int = Field(gt=0)
    status: Status
    value: StrictInt | StrictFloat | str | dict[str, StrictInt] | None
    unit: str
    error_code: str | None = None
    attempt_id: str | None = None
    algorithm_version: str | None = None
    measurement_interval_ms: int | None = Field(default=None, gt=0)

    @model_serializer(mode="wrap")
    def compatible_serialization(self, handler):
        value = handler(self)
        # Preserve v1 stored sample digests when this optional extension is absent.
        if self.measurement_interval_ms is None:
            value.pop("measurement_interval_ms", None)
        return value

    @model_validator(mode="after")
    def semantics(self):
        if self.unit != METRIC_UNITS[self.metric]:
            raise ValueError(f"invalid_unit_for_{self.metric}")
        if self.metric.startswith("icmp_") and (self.scope.kind != "interface_target" or not self.attempt_id):
            raise ValueError("icmp_requires_target_and_attempt_id")
        if self.metric == "tcp_segments" and self.scope.kind != "host_netns":
            raise ValueError("tcp_counter_scope_must_be_host_netns")
        if self.metric in ("netdev_bytes", "wifi_rssi_dbm") and self.scope.kind != "interface":
            raise ValueError("metric_requires_interface_scope")
        if self.metric == "flow_top_bytes_per_s" and self.scope.kind != "flow_subset":
            raise ValueError("flow_scope_required")
        if self.metric in ("quality_score", "quality_level") and not self.algorithm_version:
            raise ValueError("quality_requires_algorithm_version")
        if self.status != "ok":
            if self.value is not None:
                raise ValueError("non_ok_value_must_be_null")
            return self
        if self.metric in ("tcp_segments", "netdev_bytes"):
            keys = {"out_segments", "retrans_segments"} if self.metric == "tcp_segments" else {"rx_bytes", "tx_bytes"}
            if not isinstance(self.value, dict) or set(self.value) != keys or any(v < 0 for v in self.value.values()):
                raise ValueError("invalid_counter_set")
        elif self.metric == "icmp_probe_result":
            if self.value not in ("reply", "timeout", "unreachable"):
                raise ValueError("invalid_probe_result")
        elif self.metric == "quality_level":
            if isinstance(self.value, bool) or not isinstance(self.value, (int, str)):
                raise ValueError("invalid_quality_level")
        else:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
                raise ValueError("finite_numeric_value_required")
            if self.metric != "wifi_rssi_dbm" and self.value < 0:
                raise ValueError("negative_value")
            if self.metric == "wifi_rssi_dbm" and not -150 <= self.value <= 0:
                raise ValueError("invalid_rssi")
            if self.metric == "quality_score" and self.value > 100:
                raise ValueError("invalid_quality_score")
            if self.metric == "flow_top_bytes_per_s" and self.measurement_interval_ms is None:
                raise ValueError("flow_measurement_interval_required")
        return self


class Event(StrictModel):
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    observed_at: AwareDatetime
    monotonic_ns: int = Field(ge=0)
    scope: Scope
    source: str
    details: dict[str, Any] = Field(default_factory=dict)


class Identity(StrictModel):
    schema_version: Literal[1] = 1
    host_id: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)
    collector_session_id: str = Field(min_length=1)
    netns_id: str = Field(min_length=1)


class TelemetryBatch(Identity):
    stream_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    emitted_at: AwareDatetime
    observations: list[Observation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.observations and not self.events:
            raise ValueError("empty_batch")
        if len({o.sample_id for o in self.observations}) != len(self.observations):
            raise ValueError("duplicate_sample_id_in_batch")
        return self


class Watermark(StrictModel):
    collector_session_id: str
    stream_id: str
    sequence: int = Field(ge=0)


class TelemetrySnapshot(Identity):
    captured_at: AwareDatetime
    watermarks: list[Watermark] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    source_health: dict[str, Any] = Field(default_factory=dict)


class CheckContext(StrictModel):
    """Explicit operator inputs for checks, never evidence confirming a diagnosis."""
    interface: str = Field(min_length=1, max_length=15, pattern=r"^[A-Za-z0-9_.:-]+$")
    pid: StrictInt | None = Field(default=None, ge=1, le=2**31 - 1)
    hostname: str | None = Field(default=None, max_length=253)
    gateway: str | None = None
    external_target: str | None = None

    @field_validator("gateway", "external_target")
    @classmethod
    def ip_literal(cls, value):
        if value is None:
            return value
        if "%" in value:
            raise ValueError("check_address_zone_uses_interface_field")
        return str(ipaddress.ip_address(value))

    @field_validator("hostname")
    @classmethod
    def domain(cls, value):
        if value is None:
            return value
        value = value.rstrip(".").encode("idna").decode("ascii").lower()
        if len(value) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s)
                                      for s in value.split(".")):
            raise ValueError("invalid_check_hostname")
        return value

    @model_validator(mode="after")
    def distinct_targets(self):
        if self.gateway and self.gateway == self.external_target:
            raise ValueError("gateway_and_external_target_must_differ")
        return self


class DiagnosisRequest(StrictModel):
    report_type: Literal["incident_diagnosis", "knowledge_answer"] = "incident_diagnosis"
    data_namespace: str = "live"
    host_id: str | None = None
    interfaces: list[str] = Field(default_factory=list, max_length=8)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    question: str = Field(min_length=1, max_length=2000)
    as_of_received_at: AwareDatetime | None = None
    knowledge_cutoff: AwareDatetime | None = None
    check_context: CheckContext | None = None

    @model_serializer(mode="wrap")
    def compatible_serialization(self, handler):
        value = handler(self)
        if self.check_context is None:
            value.pop("check_context", None)
        return value

    @field_validator("data_namespace")
    @classmethod
    def namespace(cls, value):
        if value != "live" and (not value.startswith("replay:") or len(value) <= 7):
            raise ValueError("invalid_data_namespace")
        return value

    @model_validator(mode="after")
    def scope_required(self):
        if self.report_type == "incident_diagnosis" and not self.host_id:
            raise ValueError("host_id_required")
        if self.window_start and not self.window_end:
            raise ValueError("window_end_required_with_start")
        if self.window_start and self.window_end and self.window_start >= self.window_end:
            raise ValueError("invalid_window")
        if self.data_namespace == "live" and self.as_of_received_at is not None:
            raise ValueError("live_received_cutoff_is_server_owned")
        if len(self.interfaces) != len(set(self.interfaces)):
            raise ValueError("duplicate_interfaces")
        if self.check_context and (self.report_type != "incident_diagnosis" or
                self.interfaces and self.check_context.interface not in self.interfaces):
            raise ValueError("check_context_outside_request_scope")
        return self


class Evidence(StrictModel):
    evidence_id: str
    metric: str
    scope: dict[str, Any]
    summary: dict[str, Any]
    sample_ids: list[str]
    event_ids: list[str] = Field(default_factory=list)
    coverage: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


class EvidenceBundle(StrictModel):
    host_id: str
    data_namespace: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    as_of_received_at: AwareDatetime
    ingest_revision: int
    evidence: list[Evidence]
    events: list[dict[str, Any]] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    rule_hits: list[dict[str, Any]] = Field(default_factory=list)
    rule_set_hash: str | None = None
    trend_policy_hash: str | None = None
    digest: str


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)
    knowledge_ids: list[str] = Field(default_factory=list)


class Hypothesis(Claim):
    cause_id: str
    certainty: Literal["supported", "plausible", "unconfirmed"] = "unconfirmed"
    counterevidence: list[str] = Field(default_factory=list)
    missing_evidence_ids: list[str] = Field(default_factory=list)


class NextCheck(StrictModel):
    check_id: str
    purpose: str
    knowledge_ids: list[str]
    target: str | None = None
    parameters: dict[str, str | StrictInt] = Field(default_factory=dict)


class DiagnosisContent(StrictModel):
    outcome: Literal["diagnosed", "insufficient_evidence", "no_anomaly", "needs_input"]
    observations: list[Claim] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=3)
    next_checks: list[NextCheck] = Field(default_factory=list, max_length=10)
    warnings: list[str] = Field(default_factory=list)


class IncidentReasoning(StrictModel):
    """Model-owned fields only; incident observations are rendered from trusted facts."""
    outcome: Literal["diagnosed", "insufficient_evidence", "no_anomaly", "needs_input"]
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=2)
    next_checks: list[NextCheck] = Field(default_factory=list, max_length=2)
    warnings: list[str] = Field(default_factory=list, max_length=2)


class DiagnosisReport(DiagnosisContent):
    schema_version: Literal[1, 2] = 2
    report_id: str
    job_id: str
    report_type: Literal["incident_diagnosis", "knowledge_answer"]
    created_at: AwareDatetime
    scope: dict[str, Any]
    window: dict[str, Any]
    data_quality: dict[str, Any]
    citations: list[dict[str, Any]]
    evidence_bundle: dict[str, Any]
    degradations: list[str]
    provenance: dict[str, Any]
    retrieval_trace: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


def export_schemas(directory):
    from ..knowledge.schema import knowledge_schema
    from ..util import atomic_json
    atomic_json(directory / "knowledge-v1.schema.json", knowledge_schema())
    for name, model in [("telemetry-v1", TelemetryBatch), ("snapshot-v1", TelemetrySnapshot),
                        ("diagnosis-v2", DiagnosisReport), ("request-v1", DiagnosisRequest)]:
        atomic_json(directory / f"{name}.schema.json", model.model_json_schema())
