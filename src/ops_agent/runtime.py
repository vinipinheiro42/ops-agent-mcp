"""Wiring helpers: an in-process sandbox (API + MCP server + approvals) for the CLI, evaluation and tests,
and a stdio connection to a separately running MCP server."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp import Client, StdioServerParameters

from ops_agent.approvals.policy import DEFAULT_ROSTER, Roster
from ops_agent.approvals.service import ApprovalService
from ops_agent.approvals.store import Clock, ProposalStore
from ops_agent.approvals.writer import TicketWriter
from ops_agent.knowledge.store import DocStore
from ops_agent.mcp_server.server import ServerDeps, build_server
from ops_agent.mcp_server.ticket_client import ReadOnlyTicketClient, make_read_only_http
from ops_agent.seed.generator import Dataset, GenerationConfig, generate, write_dataset
from ops_agent.ticketing.api import create_app
from ops_agent.ticketing.db import TicketRepository

logging.getLogger("httpx").setLevel(logging.WARNING)

SANDBOX_URL = "http://sandbox.northwind.test"
SANDBOX_TOKEN = "sandbox-only-token"  # not a secret: only valid for the in-process sandbox app


@dataclass(slots=True)
class Sandbox:
    data_dir: Path
    dataset: Dataset
    repo: TicketRepository
    store: ProposalStore
    docs: DocStore
    app: Any
    mcp: Client
    approvals: ApprovalService
    api: httpx.AsyncClient


@asynccontextmanager
async def sandbox(data_dir: Path, seed: int = 42, clock: Clock | None = None) -> AsyncIterator[Sandbox]:
    """Fresh in-process environment; the client still talks to the server over MCP (in-memory transport)."""
    dataset = generate(GenerationConfig(seed=seed))
    write_dataset(dataset, data_dir, seed)
    kwargs: dict[str, Any] = {"clock": clock} if clock else {}
    repo = TicketRepository(data_dir / "tickets.db", **kwargs)
    repo.reset_and_load(dataset.tickets)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{data_dir / 'control.db'}{suffix}").unlink(missing_ok=True)
    store = ProposalStore(data_dir / "control.db", ttl=timedelta(hours=24), **kwargs)
    docs = DocStore.from_dir(data_dir / "docs")
    app = create_app(repo, write_token=SANDBOX_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with (
        make_read_only_http(SANDBOX_URL, transport=transport) as read_http,
        httpx.AsyncClient(transport=transport, base_url=SANDBOX_URL) as write_http,
    ):
        server = build_server(ServerDeps(ReadOnlyTicketClient(read_http), docs, store))
        approvals = ApprovalService(
            store, TicketWriter(write_http, SANDBOX_TOKEN), Roster.from_mapping(DEFAULT_ROSTER)
        )
        async with Client(server) as client:
            yield Sandbox(data_dir, dataset, repo, store, docs, app, client, approvals, write_http)


def stdio_params(env: dict[str, str] | None = None) -> StdioServerParameters:
    """Launch `ops-agent serve-mcp` as a child process (real process boundary)."""
    return StdioServerParameters(
        command=sys.executable, args=["-m", "ops_agent.cli", "serve-mcp"], env={**os.environ, **(env or {})}
    )
