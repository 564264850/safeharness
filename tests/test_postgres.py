import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from safeharness.approval import ApprovalActor, ApprovalOutcome
from safeharness.audit import AuditLog
from safeharness.database import engine_for
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.pg_ticket_store import PostgresTicketStore
from safeharness.runtime import run_task
from safeharness.task import TaskStatus
from safeharness.task_store import (
    ApprovalConflictError,
    ExecutionConflictError,
    TaskStore,
)
from safeharness.trace import TraceLog

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("SAFEHARNESS_POSTGRES_TESTS") != "1",
        reason="Enable dedicated local PostgreSQL tests",
    ),
]


@pytest.fixture
def pg_store():
    value = os.environ.get("SAFEHARNESS_TEST_POSTGRES_URL")
    if not value:
        value = json.loads(Path("artifacts/m4-private/postgres.json").read_text())[
            "database_url"
        ]
    base = make_url(value)
    assert base.host in {"127.0.0.1", "localhost"} and base.database in {
        "safeharness",
        "safeharness_test",
    }
    schema = "sh_test_" + uuid4().hex
    assert re.fullmatch(r"sh_test_[0-9a-f]{32}", schema)
    engine = engine_for(base)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    url = base.update_query_dict({"options": "-csearch_path=" + schema})
    store = TaskStore(url)
    store.initialize()
    try:
        yield store, PostgresTicketStore(url)
    finally:
        with engine.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))


def pending(store):
    task = store.create()
    gateway = ToolGateway(
        {},
        writable_tickets=frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    result = run_task(
        lambda _: ToolRequest(
            "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
        ),
        gateway.execute,
        lambda _: False,
        max_steps=1,
        store=store,
        task=task,
    )
    record = store.get_approval(result.task.task_id)
    return result.task, record.request


def approve(store, request):
    return store.decide_approval(
        request.approval_id,
        ApprovalOutcome.APPROVED,
        request.intent.arguments_sha256,
        actor=ApprovalActor("pg-reviewer", can_approve=True),
    )


def test_pg_concurrent_decisions_commit_once(pg_store):
    store, _ = pg_store
    task, request = pending(store)
    barrier = Barrier(2)

    def decide(outcome):
        barrier.wait()
        try:
            return store.decide_approval(
                request.approval_id,
                outcome,
                request.intent.arguments_sha256,
                actor=ApprovalActor("reviewer", can_approve=True),
            ).outcome
        except ApprovalConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(decide, [ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED])
        )
    assert sum(result is not None for result in results) == 1
    actual = store.get_approval(task.task_id).decision.outcome
    assert actual in results
    assert store.get(task.task_id).status is (
        TaskStatus.WAITING_APPROVAL
        if actual is ApprovalOutcome.APPROVED
        else TaskStatus.FAILED
    )


def test_pg_concurrent_submission_has_no_orphan_tasks(pg_store):
    from safeharness.contracts import SubmissionConflictError
    from safeharness.database import execute

    store, _ = pg_store
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
    with pytest.raises(SubmissionConflictError):
        store.submit("alice", {"scenario": "ticket_update"}, "same-key")
    with store._engine.connect() as connection:
        assert execute(connection, "SELECT count(*) FROM tasks").scalar_one() == 1
        assert (
            execute(
                connection,
                "SELECT count(*) FROM trace_events WHERE kind='task.created'",
            ).scalar_one()
            == 1
        )


def test_pg_concurrent_resume_commits_business_once(pg_store):
    store, business = pg_store
    task, request = pending(store)
    approve(store, request)
    barrier = Barrier(4)

    def resume(_):
        barrier.wait()
        return (
            TaskStore(store.database)
            .resume(task.task_id, PostgresTicketStore(store.database))
            .status
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(resume, range(4))) == [TaskStatus.SUCCEEDED] * 4
    assert business.ticket() == ("resolved", 1)
    assert store.get_execution(task.task_id)["status"] == "SUCCEEDED"
    assert any(
        event["kind"] == "execution.result"
        for event in TraceLog(store.database, task.task_id).read()
    )


def test_pg_cancel_and_dispatch_are_serialized(pg_store):
    store, business = pg_store
    task, request = pending(store)
    approve(store, request)
    barrier = Barrier(2)

    def action(cancel):
        barrier.wait()
        try:
            return (store.cancel if cancel else store.resume)(
                task.task_id, business
            ).status
        except ExecutionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(action, [True, False]))
    current = store.get(task.task_id)
    assert (current.status, business.ticket()) in [
        (TaskStatus.CANCELLED, ("open", 0)),
        (TaskStatus.SUCCEEDED, ("resolved", 1)),
    ]


