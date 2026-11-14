import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from safeharness.agent import ToolCallingAgent
from safeharness.approval import ApprovalActor, ApprovalOutcome
from safeharness.audit import AuditLog
from safeharness.checkpoints import CheckpointConflict, CheckpointSession
from safeharness.execution import ToolGateway, ToolRequest, ToolResult
from safeharness.harness import Harness
from safeharness.invocations import InvocationLedger
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.trace import TraceLog


class ProcessLoss(BaseException):
    pass


@pytest.fixture
def setup(tmp_path):
    store = TaskStore(tmp_path / "control.sqlite3")
    store.initialize()
    business = TicketStore(store.database)
    business.initialize()
    task = store.create()
    verifier = Mock(return_value=True)

    def make(*, two_writes=False, **options):
        class Backend:
            def complete(self, messages, **kwargs):
                if any(m["role"] == "tool" for m in messages):
                    assert all(
                        json.loads(m["content"])["status"] == "ok"
                        for m in messages
                        if m["role"] == "tool"
                    )
                    return {
                        "role": "assistant",
                        "content": "Observed the approved updates and read results.",
                    }
                actions = [
                    (
                        "update_ticket_status",
                        {"ticket_id": "IT-001", "status": "in_progress"},
                    ),
                    ("read_ticket", {"ticket_id": "IT-001"}),
                ]
                if two_writes:
                    actions.append(
                        (
                            "update_ticket_status",
                            {"ticket_id": "IT-001", "status": "resolved"},
                        )
                    )
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(i),
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (name, args) in enumerate(actions)
                    ],
                }

        model = ToolCallingAgent(
            Backend(),
            instruction="Update, read, and summarize.",
            environment="ticket",
            scope={"read": ["IT-001"], "write": ["IT-001"]},
            task_id=task.task_id,
            trace=TraceLog(store.database, task.task_id),
            deadline=time.monotonic() + 120,
        )
        gateway = ToolGateway(
            allowed_tickets=frozenset({"IT-001"}),
            writable_tickets=frozenset({"IT-001"}),
            ticket_reader=lambda _: business.ticket()[0],
            audit=AuditLog(store.database, task.task_id),
        )
        values = {
            "model": model,
            "execute": gateway.execute,
            "verify": verifier,
            "configuration": {"app": "approval-loop-test"},
            "approval_workspace": business,
            "max_steps": 8,
        }
        values.update(options)
        return Harness(store, **values)

    def approve():
        request = store.get_approval(task.task_id).request
        store.decide_approval(
            request.approval_id,
            ApprovalOutcome.APPROVED,
            request.intent.arguments_sha256,
            actor=ApprovalActor("synthetic-reviewer", can_approve=True),
        )
        return request

    return store, business, task, make, approve, verifier


def test_approval_restores_queue_and_runs_final_verifier(setup):
    store, business, task, make, approve, verifier = setup
    first = make().run(task)
    assert first.task.status is TaskStatus.WAITING_APPROVAL and business.ticket() == (
        "open",
        0,
    )
    verifier.assert_not_called()
    proposal = first.run.tool_results[-1]
    approve()
    done = make().resume(task.task_id)
    assert done.task.status is TaskStatus.SUCCEEDED
    assert [r.name for r in done.run.tool_results] == [
        "update_ticket_status",
        "read_ticket",
    ]
    assert done.run.tool_results[0].call_id == proposal.call_id
    assert done.run.tool_results[0].attempt_id != proposal.attempt_id
    assert done.run.tool_results[1].value == "in_progress"
    assert business.ticket() == ("in_progress", 1)
    verifier.assert_called_once()
    assert CheckpointSession.metadata(store, task.task_id)["state"] == "FINISHED"
    assert [
        e["payload"]["passed"]
        for e in TraceLog(store.database, task.task_id).read()
        if e["kind"] == "runtime.verified"
    ] == [True]


def test_two_separate_approvals_are_not_reused_for_later_write(setup):
    store, business, task, make, approve, verifier = setup
    make(two_writes=True).run(task)
    first = approve()
    middle = make(two_writes=True).resume(task.task_id)
    assert middle.task.status is TaskStatus.WAITING_APPROVAL
    assert business.ticket() == ("in_progress", 1)
    verifier.assert_not_called()
    second = approve()
    assert second.approval_id != first.approval_id
    assert (
        make(two_writes=True).resume(task.task_id).task.status is TaskStatus.SUCCEEDED
    )
    assert business.ticket() == ("resolved", 2)
    with sqlite3.connect(store.database) as connection:
        assert connection.execute(
            "SELECT sequence FROM pending_approvals ORDER BY sequence"
        ).fetchall() == [(1,), (2,)]
    writes = [
        c
        for c in InvocationLedger(store.database, task.task_id).snapshot()["calls"]
        if c["tool_name"] == "update_ticket_status"
    ]
    assert len(writes) == 2 and {c["operation_id"] for c in writes} == {
        str(first.approval_id),
        str(second.approval_id),
    }


def test_unapproved_continue_does_not_claim_or_dispatch(setup):
    store, business, task, make, _, verifier = setup
    make().run(task)
    with pytest.raises(CheckpointConflict, match="approval_not_approved"):
        make().resume(task.task_id)
    assert business.ticket() == ("open", 0)
    assert CheckpointSession.metadata(store, task.task_id)["generation"] == 1
    verifier.assert_not_called()


