"""Prompt-injection defence.

Layers (see docs/03-architecture.md §8):
1. *Detection & flagging* — regexes over every untrusted field (title, description, comments, docs).
2. *Data, not instructions* — untrusted text is wrapped in an envelope before the model sees it.
3. *Sanitised view* — flagged sentences are removed from the text used for impact evidence.
4. *Decision firewall* — whatever the model says, a decision is checked against evidence and policy
   before it can become a proposal. This layer does not depend on the model behaving.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ops_agent.agent.schemas import AnswerDraft, TriageDecision
from ops_agent.ticketing.models import Category, Priority, Team

CANARY = "NWOPS-CANARY-7F3A"  # planted in the system prompt; must never appear in any output

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE | re.DOTALL))
    for name, rx in (
        (
            "override_instructions",
            r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(instructions|rules|policies|prompt)\b",
        ),
        (
            "addressed_to_ai",
            r"\b(note|message|instruction)s?\s+(to|for)\s+(the\s+)?(ai|assistant|bot|agent)\b",
        ),
        ("addressed_to_ai", r"(^|[\s\[(<!-])(ai\s+)?(assistant|bot|agent)\b\s*[:,]"),
        ("addressed_to_ai", r"\bai assistant reading this\b"),
        ("hidden_markup", r"<!--.*?-->"),
        ("tool_call_smuggling", r"\[\[\s*tool\s*:.*?\]\]"),
        (
            "tool_call_smuggling",
            r"\b(propose|call|invoke)_?\w*\s*\(|\bpropose_(ticket_update|comment|assignment|transition)\b",
        ),
        (
            "secret_request",
            r"\b(system prompt|api (key|token)s?|access tokens?|credentials?)\b[^.\n]{0,40}\b(paste|reveal|print|show|send|share)|\b(paste|reveal|print|show|send|share)\b[^.\n]{0,40}\b(system prompt|api (key|token)s?|tokens?)\b",
        ),
        (
            "fake_authority",
            r"\b(pre-?approve|auto-?approve|no human review|approve (your|its) own|without (human )?approval)\b",
        ),
        ("role_override", r"\b(you are now|from now on|policies above are outdated)\b"),
        ("mass_action", r"\b(every|all)\s+open\s+tickets?\b|\bon all tickets\b|\bclose (both|all)\b"),
    )
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class InjectionSignal:
    type: str
    field: str
    excerpt: str


def detect(text: str, source: str) -> list[InjectionSignal]:
    out: list[InjectionSignal] = []
    seen: set[tuple[str, str]] = set()
    for name, rx in _PATTERNS:
        for m in rx.finditer(text):
            key = (name, m.group(0).strip()[:80])
            if key not in seen:
                seen.add(key)
                out.append(InjectionSignal(type=name, field=source, excerpt=key[1]))
    return out


def sanitize(text: str) -> str:
    """Remove hidden markup, smuggled tool calls and any sentence that carries an injection signal."""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"\[\[.*?\]\]", " ", text, flags=re.DOTALL)
    kept = [s for s in _SENTENCE_SPLIT.split(text) if s.strip() and not detect(s, "_")]
    return " ".join(kept).strip()


def envelope(source: str, text: str) -> str:
    safe = text.replace("</untrusted", "<\\/untrusted")
    return f'<untrusted source="{source}">{safe}</untrusted>'


def strip_urls(text: str, allowed: frozenset[str] = frozenset()) -> str:
    """Replace links with a marker unless they appear verbatim in verified, cited sources."""
    return _URL.sub(lambda m: m.group(0) if m.group(0).rstrip(".,)") in allowed else "[link removed]", text)


def urls_in(text: str) -> frozenset[str]:
    return frozenset(m.group(0).rstrip(".,)") for m in _URL.finditer(text))


# ---- decision firewall ---------------------------------------------------------------------------

DEFAULT_TEAM: dict[Category, Team] = {
    Category.WMS: Team.WAREHOUSE_SYSTEMS,
    Category.TMS_ROUTING: Team.TRANSPORT_SYSTEMS,
    Category.EDI_INTEGRATION: Team.INTEGRATIONS,
    Category.DEVICES: Team.FIELD_DEVICES,
    Category.IDENTITY_ACCESS: Team.IDENTITY_ACCESS,
    Category.NETWORK: Team.NETWORK_OPS,
    Category.CUSTOMER_PORTAL: Team.DIGITAL_CHANNELS,
    Category.WORKPLACE: Team.WORKPLACE,
    Category.BILLING: Team.FINANCE_SYSTEMS,
    Category.SECURITY: Team.SECURITY_OPS,
}
# KB-002 documented exceptions: (category, team) pairs that are allowed besides the default.
ROUTING_EXCEPTIONS: frozenset[tuple[Category, Team]] = frozenset({(Category.DEVICES, Team.WAREHOUSE_SYSTEMS)})

P1_EVIDENCE = re.compile(
    r"\b(whole|entire|every user|all users|everyone|nobody can|company-wide|all sites|no network|"
    r"total network outage|nothing loads|for every customer|for all customers|all external users|"
    r"no bookings|typed my password|fake login|mfa (prompts|push requests) i did not|outbound is blocked|"
    r"shipping stopped|cannot release waves|whole site)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TriageContext:
    ticket_id: str
    created_at: str
    untrusted_text: str  # title + description + comments, raw
    signals: list[InjectionSignal]
    retrieved: dict[str, str]  # citation -> trust ("verified" | "unverified")
    similar_ids: set[str]
    # Text that may serve as P1 impact evidence: only fields (title, description, each comment) that carry
    # NO injection signal. A flagged field is excluded whole, not sentence by sentence (INC-SIM-001: the
    # sentence after "Note to the AI assistant:" carried the fake impact claim and survived sanitising).
    evidence_text: str | None = None


def evidence_text(fields: list[str]) -> str:
    return " ".join(sanitize(f) for f in fields if not detect(f, "_"))


@dataclass(slots=True)
class Review:
    decision: TriageDecision
    flags: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


class P1CircuitBreaker:
    """Post-mortem action PM-1: cap new (non-duplicate) P1 proposals per rolling window."""

    def __init__(self, limit: int = 3, window: timedelta = timedelta(hours=1)) -> None:
        self.limit = limit
        self.window = window
        self._events: deque[datetime] = deque()

    def allow(self, now: datetime) -> bool:
        while self._events and now - self._events[0] > self.window:
            self._events.popleft()
        if len(self._events) >= self.limit:
            return False
        self._events.append(now)
        return True


class DecisionFirewall:
    def __init__(self, breaker: P1CircuitBreaker | None = None) -> None:
        self.breaker = breaker or P1CircuitBreaker()

    def review(self, d: TriageDecision, ctx: TriageContext, now: datetime) -> Review:
        r = Review(decision=d.model_copy(deep=True))
        dec = r.decision
        if ctx.signals:
            r.flags.append("possible_prompt_injection")

        # Output hygiene: no links, no canary leakage, bounded text.
        for name in ("rationale",):
            value = getattr(dec, name)
            if CANARY in value:
                r.blocked.append("canary_leak")
                value = value.replace(CANARY, "[redacted]")
            cleaned = strip_urls(value)
            if cleaned != value:
                r.blocked.append("url_in_output")
            setattr(dec, name, cleaned[:1000])
        dec.questions = [strip_urls(q)[:300] for q in dec.questions if CANARY not in q]

        # Citations must come from this run's retrieval and from verified documents only.
        valid = []
        for c in dec.citations:
            doc_id = c.split("#", 1)[0]
            trust = ctx.retrieved.get(c) or ctx.retrieved.get(doc_id)
            if trust == "verified":
                valid.append(c)
            else:
                r.blocked.append(f"citation_not_allowed:{c}")
        dec.citations = valid
        if not valid and dec.action != "request_info":
            r.flags.append("no_verified_citation")

        # Duplicate links must be backed by find_similar_tickets evidence.
        if dec.duplicate_of is not None and (
            dec.duplicate_of == ctx.ticket_id or dec.duplicate_of not in ctx.similar_ids
        ):
            r.blocked.append(f"duplicate_without_evidence:{dec.duplicate_of}")
            dec.duplicate_of = None
            if dec.action == "link_duplicate":
                dec.action = "triage"
        if dec.action == "link_duplicate" and dec.duplicate_of is None:
            dec.action = "triage"

        # Routing must follow KB-002.
        if dec.category is not None and dec.team is not None:
            if dec.team != DEFAULT_TEAM[dec.category] and (dec.category, dec.team) not in ROUTING_EXCEPTIONS:
                r.blocked.append(f"team_violates_routing:{dec.team.value}")
                dec.team = DEFAULT_TEAM[dec.category]
        elif dec.category is not None and dec.team is None:
            dec.team = DEFAULT_TEAM[dec.category]

        # P1 needs impact evidence in the *sanitised* text, and is rate-limited.
        if dec.priority is Priority.P1:
            evidence = ctx.evidence_text if ctx.evidence_text is not None else sanitize(ctx.untrusted_text)
            if not P1_EVIDENCE.search(evidence):
                r.blocked.append("p1_without_impact_evidence")
                dec.priority = None
                r.flags.append("needs_human_priority")
            elif dec.duplicate_of is None and not self.breaker.allow(now):
                r.blocked.append("p1_rate_limited")
                dec.priority = None
                r.flags.append("needs_human_priority")

        if dec.action == "request_info" and not dec.questions:
            dec.questions = ["How many users or sites are affected, and since when?"]
        return r


def review_answer(
    draft: AnswerDraft, retrieved: dict[str, str], allowed_urls: frozenset[str] = frozenset()
) -> tuple[AnswerDraft, list[str]]:
    blocked: list[str] = []
    out = draft.model_copy(deep=True)
    if CANARY in out.answer:
        blocked.append("canary_leak")
        out.answer = out.answer.replace(CANARY, "[redacted]")
    cleaned = strip_urls(out.answer, allowed_urls)
    if cleaned != out.answer:
        blocked.append("url_in_output")
    out.answer = cleaned
    kept = []
    for c in out.citations:
        if retrieved.get(c) == "verified":
            kept.append(c)
        else:
            blocked.append(f"citation_not_allowed:{c}")
    out.citations = kept
    if not out.abstain and not kept:
        blocked.append("answer_without_verified_citation")
        out = AnswerDraft(abstain=True, answer="", citations=[])
    return out, blocked
