#!/usr/bin/env bash
# Same tasks as the Makefile, for machines without `make`.  Usage: scripts/dev.sh <setup|seed|api|mcp|test|check|eval|smoke>
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
export OPS_AGENT_DATA_DIR="${OPS_AGENT_DATA_DIR:-$PWD/data}"

case "${1:-}" in
  setup)
    if command -v uv >/dev/null; then uv venv -q -p 3.11 .venv && uv pip install -q --python .venv -e ".[dev]";
    else python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"; fi ;;
  seed)  "$PY" -m ops_agent.cli seed --seed "${SEED:-42}" ;;
  api)   "$PY" -m ops_agent.cli serve-api ;;
  mcp)   "$PY" -m ops_agent.cli serve-mcp ;;
  test)  "$PY" -m pytest ;;
  check)
    "$PY" -m ruff check src tests scripts
    "$PY" -m ruff format --check src tests scripts
    "$PY" -m mypy
    "$PY" -m pytest ;;
  eval)  "$PY" -m ops_agent.cli eval --report docs/06-evaluation-report.md --json evals/results/offline-latest.json --gate ;;
  smoke) "$PY" -m ops_agent.cli seed --seed "${SEED:-42}" >/dev/null && "$PY" scripts/smoke_e2e.py ;;
  *) echo "usage: $0 <setup|seed|api|mcp|test|check|eval|smoke>"; exit 2 ;;
esac