def test_rejection_never_dispatches_remaining_queue(setup):
    store, business, task, make, _, verifier = setup
    make().run(task)
    request = store.get_approval(task.task_id).request
    store.decide_approval(
        request.approval_id,
        ApprovalOutcome.REJECTED,
        request.intent.arguments_sha256,
        actor=ApprovalActor("synthetic-reviewer", can_approve=True),
    )
    with pytest.raises(CheckpointConflict):
        make().resume(task.task_id)
    assert store.get(
        task.task_id
    ).status is TaskStatus.FAILED and business.ticket() == ("open", 0)
    verifier.assert_not_called()


@pytest.mark.parametrize("boundary", ["business_commit", "checkpoint_save"])
def test_approval_crash_reconciles_and_continues_without_double_write(
    setup, monkeypatch, boundary
):
    _store, business, task, make, approve, verifier = setup
    make().run(task)
    approve()

    def crash():
        raise ProcessLoss()

    original = CheckpointSession.save
    if boundary == "checkpoint_save":

        def save(self, task, phase, results, steps, action=None):
            if results and results[-1].code == "approved_tool_executed":
                raise ProcessLoss()
            return original(self, task, phase, results, steps, action)

        monkeypatch.setattr(CheckpointSession, "save", save)
    with pytest.raises(ProcessLoss):
        make().resume(
            task.task_id,
            after_business_commit=crash if boundary == "business_commit" else None,
        )
    assert business.ticket() == ("in_progress", 1)
    monkeypatch.setattr(CheckpointSession, "save", original)
    done = make().resume(task.task_id, recover_abandoned=True, expected_generation=2)
    assert done.task.status is TaskStatus.SUCCEEDED and business.ticket() == (
        "in_progress",
        1,
    )
    assert done.run.tool_results[1].value == "in_progress"
    verifier.assert_called_once()


def test_approval_wait_does_not_reset_steps_or_charge_active_time(setup, monkeypatch):
    _store, business, task, make, approve, _ = setup
    make(max_steps=2).run(task)
    approve()
    now = time.time()
    monkeypatch.setattr("safeharness.checkpoints.time.time", lambda: now + 600)
    done = make(max_steps=2).resume(task.task_id)
    assert (
        done.task.status is TaskStatus.FAILED and done.run.stop_reason == "step_limit"
    )
    assert business.ticket() == ("in_progress", 1)
    assert len(done.run.tool_results) == 2


def test_final_verification_can_fail_after_successful_approved_write(setup):
    _store, business, task, make, approve, _ = setup
    make().run(task)
    approve()
    done = make(verify=lambda _: False).resume(task.task_id)
    assert done.task.status is TaskStatus.FAILED and business.ticket() == (
        "in_progress",
        1,
    )
    assert len(done.run.tool_results) == 2


def test_concurrent_approval_continuations_have_one_winner(setup):
    _, business, task, make, approve, verifier = setup
    make().run(task)
    approve()

    def resume(_):
        try:
            return make().resume(task.task_id).task.status.value
        except CheckpointConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(resume, range(2))) == ["SUCCEEDED", "conflict"]
    assert business.ticket() == ("in_progress", 1)
    verifier.assert_called_once()


def test_changed_configuration_blocks_approval_before_business_write(setup):
    _, business, task, make, approve, _ = setup
    make().run(task)
    approve()
    with pytest.raises(CheckpointConflict, match="configuration_changed"):
        make(configuration={"app": "changed"}).resume(task.task_id)
    assert business.ticket() == ("open", 0)


def test_repeated_failure_guard_is_cumulative_across_pause(setup):
    store, _, task, _, _, _ = setup

    def create():
        return Harness(
            store,
            model=lambda _: ToolRequest("unknown_tool", {}),
            execute=lambda _: ToolResult("unknown_tool", "denied", "unknown_tool"),
            verify=lambda _: True,
            configuration={"app": "no-progress"},
            stateless_model=True,
            max_steps=10,
        )

    assert create().run(task, pause_after=2).run.stop_reason == "paused"
    done = create().resume(task.task_id)
    assert (
        done.task.status is TaskStatus.FAILED
        and done.run.error_code == "repeated_tool_failure"
    )
    assert len(done.run.tool_results) == 3


def test_crash_after_approval_persist_before_owner_release_keeps_wait_budget(
    setup, monkeypatch
):
    store, business, task, make, approve, verifier = setup
    original = CheckpointSession.release

    def crash(self, state):
        if state == "WAITING_APPROVAL":
            raise ProcessLoss()
        return original(self, state)

    monkeypatch.setattr(CheckpointSession, "release", crash)
    with pytest.raises(ProcessLoss):
        make().run(task)
    assert store.get(task.task_id).status is TaskStatus.WAITING_APPROVAL
    assert (
        CheckpointSession.metadata(store, task.task_id)["state"] == "WAITING_APPROVAL"
    )
    approve()
    now = time.time()
    monkeypatch.setattr(CheckpointSession, "release", original)
    monkeypatch.setattr("safeharness.checkpoints.time.time", lambda: now + 600)
    done = make().resume(task.task_id, recover_abandoned=True, expected_generation=1)
    assert done.task.status is TaskStatus.SUCCEEDED and business.ticket() == (
        "in_progress",
        1,
    )
    verifier.assert_called_once()
