import json

import pytest

from safeharness.contracts import InvocationConflictError
from safeharness.database import execute
from safeharness.invocations import (
    InvocationLedger,
    digest,
    new_attempt,
)
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore


@pytest.fixture
def store(tmp_path):
    result = TaskStore(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def test_identity_chain_is_durable_and_artifacts_are_metadata_only(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    ledger = InvocationLedger(store.database, task.task_id)
    identity = ledger.start(
        "read_ticket", "tool", "runtime", digest({"ticket_id": "IT-001"})
    )
    ledger.finish(identity, "SUCCEEDED", "ticket_read", "private result")
    snapshot = InvocationLedger(store.database, task.task_id).snapshot()
    assert (
        len({task.task_id, identity.run_id, identity.call_id, identity.attempt_id}) == 4
    )
    assert snapshot["calls"][0]["run_id"] == str(identity.run_id)
    assert snapshot["attempts"][0]["call_id"] == str(identity.call_id)
    assert snapshot["artifacts"][0]["attempt_id"] == str(identity.attempt_id)
    assert "private result" not in json.dumps(snapshot)
    assert "content" not in snapshot["artifacts"][0]


def test_settled_result_can_be_confirmed_but_not_replaced(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    ledger = InvocationLedger(store.database, task.task_id)
    identity = ledger.start("read_file", "tool", "runtime")
    ledger.finish(identity, "SUCCEEDED", "completed", "original")
    ledger.finish(identity, "SUCCEEDED", "completed", "original")
    with pytest.raises(InvocationConflictError):
        ledger.finish(identity, "SUCCEEDED", "completed", "tampered")
    with pytest.raises(InvocationConflictError):
        ledger.finish(identity, "FAILED", "different")
    assert len(ledger.snapshot()["artifacts"]) == 1


def test_unknown_tool_attempt_cannot_be_blindly_retried(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    ledger = InvocationLedger(store.database, task.task_id)
    identity = ledger.start("write_file", "tool", "runtime")
    ledger.finish(identity, "UNKNOWN", "tool_outcome_unknown")
    with ledger.engine.begin() as connection, pytest.raises(InvocationConflictError):
        new_attempt(connection, task.task_id, identity.run_id, identity.call_id, "tool")


def test_cross_task_attempt_identity_is_rejected(store):
    first = store.transition(store.create(), TaskStatus.RUNNING)
    other = store.transition(store.create(), TaskStatus.RUNNING)
    ledger = InvocationLedger(store.database, first.task_id)
    identity = ledger.start("read_ticket", "tool", "runtime")
    with ledger.engine.begin() as connection, pytest.raises(InvocationConflictError):
        new_attempt(
            connection, other.task_id, identity.run_id, identity.call_id, "tool"
        )
    with pytest.raises(InvocationConflictError):
        InvocationLedger(store.database, other.task_id).finish(
            identity, "SUCCEEDED", "completed"
        )


def test_model_retry_uses_one_logical_call_and_distinct_attempts(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    ledger = InvocationLedger(store.database, task.task_id)
    first = ledger.start("deepseek-flash", "model", "model")
    ledger.finish(first, "FAILED", "provider_timeout")
    second = ledger.next_model_attempt(first)
    assert second.call_id == first.call_id and second.run_id == first.run_id
    assert second.attempt_id != first.attempt_id and second.attempt_index == 2
    ledger.finish(second, "SUCCEEDED", "model_completed")
    with pytest.raises(InvocationConflictError):
        ledger.next_model_attempt(second)


def test_artifact_limit_rolls_back_attempt_result(store):
    task = store.transition(store.create(), TaskStatus.RUNNING)
    ledger = InvocationLedger(store.database, task.task_id)
    identity = ledger.start("read_file", "tool", "runtime")
    with pytest.raises(InvocationConflictError):
        ledger.finish(identity, "SUCCEEDED", "completed", "x" * 262145)
    assert ledger.snapshot()["attempts"][0]["status"] == "INTENT"
    assert ledger.snapshot()["artifacts"] == []


def test_new_migration_does_not_fabricate_legacy_trace_identity(tmp_path):
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config

    from safeharness.database import engine_for

    location = tmp_path / "legacy.sqlite3"
    engine = engine_for(location)
    task_id = uuid4()
    with engine.begin() as connection:
        config = Config(attributes={"connection": connection})
        config.set_main_option("script_location", "safeharness:migrations")
        command.upgrade(config, "0009_service")
        execute(
            connection,
            "INSERT INTO tasks VALUES (?,?,?)",
            (task_id.hex, "SUCCEEDED", 3),
        )
        execute(
            connection,
            "INSERT INTO trace_events (schema_version,task_id,kind,payload_json,recorded_at) VALUES (1,?,?,?,?)",
            (task_id.hex, "task.created", "{}", "2026-11-11"),
        )
    TaskStore(location).initialize()
    with engine.connect() as connection:
        assert execute(connection, "SELECT count(*) FROM task_runs").scalar_one() == 0
        assert execute(connection, "SELECT run_id FROM trace_events").fetchone() == (
            None,
        )
        assert execute(
            connection, "SELECT task_id,status,revision FROM tasks"
        ).fetchone() == (task_id.hex, "SUCCEEDED", 3)
