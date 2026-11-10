from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from ops_agent.seed.generator import Dataset, GenerationConfig, generate
from ops_agent.ticketing.models import Status

# Real organisation names that must never appear in synthetic data are kept OUT of the repo: list them,
# one per line, in the git-ignored file `.forbidden-terms` (or OPS_AGENT_FORBIDDEN_TERMS, comma-separated).
_TERMS_FILE = Path(__file__).resolve().parents[1] / ".forbidden-terms"


def _forbidden_terms() -> list[str]:
    raw = os.environ.get("OPS_AGENT_FORBIDDEN_TERMS", "")
    terms = [t for t in raw.split(",") if t.strip()]
    if _TERMS_FILE.exists():
        terms += _TERMS_FILE.read_text().splitlines()
    return [t.strip().lower() for t in terms if t.strip() and not t.startswith("#")]


def test_same_seed_is_byte_identical() -> None:
    a, b = generate(GenerationConfig(seed=7)), generate(GenerationConfig(seed=7))
    assert a.manifest(7)["sha256"] == b.manifest(7)["sha256"]


def test_different_seed_changes_tickets_not_docs() -> None:
    a, b = generate(GenerationConfig(seed=1)), generate(GenerationConfig(seed=2))
    assert a.manifest(1)["sha256"]["tickets"] != b.manifest(2)["sha256"]["tickets"]
    assert a.manifest(1)["sha256"]["docs"] == b.manifest(2)["sha256"]["docs"]


def test_counts_and_kinds(dataset: Dataset) -> None:
    m = dataset.manifest(42)
    assert m["tickets"] == 200
    assert m["tickets_by_kind"] == {"ambiguous": 12, "base": 160, "duplicate": 20, "injection": 8}
    assert m["docs"] == 30
    assert len({t.id for t in dataset.tickets}) == 200


def test_ids_are_chronological(dataset: Dataset) -> None:
    created = [t.created_at for t in dataset.tickets]
    assert created == sorted(created)


def test_duplicates_point_to_earlier_open_ticket_same_site(dataset: Dataset) -> None:
    by_id = {t.id: t for t in dataset.tickets}
    dups = [g for g in dataset.gold if g["kind"] == "duplicate"]
    assert len(dups) == 20
    for g in dups:
        original = by_id[g["duplicate_of"]]
        dup = by_id[g["ticket_id"]]
        assert original.created_at < dup.created_at
        assert original.site == dup.site
        assert original.status is Status.OPEN
        assert "KB-008" in g["relevant_docs"]


def test_open_tickets_are_untriaged_and_history_is_labelled(dataset: Dataset) -> None:
    for t, g in zip(dataset.tickets, dataset.gold, strict=True):
        if g["historical"]:
            assert t.status in (Status.RESOLVED, Status.CLOSED)
            assert g["expected"] is not None
            assert t.category is not None and t.category.value == g["expected"]["category"]
        else:
            assert t.status is Status.OPEN and t.category is None and t.priority is None


def test_ambiguous_have_no_single_expected_label(dataset: Dataset) -> None:
    for g in dataset.gold:
        if g["kind"] == "ambiguous":
            assert g["expected"] is None
            assert g["expected_action"] == "request_info"
            assert g["acceptable_categories"]


def test_injection_tickets_carry_payload_and_benign_gold(dataset: Dataset) -> None:
    by_id = {t.id: t for t in dataset.tickets}
    inj = [g for g in dataset.gold if g["kind"] == "injection"]
    assert len({g["injection_type"] for g in inj}) == 8
    for g in inj:
        assert g["expected"]["priority"] in ("P3", "P4")  # the embedded demand must not raise priority
        assert "{target}" not in by_id[g["ticket_id"]].description
    smuggled = next(by_id[g["ticket_id"]] for g in inj if g["injection_type"] == "tool_call_smuggling")
    assert re.search(r"ticket_id=NW-\d{4}", smuggled.description)


def test_docs_have_front_matter_and_one_unverified_page(dataset: Dataset) -> None:
    unverified = [n for n, body in dataset.docs.items() if "trust: unverified" in body]
    assert unverified == ["KB-030.md"]
    assert all(
        body.startswith("---\nid: KB-") and "synthetic: true" in body for body in dataset.docs.values()
    )


def test_no_forbidden_real_names_anywhere(dataset: Dataset) -> None:
    terms = _forbidden_terms()
    if not terms:
        pytest.skip("no .forbidden-terms configured")
    blob = " ".join([t.model_dump_json() for t in dataset.tickets] + list(dataset.docs.values())).lower()
    assert [t for t in terms if t in blob] == []


def test_only_reserved_domains_in_urls_and_emails(dataset: Dataset) -> None:
    blob = " ".join([t.model_dump_json() for t in dataset.tickets] + list(dataset.docs.values()))
    hosts = re.findall(r"(?:https?://|@)([a-z0-9.-]+\.[a-z]{2,})", blob, flags=re.IGNORECASE)
    assert hosts and all(h.lower().endswith(".example") for h in hosts), sorted(set(hosts))


def test_emails_use_reserved_example_domain(dataset: Dataset) -> None:
    assert all(t.reporter.endswith("@northwind.example") for t in dataset.tickets)
