import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event

pytest.importorskip("fastapi")
from test_durable_submission import store as shared_store_fixture
from test_postgres import pg_store as shared_pg_fixture
from test_service import principals

from safeharness.audit import AuditError, AuditLog
from safeharness.database import execute
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.invocations import InvocationLedger
from safeharness.pg_ticket_store import PostgresTicketStore
from safeharness.scheduling import (
    CURRENT_CLAIM,
    QueueClaimError,
    claim_next,
    claim_scope,
)
from safeharness.task import TaskStatus
from safeharness.task_store import TaskConflictError, TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.trace import TraceLog
from safeharness.worker import QueueWorker

REQUEST = {"scenario": "ticket_read", "execution_mode": "queued"}
store = shared_store_fixture
pg_store = shared_pg_fixture


def business(store):
    return (
        TicketStore(store.database)
        if store._engine.dialect.name == "sqlite"
        else PostgresTicketStore(store.database)
    )


def worker(store, actors=None):
    return QueueWorker(
        store,
        business(store),
        principals() if actors is None else actors,
        worker_id="test-worker",
    )


def test_worker_uses_harness_and_commits_terminal_queue_with_task(store):
    task, _ = store.submit("alice", REQUEST, "worker-key")
    result = worker(store).run_once()
    assert (
        result["task_id"] == str(task.task_id) and result["task_status"] == "SUCCEEDED"
    )
    assert (
        result["scheduling"]["state"] == "FINISHED"
        and result["scheduling"]["generation"] == 1
    )
    ledger = InvocationLedger(store.database, task.task_id).snapshot()
    assert (
        len(ledger["calls"]) == 1 and ledger["calls"][0]["tool_name"] == "read_ticket"
    )
    with store._engine.connect() as connection:
        queue = execute(
            connection,
            "SELECT claim_token,generation FROM task_queue WHERE task_id=?",
            (task.task_id.hex,),
        ).fetchone()
        session = execute(
            connection,
            "SELECT owner_token,generation FROM runtime_sessions WHERE task_id=?",
            (task.task_id.hex,),
        ).fetchone()
        assert session[0] is None and session[1] == queue[1]
        assert queue[0] not in json.dumps(result)
    assert CURRENT_CLAIM.get() is None and worker(store).run_once()["status"] == "idle"
    assert (
        store.submit("alice", REQUEST, "worker-key")[0].status is TaskStatus.SUCCEEDED
    )


def test_claim_releases_db_lock_and_next_task_is_available(store):
    tasks = [store.submit("alice", REQUEST)[0] for _ in range(2)]
    claim = claim_next(store, "one")
    with store._engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            execute(
                connection,
                "SELECT task_id FROM tasks WHERE task_id=? FOR UPDATE NOWAIT",
                (claim.task_id.hex,),
            ).fetchone()
    second = claim_next(store, "two")
    assert {claim.task_id, second.task_id} == {t.task_id for t in tasks}
    assert claim_next(store, "three") is None


def test_two_workers_drain_batch_without_claiming_manual_or_cancelled_tasks(store):
    jobs = {store.submit("alice", REQUEST)[0].task_id for _ in range(8)}
    manual = store.create(owner_id="alice", request={"scenario": "ticket_read"})
    cancelled = store.submit("alice", REQUEST)[0]
    store.cancel(cancelled.task_id, None)
    consumers = [worker(store), worker(store)]

    def drain(consumer):
        completed = []
        while True:
            result = consumer.run_once()
            if result["status"] == "idle":
                return completed
            assert result["task_status"] == "SUCCEEDED"
            completed.append(result["task_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = [
            identifier for batch in pool.map(drain, consumers) for identifier in batch
        ]
    assert len(completed) == len(set(completed)) == len(jobs)
    assert set(completed) == {str(task_id) for task_id in jobs}
    assert store.get(manual.task_id).status is TaskStatus.CREATED
    assert store.get(cancelled.task_id).status is TaskStatus.CANCELLED


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_POSTGRES_TESTS") != "1",
    reason="Enable PostgreSQL locking test",
)
def test_pg_locked_oldest_task_does_not_block_other_claims(pg_store):
    store, _ = pg_store
    first = store.submit("alice", REQUEST)[0]
    second = store.submit("alice", REQUEST)[0]
    with store._engine.begin() as connection:
        execute(
            connection,
            "UPDATE task_queue SET created_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (first.task_id.hex,),
        )
    with store._engine.begin() as connection:
        execute(
            connection,
            "SELECT task_id FROM tasks WHERE task_id=? FOR UPDATE",
            (first.task_id.hex,),
        ).fetchone()
        assert claim_next(store, "skip-locked").task_id == second.task_id
    assert claim_next(store, "oldest").task_id == first.task_id


