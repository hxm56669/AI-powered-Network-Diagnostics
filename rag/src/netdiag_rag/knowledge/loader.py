"""Strict Markdown/front-matter ingestion; no source or model network calls."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .common import KnowledgeError, digest, iso, strings, timestamp

REQUIRED_SECTIONS = ("适用条件", "现象", "候选原因", "支持证据", "反证", "检查步骤", "限制")
REVIEW_STATUSES = {"approved", "code-reviewed", "seed", "draft", "revoked"}


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise KnowledgeError(f"duplicate_or_invalid_metadata_key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class KnowledgeDocument:
    doc_id: str
    revision_id: str
    text: str
    metadata: dict
    source_path: str
    body_start_line: int
    sections: dict[str, str]


def load_document(path: Path, root: Path | None = None) -> KnowledgeDocument:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise KnowledgeError(f"invalid_knowledge_file: {path}")
    try:
        raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise KnowledgeError(f"knowledge_not_utf8: {path}") from exc
    lines = raw.splitlines()
    if not lines or lines[0] != "---":
        raise KnowledgeError(f"missing_front_matter: {path}")
    try:
        boundary = lines.index("---", 1)
        meta = yaml.load("\n".join(lines[1:boundary]), Loader=_UniqueLoader)
    except KnowledgeError:
        raise
    except (ValueError, yaml.YAMLError) as exc:
        raise KnowledgeError(f"invalid_front_matter: {path}") from exc
    if not isinstance(meta, dict):
        raise KnowledgeError("front_matter_requires_mapping")
    for key in ("doc_id", "title", "kind", "language", "source_uri", "source_type", "owner",
                "review_status", "license"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise KnowledgeError(f"missing_metadata: {key}: {path}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", meta["doc_id"]):
        raise KnowledgeError("invalid_doc_id")
    if meta["kind"] not in {"runbook", "protocol", "incident", "environment"}:
        raise KnowledgeError("invalid_knowledge_kind")
    if meta["review_status"] not in REVIEW_STATUSES:
        raise KnowledgeError("invalid_review_status")
    if meta["source_type"] == "seed" and meta["review_status"] == "approved":
        raise KnowledgeError("seed_must_not_claim_human_approval")
    for key in ("available_at", "valid_from"):
        if key not in meta:
            raise KnowledgeError(f"missing_metadata: {key}")
        meta[key] = iso(meta[key])
    if meta.get("valid_until") is not None:
        meta["valid_until"] = iso(meta["valid_until"])
        if timestamp(meta["valid_until"]) <= timestamp(meta["valid_from"]):
            raise KnowledgeError("invalid_validity_interval")
    else:
        meta["valid_until"] = None
    if meta["review_status"] == "approved" and not meta.get("reviewed_at"):
        raise KnowledgeError("approved_knowledge_requires_reviewed_at")
    if meta.get("reviewed_at"):
        meta["reviewed_at"] = iso(meta["reviewed_at"])
    if urlparse(meta["source_uri"]).scheme not in {"http", "https", "internal"}:
        raise KnowledgeError("invalid_source_uri")
    references = meta.get("references", [])
    if not isinstance(references, list) or not all(
        isinstance(x, str) and urlparse(x).scheme in {"http", "https"} and urlparse(x).netloc
        for x in references
    ):
        raise KnowledgeError("invalid_reference_urls")
    if meta["source_type"] == "seed" and not references:
        raise KnowledgeError("seed_requires_real_reference_urls")
    applies = meta.get("applies_to")
    if not isinstance(applies, dict):
        raise KnowledgeError("missing_applies_to")
    for key, value in applies.items():
        applies[key] = strings(value, f"applies_to.{key}")
    for key in ("symptoms", "cause_ids", "check_ids"):
        meta[key] = strings(meta.get(key, []), key)
    checks = meta.get("checks", [])
    if not isinstance(checks, list):
        raise KnowledgeError("checks_requires_list")
    seen_checks = set()
    for check in checks:
        if not isinstance(check, dict) or not all(isinstance(check.get(k), str) and check[k].strip()
                                                for k in ("check_id", "purpose")):
            raise KnowledgeError("invalid_check_definition")
        if check["check_id"] in seen_checks:
            raise KnowledgeError("duplicate_check_id")
        seen_checks.add(check["check_id"])
        if meta["source_type"] == "seed" and check.get("review_status") == "approved":
            raise KnowledgeError("seed_check_must_not_claim_human_approval")
    meta["checks"] = checks
    meta["check_ids"] = sorted(set(meta["check_ids"]) | seen_checks)
    scope = meta.get("scope", {"organizations": ["public"]})
    if isinstance(scope, str):
        scope = {"organizations": [scope]}
    if not isinstance(scope, dict) or set(scope) - {"organizations"}:
        raise KnowledgeError("invalid_knowledge_scope")
    meta["scope"] = {"organizations": strings(scope.get("organizations", []), "scope.organizations")}
    if not meta["scope"]["organizations"]:
        raise KnowledgeError("empty_knowledge_scope")
    from .schema import KnowledgeMetadata
    try:
        KnowledgeMetadata.model_validate(meta)
    except ValueError as exc:
        raise KnowledgeError("knowledge_schema_invalid: " + str(exc)) from exc
    # Keep line count stable; source locations point at original Markdown lines.
    body = "\n".join(line.rstrip() for line in lines[boundary + 1:]) + "\n"
    matches = list(re.finditer(r"(?m)^## ([^\n]+)\n", body))
    sections = {}
    for n, match in enumerate(matches):
        heading = match.group(1).strip()
        if heading in sections:
            raise KnowledgeError(f"duplicate_section: {heading}")
        end = matches[n + 1].start() if n + 1 < len(matches) else len(body)
        sections[heading] = body[match.end():end].strip()
    for heading in REQUIRED_SECTIONS:
        if not sections.get(heading):
            raise KnowledgeError(f"missing_or_empty_section: {heading}: {path}")
    try:
        revision = digest({"body": body, "metadata": meta})
    except (TypeError, ValueError) as exc:
        raise KnowledgeError("metadata_must_be_json_serializable") from exc
    source_path = str(path.relative_to(root)) if root is not None else path.name
    return KnowledgeDocument(meta["doc_id"], revision, body, meta, source_path,
                             boundary + 2, sections)


def load_knowledge(root: Path) -> list[KnowledgeDocument]:
    root = Path(root)
    if not root.is_dir():
        raise KnowledgeError(f"knowledge_directory_missing: {root}")
    documents = [load_document(p, root) for p in sorted(root.rglob("*.md"))
                 if p.name != "README.md"]
    seen = set()
    for doc in documents:
        key = (doc.doc_id, doc.revision_id)
        if key in seen:
            raise KnowledgeError(f"duplicate_document_revision: {doc.doc_id}")
        seen.add(key)
    if not documents:
        raise KnowledgeError("empty_knowledge_directory")
    return documents
