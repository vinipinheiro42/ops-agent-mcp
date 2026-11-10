"""Deterministic triage heuristics used by the offline provider.

These rules are written from the synthetic knowledge base (KB-001 priority matrix, KB-002 routing guide)
and — honestly — with knowledge of the synthetic ticket templates. They make the offline mode a useful
*plumbing and safety* proxy; they are not evidence of how well a real model triages real tickets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ops_agent.agent.guard import DEFAULT_TEAM, P1_EVIDENCE
from ops_agent.ticketing.models import Category, Priority, Team

C = Category
KEYWORDS: dict[Category, tuple[tuple[str, float], ...]] = {
    C.WMS: (
        ("wms", 1.5),
        ("nwx warehouse", 2),
        ("wave", 2),
        ("pick", 1),
        ("putaway", 2),
        ("inventory", 2),
        ("cycle count", 2),
        ("sku", 1.5),
        ("on-hand", 1.5),
        ("allocat", 1.5),
        ("label template", 3),
        ("sscc", 2.5),
        ("routing barcode", 2.5),
        ("every printer", 3),
        ("not hardware", 2),
        ("variance", 1.5),
    ),
    C.TMS_ROUTING: (
        ("tms", 1),
        ("nwx route", 2),
        ("tender", 2.5),
        ("loads", 1.5),
        ("optimizer", 2.5),
        ("route", 1),
        ("stop sequence", 2),
        ("delivery window", 2),
        ("fuel surcharge", 2.5),
        ("rate history", 2.5),
        ("dispatch", 0.5),
    ),
    C.EDI_INTEGRATION: (
        ("edi", 2.5),
        ("214", 2),
        ("856", 2),
        ("asn", 2.5),
        ("997", 2),
        ("trading partner", 3),
        ("204", 1.5),
        ("status messages", 1),
    ),
    C.DEVICES: (
        ("handheld", 2),
        ("scanner", 2),
        ("battery", 2.5),
        ("cradle", 2.5),
        ("charg", 1.5),
        ("label printer", 2),
        ("printer", 1.5),
        ("blank labels", 2.5),
        ("jammed", 2),
        ("shifted", 1.5),
        ("rf app", 2),
        ("crashes", 1),
        ("lpn", 1),
        ("devices", 1),
    ),
    C.IDENTITY_ACCESS: (
        ("locked out", 3),
        ("password reset", 3),
        ("reset link", 2),
        ("sso", 3),
        ("log in", 1.5),
        ("login", 1.5),
        ("access request", 2.5),
        ("role", 1.5),
        ("grant", 1.5),
        ("sms code", 2),
        ("read-only access", 2),
    ),
    C.NETWORK: (
        ("network", 2),
        ("wan", 2.5),
        ("wi-fi", 2.5),
        ("vpn", 3),
        ("connectivity", 2),
        ("disconnect", 2),
        ("dead zone", 3),
        ("gateway", 1.5),
    ),
    C.CUSTOMER_PORTAL: (
        ("portal", 2),
        ("503", 2),
        ("eta", 1.5),
        ("tracking page", 2.5),
        ("invite", 2),
        ("bookings", 1.5),
        ("timezone", 1.5),
    ),
    C.WORKPLACE: (
        ("laptop", 3),
        ("monitor", 2.5),
        ("docking station", 3),
        ("shared mailbox", 3),
        ("mailbox", 2),
        ("shared inbox", 3),
        ("screen cracked", 2),
        ("boot", 1.5),
        ("loaner", 1.5),
    ),
    C.BILLING: (
        ("invoice", 2.5),
        ("invoiced", 2.5),
        ("billing", 2.5),
        ("detention", 2),
        ("accessorial", 2.5),
        ("charges", 1.5),
    ),
    C.SECURITY: (
        ("phishing", 4),
        ("suspicious email", 4),
        ("fake login", 4),
        ("compromised", 4),
        ("mfa", 3),
        ("lookalike", 4),
        ("macro attachment", 3),
        ("pretending", 3),
        ("typed my password", 4),
    ),
}

P2_EVIDENCE = re.compile(
    r"\b(all rf users|several|multiple|not tendering|tender failures|zero inbound|not received|"
    r"stopped arriving|duplicate invoices|invoiced twice|both sent|ran twice|phishing|pretending|lookalike|"
    r"carrier will reject|every printer|throughput|slowness|delays)\b",
    re.IGNORECASE,
)
P4_EVIDENCE = re.compile(
    r"\b(question|how do i|could someone explain|where in|requesting|request:|please invite|how to add users|"
    r"need access|please grant|please add me|starts monday|no rush|for peak|onboarding|onboard|"
    r"set up by|new users|docking station|flickers)\b",
    re.IGNORECASE,
)
SCANNER_SOFTWARE = re.compile(r"\b(rf app|app crashes|application closes|app update|rf application)\b", re.I)
AMBIGUITY_THRESHOLD = 2.5


@dataclass(frozen=True, slots=True)
class Classification:
    category: Category | None
    scores: dict[Category, float]
    priority: Priority
    team: Team | None
    confident: bool


def score_categories(text: str, doc_category_hint: Category | None = None) -> dict[Category, float]:
    t = text.lower()
    scores = {c: sum(w for kw, w in kws if kw in t) for c, kws in KEYWORDS.items()}
    if doc_category_hint is not None and scores[doc_category_hint] > 0:
        scores[doc_category_hint] += 1.5  # retrieval agrees with the text -> extra evidence
    return scores


def priority_of(text: str) -> Priority:
    if P1_EVIDENCE.search(text):
        return Priority.P1
    if P2_EVIDENCE.search(text):
        return Priority.P2
    if P4_EVIDENCE.search(text):
        return Priority.P4
    return Priority.P3


def team_for(category: Category, text: str) -> Team:
    if category is Category.DEVICES and SCANNER_SOFTWARE.search(text):
        return Team.WAREHOUSE_SYSTEMS  # KB-002 exception
    return DEFAULT_TEAM[category]


def classify(text: str, doc_category_hint: Category | None = None) -> Classification:
    scores = score_categories(text, doc_category_hint)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].value))
    best, best_score = ranked[0]
    second = ranked[1][1]
    confident = best_score >= AMBIGUITY_THRESHOLD and best_score - second >= 0.5
    category = best if confident else None
    # KB-002: every-printer label problems are WMS label-template issues.
    if category is Category.DEVICES and re.search(r"every printer|not hardware|label template", text, re.I):
        category = Category.WMS
    return Classification(
        category=category,
        scores=scores,
        priority=priority_of(text),
        team=team_for(category, text) if category else None,
        confident=confident,
    )
