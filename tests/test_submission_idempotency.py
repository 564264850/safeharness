import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_service import headers, principals

from safeharness.contracts import SubmissionConflictError
from safeharness.service import create_app
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore


@pytest.fixture
def store(tmp_path):
    result = TaskStore(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def test_same_submission_returns_same_task_and_conflicting_payload_is_rejected(store):
    first, replayed = store.submit("alice", {"scenario": "ticket_read"}, "request-1")
    assert not replayed
    second, replayed = TaskStore(store.database).submit(
        "alice", {"scenario": "ticket_read"}, "request-1"
    )
    assert replayed and first == second
    with pytest.raises(SubmissionConflictError):
        store.submit("alice", {"scenario": "ticket_update"}, "request-1")
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM trace_events").fetchone()[0] == 1
        )


def test_concurrent_sqlite_submissions_create_one_task_without_orphans(store):
    barrier = Barrier(4)

    def submit(_):
        barrier.wait()
        return TaskStore(store.database).submit(
            "alice", {"scenario": "ticket_read"}, "same-key"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert len({task.task_id for task, _ in results}) == 1
    assert sum(not replayed for _, replayed in results) == 1
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_keys_are_scoped_to_owner_and_absent_keys_create_distinct_intents(store):
    first = store.submit("alice", {"scenario": "ticket_read"}, "same-key")[0]
    other = store.submit("bob", {"scenario": "ticket_read"}, "same-key")[0]
    no_key = [store.submit("alice", {"scenario": "ticket_read"})[0] for _ in range(2)]
    assert len({first.task_id, other.task_id, *(task.task_id for task in no_key)}) == 4


def test_http_replay_retains_current_task_status_and_checks_auth_first(store):
    business = TicketStore(store.database)
    with TestClient(create_app(store, business, principals())) as client:
        alice = {**headers("alice"), "Idempotency-Key": "demo-key"}
        first = client.post(
            "/v1/tasks", json={"scenario": "ticket_read"}, headers=alice
        )
        assert (
            first.status_code == 201
            and first.headers["Idempotency-Replayed"] == "false"
        )
        task_id = first.json()["task_id"]
        assert (
            client.post(f"/v1/tasks/{task_id}/run", headers=alice).json()["task_status"]
            == "SUCCEEDED"
        )
        again = client.post(
            "/v1/tasks",
            json={"scenario": "ticket_read", "provider": "scripted"},
            headers=alice,
        )
        assert (
            again.status_code == 200 and again.headers["Idempotency-Replayed"] == "true"
        )
        assert (
            again.json()["task_id"] == task_id
            and again.json()["task_status"] == "SUCCEEDED"
        )
        assert (
            client.post(
                "/v1/tasks", json={"scenario": "ticket_update"}, headers=alice
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/v1/tasks",
                json={"scenario": "ticket_read"},
                headers={"Idempotency-Key": "demo-key"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/tasks",
                json={"scenario": "ticket_read"},
                headers={**headers("scanner"), "Idempotency-Key": "demo-key"},
            ).status_code
            == 403
        )
        bob = client.post(
            "/v1/tasks",
            json={"scenario": "ticket_read"},
            headers={**headers("bob"), "Idempotency-Key": "demo-key"},
        )
        assert bob.status_code == 201 and bob.json()["task_id"] != task_id
    assert business.ticket() == ("open", 0)


@pytest.mark.parametrize("key", ["", "contains spaces", "x" * 129])
def test_invalid_http_key_does_not_create_task(store, key):
    with TestClient(
        create_app(store, TicketStore(store.database), principals())
    ) as client:
        assert (
            client.post(
                "/v1/tasks",
                json={"scenario": "ticket_read"},
                headers={**headers("alice"), "Idempotency-Key": key},
            ).status_code
            == 422
        )
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_raw_idempotency_key_is_not_saved(store):
    key = "private-request-key"
    store.submit("alice", {"scenario": "ticket_read"}, key)
    with sqlite3.connect(store.database) as connection:
        rows = connection.execute("SELECT * FROM task_submissions").fetchall()
        assert key not in json.dumps(rows)
