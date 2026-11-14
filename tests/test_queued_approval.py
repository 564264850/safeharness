from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from sqlalchemy import event
from test_queue_recovery import Crash, expire, worker
from test_queue_recovery import pg_store as shared_pg
from test_queue_recovery import store as shared_store

from safeharness.approval import ApprovalActor, ApprovalOutcome
from safeharness.checkpoints import CheckpointSession
from safeharness.database import execute
from safeharness.recovery import scan_expired
from safeharness.scheduling import claim_next, claim_scope

store = shared_store
pg_store = shared_pg


def pending(store):
    task, _ = store.submit(
        "alice",
        {
            "scenario": "ticket_update_read",
            "execution_mode": "queued",
            "status": "resolved",
        },
    )
    w = worker(store)
    assert w.run_once()["status"] == "waiting_approval"
    return task, w, store.get_approval(task.task_id).request


def decide(store, request, approve=True):
    return store.decide_approval(
        request.approval_id,
        ApprovalOutcome.APPROVED if approve else ApprovalOutcome.REJECTED,
        request.intent.arguments_sha256,
        actor=ApprovalActor("reviewer", can_approve=True),
    )


@pytest.mark.parametrize("action", ["approve", "reject", "cancel"])
def test_queued_approval_parks_and_durably_continues_or_stops(store, action):
    task, w, request = pending(store)
    assert store.scheduling(task.task_id)["state"] == "WAITING"
    assert worker(store).run_once()[
        "status"
    ] == "idle" and w.service.business.ticket() == ("open", 0)
    if action == "cancel":
        store.cancel(task.task_id, w.service.business)
    else:
        decide(store, request, action == "approve")
    if action == "approve":
        assert store.scheduling(task.task_id)["state"] == "READY"
        assert worker(store).run_once()["task_status"] == "SUCCEEDED"
        assert w.service.business.ticket() == ("resolved", 1)
    else:
        assert worker(store).run_once()["status"] == "idle"
        assert store.get(task.task_id).status.value == (
            "CANCELLED" if action == "cancel" else "FAILED"
        )
        assert w.service.business.ticket() == ("open", 0)


def test_approval_and_wakeup_roll_back_together(store):
    task, _, request = pending(store)

    def reject(_, __, statement, *args):
        if "update task_queue set state='ready'" in statement.lower():
            raise RuntimeError("wake failed")

    event.listen(store._engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError):
            decide(store, request)
    finally:
        event.remove(store._engine, "before_cursor_execute", reject)
    assert store.get_approval(task.task_id).decision is None
    assert store.scheduling(task.task_id)["state"] == "WAITING"
    decide(store, request)
    assert worker(store).run_once()["task_status"] == "SUCCEEDED"


def test_commit_crash_reconciles_same_business_receipt_without_rewrite(store):
    task, w, request = pending(store)
    decide(store, request)
    claim = claim_next(store, "crashing")
    original = w.service.business.apply

    def committed(*args):
        original(*args)
        raise Crash()

    with (
        claim_scope(claim),
        patch.object(w.service.business, "apply", side_effect=committed),
        pytest.raises(Crash),
    ):
        w.service.run(
            task.task_id,
            w.service.principals["alice"],
            resume=True,
            recover_abandoned=True,
            expected_generation=claim.resume_generation,
        )
    assert w.service.business.ticket() == ("resolved", 1)

    expire(store, task.task_id)
    assert scan_expired(store)["decisions"][0]["action"] == "approval"
    assert worker(store).run_once()["task_status"] == "SUCCEEDED"
    assert w.service.business.ticket() == ("resolved", 1)


@pytest.mark.parametrize("fault", ["checkpoint", "owner_revoked", "scope_revoked"])
def test_approved_but_unsafe_setup_stops_without_repeated_claims(store, fault):
    task, w, request = pending(store)
    decide(store, request)
    if fault == "checkpoint":
        with store._engine.begin() as c:
            execute(
                c,
                "UPDATE runtime_checkpoints SET sha256=? WHERE task_id=?",
                ("0" * 64, task.task_id.hex),
            )
    elif fault == "owner_revoked":
        w.service.principals.clear()
    else:
        from dataclasses import replace

        w.service.principals["alice"] = replace(
            w.service.principals["alice"], writable_tickets=frozenset()
        )
    assert w.run_once()["task_status"] == "NEEDS_REVIEW"
    assert store.scheduling(task.task_id)["state"] == "FINISHED"
    assert worker(store).run_once()["status"] == "idle"
    assert w.service.business.ticket() == ("open", 0)


@pytest.mark.parametrize("action", ["cancel", "continue"])
def test_approval_wait_pauses_deadline_once_and_cancel_after_wake_cannot_revive(
    store, action
):
    task, w, request = pending(store)
    with store._engine.begin() as c:
        execute(
            c,
            "UPDATE runtime_sessions SET approval_wait_started_at=approval_wait_started_at-180,deadline_at=deadline_at-180 WHERE task_id=?",
            (task.task_id.hex,),
        )
        execute(
            c,
            "UPDATE task_queue SET execution_deadline_at=execution_deadline_at-180 WHERE task_id=?",
            (task.task_id.hex,),
        )
    decide(store, request)
    from safeharness.task_store import ApprovalConflictError

    deadline = store.scheduling(task.task_id)["execution_deadline_at"]
    with pytest.raises(ApprovalConflictError):
        decide(store, request)
    assert store.scheduling(task.task_id)["execution_deadline_at"] == deadline
    if action == "continue":
        assert worker(store).run_once()["task_status"] == "SUCCEEDED"
        assert w.service.business.ticket() == ("resolved", 1)
        return
    store.cancel(task.task_id, w.service.business)
    assert worker(store).run_once()["status"] == "idle"
    assert store.get(task.task_id).status.value == "CANCELLED"
    assert w.service.business.ticket() == ("open", 0)


def test_new_worker_may_complete_before_old_approval_cleanup_without_overwrite(store):
    task, _ = store.submit(
        "alice", {"scenario": "ticket_update_read", "execution_mode": "queued"}
    )
    entered, release = Event(), Event()
    original = CheckpointSession.release

    def settle(session, state):
        if state == "WAITING_APPROVAL" and not entered.is_set():
            entered.set()
            assert release.wait(20)
        return original(session, state)

    with (
        patch.object(CheckpointSession, "release", settle),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(worker(store).run_once)
        try:
            assert entered.wait(8)
            decide(store, store.get_approval(task.task_id).request)
            assert worker(store).run_once()["task_status"] == "SUCCEEDED"
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "lease_lost"
    assert store.get(task.task_id).status.value == "SUCCEEDED"
    assert worker(store).service.business.ticket() == ("resolved", 1)
