from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer

from ops_agent.approvals.policy import DEFAULT_ROSTER, Roster
from ops_agent.approvals.service import ApprovalService
from ops_agent.approvals.store import ProposalStore
from ops_agent.approvals.writer import TicketWriter
from ops_agent.knowledge.store import DocStore
from ops_agent.mcp_server.server import ServerDeps, build_server
from ops_agent.mcp_server.ticket_client import ReadOnlyTicketClient, make_read_only_http
from ops_agent.seed.generator import Dataset, GenerationConfig, generate, write_dataset
from ops_agent.ticketing.api import create_app
from ops_agent.ticketing.db import TicketRepository

WRITE_TOKEN = "test-only-token"  # not a secret: used only against the in-process test app
BASE = "http://northwind.test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return generate(GenerationConfig(seed=42))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def data_dir(tmp_path: Path, dataset: Dataset) -> Path:
    write_dataset(dataset, tmp_path, seed=42)
    return tmp_path


@pytest.fixture
def repo(data_dir: Path, dataset: Dataset, clock: FakeClock) -> TicketRepository:
    r = TicketRepository(data_dir / "tickets.db", clock=clock)
    r.reset_and_load(dataset.tickets)
    return r


@pytest.fixture
def app(repo: TicketRepository) -> Any:
    return create_app(repo, write_token=WRITE_TOKEN)


@pytest.fixture
async def api(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        yield c


WRITE_HEADERS = {"Authorization": f"Bearer {WRITE_TOKEN}", "X-Actor": "tester"}


@pytest.fixture
def store(data_dir: Path, clock: FakeClock) -> ProposalStore:
    return ProposalStore(data_dir / "control.db", clock=clock, ttl=timedelta(hours=24))


@pytest.fixture
def docs(data_dir: Path) -> DocStore:
    return DocStore.from_dir(data_dir / "docs")


@pytest.fixture
async def read_client(app: Any) -> AsyncIterator[ReadOnlyTicketClient]:
    http = make_read_only_http(BASE, transport=httpx.ASGITransport(app=app))
    async with http:
        yield ReadOnlyTicketClient(http)


@pytest.fixture
def server(read_client: ReadOnlyTicketClient, docs: DocStore, store: ProposalStore) -> MCPServer:
    return build_server(ServerDeps(tickets=read_client, docs=docs, proposals=store))


@pytest.fixture
async def mcp_client(server: MCPServer) -> AsyncIterator[Client]:
    async with Client(server) as c:
        yield c


@pytest.fixture
def roster() -> Roster:
    return Roster.from_mapping(DEFAULT_ROSTER)


@pytest.fixture
async def service(app: Any, store: ProposalStore, roster: Roster) -> AsyncIterator[ApprovalService]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        yield ApprovalService(store, TicketWriter(http, WRITE_TOKEN), roster)


def db_fingerprint(db_path: Path) -> str:
    """Hash of every row in the system-of-record tables."""
    conn = sqlite3.connect(db_path)
    try:
        h = hashlib.sha256()
        for table in ("tickets", "comments", "idempotency_keys"):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
                h.update(repr(row).encode())
        return h.hexdigest()
    finally:
        conn.close()


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] | None
    text: str


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> ToolResult:
    r = await client.call_tool(name, args or {})
    text = " ".join(getattr(c, "text", "") for c in r.content)
    return ToolResult(ok=not r.is_error, data=r.structured_content, text=text)


def first_open_ticket(dataset: Dataset, kind: str = "base") -> str:
    for g, t in zip(dataset.gold, dataset.tickets, strict=True):
        if g["kind"] == kind and t.status.value == "open":
            return t.id
    raise AssertionError("no such ticket")
