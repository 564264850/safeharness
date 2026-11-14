import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import pytest
from sqlalchemy import event

pytest.importorskip("fastapi")
from test_durable_submission import store as shared_store_fixture
from test_postgres import pg_store as shared_pg_fixture
from test_queue_leases import clock as shared_clock_fixture
from test_queue_worker import business, child_env
from test_service import principals

from safeharness.checkpoints import CheckpointSession
from safeharness.database import execute
from safeharness.invocations import InvocationLedger
from safeharness.recovery import scan_expired
from safeharness.scheduling import QueueClaimError, claim_next, claim_scope, renew_claim
from safeharness.task import TaskStatus
from safeharness.trace import TraceLog
from safeharness.worker import QueueWorker

store = shared_store_fixture
pg_store = shared_pg_fixture
base_clock = shared_clock_fixture
REQUEST = {"scenario": "ticket_read", "execution_mode": "queued"}


@pytest.fixture
def clock(base_clock, monkeypatch):
    monkeypatch.setattr("safeharness.recovery.clock_sql", lambda _: str(base_clock[0]))
    return base_clock


class Crash(BaseException):
    pass


def worker(store, actors=None):
    return QueueWorker(
        store, business(store), principals() if actors is None else actors
    )


def interrupted(store, phase):
    task, _ = store.submit("alice", REQUEST)
    w = worker(store)
    claim = claim_next(store, "old")
    if phase == "created":
        return task, claim
    if phase == "no_session":
        with claim_scope(claim):
            store.transition(task, TaskStatus.RUNNING)
        return store.get(task.task_id), claim
    original = CheckpointSession.save
    wanted = "final" if phase == "verifying" else phase

    def save(session, task, checkpoint_phase, *args, **kwargs):
        if phase == "missing_checkpoint" and checkpoint_phase == "initial":
            raise Crash()
        original(session, task, checkpoint_phase, *args, **kwargs)
        if checkpoint_phase == wanted:
            raise Crash()

    with (
        claim_scope(claim),
        patch.object(CheckpointSession, "save", save),
        patch.object(CheckpointSession, "release", return_value=None),
        pytest.raises(Crash),
    ):
        w.service.run(task.task_id, w.service.principals["alice"])
    task = store.get(task.task_id)
    if phase == "verifying":
        with claim_scope(claim):
            task = store.transition(task, TaskStatus.VERIFYING)
    return task, claim


def expire(store, task_id):
    with store._engine.begin() as c:
        execute(
            c,
            "UPDATE task_queue SET lease_expires_at=0 WHERE task_id=?",
            (task_id.hex,),
        )


@pytest.mark.parametrize(
    "phase",
    [
        "created",
        "no_session",
        "initial",
        "before_model",
        "after_model",
        "after_tool",
        "final",
        "verifying",
    ],
)
def test_safe_windows_continue_without_duplicate_tools_or_budget_reset(
    store, clock, phase
):
    task, old = interrupted(store, phase)
    before = InvocationLedger(store.database, task.task_id).snapshot()
    deadline = store.scheduling(task.task_id)["execution_deadline_at"]
    expire(store, task.task_id)
    report = scan_expired(store)
    assert report["scanned"] == 1 and report["decisions"][0]["action"] in {
        "start",
        "initialize",
        "resume",
    }
    assert scan_expired(store)["scanned"] == 0
    assert store.scheduling(task.task_id)["state"] == "READY"
    with pytest.raises(QueueClaimError):
        renew_claim(store, old)
    done = worker(store).run_once()
    assert done["task_status"] == "SUCCEEDED" and done["scheduling"]["generation"] == 2
    assert done["scheduling"]["execution_deadline_at"] == deadline
    after = InvocationLedger(store.database, task.task_id).snapshot()
    assert len(after["calls"]) == 1
    if before["calls"]:
        assert after["calls"][0]["call_id"] == before["calls"][0]["call_id"]
    with store._engine.connect() as c:
        assert (
            execute(
                c,
                "SELECT generation FROM runtime_sessions WHERE task_id=?",
                (task.task_id.hex,),
            ).scalar_one()
            == 2
        )


