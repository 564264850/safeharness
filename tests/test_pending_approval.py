import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from hashlib import sha256
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError

from safeharness.approval import ApprovalIntent
from safeharness.execution import TicketGateway, ToolRequest
from safeharness.runtime import run_task
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore


def test_pending_approval_survives_writer_exit(tmp_path):
    command = [sys.executable, "-m", "safeharness"]
    created = subprocess.run(
        [*command, "request-update"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    run = json.loads(created.stdout)
    assert run["task_status"] == "WAITING_APPROVAL"
    assert run["stop_reason"] == "approval_required"
    assert run["verification_passed"] is None
    assert run["ticket_status"] == "open"

    queried = subprocess.run(
        [*command, "show-approval", run["task_id"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(queried.stdout) == {
        "approval_id": run["approval_id"],
        "task_id": run["task_id"],
        "approval_status": "PENDING",
        "tool_name": "update_ticket_status",
        "tool_version": 1,
        "arguments": {"status": "resolved", "ticket_id": "IT-001"},
        "arguments_sha256": sha256(
            b'{"status":"resolved","ticket_id":"IT-001"}'
        ).hexdigest(),
    }

    state = subprocess.run(
        [*command, "show", run["task_id"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(state.stdout) == {
        "task_id": run["task_id"],
        "task_status": "WAITING_APPROVAL",
        "revision": 2,
    }


def test_approval_pauses_model_and_verifier(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    tickets = {"IT-001": "open"}
    gateway = TicketGateway(
        tickets, frozenset(), writable_tickets=frozenset({"IT-001"})
    )
    model = Mock(
        return_value=ToolRequest(
            "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
        )
    )
    verify = Mock()

    result = run_task(model, gateway.execute, verify, max_steps=3, store=store)

    model.assert_called_once_with(())
    verify.assert_not_called()
    assert result.task.status is TaskStatus.WAITING_APPROVAL
    assert tickets == {"IT-001": "open"}


def test_failed_approval_insert_rolls_back_task_state(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    store = TaskStore(database)
    store.initialize()
    running = store.transition(store.create(), TaskStatus.RUNNING)
    intent = ApprovalIntent(
        "update_ticket_status", 1, '{"status":"resolved","ticket_id":"IT-001"}'
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("""
            CREATE TRIGGER fail_approval_insert BEFORE INSERT ON pending_approvals
            BEGIN
                SELECT RAISE(ABORT, 'injected approval insert failure');
            END
        """)

    with pytest.raises(IntegrityError, match="injected approval insert failure"):
        store.pause_for_approval(running, intent)

    assert store.get(running.task_id) == running
    assert store.get_pending_approval(running.task_id) is None


def test_task_cannot_be_paused_without_approval_record(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    running = store.transition(store.create(), TaskStatus.RUNNING)

    with pytest.raises(ValueError, match="pause_for_approval"):
        store.transition(running, TaskStatus.WAITING_APPROVAL)

    assert store.get(running.task_id) == running
