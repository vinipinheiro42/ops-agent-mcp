from __future__ import annotations

import sqlite3
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import Client

from ops_agent.approvals.models import ProposalStatus
from ops_agent.approvals.service import ApprovalService, InvalidStateError, PermissionDeniedError
from ops_agent.approvals.store import ProposalStore
from ops_agent.approvals.writer import TicketWriter
from ops_agent.seed.generator import Dataset
from ops_agent.ticketing.db import TicketRepository
from tests.conftest import BASE, WRITE_HEADERS, FakeClock, call, db_fingerprint, first_open_ticket

pytestmark = pytest.mark.anyio

LEAD, AGENT = "maya.lindqvist", "pedro.okafor"


async def _propose(client: Client, tool: str, **args: object) -> str:
    r = await call(client, tool, dict(args))
    assert r.ok and r.data is not None, r.text
    return str(r.data["proposal_id"])


async def test_approve_applies_update_and_audits(
    mcp_client: Client, service: ApprovalService, repo: TicketRepository, dataset: Dataset
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(
        mcp_client,
        "propose_ticket_update",
        ticket_id=tid,
        priority="P2",
        category="wms",
        team="warehouse_systems",
        rationale="RF slowness at one site: KB-001#p2-high",
    )
    outcome = await service.approve(pid, actor=AGENT, note="looks right")
    assert outcome.proposal.status is ProposalStatus.APPLIED and not outcome.already_applied
    t = repo.get(tid)
    assert (t.priority, t.category, t.team, t.version) == ("P2", "wms", "warehouse_systems", 2)
    events = [e.event for e in service.store.audit_events(pid)]
    assert events == ["proposed", "approved", "applied"]
    assert service.store.verify_audit_chain() == (True, None)


async def test_each_action_type_applies(
    mcp_client: Client, service: ApprovalService, repo: TicketRepository, dataset: Dataset
) -> None:
    tid = first_open_ticket(dataset)
    c = await _propose(
        mcp_client, "propose_comment", ticket_id=tid, body="See KB-010", rationale="Runbook link"
    )
    await service.approve(c, actor=AGENT)
    a = await _propose(
        mcp_client, "propose_assignment", ticket_id=tid, team="network_ops", rationale="KB-002 routing"
    )
    await service.approve(a, actor=AGENT)
    tr = await _propose(
        mcp_client, "propose_transition", ticket_id=tid, to_status="triaged", rationale="Triage done"
    )
    await service.approve(tr, actor=AGENT)
    t = repo.get(tid)
    assert t.team == "network_ops" and t.status == "triaged"
    assert [cm.body for cm in t.comments] == ["See KB-010"]
    assert t.comments[0].author == f"ops-agent (approved by {AGENT})" and t.comments[0].internal


async def test_reject_does_not_write(
    mcp_client: Client, service: ApprovalService, dataset: Dataset, data_dir: Path
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(
        mcp_client, "propose_ticket_update", ticket_id=tid, priority="P4", rationale="Looks minor"
    )
    before = db_fingerprint(data_dir / "tickets.db")
    p = service.reject(pid, actor=AGENT, reason="Impact is site-wide")
    assert p.status is ProposalStatus.REJECTED and p.decision_note == "Impact is site-wide"
    assert db_fingerprint(data_dir / "tickets.db") == before
    with pytest.raises(InvalidStateError):
        await service.approve(pid, actor=AGENT)
    assert db_fingerprint(data_dir / "tickets.db") == before


async def test_approve_twice_is_idempotent(
    mcp_client: Client, service: ApprovalService, repo: TicketRepository, dataset: Dataset
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(mcp_client, "propose_comment", ticket_id=tid, body="once", rationale="ten chars!")
    first = await service.approve(pid, actor=AGENT)
    second = await service.approve(pid, actor=LEAD)
    assert first.proposal.status is second.proposal.status is ProposalStatus.APPLIED
    assert second.already_applied
    assert repo.count_comments() == 1
    assert [e.event for e in service.store.audit_events(pid)][-1] == "approve_noop"


async def test_concurrent_approvals_apply_once(
    mcp_client: Client, service: ApprovalService, repo: TicketRepository, dataset: Dataset
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(mcp_client, "propose_comment", ticket_id=tid, body="race", rationale="ten chars!")
    async with anyio.create_task_group() as tg:
        for actor in (AGENT, LEAD, AGENT):
            tg.start_soon(service.approve, pid, actor)
    assert repo.count_comments() == 1
    p = service.store.get(pid)
    assert p is not None and p.status is ProposalStatus.APPLIED
    assert [e.event for e in service.store.audit_events(pid)].count("approved") == 1


async def test_resume_after_crash_between_claim_and_apply(
    mcp_client: Client,
    app: object,
    store: ProposalStore,
    service: ApprovalService,
    repo: TicketRepository,
    dataset: Dataset,
) -> None:
    """Simulate: the write reached the API but the process died before recording `applied`."""
    tid = first_open_ticket(dataset)
    pid = await _propose(mcp_client, "propose_comment", ticket_id=tid, body="crash", rationale="ten chars!")
    assert store.compare_and_set(
        pid,
        expected=[ProposalStatus.PENDING],
        new_status=ProposalStatus.APPLYING,
        actor=AGENT,
        event="approved",
        decided_by=AGENT,
    )
    proposal = store.get(pid)
    assert proposal is not None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:  # type: ignore[arg-type]
        await TicketWriter(http, WRITE_HEADERS["Authorization"].split()[1]).apply(proposal, approver=AGENT)
    assert repo.count_comments() == 1
    outcome = await service.approve(pid, actor=AGENT)  # operator re-runs approve
    assert outcome.proposal.status is ProposalStatus.APPLIED and outcome.already_applied
    assert repo.count_comments() == 1


async def test_transient_api_failure_leaves_proposal_resumable(
    mcp_client: Client,
    app: object,
    store: ProposalStore,
    service: ApprovalService,
    repo: TicketRepository,
    dataset: Dataset,
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(mcp_client, "propose_comment", ticket_id=tid, body="flaky", rationale="ten chars!")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("api down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url=BASE) as down:
        flaky = ApprovalService(store, TicketWriter(down), service.roster)
        with pytest.raises(httpx.ConnectError):
            await flaky.approve(pid, actor=AGENT)
    p = store.get(pid)
    assert p is not None and p.status is ProposalStatus.APPLYING
    assert repo.count_comments() == 0
    assert (await service.approve(pid, actor=AGENT)).proposal.status is ProposalStatus.APPLIED
    assert repo.count_comments() == 1


@pytest.mark.parametrize(
    ("actor", "reason"),
    [("ops-agent", "not in the approver roster"), ("mallory", "not in the approver roster")],
)
async def test_unknown_or_agent_identity_cannot_approve(
    mcp_client: Client, service: ApprovalService, dataset: Dataset, actor: str, reason: str
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(mcp_client, "propose_comment", ticket_id=tid, body="x", rationale="ten chars!")
    with pytest.raises(PermissionDeniedError, match=reason):
        await service.approve(pid, actor=actor)
    p = service.store.get(pid)
    assert p is not None and p.status is ProposalStatus.PENDING


async def test_self_approval_forbidden(
    service: ApprovalService, store: ProposalStore, dataset: Dataset
) -> None:
    from ops_agent.approvals.models import ProposalAction, Role

    tid = first_open_ticket(dataset)
    p, _ = store.create(
        ticket_id=tid,
        action=ProposalAction.COMMENT,
        payload={"body": "x", "internal": True},
        base_version=1,
        rationale="manual",
        citations=[],
        requested_by=AGENT,
        requires_role=Role.SUPPORT_AGENT,
    )
    with pytest.raises(PermissionDeniedError, match="self-approval"):
        await service.approve(p.id, actor=AGENT)


async def test_p1_and_close_require_support_lead(
    mcp_client: Client, service: ApprovalService, repo: TicketRepository, dataset: Dataset
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(
        mcp_client,
        "propose_ticket_update",
        ticket_id=tid,
        priority="P1",
        rationale="Site cannot ship: KB-001#p1-critical",
    )
    with pytest.raises(PermissionDeniedError, match="support_lead"):
        await service.approve(pid, actor=AGENT)
    assert repo.get(tid).priority is None
    assert (await service.approve(pid, actor=LEAD)).proposal.status is ProposalStatus.APPLIED
    assert repo.get(tid).priority == "P1"


async def test_expired_proposal_cannot_be_approved(
    mcp_client: Client, service: ApprovalService, clock: FakeClock, dataset: Dataset, data_dir: Path
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(mcp_client, "propose_comment", ticket_id=tid, body="late", rationale="ten chars!")
    clock.advance(hours=25)
    before = db_fingerprint(data_dir / "tickets.db")
    with pytest.raises(InvalidStateError, match="expired"):
        await service.approve(pid, actor=AGENT)
    p = service.store.get(pid)
    assert p is not None and p.status is ProposalStatus.EXPIRED
    assert db_fingerprint(data_dir / "tickets.db") == before


async def test_stale_proposal_becomes_conflict(
    mcp_client: Client,
    service: ApprovalService,
    api: httpx.AsyncClient,
    repo: TicketRepository,
    dataset: Dataset,
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(
        mcp_client, "propose_ticket_update", ticket_id=tid, priority="P3", rationale="ten chars!"
    )
    # A human edits the ticket directly in the service desk before the proposal is reviewed.
    await api.patch(f"/tickets/{tid}", json={"priority": "P2"}, headers=WRITE_HEADERS)
    with pytest.raises(InvalidStateError, match="changed since proposal"):
        await service.approve(pid, actor=AGENT)
    p = service.store.get(pid)
    assert p is not None and p.status is ProposalStatus.CONFLICT
    assert repo.get(tid).priority == "P2"  # the human's edit wins


async def test_audit_log_is_append_only_and_tamper_evident(
    mcp_client: Client, service: ApprovalService, store: ProposalStore, dataset: Dataset
) -> None:
    tid = first_open_ticket(dataset)
    pid = await _propose(
        mcp_client, "propose_comment", ticket_id=tid, body="audit me", rationale="ten chars!"
    )
    await service.approve(pid, actor=AGENT)
    conn = sqlite3.connect(store.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE audit_log SET actor = 'someone-else' WHERE seq = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM audit_log")
        # An attacker with DB access drops the triggers and rewrites history: the chain detects it.
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET actor = 'someone-else' WHERE seq = 2")
        conn.commit()
    finally:
        conn.close()
    assert store.verify_audit_chain() == (False, 2)
