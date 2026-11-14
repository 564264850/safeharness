"""Independent PostgreSQL business transactions with idempotent receipts."""

from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from .database import engine_for, execute, normalize_database
from .ticket_store import EvidenceUnavailable, TicketReceipt


class PostgresTicketStore:
    def __init__(self, database):
        self._engine = engine_for(normalize_database(database))

    def initialize(self):
        try:
            with self._engine.begin() as connection:
                execute(
                    connection,
                    "INSERT INTO mock_workspaces VALUES (1,?) ON CONFLICT (singleton) DO NOTHING",
                    (uuid4().hex,),
                )
                execute(
                    connection,
                    "INSERT INTO mock_tickets VALUES ('IT-001','open',0) ON CONFLICT (ticket_id) DO NOTHING",
                )
                return execute(
                    connection,
                    "SELECT workspace_id FROM mock_workspaces WHERE singleton=1",
                ).scalar_one()
        except SQLAlchemyError:
            raise EvidenceUnavailable("Business database is unavailable") from None

    @staticmethod
    def _identity(connection, workspace_id):
        row = execute(
            connection, "SELECT workspace_id FROM mock_workspaces WHERE singleton=1"
        ).fetchone()
        if row is None or (workspace_id is not None and row[0] != workspace_id):
            raise EvidenceUnavailable("Business workspace identity changed")

    def ticket(self, workspace_id=None):
        try:
            with self._engine.connect() as connection:
                row = execute(
                    connection,
                    "SELECT status,revision FROM mock_tickets WHERE ticket_id='IT-001'",
                ).fetchone()
                if row is None and workspace_id is None:
                    return "open", 0
                self._identity(connection, workspace_id)
                if row is None:
                    raise EvidenceUnavailable("Business ticket missing")
                return tuple(row)
        except SQLAlchemyError:
            raise EvidenceUnavailable("Business database is unavailable") from None

    def receipt(self, workspace_id, operation_id):
        try:
            with self._engine.connect() as connection:
                self._identity(connection, workspace_id)
                row = execute(
                    connection,
                    "SELECT digest,ticket_id,status,revision FROM mock_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return TicketReceipt(*row) if row else None
        except SQLAlchemyError:
            raise EvidenceUnavailable("Business receipt is unavailable") from None

    def apply(self, workspace_id, operation_id, digest, ticket_id, status):
        if ticket_id != "IT-001" or status not in {"open", "in_progress", "resolved"}:
            raise EvidenceUnavailable("Business resource is outside the service scope")
        try:
            with self._engine.begin() as connection:
                self._identity(connection, workspace_id)
                execute(
                    connection,
                    "SELECT ticket_id FROM mock_tickets WHERE ticket_id=? FOR UPDATE",
                    (ticket_id,),
                ).fetchone()
                row = execute(
                    connection,
                    "SELECT digest,ticket_id,status,revision FROM mock_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row:
                    if tuple(row[:3]) != (digest, ticket_id, status):
                        raise EvidenceUnavailable("Idempotency key payload changed")
                    return TicketReceipt(*row)
                revision = execute(
                    connection,
                    "UPDATE mock_tickets SET status=?,revision=revision+1 WHERE ticket_id=? RETURNING revision",
                    (status, ticket_id),
                ).scalar_one()
                execute(
                    connection,
                    "INSERT INTO mock_receipts VALUES (?,?,?,?,?)",
                    (operation_id, digest, ticket_id, status, revision),
                )
                return TicketReceipt(digest, ticket_id, status, revision)
        except SQLAlchemyError:
            raise EvidenceUnavailable(
                "Business write or receipt could not be confirmed"
            ) from None
