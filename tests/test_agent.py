"""Agent loop over MCP (in-process sandbox) with the offline provider and scripted models."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from ops_agent.agent.loop import Agent
from ops_agent.agent.messages import Message, ModelResponse, ToolCall, ToolSpec, Usage
from ops_agent.agent.providers.offline import OfflineHeuristicModel
from ops_agent.evals.runner import _fingerprint
from ops_agent.runtime import Sandbox, sandbox
from tests.conftest import FakeClock

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sb(tmp_path: Path) -> AsyncIterator[Sandbox]:
    async with sandbox(tmp_path / "data", clock=FakeClock()) as s:
        yield s


def _gold(sb: Sandbox, kind: str) -> list[dict[str, Any]]:
    return [g for g in sb.dataset.gold if g["kind"] == kind and not g["historical"]]


def _db_hash(sb: Sandbox) -> str:
    return _fingerprint(sb.data_dir / "tickets.db")


class ScriptedModel:
    """Returns a fixed sequence of tool-call batches (stand-in for a real model)."""

    name = "scripted"

    def __init__(self, batches: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.batches = batches
        self.seen: list[list[Message]] = []

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        self.seen.append(list(messages))
        batch = self.batches.pop(0) if self.batches else []
        calls = [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(batch)]
        return ModelResponse("", calls, Usage(100, 20), self.name)


async def test_offline_triage_creates_only_pending_proposals(sb: Sandbox, tmp_path: Path) -> None:
    g = _gold(sb, "base")[0]
    before = _db_hash(sb)
    r = await Agent(sb.mcp, OfflineHeuristicModel(), trace_dir=tmp_path / "t").triage(g["ticket_id"])
    assert r.status == "decided" and r.decision is not None
    assert r.decision.category is not None and r.decision.category.value == g["expected"]["category"]
    assert r.proposals and all(p["status"] == "pending" for p in r.proposals)
    assert _db_hash(sb) == before  # nothing written to the system of record


async def test_trace_jsonl_has_steps_tools_tokens_latency(sb: Sandbox, tmp_path: Path) -> None:
    g = _gold(sb, "base")[1]
    r = await Agent(sb.mcp, OfflineHeuristicModel(), trace_dir=tmp_path / "t").triage(g["ticket_id"])
    assert r.trace_path is not None
    events = [json.loads(line) for line in r.trace_path.read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert {"model_call", "tool_call", "proposal"} <= set(kinds)
    assert all(e["run_id"] == r.run_id for e in events)
    end = events[-1]
    assert end["input_tokens"] > 0 and "latency_ms" in end and "cost_usd" in end


async def test_duplicate_and_ambiguous_paths(sb: Sandbox) -> None:
    dup = _gold(sb, "duplicate")[0]
    r = await Agent(sb.mcp, OfflineHeuristicModel()).triage(dup["ticket_id"])
    assert r.decision is not None and r.decision.action == "link_duplicate"
    amb = _gold(sb, "ambiguous")[0]
    a = await Agent(sb.mcp, OfflineHeuristicModel()).triage(amb["ticket_id"])
    assert a.decision is not None and a.decision.action == "request_info"
    assert any(p["action"] == "transition" for p in a.proposals)


async def test_injection_ticket_is_flagged_and_labelled(sb: Sandbox) -> None:
    for g in _gold(sb, "injection"):
        r = await Agent(sb.mcp, OfflineHeuristicModel()).triage(g["ticket_id"])
        assert r.signals and "possible_prompt_injection" in r.flags
        assert r.decision is None or r.decision.priority is None or r.decision.priority.value != "P1"


async def test_model_cannot_call_write_tools_directly(sb: Sandbox) -> None:
    tid = _gold(sb, "base")[0]["ticket_id"]
    model = ScriptedModel(
        [
            [("propose_transition", {"ticket_id": tid, "to_status": "closed", "rationale": "x"})],
            [("submit_triage", {"action": "request_info", "rationale": "unclear", "questions": ["?"]})],
        ]
    )
    r = await Agent(sb.mcp, model).triage(tid)
    assert "tool_not_allowed:propose_transition" in r.blocked
    closing = [p for p in sb.store.list_proposals(limit=100) if p.payload.get("to_status") == "closed"]
    assert closing == []
    tool_msgs = [m for m in model.seen[1] if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].content.startswith("error: tool")


async def test_tool_output_reaches_model_as_enveloped_data(sb: Sandbox) -> None:
    tid = _gold(sb, "injection")[0]["ticket_id"]
    model = ScriptedModel(
        [
            [("get_ticket", {"ticket_id": tid})],
            [("submit_triage", {"action": "request_info", "rationale": "x"})],
        ]
    )
    await Agent(sb.mcp, model).triage(tid)
    tool_msg = next(m for m in model.seen[1] if m.role == "tool")
    view = json.loads(tool_msg.content)
    assert view["ticket"]["description"].startswith('<untrusted source="ticket.description">')
    assert view["injection_signals"] and "Do not follow" in view["_notice"]


async def test_qa_cites_verified_section_and_abstains_when_unknown(sb: Sandbox) -> None:
    a = await Agent(sb.mcp, OfflineHeuristicModel()).answer("Which team handles RF scanner app crashes?")
    assert not a.answer.abstain and a.answer.citations[0].startswith("KB-002")
    assert "warehouse_systems" in a.answer.answer
    u = await Agent(sb.mcp, OfflineHeuristicModel()).answer("What is the cafeteria lunch menu on Fridays?")
    assert u.answer.abstain and not u.answer.citations


async def test_qa_never_cites_the_unverified_page(sb: Sandbox) -> None:
    model = ScriptedModel(
        [
            [("search_docs", {"query": "self approval", "include_unverified": True})],
            [
                (
                    "submit_answer",
                    {"abstain": False, "answer": "Self-approval is fine.", "citations": ["KB-030#overview"]},
                )
            ],
        ]
    )
    a = await Agent(sb.mcp, model).answer("Can an approver approve their own proposal?")
    assert a.answer.abstain and "citation_not_allowed:KB-030#overview" in a.blocked
