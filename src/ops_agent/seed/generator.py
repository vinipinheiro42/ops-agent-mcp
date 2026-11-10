"""Deterministic generator for the synthetic dataset.

Same seed -> byte-identical output. Produces:
* ``seed/tickets.jsonl``  tickets as loaded into the fake service desk (no labels for open tickets);
* ``seed/gold.jsonl``     ground truth for future evaluation (M4) - never exposed to the agent;
* ``docs/KB-*.md``        the internal knowledge base;
* ``seed/manifest.json``  counts and content hashes.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ops_agent.seed.catalog import (
    AMBIGUOUS,
    CARRIERS,
    CUSTOMERS,
    EMAIL_DOMAIN,
    FIRST_NAMES,
    INJECTIONS,
    LAST_NAMES,
    SITES,
    TEMPLATES,
    Template,
)
from ops_agent.seed.corpus import ALL_DOCS
from ops_agent.ticketing.models import Category, Priority, Status, Team, Ticket

Kind = Literal["base", "duplicate", "ambiguous", "injection"]

DEFAULT_SEED = 42
WINDOW_START = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
WINDOW_DAYS = 30

RUNBOOK_BY_CATEGORY: dict[Category, str] = {
    Category.WMS: "KB-010",
    Category.TMS_ROUTING: "KB-011",
    Category.EDI_INTEGRATION: "KB-012",
    Category.DEVICES: "KB-013",
    Category.IDENTITY_ACCESS: "KB-015",
    Category.NETWORK: "KB-016",
    Category.CUSTOMER_PORTAL: "KB-017",
    Category.WORKPLACE: "KB-027",
    Category.BILLING: "KB-018",
    Category.SECURITY: "KB-019",
}


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    seed: int = DEFAULT_SEED
    base: int = 160
    historical: int = 40  # oldest base tickets, already triaged and resolved
    duplicates: int = 20
    ambiguous: int = 12
    injections: int = 8


@dataclass(slots=True)
class _Draft:
    key: str
    kind: Kind
    created: datetime
    title: str
    description: str
    reporter: str
    site: str
    template: Template | None = None
    acceptable: tuple[Category, ...] = ()
    duplicate_of_key: str | None = None
    injection_type: str | None = None
    injection_target_key: str | None = None
    historical: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Dataset:
    tickets: list[Ticket]
    gold: list[dict[str, Any]]
    docs: dict[str, str]  # filename -> markdown

    def manifest(self, seed: int) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for g in self.gold:
            kinds[g["kind"]] = kinds.get(g["kind"], 0) + 1
        return {
            "seed": seed,
            "synthetic": True,
            "client": "Northwind Logistics (fictional)",
            "tickets": len(self.tickets),
            "tickets_by_kind": dict(sorted(kinds.items())),
            "docs": len(self.docs),
            "sha256": {
                "tickets": _sha(_jsonl([t.model_dump(mode="json") for t in self.tickets])),
                "gold": _sha(_jsonl(self.gold)),
                "docs": _sha("".join(f"{k}\n{v}" for k, v in sorted(self.docs.items()))),
            },
        }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


class _Slots:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def person(self) -> tuple[str, str]:
        first = self.rng.choice(FIRST_NAMES)
        last = self.rng.choice(LAST_NAMES)
        return first, f"{first.lower()}.{last.lower()}@{EMAIL_DOMAIN}"

    def fill(self, site_code: str, site_name: str) -> dict[str, str]:
        r = self.rng
        return {
            "site": site_name,
            "site_code": site_code,
            "carrier": r.choice(CARRIERS),
            "customer": r.choice(CUSTOMERS),
            "device": f"HH-{site_code}-{r.randint(1, 240):03d}",
            "printer": f"PRN-{site_code}-{r.randint(1, 40):02d}",
            "dock": f"Dock {r.randint(1, 24)}",
            "aisle": str(r.randint(1, 60)),
            "sku": f"SKU-{r.randint(10000, 99999)}",
            "loc": f"{r.choice('ABCDEF')}-{r.randint(1, 40):02d}-{r.randint(1, 6)}",
            "n": str(r.randint(3, 60)),
            "time": f"{r.randint(5, 20):02d}:{r.choice((0, 10, 20, 30, 40, 50)):02d}",
            "first": r.choice(FIRST_NAMES),
            "invoice": f"INV-2026-{r.randint(10000, 99999)}",
        }


def generate(config: GenerationConfig | None = None) -> Dataset:
    cfg = config or GenerationConfig()
    rng = random.Random(cfg.seed)
    slots = _Slots(rng)
    window = timedelta(days=WINDOW_DAYS)

    def rand_time() -> datetime:
        return WINDOW_START + timedelta(minutes=rng.randrange(int(window.total_seconds() // 60)))

    drafts: list[_Draft] = []
    base_slots: dict[str, tuple[dict[str, str], int]] = {}

    # 1. Base tickets.
    base_times = sorted(rand_time() for _ in range(cfg.base))
    for i, created in enumerate(base_times):
        tpl = rng.choice(TEMPLATES)
        code, name = rng.choice(SITES)
        values = slots.fill(code, name)
        variant = rng.randrange(len(tpl.titles))
        _, email = slots.person()
        key = f"b{i:03d}"
        drafts.append(
            _Draft(
                key=key,
                kind="base",
                created=created,
                title=tpl.titles[variant].format_map(values),
                description=tpl.bodies[variant % len(tpl.bodies)].format_map(values),
                reporter=email,
                site=code,
                template=tpl,
                historical=i < cfg.historical,
            )
        )
        base_slots[key] = (values, variant)

    open_base = [d for d in drafts if d.kind == "base" and not d.historical]
    incident_base = [d for d in open_base if d.template and d.template.priority in (Priority.P1, Priority.P2)]

    # 2. Duplicates: same incident (same slots), alternate wording, other reporter, minutes to hours later.
    prefixes = ("", "Also affected: ", "Same here - ", "URGENT ", "Still broken: ")
    for j in range(cfg.duplicates):
        orig = rng.choice(incident_base)
        assert orig.template is not None
        values, variant = base_slots[orig.key]
        alt = (variant + 1) % len(orig.template.titles)
        _, email = slots.person()
        drafts.append(
            _Draft(
                key=f"d{j:03d}",
                kind="duplicate",
                created=orig.created + timedelta(minutes=rng.randint(10, 360)),
                title=rng.choice(prefixes) + orig.template.titles[alt].format_map(values),
                description=orig.template.bodies[alt % len(orig.template.bodies)].format_map(values),
                reporter=email,
                site=orig.site,
                template=orig.template,
                duplicate_of_key=orig.key,
            )
        )

    # 3. Ambiguous tickets.
    for j in range(cfg.ambiguous):
        title, body, acceptable = AMBIGUOUS[j % len(AMBIGUOUS)]
        code, name = rng.choice(SITES)
        values = slots.fill(code, name)
        _, email = slots.person()
        drafts.append(
            _Draft(
                key=f"a{j:03d}",
                kind="ambiguous",
                created=rand_time(),
                title=title.format_map(values),
                description=body.format_map(values),
                reporter=email,
                site=code,
                acceptable=acceptable,
            )
        )

    # 4. Prompt-injection tickets: a legitimate base problem with an embedded instruction.
    low_impact = [t for t in TEMPLATES if t.priority in (Priority.P3, Priority.P4)]
    for j in range(cfg.injections):
        inj_type, payload = INJECTIONS[j % len(INJECTIONS)]
        tpl = rng.choice(low_impact)
        code, name = rng.choice(SITES)
        values = slots.fill(code, name)
        variant = rng.randrange(len(tpl.titles))
        _, email = slots.person()
        target = rng.choice(open_base)
        drafts.append(
            _Draft(
                key=f"i{j:03d}",
                kind="injection",
                created=rand_time(),
                title=tpl.titles[variant].format_map(values),
                description=tpl.bodies[variant % len(tpl.bodies)].format_map(values) + "\n\n" + payload,
                reporter=email,
                site=code,
                template=tpl,
                injection_type=inj_type,
                injection_target_key=target.key,
            )
        )

    # Assign ids in chronological order (stable tie-break on key).
    drafts.sort(key=lambda d: (d.created, d.key))
    id_of = {d.key: f"NW-{1001 + n}" for n, d in enumerate(drafts)}

    tickets: list[Ticket] = []
    gold: list[dict[str, Any]] = []
    for d in drafts:
        tid = id_of[d.key]
        description = d.description.replace(
            "{target}", id_of[d.injection_target_key] if d.injection_target_key else ""
        )
        src = d.template
        created_iso = _iso(d.created)
        if d.historical and src is not None:
            resolved = d.created + timedelta(hours=rng.randint(2, 72))
            ticket = Ticket(
                id=tid,
                title=d.title,
                description=description,
                reporter=d.reporter,
                site=d.site,
                status=Status.CLOSED if rng.random() < 0.5 else Status.RESOLVED,
                priority=src.priority,
                category=src.category,
                team=src.team,
                created_at=created_iso,
                updated_at=_iso(resolved),
                version=4,
            )
        else:
            ticket = Ticket(
                id=tid,
                title=d.title,
                description=description,
                reporter=d.reporter,
                site=d.site,
                status=Status.OPEN,
                created_at=created_iso,
                updated_at=created_iso,
                version=1,
            )
        tickets.append(ticket)
        gold.append(_gold_row(tid, d, id_of))

    docs = {f"{doc.id}.md": doc.render() for doc in ALL_DOCS}
    return Dataset(tickets=tickets, gold=gold, docs=docs)


def _gold_row(tid: str, d: _Draft, id_of: dict[str, str]) -> dict[str, Any]:
    tpl = d.template
    expected: dict[str, str] | None = None
    relevant: list[str] = []
    if tpl is not None:
        expected = {"category": tpl.category.value, "team": tpl.team.value, "priority": tpl.priority.value}
        relevant = sorted({"KB-001", "KB-002", RUNBOOK_BY_CATEGORY[tpl.category]})
    if d.kind == "duplicate":
        relevant = sorted({*relevant, "KB-008"})
    action = {
        "base": "triage",
        "duplicate": "link_duplicate",
        "ambiguous": "request_info",
        "injection": "triage",
    }[d.kind]
    if d.historical:
        action = "none_already_resolved"
    return {
        "ticket_id": tid,
        "kind": d.kind,
        "template": tpl.key if tpl else None,
        "expected": expected,
        "acceptable_categories": [c.value for c in d.acceptable] if d.acceptable else None,
        "expected_action": action,
        "duplicate_of": id_of[d.duplicate_of_key] if d.duplicate_of_key else None,
        "injection_type": d.injection_type,
        "relevant_docs": relevant,
        "historical": d.historical,
    }


def write_dataset(dataset: Dataset, data_dir: Path, seed: int) -> dict[str, Any]:
    seed_dir = data_dir / "seed"
    docs_dir = data_dir / "docs"
    seed_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    for old in docs_dir.glob("*.md"):
        old.unlink()
    (seed_dir / "tickets.jsonl").write_text(_jsonl([t.model_dump(mode="json") for t in dataset.tickets]))
    (seed_dir / "gold.jsonl").write_text(_jsonl(dataset.gold))
    for name, content in dataset.docs.items():
        (docs_dir / name).write_text(content)
    manifest = dataset.manifest(seed)
    (seed_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load_tickets(seed_dir: Path) -> list[Ticket]:
    lines = (seed_dir / "tickets.jsonl").read_text().splitlines()
    return [Ticket.model_validate_json(line) for line in lines if line.strip()]


__all__ = ["DEFAULT_SEED", "Dataset", "GenerationConfig", "Team", "generate", "load_tickets", "write_dataset"]
