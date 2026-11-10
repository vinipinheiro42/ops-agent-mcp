# Ops Agent with MCP - developer tasks. Requires uv (https://docs.astral.sh/uv/) or falls back to pip.
PY      ?= .venv/bin/python
DATA    ?= data
export OPS_AGENT_DATA_DIR := $(abspath $(DATA))

.PHONY: setup seed api mcp test lint typecheck check eval smoke clean

setup:            ## create .venv and install the package with dev tools
	@command -v uv >/dev/null && uv venv -q -p 3.11 .venv && uv pip install -q --python .venv -e ".[dev]" \
	  || (python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]")

seed:             ## generate synthetic tickets + docs (seed 42) and load the ticket DB
	$(PY) -m ops_agent.cli seed --seed $${SEED:-42}

api:              ## run the fake ticket API on :8765
	$(PY) -m ops_agent.cli serve-api

mcp:              ## run the MCP server on stdio (API must be running)
	$(PY) -m ops_agent.cli serve-mcp

test:             ## unit + integration tests (offline, no API key needed)
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests scripts && $(PY) -m ruff format --check src tests scripts

typecheck:
	$(PY) -m mypy

check: lint typecheck test

eval:             ## offline evaluation: agent vs baseline vs red team; writes the report; fails below thresholds
	$(PY) -m ops_agent.cli eval --report docs/06-evaluation-report.md --json evals/results/offline-latest.json --gate

smoke: seed       ## end-to-end over real processes: uvicorn + MCP stdio + approval CLI
	$(PY) scripts/smoke_e2e.py

clean:
	rm -rf $(DATA) .pytest_cache .mypy_cache .ruff_cache
