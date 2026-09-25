"""Deterministic, replayable query plans; only observed scope enters soft filters."""
import re
import unicodedata
from typing import Any, Literal

from pydantic import Field

from .evidence import query_from_evidence
from .settings import StrictModel


class QueryPlan(StrictModel):
    version: Literal["network-query-v1"] = "network-query-v1"
    original_query: str
    normalized_query: str
    intent: Literal["explain_anomaly", "compare_causes", "suggest_checks", "knowledge_question"]
    facts: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    variants: list[str]


class QueryPlanner:
    def plan(self, request, bundle=None, *, redactor, knowledge_cutoff=None, query_variants=1):
        question = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", request.question)).strip()
        intent = "explain_anomaly"
        if request.report_type == "knowledge_answer":
            intent = "knowledge_question"
        elif re.search(r"比较|对比|区别|鉴别|区分|compare|versus|\bvs\b", question, re.I):
            intent = "compare_causes"
        elif re.search(r"检查|排查|怎么查|下一步|check|next step", question, re.I):
            intent = "suggest_checks"
        filters = {"os": "linux"}
        if knowledge_cutoff:
            filters["knowledge_cutoff"] = str(knowledge_cutoff)
        facts, types, protocols = [], set(), set()
        if bundle:
            observed_interfaces = {e.scope["interface"] for e in bundle.evidence if e.scope.get("interface")}
            wifi_interfaces = {e.scope["interface"] for e in bundle.evidence
                               if e.metric == "wifi_rssi_dbm" and e.summary.get("valid_samples", 0)
                               and e.scope.get("interface")}
            if observed_interfaces and observed_interfaces <= wifi_interfaces:
                types.add("wifi")
            aliases = {"probe_failure": "icmp_timeout", "interface_down": "link_down",
                       "tcp_retransmission": "tcp_retransmissions", "incomplete_telemetry": "missing_data",
                       "counter_discontinuity": "counter_reset"}
            filters["symptoms"] = sorted(set(bundle.symptoms) | {aliases[s] for s in bundle.symptoms if s in aliases})
            for e in bundle.evidence:
                if e.metric == "wifi_rssi_dbm" and e.summary.get("valid_samples", 0):
                    direction = e.summary.get("trend", {}).get("direction")
                    label = {"declining": "持续下降", "abrupt_drop": "突然下降", "recovering": "恢复",
                             "stable": "稳定", "fluctuating": "波动"}.get(direction)
                    if label:
                        facts.append("RSSI趋势：" + label)
                if e.metric.startswith("icmp_"):
                    protocols.add("icmp")
                if e.metric in ("tcp_segments", "tcp_retransmission_log"):
                    protocols.add("tcp")
                if e.scope.get("protocol") in {"tcp", "udp", "icmp", "icmpv6"}:
                    protocols.add(e.scope["protocol"])
            if types:
                filters["interface_type"] = sorted(types)
            if protocols:
                filters["protocols"] = sorted(protocols)
        query = query_from_evidence(question, bundle)
        extras = (["WiFi 接口"] if types == {"wifi"} else []) + sorted(set(facts))
        if bundle and {"interface_down", "permission_denied"}.intersection(bundle.symptoms):
            # Incidental wireless readings must not dilute a directly observed failure.
            extras = []
        if extras:
            query += "\n" + "；".join(extras)
        # Raw identifiers stay in the locally persisted plan, not model embeddings.
        query = redactor.text(query)
        variants = [query]
        if query_variants == 2:
            extension = {"compare_causes": " 鉴别 支持证据 反证 适用条件",
                         "suggest_checks": " 下一步检查 前提条件 缺失事实",
                         "knowledge_question": " 原理 定义 适用范围 限制",
                         "explain_anomaly": " 鉴别 反证 适用条件 下一步检查"}[intent]
            variants.append(query + extension)
        return QueryPlan(original_query=request.question, normalized_query=question, intent=intent,
                         facts=sorted(set((bundle.symptoms if bundle else []) + facts)),
                         filters=filters, variants=variants)