def test_cancel_before_and_after_claim_prevents_dispatch(store):
    w = worker(store)
    task, _ = store.submit("alice", REQUEST)
    store.cancel(task.task_id, w.service.business)
    assert w.run_once()["status"] == "idle"
    task, _ = store.submit("alice", REQUEST)

    def cancelled_claim(*args, **kwargs):
        claim = claim_next(*args, **kwargs)
        store.cancel(claim.task_id, w.service.business)
        return claim

    with patch("safeharness.worker.claim_next", side_effect=cancelled_claim):
        assert w.run_once()["task_status"] == "CANCELLED"
    assert store.scheduling(task.task_id)["state"] == "CANCELLED"
    assert InvocationLedger(store.database, task.task_id).snapshot()["calls"] == []


@pytest.mark.parametrize(
    "authorization", ["missing_owner", "revoked_role", "revoked_scope"]
)
def test_worker_rechecks_owner_and_scope_before_dispatch(store, authorization):
    task, _ = store.submit("alice", REQUEST)
    actors = principals()
    if authorization == "missing_owner":
        actors = [p for p in actors if p.subject != "alice"]
    elif authorization == "revoked_role":
        actors = [
            replace(p, roles=frozenset()) if p.subject == "alice" else p for p in actors
        ]
    else:
        actors = [
            replace(p, readable_tickets=frozenset()) if p.subject == "alice" else p
            for p in actors
        ]
    assert worker(store, actors).run_once()["task_status"] == "FAILED"
    assert store.scheduling(task.task_id)["state"] == "FINISHED"
    if authorization != "revoked_scope":
        assert InvocationLedger(store.database, task.task_id).snapshot()["calls"] == []
    assert worker(store).run_once()["status"] == "idle"