@pytest.mark.parametrize(
    "fault",
    [
        "before_tool",
        "missing_checkpoint",
        "corrupt_hash",
        "deep_json",
        "intent",
        "result_not_checkpointed",
    ],
)
def test_uncertain_or_damaged_evidence_stops_for_review(store, clock, fault):
    task, claim = interrupted(
        store,
        "before_tool"
        if fault in {"before_tool", "intent"}
        else "missing_checkpoint"
        if fault == "missing_checkpoint"
        else "after_tool",
    )
    if fault == "corrupt_hash":
        with store._engine.begin() as c:
            execute(
                c,
                "UPDATE runtime_checkpoints SET sha256=? WHERE task_id=?",
                ("0" * 64, task.task_id.hex),
            )
    if fault == "deep_json":
        content = b"[" * 2000 + b"0" + b"]" * 2000
        with store._engine.begin() as c:
            execute(
                c,
                "UPDATE runtime_checkpoints SET content=?,byte_count=?,sha256=? WHERE task_id=?",
                (
                    content,
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                    task.task_id.hex,
                ),
            )
    if fault == "intent":
        with claim_scope(claim):
            InvocationLedger(store.database, task.task_id).start(
                "read_ticket", "tool", "fixture"
            )
    if fault == "result_not_checkpointed":
        with store._engine.begin() as c:
            execute(
                c,
                "UPDATE runtime_sessions SET checkpoint_id=(SELECT checkpoint_id FROM runtime_checkpoints WHERE task_id=? AND phase='before_tool' ORDER BY sequence DESC LIMIT 1) WHERE task_id=?",
                (task.task_id.hex, task.task_id.hex),
            )
    before = InvocationLedger(store.database, task.task_id).snapshot()
    expire(store, task.task_id)
    decision = scan_expired(store)["decisions"][0]
    assert decision["action"] == "review"
    assert store.get(task.task_id).status is TaskStatus.NEEDS_REVIEW
    assert store.scheduling(task.task_id)["state"] == "FINISHED"
    assert (
        worker(store).run_once()["status"] == "idle"
        and scan_expired(store)["scanned"] == 0
    )
    assert (
        InvocationLedger(store.database, task.task_id).snapshot()["calls"]
        == before["calls"]
    )


def test_deadline_and_recovery_limit_are_not_reset_by_restarts(store, clock):
    task, _ = interrupted(store, "created")
    deadline = store.scheduling(task.task_id)["execution_deadline_at"]
    for i in range(3):
        expire(store, task.task_id)
        assert scan_expired(store)["decisions"][0]["action"] == "start"
        assert claim_next(store, f"next-{i}").generation == i + 2
        assert store.scheduling(task.task_id)["execution_deadline_at"] == deadline
    expire(store, task.task_id)
    assert scan_expired(store)["decisions"][0]["code"] == "recovery_limit_reached"
    assert store.get(task.task_id).status is TaskStatus.NEEDS_REVIEW
    task, _ = interrupted(store, "created")
    clock[0] += 121
    expire(store, task.task_id)
    assert scan_expired(store)["decisions"][0]["action"] == "failed"
    assert store.get(task.task_id).status is TaskStatus.FAILED


def test_unconfirmed_call_takes_priority_over_expired_task_budget(store, clock):
    task, claim = interrupted(store, "before_tool")
    with claim_scope(claim):
        InvocationLedger(store.database, task.task_id).start(
            "read_ticket", "tool", "fixture"
        )
    clock[0] += 121
    expire(store, task.task_id)
    assert scan_expired(store)["decisions"][0]["code"] == "unconfirmed_call"
    assert store.get(task.task_id).status is TaskStatus.NEEDS_REVIEW


def test_crash_between_new_claim_and_checkpoint_acquire_can_recover_again(store, clock):
    task, _ = interrupted(store, "after_tool")
    expire(store, task.task_id)
    scan_expired(store)
    second = claim_next(store, "second")
    assert second.generation == 2 and second.resume_generation == 1
    expire(store, task.task_id)
    scan_expired(store)
    done = worker(store).run_once()
    assert done["task_status"] == "SUCCEEDED" and done["scheduling"]["generation"] == 3
    assert len(InvocationLedger(store.database, task.task_id).snapshot()["calls"]) == 1


@pytest.mark.parametrize("changed", ["config", "pinned_checkpoint"])
def test_changed_resume_context_is_rejected_without_dispatch(store, clock, changed):
    task, _ = interrupted(store, "after_tool")
    expire(store, task.task_id)
    scan_expired(store)
    actors = principals()
    if changed == "config":
        actors = [
            replace(p, writable_tickets=frozenset()) if p.subject == "alice" else p
            for p in actors
        ]
    else:
        with store._engine.begin() as c:
            execute(
                c,
                "UPDATE runtime_sessions SET checkpoint_id=NULL WHERE task_id=?",
                (task.task_id.hex,),
            )
    assert worker(store, actors).run_once()["task_status"] == "NEEDS_REVIEW"
    assert len(InvocationLedger(store.database, task.task_id).snapshot()["calls"]) == 1


def test_live_lease_manual_and_cancelled_tasks_are_not_recovered(store, clock):
    task, _claim = interrupted(store, "created")
    manual = store.create(owner_id="alice", request={"scenario": "ticket_read"})
    assert scan_expired(store)["scanned"] == 0
    expire(store, task.task_id)
    scan_expired(store)
    store.cancel(task.task_id, None)
    assert (
        worker(store).run_once()["status"] == "idle"
        and scan_expired(store)["scanned"] == 0
    )
    assert store.get(manual.task_id).status is TaskStatus.CREATED


