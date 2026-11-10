"""Structured tracing: one JSONL file per agent run.

Event types: run_start, model_call, tool_call, guard, proposal, run_end. Each line carries run_id,
seq and a monotonic offset in ms, so traces can be replayed, diffed and aggregated by the evaluator.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Pricing:
    """USD per million tokens. Defaults are 0 (offline). Set real prices via env; we do not hard-code them."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    @classmethod
    def from_env(cls) -> Pricing:
        return cls(
            float(os.environ.get("OPS_AGENT_PRICE_INPUT_PER_MTOK", "0")),
            float(os.environ.get("OPS_AGENT_PRICE_OUTPUT_PER_MTOK", "0")),
        )

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1_000_000


@dataclass(slots=True)
class RunStats:
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_estimated: bool = False
    cost_usd: float = 0.0
    model_ms: float = 0.0
    tool_ms: float = 0.0
    tools_used: list[str] = field(default_factory=list)


class Tracer:
    def __init__(self, trace_dir: Path | None, kind: str, subject: str, model: str, pricing: Pricing) -> None:
        self.run_id = f"run_{time.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}"
        self.pricing = pricing
        self.stats = RunStats()
        self._t0 = time.perf_counter()
        self._seq = 0
        self.path = (trace_dir / f"{self.run_id}.jsonl") if trace_dir else None
        self.events: list[dict[str, Any]] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.emit("run_start", kind=kind, subject=subject, model=model)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def emit(self, event: str, **data: Any) -> None:
        self._seq += 1
        row = {
            "run_id": self.run_id,
            "seq": self._seq,
            "t_ms": round(self.elapsed_ms(), 3),
            "event": event,
            **data,
        }
        self.events.append(row)
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(row, default=str, sort_keys=True) + "\n")

    def model_call(
        self, model: str, input_tokens: int, output_tokens: int, estimated: bool, ms: float, n_tool_calls: int
    ) -> None:
        cost = self.pricing.cost(input_tokens, output_tokens)
        s = self.stats
        s.model_calls += 1
        s.input_tokens += input_tokens
        s.output_tokens += output_tokens
        s.tokens_estimated = s.tokens_estimated or estimated
        s.cost_usd += cost
        s.model_ms += ms
        self.emit(
            "model_call",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_estimated=estimated,
            latency_ms=round(ms, 3),
            cost_usd=round(cost, 6),
            tool_calls_requested=n_tool_calls,
        )

    def tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        ms: float,
        result_chars: int,
        blocked: str | None = None,
    ) -> None:
        self.stats.tool_calls += 1
        self.stats.tool_ms += ms
        self.stats.tools_used.append(name)
        self.emit(
            "tool_call",
            tool=name,
            arguments=arguments,
            ok=ok,
            latency_ms=round(ms, 3),
            result_chars=result_chars,
            blocked=blocked,
        )

    def end(self, status: str, **data: Any) -> None:
        s = self.stats
        self.emit(
            "run_end",
            status=status,
            latency_ms=round(self.elapsed_ms(), 3),
            model_calls=s.model_calls,
            tool_calls=s.tool_calls,
            input_tokens=s.input_tokens,
            output_tokens=s.output_tokens,
            tokens_estimated=s.tokens_estimated,
            cost_usd=round(s.cost_usd, 6),
            **data,
        )
