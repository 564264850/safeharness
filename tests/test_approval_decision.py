import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from safeharness.approval import ApprovalActor, ApprovalOutcome
from safeharness.execution import TicketGateway, ToolRequest
from safeharness.task import TaskStatus
from safeharness.task_store import ApprovalConflictError, TaskStore


@pytest.fixture
def pending_approval(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    store = TaskStore(database)
    store.initialize()
    running = store.transition(store.create(), TaskStatus.RUNNING)
    gateway = TicketGateway(
        {"IT-001": "open"}, frozenset(), writable_tickets=frozenset({"IT-001"})
    )
    intent = gateway.execute(
        ToolRequest(
            "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
        )
    ).approval
    assert intent is not None
    waiting, approval = store.pause_for_approval(running, intent)
    return database, store, waiting, approval


def test_approval_decision_can_be_read_after_operator_process_exits(tmp_path):
    command = [sys.executable, "-m", "safeharness"]

    def run_json(*arguments):
        result = subprocess.run(
            [*command, *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    run = run_json("request-update")
    request = run_json("show-approval", run["task_id"])
    decision_args = [
        "decide-approval",
        request["approval_id"],
        "approve",
        "--arguments-sha256",
        request["arguments_sha256"],
    ]
    decision = run_json(*decision_args)
    assert decision["approval_status"] == "APPROVED"
    assert decision["decided_by"]
    assert decision["reviewed_arguments_sha256"] == request["arguments_sha256"]

    saved = run_json("show-approval", run["task_id"])
    assert saved == {**request, **decision}

    duplicate = subprocess.run(
        [*command, *decision_args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert duplicate.returncode == 1
    assert "already been decided" in duplicate.stderr
    task = TaskStore(tmp_path / "artifacts/tasks.sqlite3").get(UUID(run["task_id"]))
    assert task is not None
    assert task.status is TaskStatus.WAITING_APPROVAL
    assert task.revision == 2


def test_unprivileged_actor_cannot_decide_approval(pending_approval):
    _, store, waiting, approval = pending_approval

    with pytest.raises(PermissionError, match="does not have approval permission"):
        store.decide_approval(
            approval.approval_id,
            ApprovalOutcome.APPROVED,
            approval.intent.arguments_sha256,
            actor=ApprovalActor(subject="model"),
        )

    assert store.get_pending_approval(waiting.task_id) == approval
    assert store.get(waiting.task_id) == waiting


def test_changed_arguments_cannot_be_approved_using_previous_digest(pending_approval):
    database, store, waiting, approval = pending_approval
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE pending_approvals SET canonical_arguments = ? WHERE approval_id = ?",
            ('{"status":"in_progress","ticket_id":"IT-001"}', approval.approval_id.hex),
        )

    with pytest.raises(ApprovalConflictError, match="arguments do not match"):
        store.decide_approval(
            approval.approval_id,
            ApprovalOutcome.APPROVED,
            approval.intent.arguments_sha256,
            actor=ApprovalActor(subject="operator", can_approve=True),
        )

    assert store.get_approval(waiting.task_id).decision is None
    assert store.get(waiting.task_id) == waiting


def test_rejection_records_decision_and_stops_task(pending_approval):
    _, store, waiting, approval = pending_approval
    decision = store.decide_approval(
        approval.approval_id,
        ApprovalOutcome.REJECTED,
        approval.intent.arguments_sha256,
        actor=ApprovalActor(subject="operator", can_approve=True),
    )

    assert decision.outcome is ApprovalOutcome.REJECTED
    assert store.get_approval(waiting.task_id).decision == decision
    assert store.get_pending_approval(waiting.task_id) is None
    assert store.get(waiting.task_id) == waiting.transition(TaskStatus.FAILED)


def test_rejection_rolls_back_when_task_update_fails(pending_approval):
    database, store, waiting, approval = pending_approval
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("""
            CREATE TRIGGER fail_rejection BEFORE UPDATE OF status ON tasks
            WHEN NEW.status = 'FAILED'
            BEGIN
                SELECT RAISE(ABORT, 'injected rejection transition failure');
            END
        """)

    with pytest.raises(IntegrityError, match="injected rejection transition failure"):
        store.decide_approval(
            approval.approval_id,
            ApprovalOutcome.REJECTED,
            approval.intent.arguments_sha256,
            actor=ApprovalActor(subject="operator", can_approve=True),
        )

    assert store.get_pending_approval(waiting.task_id) == approval
    assert store.get(waiting.task_id) == waiting
