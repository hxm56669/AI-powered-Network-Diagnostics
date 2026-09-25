"""Scope-first sparse/dense search, rank fusion and bounded per-instance caches."""
from __future__ import annotations

import copy
import heapq
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping

import numpy as np

from ..indexing import HistoricalKnowledgeUnavailable, Snapshot, SnapshotError
from ..knowledge.common import KnowledgeError, citation_is_revoked, digest, timestamp
from ..providers.embedding import EmbeddingError, make_online_embedding, normalized
from ..providers.reranker import RerankerError, RerankerTimeout, make_reranker


class BoundedCache:
    def __init__(self, capacity: int = 256, ttl_s: float = 60):
        self.capacity = max(0, int(capacity))
        self.ttl = max(0, float(ttl_s))
        self.values = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            entry = self.values.pop(key, None)
            if entry is None:
                return None
            expires, value = entry
            if time.monotonic() >= expires:
                return None
            self.values[key] = entry
            return copy.deepcopy(value)

    def put(self, key, value):
        if not self.capacity:
            return
        with self.lock:
            self.values.pop(key, None)
            self.values[key] = (time.monotonic() + self.ttl, copy.deepcopy(value))
            while len(self.values) > self.capacity:
                self.values.popitem(last=False)

    def __len__(self):
        with self.lock:
            return len(self.values)


def rrf(rankings: Mapping[str, list[dict]], k: int = 60,
        weights: Mapping[str, float] | None = None) -> list[dict]:
    if k <= 0:
        raise KnowledgeError("rrf_k_must_be_positive")
    weights = weights or {}
    merged = {}
    for route, records in rankings.items():
        seen = set()
        rank = 0
        weight = float(weights.get(route, 1.0))
        if not np.isfinite(weight) or weight < 0:
            raise KnowledgeError("rrf_weight_must_be_finite_nonnegative")
        for record in records:
            chunk_id = record["chunk_id"]
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank += 1
            if chunk_id not in merged:
                merged[chunk_id] = {**copy.deepcopy(record), "score": 0.0, "ranks": {}, "route_scores": {}}
            result = merged[chunk_id]
            result["score"] += weight / (k + rank)
            result["ranks"][route] = rank
            result["route_scores"][route] = float(record.get("score", 0))
    return sorted(merged.values(), key=lambda c: (-c["score"], c["chunk_id"]))


def _values(value):
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, set, tuple)) and all(isinstance(x, str) for x in value):
        return set(value)
    raise KnowledgeError("filter_requires_string_or_string_list")


def _compatible(allowed, requested):
    allowed, requested = _values(allowed), _values(requested)
    return not requested or not allowed or bool(allowed & {"generic", "all", "*"}) or bool(allowed & requested)


def is_revoked(candidate: dict, revoked: set[str]) -> bool:
    meta = candidate.get("metadata", {})
    return (meta.get("review_status") in {"draft", "revoked"} or bool(meta.get("revoked"))
            or citation_is_revoked(candidate, revoked))


def _allowed(candidate, filters, revoked, default_time, allow_seed):
    if is_revoked(candidate, revoked):
        return False
    meta = candidate["metadata"]
    reviews = {"approved", "code-reviewed", "seed"} if allow_seed else {"approved"}
    if meta.get("review_status") not in reviews:
        return False
    scope = filters.get("scope", {})
    if isinstance(scope, str):
        scope = {"organizations": [scope]}
    if not isinstance(scope, dict):
        raise KnowledgeError("invalid_scope_filter")
    orgs = _values(filters.get("organizations", scope.get("organizations")))
    orgs |= _values(filters.get("organization_id", filters.get("org_id", filters.get("tenant_id"))))
    permitted = set(meta.get("scope", {}).get("organizations", ["public"]))
    if "public" not in permitted and not orgs.intersection(permitted):
        return False
    cutoff = timestamp(filters.get("knowledge_cutoff", filters.get("as_of", default_time)))
    valid_at = timestamp(filters.get("valid_at", filters.get("window_end", cutoff)))
    if timestamp(meta["available_at"]) > cutoff:
        return False
    if meta.get("reviewed_at") and timestamp(meta["reviewed_at"]) > cutoff:
        return False
    if timestamp(meta["valid_from"]) > valid_at:
        return False
    if meta.get("valid_until") and timestamp(meta["valid_until"]) <= valid_at:
        return False
    applies = meta.get("applies_to", {})
    if not _compatible(applies.get("os"), filters.get("os")):
        return False
    if not _compatible(applies.get("product_versions"), filters.get("product_versions", filters.get("product_version"))):
        return False
    for key in ("doc_ids", "revision_ids", "chunk_ids"):
        if key in filters and candidate[key[:-1]] not in _values(filters[key]):
            return False
    return True


