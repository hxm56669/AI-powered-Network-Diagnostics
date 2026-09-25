"""Disposable persistent encoder cache, independent of immutable retrieval generations."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from collections import Counter
from pathlib import Path

import numpy as np

from ..knowledge.common import digest
from ..providers.embedding import normalized


def encode_cached(embedding, chunks, path: Path, max_entries=100000):
    texts, ids = [c["text"] for c in chunks], [c["doc_id"] for c in chunks]
    stats = {"hits": 0, "misses": 0, "invalidated": 0, "enabled": max_entries > 0}
    if not max_entries:
        stats["misses"] = len(chunks)
        return normalized(embedding.encode(texts, ids), embedding.dimension), stats
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS vectors(key TEXT PRIMARY KEY, value BLOB NOT NULL, "
                   "checksum TEXT NOT NULL, used_at REAL NOT NULL)")
        keys = [digest({"encoder": embedding.identity, "preprocessing": "encoder-input-v1", "text": text,
                        "fixture_key": ids[i] if embedding.identity["provider"] == "fixture" else None})
                for i, text in enumerate(texts)]
        counts = Counter(keys)  # Query each key once; receipts still count input chunks.
        values, missing = {}, {}
        for i, key in enumerate(keys):
            if key in values or key in missing:
                continue
            row = db.execute("SELECT value,checksum FROM vectors WHERE key=?", (key,)).fetchone()
            if row:
                data, checksum = row
                vector = np.frombuffer(data, dtype="<f4") if len(data) == embedding.dimension * 4 else None
                valid = (vector is not None and np.isfinite(vector).all()
                         and abs(float(np.linalg.norm(vector)) - 1) < 1e-4
                         and hashlib.sha256(data).hexdigest() == checksum)
                if valid:
                    values[key] = vector
                    stats["hits"] += counts[key]
                    continue
                stats["invalidated"] += counts[key]
            missing[key] = i
            stats["misses"] += counts[key]
        if missing:
            positions = list(missing.values())
            encoded = normalized(embedding.encode([texts[i] for i in positions], [ids[i] for i in positions]),
                                 embedding.dimension)
            if len(encoded) != len(positions):
                raise ValueError("embedding_count_mismatch")
            for key, vector in zip(missing, encoded):
                values[key] = vector
                data = vector.astype("<f4").tobytes()
                db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)",
                           (key, data, hashlib.sha256(data).hexdigest(), time.time()))
        db.executemany("UPDATE vectors SET used_at=? WHERE key=?", [(time.time(), k) for k in values])
        db.execute("DELETE FROM vectors WHERE key IN (SELECT key FROM vectors ORDER BY used_at DESC,key LIMIT -1 OFFSET ?)",
                   (max_entries,))
        db.commit()
        return np.ascontiguousarray([values[k] for k in keys], dtype=np.float32), stats
    finally:
        db.close()
