"""The only HTTP client in the system that performs writes against the ticket API."""

from __future__ import annotations

from typing import Any

import httpx

from ops_agent.approvals.models import Proposal, ProposalAction


class ApplyConflictError(Exception):
    """The ticket changed since the proposal was made (HTTP 409)."""


class ApplyRejectedError(Exception):
    """The API refused the request for a non-retryable reason (4xx other than 409)."""


class TicketWriter:
    def __init__(self, client: httpx.AsyncClient, write_token: str | None = None) -> None:
        self._client = client
        self._token = write_token

    @staticmethod
    def request_for(proposal: Proposal) -> tuple[str, str, dict[str, Any]]:
        tid = proposal.ticket_id
        p = proposal.payload
        if proposal.action is ProposalAction.UPDATE_FIELDS:
            return "PATCH", f"/tickets/{tid}", {**p["fields"], "expected_version": proposal.base_version}
        if proposal.action is ProposalAction.COMMENT:
            return "POST", f"/tickets/{tid}/comments", {"body": p["body"], "internal": p["internal"]}
        if proposal.action is ProposalAction.ASSIGN:
            return (
                "POST",
                f"/tickets/{tid}/assign",
                {
                    "team": p["team"],
                    "assignee": p.get("assignee"),
                    "expected_version": proposal.base_version,
                },
            )
        if proposal.action is ProposalAction.TRANSITION:
            return (
                "POST",
                f"/tickets/{tid}/transitions",
                {
                    "to_status": p["to_status"],
                    "expected_version": proposal.base_version,
                },
            )
        raise ValueError(f"unsupported action {proposal.action}")

    async def apply(self, proposal: Proposal, approver: str) -> dict[str, Any]:
        method, path, body = self.request_for(proposal)
        headers = {
            # The proposal id is the idempotency key: re-sending an approved proposal is always safe.
            "Idempotency-Key": proposal.id,
            "X-Actor": f"ops-agent (approved by {approver})",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = await self._client.request(method, path, json=body, headers=headers)
        if resp.status_code == 409:
            raise ApplyConflictError(resp.json().get("detail", "conflict"))
        if 400 <= resp.status_code < 500:
            raise ApplyRejectedError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()  # 5xx -> httpx.HTTPStatusError, retryable
        data: dict[str, Any] = resp.json()
        return {
            "http_status": resp.status_code,
            "replayed": resp.headers.get("Idempotent-Replayed") == "true",
            "response": data,
        }
