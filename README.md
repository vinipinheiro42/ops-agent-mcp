# Ops Agent with MCP — Northwind Logistics (fictional client)

> **Status: work in progress (M1–M2 of 5).** Portfolio project simulating a Forward Deployed Engineering
> engagement. Northwind Logistics is a **fictional** client and **all data is synthetic**.

An assistant that triages service-desk tickets (category, priority, team, duplicates) and answers internal
policy questions with citations — through a custom **MCP server** where every change is a *proposal*
that only executes after **human approval**, exactly once, with a tamper-evident audit trail.

## What exists today

| Milestone | Scope | Status |
|---|---|---|
| M1 | [Discovery call](docs/01-discovery-call.md), [requirements](docs/02-requirements.md), [architecture + ADRs](docs/03-architecture.md) | ✅ |
| M2 | Seeded synthetic data (200 tickets, 30 KB docs, gold labels) · fake Jira-style API (FastAPI + SQLite) · MCP server (read + proposal tools) · approval CLI with roles, expiry, idempotency, conflict detection, hash-chained audit · tests | ✅ |
| M3 | Agent loop (LLM behind an interface + offline mode), prompt-injection defence, tracing | planned |
| M4 | Evaluation: ~50 cases, rules baseline, accuracy, tool calls, unsupported answers, cost, p50/p95 latency | planned |
| M5 | Docker/compose, CI, deployment runbook, simulated incident post-mortem, final README | planned |

## Quick start

```bash
scripts/dev.sh setup      # or: make setup   (Python 3.11+, uses uv if available)
scripts/dev.sh seed       # writes ./data: tickets.db, docs/KB-*.md, seed/gold.jsonl, approvers.json
scripts/dev.sh check      # ruff + mypy --strict + pytest (offline, no API key)
scripts/dev.sh smoke      # end-to-end: uvicorn API + MCP over stdio + approval CLI
```

Manual walk-through:

```bash
export OPS_AGENT_DATA_DIR=$PWD/data
.venv/bin/ops-agent serve-api &                       # fake service desk on :8765
.venv/bin/ops-agent serve-mcp                         # MCP server (stdio) — point any MCP host at this
.venv/bin/ops-agent proposals list                    # pending proposals created by the agent
.venv/bin/ops-agent approve prop_… --actor maya.lindqvist   # human step (support_lead)
.venv/bin/ops-agent audit --verify
```

## Safety model in one paragraph

The MCP server holds no write credential and its HTTP client refuses any non-GET request. `propose_*`
tools validate the change against the current ticket and store a pending proposal. A separate approval
service — run by a human from the roster, never the requester — claims the proposal with a
compare-and-swap, writes with `Idempotency-Key = proposal id` and `expected_version`, and records each step
in an append-only, hash-chained audit log. P1 escalations and closures require a support lead.

## Layout

```
docs/                 M1 documents (discovery, requirements, architecture)
src/ops_agent/
  seed/               deterministic generator: catalog (world + templates), corpus (KB), generator
  ticketing/          fake service-desk API: models, SQLite repository, FastAPI app
  knowledge/          BM25 + markdown knowledge store with section citations
  mcp_server/         MCP server (official `mcp` SDK v2) + read-only ticket client
  approvals/          proposals store, policy/roster, approval service, ticket writer
  cli.py              ops-agent seed | serve-api | serve-mcp | proposals | approve | reject | audit
scripts/              dev.sh (Makefile equivalent), smoke_e2e.py
tests/                pytest: generator, API, knowledge, MCP tools, approvals, CLI
```

License: MIT. No real company, customer or employee data is used anywhere in this repository.
