"""FastAPI app emulating a small slice of a Jira-style service-desk REST API.

Write endpoints:
* accept an optional `Idempotency-Key` header (replays return the stored response, never re-apply);
* accept `expected_version` in the body for optimistic concurrency (409 on mismatch);
* require `Authorization: Bearer <token>` when the app is created with a write token;
* record the acting identity from the `X-Actor` header.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response

from ops_agent.ticketing.db import (
    ConflictError,
    IdempotencyMismatchError,
    NotFoundError,
    TicketRepository,
    WriteResult,
    request_fingerprint,
)
from ops_agent.ticketing.models import (
    AssignRequest,
    Category,
    CommentCreate,
    Priority,
    Status,
    Team,
    TicketPage,
    TicketPatch,
    TicketWithComments,
    TransitionRequest,
)

IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)]
Actor = Annotated[str, Header(alias="X-Actor", min_length=1, max_length=100)]


def create_app(repo: TicketRepository, write_token: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Northwind Service Desk (fake)",
        version="0.2.0",
        description="Synthetic Jira-style ticket API for the Ops Agent portfolio project.",
    )

    def require_writer(authorization: Annotated[str | None, Header()] = None) -> None:
        if write_token is None:
            return
        expected = f"Bearer {write_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="write token required")

    writer_only = [Depends(require_writer)]

    def execute(
        response: Response,
        key: str | None,
        method: str,
        path: str,
        body: dict[str, Any],
        operation: Callable[..., tuple[int, dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            result: WriteResult = repo.run_write(
                idempotency_key=key,
                fingerprint=request_fingerprint(method, path, body),
                operation=operation,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"ticket not found: {exc}") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IdempotencyMismatchError as exc:
            raise HTTPException(
                status_code=422, detail="Idempotency-Key reused with a different request body"
            ) from exc
        response.status_code = result.status_code
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return result.body

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/tickets", response_model=TicketPage)
    def list_tickets(
        q: str | None = None,
        status: Status | None = None,
        category: Category | None = None,
        team: Team | None = None,
        priority: Priority | None = None,
        site: str | None = None,
        untriaged: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> TicketPage:
        return repo.search(
            q=q,
            status=status.value if status else None,
            category=category.value if category else None,
            team=team.value if team else None,
            priority=priority.value if priority else None,
            site=site,
            untriaged=untriaged,
            limit=limit,
            offset=offset,
        )

    @app.get("/tickets/{ticket_id}", response_model=TicketWithComments)
    def get_ticket(ticket_id: str) -> TicketWithComments:
        try:
            return repo.get(ticket_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"ticket not found: {ticket_id}") from exc

    @app.patch("/tickets/{ticket_id}", dependencies=writer_only)
    def update_ticket(
        ticket_id: str, patch: TicketPatch, response: Response, actor: Actor, key: IdemKey = None
    ) -> dict[str, Any]:
        fields = patch.model_dump(mode="json", exclude_unset=True, exclude={"expected_version"})
        return execute(
            response,
            key,
            "PATCH",
            f"/tickets/{ticket_id}",
            patch.model_dump(mode="json", exclude_unset=True),
            repo.op_update_fields(ticket_id, fields, patch.expected_version),
        )

    @app.post("/tickets/{ticket_id}/comments", status_code=201, dependencies=writer_only)
    def add_comment(
        ticket_id: str, body: CommentCreate, response: Response, actor: Actor, key: IdemKey = None
    ) -> dict[str, Any]:
        return execute(
            response,
            key,
            "POST",
            f"/tickets/{ticket_id}/comments",
            body.model_dump(mode="json"),
            repo.op_comment(ticket_id, actor, body.body, body.internal),
        )

    @app.post("/tickets/{ticket_id}/assign", dependencies=writer_only)
    def assign(
        ticket_id: str, body: AssignRequest, response: Response, actor: Actor, key: IdemKey = None
    ) -> dict[str, Any]:
        return execute(
            response,
            key,
            "POST",
            f"/tickets/{ticket_id}/assign",
            body.model_dump(mode="json"),
            repo.op_assign(ticket_id, body.team.value, body.assignee, body.expected_version),
        )

    @app.post("/tickets/{ticket_id}/transitions", dependencies=writer_only)
    def transition(
        ticket_id: str, body: TransitionRequest, response: Response, actor: Actor, key: IdemKey = None
    ) -> dict[str, Any]:
        return execute(
            response,
            key,
            "POST",
            f"/tickets/{ticket_id}/transitions",
            body.model_dump(mode="json"),
            repo.op_transition(ticket_id, body.to_status, body.expected_version),
        )

    return app
