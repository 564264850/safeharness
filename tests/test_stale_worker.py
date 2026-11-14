from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from test_queue_recovery import pg_store as shared_pg
from test_queue_recovery import store as shared_store
from test_queue_worker import worker

from safeharness import worker as worker_module
from safeharness.audit import AuditError, AuditLog
from safeharness.checkpoints import CheckpointSession
from safeharness.database import execute
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.heartbeat import LeaseHeartbeat
from safeharness.invocations import InvocationLedger
from safeharness.recovery import scan_expired
from safeharness.scheduling import QueueClaimError, claim_scope, renew_claim

store = shared_store
pg_store = shared_pg


def test_frozen_old_worker_cannot_publish_after_standby_finishes(store):
    task, _ = store.submit(
        "alice", {"scenario": "ticket_read", "execution_mode": "queued"}
    )
    old = worker(store)
    entered, release = Event(), Event()
    hearts = []
    original = CheckpointSession.save

    def heartbeat(*args, **kwargs):
        heart = LeaseHeartbeat(*args, **kwargs)
        hearts.append(heart)
        return heart

    def save(session, task, phase, *args, **kwargs):
        original(session, task, phase, *args, **kwargs)
        if phase == "after_tool" and not entered.is_set():
            entered.set()
            assert release.wait(15)

    with (
        patch.object(worker_module, "LeaseHeartbeat", side_effect=heartbeat),
        patch.object(CheckpointSession, "save", save),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(old.run_once)
        try:
            assert entered.wait(5)
            hearts[0].stopped.set()
            hearts[0].thread.join(timeout=5)
            with store._engine.begin() as c:
                execute(
                    c,
                    "UPDATE task_queue SET lease_expires_at=0 WHERE task_id=?",
                    (task.task_id.hex,),
                )
            assert scan_expired(store)["decisions"][0]["action"] == "resume"
            done = worker(store).run_once()
            assert (
                done["task_status"] == "SUCCEEDED"
                and done["scheduling"]["generation"] == 2
            )
            ledger_before = InvocationLedger(store.database, task.task_id).snapshot()
            with pytest.raises(QueueClaimError):
                renew_claim(store, hearts[0].claim)
            with claim_scope(hearts[0].claim), pytest.raises(AuditError):
                ToolGateway(
                    {"IT-001": "open"},
                    frozenset({"IT-001"}),
                    audit=AuditLog(store.database, task.task_id),
                ).execute(ToolRequest("read_ticket", {"ticket_id": "IT-001"}))
        finally:
            release.set()
        late = future.result(timeout=10)
    assert late["status"] == "lease_lost" and late["code"] == "ownership_changed"
    assert store.get(task.task_id).status.value == "SUCCEEDED"
    assert (
        InvocationLedger(store.database, task.task_id).snapshot()["calls"]
        == ledger_before["calls"]
    )
