"""Small, dependency-free Okapi BM25 implementation.

Lexical retrieval is a deliberate first choice (ADR-004): it is deterministic, explainable, cheap, and
strong on this corpus of short, jargon-heavy runbooks (error codes, EDI message numbers). Embeddings can
be added behind the same interface later if evaluation shows recall problems.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "my",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "that",
        "the",
        "their",
        "there",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "do",
        "does",
        "can",
    ]
)


_SUFFIXES = ("ations", "ation", "ing", "ed", "es", "s", "al", "e")


def stem(token: str) -> str:
    """Very light suffix stripping so "approves"/"approved"/"approval" meet at "approv"."""
    if token.isdigit():
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for tok in _TOKEN.findall(text.lower()):
        if tok in STOPWORDS:
            continue
        tokens.append(stem(tok))
        # Also index the parts of compound tokens ("wave_alloc_timeout", "hh-rno1-001").
        if "-" in tok or "_" in tok:
            tokens.extend(stem(p) for p in re.split(r"[-_]", tok) if p and p not in STOPWORDS)
    return tokens


class BM25Index:
    def __init__(self, documents: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs = [Counter(tokenize(d)) for d in documents]
        self._lengths = [sum(c.values()) for c in self._docs]
        self._avg = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        df: Counter[str] = Counter()
        for counts in self._docs:
            df.update(counts.keys())
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def __len__(self) -> int:
        return len(self._docs)

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out: list[float] = []
        for counts, length in zip(self._docs, self._lengths, strict=True):
            s = 0.0
            norm = self.k1 * (1 - self.b + self.b * (length / self._avg if self._avg else 0.0))
            for t in terms:
                f = counts.get(t)
                if f:
                    s += self._idf[t] * (f * (self.k1 + 1)) / (f + norm)
            out.append(s)
        return out

    def top_k(self, query: str, k: int) -> list[tuple[int, float]]:
        ranked = sorted(enumerate(self.scores(query)), key=lambda p: (-p[1], p[0]))
        return [(i, s) for i, s in ranked[:k] if s > 0]
