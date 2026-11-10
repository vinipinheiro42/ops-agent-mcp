"""Command-line entry point: `ops-agent <command>`.

seed                         generate the synthetic dataset and load the ticket database
serve-api                    run the fake ticket API (uvicorn)
serve-mcp                    run the MCP server over stdio (expects the API to be running)
proposals list|show          inspect proposals
approve <id> --actor NAME    approve and apply a proposal (human step)
reject <id> --actor NAME     reject a proposal
audit [--verify]             print or verify the hash-chained audit log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import httpx

from ops_agent.approvals.models import ProposalStatus
from ops_agent.approvals.policy import DEFAULT_ROSTER, Roster
from ops_agent.approvals.service import ApprovalError, ApprovalService
from ops_agent.approvals.store import ProposalStore
from ops_agent.approvals.writer import TicketWriter
from ops_agent.config import Settings


def _store(settings: Settings) -> ProposalStore:
    return ProposalStore(settings.control_db, ttl=timedelta(hours=settings.proposal_ttl_hours))


def _roster_path(settings: Settings) -> Path:
    return settings.data_dir / "approvers.json"


def cmd_seed(args: argparse.Namespace, settings: Settings) -> int:
    from ops_agent.seed.generator import GenerationConfig, generate, write_dataset
    from ops_agent.ticketing.db import TicketRepository

    dataset = generate(GenerationConfig(seed=args.seed))
    manifest = write_dataset(dataset, settings.data_dir, args.seed)
    TicketRepository(settings.tickets_db).reset_and_load(dataset.tickets)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.control_db}{suffix}").unlink(missing_ok=True)
    _store(settings)
    _roster_path(settings).write_text(json.dumps(DEFAULT_ROSTER, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


def cmd_serve_api(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from ops_agent.ticketing.api import create_app
    from ops_agent.ticketing.db import TicketRepository

    app = create_app(TicketRepository(settings.tickets_db), write_token=settings.write_token)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_serve_mcp(args: argparse.Namespace, settings: Settings) -> int:
    from ops_agent.knowledge.store import DocStore
    from ops_agent.mcp_server.server import ServerDeps, build_server
    from ops_agent.mcp_server.ticket_client import ReadOnlyTicketClient, make_read_only_http

    deps = ServerDeps(
        tickets=ReadOnlyTicketClient(make_read_only_http(settings.api_base_url)),
        docs=DocStore.from_dir(settings.docs_dir),
        proposals=_store(settings),
    )
    build_server(deps).run("stdio")
    return 0


def cmd_proposals(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    if args.action == "show":
        p = store.get(args.id)
        if p is None:
            print(f"not found: {args.id}", file=sys.stderr)
            return 1
        print(p.model_dump_json(indent=2))
        return 0
    status = None if args.status == "all" else ProposalStatus(args.status)
    for p in store.list_proposals(status=status, limit=args.limit):
        print(
            f"{p.id}  {p.status.value:<9} {p.ticket_id:<8} {p.action.value:<14} "
            f"needs={p.requires_role.value:<13} {json.dumps(p.payload)[:70]}"
        )
    return 0


async def _approve(args: argparse.Namespace, settings: Settings) -> int:
    async with httpx.AsyncClient(base_url=settings.api_base_url, timeout=10) as http:
        service = ApprovalService(
            _store(settings),
            TicketWriter(http, settings.write_token),
            Roster.from_file(_roster_path(settings)),
        )
        outcome = await service.approve(args.id, actor=args.actor, note=args.note)
    note = " (already applied - no-op)" if outcome.already_applied else ""
    print(f"{outcome.proposal.id}: {outcome.proposal.status.value}{note}")
    return 0


def cmd_approve(args: argparse.Namespace, settings: Settings) -> int:
    return asyncio.run(_approve(args, settings))


def cmd_reject(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    service = ApprovalService(
        store,
        TicketWriter(httpx.AsyncClient(base_url=settings.api_base_url)),
        Roster.from_file(_roster_path(settings)),
    )
    p = service.reject(args.id, actor=args.actor, reason=args.reason)
    print(f"{p.id}: {p.status.value}")
    return 0


def cmd_audit(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    if args.verify:
        ok, bad = store.verify_audit_chain()
        print("audit chain OK" if ok else f"audit chain BROKEN at seq {bad}")
        return 0 if ok else 2
    for e in store.audit_events(args.proposal):
        print(
            f"{e.seq:>5} {e.ts} {e.actor:<28} {e.event:<22} {e.proposal_id or '-'} "
            f"{json.dumps(e.detail)[:80]}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("seed", help="generate synthetic data and load the ticket DB")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("serve-api", help="run the fake ticket API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve_api)

    s = sub.add_parser("serve-mcp", help="run the MCP server over stdio")
    s.set_defaults(func=cmd_serve_mcp)

    s = sub.add_parser("proposals", help="inspect proposals")
    s.add_argument("action", choices=["list", "show"])
    s.add_argument("id", nargs="?")
    s.add_argument("--status", default="pending", choices=["all", *[x.value for x in ProposalStatus]])
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_proposals)

    s = sub.add_parser("approve", help="approve and apply a proposal")
    s.add_argument("id")
    s.add_argument("--actor", required=True, help="approver identity (must be in data/approvers.json)")
    s.add_argument("--note")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("reject", help="reject a proposal")
    s.add_argument("id")
    s.add_argument("--actor", required=True)
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_reject)

    s = sub.add_parser("audit", help="show or verify the audit log")
    s.add_argument("--proposal")
    s.add_argument("--verify", action="store_true")
    s.set_defaults(func=cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    try:
        return int(args.func(args, settings))
    except ApprovalError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
