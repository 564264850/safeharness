"""Fault injection for live-run correctness; no paid calls or Docker required."""

import json
import sqlite3
from unittest.mock import Mock

import pytest

from safeharness.__main__ import main
from safeharness.approval import ApprovalActor, ApprovalIntent, ApprovalOutcome
from safeharness.audit import AuditLog
from safeharness.coding_demo import run_coding_demo
from safeharness.execution import ToolGateway, ToolRequest, ToolResult
from safeharness.failures import RuntimeFailure, RuntimePersistenceError
from safeharness.runtime import FinalAnswer, run_task
from safeharness.task import Task, TaskStatus
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.trace import TraceLog

PRIVATE = "private exception body with credential " + "sk-" + "private" * 6


def crash(*args, **kwargs):
    raise RuntimeError(PRIVATE)


@pytest.fixture
def store(tmp_path):
    result = TaskStore(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


@pytest.mark.parametrize(
    "stage,expected,code",
    [
        ("model", "FAILED", "model_adapter_error"),
        ("model_value", "FAILED", "invalid_model_action"),
        ("empty_answer", "FAILED", "invalid_final_answer"),
        ("tool", "NEEDS_REVIEW", "tool_outcome_unknown"),
        ("result_none", "NEEDS_REVIEW", "invalid_tool_result"),
        ("result_wrong_name", "NEEDS_REVIEW", "invalid_tool_result"),
        ("result_bad_status", "NEEDS_REVIEW", "invalid_tool_result"),
        ("result_bad_approval", "NEEDS_REVIEW", "invalid_tool_result"),
        ("verify", "FAILED", "verification_error"),
        ("verify_truthy", "FAILED", "invalid_verification_result"),
        ("complete", "NEEDS_REVIEW", "completion_error"),
        ("complete_none", "NEEDS_REVIEW", "invalid_completion_result"),
        ("complete_unsaved", "NEEDS_REVIEW", "completion_not_persisted"),
    ],
)
def test_runtime_failure_converges_and_is_durable(store, stage, expected, code):
    task = store.create()
    model, execute, verify = (
        Mock(return_value=FinalAnswer("ready")),
        Mock(),
        Mock(return_value=True),
    )
    complete = None
    if stage == "model":
        model.side_effect = crash
    elif stage == "model_value":
        model.return_value = {"answer": "not a protocol object"}
    elif stage == "empty_answer":
        model.return_value = FinalAnswer("")
    elif stage.startswith("result") or stage == "tool":
        model.return_value = ToolRequest("read_ticket", {"ticket_id": "IT-001"})
        if stage == "tool":
            execute.side_effect = crash
        else:
            execute.return_value = {
                "result_none": None,
                "result_wrong_name": ToolResult("different", "ok", "completed"),
                "result_bad_status": ToolResult("read_ticket", [], "completed"),
                "result_bad_approval": ToolResult(
                    "read_ticket", "approval_required", "write_approval_required"
                ),
            }[stage]
    elif stage == "verify":
        verify.side_effect = crash
    elif stage == "verify_truthy":
        verify.return_value = 1
    elif stage == "complete":
        complete = Mock(side_effect=crash)
    elif stage == "complete_none":
        complete = Mock(return_value=None)
    elif stage == "complete_unsaved":
        complete = lambda verifying, run, passed: verifying.transition(
            TaskStatus.SUCCEEDED
        )
    result = run_task(
        model, execute, verify, max_steps=3, store=store, task=task, complete=complete
    )
    assert result.task.status.value == expected
    assert result.run.error_code == code
    restarted = TaskStore(store.database)
    assert restarted.get(task.task_id) == result.task
    failure = restarted.get_runtime_failure(task.task_id)
    assert failure["code"] == code and failure["recorded_status"] == expected
    assert failure["outcome_unknown"] is (expected == "NEEDS_REVIEW")
    assert PRIVATE not in TraceLog(store.database, task.task_id).jsonl()
    if stage.startswith("result") or stage in {
        "model",
        "model_value",
        "empty_answer",
        "tool",
    }:
        verify.assert_not_called()
    if stage.startswith("result") or stage == "tool":
        execute.assert_called_once()
        model.assert_called_once()


@pytest.mark.parametrize(
    "tool,unknown", [("read_file", False), ("write_file", True), ("run_tests", True)]
)
def test_gateway_classifies_adapter_exception_and_never_retries(store, tool, unknown):
    task = store.create()
    coding = Mock()
    calls = []

    def fail_after_dispatch(*args, **kwargs):
        calls.append(tool)
        crash()

    coding.execute.side_effect = fail_after_dispatch
    gateway = ToolGateway(
        coding=coding,
        readable_files=frozenset({"a.py"}),
        writable_files=frozenset({"a.py"}),
        test_paths=frozenset({"tests/test_a.py"}),
        audit=AuditLog(store.database, task.task_id),
    )
    arguments = (
        {"paths": ["tests/test_a.py"]} if tool == "run_tests" else {"path": "a.py"}
    )
    if tool == "write_file":
        arguments["content"] = "new"
    verify = Mock()
    result = run_task(
        lambda _: ToolRequest(tool, arguments),
        gateway.execute,
        verify,
        max_steps=5,
        store=store,
        task=task,
    )
    assert result.task.status is (
        TaskStatus.NEEDS_REVIEW if unknown else TaskStatus.FAILED
    )
    assert calls == [tool]
    verify.assert_not_called()
    events = AuditLog(store.database, task.task_id).read()
    assert [event["phase"] for event in events] == ["POLICY", "INTENT", "RESULT"]
    assert events[-1]["code"] == (
        "tool_outcome_unknown" if unknown else "execution_adapter_error"
    )
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize(
    "when,tool,expected",
    [
        ("before", "write_file", TaskStatus.FAILED),
        ("after", "read_file", TaskStatus.FAILED),
        ("after", "write_file", TaskStatus.NEEDS_REVIEW),
    ],
)
def test_audit_failure_before_and_after_dispatch_are_distinguished(
    store, when, tool, expected
):
    task = store.create()
    with sqlite3.connect(store.database) as connection:
        condition = "" if when == "before" else "WHEN NEW.phase='RESULT'"
        connection.execute(
            f"CREATE TRIGGER refuse_event BEFORE INSERT ON gateway_events {condition} BEGIN SELECT RAISE(ABORT,'test outage'); END"
        )
    from safeharness.coding import CodingReply

    coding = Mock()
    coding.execute.return_value = CodingReply(True, "completed", "done")
    gateway = ToolGateway(
        coding=coding,
        readable_files=frozenset({"a.py"}),
        writable_files=frozenset({"a.py"}),
        audit=AuditLog(store.database, task.task_id),
    )
    args = {"path": "a.py", **({"content": "new"} if tool == "write_file" else {})}
    result = run_task(
        lambda _: ToolRequest(tool, args),
        gateway.execute,
        Mock(),
        max_steps=3,
        store=store,
        task=task,
    )
    assert result.task.status is expected
    assert coding.execute.call_count == (0 if when == "before" else 1)
    assert store.get_runtime_failure(task.task_id)["code"].startswith("audit_")


def test_verifier_tool_outcome_unknown_can_stop_for_review(store):
    task = store.create()
    coding = Mock()
    coding.verify_contract.side_effect = crash
    gateway = ToolGateway(coding=coding, audit=AuditLog(store.database, task.task_id))
    result = run_task(
        lambda _: FinalAnswer("ready"),
        Mock(),
        lambda _: gateway.verify_contract([[1, 0, 2]]),
        max_steps=1,
        store=store,
        task=task,
    )
    assert result.task.status is TaskStatus.NEEDS_REVIEW
    assert store.get_runtime_failure(task.task_id)["phase"] == "verification"


@pytest.mark.parametrize("passed", [False, True])
def test_completion_commit_then_exception_preserves_committed_terminal_state(
    store, passed
):
    task = store.create()
    target = TaskStatus.SUCCEEDED if passed else TaskStatus.FAILED
    calls = []

    def complete(verifying, run, actual):
        calls.append(actual)
        store.transition(verifying, target)
        crash()

    result = run_task(
        lambda _: FinalAnswer("ready"),
        Mock(),
        lambda _: passed,
        max_steps=1,
        store=store,
        task=task,
        complete=complete,
    )
    assert result.task.status is target and store.get(task.task_id) == result.task
    assert calls == [passed]
    assert store.get_runtime_failure(task.task_id)["code"] == "completion_error"


@pytest.mark.parametrize("after_commit", [False, True])
def test_approval_persistence_exception_never_creates_false_wait_or_duplicate(
    store, monkeypatch, after_commit
):
    task = store.create()
    gateway = ToolGateway(
        {},
        writable_tickets=frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    pause = store.pause_for_approval

    def fail_pause(*args):
        if after_commit:
            pause(*args)
        crash()

    monkeypatch.setattr(store, "pause_for_approval", fail_pause)
    result = run_task(
        lambda _: ToolRequest(
            "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
        ),
        gateway.execute,
        Mock(),
        max_steps=1,
        store=store,
        task=task,
    )
    assert result.task.status is (
        TaskStatus.WAITING_APPROVAL if after_commit else TaskStatus.FAILED
    )
    assert (store.get_pending_approval(task.task_id) is not None) is after_commit
    assert TicketStore(store.database).ticket() == ("open", 0)


def test_cancelled_task_is_not_reopened_by_error_handler(store):
    task = store.create(owner_id="alice", request={"scenario": "ticket_read"})

    def cancel_then_fail(_):
        store.cancel(task.task_id, TicketStore(store.database))
        crash()

    result = run_task(
        cancel_then_fail, Mock(), Mock(), max_steps=1, store=store, task=task
    )
    assert result.task.status is TaskStatus.CANCELLED
    assert store.get_runtime_failure(task.task_id)["recorded_status"] == "CANCELLED"


def test_stale_runtime_failure_cannot_overwrite_newer_revision(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    verifying = store.transition(task, TaskStatus.VERIFYING)
    assert (
        store.record_runtime_failure(
            task, RuntimeFailure("model", "model_adapter_error")
        )
        == verifying
    )
    assert (
        store.get_runtime_failure(task.task_id)["recovery_action"]
        == "inspect_current_revision"
    )


def test_existing_business_intent_is_left_for_receipt_reconciliation(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    _, approval = store.pause_for_approval(
        task,
        ApprovalIntent(
            "update_ticket_status", 1, '{"status":"resolved","ticket_id":"IT-001"}'
        ),
    )
    store.decide_approval(
        approval.approval_id,
        ApprovalOutcome.APPROVED,
        approval.intent.arguments_sha256,
        actor=ApprovalActor("reviewer", can_approve=True),
    )
    business = TicketStore(store.database)
    store._claim_execution(task.task_id, business)
    running = store.get(task.task_id)
    assert (
        store.record_runtime_failure(
            running, RuntimeFailure("tool", "tool_outcome_unknown", True)
        )
        == running
    )
    assert store.get_execution(task.task_id)["status"] == "INTENT"
    assert (
        store.get_runtime_failure(task.task_id)["recovery_action"]
        == "reconcile_business_receipt"
    )
    assert store.resume(task.task_id, business).status is TaskStatus.SUCCEEDED
    assert store.resume(task.task_id, business).status is TaskStatus.SUCCEEDED
    assert business.ticket() == ("resolved", 1)


def test_error_record_and_terminal_state_roll_back_together_when_storage_fails(store):
    task = store.create()
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_runtime BEFORE INSERT ON trace_events WHEN NEW.kind='runtime.error' BEGIN SELECT RAISE(ABORT,'test outage'); END"
        )
    with pytest.raises(RuntimePersistenceError) as caught:
        run_task(crash, Mock(), Mock(), max_steps=1, store=store, task=task)
    assert caught.value.task_id == task.task_id
    assert caught.value.code == "runtime_failure_not_persisted"
    assert PRIVATE not in str(caught.value)
    assert store.get(task.task_id).status is TaskStatus.RUNNING
    assert store.get_runtime_failure(task.task_id) is None
    assert [
        event["kind"] for event in TraceLog(store.database, task.task_id).read()
    ] == ["task.created", "task.transition"]


def test_cli_show_includes_safe_diagnostic_after_process_restart(store, capsys):
    result = run_task(crash, Mock(), Mock(), max_steps=1, store=store)
    assert main(["--db", str(store.database), "show", str(result.task.task_id)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["task_status"] == "FAILED"
    assert output["last_runtime_error"]["code"] == "model_adapter_error"
    assert PRIVATE not in json.dumps(output)


def test_pure_runtime_also_converges_without_store():
    failed = run_task(lambda _: FinalAnswer("ready"), Mock(), crash, max_steps=1)
    assert failed.task.status is TaskStatus.FAILED
    unknown = run_task(
        lambda _: ToolRequest("write_file", {}), crash, Mock(), max_steps=1
    )
    assert unknown.task.status is TaskStatus.NEEDS_REVIEW


def test_completion_cannot_return_forged_revision_without_store():
    result = run_task(
        lambda _: FinalAnswer("ready"),
        Mock(),
        lambda _: True,
        max_steps=1,
        complete=lambda task, run, passed: Task(task.task_id, TaskStatus.SUCCEEDED, 0),
    )
    assert result.task.status is TaskStatus.NEEDS_REVIEW
    assert result.run.error_code == "invalid_completion_result"


def test_cleanup_uncertainty_stops_before_next_model_or_verifier(store):
    model = Mock(return_value=ToolRequest("run_tests", {"paths": ["tests/test_a.py"]}))
    execute = Mock(
        return_value=ToolResult("run_tests", "failed", "container_cleanup_failed")
    )
    verify = Mock()
    result = run_task(model, execute, verify, max_steps=4, store=store)
    assert result.task.status is TaskStatus.NEEDS_REVIEW
    assert result.run.error_code == "container_cleanup_failed"
    model.assert_called_once()
    execute.assert_called_once()
    verify.assert_not_called()


def test_exception_text_cannot_become_public_error_code(store):
    from safeharness.runtime import RunInterrupted

    result = run_task(
        Mock(side_effect=RunInterrupted(PRIVATE)),
        Mock(),
        Mock(),
        max_steps=1,
        store=store,
    )
    assert result.run.error_code == "unclassified_error"
    assert PRIVATE not in TraceLog(store.database, result.task.task_id).jsonl()


def test_cli_persistence_failure_has_task_id_and_safe_nonzero_exit(
    store, monkeypatch, capsys
):
    def outage(*args, **kwargs):
        raise RuntimePersistenceError(
            Task().task_id, RuntimeFailure("model", "model_adapter_error")
        )

    monkeypatch.setattr("safeharness.__main__.run_demo", outage)
    assert main(["--db", str(store.database)]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output["error_code"] == "runtime_failure_not_persisted"
    assert (
        output["task_id"] and output["recovery_action"] == "inspect_task_before_retry"
    )


@pytest.mark.parametrize("steps", [0, -1, True, 1.5, None])
def test_bad_budget_is_rejected_before_task_creation(store, steps):
    with pytest.raises(ValueError, match="positive integer"):
        run_task(Mock(), Mock(), Mock(), max_steps=steps, store=store)
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_coding_demo_returns_actual_error_instead_of_empty_success_report(
    store, monkeypatch
):
    monkeypatch.setattr(
        "safeharness.coding_demo.scripted_coding", lambda **kwargs: crash
    )
    report = run_coding_demo(store)
    assert report["task_status"] == "FAILED"
    assert report["verification"]["passed"] is False
    assert report["last_runtime_error"]["code"] == "model_adapter_error"


def test_failed_coding_report_transaction_cannot_return_in_memory_success(
    store, monkeypatch
):
    from safeharness.coding_demo import Verification

    monkeypatch.setattr(
        "safeharness.coding_demo.scripted_coding",
        lambda **kwargs: lambda _: FinalAnswer("ready"),
    )
    monkeypatch.setattr(
        "safeharness.coding_demo.verify_coding",
        lambda *args: Verification(True, "test_passed"),
    )
    monkeypatch.setattr(store, "complete_coding", crash)
    report = run_coding_demo(store)
    assert report["task_status"] == "NEEDS_REVIEW"
    assert report["verification"]["passed"] is False
    assert report["last_runtime_error"]["code"] == "completion_error"


def test_evaluation_rebuilds_report_after_completion_transaction_failure(
    tmp_path, monkeypatch
):
    from uuid import UUID

    from safeharness.dataset import load_dataset
    from safeharness.evaluation import read_evaluation, run_case

    original = TaskStore.record_evaluation
    failed = []

    def fail_first_completion(self, task, report, private, *, finalize=False):
        if finalize and not failed:
            failed.append(report["task_status"])
            crash()
        return original(self, task, report, private, finalize=finalize)

    monkeypatch.setattr(TaskStore, "record_evaluation", fail_first_completion)
    case = next(
        case for case in load_dataset()[0].cases if case.case_id == "normal_ticket_open"
    )
    report = run_case(case, tmp_path / "case")
    assert failed == ["SUCCEEDED"]
    assert (
        report["task_status"] == "NEEDS_REVIEW" and report["contract_passed"] is False
    )
    assert report["verification_code"] == "completion_error"
    assert (
        read_evaluation(tmp_path / "case/tasks.sqlite3", UUID(report["task_id"]))
        == report
    )
