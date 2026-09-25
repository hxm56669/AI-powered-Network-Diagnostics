"""Content-addressed immutable snapshots, checked loads and atomic CURRENT publication."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path

import numpy as np

from ..knowledge import KnowledgeError, chunk_document, load_knowledge
from ..knowledge.common import canonical, digest, iso, timestamp
from ..knowledge.tokenization import ChineseTokenizer, ConservativeTokenizer, read_domain_terms
from ..providers.embedding import EmbeddingError, make_embedding
from ..maintenance import require_capacity
from ..util import atomic_write, fsync_dir
from .vector_cache import encode_cached


class SnapshotError(KnowledgeError):
    pass


class HistoricalKnowledgeUnavailable(SnapshotError):
    pass


def _root(settings: Mapping) -> Path:
    if not settings.get("data_dir"):
        raise SnapshotError("data_dir_required")
    return Path(settings["data_dir"]).resolve() / "indexes"


def _file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while data := handle.read(1024 * 1024):
            hasher.update(data)
    return hasher.hexdigest()


@contextlib.contextmanager
def _lock(root: Path, timeout_s: float = 30):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".build-publish.lock").open("a+b") as handle:
        until = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise SnapshotError("index_writer_busy")
                time.sleep(0.025)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _label(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise SnapshotError("invalid_index_label")
    return value


def _resolve(root: Path, value: str | None) -> str:
    if value is None:
        path = root / "CURRENT"
        if not path.is_file() or path.is_symlink():
            raise SnapshotError("index_not_published")
        value = path.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        path = root / "labels" / (_label(value) + ".json")
        if not path.is_file() or path.is_symlink():
            raise SnapshotError(f"index_generation_or_label_missing: {value}")
        value = json.loads(path.read_text())["generation_id"]
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise SnapshotError("invalid_generation_id")
    return value


@dataclass
class Snapshot:
    manifest: dict
    chunks: list[dict]
    path: Path
    documents: list[dict] = field(default_factory=list)
    tokenizer: ChineseTokenizer | None = None
    bm25: object = None
    dense: object = None
    recovery: dict | None = None

    @property
    def generation_id(self) -> str:
        return self.manifest["generation_id"]

    @cached_property
    def full_contexts(self) -> list[dict]:
        return [d["full_context"] for d in self.documents]

    @cached_property
    def children_by_parent(self) -> dict[str, list[dict]]:
        children = {}
        for chunk in self.chunks:
            children.setdefault(chunk["parent_id"], []).append(chunk)
        return children


def _choose_documents(documents, cutoff, allow_seed):
    grouped = {}
    excluded = Counter()
    statuses = {"approved", "code-reviewed", "seed"} if allow_seed else {"approved"}
    for doc in documents:
        meta = doc.metadata
        if meta["review_status"] not in statuses:
            excluded[meta["review_status"]] += 1
            continue
        if timestamp(meta["available_at"]) > cutoff or (meta.get("reviewed_at") and
                                                       timestamp(meta["reviewed_at"]) > cutoff):
            excluded["not_yet_available"] += 1
            continue
        if timestamp(meta["valid_from"]) > cutoff or (meta["valid_until"] is not None and
                                                     timestamp(meta["valid_until"]) <= cutoff):
            excluded["not_valid_at_cutoff"] += 1
            continue
        previous = grouped.get(doc.doc_id)
        if previous and previous.metadata["available_at"] == meta["available_at"]:
            raise SnapshotError(f"ambiguous_revision_at_same_time: {doc.doc_id}")
        if previous is None or timestamp(previous.metadata["available_at"]) < timestamp(meta["available_at"]):
            grouped[doc.doc_id] = doc
    return sorted(grouped.values(), key=lambda d: d.doc_id), dict(excluded)


def _embedding_for_build(settings):
    try:
        return make_embedding(settings), []
    except Exception as exc:
        # Bad config and fixture mistakes are never hidden as a model outage.
        allow = settings.get("fallback", {}).get("allow_lexical_only", True)
        recoverable = isinstance(exc, (ImportError, OSError)) or (
            isinstance(exc, EmbeddingError) and str(exc) == "bge_requires_models_extra")
        if allow and recoverable:
            return None, ["dense_unavailable"]
        raise


def build_index(settings: Mapping, knowledge_dir: Path, label: str = "initial",
                as_of: str | None = None) -> dict:
    """Build a complete candidate generation. Does not change CURRENT."""
    import bm25s

    root = _root(settings)
    label = _label(label)
    cutoff = timestamp(as_of) if as_of is not None else datetime.now(timezone.utc)
    with _lock(root), contextlib.ExitStack() as resources:
        require_capacity(settings, nonessential=True)
        documents = load_knowledge(Path(knowledge_dir))
        allow_seed = bool(settings.get("knowledge", {}).get("allow_seed", False))
        selected, excluded = _choose_documents(documents, cutoff, allow_seed)
        if not selected:
            raise HistoricalKnowledgeUnavailable("historical_knowledge_unavailable: no eligible revisions")
        embedding, degradations = _embedding_for_build(settings)
        if embedding is not None and hasattr(embedding, "close"):
            resources.callback(embedding.close)
        token_counter = embedding.tokenizer if embedding else ConservativeTokenizer()
        chunk_config = settings.get("chunking", {})
        chunk_args = {"max_input_tokens": int(settings.get("embedding", {}).get("max_input_tokens", 512)),
                      "child_tokens": int(chunk_config.get("child_tokens", 450)),
                      "parent_tokens": int(chunk_config.get("parent_tokens", 1000)),
                      "overlap_tokens": int(chunk_config.get("overlap_tokens", 50))}
        chunks, parents = [], []
        for doc in selected:
            child, parent = chunk_document(doc, token_counter, **chunk_args)
            chunks.extend(child)
            parents.extend(parent)
        document_payloads = []
        for doc in selected:
            root_parent = digest({"revision_id": doc.revision_id, "view": "document-root-v1"})
            full_id = digest({"revision_id": doc.revision_id, "view": "full-document-v1", "text": doc.text})
            full_context = {"chunk_id": full_id, "doc_id": doc.doc_id, "revision_id": doc.revision_id,
                            "parent_id": root_parent, "text": doc.text,
                            "title": doc.metadata["title"], "source_uri": doc.metadata["source_uri"],
                            "metadata": {**doc.metadata, "source_path": doc.source_path,
                                         "context_type": "full_document",
                                         "source_spans": [{"start": 0, "end": len(doc.text),
                                                           "line_start": doc.body_start_line,
                                                           "line_end": doc.body_start_line + doc.text.count("\n")}],
                                         "constituent_chunk_ids": sorted(c["chunk_id"] for c in chunks
                                                                         if c["revision_id"] == doc.revision_id)}}
            document_payloads.append({"doc_id": doc.doc_id, "revision_id": doc.revision_id,
                                      "metadata": doc.metadata, "source_path": doc.source_path,
                                      "full_context": full_context})
            parents.append({"parent_id": root_parent, "doc_id": doc.doc_id,
                            "revision_id": doc.revision_id, "text": doc.text,
                            "context_type": "full_document"})
        chunks.sort(key=lambda c: c["chunk_id"])
        if len({c["chunk_id"] for c in chunks}) != len(chunks):
            raise SnapshotError("duplicate_chunk_ids")
        tokenizer = ChineseTokenizer(terms=read_domain_terms(Path(knowledge_dir)))
        retrieval = settings.get("retrieval", {})
        model = bm25s.BM25(k1=float(retrieval.get("bm25_k1", 1.2)),
                           b=float(retrieval.get("bm25_b", 0.75)), method="lucene", backend="numpy")
        tokenized = [tokenizer(c["text"]) for c in chunks]
        vocabulary = {token: i for i, token in enumerate(sorted({t for row in tokenized for t in row}))}
        if not vocabulary:
            raise SnapshotError("empty_sparse_vocabulary")
        model.index(([[vocabulary[t] for t in row] for row in tokenized], vocabulary), show_progress=False)
        stage_root = root / "staging"
        stage_root.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="build-", dir=stage_root))
        try:
            with sqlite3.connect(stage / "metadata.sqlite") as database:
                database.executescript("""
                    CREATE TABLE documents(doc_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, payload TEXT NOT NULL);
                    CREATE TABLE chunks(row_id INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE NOT NULL,
                                        doc_id TEXT NOT NULL, revision_id TEXT NOT NULL, parent_id TEXT NOT NULL,
                                        payload TEXT NOT NULL);
                    CREATE TABLE parents(parent_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                """)
                database.executemany("INSERT INTO documents VALUES(?,?,?)", [
                    (d["doc_id"], d["revision_id"], canonical(d).decode()) for d in document_payloads])
                database.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?)", [
                    (i, c["chunk_id"], c["doc_id"], c["revision_id"], c["parent_id"], canonical(c).decode())
                    for i, c in enumerate(chunks)])
                database.executemany("INSERT INTO parents VALUES(?,?)", [
                    (p["parent_id"], canonical(p).decode()) for p in parents])
            model.save(str(stage / "bm25"), allow_pickle=False)
            (stage / "tokenizer.json").write_bytes(canonical(tokenizer.state))
            dense_identity = {"provider": "none", "dimension": 0, "normalize": True,
                              "quality_evaluation_eligible": settings.get("profile") != "offline-fixture"}
            cache_stats = {"enabled": False, "hits": 0, "misses": 0, "invalidated": 0}
            if embedding is not None:
                import faiss

                require_capacity(settings, reserve_bytes=len(chunks) * embedding.dimension * 8,
                                 nonessential=True)
                vectors, cache_stats = encode_cached(embedding, chunks, root / "embedding-cache.sqlite",
                    settings.get("maintenance", {}).get("embedding_cache_max_entries", 100000))
                dense_index = faiss.IndexFlatIP(embedding.dimension)
                dense_index.add(vectors)
                faiss.write_index(dense_index, str(stage / "dense.faiss"))
                dense_identity = embedding.identity
                (stage / "dense_row_map.jsonl").write_text("".join(
                    canonical({"row": i, "chunk_id": c["chunk_id"]}).decode() + "\n"
                    for i, c in enumerate(chunks)))
            files = {str(p.relative_to(stage)): _file_hash(p) for p in sorted(stage.rglob("*")) if p.is_file()}
            reviews = dict(Counter(d.metadata["review_status"] for d in selected))
            deps = {name: importlib.metadata.version(name) for name in ("bm25s", "jieba", "numpy", "PyYAML")}
            if embedding is not None:
                deps["faiss-cpu"] = importlib.metadata.version("faiss-cpu")
            manifest = {"schema_version": 1, "corpus_as_of": iso(cutoff),
                        "built_at": iso(datetime.now(timezone.utc)),
                        "document_count": len(selected), "chunk_count": len(chunks),
                        "parent_count": len(parents), "review_counts": reviews,
                        "production_review_complete": all(d.metadata["review_status"] == "approved" for d in selected),
                        "excluded_revisions": excluded, "seed_policy": "allow" if allow_seed else "reject",
                        "corpus_hash": digest([(d.doc_id, d.revision_id) for d in selected]),
                        "embedding": dense_identity, "chunking": {**chunk_args, "tokenizer": token_counter.identity},
                        "bm25": {"method": "lucene", "k1": model.k1, "b": model.b,
                                 "statistics_as_of": iso(cutoff), "tokenizer_hash": tokenizer.fingerprint},
                        "dependencies": deps, "dependency_fingerprint": digest(deps),
                        "profile": settings.get("profile", "lexical-only"),
                        "build": {"vector_cache": cache_stats},
                        "actual_mode": "fixture" if dense_identity["provider"] == "fixture" else (
                            "hybrid" if embedding is not None else "lexical-only"),
                        "degradations": degradations, "files": files}
            manifest["generation_id"] = digest(manifest)
            (stage / "manifest.json").write_bytes(canonical(manifest))
            (stage / "checksums.json").write_bytes(canonical(files))
            for path in stage.rglob("*"):
                if path.is_file():
                    with path.open("rb") as handle:
                        os.fsync(handle.fileno())
            for path in sorted((p for p in stage.rglob("*") if p.is_dir()), reverse=True):
                fsync_dir(path)
            fsync_dir(stage)
            generations = root / "generations"
            generations.mkdir(exist_ok=True)
            destination = generations / manifest["generation_id"]
            require_capacity(settings, nonessential=True)
            os.replace(stage, destination)
            fsync_dir(generations)
            # Full deserialization smoke-check precedes label publication.
            _load_directory(destination, manifest["generation_id"])
            atomic_write(root / "labels" / (label + ".json"), canonical({"generation_id": manifest["generation_id"]}))
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def _load_directory(path: Path, generation_id: str) -> Snapshot:
    import bm25s

    if not path.is_dir() or path.is_symlink():
        raise SnapshotError("snapshot_missing_or_symlink")
    try:
        if any(p.is_symlink() for p in path.rglob("*")):
            raise SnapshotError("snapshot_contains_symlink")
        manifest = json.loads((path / "manifest.json").read_text())
        content = {k: v for k, v in manifest.items() if k != "generation_id"}
        if digest(content) != generation_id or manifest.get("generation_id") != generation_id:
            raise SnapshotError("manifest_checksum_mismatch")
        if manifest.get("schema_version") != 1:
            raise SnapshotError("unsupported_snapshot_schema")
        files = manifest["files"]
        if json.loads((path / "checksums.json").read_text()) != files:
            raise SnapshotError("checksum_manifest_mismatch")
        actual = {str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()}
        if actual != set(files) | {"manifest.json", "checksums.json"}:
            raise SnapshotError("snapshot_file_set_mismatch")
        for name, expected in files.items():
            if Path(name).is_absolute() or ".." in Path(name).parts or _file_hash(path / name) != expected:
                raise SnapshotError(f"snapshot_checksum_mismatch: {name}")
        if not {"metadata.sqlite", "tokenizer.json", "bm25/params.index.json"}.issubset(files):
            raise SnapshotError("snapshot_required_file_missing")
        for package in ("bm25s", "jieba"):
            if importlib.metadata.version(package) != manifest["dependencies"][package]:
                raise SnapshotError(f"snapshot_dependency_version_mismatch: {package}")
        with sqlite3.connect((path / "metadata.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SnapshotError("metadata_integrity_failed")
            chunks = [json.loads(row[0]) for row in db.execute("SELECT payload FROM chunks ORDER BY row_id")]
            parent_ids = set()
            for parent_id, payload in db.execute("SELECT parent_id,payload FROM parents"):
                json.loads(payload)  # Validate stored JSON without retaining unused parent bodies.
                parent_ids.add(parent_id)
            documents = [json.loads(row[0]) for row in db.execute("SELECT payload FROM documents ORDER BY doc_id")]
        if len(chunks) != manifest["chunk_count"] or len(documents) != manifest["document_count"]:
            raise SnapshotError("snapshot_count_mismatch")
        if len({c["chunk_id"] for c in chunks}) != len(chunks) or any(c["parent_id"] not in parent_ids for c in chunks):
            raise SnapshotError("invalid_chunk_parent_mapping")
        if any(d.get("full_context", {}).get("parent_id") not in parent_ids for d in documents):
            raise SnapshotError("full_document_context_missing: rebuild this snapshot")
        tokenizer = ChineseTokenizer(state=json.loads((path / "tokenizer.json").read_text()))
        if tokenizer.fingerprint != manifest["bm25"]["tokenizer_hash"]:
            raise SnapshotError("tokenizer_fingerprint_mismatch")
        sparse = bm25s.BM25.load(str(path / "bm25"), allow_pickle=False, mmap=False, load_corpus=False)
        probe = sparse.get_scores([next(iter(sparse.vocab_dict))])
        if len(probe) != len(chunks) or not np.isfinite(probe).all():
            raise SnapshotError("bm25_row_count_or_values_invalid")
        dense = None
        if manifest["embedding"]["provider"] != "none":
            import faiss

            dense = faiss.read_index(str(path / "dense.faiss"))
            if (dense.ntotal != len(chunks) or dense.d != manifest["embedding"]["dimension"]
                    or dense.metric_type != faiss.METRIC_INNER_PRODUCT):
                raise SnapshotError("dense_index_dimension_or_count_mismatch")
            row_map = [json.loads(line) for line in (path / "dense_row_map.jsonl").read_text().splitlines()]
            if row_map != [{"row": i, "chunk_id": c["chunk_id"]} for i, c in enumerate(chunks)]:
                raise SnapshotError("dense_row_mapping_mismatch")
            vectors = dense.reconstruct_n(0, dense.ntotal)
            if not np.isfinite(vectors).all() or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5):
                raise SnapshotError("dense_vectors_not_normalized")
        return Snapshot(manifest, chunks, path, documents, tokenizer, sparse, dense)
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError(f"snapshot_load_failed: {type(exc).__name__}: {exc}") from exc


def load_snapshot(settings: Mapping, generation_id: str | None = None) -> Snapshot:
    root = _root(settings)
    generation_id = _resolve(root, generation_id)
    return _load_directory(root / "generations" / generation_id, generation_id)


def recover_current(settings: Mapping) -> Snapshot:
    """Recover only the active pointer. Explicit historical reads never change generations."""
    from ..providers.embedding import configured_embedding_identity

    root = _root(settings)
    with _lock(root):
        try:
            return load_snapshot(settings)
        except (SnapshotError, OSError, ValueError) as error:
            failure = type(error).__name__ + ":" + str(error)[:200]
        candidates = []
        previous = root / "PREVIOUS"
        if previous.is_file() and not previous.is_symlink():
            try:
                candidates.append(previous.read_text().strip())
            except (OSError, ValueError):
                pass
        history = root / "PUBLISHED.json"
        if history.is_file() and not history.is_symlink():
            try:
                candidates.extend(reversed(json.loads(history.read_text())))
            except (OSError, ValueError, TypeError):
                pass
        identity = configured_embedding_identity(settings)
        for generation in dict.fromkeys(c for c in candidates if isinstance(c, str)):
            if not re.fullmatch(r"[a-f0-9]{64}", generation):
                continue
            try:
                snapshot = load_snapshot(settings, generation)
                expected = snapshot.manifest["embedding"]
                if (identity is None and expected["provider"] != "none") or (identity is not None and identity != expected):
                    continue
            except (SnapshotError, OSError, ValueError):
                continue
            snapshot.recovery = {"reason": failure, "generation_id": generation,
                                 "recovered_at": iso(datetime.now(timezone.utc))}
            atomic_write(root / "RECOVERY.json", canonical(snapshot.recovery))
            atomic_write(root / "CURRENT", (generation + "\n").encode())
            return snapshot
        raise SnapshotError("no_valid_recovery_generation: " + failure)


def collect_generations(settings: Mapping, pinned=(), *, dry_run=False) -> dict:
    """Call under the service publication lock, or the offline data-owner lock."""
    root = _root(settings)
    config = settings.get("maintenance", {})
    with _lock(root):
        protected = set(pinned)
        for name in ("CURRENT", "PREVIOUS"):
            path = root / name
            if path.is_file() and not path.is_symlink():
                value = path.read_text().strip()
                if re.fullmatch(r"[a-f0-9]{64}", value):
                    protected.add(value)
        generations = sorted((p for p in (root / "generations").glob("*")
                              if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[a-f0-9]{64}", p.name)),
                             key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        protected.update(p.name for p in generations[:max(2, config.get("keep_generations", 2))])
        deadline = time.time() - config.get("index_retention_days", 7) * 86400
        removed = []
        for path in generations:
            if path.name in protected or path.stat().st_mtime >= deadline:
                continue
            removed.append(path.name)
            if not dry_run:
                shutil.rmtree(path)
        # Labels are references for humans, not permanent retention leases. Remove dangling ones.
        if not dry_run:
            for label in (root / "labels").glob("*.json"):
                if label.is_symlink():
                    continue
                try:
                    if json.loads(label.read_text()).get("generation_id") in removed:
                        label.unlink()
                except (ValueError, AttributeError):
                    pass
            fsync_dir(root / "generations") if (root / "generations").exists() else None
        return {"dry_run": dry_run, "removed": removed, "protected": sorted(protected)}


def publish_index(settings: Mapping, generation_or_label: str, *,
                  prepared_embedding_identity: Mapping | None = None) -> dict:
    """Validate and publish; a warmed encoder identity avoids loading it again.

    Without a prepared identity, dense snapshots validate an offline encoder.
    """
    root = _root(settings)
    with _lock(root):
        snapshot = load_snapshot(settings, generation_or_label)
        expected = snapshot.manifest["embedding"]
        if prepared_embedding_identity is not None:
            identity = prepared_embedding_identity
        elif expected["provider"] != "none":
            embedding = make_embedding(settings)
            try:
                identity = embedding.identity if embedding is not None else None
            finally:
                if embedding is not None and hasattr(embedding, "close"):
                    embedding.close()
        else:
            identity = expected
        if identity != expected:
            raise SnapshotError("snapshot_query_encoder_mismatch")
        current = root / "CURRENT"
        if current.exists():
            try:
                previous = load_snapshot(settings)
            except SnapshotError:
                previous = None
            if previous is not None and previous.generation_id != snapshot.generation_id:
                atomic_write(root / "PREVIOUS", (previous.generation_id + "\n").encode())
        history_path = root / "PUBLISHED.json"
        try:
            history = json.loads(history_path.read_text())
            if not isinstance(history, list):
                history = []
        except (OSError, ValueError):
            history = []
        history = [g for g in history if isinstance(g, str) and g != snapshot.generation_id]
        atomic_write(history_path, canonical(history[-63:] + [snapshot.generation_id]))
        atomic_write(current, (snapshot.generation_id + "\n").encode())
        return snapshot.manifest
