"""Whole evidence packing: retain provenance and safety conditions, never cut to fit."""
from __future__ import annotations

import copy
import json

from .core import is_revoked


class AllContextBudgetExceeded(ValueError):
    """Full context exceeds the budget; never silently replace it with top-k."""
    code = "all_context_budget_exceeded"


def render_context(candidates: list[dict]) -> str:
    """The generation payload; full provenance stays on the returned candidates."""
    payload = [{"chunk_id": c["chunk_id"], "text": c["text"],
                "cause_ids": c.get("metadata", {}).get("cause_ids", []),
                "checks": [{k: check[k] for k in ("check_id", "purpose")} for check in
                           c.get("metadata", {}).get("checks", [])],
                "decision_label": c.get("decision_label", "unassessed")} for c in candidates]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def context_token_count(candidates: list[dict]) -> int:
    # Explicit conservative estimate, not a claim to use the generation tokenizer.
    # Count the actual payload rather than repeated metadata and retrieval traces.
    return len(render_context(candidates).encode("utf-8")) + 2


def _citation(record: dict) -> dict:
    return {"chunk_id": record["chunk_id"], "doc_id": record["doc_id"],
            "revision_id": record["revision_id"], "parent_id": record["parent_id"],
            "source_uri": record["source_uri"],
            "source_path": record.get("metadata", {}).get("source_path"),
            "source_spans": record.get("metadata", {}).get("source_spans", []),
            "safety_source_spans": record.get("metadata", {}).get("safety_source_spans", []),
            "generation_id": record.get("metadata", {}).get("generation_id")}


def _overlap_join(left: str, right: str) -> str:
    for size in range(min(len(left), len(right)), 0, -1):
        if left[-size:] == right[:size]:
            return left + right[size:]
    return left + "\n\n" + right


def pack_all_context(candidates, token_budget: int) -> list[dict]:
    """All eligible full-document views or an explicit budget error."""
    if token_budget < 0:
        raise ValueError("negative_context_budget")
    result, seen = [], set()
    for original in candidates:
        if original.get("metadata", {}).get("context_type") != "full_document":
            raise ValueError("all_context_requires_full_document_candidates")
        if is_revoked(original, set()):
            raise ValueError("all_context_contains_revoked_document")
        if original["chunk_id"] in seen:
            continue
        seen.add(original["chunk_id"])
        record = copy.deepcopy(original)
        record["citations"] = [_citation(original)]
        result.append(record)
    if result and context_token_count(result) > token_budget:
        raise AllContextBudgetExceeded("all_context_budget_exceeded")
    return result


def pack_context(candidates, token_budget: int, max_chunks: int) -> list[dict]:
    candidates = list(candidates)
    all_context = bool(candidates) and all("all_context" in c.get("ranks", {}) for c in candidates)
    if token_budget < 0 or max_chunks < 0:
        raise ValueError("negative_context_budget")
    if all_context:
        return pack_all_context(candidates, token_budget)
    if token_budget == 0 or max_chunks == 0:
        return []
    selected, seen = [], set()
    parent_members = {}
    parent_indices, payload_sizes = {}, []
    running_size = context_token_count([])
    for original in candidates:
        if original["chunk_id"] in seen or is_revoked(original, set()):
            continue
        seen.add(original["chunk_id"])
        parent = original["parent_id"]
        members = parent_members.get(parent, [])
        if len(members) >= 2:
            continue
        if members:
            index = parent_indices[parent]
            ordered = sorted(members + [original], key=lambda c: (
                c.get("metadata", {}).get("source_spans", [{}])[0].get("start", 0), c["chunk_id"]))
            body = ordered[0].get("body_text", ordered[0]["text"])
            for record in ordered[1:]:
                body = _overlap_join(body, record.get("body_text", record["text"]))
            candidate = copy.deepcopy(selected[index])
            safety = candidate.get("metadata", {}).get("safety_context", "")
            candidate["text"] = candidate["title"] + "\n" + safety + "\n\n" + body
            candidate["citations"] = [_citation(c) for c in ordered]
            candidate["chunk_ids"] = [c["chunk_id"] for c in ordered]
            labels = {c.get("decision_label", "unassessed") for c in ordered}
            if any("decision_label" in c for c in ordered):
                priority = {"counterevidence": 0, "applicable": 1, "background": 2, "unassessed": 3, "exclude": 4}
                candidate["decision_label"] = min(labels, key=lambda label: priority[label])
                candidate["decision_labels"] = sorted(labels)
            candidate["metadata"]["context_citations"] = candidate["citations"]
            candidate["metadata"]["source_spans"] = [
                span for c in ordered for span in c.get("metadata", {}).get("source_spans", [])]
        else:
            if len(selected) >= max_chunks:
                continue
            index = len(selected)
            candidate = copy.deepcopy(original)
            # Retrieval-only text/rank traces are not sent to the model twice.
            candidate.pop("body_text", None)
            candidate.pop("route_scores", None)
            candidate.pop("rerank", None)
            candidate["citations"] = [_citation(original)]
        # Singleton serialization minus [] and the conservative +2 overhead.
        # Only a new/merged entry is rendered; JSON list separators cost one byte.
        size = context_token_count([candidate]) - 4
        proposed_size = running_size + size - payload_sizes[index] if members else (
            running_size + size + bool(selected))
        if proposed_size <= token_budget:
            if members:
                selected[index] = candidate
                payload_sizes[index] = size
            else:
                parent_indices[parent] = index
                selected.append(candidate)
                payload_sizes.append(size)
            running_size = proposed_size
            parent_members[parent] = members + [original]
        elif not selected:
            # An oversized top passage leaves no usable context; generation must
            # fail rather than substitute a short, unrelated tail candidate.
            return []
    return selected
