"""Frozen sparse tokenization and explicitly conservative token accounting."""
from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import re
import unicodedata
from pathlib import Path

from .common import KnowledgeError, digest

SYNONYMS = {"rtt": ["往返时延"], "往返时延": ["rtt"],
            "rssi": ["无线信号"], "无线信号": ["rssi"]}
NORMALIZATION_VERSION = "nfkc-casefold-technical-jieba-nohmm-v2"
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}"
                      r"(?:%[A-Za-z0-9_.-]+)?(?![A-Za-z0-9])|"
                      r"--?[A-Za-z][\w.-]*(?:=[^\s,;，；]+)?|"
                      r"[A-Za-z0-9_]+(?:[.:/=%-][A-Za-z0-9_]+)+|"
                      r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[\u3400-\u9fff]+")


class ConservativeTokenizer:
    """UTF-8 bytes are a conservative budget, NOT a semantic embedding."""
    identity = "utf8-byte-upper-bound-v1"

    def count(self, text: str, add_special_tokens: bool = False) -> int:
        return len(text.encode("utf-8")) + (2 if add_special_tokens else 0)


class ModelTokenizer:
    def __init__(self, tokenizer, identity: str):
        self.tokenizer = tokenizer
        self.identity = identity

    def count(self, text: str, add_special_tokens: bool = False) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=add_special_tokens,
                                         truncation=False))


class ChineseTokenizer:
    def __init__(self, state: dict | None = None, terms: list[str] | None = None):
        import jieba

        version = importlib.metadata.version("jieba")
        dictionary = Path(jieba.__file__).parent / "dict.txt"
        dictionary_hash = hashlib.sha256(dictionary.read_bytes()).hexdigest()
        if state is not None:
            if (state.get("version") != version or state.get("dictionary_sha256") != dictionary_hash
                    or state.get("normalization") != NORMALIZATION_VERSION):
                raise KnowledgeError("sparse_tokenizer_version_mismatch")
            self.state = state
        else:
            self.state = {"engine": "jieba", "version": version,
                          "dictionary_sha256": dictionary_hash,
                          "normalization": NORMALIZATION_VERSION,
                          "terms": sorted(set(terms or [])), "stopwords": [],
                          "synonyms": SYNONYMS}
        self._engine = jieba.Tokenizer(str(dictionary))
        jieba.setLogLevel(logging.WARNING)
        self._engine.initialize()
        for term in self.state["terms"]:
            self._engine.add_word(term, freq=10_000_000)
        self.stopwords = frozenset(self.state["stopwords"])
        self.fingerprint = digest(self.state)

    def __call__(self, text: str) -> list[str]:
        result = []
        for piece in TOKEN_RE.findall(unicodedata.normalize("NFKC", text).casefold()):
            tokens = list(self._engine.cut(piece, HMM=False)) if re.fullmatch(
                r"[\u3400-\u9fff]+", piece) else [piece]
            for token in tokens:
                if token.strip() and token not in self.stopwords:
                    result.append(token)
                    result.extend(self.state["synonyms"].get(token, []))
        return result


def read_domain_terms(root: Path) -> list[str]:
    path = root / "domain_terms.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