def _preferred(candidate, filters):
    meta = candidate["metadata"]
    applies = meta.get("applies_to", {})
    return (_compatible(applies.get("interface_type"), filters.get("interface_type"))
            and _compatible(applies.get("protocols"), filters.get("protocols", filters.get("protocol")))
            and _compatible(meta.get("symptoms"), filters.get("symptoms")))


class Retriever:
    def __init__(self, settings: Mapping, snapshot: Snapshot, *, budget=None):
        self.settings = copy.deepcopy(dict(settings))
        self.snapshot = snapshot
        config = self.settings.get("retrieval", {})
        self.mode = config.get("mode", "bm25")
        if self.mode not in {"bm25", "dense", "hybrid", "all_context"}:
            raise KnowledgeError("invalid_retrieval_mode")
        self.cache = BoundedCache(config.get("cache_size", 256), config.get("cache_ttl_s", 60))
        self.rerank_cache = BoundedCache(self.settings.get("reranker", {}).get("cache_size", 2048), 3600)
        self.config_hash = digest(self.settings)
        self.embedding = None
        self.reranker = None
        self.degradations = list(snapshot.manifest.get("degradations", []))
        self.last_trace = {}
        self._lock = threading.RLock()
        self._closed = False
        allow_fallback = self.settings.get("fallback", {}).get("allow_lexical_only", True)
        if self.mode in {"dense", "hybrid"}:
            if snapshot.dense is None:
                if not allow_fallback:
                    raise SnapshotError("dense_snapshot_unavailable")
                self.mode = "bm25"
                if self.settings.get("profile") != "lexical-only":
                    self.degradations.append("dense_unavailable")
            else:
                try:
                    self.embedding = make_online_embedding(self.settings, budget)
                    if self.embedding is None or self.embedding.identity != snapshot.manifest["embedding"]:
                        if self.embedding is not None and hasattr(self.embedding, "close"):
                            self.embedding.close()
                        raise SnapshotError("snapshot_query_encoder_mismatch")
                except (TimeoutError, InterruptedError):
                    raise
                except (EmbeddingError, ImportError, OSError) as exc:
                    if not allow_fallback:
                        raise SnapshotError("query_encoder_unavailable") from exc
                    self.mode = "bm25"
                    self.degradations.append("dense_unavailable")
        if self.mode != "all_context" and self.settings.get("reranker", {}).get("enabled", False):
            try:
                self.reranker = make_reranker(self.settings, budget=budget)
            except (TimeoutError, InterruptedError):
                self.close()
                raise
            except (RerankerError, ImportError, OSError):
                self.degradations.append("reranker_unavailable")

    def search(self, query: str, filters: dict | None = None,
               revoked: set[str] | None = None, budget=None, *, variants=None) -> list[dict]:
        if not isinstance(query, str):
            raise KnowledgeError("query_requires_string")
        if len(query) > 16000:
            raise KnowledgeError("retrieval_query_too_long")
        if variants is not None and (not 1 <= len(variants) <= 2 or variants[0] != query or
                any(not isinstance(v, str) or not v.strip() or len(v) > 16000 for v in variants)):
            raise KnowledgeError("invalid_query_variants")
        filters = copy.deepcopy(filters or {})
        revoked = set(revoked or ())
        self._check(budget)
        if budget is None:
            self._lock.acquire()
        else:
            while not self._lock.acquire(timeout=min(.05, budget.check())):
                self._check(budget)
        try:
            self._check(budget)
            return self._search(query, filters, revoked, budget, variants)
        finally:
            self._lock.release()

    def _check(self, budget):
        if self._closed:
            raise InterruptedError("retriever_closed")
        if budget is not None:
            budget.check()

    def _search(self, query, filters, revoked, budget=None, variants=None):
        self.last_trace = {"generation_id": self.snapshot.generation_id, "mode": self.mode,
                           "degradations": list(self.degradations), "cache_hit": False,
                           "quality_evaluation_eligible": self.settings.get("profile") != "offline-fixture"}
        if not query.strip():
            return []
        cutoff = filters.get("knowledge_cutoff", filters.get("as_of"))
        corpus_time = self.snapshot.manifest["corpus_as_of"]
        if cutoff is not None and timestamp(cutoff) < timestamp(corpus_time):
            raise HistoricalKnowledgeUnavailable(
                "historical_knowledge_unavailable: select/build a snapshot whose corpus_as_of is not later than knowledge_cutoff")
        config = self.settings.get("retrieval", {})
        allow_seed = self.settings.get("knowledge", {}).get("allow_seed", False)
        allowed = [i for i, c in enumerate(self.snapshot.chunks)
                   if _allowed(c, filters, revoked, corpus_time, allow_seed)]
        self._check(budget)
        # Recheck time eligibility on every query; equivalent eligible corpora may
        # share results without reusing expired or newly revoked knowledge.
        cache_filters = {k: v for k, v in filters.items()
                         if k not in {"knowledge_cutoff", "as_of", "valid_at", "window_end"}}
        key = digest({"query": query, "variants": variants, "filters": cache_filters,
                      "allowed": allowed, "revoked": sorted(revoked),
                      "generation": self.snapshot.generation_id, "config": self.config_hash,
                      "mode": self.mode, "degradations": self.degradations})
        cached = self.cache.get(key)
        if cached is not None:
            records, trace = cached
            self.last_trace = {**trace, "cache_hit": True}
            self._check(budget)
            return records
        preferred = [i for i in allowed if _preferred(self.snapshot.chunks[i], filters)]
        self.last_trace["hard_allowed_count"] = len(allowed)
        if self.mode != "all_context" and len(preferred) >= int(config.get("final_top_n", 6)):
            allowed = preferred
        elif len(preferred) < len(allowed) and self.mode != "all_context":
            self.last_trace["soft_filter_expanded"] = True
        self.last_trace["scanned_allowed_count"] = len(allowed)
        if not allowed:
            self.last_trace["unfilled_reason"] = "no_eligible_knowledge"
            return []
        if self.mode == "all_context":
            # A full view is persisted with its own content-addressed ID, source spans
            # and document-root parent. Never relabel a partial child as a whole document.
            allowed_ids = {self.snapshot.chunks[i]["chunk_id"] for i in allowed}
            complete = [c for c in self.snapshot.full_contexts
                        if not is_revoked(c, revoked)
                        and set(c["metadata"]["constituent_chunk_ids"]) <= allowed_ids]
            complete.sort(key=lambda c: (c["doc_id"], c["revision_id"]))
            result = [{**copy.deepcopy(c), "score": 0.0,
                       "ranks": {"all_context": rank}, "route_scores": {}}
                      for rank, c in enumerate(complete, 1)]
        else:
            if variants is None:
                variants = [query]
                if int(config.get("query_variants", 1)) == 2:
                    variants.append(query + " 鉴别 反证 适用条件 下一步检查")
            routes, weights = {}, {}
            dense_failed = False
            dense_params = None
            for variant_index, variant in enumerate(variants):
                self._check(budget)
                if self.mode in {"bm25", "hybrid"}:
                    name = "bm25" if len(variants) == 1 else f"bm25:{variant_index}"
                    routes[name] = self._bm25(variant, allowed)
                    weights[name] = float(config.get("bm25_weight", 1)) / len(variants)
                if self.mode in {"dense", "hybrid"}:
                    try:
                        import faiss

                        query_vector = normalized(self.embedding.encode([variant], budget=budget), self.snapshot.dense.d)
                        self._check(budget)
                        # Filter inside FAISS, reusing the immutable full index. IDs are
                        # global rows; no vector reconstruction or per-query sub-index.
                        if dense_params is None and len(allowed) != len(self.snapshot.chunks):
                            selector = faiss.IDSelectorBatch(np.asarray(allowed, dtype=np.int64))
                            dense_params = faiss.SearchParameters(sel=selector)
                        scores, positions = self.snapshot.dense.search(
                            query_vector, len(allowed), params=dense_params)
                        scored = heapq.nsmallest(int(config.get("dense_top_k", 40)),
                            ((int(pos), float(score)) for pos, score in zip(positions[0], scores[0]) if pos >= 0),
                            key=lambda item: (-item[1], self.snapshot.chunks[item[0]]["chunk_id"]))
                        name = "dense" if len(variants) == 1 else f"dense:{variant_index}"
                        routes[name] = [{**self.snapshot.chunks[i], "score": score}
                                        for i, score in scored]
                        weights[name] = float(config.get("dense_weight", 1)) / len(variants)
                    except (TimeoutError, InterruptedError):
                        raise
                    except (EmbeddingError, OSError, RuntimeError):
                        self._check(budget)
                        if (self.settings.get("profile") == "offline-fixture" or
                                not self.settings.get("fallback", {}).get("allow_lexical_only", True)):
                            raise
                        dense_failed = True
            if dense_failed:
                self.last_trace["degradations"].append("dense_unavailable")
                routes = {k: v for k, v in routes.items() if not k.startswith("dense")}
                if not routes:
                    routes["bm25"] = self._bm25(query, allowed)
            result = rrf(routes, int(config.get("rrf_k", 60)), weights)[:int(config.get("candidate_limit", 120))]
            self.last_trace["route_counts"] = {name: len(values) for name, values in routes.items()}
            self.last_trace["fused_chunk_ids"] = [c["chunk_id"] for c in result]
            if self.reranker is not None and result:
                self._check(budget)
                result = result[:int(config.get("rerank_top_n", 30))]
                try:
                    scored = self._rerank(query, result, budget)
                    for record, value in zip(result, scored):
                        record["rrf_score"] = record["score"]
                        record["score"] = float(value["score"])
                        record["rerank"] = value
                    result.sort(key=lambda c: (-c["score"], c["chunk_id"]))
                    for rank, record in enumerate(result, 1):
                        record["ranks"]["reranker"] = rank
                except (TimeoutError, InterruptedError):
                    raise
                except (RerankerError, OSError, EOFError) as exc:
                    self._check(budget)
                    self.last_trace["degradations"].append(
                        "rerank_timeout" if isinstance(exc, RerankerTimeout) else "reranker_unavailable")
        self._check(budget)
        for record in result:
            record["metadata"]["generation_id"] = self.snapshot.generation_id
            record["metadata"]["degradations"] = list(self.last_trace["degradations"])
        self.last_trace["returned_count"] = len(result)
        # Startup degradation is stable for this instance. A failure during this
        # query must not prevent a later query from trying the provider again.
        if self.last_trace["degradations"] == self.degradations:
            self.cache.put(key, (result, self.last_trace))
        return result

    def _bm25(self, query, allowed):
        tokens = self.snapshot.tokenizer(query)
        scores = self.snapshot.bm25.get_scores(tokens) if tokens else np.zeros(len(self.snapshot.chunks))
        limit = int(self.settings.get("retrieval", {}).get("bm25_top_k", 40))
        indices = heapq.nsmallest(limit, (i for i in allowed if scores[i] > 0),
                                 key=lambda i: (-float(scores[i]), self.snapshot.chunks[i]["chunk_id"]))
        return [{**self.snapshot.chunks[i], "score": float(scores[i])} for i in indices]

    def _rerank(self, query, candidates, budget=None):
        config = self.settings.get("reranker", {})
        keys, resolved, pending = [], {}, {}
        for c in candidates:
            key = digest({"query": query, "title": c.get("title", ""),
                          "body": c.get("body_text", c["text"]),
                          "safety": c.get("metadata", {}).get("safety_context", ""),
                          "model": config, "truncation_policy": "safety-first-query-match-v1",
                          "fixture_id": c["chunk_id"] if "fixture_scores" in config else None})
            keys.append(key)
            if key in resolved or key in pending:
                continue
            value = self.rerank_cache.get(key)
            if value is not None:
                resolved[key] = value
            else:
                # Equal model inputs share one score; keys below restore the full
                # candidate order, including distinct chunks with identical text.
                pending[key] = c
        if pending:
            scored = self.reranker.score(query, list(pending.values()), budget=budget)
            self._check(budget)
            if len(scored) != len(pending) or not all(np.isfinite(float(x["score"])) for x in scored):
                raise RerankerError("reranker_result_count_mismatch")
            for key, value in zip(pending, scored):
                self.rerank_cache.put(key, value)
                resolved[key] = copy.deepcopy(value)
        # Cache hits already own a copy. Only repeated inputs need another one
        # so no two candidates share mutable score/truncation metadata.
        result, seen = [], set()
        for key in keys:
            value = resolved[key]
            result.append(copy.deepcopy(value) if key in seen else value)
            seen.add(key)
        return result

    def expand_neighbors(self, candidates, filters, revoked, budget=None):
        """Append at most one nearest eligible sibling per selected parent.

        Original hits retain ordering and budget priority. Expansion entries are
        identifiable and do not claim a retrieval/rerank score or model judgment.
        """
        from .context import pack_context
        self._check(budget)
        if self.mode == "all_context" or not self.settings.get("context", {}).get("expand_neighbors", True):
            return candidates
        token_budget = self.settings.get("context", {}).get("knowledge_tokens", 4500)
        limit = self.settings.get("retrieval", {}).get("final_top_n", 6)
        base = pack_context(candidates, token_budget, limit)
        present = {c["chunk_id"] for c in candidates}
        result = list(candidates)
        for anchor in base:
            self._check(budget)
            if len(anchor.get("chunk_ids", [anchor["chunk_id"]])) >= 2:
                continue
            def start(c):
                return min((s["start"] for s in c.get("metadata", {}).get("source_spans", [])), default=0)
            siblings = [c for c in self.snapshot.children_by_parent.get(anchor["parent_id"], ())
                        if c["chunk_id"] not in present
                        and _allowed(c, filters, revoked, self.snapshot.manifest["corpus_as_of"],
                                     self.settings.get("knowledge", {}).get("allow_seed", False))]
            siblings.sort(key=lambda c: (abs(start(c) - start(anchor)), c["chunk_id"]))
            for sibling in siblings[:1]:
                addition = copy.deepcopy(sibling)
                addition.update(context_expansion_of=anchor["chunk_id"], ranks={}, route_scores={})
                addition["metadata"]["generation_id"] = self.snapshot.generation_id
                proposed = pack_context(result + [addition], token_budget, limit)
                if any(addition["chunk_id"] in c.get("chunk_ids", [c["chunk_id"]]) for c in proposed):
                    result.append(addition)
                    present.add(addition["chunk_id"])
        return result

    def close(self):
        # Do not wait for the search lock: shutdown must be able to kill in-flight work.
        self._closed = True
        if self.reranker is not None:
            self.reranker.close()
        if self.embedding is not None and hasattr(self.embedding, "close"):
            self.embedding.close()
        with self.cache.lock:
            self.cache.values.clear()
        with self.rerank_cache.lock:
            self.rerank_cache.values.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
