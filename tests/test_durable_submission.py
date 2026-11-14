import json
import os
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_postgres import pg_store  # noqa: F401 - shared dedicated-schema fixture
from test_service import headers, principals

from safeharness.contracts import SubmissionConflictError
from safeharness.database import execute
from safeharness.service import create_app
from safeharness.task import TaskStatus
from safeharness.task_store import TaskConflictError, TaskStore
from safeharness.ticket_store import TicketStore

REQUEST = {"scenario": "ticket_read", "execution_mode": "queued"}
TABLES = ("tasks", "service_tasks", "task_queue", "task_submissions", "trace_events")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    os.getenv("SAFEHARNESS_POSTGRES_TESTS") != "1",
                    reason="Enable dedicated local PostgreSQL tests",
                ),
            ],
        ),
    ]
)
def store(request, tmp_path):
    if request.param == "postgres":
        return request.getfixturevalue("pg_store")[0]
    store = TaskStore(tmp_path / "control.sqlite3")
    store.initialize()
    return store


def counts(store):
    with store._engine.connect() as connection:
        return {
            table: execute(connection, f"SELECT count(*) FROM {table}").scalar_one()
            for table in TABLES
        }


def test_queued_submission_survives_new_store_and_replays_once(store):
    task, replayed = store.submit("alice", REQUEST, "queued-1")
    assert not replayed and task.status is TaskStatus.CREATED
    metadata = store.scheduling(task.task_id)
    other = TaskStore(store.database)
    other.initialize()
    assert other.get(task.task_id) == task
    assert other.scheduling(task.task_id) == metadata
    assert metadata["state"] == "QUEUED" and metadata["schema_version"] == 1
    assert other.submit("alice", REQUEST, "queued-1") == (task, True)
    assert counts(store) == dict(zip(TABLES, (1, 1, 1, 1, 2)))
    with store._engine.connect() as connection:
        assert (
            execute(connection, "SELECT count(*) FROM runtime_sessions").scalar_one()
            == 0
        )


def test_queue_mode_is_part_of_request_identity_but_manual_defaults_are_not(store):
    manual = {"scenario": "ticket_read"}
    old, _ = store.submit("alice", manual, "same-key")
    assert store.submit(
        "alice", {**manual, "execution_mode": "manual"}, "same-key"
    ) == (old, True)
    assert store.scheduling(old.task_id) is None
    with pytest.raises(SubmissionConflictError):
        store.submit("alice", REQUEST, "same-key")
    queued, _ = store.submit("bob", REQUEST, "same-key")
    assert queued.task_id != old.task_id
    assert counts(store)["task_queue"] == 1


