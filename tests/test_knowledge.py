from __future__ import annotations

import pytest

from ops_agent.knowledge.bm25 import BM25Index, tokenize
from ops_agent.knowledge.store import DocStore, parse_markdown


def test_tokenize_splits_compounds_and_stems() -> None:
    toks = tokenize("WAVE_ALLOC_TIMEOUT approves the HH-RNO1-001")
    assert "wave" in toks and "alloc" in toks and "approv" in toks and "rno1" in toks
    assert "the" not in toks


def test_bm25_ranks_relevant_first() -> None:
    idx = BM25Index(["carrier tender error", "password reset sms", "wave release stuck allocating"])
    assert idx.top_k("waves stuck", 1)[0][0] == 2
    assert idx.top_k("zzz", 3) == []


@pytest.mark.parametrize(
    ("query", "expected_doc"),
    [
        ("pick waves stuck allocating WAVE_ALLOC_TIMEOUT", "KB-010"),
        ("no EDI 214 status messages from carrier", "KB-012"),
        ("who approves changes made by automation", "KB-004"),
        ("RF app crashes on several handhelds after update", "KB-002"),
        ("how do I reset my password sms code", "KB-015"),
        ("same incident reported twice duplicate", "KB-008"),
        ("customer disputes detention charges on invoice", "KB-026"),
        ("what does SSCC mean", "KB-028"),
    ],
)
def test_search_top1(docs: DocStore, query: str, expected_doc: str) -> None:
    hits = docs.search(query, limit=3)
    assert hits, query
    assert hits[0].doc_id == expected_doc, [h.citation for h in hits]
    assert hits[0].citation.startswith(f"{expected_doc}#")


def test_unverified_docs_can_be_excluded(docs: DocStore) -> None:
    q = "scanner tips restart twice sync errors"
    assert any(h.doc_id == "KB-030" for h in docs.search(q, limit=5))
    assert all(h.doc_id != "KB-030" for h in docs.search(q, limit=5, include_unverified=False))


def test_get_doc_sections(docs: DocStore) -> None:
    doc = docs.get("KB-001")
    assert doc is not None and doc.trust == "verified"
    assert [s.anchor for s in doc.sections][:3] == ["purpose", "p1-critical", "p2-high"]
    assert docs.get("KB-999") is None
    assert len(docs) == 30


def test_parse_requires_front_matter() -> None:
    with pytest.raises(ValueError):
        parse_markdown("# no front matter")
