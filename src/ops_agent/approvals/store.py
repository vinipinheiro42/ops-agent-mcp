"""Proposal store + append-only, hash-chained audit log (SQLite, separate from the ticket system).

The MCP server can *create* proposals here; only the approval service moves them past `pending`.
State changes use compare-and-swap (`UPDATE ... WHERE status IN (...)`) so two concurrent approvers
cannot both win, and every state change is written to the audit log in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops_agent.approvals.models import AuditEvent, Proposal, ProposalAction, ProposalStatus, Role

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id             TEXT PRIMARY KEY,
    ticket_id      TEXT NOT NULL,
    action         TEXT NOT NULL,
    payload        TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    base_version   INTEGER NOT NULL,
    rationale      TEXT NOT NULL,
    citations      TEXT NOT NULL,
    requested_by   TEXT NOT NULL,
    requires_role  TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    decided_by     TEXT,
    decided_at     TEXT,
    decision_note  TEXT,
    applied_at     TEXT,
    result         TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS ix_proposals_ticket ON proposals(ticket_id);

CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    proposal_id  TEXT,
    actor        TEXT NOT NULL,
    event        TEXT NOT NULL,
    detail       TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

GENESIS = "0" * 64
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _event_hash(prev_hash: str, ts: str, proposal_id: str | None, actor: str, event: str, detail: str) -> str:
    material = "|".join([prev_hash, ts, proposal_id or "", actor, event, detail])
    return hashlib.sha256(material.encode()).hexdigest()


class ProposalStore:
    def __init__(self, db_path: Path, clock: Clock = _utc_now, ttl: timedelta = timedelta(hours=24)) -> None:
        self.db_path = db_path
        self._clock = clock
        self.ttl = ttl
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def now(self) -> datetime:
        return self._clock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---- audit ---------------------------------------------------------------------------------

    def _audit(
        self,
        conn: sqlite3.Connection,
        proposal_id: str | None,
        actor: str,
        event: str,
        detail: dict[str, Any],
    ) -> None:
        row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        ts = iso(self.now())
        detail_json = json.dumps(detail, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO audit_log (ts, proposal_id, actor, event, detail, prev_hash, hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                ts,
                proposal_id,
                actor,
                event,
                detail_json,
                prev,
                _event_hash(prev, ts, proposal_id, actor, event, detail_json),
            ),
        )

    def audit(self, proposal_id: str | None, actor: str, event: str, detail: dict[str, Any]) -> None:
        with self._tx() as conn:
            self._audit(conn, proposal_id, actor, event, detail)

    def audit_events(self, proposal_id: str | None = None) -> list[AuditEvent]:
        conn = self._connect()
        try:
            if proposal_id is None:
                rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE proposal_id = ? ORDER BY seq", (proposal_id,)
                ).fetchall()
        finally:
            conn.close()
        return [AuditEvent(**{**dict(r), "detail": json.loads(r["detail"])}) for r in rows]

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        """Recompute the hash chain. Returns (ok, first_bad_seq)."""
        prev = GENESIS
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        finally:
            conn.close()
        for r in rows:
            expected = _event_hash(prev, r["ts"], r["proposal_id"], r["actor"], r["event"], r["detail"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return False, int(r["seq"])
            prev = r["hash"]
        return True, None

    # ---- proposals -----------------------------------------------------------------------------

    @staticmethod
    def _row(row: sqlite3.Row) -> Proposal:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        data["citations"] = json.loads(data["citations"])
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return Proposal.model_validate(data)

    def create(
        self,
        *,
        ticket_id: str,
        action: ProposalAction,
        payload: dict[str, Any],
        base_version: int,
        rationale: str,
        citations: Iterable[str],
        requested_by: str,
        requires_role: Role,
        max_pending_per_ticket: int = 5,
    ) -> tuple[Proposal, bool]:
        """Create a pending proposal. Returns (proposal, deduplicated)."""
        payload_hash = canonical_hash({"t": ticket_id, "a": action.value, "p": payload})
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM proposals WHERE payload_hash = ? AND status = ?",
                (payload_hash, ProposalStatus.PENDING.value),
            ).fetchone()
            if existing is not None:
                return self._row(existing), True
            pending = conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE ticket_id = ? AND status = ?",
                (ticket_id, ProposalStatus.PENDING.value),
            ).fetchone()[0]
            if pending >= max_pending_per_ticket:
                raise TooManyPendingError(ticket_id)
            now = self.now()
            pid = f"prop_{secrets.token_hex(8)}"
            conn.execute(
                "INSERT INTO proposals (id, ticket_id, action, payload, payload_hash, base_version,"
                " rationale,"
                " citations, requested_by, requires_role, status, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    ticket_id,
                    action.value,
                    json.dumps(payload, sort_keys=True),
                    payload_hash,
                    base_version,
                    rationale,
                    json.dumps(list(citations)),
                    requested_by,
                    requires_role.value,
                    ProposalStatus.PENDING.value,
                    iso(now),
                    iso(now + self.ttl),
                ),
            )
            self._audit(
                conn,
                pid,
                requested_by,
                "proposed",
                {
                    "ticket_id": ticket_id,
                    "action": action.value,
                    "payload": payload,
                    "base_version": base_version,
                    "requires_role": requires_role.value,
                },
            )
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        return self._row(row), False

    def get(self, proposal_id: str) -> Proposal | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        finally:
            conn.close()
        return self._row(row) if row else None

    def list_proposals(
        self, status: ProposalStatus | None = None, ticket_id: str | None = None, limit: int = 50
    ) -> list[Proposal]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if ticket_id is not None:
            clauses.append("ticket_id = ?")
            params.append(ticket_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM proposals {where} ORDER BY created_at, id LIMIT ?", [*params, limit]
            ).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    def compare_and_set(
        self,
        proposal_id: str,
        *,
        expected: Iterable[ProposalStatus],
        new_status: ProposalStatus,
        actor: str,
        event: str,
        detail: dict[str, Any] | None = None,
        **fields: Any,
    ) -> bool:
        """Atomically move a proposal from one of `expected` to `new_status` and audit it."""
        expected_values = [s.value for s in expected]
        values = {k: (json.dumps(v) if k == "result" and v is not None else v) for k, v in fields.items()}
        sets = ", ".join(["status = ?", *(f"{k} = ?" for k in values)])
        marks = ",".join("?" for _ in expected_values)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE proposals SET {sets} WHERE id = ? AND status IN ({marks})",
                [new_status.value, *values.values(), proposal_id, *expected_values],
            )
            if cur.rowcount != 1:
                return False
            self._audit(conn, proposal_id, actor, event, {"status": new_status.value, **(detail or {})})
            return True


class TooManyPendingError(Exception):
    pass