def test_pg_real_process_crash_after_business_commit_reconciles(pg_store, tmp_path):
    store, business = pg_store
    task, request = pending(store)
    approve(store, request)
    config = tmp_path / "database.json"
    config.write_text(
        json.dumps({"url": store.database.render_as_string(hide_password=False)})
    )
    script = "import json,os,sys; from pathlib import Path; from uuid import UUID; from safeharness.task_store import TaskStore; from safeharness.pg_ticket_store import PostgresTicketStore; url=json.loads(Path(sys.argv[1]).read_text())['url']; TaskStore(url).resume(UUID(sys.argv[2]),PostgresTicketStore(url),after_business_commit=lambda:os._exit(77))"
    process = subprocess.run(
        [sys.executable, "-c", script, str(config), str(task.task_id)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 77
    assert store.get(task.task_id).status is TaskStatus.RUNNING
    assert business.ticket() == ("resolved", 1)
    assert store.resume(task.task_id, business).status is TaskStatus.SUCCEEDED
    assert store.get_execution(task.task_id)["detail"] == "reconciled_business_receipt"
    assert business.ticket() == ("resolved", 1)
    from safeharness.invocations import InvocationLedger

    ledger = InvocationLedger(store.database, task.task_id).snapshot()
    assert len(ledger["calls"]) == 1 and len(ledger["attempts"]) == 3
    assert {a["status"] for a in ledger["attempts"]} == {
        "WAITING_APPROVAL",
        "RECONCILED",
        "SUCCEEDED",
    }
    assert ledger["calls"][0]["operation_id"] == str(request.approval_id)


def test_pg_two_http_processes_share_approval_and_idempotent_resume(pg_store, tmp_path):
    import socket
    import time

    import httpx

    from safeharness.service import initialize_auth

    store, business = pg_store
    initialize_auth(tmp_path)
    (tmp_path / "postgres.json").write_text(
        json.dumps(
            {"database_url": store.database.render_as_string(hide_password=False)}
        )
    )
    tokens = json.loads((tmp_path / "service-tokens.json").read_text())
    processes, clients, logs = [], [], []
    try:
        for _ in range(2):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            log = (tmp_path / f"http-{port}.log").open("wb")
            logs.append(log)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "safeharness",
                    "serve",
                    "--backend",
                    "postgres",
                    "--config-dir",
                    str(tmp_path),
                    "--port",
                    str(port),
                ],
                stdout=log,
                stderr=log,
            )
            processes.append(process)
            client = httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=20
            )
            clients.append(client)
            for attempt in range(150):
                assert process.poll() is None, "HTTP subprocess exited"
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("HTTP startup timeout")
        owner = {"Authorization": "Bearer " + tokens["alice"]}
        reviewer = {"Authorization": "Bearer " + tokens["reviewer"]}

        def submit(index):
            return clients[index].post(
                "/v1/tasks",
                json={"scenario": "ticket_update"},
                headers={**owner, "Idempotency-Key": "two-process-submit"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            submissions = list(pool.map(submit, [0, 1]))
        assert sorted(s.status_code for s in submissions) == [200, 201]
        assert len({s.json()["task_id"] for s in submissions}) == 1
        task = submissions[0]
        assert (
            clients[1]
            .post(
                "/v1/tasks",
                json={"scenario": "ticket_read"},
                headers={**owner, "Idempotency-Key": "two-process-submit"},
            )
            .status_code
            == 409
        )
        task_id = task.json()["task_id"]
        assert (
            clients[0]
            .post(f"/v1/tasks/{task_id}/run", headers=owner)
            .json()["task_status"]
            == "WAITING_APPROVAL"
        )
        approval = (
            clients[1].get(f"/v1/tasks/{task_id}/approval", headers=reviewer).json()
        )

        def decide(index):
            return (
                clients[index]
                .post(
                    f"/v1/approvals/{approval['approval_id']}/decision",
                    json={
                        "decision": "approve",
                        "arguments_sha256": approval["arguments_sha256"],
                    },
                    headers=reviewer,
                )
                .status_code
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(decide, [0, 1])) == [200, 409]

        def resume(index):
            response = clients[index % 2].post(
                f"/v1/tasks/{task_id}/resume", headers=owner
            )
            assert response.status_code in {200, 409, 429}
            return (
                response.json()["task_status"]
                if response.status_code == 200
                else "busy"
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(resume, range(4)))
            assert "SUCCEEDED" in outcomes and set(outcomes) <= {"SUCCEEDED", "busy"}
        assert business.ticket() == ("resolved", 1)
        trace = clients[1].get(f"/v1/tasks/{task_id}/trace", headers=owner).json()
        assert sum(event["kind"] == "execution.result" for event in trace) == 1
        assert all(token not in json.dumps(trace) for token in tokens.values())
    finally:
        for client in clients:
            client.close()
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


@pytest.mark.parametrize("phase", ["model", "tool", "verification", "completion"])
def test_pg_runtime_exception_converges_with_durable_error(pg_store, phase):
    from unittest.mock import Mock

    from safeharness.runtime import FinalAnswer

    store, business = pg_store
    task = store.create()

    def crash(*args):
        raise RuntimeError("private synthetic failure")

    model = crash if phase == "model" else lambda _: FinalAnswer("ready")
    execute, verify, complete = Mock(), lambda _: True, None
    if phase == "tool":
        model = lambda _: ToolRequest(
            "write_file", {"path": "solution.py", "content": "new"}
        )
        execute = crash
    if phase == "verification":
        verify = crash
    if phase == "completion":
        complete = crash
    result = run_task(
        model, execute, verify, max_steps=1, store=store, task=task, complete=complete
    )
    assert result.task.status is (
        TaskStatus.NEEDS_REVIEW
        if phase in {"tool", "completion"}
        else TaskStatus.FAILED
    )
    restarted = TaskStore(store.database)
    assert restarted.get(task.task_id) == result.task
    assert restarted.get_runtime_failure(task.task_id)["phase"] == phase
    assert TraceLog(store.database, task.task_id).read()[-1]["kind"] == "runtime.error"
    assert business.ticket() == ("open", 0)


def test_pg_runtime_failure_and_cancel_do_not_reopen_or_overwrite_terminal(pg_store):
    from safeharness.failures import RuntimeFailure

    store, business = pg_store
    created = store.create(owner_id="alice", request={"scenario": "ticket_read"})
    task = store.transition(created, TaskStatus.RUNNING)
    barrier = Barrier(2)

    def action(cancel):
        barrier.wait()
        try:
            return (
                store.cancel(task.task_id, business)
                if cancel
                else store.record_runtime_failure(
                    task, RuntimeFailure("model", "model_adapter_error")
                )
            )
        except ExecutionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(action, [True, False]))
    current = store.get(task.task_id)
    assert current.status in {TaskStatus.CANCELLED, TaskStatus.FAILED}
    assert (
        store.get_runtime_failure(task.task_id)["recorded_status"]
        == current.status.value
    )


def test_pg_checkpoint_pause_and_resume_uses_same_runtime(pg_store):
    from test_checkpoints import gateway, model, session

    from safeharness.checkpoints import CheckpointSession

    store, _ = pg_store
    task = store.create()
    paused = run_task(
        model,
        gateway(store, task).execute,
        lambda _: True,
        max_steps=5,
        store=store,
        task=task,
        checkpoint=session(store, task),
        pause_after=1,
    )
    assert paused.run.stop_reason == "paused"
    done = run_task(
        model,
        gateway(store, task).execute,
        lambda r: len(r.tool_results) == 2,
        max_steps=5,
        store=store,
        checkpoint=session(store, task),
        resume=True,
    )
    assert done.task.status is TaskStatus.SUCCEEDED
    metadata = CheckpointSession.metadata(store, task.task_id)
    assert metadata["generation"] == 2 and metadata["state"] == "FINISHED"


def test_pg_approved_agent_continues_to_live_read_and_verification(pg_store):
    from safeharness.harness import Harness
    from safeharness.runtime import FinalAnswer

    store, business = pg_store
    task = store.create()

    def model(results):
        if not results:
            return ToolRequest(
                "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
            )
        if len(results) == 1:
            return ToolRequest("read_ticket", {"ticket_id": "IT-001"})
        return FinalAnswer("Read back the approved result")

    def harness():
        gateway = ToolGateway(
            allowed_tickets=frozenset({"IT-001"}),
            writable_tickets=frozenset({"IT-001"}),
            ticket_reader=lambda _: business.ticket()[0],
            audit=AuditLog(store.database, task.task_id),
        )
        return Harness(
            store,
            model=model,
            execute=gateway.execute,
            verify=lambda r: r.tool_results[-1].value == "resolved",
            configuration={"pg": "approval-loop"},
            approval_workspace=business,
            stateless_model=True,
            max_steps=4,
        )

    assert harness().run(task).task.status is TaskStatus.WAITING_APPROVAL
    request = store.get_approval(task.task_id).request
    store.decide_approval(
        request.approval_id,
        ApprovalOutcome.APPROVED,
        request.intent.arguments_sha256,
        actor=ApprovalActor("synthetic-reviewer", can_approve=True),
    )
    assert harness().resume(task.task_id).task.status is TaskStatus.SUCCEEDED
    assert business.ticket() == ("resolved", 1)