def test_recovery_decision_and_ready_transition_roll_back_together(store, clock):
    task, _ = interrupted(store, "created")
    expire(store, task.task_id)

    def reject(_, __, statement, parameters, *args):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if (
            "insert into trace_events" in statement.lower()
            and "scheduling.recovery" in values
        ):
            raise RuntimeError("injected decision failure")

    event.listen(store._engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError):
            scan_expired(store)
    finally:
        event.remove(store._engine, "before_cursor_execute", reject)
    assert store.scheduling(task.task_id)[
        "state"
    ] == "CLAIMED" and "recovery_count" not in store.scheduling(task.task_id)
    assert scan_expired(store)["scanned"] == 1


def test_two_actual_scanner_processes_make_one_decision(store, tmp_path):
    task, _ = interrupted(store, "created")
    expire(store, task.task_id)
    code = """
import json,os,time
from pathlib import Path
from safeharness.task_store import TaskStore
from safeharness.recovery import scan_expired
p=Path(os.environ['SH_S4_GATE']);(p/('ready-'+os.environ['SH_S4_INDEX'])).touch()
deadline=time.monotonic()+15
while not (p/'go').exists():
 if time.monotonic()>deadline:raise SystemExit(9)
 time.sleep(.01)
print(json.dumps(scan_expired(TaskStore(os.environ['SH_S2_DATABASE']))))
"""
    children = []
    try:
        for i in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    env={
                        **child_env(store),
                        "SH_S4_GATE": str(tmp_path),
                        "SH_S4_INDEX": str(i),
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        until = time.monotonic() + 15
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < until and all(p.poll() is None for p in children)
            time.sleep(0.01)
        (tmp_path / "go").touch()
        reports = []
        for child in children:
            out, _ = child.communicate(timeout=30)
            assert child.returncode == 0
            reports.append(json.loads(out))
        assert sum(r["scanned"] for r in reports) == 1
        assert (
            store.scheduling(task.task_id)["recovery_count"] == 1
            and store.scheduling(task.task_id)["state"] == "READY"
        )
        assert (
            sum(
                e["kind"] == "scheduling.recovery"
                for e in TraceLog(store.database, task.task_id).read()
            )
            == 1
        )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_terminal_reconciliation_does_not_reexecute_or_change_result(store):
    task, _ = store.submit("alice", REQUEST)
    worker(store).run_once()
    before = store.get(task.task_id)
    with store._engine.begin() as c:
        execute(
            c,
            "UPDATE task_queue SET state='CLAIMED',lease_expires_at=0 WHERE task_id=?",
            (task.task_id.hex,),
        )
    assert scan_expired(store)["decisions"][0]["action"] == "terminal"
    assert (
        store.get(task.task_id) == before
        and store.scheduling(task.task_id)["state"] == "FINISHED"
    )
    assert len(InvocationLedger(store.database, task.task_id).snapshot()["calls"]) == 1


def test_review_generation_does_not_inherit_old_heartbeat_count(store, clock):
    task, claim = interrupted(store, "before_tool")
    assert renew_claim(store, claim)
    assert store.scheduling(task.task_id)["heartbeat_count"] == 1
    expire(store, task.task_id)
    assert scan_expired(store)["decisions"][0]["action"] == "review"
    queue = store.scheduling(task.task_id)
    assert queue["generation"] == 2 and queue["heartbeat_count"] == 0


@pytest.mark.postgres
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_POSTGRES_TESTS") != "1",
    reason="Enable PostgreSQL renewal race",
)
def test_scanner_rechecks_lease_after_inflight_renewal_commits(pg_store):
    store, _ = pg_store
    task, claim = interrupted(store, "created")
    with store._engine.begin() as c:
        execute(
            c,
            "UPDATE task_queue SET lease_expires_at=EXTRACT(EPOCH FROM clock_timestamp())+1 WHERE task_id=?",
            (task.task_id.hex,),
        )
    renewed, release, selecting = Event(), Event(), Event()

    def hold(_, __, statement, *args):
        if statement.lower().startswith("update task_queue set last_heartbeat_at"):
            renewed.set()
            assert release.wait(8)

    def arriving(_, __, statement, *args):
        if statement.lower().startswith("select *,lease_expires_at"):
            selecting.set()

    event.listen(store._engine, "after_cursor_execute", hold)
    event.listen(store._engine, "before_cursor_execute", arriving)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            renewal = pool.submit(renew_claim, store, claim)
            try:
                assert renewed.wait(4)
                time.sleep(1.2)
                scanner = pool.submit(scan_expired, store)
                assert selecting.wait(4)
            finally:
                release.set()
            assert renewal.result(timeout=5)
            report = scanner.result(timeout=5)
        assert report["decisions"][0]["action"] == "skipped"
        assert store.scheduling(task.task_id)["lease_status"] == "ACTIVE"
        assert "recovery_count" not in store.scheduling(task.task_id)
    finally:
        release.set()
        event.remove(store._engine, "after_cursor_execute", hold)
        event.remove(store._engine, "before_cursor_execute", arriving)


@pytest.mark.parametrize(
    "batch,limit", [(0, 3), (101, 3), (True, 3), (1, 0), (1, 11), (1, True)]
)
def test_invalid_scan_budgets_are_rejected(batch, limit):
    with pytest.raises(ValueError):
        scan_expired(None, batch_size=batch, max_recoveries=limit)
