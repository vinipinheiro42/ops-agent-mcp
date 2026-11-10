from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from mcp import Client

from ops_agent.approvals.store import ProposalStore
from ops_agent.mcp_server.ticket_client import ReadOnlyTicketClient, ReadOnlyViolationError
from ops_agent.seed.generator import Dataset
from tests.conftest import BASE, call, db_fingerprint, first_open_ticket

READ_TOOLS = {
    "search_tickets",
    "get_ticket",
    "find_similar_tickets",
    "search_docs",
    "get_doc",
    "get_proposal",
    "list_proposals",
}
PROPOSE_TOOLS = {"propose_ticket_update", "propose_comment", "propose_assignment", "propose_transition"}

pytestmark = pytest.mark.anyio


async def test_tool_catalog_and_annotations(mcp_client: Client) -> None:
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | PROPOSE_TOOLS
    for name in READ_TOOLS:
        assert tools[name].annotations is not None and tools[name].annotations.read_only_hint is True
    for name in PROPOSE_TOOLS:
        ann = tools[name].annotations
        assert ann is not None and ann.read_only_hint is False and ann.destructive_hint is False
        assert "approval" in (tools[name].description or "").lower()
    # No tool that writes directly is exposed.
    assert not any(n.startswith(("update_", "add_", "assign_", "transition_", "approve")) for n in tools)


async def test_search_tickets(mcp_client: Client) -> None:
    r = await call(mcp_client, "search_tickets", {"status": "open", "untriaged": True, "limit": 5})
    assert r.ok and r.data is not None
    assert r.data["total"] == 160 and len(r.data["items"]) == 5
    assert "UNTRUSTED" in r.data["untrusted_content_notice"]


async def test_get_ticket_and_not_found(mcp_client: Client) -> None:
    r = await call(mcp_client, "get_ticket", {"ticket_id": "NW-1001"})
    assert r.ok and r.data is not None and r.data["ticket"]["id"] == "NW-1001"
    missing = await call(mcp_client, "get_ticket", {"ticket_id": "NW-9999"})
    assert not missing.ok and "not found" in missing.text
    bad = await call(mcp_client, "get_ticket", {"ticket_id": "DROP TABLE"})
    assert not bad.ok


async def test_find_similar_tickets_surfaces_known_duplicate(mcp_client: Client, dataset: Dataset) -> None:
    dup = next(g for g in dataset.gold if g["kind"] == "duplicate")
    r = await call(mcp_client, "find_similar_tickets", {"ticket_id": dup["ticket_id"], "limit": 5})
    assert r.ok and r.data is not None
    ids = [c["id"] for c in r.data["candidates"]]
    assert dup["duplicate_of"] in ids


async def test_search_and_get_doc(mcp_client: Client) -> None:
    r = await call(mcp_client, "search_docs", {"query": "priority matrix site cannot ship", "limit": 3})
    assert r.ok and r.data is not None and r.data["hits"][0]["doc_id"] == "KB-001"
    assert "#" in r.data["hits"][0]["citation"]
    d = await call(mcp_client, "get_doc", {"doc_id": "KB-004"})
    assert d.ok and d.data is not None and d.data["title"].startswith("Change Approval")
    assert not (await call(mcp_client, "get_doc", {"doc_id": "KB-999"})).ok


async def test_every_proposal_tool_leaves_ticket_db_untouched(
    mcp_client: Client, dataset: Dataset, data_dir: Path, store: ProposalStore
) -> None:
    before = db_fingerprint(data_dir / "tickets.db")
    tid = first_open_ticket(dataset)
    other = first_open_ticket(dataset, kind="duplicate")
    calls = [
        (
            "propose_ticket_update",
            {
                "ticket_id": tid,
                "priority": "P2",
                "category": "wms",
                "team": "warehouse_systems",
                "rationale": "Site degraded per KB-001#p2-high",
                "citations": ["KB-001#p2-high"],
            },
        ),
        (
            "propose_ticket_update",
            {"ticket_id": other, "duplicate_of": tid, "rationale": "Same incident and site, see KB-008"},
        ),
        (
            "propose_comment",
            {"ticket_id": tid, "body": "Linked runbook KB-010.", "rationale": "Help L2 start"},
        ),
        (
            "propose_assignment",
            {"ticket_id": tid, "team": "warehouse_systems", "rationale": "Routing KB-002"},
        ),
        ("propose_transition", {"ticket_id": tid, "to_status": "triaged", "rationale": "Triage complete"}),
    ]
    receipts = []
    for name, args in calls:
        r = await call(mcp_client, name, args)
        assert r.ok, (name, r.text)
        assert r.data is not None and r.data["status"] == "pending"
        assert "No ticket data has been changed" in r.data["message"]
        receipts.append(r.data)
    assert db_fingerprint(data_dir / "tickets.db") == before
    pending = store.list_proposals()
    assert len(pending) == 5
    assert all(p.requested_by == "ops-agent" and p.base_version == 1 for p in pending)
    events = [e.event for e in store.audit_events()]
    assert events == ["proposed"] * 5


