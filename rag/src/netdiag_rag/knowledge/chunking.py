"""Token-aware parent/child chunks with source spans and repeated safety context."""
from __future__ import annotations

import re

from .common import KnowledgeError, digest
from .loader import KnowledgeDocument
from .tokenization import ConservativeTokenizer


def _paragraphs(text: str):
    """Yield exact source spans, preserving fenced code and Markdown table blocks."""
    lines = text.splitlines(keepends=True)
    start = offset = 0
    current = []
    fence = None
    for line in lines:
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence:
                fence = None
        if not line.strip() and fence is None:
            if current:
                yield "".join(current), start, offset
                current = []
            offset += len(line)
            start = offset
        else:
            if not current:
                start = offset
            current.append(line)
            offset += len(line)
    if fence:
        raise KnowledgeError("unclosed_markdown_fence")
    if current:
        yield "".join(current), start, offset


def _split(text: str, start: int, end: int, tokenizer, limit: int):
    if tokenizer.count(text) <= limit:
        return [(text, start, end)]
    if re.search(r"(?m)^\s*(`{3,}|~{3,})", text):
        raise KnowledgeError("code_block_exceeds_token_budget: split the example before indexing")
    lines = text.splitlines(keepends=True)
    if len(lines) >= 2 and "|" in lines[0] and re.search(r"\|?\s*:?-{3,}", lines[1]):
        header = "".join(lines[:2])
        result, pos = [], start + len(header)
        for row in lines[2:]:
            if tokenizer.count(header + row) > limit:
                raise KnowledgeError("table_row_exceeds_token_budget")
            result.append((header + row, pos, pos + len(row)))
            pos += len(row)
        return result
    result, consumed = [], 0
    while consumed < len(text):
        lo, hi = 1, len(text) - consumed
        while lo <= hi:
            mid = (lo + hi) // 2
            if tokenizer.count(text[consumed:consumed + mid]) <= limit:
                lo = mid + 1
            else:
                hi = mid - 1
        if hi < 1:
            raise KnowledgeError("chunk_budget_cannot_hold_one_character")
        segment = text[consumed:consumed + hi]
        if consumed + hi < len(text):
            stops = [m.end() for m in re.finditer(r"[。！？；\n]", segment)]
            if stops and stops[-1] >= hi // 2:
                hi = stops[-1]
        result.append((text[consumed:consumed + hi], start + consumed, start + consumed + hi))
        consumed += hi
    return result


def chunk_document(doc: KnowledgeDocument, tokenizer=None, *, max_input_tokens: int = 512,
                   child_tokens: int = 450, parent_tokens: int = 1000,
                   overlap_tokens: int = 50) -> tuple[list[dict], list[dict]]:
    tokenizer = tokenizer or ConservativeTokenizer()
    if min(max_input_tokens, child_tokens, parent_tokens) <= 0 or overlap_tokens < 0:
        raise KnowledgeError("invalid_chunk_budget")
    safety = "\n".join(f"{name}：{doc.sections[name]}" for name in ("适用条件", "反证", "限制"))
    headings = list(re.finditer(r"(?m)^## ([^\n]+)\n", doc.text))
    safety_spans = []
    for number, heading in enumerate(headings):
        if heading.group(1).strip() in {"适用条件", "反证", "限制"}:
            end = headings[number + 1].start() if number + 1 < len(headings) else len(doc.text)
            safety_spans.append({"start": heading.start(), "end": end,
                                 "line_start": doc.body_start_line + doc.text[:heading.start()].count("\n"),
                                 "line_end": doc.body_start_line + doc.text[:end].count("\n")})
    prefix = f"{doc.metadata['title']}\n{safety}\n\n"
    room = max_input_tokens - tokenizer.count(prefix, add_special_tokens=True) - 8
    limit = min(child_tokens, room)
    if limit < 16:
        raise KnowledgeError(f"safety_context_exceeds_chunk_budget: {doc.doc_id}")
    policy = {"version": "structural-parent-child-v1", "tokenizer": tokenizer.identity,
              "max_input_tokens": max_input_tokens, "child_tokens": child_tokens,
              "parent_tokens": parent_tokens, "overlap_tokens": overlap_tokens}
    pieces = []
    for text, start, end in _paragraphs(doc.text):
        pieces.extend(_split(text, start, end, tokenizer, limit))
    groups, group = [], []
    for piece in pieces:
        if group and tokenizer.count("\n\n".join(x[0] for x in group + [piece])) > parent_tokens:
            groups.append(group)
            group = []
        group.append(piece)
    if group:
        groups.append(group)
    parents, chunks = [], []
    for group in groups:
        parent_id = digest({"revision": doc.revision_id, "range": [group[0][1], group[-1][2]],
                            "policy": policy})
        parent_body = "\n\n".join(x[0].strip() for x in group)
        parents.append({"parent_id": parent_id, "doc_id": doc.doc_id,
                        "revision_id": doc.revision_id, "text": prefix + parent_body})
        i = 0
        while i < len(group):
            j = i + 1
            while j < len(group):
                proposed = prefix + "\n\n".join(x[0].strip() for x in group[i:j + 1])
                if (tokenizer.count(proposed, add_special_tokens=True) > max_input_tokens
                        or tokenizer.count("\n\n".join(x[0] for x in group[i:j + 1])) > child_tokens):
                    break
                j += 1
            selected = group[i:j]
            text = prefix + "\n\n".join(x[0].strip() for x in selected)
            count = tokenizer.count(text, add_special_tokens=True)
            if count > max_input_tokens:
                raise KnowledgeError(f"chunk_exceeds_embedding_budget: {doc.doc_id}")
            spans = [{"start": a, "end": b,
                      "line_start": doc.body_start_line + doc.text[:a].count("\n"),
                      "line_end": doc.body_start_line + doc.text[:b].count("\n")}
                     for _, a, b in selected]
            heading_path = [m.group(1) for m in headings if m.start() <= selected[0][1]][-1:]
            chunk_id = digest({"parent": parent_id, "spans": spans, "text": text})
            chunks.append({"chunk_id": chunk_id, "doc_id": doc.doc_id,
                           "revision_id": doc.revision_id, "parent_id": parent_id,
                           "text": text, "body_text": "\n\n".join(x[0].strip() for x in selected),
                           "source_uri": doc.metadata["source_uri"], "title": doc.metadata["title"],
                           "metadata": {**doc.metadata, "source_path": doc.source_path,
                                        "source_spans": spans, "safety_source_spans": safety_spans,
                                        "heading_path": heading_path,
                                        "safety_context": safety, "token_count": count,
                                        "tokenizer": tokenizer.identity}})
            next_i = j
            while next_i > i + 1 and tokenizer.count(
                    "\n\n".join(x[0] for x in group[next_i - 1:j])) <= overlap_tokens:
                next_i -= 1
            i = j if j == len(group) else next_i
    return chunks, parents
