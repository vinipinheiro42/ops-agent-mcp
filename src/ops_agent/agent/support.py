"""Checks whether an answer is supported by the sections it cites (used by the guard and the evaluator)."""

from __future__ import annotations

import re

from ops_agent.knowledge.bm25 import tokenize

_PREFIX = re.compile(r"^According to .*?:\s*", re.IGNORECASE)


def unsupported_sentences(answer: str, cited_texts: list[str], min_overlap: float = 0.6) -> list[str]:
    """Sentences whose content tokens are mostly absent from the cited sections."""
    source = set(tokenize(" ".join(cited_texts)))
    body = _PREFIX.sub("", answer)
    bad: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", body):
        toks = [t for t in tokenize(sentence) if not t.startswith("kb")]
        if len(toks) < 2:
            continue
        overlap = sum(1 for t in toks if t in source) / len(toks)
        if overlap < min_overlap:
            bad.append(sentence)
    return bad
