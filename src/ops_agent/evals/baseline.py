"""'No agent' baseline: the kind of keyword automation rules a service desk can configure natively.

First-match keyword rules on the raw ticket text, default routing (no KB-002 exceptions), naive urgency
keywords for priority, no duplicate detection, never asks for information. Q&A baseline returns the top
BM25 hit (any trust level) and never abstains.
"""

from __future__ import annotations

import re

from ops_agent.agent.guard import DEFAULT_TEAM
from ops_agent.agent.schemas import AnswerDraft, TriageDecision
from ops_agent.knowledge.store import DocStore
from ops_agent.ticketing.models import Category, Priority

RULES: tuple[tuple[str, Category], ...] = (
    (r"phish|suspicious|mfa|compromis", Category.SECURITY),
    (r"\bedi\b|\basn\b|214|856", Category.EDI_INTEGRATION),
    (r"invoice|billing", Category.BILLING),
    (r"portal", Category.CUSTOMER_PORTAL),
    (r"\btms\b|tender|route", Category.TMS_ROUTING),
    (r"\bwms\b|wave|inventory|putaway", Category.WMS),
    (r"scanner|handheld|printer|label", Category.DEVICES),
    (r"password|locked|sso|login|access", Category.IDENTITY_ACCESS),
    (r"network|wi-?fi|vpn|wan", Category.NETWORK),
    (r"laptop|monitor|mailbox|inbox", Category.WORKPLACE),
)
URGENT = re.compile(r"\b(down|outage|urgent|asap|p1|critical|nobody|immediately)\b", re.I)
HIGH = re.compile(r"\b(slow|error|failed|failing|not working|stuck)\b", re.I)


def baseline_triage(title: str, description: str) -> TriageDecision:
    text = f"{title} {description}".lower()
    category = next((c for rx, c in RULES if re.search(rx, text)), Category.WORKPLACE)
    priority = Priority.P1 if URGENT.search(text) else Priority.P2 if HIGH.search(text) else Priority.P3
    return TriageDecision(
        action="triage",
        category=category,
        priority=priority,
        team=DEFAULT_TEAM[category],
        rationale="keyword rule",
        citations=[],
    )


def baseline_answer(docs: DocStore, question: str) -> AnswerDraft:
    hits = docs.search(question, limit=1, include_unverified=True)
    if not hits:
        return AnswerDraft(abstain=True)
    return AnswerDraft(abstain=False, answer=hits[0].snippet, citations=[hits[0].citation])
