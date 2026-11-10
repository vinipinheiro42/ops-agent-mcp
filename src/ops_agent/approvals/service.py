"""Approval workflow: authorise the approver, claim the proposal, apply it once, audit everything."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ops_agent.approvals.models import TERMINAL, Proposal, ProposalStatus
from ops_agent.approvals.policy import Roster
from ops_agent.approvals.store import ProposalStore, iso, parse_iso
from ops_agent.approvals.writer import ApplyConflictError, ApplyRejectedError, TicketWriter


class ApprovalError(Exception):
    pass


class ProposalNotFoundError(ApprovalError):
    pass


class PermissionDeniedError(ApprovalError):
    pass


class InvalidStateError(ApprovalError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    proposal: Proposal
    already_applied: bool = False


class ApprovalService:
    def __init__(self, store: ProposalStore, writer: TicketWriter, roster: Roster) -> None:
        self.store = store
        self.writer = writer
        self.roster = roster

    def _load(self, proposal_id: str) -> Proposal:
        p = self.store.get(proposal_id)
        if p is None:
            raise ProposalNotFoundError(proposal_id)
        return p

    def _authorise(self, p: Proposal, actor: str, verb: str) -> None:
        reason: str | None = None
        if self.roster.role_of(actor) is None:
            reason = "actor is not in the approver roster"
        elif actor == p.requested_by:
            reason = "self-approval is not allowed"
        elif not self.roster.can_approve(actor, p.requires_role):
            reason = f"requires role {p.requires_role.value}"
        if reason:
            self.store.audit(p.id, actor, f"{verb}_denied", {"reason": reason})
            raise PermissionDeniedError(reason)

    async def approve(self, proposal_id: str, actor: str, note: str | None = None) -> ApprovalOutcome:
        p = self._load(proposal_id)
        if p.status is ProposalStatus.APPLIED:
            self.store.audit(p.id, actor, "approve_noop", {"reason": "already applied"})
            return ApprovalOutcome(p, already_applied=True)
        if p.status in TERMINAL:
            raise InvalidStateError(f"proposal is {p.status.value}")
        self._authorise(p, actor, "approve")

        if p.status is ProposalStatus.PENDING:
            if self.store.now() >= parse_iso(p.expires_at):
                self.store.compare_and_set(
                    p.id,
                    expected=[ProposalStatus.PENDING],
                    new_status=ProposalStatus.EXPIRED,
                    actor=actor,
                    event="expired",
                )
                raise InvalidStateError("proposal expired; re-propose against the current ticket")
            claimed = self.store.compare_and_set(
                p.id,
                expected=[ProposalStatus.PENDING],
                new_status=ProposalStatus.APPLYING,
                actor=actor,
                event="approved",
                detail={"note": note},
                decided_by=actor,
                decided_at=iso(self.store.now()),
                decision_note=note,
            )
            if not claimed:  # lost a race: re-read and follow whatever state won
                return await self.approve(proposal_id, actor, note)
            p = self._load(proposal_id)

        # status is APPLYING: first attempt, or resuming after a crash / concurrent approver.
        try:
            result = await self.writer.apply(p, approver=p.decided_by or actor)
        except ApplyConflictError as exc:
            self.store.compare_and_set(
                p.id,
                expected=[ProposalStatus.APPLYING],
                new_status=ProposalStatus.CONFLICT,
                actor=actor,
                event="apply_conflict",
                detail={"error": str(exc)},
                error=str(exc),
            )
            raise InvalidStateError(f"ticket changed since proposal: {exc}") from exc
        except ApplyRejectedError as exc:
            self.store.compare_and_set(
                p.id,
                expected=[ProposalStatus.APPLYING],
                new_status=ProposalStatus.FAILED,
                actor=actor,
                event="apply_failed",
                detail={"error": str(exc)},
                error=str(exc),
            )
            raise InvalidStateError(str(exc)) from exc
        except httpx.HTTPError as exc:
            # Transient: leave it APPLYING so `approve` can be re-run; the idempotency key prevents doubles.
            self.store.audit(p.id, actor, "apply_error_retryable", {"error": repr(exc)})
            raise

        self.store.compare_and_set(
            p.id,
            expected=[ProposalStatus.APPLYING],
            new_status=ProposalStatus.APPLIED,
            actor=actor,
            event="applied",
            detail={"replayed": result["replayed"]},
            applied_at=iso(self.store.now()),
            result=result,
        )
        final = self._load(proposal_id)
        return ApprovalOutcome(final, already_applied=bool(result["replayed"]))

    def reject(self, proposal_id: str, actor: str, reason: str) -> Proposal:
        p = self._load(proposal_id)
        if p.status is not ProposalStatus.PENDING:
            raise InvalidStateError(f"only pending proposals can be rejected (status: {p.status.value})")
        self._authorise(p, actor, "reject")
        ok = self.store.compare_and_set(
            p.id,
            expected=[ProposalStatus.PENDING],
            new_status=ProposalStatus.REJECTED,
            actor=actor,
            event="rejected",
            detail={"reason": reason},
            decided_by=actor,
            decided_at=iso(self.store.now()),
            decision_note=reason,
        )
        if not ok:
            raise InvalidStateError("proposal changed state concurrently")
        return self._load(proposal_id)
