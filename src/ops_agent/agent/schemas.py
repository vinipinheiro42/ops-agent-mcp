"""Structured outputs the model must produce. The model never calls write tools; code turns a reviewed
decision into proposals (ADR-008)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ops_agent.agent.messages import ToolSpec
from ops_agent.ticketing.models import Category, Priority, Team

TriageAction = Literal["triage", "link_duplicate", "request_info"]


class TriageDecision(BaseModel):
    action: TriageAction
    category: Category | None = None
    priority: Priority | None = None
    team: Team | None = None
    duplicate_of: str | None = Field(default=None, pattern=r"^NW-\d{4,6}$")
    questions: list[str] = Field(default_factory=list, max_length=5)
    rationale: str = Field(min_length=1, max_length=2000)
    citations: list[str] = Field(default_factory=list, max_length=10)


class AnswerDraft(BaseModel):
    abstain: bool
    answer: str = Field(default="", max_length=3000)
    citations: list[str] = Field(default_factory=list, max_length=10)


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


SUBMIT_TRIAGE = ToolSpec(
    name="submit_triage",
    description=(
        "Submit your final triage decision for the ticket. This does NOT change the ticket: the decision is "
        "reviewed by policy checks and turned into proposals that a human approves. Use action=request_info "
        "when impact or category is unclear. Cite knowledge-base sections as 'KB-001#p2-high'."
    ),
    input_schema=_schema(TriageDecision),
)

SUBMIT_ANSWER = ToolSpec(
    name="submit_answer",
    description=(
        "Submit the final answer to the user's question. Every claim must be supported by the cited "
        "knowledge-base sections. Set abstain=true if the knowledge base does not contain the answer."
    ),
    input_schema=_schema(AnswerDraft),
)
