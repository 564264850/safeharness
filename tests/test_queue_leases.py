import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from alembic import command
from alembic.config import Config

pytest.importorskip("fastapi")
from test_durable_submission import store as shared_store_fixture
from test_postgres import pg_store as shared_pg_fixture
from test_queue_worker import business, child_env
from test_service import principals

from safeharness import heartbeat, scheduling, service, task_store
from safeharness.audit import AuditError, AuditLog
from safeharness.checkpoints import CheckpointConflict, CheckpointSession
from safeharness.database import execute
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.heartbeat import LeaseHeartbeat, validate_intervals
from safeharness.invocations import InvocationLedger
from safeharness.runtime import FinalAnswer
from safeharness.scheduling import (
    CURRENT_CLAIM,
    QueueClaimError,
    claim_next,
    claim_scope,
    renew_claim,
    require_claim,
)
from safeharness.task import TaskStatus
from safeharness.task_store import TaskConflictError, TaskStore
from safeharness.trace import TraceLog
from safeharness.worker import QueueWorker

store = shared_store_fixture
pg_store = shared_pg_fixture
REQUEST = {"scenario": "ticket_read", "execution_mode": "queued"}


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]

    def sql(_):
        return str(value[0])

    monkeypatch.setattr(scheduling, "clock_sql", sql)
    monkeypatch.setattr(task_store, "clock_sql", sql)
    monkeypatch.setattr(
        "safeharness.checkpoints.time",
        SimpleNamespace(time=lambda: value[0], monotonic=time.monotonic),
    )
    return value


def test_expiry_boundary_and_no_resurrection(store, clock):
    task, _ = store.submit("alice", REQUEST)
    claim = claim_next(store, "boundary", lease_seconds=10)
    clock[0] = 1009.999
    with claim_scope(claim), store._engine.connect() as c:
        assert require_claim(c, task.task_id) == claim
    clock[0] = 1010
    with (
        claim_scope(claim),
        store._engine.connect() as c,
        pytest.raises(QueueClaimError, match="expired"),
    ):
        require_claim(c, task.task_id)
    with pytest.raises(QueueClaimError):
        renew_claim(store, claim)
    result = store.scheduling(task.task_id)
    assert (
        result["lease_status"] == "EXPIRED"
        and result["heartbeat_count"] == 0
        and result["lease_expires_at"] == 1010
    )
    assert claim_next(store, "other") is None


def test_renewal_is_db_timed_and_does_not_reset_ownership(store, clock):
    task, _ = store.submit("alice", REQUEST)
    with patch.object(scheduling, "now", return_value="2099-01-01T00:00:00+00:00"):
        claim = claim_next(store, "renew", lease_seconds=10)
    clock[0] = 1004
    assert renew_claim(store, claim)
    result = store.scheduling(task.task_id)
    assert result["last_heartbeat_at"] == 1004 and result["lease_expires_at"] == 1014
    assert result["generation"] == 1 and result["heartbeat_count"] == 1
    for fake in (replace(claim, token="0" * 32), replace(claim, generation=2)):
        with pytest.raises(QueueClaimError):
            renew_claim(store, fake)
    assert store.scheduling(task.task_id)["heartbeat_count"] == 1


def test_real_db_clock_not_host_timestamp_controls_lease(store):
    task, _ = store.submit("alice", REQUEST)
    with patch.object(scheduling, "now", return_value="2099-01-01T00:00:00+00:00"):
        claim_next(store, "db-clock", lease_seconds=10)
    result = store.scheduling(task.task_id)
    with store._engine.connect() as c:
        db_now = float(execute(c, "SELECT " + scheduling.clock_sql(c)).scalar_one())
    assert 8 < result["lease_expires_at"] - db_now <= 10.1
    assert result["claimed_at"].startswith("2099")


def test_finished_and_cancelled_claims_cannot_be_renewed(store, clock):
    for cancel in (False, True):
        task, _ = store.submit("alice", REQUEST)
        claim = claim_next(store, "settle", lease_seconds=10)
        if cancel:
            store.cancel(task.task_id, None)
        else:
            with claim_scope(claim):
                running = store.transition(task, TaskStatus.RUNNING)
                verifying = store.transition(running, TaskStatus.VERIFYING)
                store.transition(verifying, TaskStatus.SUCCEEDED)
        assert renew_claim(store, claim) is False
        assert store.scheduling(task.task_id)["heartbeat_count"] == 0


