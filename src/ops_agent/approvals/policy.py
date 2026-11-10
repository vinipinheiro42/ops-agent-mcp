"""Approval policy derived from the client's KB-004 'Change Approval for Automated Actions'."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ops_agent.approvals.models import ROLE_RANK, ProposalAction, Role


def required_role(action: ProposalAction, payload: dict[str, Any]) -> Role:
    """P1 escalations and closing a ticket need a support lead; everything else a support agent."""
    if action is ProposalAction.UPDATE_FIELDS and payload.get("fields", {}).get("priority") == "P1":
        return Role.SUPPORT_LEAD
    if action is ProposalAction.TRANSITION and payload.get("to_status") == "closed":
        return Role.SUPPORT_LEAD
    return Role.SUPPORT_AGENT


@dataclass(frozen=True, slots=True)
class Roster:
    """Who may approve, and with which role. In production this comes from the IdP (SSO groups)."""

    members: dict[str, Role]

    @classmethod
    def from_mapping(cls, raw: dict[str, str]) -> Roster:
        return cls({name: Role(role) for name, role in raw.items()})

    @classmethod
    def from_file(cls, path: Path) -> Roster:
        return cls.from_mapping(json.loads(path.read_text()))

    def role_of(self, actor: str) -> Role | None:
        return self.members.get(actor)

    def can_approve(self, actor: str, needed: Role) -> bool:
        role = self.role_of(actor)
        return role is not None and ROLE_RANK[role] >= ROLE_RANK[needed]


# Synthetic roster written by `ops-agent seed`. Fictional people.
DEFAULT_ROSTER: dict[str, str] = {
    "maya.lindqvist": Role.SUPPORT_LEAD.value,  # Head of Support (fictional)
    "pedro.okafor": Role.SUPPORT_AGENT.value,
    "grace.hollis": Role.SUPPORT_AGENT.value,
}
