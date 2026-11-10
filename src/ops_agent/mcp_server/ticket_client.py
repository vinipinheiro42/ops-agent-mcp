"""Read-only client for the ticket API, used by the MCP server.

Defence in depth for "the agent cannot write": this client has no write methods, carries no write
credential, and a request hook refuses any HTTP method other than GET/HEAD before it leaves the process.
"""

from __future__ import annotations

from typing import Any

import httpx

from ops_agent.ticketing.models import TicketPage, TicketWithComments

READ_METHODS = frozenset({"GET", "HEAD"})


class ReadOnlyViolationError(RuntimeError):
    pass


class TicketNotFoundError(LookupError):
    pass


async def _enforce_read_only(request: httpx.Request) -> None:
    if request.method.upper() not in READ_METHODS:
        raise ReadOnlyViolationError(f"read-only client refused {request.method} {request.url.path}")


def make_read_only_http(
    base_url: str, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        timeout=httpx.Timeout(10.0),
        event_hooks={"request": [_enforce_read_only]},
        headers={"User-Agent": "ops-agent-mcp/0.2 (read-only)"},
    )


class ReadOnlyTicketClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        hooks = http.event_hooks.get("request", [])
        if _enforce_read_only not in hooks:
            raise ValueError("ReadOnlyTicketClient requires a client built with make_read_only_http()")
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def search(self, **params: Any) -> TicketPage:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._http.get("/tickets", params=clean)
        resp.raise_for_status()
        return TicketPage.model_validate(resp.json())

    async def get(self, ticket_id: str) -> TicketWithComments:
        resp = await self._http.get(f"/tickets/{ticket_id}")
        if resp.status_code == 404:
            raise TicketNotFoundError(ticket_id)
        resp.raise_for_status()
        return TicketWithComments.model_validate(resp.json())

    async def iter_all(self, page_size: int = 500, **params: Any) -> list[TicketPage]:
        pages: list[TicketPage] = []
        offset = 0
        while True:
            page = await self.search(limit=page_size, offset=offset, **params)
            pages.append(page)
            offset += page_size
            if offset >= page.total:
                return pages
