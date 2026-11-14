"""A mock ticket service with its own database and transactional receipts."""

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

TICKET_ID = "IT-001"


class EvidenceUnavailable(RuntimeError):
    """The ticket service cannot establish what happened to the operation."""


@dataclass(frozen=True)
class TicketReceipt:
    digest: str
    ticket_id: str
    status: str
    revision: int


class TicketStore:
    def __init__(self, control_database: Path):
        self._path = control_database.resolve().with_name(
            control_database.name + ".tickets.sqlite3"
        )

    def initialize(
        self, *, initial_status: Literal["open", "in_progress", "resolved"] = "open"
    ) -> str:
        with self._open(mode="rwc") as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workspace (singleton INTEGER PRIMARY KEY, id TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tickets "
                "(ticket_id TEXT PRIMARY KEY, "
                "status TEXT NOT NULL CHECK(status IN ('open','in_progress','resolved')), "
                "revision INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS receipts (operation_id TEXT PRIMARY KEY, "
                "digest TEXT NOT NULL, ticket_id TEXT NOT NULL, status TEXT NOT NULL, "
                "revision INTEGER NOT NULL)"
            )
            existing = connection.execute(
                "SELECT id FROM workspace WHERE singleton = 1"
            ).fetchone()
            if existing is not None:
                return existing[0]
            workspace_id = uuid4().hex
            connection.execute("INSERT INTO workspace VALUES (1, ?)", (workspace_id,))
            connection.execute(
                "INSERT INTO tickets VALUES (?, ?, 0)", (TICKET_ID, initial_status)
            )
        return workspace_id

    @contextmanager
    def _open(
        self,
        workspace_id: str | None = None,
        *,
        mode: Literal["ro", "rw", "rwc"] = "ro",
    ):
        try:
            with (
                closing(
                    sqlite3.connect(self._path.as_uri() + f"?mode={mode}", uri=True)
                ) as connection,
                connection,
            ):
                connection.execute("BEGIN" if mode == "ro" else "BEGIN IMMEDIATE")
                if workspace_id is not None:
                    row = connection.execute(
                        "SELECT id FROM workspace WHERE singleton = 1"
                    ).fetchone()
                    if row != (workspace_id,):
                        raise EvidenceUnavailable("Ticket workspace identity changed")
                yield connection
        except sqlite3.Error as exc:
            raise EvidenceUnavailable(
                "Ticket database could not supply reliable evidence"
            ) from exc

    def ticket(self, workspace_id: str | None = None) -> tuple[str, int]:
        if workspace_id is None and not self._path.exists():
            return "open", 0
        with self._open(workspace_id) as connection:
            row = connection.execute(
                "SELECT status, revision FROM tickets WHERE ticket_id = ?", (TICKET_ID,)
            ).fetchone()
            if row is None:
                raise EvidenceUnavailable("Mock ticket is missing")
            return row

    @staticmethod
    def _read_receipt(
        connection: sqlite3.Connection, operation_id: str
    ) -> TicketReceipt | None:
        row = connection.execute(
            "SELECT digest, ticket_id, status, revision FROM receipts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return TicketReceipt(*row) if row is not None else None

    def receipt(self, workspace_id: str, operation_id: str) -> TicketReceipt | None:
        with self._open(workspace_id) as connection:
            return self._read_receipt(connection, operation_id)

    def apply(
        self,
        workspace_id: str,
        operation_id: str,
        digest: str,
        ticket_id: str,
        status: str,
    ) -> TicketReceipt:
        with self._open(workspace_id, mode="rw") as connection:
            previous = self._read_receipt(connection, operation_id)
            if previous is not None:
                if (previous.digest, previous.ticket_id, previous.status) != (
                    digest,
                    ticket_id,
                    status,
                ):
                    raise EvidenceUnavailable(
                        "Operation key is bound to different ticket arguments"
                    )
                return previous

            updated = connection.execute(
                "UPDATE tickets SET status = ?, revision = revision + 1 "
                "WHERE ticket_id = ? RETURNING revision",
                (status, ticket_id),
            ).fetchone()
            if updated is None:
                raise EvidenceUnavailable("Target ticket is missing")
            receipt = TicketReceipt(digest, ticket_id, status, updated[0])
            connection.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                (operation_id, digest, ticket_id, status, receipt.revision),
            )
        return receipt
