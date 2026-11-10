"""The single seam between the agent loop and any language model."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ops_agent.agent.messages import Message, ModelResponse, ToolSpec


class ProviderConfigError(RuntimeError):
    pass


@runtime_checkable
class ModelClient(Protocol):
    name: str

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        """Return either tool calls or a final text. Must not have side effects beyond the HTTP call."""
        ...


def estimate_tokens(text: str) -> int:
    """Rough chars/4 estimate, used only where a provider does not report usage."""
    return max(1, len(text) // 4) if text else 0
