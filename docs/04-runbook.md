# 04 — Runbook: Northwind Ops Agent (pilot)

> Fictional client, synthetic data. This runbook describes how the pilot **would** be operated. The Docker
> image and compose file in this repository were written but **not built or run** (the authoring machine
> has no Docker); every command below that does not involve Docker was executed locally.

## 1. What runs where

| Component | Process | Holds a write credential? | State |
|---|---|---|---|
| Ticket API (fake service desk; in production: the client's service desk) | `ops-agent serve-api` | validates it | `tickets.db` |
| Agent (triage / ask) | `ops-agent triage …`, `ops-agent ask …` — starts the MCP server as a **stdio child process** | **no** | writes proposals to `control.db`, traces to `traces/` |
| MCP server | `ops-agent serve-mcp` (child of the agent, or any MCP host) | **no** — its HTTP client refuses non-GET | reads `docs/`, writes `control.db` proposals |
| Approver | `ops-agent proposals list`, `approve`, `reject`, `audit --verify` (a human) | **yes** | `control.db` |

The single most important operational invariant: **`OPS_AGENT_WRITE_TOKEN` is only ever present in the API
and the approver.** If it shows up in the agent's environment, treat it as a security incident (§7).

## 2. Deploy

### Local (no Docker) — verified

```bash
scripts/dev.sh setup          # or: make setup   (uv venv + uv pip install -e ".[dev]")
scripts/dev.sh seed           # synthetic data, seed 42 -> ./data
scripts/dev.sh api &          # http://127.0.0.1:8765
.venv/bin/ops-agent triage NW-1052            # agent over MCP stdio -> pending proposals
.venv/bin/ops-agent proposals list
.venv/bin/ops-agent approve <id> --actor maya.lindqvist
scripts/dev.sh smoke          # end-to-end check over real processes
```

### Docker compose — written, not built

```bash
cp .env.example .env && sed -i "s/change-me.*/$(openssl rand -hex 32)/" .env
docker compose build
docker compose up -d api                       # one-shot `seed` runs first, only if /data is empty
docker compose run --rm agent                  # triage --all-open
docker compose run --rm approver proposals list
docker compose run --rm approver approve <id> --actor <name>
```

Image: single `python:3.11-slim` image, non-root user (uid 10001), dependencies pinned by `uv.lock`
(`uv sync --frozen`), role chosen by the command. Tag images with the git SHA, never deploy `:latest`.

### Pilot rollout gates (from 02-requirements §10)

1. CI green, including `ops-agent eval --gate` (offline floors in `evals/thresholds.json`).
2. **Real-model evaluation run and reviewed** (`OPS_AGENT_PROVIDER=… ops-agent eval`) — *not done yet in this
   repository; required before any shadow traffic.*
3. Shadow mode: agent runs on new tickets, proposals visible to the support lead only, nobody approves.
4. Assisted mode: L1 approves proposals; support lead approves P1 and close.

## 3. Configuration

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `OPS_AGENT_DATA_DIR` | all | `./data` (`/data` in the image) | tickets.db, control.db, docs/, seed/, traces/ |
| `OPS_AGENT_API_URL` | agent, MCP, approver | `http://127.0.0.1:8765` | |
| `OPS_AGENT_WRITE_TOKEN` | API, approver **only** | unset (API then accepts unauthenticated writes — local dev only) | secret |
| `OPS_AGENT_PROPOSAL_TTL_HOURS` | MCP, approver | `24` | pending proposals expire after this |
| `OPS_AGENT_PROVIDER` | agent | `offline` | `offline` \| `anthropic` \| `openai` |
| `OPS_AGENT_MODEL` | agent | — | required for real providers |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | agent | — | secret; only for the chosen provider |
| `OPENAI_BASE_URL` | agent | `https://api.openai.com/v1` | any OpenAI-compatible endpoint (e.g. a private gateway) |
| `OPS_AGENT_PRICE_INPUT_PER_MTOK` / `…_OUTPUT_PER_MTOK` | agent | `0` | USD per million tokens, for cost in traces |
| `data/approvers.json` | approver | 3 fictional people | name → role; in production this comes from SSO groups |

Policy knobs that live in code (change via PR + eval run): P1 breaker `limit=3 / 1 h`
(`agent/guard.py`), max 5 pending proposals per ticket, duplicate threshold and 12 h window
(`agent/providers/offline.py`), max 6 model steps per run (`agent/loop.py`).

## 4. Secrets

- No secrets in code, images, or the repository. `.env` is git-ignored; `.env.example` holds placeholders.
- Write token: generate with `openssl rand -hex 32`; give it to the API and the approver only.
  In production use the platform's secret store (and a service-desk integration user with the minimum
  scopes: edit fields, comment, transition — no admin, no delete).
