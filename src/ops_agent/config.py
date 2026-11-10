"""Runtime settings, resolved from environment variables with safe local defaults.

No secrets live in code. The optional write token for the ticket API is read from the
environment only, and only by the components allowed to write (the API and the approval service).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    api_base_url: str
    write_token: str | None
    proposal_ttl_hours: int

    @property
    def tickets_db(self) -> Path:
        return self.data_dir / "tickets.db"

    @property
    def control_db(self) -> Path:
        """Proposals + audit log. Deliberately a separate database from the system of record."""
        return self.data_dir / "control.db"

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "docs"

    @property
    def seed_dir(self) -> Path:
        return self.data_dir / "seed"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.environ.get("OPS_AGENT_DATA_DIR", "data")).resolve(),
            api_base_url=os.environ.get("OPS_AGENT_API_URL", "http://127.0.0.1:8765"),
            write_token=os.environ.get("OPS_AGENT_WRITE_TOKEN") or None,
            proposal_ttl_hours=int(os.environ.get("OPS_AGENT_PROPOSAL_TTL_HOURS", "24")),
        )