@pytest.mark.parametrize("credential", ["missing", "token", "generation"])
def test_unowned_or_forged_claim_cannot_transition_or_dispatch(store, credential):
    task, _ = store.submit("alice", REQUEST)
    claim = claim_next(store, "legitimate")
    fake = (
        None
        if credential == "missing"
        else replace(
            claim,
            **({"token": "0" * 32} if credential == "token" else {"generation": 2}),
        )
    )
    with claim_scope(fake), pytest.raises(TaskConflictError, match="worker claim"):
        store.transition(task, TaskStatus.RUNNING)
    with claim_scope(claim):
        store.transition(task, TaskStatus.RUNNING)
    gateway = ToolGateway(
        {"IT-001": "open"},
        frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    with claim_scope(fake), pytest.raises(AuditError):
        gateway.execute(ToolRequest("read_ticket", {"ticket_id": "IT-001"}))
    assert InvocationLedger(store.database, task.task_id).snapshot()["calls"] == []


def test_claim_and_finish_events_are_atomic_with_queue_changes(store):
    task, _ = store.submit("alice", REQUEST)
    fail_kind = "scheduling.claimed"

    def reject(_, __, statement, parameters, *args):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if "insert into trace_events" in statement.lower() and fail_kind in values:
            raise RuntimeError("injected scheduler event failure")

    event.listen(store._engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError):
            claim_next(store, "one")
        assert store.scheduling(task.task_id)["state"] == "QUEUED"
    finally:
        event.remove(store._engine, "before_cursor_execute", reject)
    claim = claim_next(store, "two")
    with claim_scope(claim):
        running = store.transition(task, TaskStatus.RUNNING)
        verifying = store.transition(running, TaskStatus.VERIFYING)
        fail_kind = "scheduling.finished"
        event.listen(store._engine, "before_cursor_execute", reject)
        try:
            with pytest.raises(RuntimeError):
                store.transition(verifying, TaskStatus.SUCCEEDED)
        finally:
            event.remove(store._engine, "before_cursor_execute", reject)
        assert (
            store.get(task.task_id) == verifying
            and store.scheduling(task.task_id)["state"] == "CLAIMED"
        )
        store.transition(verifying, TaskStatus.SUCCEEDED)
    assert store.scheduling(task.task_id)["state"] == "FINISHED"


def child_env(store):
    database = store.database
    return {
        **os.environ,
        "SH_S2_DATABASE": database.render_as_string(hide_password=False)
        if hasattr(database, "render_as_string")
        else str(database),
    }


def test_two_real_workers_process_one_task_once(store, tmp_path):
    task, _ = store.submit("alice", REQUEST)
    business(store).initialize()
    code = """
import json,os,time
from pathlib import Path
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.pg_ticket_store import PostgresTicketStore
from safeharness.service import Principal
from safeharness.worker import QueueWorker
s=TaskStore(os.environ['SH_S2_DATABASE'])
b=TicketStore(s.database) if s._engine.dialect.name=='sqlite' else PostgresTicketStore(s.database)
w=QueueWorker(s,b,[Principal('alice','unused',frozenset({'requester'}))],worker_id='worker-'+os.environ['SH_S2_INDEX'])
p=Path(os.environ['SH_S2_GATE']);(p/('ready-'+os.environ['SH_S2_INDEX'])).touch()
deadline=time.monotonic()+15
while not (p/'go').exists():
    if time.monotonic()>deadline:raise SystemExit(9)
    time.sleep(.01)
print(json.dumps(w.run_once()))
"""
    children = []
    try:
        for i in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    env={
                        **child_env(store),
                        "SH_S2_INDEX": str(i),
                        "SH_S2_GATE": str(tmp_path),
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        deadline = time.monotonic() + 15
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline and all(
                p.poll() is None for p in children
            )
            time.sleep(0.01)
        (tmp_path / "go").touch()
        results = []
        for child in children:
            stdout, _ = child.communicate(timeout=30)
            assert child.returncode == 0
            results.append(json.loads(stdout))
        assert sorted(r["status"] for r in results) == ["idle", "processed"]
        assert store.get(task.task_id).status is TaskStatus.SUCCEEDED
        events = TraceLog(store.database, task.task_id).read()
        assert sum(e["kind"] == "scheduling.claimed" for e in events) == 1
        assert (
            len(InvocationLedger(store.database, task.task_id).snapshot()["calls"]) == 1
        )
        assert business(store).ticket() == ("open", 0)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_crashed_claim_is_not_blindly_reclaimed_in_s2(store):
    task, _ = store.submit("alice", REQUEST)
    code = """
import os
from safeharness.task_store import TaskStore
from safeharness.scheduling import claim_next
assert claim_next(TaskStore(os.environ['SH_S2_DATABASE']),'crashed') is not None
os._exit(77)
"""
    child = subprocess.run(
        [sys.executable, "-c", code],
        env=child_env(store),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert (
        child.returncode == 77 and store.scheduling(task.task_id)["state"] == "CLAIMED"
    )
    assert (
        worker(store).run_once()["status"] == "idle"
        and store.get(task.task_id).status is TaskStatus.CREATED
    )


def test_worker_does_not_report_unfinished_task_as_processed(store):
    task, _ = store.submit("alice", REQUEST)
    w = worker(store)
    with (
        # Return the actual post-claim snapshot while deliberately doing no work.
        # Capturing it before run_once would omit the newly assigned generation.
        patch.object(
            w.service,
            "run",
            side_effect=lambda *a, **k: w.service.snapshot(task.task_id),
        ),
        pytest.raises(QueueClaimError, match="unfinished task"),
    ):
        w.run_once()
    assert store.scheduling(task.task_id)["state"] == "CLAIMED"


def test_s1_migration_preserves_queued_cancelled_and_manual_tasks(tmp_path):
    store = TaskStore(tmp_path / "legacy.sqlite3")
    with store._engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        config = Config(attributes={"connection": connection})
        config.set_main_option("script_location", "safeharness:migrations")
        command.upgrade(config, "0013_durable_queue")
    queued = store.create(owner_id="alice", request=REQUEST)
    cancelled = store.create(owner_id="alice", request=REQUEST)
    manual = store.create(owner_id="alice", request={"scenario": "ticket_read"})
    with store._engine.begin() as connection:
        execute(
            connection,
            "UPDATE tasks SET status='CANCELLED',revision=1 WHERE task_id=?",
            (cancelled.task_id.hex,),
        )
        execute(
            connection,
            "UPDATE task_queue SET state='CANCELLED' WHERE task_id=?",
            (cancelled.task_id.hex,),
        )
    store.initialize()
    assert worker(store).run_once()["task_id"] == str(queued.task_id)
    assert store.get(cancelled.task_id).status is TaskStatus.CANCELLED
    assert (
        store.scheduling(manual.task_id) is None
        and store.get(manual.task_id).status is TaskStatus.CREATED
    )
