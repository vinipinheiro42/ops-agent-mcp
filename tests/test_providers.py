"""Real-provider adapters tested against httpx.MockTransport: request shape and response parsing.
No network, no keys."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ops_agent.agent.messages import Message, ToolCall, ToolSpec
from ops_agent.agent.providers import ProviderConfigError, make_model
from ops_agent.agent.providers.anthropic import AnthropicModel
from ops_agent.agent.providers.offline import OfflineHeuristicModel
from ops_agent.agent.providers.openai import OpenAIModel

pytestmark = pytest.mark.anyio

TOOLS = [
    ToolSpec(
        "get_ticket", "Read a ticket", {"type": "object", "properties": {"ticket_id": {"type": "string"}}}
    )
]
CONVO = [
    Message("user", "Triage NW-1001"),
    Message("assistant", "", tool_calls=[ToolCall("t1", "get_ticket", {"ticket_id": "NW-1001"})]),
    Message("tool", '{"ticket": {}}', tool_call_id="t1", tool_name="get_ticket"),
]


def _mock(reply: dict[str, Any], seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_anthropic_request_and_parse() -> None:
    seen: list[httpx.Request] = []
    reply = {
        "content": [
            {"type": "text", "text": "Reading."},
            {"type": "tool_use", "id": "tu_1", "name": "get_ticket", "input": {"ticket_id": "NW-1002"}},
        ],
        "usage": {"input_tokens": 321, "output_tokens": 12},
    }
    m = AnthropicModel("test-key", "some-model", http=_mock(reply, seen))
    r = await m.complete("SYSTEM", CONVO, TOOLS)
    body = json.loads(seen[0].content)
    assert seen[0].headers["x-api-key"] == "test-key" and seen[0].headers["anthropic-version"]
    assert body["system"] == "SYSTEM" and body["model"] == "some-model"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": '{"ticket": {}}',
    }
    assert r.tool_calls == [ToolCall("tu_1", "get_ticket", {"ticket_id": "NW-1002"})]
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.estimated) == (321, 12, False)


async def test_openai_request_and_parse() -> None:
    seen: list[httpx.Request] = []
    reply = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_ticket", "arguments": '{"ticket_id": "NW-1003"}'},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 400, "completion_tokens": 9},
    }
    m = OpenAIModel("test-key", "some-model", base_url="http://llm.test/v1/", http=_mock(reply, seen))
    r = await m.complete("SYSTEM", CONVO, TOOLS)
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://llm.test/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "get_ticket"
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": '{"ticket": {}}'}
    assert body["tools"][0]["type"] == "function"
    assert r.tool_calls == [ToolCall("call_1", "get_ticket", {"ticket_id": "NW-1003"})]
    assert (r.usage.input_tokens, r.usage.output_tokens) == (400, 9)


def test_make_model_defaults_offline_and_validates_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("OPS_AGENT_PROVIDER", "OPS_AGENT_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert isinstance(make_model(), OfflineHeuristicModel)
    with pytest.raises(ProviderConfigError, match="OPS_AGENT_MODEL"):
        make_model("anthropic")
    monkeypatch.setenv("OPS_AGENT_MODEL", "m")
    with pytest.raises(ProviderConfigError, match="ANTHROPIC_API_KEY"):
        make_model("anthropic")
    with pytest.raises(ProviderConfigError, match="unknown"):
        make_model("nope")
