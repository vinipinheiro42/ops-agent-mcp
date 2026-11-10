"""Anthropic Messages API provider (raw HTTP, no SDK dependency). Optional; never used in tests/CI."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ops_agent.agent.messages import Message, ModelResponse, ToolCall, ToolSpec, Usage

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicModel:
    def __init__(
        self, api_key: str, model: str, max_tokens: int = 1024, http: httpx.AsyncClient | None = None
    ) -> None:
        self.name = f"anthropic:{model}"
        self._model = model
        self._max_tokens = max_tokens
        self._key = api_key
        self._http = http or httpx.AsyncClient(timeout=60)

    @staticmethod
    def to_wire(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in m.tool_calls
                ]
                out.append({"role": "assistant", "content": blocks})
            elif m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                # Consecutive tool results must be grouped in a single user turn.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": self.to_wire(messages),
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ],
        }
        resp = await self._http.post(
            API_URL,
            headers={
                "x-api-key": self._key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            content=json.dumps(body),
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        calls = [
            ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
            for b in data.get("content", [])
            if b.get("type") == "tool_use"
        ]
        usage = data.get("usage", {})
        return ModelResponse(
            content=text,
            tool_calls=calls,
            usage=Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
            model=self.name,
        )