- Model API key: agent only. Prefer a gateway that enforces data-retention terms agreed with Security.
- Rotation: write token — set the new value on the API and approver, restart both, nothing else changes.
  Model key — rotate at the provider, restart the agent job.
- The test suite uses only obviously fake tokens (`test-only-token`, `sandbox-only-token`) against
  in-process apps.

## 5. Rollback

| Situation | Action | Data impact |
|---|---|---|
| Bad agent release (prompt, rules, model) | Stop the agent job (`docker compose stop agent` / disable the scheduler); redeploy the previous image tag | none — the agent never writes tickets |
| Burst of bad proposals | Stop the agent; `ops-agent proposals list` and `reject … --reason "rollback <release>"`; untouched ones expire after 24 h | none until someone approves |
| A bad proposal was **approved** | The audit log shows the before/after (`ops-agent audit --proposal <id>`); revert the field in the service desk by hand or by a new proposal | one ticket per approval; history kept |
| Model provider outage | Switch `OPS_AGENT_PROVIDER=offline` (rules mode) or pause the agent; humans triage as before | none |
| Schema/DB issue in `control.db` | Restore from the last backup; verify with `ops-agent audit --verify` | proposals since the backup are lost; tickets unaffected |

Back up `control.db` daily (SQLite online backup, e.g. `sqlite3 control.db ".backup control-$(date +%F).db"`).

## 6. Monitoring

Every agent run writes one JSONL trace (`traces/run_*.jsonl`) with `run_start`, `model_call` (tokens,
latency, cost), `tool_call` (tool, ok, latency, blocked), `guard` (signals, blocked rules, flags),
`proposal`, `run_end`. Ship them to the log platform and alert on:

| Signal | Source | Alert when |
|---|---|---|
| P1 proposals | `guard` / `proposal` events | > 3 per hour (breaker) — page the support lead to look at the queue |
| `p1_rate_limited` / `p1_without_impact_evidence` | `guard.blocked` | any — review the ticket; possible injection campaign |
| `possible_prompt_injection` flags | `guard.flags` | > 2 per day, or the same text on several tickets |
| `tool_not_allowed`, `canary_leak` | `guard` | any — model tried something outside its allowlist |
| Run errors / `invalid_decision` | `run_end.status` | > 5% of runs in an hour |
| Latency | `run_end.latency_ms` | p95 > 20 s |
| Cost | `run_end.cost_usd` | daily total > budget (target ≤ US$ 0.02 per ticket) |
| Pending queue | `ops-agent proposals list --status pending` | > 50 pending, or oldest > 12 h (risk of mass expiry) |
| Acceptance rate | audit log (`approved` vs `rejected`) | < 60% (agent is noisy) or > 95% (rubber-stamping) |
| Audit integrity | `ops-agent audit --verify` (daily job) | any failure — security incident |

## 7. On-call

**Owner:** Service Desk Platform team (agent, MCP server), Support lead (approval queue). Business hours
only during the pilot; the agent is non-critical by design — if it stops, humans triage as today.

| Symptom | First checks | Mitigation |
|---|---|---|
| Many P1 proposals / breaker tripping | `jq 'select(.event=="guard")' traces/*.jsonl`; look for shared text in the tickets | Stop the agent; reject the burst; see post-mortem INC-SIM-001 |
| Approve ends in status `conflict` | Ticket changed after the proposal (version mismatch) | Expected; reject and re-run triage on that ticket |
| Approve fails with `PermissionDeniedError` | `data/approvers.json` / SSO group | Ask a support lead (P1 and close need `support_lead`) |
| `audit --verify` reports BROKEN | Someone edited `control.db` | Treat as security incident: freeze approvals, preserve the file, restore from backup |
| Agent cannot reach the API | `curl $OPS_AGENT_API_URL/health` | Restart the API; the agent is safe to re-run (proposals are de-duplicated) |
| Real provider 401/429 | provider status page, key validity | Rotate key / lower rate; fall back to `offline` |
| Write token found in agent env | `docker compose config`, job definition | Rotate the token immediately; remove it from the agent; review the audit log |
