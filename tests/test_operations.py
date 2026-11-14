from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_queue_recovery import pg_store as shared_pg
from test_queue_recovery import store as shared_store
from test_service import headers, principals

from safeharness.database import execute
from safeharness.operations import QueueCapacityError, metrics
from safeharness.service import create_app
from safeharness.ticket_store import TicketStore

store = shared_store
pg_store = shared_pg


def test_durable_capacity_allows_replay_and_releases_cancelled_slot(store):
    body = {"scenario": "ticket_read", "execution_mode": "queued"}
    task, _ = store.submit("alice", body, "one", max_pending=1)
    assert store.submit("alice", body, "one", max_pending=1)[1]
    with pytest.raises(QueueCapacityError):
        store.submit("alice", body, "two", max_pending=1)
    store.cancel(task.task_id, None)
    store.submit("alice", body, "two", max_pending=1)
    assert metrics(store)["task_total"] == 2


def test_concurrent_submitters_cannot_overbook_capacity(store):
    barrier = Barrier(2)

    def submit(index):
        barrier.wait(timeout=5)
        try:
            store.submit(
                "alice",
                {"scenario": "ticket_read", "execution_mode": "queued"},
                str(index),
                max_pending=1,
            )
            return "accepted"
        except QueueCapacityError:
            return "full"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == ["accepted", "full"]
    assert metrics(store)["task_total"] == metrics(store)["queue_total"] == 1


def test_operations_exposes_expired_claims_and_review_alerts(store):
    from safeharness.scheduling import claim_next

    task, _ = store.submit(
        "alice", {"scenario": "ticket_read", "execution_mode": "queued"}
    )
    claim_next(store, "stale")
    with store._engine.begin() as c:
        execute(
            c,
            "UPDATE task_queue SET lease_expires_at=0 WHERE task_id=?",
            (task.task_id.hex,),
        )
    assert metrics(store)["alerts"] == ["expired_claims"]


def test_http_capacity_and_readiness_and_operations_permissions(tmp_path):
    from dataclasses import replace

    actors = principals()
    actors.append(
        replace(
            actors[-1],
            subject="auditor",
            token_sha256=__import__("hashlib").sha256(b"test-auditor").hexdigest(),
            roles=frozenset({"auditor"}),
        )
    )
    from safeharness.task_store import TaskStore

    store = TaskStore(tmp_path / "api.sqlite3")
    with TestClient(
        create_app(store, TicketStore(store.database), actors, max_pending=1)
    ) as client:
        assert client.get("/ready").json()["ready"]
        from sqlalchemy.exc import OperationalError

        with patch.object(
            store._engine,
            "connect",
            side_effect=OperationalError("private", {}, Exception("private")),
        ):
            response = client.get("/ready")
            assert response.status_code == 503 and response.json() == {
                "detail": "storage_unavailable"
            }
            assert client.get("/health").status_code == 200
        body = {"scenario": "ticket_read", "execution_mode": "queued"}
        assert (
            client.post("/v1/tasks", json=body, headers=headers("alice")).status_code
            == 201
        )
        assert (
            client.post("/v1/tasks", json=body, headers=headers("alice")).status_code
            == 429
        )
        assert client.get("/v1/operations", headers=headers("alice")).status_code == 403
        assert (
            client.get("/v1/operations", headers=headers("auditor")).json()[
                "queue_total"
            ]
            == 1
        )