def test_expired_owner_cannot_publish_tool_ledger_checkpoint_or_success(store, clock):
    task, _ = store.submit("alice", REQUEST)
    claim = claim_next(store, "fenced", lease_seconds=10)
    audit = AuditLog(store.database, task.task_id)
    ledger = InvocationLedger(store.database, task.task_id)
    with claim_scope(claim):
        running = store.transition(task, TaskStatus.RUNNING)
        session = CheckpointSession(
            store, task.task_id, {}, model=lambda _: None, stateless_model=True
        )
        session.begin(running, 5)
        call = audit.begin(
            tool="read_ticket",
            version=1,
            policy="mock-ticket-v1",
            digest=None,
            resources=(),
            decision="ALLOW",
            code="authorized",
        )
        identity = audit.identity(call)
        clock[0] = 1010
        with pytest.raises(AuditError):
            audit.finish(call, "ok", "ticket_read", 0, value="late value")
        with pytest.raises(QueueClaimError):
            ledger.finish(identity, "SUCCEEDED", "ticket_read", "late value")
        with pytest.raises(QueueClaimError):
            ledger.start("read_ticket", "tool", "runtime")
        with pytest.raises(CheckpointConflict):
            session.save(running, "before_model", (), 1)
        with pytest.raises(QueueClaimError):
            session.release("FINISHED")
        with pytest.raises(TaskConflictError):
            store.transition(running, TaskStatus.VERIFYING)
        with pytest.raises(QueueClaimError):
            TraceLog(store.database, task.task_id).emit(
                "runtime.verified", {"passed": True, "code": "verifier_passed"}
            )
        reader = Mock(return_value="open")
        gateway = ToolGateway(
            {}, frozenset({"IT-001"}), ticket_reader=reader, audit=audit
        )
        with pytest.raises(AuditError):
            gateway.execute(ToolRequest("read_ticket", {"ticket_id": "IT-001"}))
        reader.assert_not_called()
    snapshot = ledger.snapshot()
    assert snapshot["attempts"][0]["status"] == "INTENT" and snapshot["artifacts"] == []
    assert store.get(task.task_id) == running
    with store._engine.connect() as c:
        assert (
            execute(
                c,
                "SELECT owner_token FROM runtime_sessions WHERE task_id=?",
                (task.task_id.hex,),
            ).scalar_one()
            == claim.token
        )


def test_heartbeat_failure_fences_immediately_even_before_expiry(store, clock):
    task, _ = store.submit("alice", REQUEST)
    claim = claim_next(store, "local-loss", lease_seconds=10)
    with (
        patch.object(
            heartbeat, "renew_claim", side_effect=RuntimeError("synthetic failure")
        ),
        LeaseHeartbeat(store, claim, 0.1) as heart,
    ):
        assert heart.failed.is_set() and claim.fenced.is_set()
    assert store.scheduling(task.task_id)["lease_status"] == "ACTIVE"
    with (
        claim_scope(claim),
        store._engine.connect() as c,
        pytest.raises(QueueClaimError, match="heartbeat_lost"),
    ):
        require_claim(c, task.task_id)
    with pytest.raises(QueueClaimError):
        renew_claim(store, claim)


def test_worker_observes_fence_before_failure_notification_is_published(store, clock):
    task, _ = store.submit("alice", REQUEST)
    worker = QueueWorker(store, business(store), principals())

    @contextmanager
    def failure_between_flags(_store, claim, _interval):
        claim.fenced.set()
        yield SimpleNamespace(failed=Event(), failure_code="heartbeat_failed")

    with patch("safeharness.worker.LeaseHeartbeat", side_effect=failure_between_flags):
        result = worker.run_once()
    assert result["status"] == "lease_lost" and result["task_status"] == "CREATED"
    assert result["scheduling"]["lease_status"] == "ACTIVE"
    assert InvocationLedger(store.database, task.task_id).snapshot()["calls"] == []


