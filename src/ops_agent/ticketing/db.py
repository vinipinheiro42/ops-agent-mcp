"""SQLite persistence for the fake ticketing system.

Every write runs inside a single `BEGIN IMMEDIATE` transaction together with its idempotency record,
so a replayed request can never be applied twice and a crash can never leave a write without its
idempotency record (or vice versa).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_agent.ticketing.models import (
    TRANSITIONS,
    Comment,
    Status,
    Ticket,
    TicketPage,
    TicketWithComments,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    reporter     TEXT NOT NULL,
    site         TEXT NOT NULL,
    status       TEXT NOT NULL,
    priority     TEXT,
    category     TEXT,
    team         TEXT,
    assignee     TEXT,
    labels       TEXT NOT NULL DEFAULT '[]',
    duplicate_of TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_tickets_created ON tickets(created_at);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT NOT NULL REFERENCES tickets(id),
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    internal   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_comments_ticket ON comments(ticket_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    """Version mismatch or invalid workflow transition."""


class IdempotencyMismatchError(Exception):
    """The same idempotency key was reused with a different request body."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def request_fingerprint(method: str, path: str, body: dict[str, Any]) -> str:
    canonical = json.dumps({"m": method, "p": path, "b": body}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class TicketRepository:
    def __init__(self, db_path: Path, clock: Clock = _utc_now) -> None:
        self.db_path = db_path
        self._clock = clock
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ---- plumbing ----------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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

    def now(self) -> str:
        return _iso(self._clock())

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> Ticket:
        data = dict(row)
        data["labels"] = json.loads(data["labels"])
        return Ticket.model_validate(data)

    # ---- seed --------------------------------------------------------------------------------

    def reset_and_load(self, tickets: Sequence[Ticket]) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM comments")
            conn.execute("DELETE FROM idempotency_keys")
            conn.execute("DELETE FROM tickets")
            conn.executemany(
                "INSERT INTO tickets (id, title, description, reporter, site, status, priority, category,"
                " team, assignee, labels, duplicate_of, created_at, updated_at, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        t.id,
                        t.title,
                        t.description,
                        t.reporter,
                        t.site,
                        t.status.value,
                        t.priority.value if t.priority else None,
                        t.category.value if t.category else None,
                        t.team.value if t.team else None,
                        t.assignee,
                        json.dumps(t.labels),
                        t.duplicate_of,
                        t.created_at,
                        t.updated_at,
                        t.version,
                    )
                    for t in tickets
                ],
            )

    # ---- reads -------------------------------------------------------------------------------

    def get(self, ticket_id: str) -> TicketWithComments:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise NotFoundError(ticket_id)
            comments = [
                Comment(
                    id=c["id"],
                    ticket_id=c["ticket_id"],
                    author=c["author"],
                    body=c["body"],
                    internal=bool(c["internal"]),
                    created_at=c["created_at"],
                )
                for c in conn.execute(
                    "SELECT * FROM comments WHERE ticket_id = ? ORDER BY id", (ticket_id,)
                ).fetchall()
            ]
        finally:
            conn.close()
        return TicketWithComments(**self._row_to_ticket(row).model_dump(), comments=comments)

    def search(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        category: str | None = None,
        team: str | None = None,
        priority: str | None = None,
        site: str | None = None,
        untriaged: bool | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> TicketPage:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("category", category),
            ("team", team),
            ("priority", priority),
            ("site", site),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if untriaged is True:
            clauses.append("category IS NULL")
        elif untriaged is False:
            clauses.append("category IS NOT NULL")
        if q:
            for term in q.split():
                clauses.append("(title LIKE ? OR description LIKE ?)")
                like = f"%{term}%"
                params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM tickets {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM tickets {where} ORDER BY created_at, id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        finally:
            conn.close()
        return TicketPage(
            items=[self._row_to_ticket(r) for r in rows], total=total, limit=limit, offset=offset
        )

    def count_comments(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0])
        finally:
            conn.close()

    # ---- writes ------------------------------------------------------------------------------

    def run_write(
        self,
        *,
        idempotency_key: str | None,
        fingerprint: str,
        operation: Callable[[sqlite3.Connection], tuple[int, dict[str, Any]]],
    ) -> WriteResult:
        """Execute `operation` atomically, deduplicated by `idempotency_key` when provided."""
        with self._tx() as conn:
            if idempotency_key is not None:
                row = conn.execute(
                    "SELECT request_hash, status_code, response FROM idempotency_keys WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    if row["request_hash"] != fingerprint:
                        raise IdempotencyMismatchError(idempotency_key)
                    return WriteResult(row["status_code"], json.loads(row["response"]), replayed=True)
            status_code, body = operation(conn)
            if idempotency_key is not None:
                conn.execute(
                    "INSERT INTO idempotency_keys (key, request_hash, status_code, response, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (idempotency_key, fingerprint, status_code, json.dumps(body), self.now()),
                )
            return WriteResult(status_code, body, replayed=False)

    def _load_for_write(
        self, conn: sqlite3.Connection, ticket_id: str, expected_version: int | None
    ) -> Ticket:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise NotFoundError(ticket_id)
        ticket = self._row_to_ticket(row)
        if expected_version is not None and ticket.version != expected_version:
            raise ConflictError(
                f"version conflict on {ticket_id}: expected {expected_version}, current {ticket.version}"
            )
        return ticket

    def _bump(self, conn: sqlite3.Connection, ticket_id: str, changes: dict[str, Any]) -> Ticket:
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(
            f"UPDATE tickets SET {sets}, updated_at = ?, version = version + 1 WHERE id = ?",
            [*changes.values(), self.now(), ticket_id],
        )
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return self._row_to_ticket(row)

    def op_update_fields(
        self, ticket_id: str, fields: dict[str, Any], expected_version: int | None
    ) -> Callable[[sqlite3.Connection], tuple[int, dict[str, Any]]]:
        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            self._load_for_write(conn, ticket_id, expected_version)
            if "duplicate_of" in fields and fields["duplicate_of"] is not None:
                target = fields["duplicate_of"]
                if target == ticket_id:
                    raise ConflictError("a ticket cannot be a duplicate of itself")
                if conn.execute("SELECT 1 FROM tickets WHERE id = ?", (target,)).fetchone() is None:
                    raise NotFoundError(target)
            changes = {k: (json.dumps(v) if k == "labels" else v) for k, v in fields.items()}
            if not changes:
                raise ConflictError("empty update")
            return 200, self._bump(conn, ticket_id, changes).model_dump(mode="json")

        return op

    def op_assign(
        self, ticket_id: str, team: str, assignee: str | None, expected_version: int | None
    ) -> Callable[[sqlite3.Connection], tuple[int, dict[str, Any]]]:
        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            self._load_for_write(conn, ticket_id, expected_version)
            ticket = self._bump(conn, ticket_id, {"team": team, "assignee": assignee})
            return 200, ticket.model_dump(mode="json")

        return op

    def op_transition(
        self, ticket_id: str, to_status: Status, expected_version: int | None
    ) -> Callable[[sqlite3.Connection], tuple[int, dict[str, Any]]]:
        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            current = self._load_for_write(conn, ticket_id, expected_version)
            if to_status not in TRANSITIONS[current.status]:
                raise ConflictError(f"invalid transition {current.status.value} -> {to_status.value}")
            return 200, self._bump(conn, ticket_id, {"status": to_status.value}).model_dump(mode="json")

        return op

    def op_comment(
        self, ticket_id: str, author: str, body: str, internal: bool
    ) -> Callable[[sqlite3.Connection], tuple[int, dict[str, Any]]]:
        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            self._load_for_write(conn, ticket_id, None)
            now = self.now()
            cur = conn.execute(
                "INSERT INTO comments (ticket_id, author, body, internal, created_at) VALUES (?,?,?,?,?)",
                (ticket_id, author, body, int(internal), now),
            )
            conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
            comment = Comment(
                id=int(cur.lastrowid or 0),
                ticket_id=ticket_id,
                author=author,
                body=body,
                internal=internal,
                created_at=now,
            )
            return 201, comment.model_dump(mode="json")

        return op