@pytest.mark.parametrize("failure", ["task_queue", "task_submissions", "enqueue_event"])
def test_partial_submission_rolls_back_task_queue_key_and_trace(store, failure):
    def reject(_, __, statement, parameters, *args):
        table = "trace_events" if failure == "enqueue_event" else failure
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if f"insert into {table}" in statement.lower() and (
            failure != "enqueue_event" or "scheduling.enqueued" in values
        ):
            raise RuntimeError("injected submission failure")

    event.listen(store._engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected submission failure"):
            store.submit("alice", REQUEST, "retry-key")
    finally:
        event.remove(store._engine, "before_cursor_execute", reject)
    assert counts(store) == dict.fromkeys(TABLES, 0)
    task, replayed = store.submit("alice", REQUEST, "retry-key")
    assert not replayed and store.scheduling(task.task_id)["state"] == "QUEUED"


@pytest.mark.parametrize("crash", ["before_commit", "after_commit"])
def test_real_process_exit_preserves_only_committed_submissions(store, crash):
    # Only the child environment receives the URL; never put credentials in argv/output.
    database = store.database
    env = {
        **os.environ,
        "SH_S1_TEST_DATABASE": database.render_as_string(hide_password=False)
        if hasattr(database, "render_as_string")
        else str(database),
        "SH_S1_CRASH": crash,
    }
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from sqlalchemy import event
from safeharness.task_store import TaskStore
s=TaskStore(os.environ['SH_S1_TEST_DATABASE'])
if os.environ['SH_S1_CRASH']=='before_commit':
    def crash(*args):
        if 'insert into task_queue' in args[2].lower():
            os._exit(77)
    event.listen(s._engine,'after_cursor_execute',crash)
t,_=s.submit('alice',{'scenario':'ticket_read','execution_mode':'queued'},'crash-key')
os.write(1,str(t.task_id).encode())
os._exit(78)
""",
        ],
        env=env,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == (77 if crash == "before_commit" else 78)
    if crash == "before_commit":
        assert counts(store) == dict.fromkeys(TABLES, 0)
    else:
        committed_id = UUID(child.stdout.decode())
        assert store.get(committed_id).status is TaskStatus.CREATED
        assert store.scheduling(committed_id)["state"] == "QUEUED"
    task, replayed = store.submit("alice", REQUEST, "crash-key")
    assert replayed is (crash == "after_commit")
    if replayed:
        assert task.task_id == committed_id
    assert counts(store) == dict(zip(TABLES, (1, 1, 1, 1, 2)))


def test_queued_task_cannot_enter_manual_execution(store):
    task, _ = store.submit("alice", REQUEST)
    with pytest.raises(TaskConflictError, match="worker claim"):
        store.transition(task, TaskStatus.RUNNING)
    assert store.get(task.task_id) == task
    assert store.scheduling(task.task_id)["state"] == "QUEUED"


def test_two_real_processes_submit_one_queued_task(store, tmp_path):
    database = store.database
    env = {
        **os.environ,
        "SH_S1_TEST_DATABASE": database.render_as_string(hide_password=False)
        if hasattr(database, "render_as_string")
        else str(database),
        "SH_S1_GATE": str(tmp_path),
    }
    children = []
    code = """
import json,os,time
from pathlib import Path
from safeharness.task_store import TaskStore
s=TaskStore(os.environ['SH_S1_TEST_DATABASE'])
folder=Path(os.environ['SH_S1_GATE'])
(folder/('ready-'+os.environ['SH_S1_INDEX'])).touch()
deadline=time.monotonic()+15
while not (folder/'go').exists():
    if time.monotonic()>deadline: raise SystemExit(9)
    time.sleep(.01)
t,replayed=s.submit('alice',{'scenario':'ticket_read','execution_mode':'queued'},'process-key')
print(json.dumps({'task_id':str(t.task_id),'replayed':replayed}))
"""
    try:
        for i in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    env={**env, "SH_S1_INDEX": str(i)},
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
        assert len({r["task_id"] for r in results}) == 1
        assert sorted(r["replayed"] for r in results) == [False, True]
        assert counts(store) == dict(zip(TABLES, (1, 1, 1, 1, 2)))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_cancel_rolls_back_task_and_queue_together(store):
    task, _ = store.submit("alice", REQUEST, "cancel-key")

    def reject(_, __, statement, *args):
        if "update task_queue" in statement.lower():
            raise RuntimeError("injected cancellation failure")

    event.listen(store._engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected cancellation failure"):
            store.cancel(task.task_id, None)
    finally:
        event.remove(store._engine, "before_cursor_execute", reject)
    observed, scheduling = store.task_with_scheduling(task.task_id)
    assert observed == task and scheduling["state"] == "QUEUED"
    store.cancel(task.task_id, None)
    cancelled, scheduling = store.task_with_scheduling(task.task_id)
    assert (
        cancelled.status is TaskStatus.CANCELLED and scheduling["state"] == "CANCELLED"
    )
    assert store.submit("alice", REQUEST, "cancel-key") == (cancelled, True)


def test_http_restart_response_loss_ownership_and_cancel(tmp_path):
    store = TaskStore(tmp_path / "http.sqlite3")
    business = TicketStore(store.database)
    app = create_app(store, business, principals())
    owner = {**headers("alice"), "Idempotency-Key": "http-key"}
    # Commit succeeds, but constructing the response fails: client must safely retry.
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        patch.object(
            app.state.service, "snapshot", side_effect=RuntimeError("response lost")
        ),
    ):
        assert client.post("/v1/tasks", json=REQUEST, headers=owner).status_code == 500
    restarted = create_app(
        TaskStore(store.database), TicketStore(store.database), principals()
    )
    with TestClient(restarted) as client:
        response = client.post("/v1/tasks", json=REQUEST, headers=owner)
        assert (
            response.status_code == 200
            and response.headers["Idempotency-Replayed"] == "true"
        )
        snapshot = response.json()
        task_id = snapshot["task_id"]
        assert (
            snapshot["task_status"] == "CREATED"
            and snapshot["scheduling"]["state"] == "QUEUED"
        )
        assert client.get(f"/v1/tasks/{task_id}", headers=owner).json() == snapshot
        assert (
            client.get(f"/v1/tasks/{task_id}", headers=headers("bob")).status_code
            == 404
        )
        assert (
            client.post(f"/v1/tasks/{task_id}/run", headers=headers("bob")).status_code
            == 404
        )
        blocked = client.post(f"/v1/tasks/{task_id}/run", headers=owner)
        assert (
            blocked.status_code == 409
            and blocked.json()["detail"] == "queued_task_requires_worker"
        )
        cancelled = client.post(f"/v1/tasks/{task_id}/cancel", headers=owner).json()
        assert (
            cancelled["task_status"] == cancelled["scheduling"]["state"] == "CANCELLED"
        )
        replay = client.post("/v1/tasks", json=REQUEST, headers=owner).json()
        assert replay == cancelled
    assert counts(store)["task_queue"] == 1 and business.ticket() == ("open", 0)


@pytest.mark.parametrize(
    "extra",
    [
        {"scenario": "ticket_update", "provider": "deepseek"},
        {"scenario": "coding_fix"},
        {"provider": "deepseek"},
        {"execution_mode": "CLAIMED"},
        {"owner_id": "reviewer"},
    ],
)
def test_unsupported_or_forged_queue_request_creates_nothing(tmp_path, extra):
    store = TaskStore(tmp_path / "invalid.sqlite3")
    with TestClient(
        create_app(store, TicketStore(store.database), principals())
    ) as client:
        assert (
            client.post(
                "/v1/tasks", json={**REQUEST, **extra}, headers=headers("alice")
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/tasks", json=REQUEST, headers=headers("scanner")
            ).status_code
            == 403
        )
        assert client.post("/v1/tasks", json=REQUEST).status_code == 401
    assert counts(store) == dict.fromkeys(TABLES, 0)


def test_migration_preserves_legacy_request_digest_without_enqueuing_old_tasks(
    tmp_path,
):
    store = TaskStore(tmp_path / "old.sqlite3")
    with store._engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        config = Config(attributes={"connection": connection})
        config.set_main_option("script_location", "safeharness:migrations")
        command.upgrade(config, "0012_approval_loop")
    old_request = {
        "scenario": "ticket_read",
        "provider": "scripted",
        "ticket_id": "IT-001",
        "status": "resolved",
    }
    # Create a genuine pre-S1 idempotency record using the original request shape.
    task = store.create(owner_id="alice", request=old_request)
    from hashlib import sha256

    from safeharness.invocations import digest, now

    with store._engine.begin() as connection:
        execute(
            connection,
            "INSERT INTO task_submissions VALUES (?,?,?,?,?)",
            (
                "alice",
                sha256(b"legacy-key").hexdigest(),
                digest(old_request),
                task.task_id.hex,
                now(),
            ),
        )
    store.initialize()
    assert store.get(task.task_id) == task and store.scheduling(task.task_id) is None
    with TestClient(
        create_app(store, TicketStore(store.database), principals())
    ) as client:
        response = client.post(
            "/v1/tasks",
            json={"scenario": "ticket_read"},
            headers={**headers("alice"), "Idempotency-Key": "legacy-key"},
        )
        assert response.status_code == 200 and response.json()["task_id"] == str(
            task.task_id
        )
    with store._engine.connect() as connection:
        assert (
            execute(connection, "SELECT version_num FROM alembic_version").scalar_one()
            == "0017_queued_approvals"
        )
        assert execute(connection, "SELECT count(*) FROM task_queue").scalar_one() == 0
    assert (
        json.loads(json.dumps(store.service_task(task.task_id)["request"]))
        == old_request
    )
