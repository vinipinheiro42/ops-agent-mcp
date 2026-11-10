"""OpenAI-compatible Chat Completions provider (raw HTTP). Optional; never used in tests/CI."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ops_agent.agent.messages import Message, ModelResponse, ToolCall, ToolSpec, Usage

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIModel:
    def __init__(
        self, api_key: str, model: str, base_url: str | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        self.name = f"openai:{model}"
        self._model = model
        self._key = api_key
        self._url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
        self._http = http or httpx.AsyncClient(timeout=60)

    @staticmethod
    def to_wire(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in m.tool_calls
                    ]
                out.append(msg)
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        return out

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        body = {
            "model": self._model,
            "messages": self.to_wire(system, messages),
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in tools
            ],
        }
        resp = await self._http.post(self._url, headers={"Authorization": f"Bearer {self._key}"}, json=body)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": c["function"].get("arguments")}
            calls.append(ToolCall(id=c["id"], name=c["function"]["name"], arguments=args))
        usage = data.get("usage", {})
        return ModelResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))),
            model=self.name,
        )
