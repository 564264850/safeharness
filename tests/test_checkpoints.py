import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest

from safeharness.audit import AuditLog
from safeharness.checkpoints import CheckpointConflict, CheckpointSession
from safeharness.coding import CodingWorkspace
from safeharness.execution import ToolGateway, ToolRequest, ToolResult
from safeharness.invocations import InvocationLedger
from safeharness.runtime import FinalAnswer, run_task
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore


@pytest.fixture
def store(tmp_path):
    result = TaskStore(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def model(results):
    return (
        ToolRequest("read_ticket", {"ticket_id": "IT-001"})
        if len(results) < 2
        else FinalAnswer("done")
    )


def session(store, task, **options):
    return CheckpointSession(
        store,
        task.task_id,
        {"version": "test-v1", "scope": ["IT-001"]},
        model=model,
        stateless_model=True,
        **options,
    )


def gateway(store, task):
    return ToolGateway(
        {"IT-001": "open"},
        frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )


def test_pause_and_reconstruct_resume_preserves_results_and_call_ids(store):
    task = store.create()
    first = run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=5,
        store=store,
        task=task,
        checkpoint=session(store, task),
        pause_after=1,
    )
    assert first.run.stop_reason == "paused" and first.task.status is TaskStatus.RUNNING
    before = CheckpointSession.metadata(store, task.task_id)
    assert (
        before["state"] == "PAUSED"
        and before["checkpoints"][-1]["phase"] == "after_tool"
    )
    assert "content" not in json.dumps(before)
    second = run_task(
        model,
        gateway(store, task).execute,
        lambda r: len(r.tool_results) == 2,
        max_steps=5,
        store=store,
        checkpoint=session(store, task),
        resume=True,
    )
    assert second.task.status is TaskStatus.SUCCEEDED
    assert second.run.tool_results[0].call_id == first.run.tool_results[0].call_id
    assert len(InvocationLedger(store.database, task.task_id).snapshot()["calls"]) == 2
    metadata = CheckpointSession.metadata(store, task.task_id)
    assert metadata["generation"] == 2 and metadata["state"] == "FINISHED"
    assert {c["run_id"] for c in metadata["checkpoints"]} == {str(first.run_id)}


def test_step_budget_is_total_across_resume(store):
    task = store.create()
    run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=2,
        store=store,
        task=task,
        checkpoint=session(store, task),
        pause_after=1,
    )
    finished = run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=2,
        store=store,
        checkpoint=session(store, task),
        resume=True,
    )
    assert (
        finished.task.status is TaskStatus.FAILED
        and finished.run.stop_reason == "step_limit"
    )
    assert len(finished.run.tool_results) == 2


def test_configuration_change_refuses_resume_without_claiming_task(store):
    task = store.create()
    first = run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=5,
        store=store,
        task=task,
        checkpoint=session(store, task),
        pause_after=1,
    )
    changed = CheckpointSession(
        store, task.task_id, {"version": "different"}, model=model, stateless_model=True
    )
    with pytest.raises(CheckpointConflict, match="configuration_changed"):
        run_task(
            model,
            Mock(),
            lambda _: True,
            max_steps=5,
            store=store,
            checkpoint=changed,
            resume=True,
        )
    assert store.get(task.task_id) == first.task


def test_corrupted_checkpoint_never_reaches_tools(store):
    from safeharness.database import execute

    task = store.create()
    run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=5,
        store=store,
        task=task,
        checkpoint=session(store, task),
        pause_after=1,
    )
    with store._engine.begin() as connection:
        execute(
            connection,
            "UPDATE runtime_checkpoints SET content=? WHERE checkpoint_id=(SELECT checkpoint_id FROM runtime_sessions)",
            (b"tampered",),
        )
    tool = Mock()
    with pytest.raises(CheckpointConflict, match="integrity_error"):
        run_task(
            model,
            tool,
            lambda _: True,
            max_steps=5,
            store=store,
            checkpoint=session(store, task),
            resume=True,
        )
    tool.assert_not_called()


def test_unconfirmed_tool_checkpoint_quarantines_instead_of_replaying(store):
    class ProcessLoss(BaseException):
        pass

    task = store.create()
    executed = Mock(side_effect=ProcessLoss)
    with pytest.raises(ProcessLoss):
        run_task(
            model,
            executed,
            lambda _: True,
            max_steps=5,
            store=store,
            task=task,
            checkpoint=session(store, task),
        )
    assert executed.call_count == 1
    recovered = run_task(
        model,
        executed,
        lambda _: True,
        max_steps=5,
        store=store,
        checkpoint=session(store, task),
        resume=True,
        recover_abandoned=True,
        expected_generation=1,
    )
    assert recovered.task.status is TaskStatus.NEEDS_REVIEW
    assert recovered.run.error_code == "checkpoint_tool_outcome_unconfirmed"
    assert executed.call_count == 1


