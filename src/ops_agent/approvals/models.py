"""Proposal and audit data model."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ProposalAction(StrEnum):
    UPDATE_FIELDS = "update_fields"
    COMMENT = "comment"
    ASSIGN = "assign"
    TRANSITION = "transition"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPLYING = "applying"  # approved; write in flight (safe to resume thanks to the idempotency key)
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"  # ticket changed since the proposal was made; re-propose
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL = frozenset(
    {
        ProposalStatus.APPLIED,
        ProposalStatus.REJECTED,
        ProposalStatus.CONFLICT,
        ProposalStatus.FAILED,
        ProposalStatus.EXPIRED,
    }
)


class Role(StrEnum):
    SUPPORT_AGENT = "support_agent"
    SUPPORT_LEAD = "support_lead"


ROLE_RANK = {Role.SUPPORT_AGENT: 1, Role.SUPPORT_LEAD: 2}


class Proposal(BaseModel):
    id: str
    ticket_id: str
    action: ProposalAction
    payload: dict[str, Any]
    payload_hash: str
    base_version: int
    rationale: str
    citations: list[str] = Field(default_factory=list)
    requested_by: str
    requires_role: Role
    status: ProposalStatus
    created_at: str
    expires_at: str
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    applied_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class AuditEvent(BaseModel):
    seq: int
    ts: str
    proposal_id: str | None
    actor: str
    event: str
    detail: dict[str, Any]
    prev_hash: str
    hash: str