def blocked_worker(store, *, lease_seconds=1.5, heartbeat_seconds=0.1):
    worker = QueueWorker(
        store,
        business(store),
        principals(),
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    entered, release = Event(), Event()
    original = worker.service.business.ticket
    calls = [0]
    claims = []

    def ticket():
        calls[0] += 1
        if calls[0] == 2:
            claims.append(CURRENT_CLAIM.get())
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test gate timed out")
        return original()

    return worker, entered, release, claims, ticket


@pytest.mark.parametrize("phase", ["tool", "model"])
def test_long_callback_keeps_lease_alive_in_independent_thread(store, phase):
    task, _ = store.submit("alice", REQUEST)
    worker, entered, release, _, ticket = blocked_worker(store)

    def final(text):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("test model gate timed out")
        return FinalAnswer(text)

    hook = (
        patch.object(worker.service.business, "ticket", side_effect=ticket)
        if phase == "tool"
        else patch.object(service, "FinalAnswer", side_effect=final)
    )
    with (
        hook,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(worker.run_once)
        try:
            assert entered.wait(5)
            first = store.scheduling(task.task_id)
            time.sleep(2.2)
            later = store.scheduling(task.task_id)
            assert (
                later["lease_status"] == "ACTIVE"
                and later["heartbeat_count"] > first["heartbeat_count"]
            )
            assert later["lease_expires_at"] > first["lease_expires_at"]
            assert claim_next(store, "competitor") is None
        finally:
            release.set()
        assert future.result(timeout=10)["task_status"] == "SUCCEEDED"
    assert store.scheduling(task.task_id)["lease_status"] == "RELEASED"


def test_lost_heartbeat_discards_late_tool_result(store, clock):
    task, _ = store.submit("alice", REQUEST)
    worker, entered, release, claims, ticket = blocked_worker(store, lease_seconds=10)
    fail = Event()
    original = heartbeat.renew_claim

    def renew(*args, **kwargs):
        if fail.is_set():
            raise RuntimeError("simulated heartbeat write failure")
        return original(*args, **kwargs)

    with (
        patch.object(heartbeat, "renew_claim", side_effect=renew),
        patch.object(worker.service.business, "ticket", side_effect=ticket),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(worker.run_once)
        try:
            assert entered.wait(5)
            fail.set()
            assert claims[0].fenced.wait(5)
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result["status"] == "lease_lost" and result["task_status"] == "RUNNING"
    assert result["scheduling"]["state"] == "CLAIMED"
    calls = InvocationLedger(store.database, task.task_id).snapshot()
    assert calls["attempts"][0]["status"] == "INTENT" and calls["artifacts"] == []


def test_blocked_heartbeat_db_write_is_bounded_and_fences_worker(store):
    task, _ = store.submit("alice", REQUEST)
    claim = claim_next(store, "blocked-db", lease_seconds=10)
    with store._engine.begin() as c:
        if c.dialect.name == "sqlite":
            c.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            execute(
                c,
                "SELECT task_id FROM task_queue WHERE task_id=? FOR UPDATE",
                (task.task_id.hex,),
            ).fetchone()
        started = time.monotonic()
        with LeaseHeartbeat(store, claim, 0.1) as heart:
            assert heart.failed.is_set()
        assert time.monotonic() - started < 4
    assert (
        claim.fenced.is_set() and store.scheduling(task.task_id)["heartbeat_count"] == 0
    )


def test_process_death_stops_heartbeat_and_lease_expires_without_reclaim(store):
    task, _ = store.submit("alice", REQUEST)
    env = {**child_env(store), "SH_S2_DATABASE": child_env(store)["SH_S2_DATABASE"]}
    code = """
import os
from safeharness.task_store import TaskStore
from safeharness.scheduling import claim_next
from safeharness.heartbeat import LeaseHeartbeat
s=TaskStore(os.environ['SH_S2_DATABASE'])
c=claim_next(s,'dead-process',lease_seconds=.5)
with LeaseHeartbeat(s,c,.05):os._exit(77)
"""
    child = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert child.returncode == 77
    count = store.scheduling(task.task_id)["heartbeat_count"]
    deadline = time.monotonic() + 5
    while store.scheduling(task.task_id)["lease_status"] != "EXPIRED":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert store.scheduling(task.task_id)["heartbeat_count"] == count
    assert claim_next(store, "other-worker") is None
    assert store.get(task.task_id).status is TaskStatus.CREATED


@pytest.mark.parametrize(
    "lease,interval",
    [(0, 1), (float("nan"), 1), (True, 0.1), (1, 0), (1, 0.5), (1, float("inf"))],
)
def test_invalid_intervals_are_rejected(lease, interval):
    with pytest.raises(ValueError):
        validate_intervals(lease, interval)


def test_s2_claim_migration_expires_old_owner_without_requeue(tmp_path):
    store = TaskStore(tmp_path / "s2.sqlite3")
    with store._engine.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys=OFF")
        c.exec_driver_sql("BEGIN IMMEDIATE")
        cfg = Config(attributes={"connection": c})
        cfg.set_main_option("script_location", "safeharness:migrations")
        command.upgrade(cfg, "0014_queue_claims")
    task = store.create(owner_id="alice", request=REQUEST)
    with store._engine.begin() as c:
        execute(
            c,
            "UPDATE task_queue SET state='CLAIMED',claim_token=?,claimed_by='s2',claimed_at='2026-11-13',generation=1 WHERE task_id=?",
            ("a" * 32, task.task_id.hex),
        )
    store.initialize()
    result = store.scheduling(task.task_id)
    assert (
        result["state"] == "CLAIMED"
        and result["lease_status"] == "EXPIRED"
        and result["heartbeat_count"] == 0
    )
    assert (
        store.get(task.task_id).status is TaskStatus.CREATED
        and claim_next(store, "s3") is None
    )
