"""Domain model shared by the ticket API, the MCP server, and the approval service."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Status(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    WAITING_ON_REPORTER = "waiting_on_reporter"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Priority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class Category(StrEnum):
    WMS = "wms"
    TMS_ROUTING = "tms_routing"
    EDI_INTEGRATION = "edi_integration"
    DEVICES = "devices"
    IDENTITY_ACCESS = "identity_access"
    NETWORK = "network"
    CUSTOMER_PORTAL = "customer_portal"
    WORKPLACE = "workplace"
    BILLING = "billing"
    SECURITY = "security"


class Team(StrEnum):
    WAREHOUSE_SYSTEMS = "warehouse_systems"
    TRANSPORT_SYSTEMS = "transport_systems"
    INTEGRATIONS = "integrations"
    FIELD_DEVICES = "field_devices"
    IDENTITY_ACCESS = "identity_access"
    NETWORK_OPS = "network_ops"
    DIGITAL_CHANNELS = "digital_channels"
    WORKPLACE = "workplace"
    FINANCE_SYSTEMS = "finance_systems"
    SECURITY_OPS = "security_ops"


# Allowed workflow transitions (from -> set of to). Mirrors a typical Jira service-desk workflow.
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.OPEN: frozenset({Status.TRIAGED, Status.WAITING_ON_REPORTER, Status.RESOLVED, Status.CLOSED}),
    Status.TRIAGED: frozenset({Status.IN_PROGRESS, Status.WAITING_ON_REPORTER, Status.RESOLVED}),
    Status.IN_PROGRESS: frozenset({Status.WAITING_ON_REPORTER, Status.RESOLVED}),
    Status.WAITING_ON_REPORTER: frozenset({Status.TRIAGED, Status.IN_PROGRESS, Status.RESOLVED}),
    Status.RESOLVED: frozenset({Status.CLOSED, Status.IN_PROGRESS}),
    Status.CLOSED: frozenset(),
}


class Comment(BaseModel):
    id: int
    ticket_id: str
    author: str
    body: str
    internal: bool
    created_at: str


class Ticket(BaseModel):
    id: str
    title: str
    description: str
    reporter: str
    site: str
    status: Status
    priority: Priority | None = None
    category: Category | None = None
    team: Team | None = None
    assignee: str | None = None
    labels: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    created_at: str
    updated_at: str
    version: int = 1


class TicketWithComments(Ticket):
    comments: list[Comment] = Field(default_factory=list)


class TicketPage(BaseModel):
    items: list[Ticket]
    total: int
    limit: int
    offset: int


# ---- write requests -------------------------------------------------------------------------


class _WriteBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = Field(
        default=None, description="Optimistic concurrency: reject with 409 if the ticket changed since."
    )


class TicketPatch(_WriteBase):
    priority: Priority | None = None
    category: Category | None = None
    team: Team | None = None
    labels: list[str] | None = None
    duplicate_of: str | None = None


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=5000)
    internal: bool = True


class AssignRequest(_WriteBase):
    team: Team
    assignee: str | None = None


class TransitionRequest(_WriteBase):
    to_status: Status
