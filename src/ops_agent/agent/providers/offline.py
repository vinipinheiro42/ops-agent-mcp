"""Deterministic offline provider: a scripted 'model' that plans tool calls and decides with heuristics.

It speaks the same interface as real providers (tool calls in, tool calls out), so the agent loop, MCP
integration, guardrails, tracing and evaluation are exercised end to end without a key or network.
Token counts are estimated (chars/4) so the trace shows the volume a real model would have consumed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ops_agent.agent.guard import sanitize
from ops_agent.agent.heuristics import classify
from ops_agent.agent.messages import Message, ModelResponse, ToolCall, ToolSpec, Usage
from ops_agent.agent.providers.base import estimate_tokens
from ops_agent.knowledge.bm25 import tokenize
from ops_agent.ticketing.models import Category

DUPLICATE_MIN_SCORE = 6.0
DUPLICATE_WINDOW_HOURS = 12
ANSWER_MIN_SCORE = 5.0
ANSWER_MIN_COVERAGE = 0.5
PRIORITY_ANCHOR = {"P1": "p1-critical", "P2": "p2-high", "P3": "p3-medium", "P4": "p4-low"}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class OfflineHeuristicModel:
    name = "offline-heuristic-v1"

    def __init__(self) -> None:
        self._n = 0

    def _call(self, name: str, args: dict[str, Any]) -> ToolCall:
        self._n += 1
        return ToolCall(id=f"call_{self._n:04d}", name=name, arguments=args)

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        task = json.loads(messages[0].content.split("\n", 1)[0])
        results = {m.tool_name: m.data for m in messages if m.role == "tool" and m.data is not None}
        results["_fetched_docs"] = {
            "ids": [m.data.get("id") for m in messages if m.tool_name == "get_doc" and m.data is not None]
        }
        if task["task"] == "triage":
            calls = self._triage(task["ticket_id"], results)
        else:
            calls = self._answer(task["question"], results)
        text_in = system + "".join(m.content for m in messages) + json.dumps([t.name for t in tools])
        text_out = json.dumps([{"name": c.name, "arguments": c.arguments} for c in calls])
        return ModelResponse(
            content="",
            tool_calls=calls,
            usage=Usage(estimate_tokens(text_in), estimate_tokens(text_out), estimated=True),
            model=self.name,
        )

    # ---- triage ---------------------------------------------------------------------------------

    def _triage(self, ticket_id: str, results: dict[str | None, dict[str, Any]]) -> list[ToolCall]:
        view = results.get("get_ticket")
        if view is None:
            return [self._call("get_ticket", {"ticket_id": ticket_id})]
        ticket = view["ticket"]
        raw = " ".join(
            [ticket["title"], ticket["description"], *(c["body"] for c in view.get("comments", []))]
        )
        text = sanitize(raw)  # a careful model treats embedded instructions as data
        if "search_docs" not in results:
            query = " ".join(tokenize(text)[:30]) or ticket["title"]
            return [
                self._call("search_docs", {"query": query, "limit": 5, "include_unverified": False}),
                self._call("find_similar_tickets", {"ticket_id": ticket_id, "limit": 10}),
                self._call("get_doc", {"doc_id": "KB-001"}),
                self._call("get_doc", {"doc_id": "KB-002"}),
            ]
        hits = (results["search_docs"] or {}).get("hits", [])
        hint = self._category_hint(hits)
        cls = classify(text, hint)
        citations = [f"KB-001#{PRIORITY_ANCHOR[cls.priority.value]}"]
        if cls.category is None:
            return [
                self._call(
                    "submit_triage",
                    {
                        "action": "request_info",
                        "rationale": "Category and impact are unclear from the ticket text; "
                        "KB-001 says to ask for scope.",
                        "questions": [
                            "Which system or device is affected (WMS, TMS, scanner, printer, portal...)?",
                            "How many users or which site are affected, and since when?",
                            "What exact error message do you see?",
                        ],
                        "citations": ["KB-001#rules"],
                    },
                )
            ]
        citations.append(
            "KB-002#exceptions"
            if cls.team and cls.team.value == "warehouse_systems" and cls.category is Category.DEVICES
            else "KB-002#default-routing-by-category"
        )
        runbook = next(
            (h for h in hits if h["trust"] == "verified" and h["doc_id"] not in ("KB-001", "KB-002")), None
        )
        if runbook:
            citations.append(runbook["citation"])
        dup = self._duplicate(
            ticket, (results.get("find_similar_tickets") or {}).get("candidates", []), cls.category
        )
        if dup and "KB-008" not in results["_fetched_docs"]["ids"]:
            return [self._call("get_doc", {"doc_id": "KB-008"})]
        decision: dict[str, Any] = {
            "action": "link_duplicate" if dup else "triage",
            "category": cls.category.value,
            "priority": cls.priority.value,
            "team": cls.team.value if cls.team else None,
            "rationale": (
                f"Category {cls.category.value} (keyword score {cls.scores[cls.category]:.1f}"
                f"{', supported by retrieved runbook' if hint is cls.category else ''});"
                f" priority {cls.priority.value}"
                f" per KB-001; team per KB-002." + (f" Same incident as {dup} (KB-008)." if dup else "")
            ),
            "citations": citations + (["KB-008#definition"] if dup else []),
        }
        if dup:
            decision["duplicate_of"] = dup
        return [self._call("submit_triage", decision)]

    @staticmethod
    def _category_hint(hits: list[dict[str, Any]]) -> Category | None:
        runbook_map = {
            "KB-010": "wms",
            "KB-011": "tms_routing",
            "KB-012": "edi_integration",
            "KB-013": "devices",
            "KB-014": "devices",
            "KB-015": "identity_access",
            "KB-016": "network",
            "KB-017": "customer_portal",
            "KB-018": "billing",
            "KB-019": "security",
            "KB-020": "identity_access",
            "KB-021": "network",
            "KB-022": "devices",
            "KB-023": "devices",
            "KB-024": "edi_integration",
            "KB-025": "customer_portal",
            "KB-026": "billing",
            "KB-027": "workplace",
        }
        for h in hits:
            if h["doc_id"] in runbook_map:
                return Category(runbook_map[h["doc_id"]])
        return None

    @staticmethod
    def _duplicate(
        ticket: dict[str, Any], candidates: list[dict[str, Any]], category: Category
    ) -> str | None:
        """KB-008: same incident = same system/symptom, same site, reported a few hours before."""
        created = _parse_ts(ticket["created_at"])
        for c in candidates:  # ranked by similarity
            age_h = (created - _parse_ts(c["created_at"])).total_seconds() / 3600
            if c["score"] < DUPLICATE_MIN_SCORE or not 0 < age_h <= DUPLICATE_WINDOW_HOURS:
                continue
            title = re.sub(r"</?untrusted[^>]*>", "", c["title"])
            other = classify(sanitize(title)).category
            if other is None or other is category:  # same system, or too little text to tell
                return str(c["id"])
        return None

    # ---- Q&A -------------------------------------------------------------------------------------

    def _answer(self, question: str, results: dict[str | None, dict[str, Any]]) -> list[ToolCall]:
        if "search_docs" not in results:
            return [self._call("search_docs", {"query": question, "limit": 5, "include_unverified": False})]
        hits = [h for h in (results["search_docs"] or {}).get("hits", []) if h["trust"] == "verified"]
        q = set(tokenize(question))
        best = hits[0] if hits else None
        coverage = (
            len(q & set(tokenize(f"{best['title']} {best['section']} {best['snippet']}"))) / max(1, len(q))
            if best
            else 0.0
        )
        if best is None or best["score"] < ANSWER_MIN_SCORE or coverage < ANSWER_MIN_COVERAGE:
            return [self._call("submit_answer", {"abstain": True, "answer": "", "citations": []})]
        doc = results.get("get_doc")
        if doc is None or doc.get("id") != best["doc_id"]:
            return [self._call("get_doc", {"doc_id": best["doc_id"]})]
        # Pick the section of the fetched document that best covers the question, not just the top hit.
        section = max(
            doc["sections"],
            key=lambda s: (
                len(q & set(tokenize(f"{s['heading']} {s['text']}"))),
                s["heading"] == best["section"],
            ),
        )
        parts = re.split(r"(?<=[.!?])\s+|\n+", section["text"])
        sentences = [s.strip(" -*").replace("**", "") for s in parts if len(s.strip()) > 3]
        ranked = sorted(sentences, key=lambda s: -len(q & set(tokenize(s))))
        chosen = [s for s in sentences if s in ranked[:3]]
        answer = f"According to {doc['title']} ({doc['id']}, section '{section['heading']}'): " + " ".join(
            chosen
        )
        return [
            self._call(
                "submit_answer",
                {
                    "abstain": False,
                    "answer": answer,
                    "citations": [f"{doc['id']}#{section['anchor']}"],
                },
            )
        ]
