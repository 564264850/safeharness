from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_queue_recovery import expire, worker
from test_queue_recovery import pg_store as shared_pg
from test_queue_recovery import store as shared_store

from safeharness.heartbeat import LeaseHeartbeat
from safeharness.recovery import scan_expired
from safeharness.worker import QueueWorker, run_worker

store = shared_store
pg_store = shared_pg


def test_persistent_consumer_reuses_process_but_not_fenced_claim(store):
    request = {"scenario": "ticket_read", "execution_mode": "queued"}
    lost, _ = store.submit("alice", request)
    good, _ = store.submit("alice", request)
    setup = worker(store)
    calls = [0]
    original = QueueWorker.run_once

    @contextmanager
    def heartbeat(current_store, claim, interval):
        calls[0] += 1
        if calls[0] == 1:
            claim.fenced.set()
            yield SimpleNamespace(failed=Event(), failure_code="heartbeat_failed")
        else:
            with LeaseHeartbeat(current_store, claim, interval) as heart:
                yield heart

    def until_idle(consumer):
        result = original(consumer)
        if result["status"] == "idle":
            raise KeyboardInterrupt()
        return result

    with (
        patch("safeharness.worker.LeaseHeartbeat", side_effect=heartbeat),
        patch.object(QueueWorker, "run_once", until_idle),
    ):
        assert (
            run_worker(
                store,
                setup.service.business,
                list(setup.service.principals.values()),
                poll_seconds=0.05,
            )
            == 0
        )
    assert calls[0] == 2
    assert store.get(lost.task_id).status.value == "CREATED"
    assert store.get(good.task_id).status.value == "SUCCEEDED"
    assert store.scheduling(lost.task_id)["state"] == "CLAIMED"
    expire(store, lost.task_id)
    assert scan_expired(store)["decisions"][0]["action"] == "start"
    assert worker(store).run_once()["task_status"] == "SUCCEEDED"
    assert setup.service.business.ticket() == ("open", 0)


@pytest.mark.parametrize("mode", ["once", "drain"])
def test_bounded_worker_command_preserves_failure_exit_code(store, mode):
    setup = worker(store)
    with patch.object(QueueWorker, "run_once", return_value={"status": "lease_lost"}):
        assert (
            run_worker(
                store,
                setup.service.business,
                list(setup.service.principals.values()),
                **{mode: True},
            )
            == 1
        )
