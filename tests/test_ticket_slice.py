import json
from unittest.mock import Mock

import pytest

from safeharness.__main__ import main, verify_demo
from safeharness.execution import TicketGateway, ToolRequest
from safeharness.runtime import FinalAnswer, run_task
from safeharness.task import Task, TaskStatus
from safeharness.task_store import TaskStore


def test_demo_runs_through_the_tool_and_verifies(capsys, tmp_path):
    assert main(["--db", str(tmp_path / "tasks.sqlite3")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["verification_passed"]
    assert output["task_status"] == "SUCCEEDED"
    assert output["events"] == [
        {"tool": "read_ticket", "status": "ok", "code": "ticket_read"}
    ]
    assert output["answer"] == "Ticket IT-001: open"


@pytest.mark.parametrize(
    ("request_data", "code"),
    [
        (ToolRequest("update_ticket", {"ticket_id": "IT-001"}), "unknown_tool"),
        (ToolRequest("read_ticket", {"ticket_id": 1}), "invalid_arguments"),
        (
            ToolRequest("read_ticket", {"ticket_id": "IT-001", "admin": True}),
            "invalid_arguments",
        ),
        (ToolRequest("read_ticket", {"ticket_id": "IT-002"}), "resource_out_of_scope"),
    ],
)
def test_gateway_rejects_before_reading(request_data, code):
    tickets = Mock(spec=dict)
    gateway = TicketGateway(tickets, frozenset({"IT-001"}))
    assert gateway.execute(request_data).code == code
    tickets.get.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "writable_tickets", "status", "code"),
    [
        (
            {"ticket_id": "IT-001", "status": "resolved"},
            frozenset(),
            "denied",
            "resource_out_of_scope",
        ),
        (
            {"ticket_id": "IT-001", "status": "resolved"},
            frozenset({"IT-002"}),
            "denied",
            "resource_out_of_scope",
        ),
        (
            {"ticket_id": "IT-001", "status": "resolved"},
            frozenset({"IT-001"}),
            "approval_required",
            "write_approval_required",
        ),
        (
            {"ticket_id": "IT-001", "status": "deleted"},
            frozenset({"IT-001"}),
            "invalid",
            "invalid_arguments",
        ),
        (
            {"ticket_id": "IT-001", "status": "resolved", "approved": True},
            frozenset({"IT-001"}),
            "invalid",
            "invalid_arguments",
        ),
    ],
)
def test_ticket_update_is_checked_before_mutation(
    arguments, writable_tickets, status, code
):
    tickets = {"IT-001": "open"}
    gateway = TicketGateway(
        tickets, frozenset({"IT-001"}), writable_tickets=writable_tickets
    )
    result = gateway.execute(ToolRequest("update_ticket_status", arguments))
    assert (result.status, result.code) == (status, code)
    assert tickets == {"IT-001": "open"}


def test_model_claim_without_tool_evidence_does_not_pass(tmp_path):
    execute = Mock()
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    result = run_task(
        lambda _: FinalAnswer("Ticket IT-001: open"),
        execute,
        verify_demo,
        max_steps=2,
        store=store,
    )
    assert result.run.stop_reason == "model_finished"
    assert result.task.status is TaskStatus.FAILED
    assert store.get(result.task.task_id) == result.task
    execute.assert_not_called()


def test_step_limit_stops_further_model_actions():
    gateway = TicketGateway({"IT-001": "open"}, frozenset({"IT-001"}))
    action = Mock(return_value=ToolRequest("read_ticket", {"ticket_id": "IT-001"}))
    verify = Mock()
    result = run_task(action, gateway.execute, verify, max_steps=2)
    assert result.run.stop_reason == "step_limit"
    assert result.task.status is TaskStatus.FAILED
    assert action.call_count == len(result.run.tool_results) == 2
    verify.assert_not_called()


def test_task_cannot_skip_verification_or_reopen_after_completion():
    created = Task()
    running = created.transition(TaskStatus.RUNNING)
    with pytest.raises(ValueError, match="RUNNING -> SUCCEEDED"):
        running.transition(TaskStatus.SUCCEEDED)

    completed = running.transition(TaskStatus.VERIFYING).transition(
        TaskStatus.SUCCEEDED
    )
    assert completed.task_id == created.task_id
    with pytest.raises(ValueError, match="SUCCEEDED -> RUNNING"):
        completed.transition(TaskStatus.RUNNING)