async def test_comment_proposals_are_always_internal(
    mcp_client: Client, dataset: Dataset, store: ProposalStore
) -> None:
    tid = first_open_ticket(dataset)
    r = await call(mcp_client, "propose_comment", {"ticket_id": tid, "body": "x", "rationale": "ten chars!"})
    assert r.ok and r.data is not None
    p = store.get(r.data["proposal_id"])
    assert p is not None and p.payload["internal"] is True
    public = await call(
        mcp_client,
        "propose_comment",
        {"ticket_id": tid, "body": "x", "rationale": "ten chars!", "internal": False},
    )
    # Unknown arguments are ignored by the SDK; the tool has no way to make a comment public.
    assert public.ok and public.data is not None
    forced = store.get(public.data["proposal_id"])
    assert forced is not None and forced.payload["internal"] is True


async def test_identical_pending_proposal_is_deduplicated(mcp_client: Client, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    args = {"ticket_id": tid, "priority": "P3", "rationale": "Single user with workaround (KB-001)"}
    a = await call(mcp_client, "propose_ticket_update", args)
    b = await call(mcp_client, "propose_ticket_update", args)
    assert a.data is not None and b.data is not None
    assert a.data["proposal_id"] == b.data["proposal_id"]
    assert (a.data["deduplicated"], b.data["deduplicated"]) == (False, True)


async def test_pending_cap_per_ticket(mcp_client: Client, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    for i in range(5):
        r = await call(
            mcp_client, "propose_comment", {"ticket_id": tid, "body": f"c{i}", "rationale": "ten chars!"}
        )
        assert r.ok
    r = await call(mcp_client, "propose_comment", {"ticket_id": tid, "body": "c5", "rationale": "ten chars!"})
    assert not r.ok and "too many pending" in r.text


@pytest.mark.parametrize(
    ("tool", "args", "error"),
    [
        (
            "propose_ticket_update",
            {"ticket_id": "NW-9999", "priority": "P2", "rationale": "ten chars!"},
            "not found",
        ),
        ("propose_ticket_update", {"ticket_id": "{tid}", "rationale": "ten chars!"}, "nothing to update"),
        (
            "propose_ticket_update",
            {"ticket_id": "{tid}", "duplicate_of": "{tid}", "rationale": "ten chars!"},
            "itself",
        ),
        ("propose_ticket_update", {"ticket_id": "{tid}", "priority": "P0", "rationale": "ten chars!"}, ""),
        ("propose_ticket_update", {"ticket_id": "{tid}", "priority": "P2", "rationale": "short"}, ""),
        (
            "propose_transition",
            {"ticket_id": "{tid}", "to_status": "in_progress", "rationale": "ten chars!"},
            "invalid transition",
        ),
        ("get_proposal", {"proposal_id": "prop_0000000000000000"}, "not found"),
    ],
)
async def test_proposal_validation_errors(
    mcp_client: Client, dataset: Dataset, store: ProposalStore, tool: str, args: dict[str, str], error: str
) -> None:
    tid = first_open_ticket(dataset)
    resolved = {k: (v.replace("{tid}", tid) if isinstance(v, str) else v) for k, v in args.items()}
    r = await call(mcp_client, tool, resolved)
    assert not r.ok
    assert error in r.text
    assert store.list_proposals() == []


async def test_list_and_get_proposal(mcp_client: Client, dataset: Dataset) -> None:
    tid = first_open_ticket(dataset)
    made = await call(
        mcp_client,
        "propose_transition",
        {"ticket_id": tid, "to_status": "closed", "rationale": "Reporter confirmed fixed"},
    )
    assert made.ok and made.data is not None and made.data["requires_role"] == "support_lead"
    listed = await call(mcp_client, "list_proposals", {})
    assert listed.data is not None and [p["id"] for p in listed.data["items"]] == [made.data["proposal_id"]]
    got = await call(mcp_client, "get_proposal", {"proposal_id": made.data["proposal_id"]})
    assert got.data is not None and got.data["status"] == "pending"


async def test_read_only_client_refuses_writes_at_transport(app: object) -> None:
    from ops_agent.mcp_server.ticket_client import make_read_only_http

    async with make_read_only_http(BASE, transport=httpx.ASGITransport(app=app)) as http:  # type: ignore[arg-type]
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with pytest.raises(ReadOnlyViolationError):
                await http.request(method, "/tickets/NW-1001")
        assert (await http.get("/health")).status_code == 200


def test_read_only_client_rejects_plain_http_client() -> None:
    with pytest.raises(ValueError):
        ReadOnlyTicketClient(httpx.AsyncClient(base_url=BASE))