def test_checkpoint_restores_coding_snapshot_without_repeating_write(store):
    task = store.create()
    initial = {"solution.py": "old"}
    workspace = CodingWorkspace(initial, task.task_id)

    def actions(results):
        if not results:
            return ToolRequest("write_file", {"path": "solution.py", "content": "new"})
        return FinalAnswer("done")

    def write(_):
        workspace._files["solution.py"] = "new"  # host-side synthetic adapter only
        return ToolResult("write_file", "ok", "file_written")

    check = CheckpointSession(
        store,
        task.task_id,
        {"fixture": "v1"},
        model=actions,
        stateless_model=True,
        workspace=workspace,
    )
    run_task(
        actions,
        write,
        lambda _: True,
        max_steps=3,
        store=store,
        task=task,
        checkpoint=check,
        pause_after=1,
    )
    restored = CodingWorkspace(initial, task.task_id)
    check = CheckpointSession(
        store,
        task.task_id,
        {"fixture": "v1"},
        model=actions,
        stateless_model=True,
        workspace=restored,
    )
    execute = Mock()
    done = run_task(
        actions,
        execute,
        lambda _: restored.files["solution.py"] == "new",
        max_steps=3,
        store=store,
        checkpoint=check,
        resume=True,
    )
    assert done.task.status is TaskStatus.SUCCEEDED
    execute.assert_not_called()


def test_two_resume_claims_cannot_own_same_generation(store):
    task = store.create()
    run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=5,
        store=store,
        task=task,
        checkpoint=session(store, task),
        pause_after=1,
    )
    barrier = Barrier(2)

    def acquire(_):
        barrier.wait()
        try:
            return session(store, task).acquire(max_steps=5).revision
        except CheckpointConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, range(2)))
    assert sum(v is not None for v in results) == 1


def test_abandoned_owner_requires_explicit_generation_and_fences_old_owner(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    old = session(store, task)
    old.begin(task, 5)
    with pytest.raises(CheckpointConflict, match="runner_active"):
        session(store, task).acquire(max_steps=5)
    newer = session(store, task)
    claimed = newer.acquire(max_steps=5, recover_abandoned=True, expected_generation=1)
    with pytest.raises(CheckpointConflict, match="runner_fenced"):
        old.save(task, "before_model", (), 1)
    with pytest.raises(CheckpointConflict, match="generation_changed"):
        session(store, task).acquire(
            max_steps=5, recover_abandoned=True, expected_generation=1
        )
    assert claimed.revision == task.revision + 1
    from safeharness.task_store import TaskConflictError

    with pytest.raises(TaskConflictError):
        store.transition(task, TaskStatus.VERIFYING)


def test_deadline_is_not_reset_by_new_session_instance(store):
    from safeharness.database import execute

    task = store.create()
    run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=5,
        store=store,
        task=task,
        checkpoint=session(store, task),
        pause_after=1,
    )
    with store._engine.begin() as connection:
        execute(connection, "UPDATE runtime_sessions SET deadline_at=0")
    tool = Mock()
    done = run_task(
        model,
        tool,
        lambda _: True,
        max_steps=5,
        store=store,
        checkpoint=session(store, task, timeout_seconds=3600),
        resume=True,
    )
    assert (
        done.task.status is TaskStatus.FAILED
        and done.run.error_code == "checkpoint_deadline_exceeded"
    )
    tool.assert_not_called()


def test_verification_phase_can_resume_without_another_model_call(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    original = session(store, task)
    original.begin(task, 5)
    original.save(task, "final", (), 1, FinalAnswer("ready"))
    store.transition(task, TaskStatus.VERIFYING)
    execute = Mock()
    restored = run_task(
        model,
        execute,
        lambda _: True,
        max_steps=5,
        store=store,
        checkpoint=session(store, task),
        resume=True,
        recover_abandoned=True,
        expected_generation=1,
    )
    assert restored.task.status is TaskStatus.SUCCEEDED
    execute.assert_not_called()


def test_failed_checkpoint_after_tool_does_not_report_execution_as_safely_recoverable(
    store, monkeypatch
):
    monkeypatch.setattr("safeharness.checkpoints.MAX_CHECKPOINT_BYTES", 1000)
    task = store.create()
    executed = Mock(
        return_value=ToolResult("read_ticket", "ok", "ticket_read", "x" * 2000)
    )
    done = run_task(
        model,
        executed,
        lambda _: True,
        max_steps=5,
        store=store,
        task=task,
        checkpoint=session(store, task),
    )
    assert done.task.status is TaskStatus.NEEDS_REVIEW
    assert done.run.error_code == "checkpoint_byte_limit" and executed.call_count == 1
