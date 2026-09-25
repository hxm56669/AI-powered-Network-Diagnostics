"""Deterministic window statistics; no language model performs arithmetic."""
from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict

from ..contracts import DiagnosisRequest, Evidence, EvidenceBundle
from ..util import canonical, digest, iso, timestamp
from ..settings import TrendSettings
from .rules import RuleEngine
from .trends import rssi_trend


def _percentile(values, q):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)]


def _coverage(rows, start, end, valid_only=True):
    """Distinct scheduled slots in time-sorted rows; period changes split the denominator."""
    if not rows:
        return 0.0
    runs = []
    for row in rows:
        if not runs or row["interval_ms"] != runs[-1][0]:
            runs.append((row["interval_ms"], []))
        runs[-1][1].append(row)
    expected = valid = 0
    for i, (interval, samples) in enumerate(runs):
        begin = start if i == 0 else timestamp(samples[0]["observed_at"])
        finish = timestamp(runs[i+1][1][0]["observed_at"]) if i+1 < len(runs) else end
        step = interval / 1000
        count = math.ceil(max(0, (finish - begin).total_seconds()) / step)
        expected += count
        slots = {int((timestamp(s["observed_at"]) - begin).total_seconds() // step) for s in samples
                 if (not valid_only or s["status"] == "ok") and begin <= timestamp(s["observed_at"]) < finish}
        valid += min(count, len(slots))
    return min(1.0, valid / expected) if expected else 0.0


def _log_evidence(events, namespace):
    grouped = defaultdict(list)
    priorities = {}
    for event in events:
        if event["event_type"] != "service_log" or event["details"].get("format") == "telemetry_v1":
            continue
        scope, identity = event["scope"], canonical(event["_identity"])
        facts = event["details"].get("facts", [])
        for fact in facts:
            # Detailed monitor records take precedence over duplicate weak_mgr and
            # interface summaries. Keep distinct destinations and capture sessions.
            family = (fact["metric"], scope.get("interface"), identity)
            priorities[family] = max(priorities.get(family, 0), fact["priority"])
            grouped[(family, canonical(scope), fact["priority"])].append((event, fact))
        if not facts:
            family = ("service_log", scope.get("interface"), identity)
            priorities[family] = 0
            grouped[(family, canonical(scope), 0)].append((event, None))
    result = []
    for (family, _, priority), records in grouped.items():
        if priority != priorities[family]:
            continue
        records.sort(key=lambda r: (timestamp(r[0]["observed_at"]), r[0]["details"]["line_number"]))
        metric = family[0]
        values = [fact["value"] for _, fact in records if fact and fact["status"] == "ok"]
        summary = {"source": "console_log_v1", "samples": len(records), "valid_samples": len(values),
                   "timestamp_source": "capture_time", "sampling_basis": "logged_values"}
        warnings = ["log_sampling_coverage_unknown", "log_timestamp_is_capture_time"]
        if metric == "service_log":
            raw = [event["details"]["raw"] for event, _ in records]
            errors = [line for line in raw if re.search(r"(?i)\b(?:error|failed|failure|denied|fatal)\b|失败|错误|权限不足|^[EF]\d{4}", line)]
            summary.update(error_lines=len(errors), messages=(errors or raw)[-5:])
            summary["messages"] = [line[:400] for line in summary["messages"]]
            if errors:
                warnings.append("service_errors_logged")
        else:
            summary["unit"] = records[0][1]["unit"]
            summary["latest_log_fields"] = records[-1][0]["details"].get("metadata", {})
            summary["status_counts"] = {"ok": len(values), "unavailable": len(records) - len(values)}
            if values:
                summary.update(min=min(values), median=statistics.median(values), max=max(values),
                               p95=_percentile(values, .95), latest=values[-1], percentile_method="nearest_rank")
            if metric == "wifi_rssi_dbm":
                summary["trend"] = {"direction": "unknown"}
            if metric == "tcp_retransmission_log":
                warnings.append("logged_tcp_ratio_is_not_interface_packet_loss")
            if metric == "icmp_rtt_ms" and not records[0][0]["scope"].get("target"):
                warnings.append("log_probe_target_unknown")
        if any(event["details"].get("truncated") for event, _ in records):
            warnings.append("oversized_log_line_truncated")
        if any(event["details"].get("parse_error") for event, _ in records):
            warnings.append("invalid_structured_log")
        value = dict(metric=metric, scope=records[0][0]["scope"], summary=summary, sample_ids=[],
                     event_ids=[event["event_id"] for event, _ in records], coverage=0.0, warnings=warnings)
        result.append(Evidence(**value, evidence_id="ev-" + digest({**value, "namespace": namespace})))
    return result


class EvidenceBuilder:
    def __init__(self, repository, min_coverage=0.8, rule_engine=None, trend_settings=None):
        self.repository = repository
        self.min_coverage = min_coverage
        self.rules = rule_engine or RuleEngine()
        self.trends = trend_settings or TrendSettings()

    def build(self, request: DiagnosisRequest, revision: int, received_at) -> EvidenceBundle:
        start, end = timestamp(request.window_start), timestamp(request.window_end)
        rows, events = self.repository.window(request, revision, received_at)
        log_references = defaultdict(list)
        for event in events:
            if event["event_type"] == "service_log" and event["details"].get("format") == "telemetry_v1":
                for item_id in [*event["details"].get("sample_ids", []), *event["details"].get("event_ids", [])]:
                    log_references[item_id].append(event["event_id"])
        grouped = defaultdict(list)
        for row in rows:
            if row["metric"] not in ("tcp_segments", "netdev_bytes") and timestamp(row["observed_at"]) >= end:
                continue
            # Never combine a different boot/session/netns/source or a reused interface identity.
            key = (row["metric"], canonical(row["scope"]), row["source"], canonical(row["_identity"]),
                   row.get("algorithm_version") or "")
            grouped[key].append(row)
        evidence = []
        symptoms = set()
        missing = []
        for (metric, _, source, _, algorithm), samples in sorted(grouped.items()):
            samples.sort(key=lambda r: (timestamp(r["observed_at"]), r["monotonic_ns"], r["sample_id"]))
            valid = [r for r in samples if r["status"] == "ok"]
            warnings = []
            summary = {"source": source, "samples": len(samples), "valid_samples": len(valid),
                       "unit": samples[0]["unit"], "acquisition_coverage": _coverage(samples, start, end, False)}
            if algorithm:
                summary["algorithm_version"] = algorithm
            if metric == "flow_top_bytes_per_s":
                warnings.append("top_flow_subset_is_not_full_interface_throughput")
                summary["measurement_interval_ms"] = sorted({r["measurement_interval_ms"] for r in valid
                                                               if r.get("measurement_interval_ms")})
            summary["status_counts"] = {status: sum(r["status"] == status for r in samples)
                                        for status in sorted({r["status"] for r in samples})}
            coverage = _coverage(samples, start, end)
            if metric in ("tcp_segments", "netdev_bytes"):
                totals = {k: 0 for k in ("out_segments", "retrans_segments") if metric == "tcp_segments"}
                if metric == "netdev_bytes":
                    totals = {"rx_bytes": 0, "tx_bytes": 0}
                duration = 0.0
                effective_start = effective_end = None
                for previous, current in zip(samples, samples[1:]):
                    if previous["status"] != "ok" or current["status"] != "ok":
                        continue
                    a, b = timestamp(previous["observed_at"]), timestamp(current["observed_at"])
                    elapsed = (current["monotonic_ns"] - previous["monotonic_ns"]) / 1e9
                    wall = (b - a).total_seconds()
                    if not start <= a < b <= end or elapsed <= 0 or abs(elapsed-wall) > max(0.25, wall * 0.1):
                        warnings.append("clock_discontinuity")
                        continue
                    delta = {k: current["value"][k] - previous["value"][k] for k in totals}
                    if min(delta.values()) < 0:
                        warnings.append("counter_reset")
                        continue
                    if elapsed > 2.5 * max(previous["interval_ms"], current["interval_ms"]) / 1000:
                        warnings.append("counter_gap")
                        continue
                    for k in totals:
                        totals[k] += delta[k]
                    duration += elapsed
                    effective_start = effective_start or iso(a)
                    effective_end = iso(b)
                # Acquisition counts received slots, including error records. Metric
                # coverage for counters instead requires valid contiguous intervals.
                coverage = min(1.0, duration / (end-start).total_seconds())
                summary.update(totals, effective_seconds=duration, effective_start=effective_start,
                               effective_end=effective_end, interval_coverage=coverage)
                if metric == "tcp_segments":
                    summary["retrans_out_ratio"] = totals["retrans_segments"] / totals["out_segments"] if totals["out_segments"] >= 10 else None
                    if summary["retrans_out_ratio"] is None:
                        warnings.append("insufficient_samples")
                    warnings.append("host_netns_ratio_is_not_interface_packet_loss")
                else:
                    summary["rx_bytes_per_s"] = totals["rx_bytes"] / duration if duration else None
                    summary["tx_bytes_per_s"] = totals["tx_bytes"] / duration if duration else None
                    summary["rx_mbit_per_s"] = summary["rx_bytes_per_s"] * 8 / 1_000_000 if duration else None
                    summary["tx_mbit_per_s"] = summary["tx_bytes_per_s"] * 8 / 1_000_000 if duration else None
            elif metric == "icmp_probe_result":
                by_attempt = {}
                for r in valid:
                    attempt = r["attempt_id"]
                    if attempt in by_attempt and by_attempt[attempt] != r["value"]:
                        warnings.append("conflicting_probe_attempt")
                    by_attempt[attempt] = r["value"]
                summary.update(attempts=len(by_attempt), replies=list(by_attempt.values()).count("reply"),
                               timeouts=list(by_attempt.values()).count("timeout"),
                               unreachable=list(by_attempt.values()).count("unreachable"), errors=len(samples)-len(valid))
            elif metric in ("icmp_rtt_ms", "wifi_rssi_dbm", "quality_score", "flow_top_bytes_per_s"):
                values = [r["value"] for r in valid]
                if values:
                    summary.update(min=min(values), median=statistics.median(values), max=max(values),
                                   p95=_percentile(values, 0.95), percentile_method="nearest_rank")
                if metric == "wifi_rssi_dbm":
                    summary["trend"] = rssi_trend(samples, coverage, self.min_coverage, self.trends)
            elif metric == "quality_level" and valid:
                summary["latest"] = valid[-1]["value"]
                summary["algorithm_version"] = valid[-1].get("algorithm_version")
            if all(r["status"] == "not_applicable" for r in samples):
                warnings.append("metric_not_applicable")
            elif metric == "flow_top_bytes_per_s" and all(
                    r["status"] == "unavailable" and r.get("error_code") == "flow_not_enabled" for r in samples):
                warnings.append("optional_metric_disabled")
            elif coverage < self.min_coverage:
                warnings.append("low_metric_coverage")
            if any(w in warnings for w in ("counter_reset", "counter_gap", "clock_discontinuity")):
                symptoms.add("counter_discontinuity")
            value = {"metric": metric, "scope": samples[0]["scope"], "summary": summary,
                     "sample_ids": sorted(r["sample_id"] for r in samples), "coverage": coverage,
                     "warnings": sorted(set(warnings))}
            references = sorted({key for sample in samples for key in log_references[sample["sample_id"]]})
            if references:
                value["event_ids"] = references
            value["evidence_id"] = "ev-" + digest({**value, "namespace": request.data_namespace})
            evidence.append(Evidence.model_validate(value))
        evidence.extend(_log_evidence(events, request.data_namespace))
        if any(e.metric == "service_log" and e.summary["error_lines"] for e in evidence):
            symptoms.add("service_log_error")
        if not any(e.metric == "icmp_probe_result" and e.summary.get("attempts", 0) > 0 for e in evidence):
            missing.append("icmp_probe_results")
        interfaces = {e.scope["interface"] for e in evidence if e.scope.get("interface")}
        if not request.interfaces and len(interfaces) > 1:
            missing.append("ambiguous_interface")
        if not evidence:
            missing.append("telemetry_unavailable")
        latest_links = {}
        for event in events:
            if event["event_type"] in ("link_state_changed", "link_down", "link_up"):
                latest_links[canonical(event["scope"])] = event
        for event in latest_links.values():
            state = event["details"].get("state") or {"link_down": "down", "link_up": "up"}.get(event["event_type"])
            if state not in ("up", "down"):
                continue
            record = {"metric": "link_state", "scope": event["scope"],
                      "summary": {"state": state, "observed_at": event["observed_at"], "event_id": event["event_id"]},
                      "sample_ids": [], "event_ids": [event["event_id"], *log_references[event["event_id"]]],
                      "coverage": 1.0, "warnings": []}
            evidence.append(Evidence(**record, evidence_id="ev-" + digest(record)))
        hits = self.rules.evaluate(evidence)
        symptoms.update(hit["symptom"] for hit in hits)
        if {"low_rssi", "high_rtt"} <= symptoms:
            # Operator check targets alone do not establish a measured comparison.
            missing.append("gateway_external_comparison")
        # Console text has no scheduled coverage; it can expose anomalies but is
        # neither a missing scheduled sample nor proof of a healthy probe window.
        if any("low_metric_coverage" in e.warnings for e in evidence):
            symptoms.add("incomplete_telemetry")
        content = dict(host_id=request.host_id, data_namespace=request.data_namespace,
                       window_start=iso(start), window_end=iso(end), as_of_received_at=iso(received_at),
                       ingest_revision=revision, evidence=[e.model_dump(mode="json") for e in evidence],
                       events=events, missing=sorted(missing), symptoms=sorted(symptoms),
                       rule_hits=hits, rule_set_hash=self.rules.fingerprint,
                       trend_policy_hash=digest(self.trends.model_dump()))
        return EvidenceBundle(**content, digest=digest(content))


def query_from_evidence(question, bundle: EvidenceBundle | None):
    names = {"high_rtt": "ICMP RTT 往返时延升高", "low_rssi": "WiFi RSSI 无线信号偏弱",
             "probe_failure": "ICMP 探测超时或不可达", "tcp_retransmission": "TCP 重传增加",
             "incomplete_telemetry": "指标缺测 采集覆盖不足", "interface_down": "接口链路断开 down",
             "permission_denied": "ICMP 原始套接字 EACCES EPERM CAP_NET_RAW 采集权限不足",
             "service_log_error": "网络诊断服务日志报错或采集失败",
             "counter_discontinuity": "累计计数器重置 基线中断 统计范围不连续"}
    return question + ("\n观测事实：" + "；".join(names[s] for s in bundle.symptoms if s in names) if bundle else "")
