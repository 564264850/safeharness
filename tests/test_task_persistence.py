import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import uuid4

import pytest

from safeharness.__main__ import main
from safeharness.task import TaskStatus
from safeharness.task_store import TaskConflictError, TaskStore


def test_task_can_be_read_after_writer_process_exits(tmp_path):
    command = [sys.executable, "-m", "safeharness"]
    created = subprocess.run(
        command, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=30
    )
    run = json.loads(created.stdout)

    queried = subprocess.run(
        [*command, "show", run["task_id"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(queried.stdout) == {
        "task_id": run["task_id"],
        "task_status": "SUCCEEDED",
        "revision": 3,
    }
    with closing(sqlite3.connect(tmp_path / "artifacts/tasks.sqlite3")) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0017_queued_approvals",)


def test_stale_task_snapshot_cannot_overwrite_committed_state(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    writer = TaskStore(database)
    reader = TaskStore(database)
    writer.initialize()

    created = writer.create()
    stale = reader.get(created.task_id)
    assert stale == created
    current = writer.transition(created, TaskStatus.RUNNING)

    with pytest.raises(TaskConflictError):
        reader.transition(stale, TaskStatus.RUNNING)

    assert reader.get(created.task_id) == current


def test_show_with_missing_database_does_not_create_one(tmp_path):
    queried = subprocess.run(
        [sys.executable, "-m", "safeharness", "show", str(uuid4())],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert queried.returncode == 1
    assert "not found" in queried.stderr
    assert not (tmp_path / "artifacts").exists()


def test_migration_adopts_legacy_table_and_preserves_task(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    task_id = uuid4()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("""
            CREATE TABLE tasks (
                task_id CHAR(32) NOT NULL,
                status VARCHAR(9) NOT NULL,
                revision INTEGER NOT NULL,
                PRIMARY KEY (task_id),
                CONSTRAINT taskstatus CHECK (status IN ('CREATED', 'RUNNING', 'VERIFYING', 'SUCCEEDED', 'FAILED'))
            )
        """)
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?)", (task_id.hex, "SUCCEEDED", 3)
        )

    arguments = ["--db", str(database), "migrate"]
    assert main(arguments) == 0
    assert main(arguments) == 0

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall() == [("0017_queued_approvals",)]
        assert connection.execute("SELECT * FROM tasks").fetchall() == [
            (task_id.hex, "SUCCEEDED", 3)
        ]


def test_migration_refuses_incompatible_table_without_stamping(tmp_path):
    database = tmp_path / "incompatible.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE tasks (description TEXT)")
        connection.execute("INSERT INTO tasks VALUES ('existing data')")

    with pytest.raises(RuntimeError, match="does not match the M1.2 schema"):
        TaskStore(database).initialize()

    with closing(sqlite3.connect(database)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
        assert tables == [("tasks",)]
        assert connection.execute("SELECT * FROM tasks").fetchall() == [
            ("existing data",)
        ]


def test_approval_loop_migration_preserves_decided_legacy_request(tmp_path):
    from datetime import UTC, datetime
    from hashlib import sha256

    from alembic import command
    from alembic.config import Config

    from safeharness.ticket_store import TicketStore

    database = tmp_path / "legacy-approval.sqlite3"
    store = TaskStore(database)
    task_id, approval_id = uuid4(), uuid4()
    arguments = '{"status":"resolved","ticket_id":"IT-001"}'
    with store._engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        config = Config(attributes={"connection": connection})
        config.set_main_option("script_location", "safeharness:migrations")
        command.upgrade(config, "0011_sessions")
        connection.exec_driver_sql(
            "INSERT INTO tasks VALUES (?,?,?)", (task_id.hex, "WAITING_APPROVAL", 2)
        )
        connection.exec_driver_sql(
            "INSERT INTO pending_approvals VALUES (?,?,?,?,?,?)",
            (
                approval_id.hex,
                task_id.hex,
                "update_ticket_status",
                1,
                arguments,
                "mock-ticket-v1",
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO approval_decisions VALUES (?,?,?,?,?)",
            (
                approval_id.hex,
                "APPROVED",
                "legacy-reviewer",
                sha256(arguments.encode()).hexdigest(),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    store.initialize()
    record = store.get_approval(task_id)
    assert (
        record.request.approval_id == approval_id
        and record.decision.decided_by == "legacy-reviewer"
    )
    assert store.resume(task_id, TicketStore(database)).status is TaskStatus.SUCCEEDED
