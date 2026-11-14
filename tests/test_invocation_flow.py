from uuid import UUID, uuid4

import pytest

from safeharness.approval import ApprovalActor, ApprovalOutcome
from safeharness.audit import AuditError, AuditLog
from safeharness.contracts import CURRENT_CALL
from safeharness.execution import ToolGateway, ToolRequest, ToolResult
from safeharness.invocations import InvocationLedger
from safeharness.runtime import FinalAnswer, run_task
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.trace import TraceLog


@pytest.fixture
def store(tmp_path):
    result = TaskStore(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def proposal(store):
    task = store.create()
    gateway = ToolGateway(
        {},
        writable_tickets=frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    result = run_task(
        lambda _: ToolRequest(
            "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
        ),
        gateway.execute,
        lambda _: False,
        max_steps=1,
        task=task,
        store=store,
    )
    request = store.get_approval(task.task_id).request
    store.decide_approval(
        request.approval_id,
        ApprovalOutcome.APPROVED,
        request.intent.arguments_sha256,
        actor=ApprovalActor("reviewer", can_approve=True),
    )
    return result, request


def test_runtime_gateway_and_trace_share_one_host_identity(store):
    task = store.create()
    gateway = ToolGateway(
        {"IT-001": "open"},
        allowed_tickets=frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    result = run_task(
        lambda r: (
            FinalAnswer("done")
            if r
            else ToolRequest("read_ticket", {"ticket_id": "IT-001"})
        ),
        gateway.execute,
        lambda _: True,
        max_steps=2,
        store=store,
        task=task,
    )
    assert result.task.status is TaskStatus.SUCCEEDED
    ledger = InvocationLedger(store.database, task.task_id).snapshot()
    assert (
        len(ledger["calls"]) == len(ledger["attempts"]) == len(ledger["artifacts"]) == 1
    )
    call, attempt = ledger["calls"][0], ledger["attempts"][0]
    assert (
        result.run_id
        == result.run.tool_results[0].run_id
        == UUID(ledger["runs"][0]["run_id"])
    )
    assert ledger["runs"][0]["status"] == "SUCCEEDED"
    assert (
        call["call_id"] == str(result.run.tool_results[0].call_id) == attempt["call_id"]
    )
    for event in TraceLog(store.database, task.task_id).read():
        if event["kind"].startswith("tool."):
            assert event["payload"]["call_id"] == call["call_id"]
            assert event["payload"]["attempt_id"] == attempt["attempt_id"]
            assert event["run_id"] == call["run_id"]


def test_callback_returned_ids_are_not_authority(store):
    task = store.create()
    forged = uuid4()
    result = run_task(
        lambda r: FinalAnswer("done") if r else ToolRequest("read_ticket", {}),
        lambda _: ToolResult(
            "read_ticket",
            "ok",
            "ticket_read",
            call_id=forged,
            run_id=forged,
            attempt_id=forged,
        ),
        lambda _: True,
        max_steps=2,
        store=store,
        task=task,
    )
    recorded = result.run.tool_results[0]
    assert forged not in {recorded.call_id, recorded.run_id, recorded.attempt_id}
    assert CURRENT_CALL.get() is None


def test_approval_recovery_keeps_call_and_operation_but_changes_attempt(store):
    result, request = proposal(store)
    ledger = InvocationLedger(store.database, result.task.task_id)
    before = ledger.snapshot()
    assert before["calls"][0]["operation_id"] == str(request.approval_id)
    assert before["attempts"][0]["status"] == "WAITING_APPROVAL"
    business = TicketStore(store.database)

    def crash():
        raise RuntimeError("simulated process loss after business commit")

    with pytest.raises(RuntimeError):
        store.resume(result.task.task_id, business, after_business_commit=crash)
    assert business.ticket() == ("resolved", 1)
    assert store.get(result.task.task_id).status is TaskStatus.RUNNING
    assert store.resume(result.task.task_id, business).status is TaskStatus.SUCCEEDED
    after = ledger.snapshot()
    assert len(after["calls"]) == 1 and len(after["attempts"]) == 3
    assert after["calls"][0]["call_id"] == before["calls"][0]["call_id"]
    assert after["calls"][0]["operation_id"] == str(request.approval_id)
    assert {a["status"] for a in after["attempts"]} == {
        "WAITING_APPROVAL",
        "RECONCILED",
        "SUCCEEDED",
    }
    assert len({a["attempt_id"] for a in after["attempts"]}) == 3
    assert sorted(r["status"] for r in after["runs"]) == [
        "SUCCEEDED",
        "WAITING_APPROVAL",
    ]
    assert len({a["run_id"] for a in after["attempts"] if a["kind"] == "business"}) == 1
    assert before["attempts"][0]["run_id"] != next(
        a["run_id"] for a in after["attempts"] if a["kind"] == "business"
    )
    assert business.ticket() == ("resolved", 1)
    store.resume(result.task.task_id, business)
    assert ledger.snapshot() == after


def test_attempt_cannot_register_dispatch_twice_even_before_result(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    ledger = InvocationLedger(store.database, task.task_id)
    identity = ledger.start("read_ticket", "tool", "runtime")
    audit = AuditLog(store.database, task.task_id)
    token = CURRENT_CALL.set(identity)
    try:
        fields = {
            "tool": "read_ticket",
            "version": 1,
            "policy": "mock-ticket-v1",
            "digest": "f" * 64,
            "resources": ("IT-001",),
            "decision": "ALLOW",
            "code": "authorized",
        }
        audit.begin(**fields)
        with pytest.raises(AuditError, match="before dispatch"):
            audit.begin(**fields)
    finally:
        CURRENT_CALL.reset(token)
    assert sum(e["phase"] == "INTENT" for e in audit.read()) == 1


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_abandoned_approval_closes_call_without_business_attempt(store, decision):
    result, request = proposal(store)
    # proposal already approved; cancellation remains legal before dispatch.
    if decision == "reject":
        # Use a fresh undecided request for the reject branch.
        task = store.transition(store.create(), TaskStatus.RUNNING)
        task, request = store.pause_for_approval(task, request.intent)
        store.decide_approval(
            request.approval_id,
            ApprovalOutcome.REJECTED,
            request.intent.arguments_sha256,
            actor=ApprovalActor("reviewer", can_approve=True),
        )
    else:
        task = store.cancel(result.task.task_id, TicketStore(store.database))
    data = InvocationLedger(store.database, task.task_id).snapshot()
    assert data["calls"][0]["status"] == (
        "FAILED" if decision == "reject" else "CANCELLED"
    )
    assert not any(a["kind"] == "business" for a in data["attempts"])
    assert not any(r["status"] == "RUNNING" for r in data["runs"])


def test_invocation_prepare_failure_converges_without_dispatch(store, monkeypatch):
    from unittest.mock import Mock

    execute = Mock()
    monkeypatch.setattr(
        InvocationLedger,
        "active_run_id",
        Mock(side_effect=RuntimeError("storage unavailable")),
    )
    result = run_task(
        lambda _: ToolRequest("read_ticket", {}),
        execute,
        lambda _: True,
        max_steps=1,
        store=store,
    )
    execute.assert_not_called()
    assert result.task.status is TaskStatus.NEEDS_REVIEW
    assert store.get(result.task.task_id) == result.task
    assert not any(
        r["status"] == "RUNNING"
        for r in InvocationLedger(store.database, result.task.task_id).snapshot()[
            "runs"
        ]
    )
