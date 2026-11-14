import json
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy.exc import IntegrityError

from safeharness.audit import AuditLog
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore
from safeharness.trace import TraceLog


def test_unified_trace_orders_state_policy_intent_and_result(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = store.transition(store.create(), TaskStatus.RUNNING)
    gateway = ToolGateway(
        {"IT-001": "private-ticket-body"},
        frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    gateway.execute(ToolRequest("read_ticket", {"ticket_id": "IT-001"}))
    task = store.transition(task, TaskStatus.VERIFYING)
    store.transition(task, TaskStatus.SUCCEEDED)
    trace = TraceLog(store.database, task.task_id)
    events = trace.read()
    assert [event["kind"] for event in events] == [
        "task.created",
        "task.transition",
        "tool.policy",
        "tool.intent",
        "tool.result",
        "task.transition",
        "task.transition",
    ]
    assert len({event["sequence"] for event in events}) == len(events)
    assert (
        len(
            {
                event["payload"]["call_id"]
                for event in events
                if event["kind"].startswith("tool.")
            }
        )
        == 1
    )
    assert "private-ticket-body" not in trace.jsonl()
    assert 'arguments"' not in trace.jsonl()
    assert [json.loads(line) for line in trace.jsonl().splitlines()] == events


def test_trace_failure_rolls_back_state_transition(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = store.create()
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER refuse_trace BEFORE INSERT ON trace_events BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
    with pytest.raises(IntegrityError):
        store.transition(task, TaskStatus.RUNNING)
    assert store.get(task.task_id) == task


@pytest.mark.parametrize(
    "payload",
    [
        {"raw_prompt": "private"},
        {"code": "sk-" + "testsecret" * 4},
        {"code": {"secret": "value"}},
    ],
)
def test_trace_rejects_unregistered_and_sensitive_fields(tmp_path, payload):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = store.create()
    trace = TraceLog(store.database, task.task_id)
    with pytest.raises(ValueError):
        trace.emit("model.error", payload)
    assert len(trace.read()) == 1
