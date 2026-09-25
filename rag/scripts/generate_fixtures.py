"""Generate explicitly synthetic telemetry for contract/engineering acceptance.

These fixtures are not the independent real-incident dataset required for quality claims.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

from netdiag_rag.contracts import TelemetryBatch
from netdiag_rag.util import canonical, iso

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
KNOWLEDGE_CUTOFF = "2026-09-23T16:55:40Z"
RELEVANCE = {
    "wifi_weak_signal": {"runbook.wifi.weak_signal": 2, "runbook.icmp.rtt_window": 1, "runbook.tcp.retransmission_scope": 1},
    "normal": {"runbook.icmp.rtt_window": 1, "runbook.netdev.idle_throughput": 1},
    "all_timeout": {"runbook.icmp.timeout": 2, "runbook.route.external_path": 1},
    "permission_denied": {"runbook.collector.permission": 2},
    "counter_reset": {"runbook.telemetry.counter_reset": 2},
    "link_down": {"runbook.interface.link_down": 2},
    "missing_data": {"runbook.telemetry.freshness": 2},
    "late_data": {"runbook.telemetry.freshness": 2},
}


def generate_case(name):
    identity = {"schema_version": 1, "host_id": "lab-host-01", "boot_id": "boot-" + name,
                "collector_session_id": "collector-" + name, "netns_id": "netns-demo-01"}
    interface = {"kind": "interface", "interface": "wlan0", "ifindex": 3, "interface_generation": 1}
    probe_scope = {**interface, "kind": "interface_target", "target": "223.5.5.5"}
    records = []
    for i in range(61):
        now = START + timedelta(seconds=i)
        observations = []
        def add(metric, value, unit, scope, status="ok", interval=1000, attempt=None):
            observations.append({"sample_id": f"{name}/{i}/{len(observations)}", "metric": metric, "scope": scope,
                                 "source": "synthetic_fixture_v1", "observed_at": iso(now), "monotonic_ns": (120+i)*1_000_000_000,
                                 "interval_ms": interval, "status": status, "value": value, "unit": unit,
                                 "error_code": "EACCES" if status == "permission_denied" else None,
                                 "attempt_id": attempt})
        if i < 60:
            timeout = name == "all_timeout" or (name == "wifi_weak_signal" and i >= 54)
            permission = name == "permission_denied"
            result = "timeout" if timeout else "reply"
            rtt = 220 if name == "wifi_weak_signal" and 50 <= i < 54 else 160+i%20 if name == "wifi_weak_signal" else 20
            add("icmp_probe_result", None if permission else result, "result", probe_scope,
                "permission_denied" if permission else "ok", attempt=f"{name}-icmp-{i}")
            add("icmp_rtt_ms", None if permission or timeout else rtt, "ms", probe_scope,
                "permission_denied" if permission else "timeout" if timeout else "ok", attempt=f"{name}-icmp-{i}")
            if i % 5 == 0:
                add("wifi_rssi_dbm", -79 if name == "wifi_weak_signal" else -50, "dBm", interface, interval=5000)
        out = 100000 + 10000*i//60
        retrans = 2000 + (400*i//60 if name == "wifi_weak_signal" else 0)
        if name == "counter_reset" and i >= 30:
            out -= 100000
            retrans -= 2000
        add("tcp_segments", {"out_segments": out, "retrans_segments": retrans}, "segments", {"kind": "host_netns"})
        add("netdev_bytes", {"rx_bytes": 1000000+1000*i, "tx_bytes": 500000+500*i}, "bytes", interface)
        events = []
        if name == "link_down" and i == 30:
            events.append({"event_id": name+"/link-down", "event_type": "link_state_changed", "observed_at": iso(now),
                           "monotonic_ns": (120+i)*1_000_000_000, "scope": interface,
                           "source": "synthetic_fixture_v1", "details": {"state": "down"}})
        # Sparse measurements exercise insufficient coverage without converting gaps to zero.
        if name == "missing_data" and i not in (0, 60):
            continue
        batch = {**identity, "stream_id": "network", "sequence": i+1,
                 "emitted_at": iso(now+timedelta(milliseconds=10)), "observations": observations, "events": events}
        TelemetryBatch.model_validate(batch)
        received = now + timedelta(milliseconds=20)
        if name == "late_data" and 30 <= i < 40:
            received += timedelta(seconds=120)
        records.append({"received_at": iso(received), "batch": batch})
    return records


def main():
    directory = ROOT / "datasets/fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    cases = []
    names = ["wifi_weak_signal", "normal", "all_timeout", "permission_denied", "counter_reset", "link_down", "missing_data", "late_data"]
    for name in names:
        records = generate_case(name)
        (directory / f"{name}.jsonl").write_text("".join(canonical(r)+"\n" for r in records))
        request = {"data_namespace": "replay:"+("wifi-demo" if name == "wifi_weak_signal" else name),
                   "host_id": "lab-host-01", "interfaces": ["wlan0"],
                   "window_start": iso(START), "window_end": iso(START+timedelta(seconds=60)),
                   "as_of_received_at": iso(START+timedelta(seconds=61)),
                   "knowledge_cutoff": KNOWLEDGE_CUTOFF,
                   "question": "这一分钟为什么网络变慢，优先检查什么？"}
        (directory / f"{name}.request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2)+"\n")
        case_class = "A" if name in ("permission_denied", "link_down") else "N" if name == "normal" else "U"
        cause = {"permission_denied": "collector_permission_denied", "link_down": "interface_link_down"}.get(name)
        cases.append({"case_id": name, "group_id": "synthetic-"+name, "origin_id": "fixture-template-"+name,
                      "split": "test", "class": case_class, "causes": [cause] if cause else [],
                      "telemetry": f"../fixtures/{name}.jsonl", "request": request, "relevance": RELEVANCE[name]})
    manifests = ROOT / "datasets/manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "fixtures.json").write_text(json.dumps({"schema_version": 1, "purpose": "engineering_smoke",
        "synthetic": True, "annotation_status": "code_authored_engineering_expectations_not_independent_review",
        "knowledge_cutoff": KNOWLEDGE_CUTOFF, "cases": cases}, ensure_ascii=False, indent=2)+"\n")
    print(f"Generated {len(cases)} explicitly synthetic engineering cases")


if __name__ == "__main__":
    main()
