import json
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from safeharness.approval import ApprovalActor, ApprovalIntent, ApprovalOutcome
from safeharness.task import TaskStatus
from safeharness.task_store import ExecutionConflictError, TaskStore
from safeharness.ticket_store import TicketStore


@pytest.fixture
def pending(tmp_path):
    database = tmp_path / "tasks.sqlite3"
    store = TaskStore(database)
    store.initialize()
    running = store.transition(store.create(), TaskStatus.RUNNING)
    intent = ApprovalIntent(
        "update_ticket_status", 1, '{"status":"resolved","ticket_id":"IT-001"}'
    )
    waiting, request = store.pause_for_approval(running, intent)
    return database, store, waiting, request


def approve(store, request):
    store.decide_approval(
        request.approval_id,
        ApprovalOutcome.APPROVED,
        request.intent.arguments_sha256,
        actor=ApprovalActor("test-operator", can_approve=True),
    )


def test_crash_after_business_commit_reconciles_without_rewriting(tmp_path):
    command = [sys.executable, "-m", "safeharness"]

    def invoke(*args, check=True):
        return subprocess.run(
            [*command, *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
            timeout=30,
        )

    task_id = json.loads(invoke("request-update").stdout)["task_id"]
    approval = json.loads(invoke("show-approval", task_id).stdout)
    invoke(
        "decide-approval",
        approval["approval_id"],
        "approve",
        "--arguments-sha256",
        approval["arguments_sha256"],
    )

    crashed = invoke("resume", task_id, "--simulate-crash-after-write", check=False)
    assert crashed.returncode == 77, crashed.stderr
    assert json.loads(invoke("show", task_id).stdout)["task_status"] == "RUNNING"
    assert json.loads(invoke("show-execution", task_id).stdout)["status"] == "INTENT"
    assert json.loads(invoke("show-ticket").stdout) == {
        "ticket_id": "IT-001",
        "status": "resolved",
        "write_count": 1,
    }

    cancellation = invoke("cancel", task_id, check=False)
    assert cancellation.returncode == 1
    assert "already committed" in cancellation.stderr

    with (
        closing(sqlite3.connect(tmp_path / "artifacts/tasks.sqlite3")) as connection,
        connection,
    ):
        connection.execute(
            "UPDATE approval_decisions SET decided_at = '2000-01-01T00:00:00+00:00'"
        )

    reconciled = json.loads(invoke("resume", task_id).stdout)
    assert reconciled["task_status"] == "SUCCEEDED"
    assert reconciled["execution"]["operation_id"] == approval["approval_id"]
    assert reconciled["execution"]["detail"] == "reconciled_business_receipt"
    assert json.loads(invoke("resume", task_id).stdout) == reconciled
    assert json.loads(invoke("show-ticket").stdout)["write_count"] == 1
    assert json.loads(invoke().stdout)["answer"] == "Ticket IT-001: resolved"


@pytest.mark.parametrize(
    ("condition", "match"),
    [
        ("unapproved", "approved request is required"),
        ("expired", "24-hour execution window"),
        ("arguments_changed", "approved digest"),
        ("tool_changed", "tool policy"),
        ("policy_changed", "policy version has changed"),
        ("scope_denied", "tool policy"),
        ("cancelled", "not waiting"),
    ],
)
def test_invalid_approval_or_cancelled_task_cannot_dispatch(pending, condition, match):
    database, store, task, request = pending
    workspace = TicketStore(database)
    if condition == "scope_denied":
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE pending_approvals SET canonical_arguments = ? WHERE approval_id = ?",
                ('{"status":"resolved","ticket_id":"IT-002"}', request.approval_id.hex),
            )
        request = store.get_pending_approval(task.task_id)
        assert request is not None
    if condition != "unapproved":
        approve(store, request)

    with closing(sqlite3.connect(database)) as connection, connection:
        if condition == "expired":
            connection.execute(
                "UPDATE approval_decisions SET decided_at = ? WHERE approval_id = ?",
                ("2000-01-01T00:00:00+00:00", request.approval_id.hex),
            )
        if condition == "arguments_changed":
            connection.execute(
                "UPDATE pending_approvals SET canonical_arguments = ? WHERE approval_id = ?",
                (
                    '{"status":"in_progress","ticket_id":"IT-001"}',
                    request.approval_id.hex,
                ),
            )
        if condition == "tool_changed":
            connection.execute(
                "UPDATE pending_approvals SET tool_version = 99 WHERE approval_id = ?",
                (request.approval_id.hex,),
            )
        if condition == "policy_changed":
            connection.execute(
                "UPDATE pending_approvals SET policy_version = 'old-policy' WHERE approval_id = ?",
                (request.approval_id.hex,),
            )
    if condition == "cancelled":
        assert store.cancel(task.task_id, workspace).status is TaskStatus.CANCELLED

    with pytest.raises(ExecutionConflictError, match=match):
        store.resume(task.task_id, workspace)
    assert store.get_execution(task.task_id) is None
    assert not database.with_name(database.name + ".tickets.sqlite3").exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_recovery_checks_absence_of_receipt_before_dispatch(pending, cancel):
    database, store, task, request = pending
    workspace = TicketStore(database)
    approve(store, request)
    store._claim_execution(task.task_id, workspace)
    restarted = TaskStore(database)

    if cancel:
        assert restarted.cancel(task.task_id, workspace).status is TaskStatus.CANCELLED
    result = restarted.resume(task.task_id, workspace)
    assert result.status is (TaskStatus.CANCELLED if cancel else TaskStatus.SUCCEEDED)
    assert workspace.ticket() == (("open", 0) if cancel else ("resolved", 1))


def test_missing_business_evidence_stops_for_review(pending):
    database, store, task, request = pending
    workspace = TicketStore(database)
    approve(store, request)
    store._claim_execution(task.task_id, workspace)
    business_database = database.with_name(database.name + ".tickets.sqlite3")
    business_database.unlink()

    restarted = TaskStore(database)
    assert restarted.resume(task.task_id, workspace).status is TaskStatus.NEEDS_REVIEW
    assert restarted.get_execution(task.task_id)["status"] == "UNKNOWN"
    assert restarted.resume(task.task_id, workspace).status is TaskStatus.NEEDS_REVIEW
    assert not business_database.exists()
