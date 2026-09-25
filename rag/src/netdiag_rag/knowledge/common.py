"""Small, dependency-light primitives shared by the knowledge subsystem."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..util import canonical as _canonical, digest as digest, iso as _iso, timestamp as _timestamp


class KnowledgeError(ValueError):
    """Invalid knowledge or an unsafe/incompatible knowledge operation."""


def canonical(value: Any) -> bytes:
    return _canonical(value).encode("utf-8")


def timestamp(value: str | datetime) -> datetime:
    if isinstance(value, str):
        try:
            return _timestamp(value)
        except ValueError as exc:
            if str(exc) != "timezone_required":
                raise KnowledgeError(f"invalid_timestamp: {value}") from exc
    elif isinstance(value, datetime) and value.tzinfo is not None:
        return _timestamp(value)
    raise KnowledgeError("timestamp_requires_timezone")


def iso(value: str | datetime) -> str:
    # Knowledge hashes omit zero microseconds; runtime timestamps retain them.
    return _iso(timestamp(value), timespec="auto")


def strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise KnowledgeError(f"{field}_requires_string_list")
    return sorted(set(value))


def citation_is_revoked(citation, revoked):
    for part in [citation, *(citation.get("citations") or [])]:
        for field in ("doc_id", "revision_id", "chunk_id", "parent_id"):
            key = part.get(field)
            if key in revoked or (key and field + ":" + key in revoked):
                return True
    for key in (citation.get("chunk_ids") or []) + citation.get("metadata", {}).get("constituent_chunk_ids", []):
        if key in revoked or "chunk_id:" + key in revoked:
            return True
    return False
