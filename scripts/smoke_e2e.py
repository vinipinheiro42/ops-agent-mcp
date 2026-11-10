"""End-to-end smoke test over real processes: uvicorn API + MCP server on stdio + approval CLI.

Usage: python scripts/smoke_e2e.py   (expects `ops-agent seed` to have run for $OPS_AGENT_DATA_DIR)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import httpx
from mcp import Client, StdioServerParameters

PORT = int(os.environ.get("SMOKE_PORT", "8799"))
URL = f"http://127.0.0.1:{PORT}"


def main() -> int:
    env = {**os.environ, "OPS_AGENT_API_URL": URL}
    api = subprocess.Popen(
        [sys.executable, "-m", "ops_agent.cli", "serve-api", "--port", str(PORT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{URL}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            print("API did not start", file=sys.stderr)
            return 1
        pid = asyncio.run(_agent_side(env))
        before = httpx.get(f"{URL}/tickets/NW-1051").json()
        out = subprocess.run(
            [sys.executable, "-m", "ops_agent.cli", "approve", pid, "--actor", "maya.lindqvist"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        print("approve:", out.stdout.strip())
        after = httpx.get(f"{URL}/tickets/NW-1051").json()
        print(
            f"NW-1051 priority {before['priority']} -> {after['priority']}, version {before['version']} -> "
            f"{after['version']}"
        )
        again = subprocess.run(
            [sys.executable, "-m", "ops_agent.cli", "approve", pid, "--actor", "maya.lindqvist"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        print("approve again:", again.stdout.strip())
        audit = subprocess.run(
            [sys.executable, "-m", "ops_agent.cli", "audit", "--verify"],
            env=env,
            capture_output=True,
            text=True,
        )
        print(audit.stdout.strip())
        ok = after["priority"] == "P1" and after["version"] == before["version"] + 1 and audit.returncode == 0
        print("SMOKE OK" if ok else "SMOKE FAILED")
        return 0 if ok else 1
    finally:
        api.terminate()
        api.wait(timeout=5)


async def _agent_side(env: dict[str, str]) -> str:
    params = StdioServerParameters(command=sys.executable, args=["-m", "ops_agent.cli", "serve-mcp"], env=env)
    async with Client(params) as client:
        tools = [t.name for t in (await client.list_tools()).tools]
        print("MCP tools:", ", ".join(sorted(tools)))
        hits = await client.call_tool("search_docs", {"query": "site network outage priority", "limit": 2})
        print("search_docs:", [h["citation"] for h in (hits.structured_content or {})["hits"]])
        r = await client.call_tool(
            "propose_ticket_update",
            {
                "ticket_id": "NW-1051",
                "priority": "P1",
                "category": "network",
                "team": "network_ops",
                "rationale": "Whole site WAN down, docks stopped (KB-001#p1-critical, KB-016)",
                "citations": ["KB-001#p1-critical", "KB-016#symptoms"],
            },
        )
        receipt = r.structured_content or {}
        print(
            "proposal:",
            receipt.get("proposal_id"),
            receipt.get("status"),
            "needs",
            receipt.get("requires_role"),
        )
        return str(receipt["proposal_id"])


if __name__ == "__main__":
    raise SystemExit(main())
