"""Provider-neutral conversation types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    # Structured tool output, kept alongside the text the model sees. Real providers ignore it; the
    # offline provider reads it instead of re-parsing text.
    data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False  # True when counted with the chars/4 heuristic (offline provider)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
