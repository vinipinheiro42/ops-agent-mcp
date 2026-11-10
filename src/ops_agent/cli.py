"""Command-line entry point: `ops-agent <command>`.

seed                         generate the synthetic dataset and load the ticket database
serve-api                    run the fake ticket API (uvicorn)
serve-mcp                    run the MCP server over stdio (expects the API to be running)
proposals list|show          inspect proposals
approve <id> --actor NAME    approve and apply a proposal (human step)
reject <id> --actor NAME     reject a proposal
audit [--verify]             print or verify the hash-chained audit log
triage <id> | --all-open     run the triage agent; it only creates pending proposals
ask "question"               cited answer from verified knowledge-base pages (or an abstention)
eval [--gate]                offline evaluation: agent vs. baseline vs. red-team, markdown + JSON report

Agent commands talk to the MCP server over stdio (child process; the API must be running), or with
--sandbox to a fresh in-process copy of the synthetic environment (no servers needed).
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


# ---- agent ----------------------------------------------------------------------------------------


def _print_triage(r: object) -> None:
    from ops_agent.agent.loop import TriageResult

    assert isinstance(r, TriageResult)
    d = r.decision
    summary = (
        "no decision"
        if d is None
        else (
            f"{d.action} category={d.category.value if d.category else '-'} "
            f"priority={d.priority.value if d.priority else '-'} team={d.team.value if d.team else '-'}"
            + (f" duplicate_of={d.duplicate_of}" if d.duplicate_of else "")
        )
    )
    print(f"{r.ticket_id}: {r.status} | {summary}")
    for f in r.flags:
        print(f"    flag: {f}")
    for b in r.blocked:
        print(f"    blocked: {b}")
    for p in r.proposals:
        print(f"    proposal {p.get('proposal_id', p.get('id', '?'))}: {p.get('action')} ({p.get('status')})")
    for e in r.proposal_errors:
        print(f"    proposal error: {e}")


async def _agent_session(args: argparse.Namespace, settings: Settings, work: object) -> int:
    from collections.abc import Awaitable, Callable

    from mcp import Client

    from ops_agent.agent.loop import Agent
    from ops_agent.agent.providers import make_model
    from ops_agent.agent.tracing import Pricing
    from ops_agent.runtime import sandbox, stdio_params

    assert callable(work)
    job: Callable[[Agent, Client], Awaitable[int]] = work
    trace_dir = Path(args.trace_dir) if args.trace_dir else settings.data_dir / "traces"
    model = make_model(args.provider)
    if args.sandbox:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="ops-agent-sandbox-") as tmp:
            async with sandbox(Path(tmp)) as sb:
                agent = Agent(sb.mcp, model, trace_dir=trace_dir, pricing=Pricing.from_env())
                return await job(agent, sb.mcp)
    async with Client(stdio_params()) as client:
        agent = Agent(client, model, trace_dir=trace_dir, pricing=Pricing.from_env())
        return await job(agent, client)


def cmd_triage(args: argparse.Namespace, settings: Settings) -> int:
    async def job(agent: object, client: object) -> int:
        from mcp import Client

        from ops_agent.agent.loop import Agent

        assert isinstance(agent, Agent) and isinstance(client, Client)
        ids: list[str] = list(args.ids)
        if args.all_open:
            res = await client.call_tool("search_tickets", {"status": "open", "untriaged": True, "limit": 50})
            if res.is_error:
                print(f"search_tickets failed: {res.content}", file=sys.stderr)
                return 1
            data = res.structured_content or {}
            ids += [t["id"] for t in data.get("items", [])][: args.limit]
        if not ids:
            print("nothing to triage (give ticket ids or --all-open)", file=sys.stderr)
            return 1
        for tid in ids:
            _print_triage(await agent.triage(tid))
        if args.sandbox:
            print("\nSandbox run: proposals lived in a temporary copy and were discarded.")
        else:
            print("\nNothing was changed. Review with `ops-agent proposals list`, then approve/reject.")
        return 0

    return asyncio.run(_agent_session(args, settings, job))


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    async def job(agent: object, client: object) -> int:
        from ops_agent.agent.loop import Agent

        assert isinstance(agent, Agent)
        r = await agent.answer(args.question)
        if r.answer.abstain:
            print("I could not find a supported answer in the verified knowledge base.")
            if r.answer.answer:
                print(r.answer.answer)
        else:
            print(r.answer.answer)
            print("Sources: " + ", ".join(r.answer.citations))
        return 0

    return asyncio.run(_agent_session(args, settings, job))


def cmd_eval(args: argparse.Namespace, settings: Settings) -> int:
    import tempfile

    from ops_agent.evals.report import check_gate, render_markdown, results_json
    from ops_agent.evals.runner import run_eval

    with tempfile.TemporaryDirectory(prefix="ops-agent-eval-") as tmp:
        trace_dir = Path(args.trace_dir) if args.trace_dir else None
        res = asyncio.run(run_eval(Path(tmp), provider=args.provider, trace_dir=trace_dir))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(render_markdown(res))
        print(f"report: {args.report}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(results_json(res), indent=1, default=str) + "\n")
        print(f"results: {args.json}")
    a, b = res.metrics["agent"], res.metrics["baseline"]
    for key in (
        "category_accuracy",
        "priority_accuracy",
        "team_accuracy",
        "duplicate_precision",
        "injection_cases_with_violation",
        "qa_accuracy",
        "qa_abstention_correct",
    ):
        print(f"{key:<34} agent={a[key]!s:<8} baseline={b[key]!s}")
    print(f"{'redteam injection violations':<34} {res.metrics['redteam']['injection_cases_with_violation']}")
    print(f"{'unapproved writes':<34} {res.unapproved_writes}")
    if args.gate:
        problems = check_gate(res, Path(args.thresholds))
        if problems:
            print("GATE FAILED:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 3
        print("gate: PASS")
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

    def agent_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--provider", help="offline (default) | anthropic | openai; or OPS_AGENT_PROVIDER")
        sp.add_argument("--sandbox", action="store_true", help="use a fresh in-process synthetic environment")
        sp.add_argument("--trace-dir", help="JSONL trace directory (default: <data>/traces)")

    s = sub.add_parser("triage", help="triage tickets (creates pending proposals only)")
    s.add_argument("ids", nargs="*")
    s.add_argument("--all-open", action="store_true", help="every new, untriaged ticket")
    s.add_argument("--limit", type=int, default=50)
    agent_opts(s)
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("ask", help="cited answer from the verified knowledge base")
    s.add_argument("question")
    agent_opts(s)
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("eval", help="run the offline evaluation")
    s.add_argument("--provider")
    s.add_argument("--report", help="write the markdown report here")
    s.add_argument("--json", help="write per-case JSON results here")
    s.add_argument("--trace-dir")
    s.add_argument("--gate", action="store_true", help="exit 3 if a threshold in --thresholds fails")
    s.add_argument("--thresholds", default="evals/thresholds.json")
    s.set_defaults(func=cmd_eval)
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
