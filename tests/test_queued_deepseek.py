import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from test_durable_submission import store as shared_store_fixture
from test_postgres import pg_store as shared_pg_fixture
from test_provider import response, tool_call
from test_queue_recovery import Crash, business, expire
from test_queued_approval import decide
from test_service import principals

from safeharness.budget import BudgetLedger
from safeharness.checkpoints import CheckpointSession
from safeharness.database import execute
from safeharness.invocations import InvocationLedger
from safeharness.provider import DeepSeekClient
from safeharness.recovery import scan_expired
from safeharness.scheduling import (
    CURRENT_CLAIM,
    canonical_request,
    claim_next,
    claim_scope,
)
from safeharness.service import TaskInput, TaskService
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.trace import TraceLog
from safeharness.worker import QueueWorker

FAKE_KEY = "sk-" + "synthetic-stage2-credential-12345678"
REQUEST = {
    "scenario": "ticket_read",
    "provider": "deepseek",
    "execution_mode": "queued",
}
store = shared_store_fixture
pg_store = shared_pg_fixture


@pytest.mark.parametrize("action", ["approve", "reject", "cancel"])
def test_deepseek_queued_approval_continues_once_or_stops(
    store, paid_worker, monkeypatch, action
):
    make, _ = paid_worker
    payloads = []

    def transport(payload, timeout):
        payloads.append(payload)
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        if len(results) >= 2:
            return response()
        if results:
            return response(tool_call())
        message = tool_call()
        message["tool_calls"][0]["function"] = {
            "name": "update_ticket_status",
            "arguments": '{"ticket_id":"IT-001","status":"resolved"}',
        }
        return response(message)

    monkeypatch.setattr(
        "safeharness.service.DeepSeekClient",
        lambda credentials, ledger: DeepSeekClient(
            credentials, ledger, transport=transport
        ),
    )
    task, _ = store.submit("alice", {**REQUEST, "scenario": "ticket_update_read"})
    first = make()
    assert first.run_once()["status"] == "waiting_approval"
    assert first.service.business.ticket() == ("open", 0)
    assert make().run_once()["status"] == "idle"
    request = store.get_approval(task.task_id).request
    if action == "cancel":
        store.cancel(task.task_id, first.service.business)
    else:
        decide(store, request, action == "approve")
    if action == "approve":
        assert make().run_once()["task_status"] == "SUCCEEDED"
        assert first.service.business.ticket() == ("resolved", 1)
        assert len(payloads) == 3
        evidence = InvocationLedger(store.database, task.task_id).snapshot()
        assert any(
            c["tool_name"] == "read_ticket" and c["status"] == "SUCCEEDED"
            for c in evidence["calls"]
        )
    else:
        assert first.service.business.ticket() == ("open", 0)
        assert len(payloads) == 1
        assert store.get(task.task_id).status.value == (
            "CANCELLED" if action == "cancel" else "FAILED"
        )
    assert make().run_once()["status"] == "idle"


@pytest.fixture
def paid_worker(store, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    credentials = tmp_path / "key.txt"
    credentials.write_text(FAKE_KEY, encoding="utf-8")
    settings = {
        "allow_deepseek": True,
        "credentials_path": credentials,
        "budget_path": tmp_path / "budget.sqlite3",
    }
    payloads = []

    def transport(payload, timeout):
        payloads.append(payload)
        return (
            response()
            if any(m["role"] == "tool" for m in payload["messages"])
            else response(tool_call())
        )

    monkeypatch.setattr(
        "safeharness.service.DeepSeekClient",
        lambda credentials, ledger: DeepSeekClient(
            credentials, ledger, transport=transport
        ),
    )
    return lambda: QueueWorker(
        store, business(store), principals(), task_service_settings=settings
    ), payloads


@pytest.mark.parametrize("fault", ["saved", "unknown", "corrupt", "missing"])
def test_recovery_after_model_before_checkpoint(store, paid_worker, fault):
    make, payloads = paid_worker
    task, _ = store.submit("alice", REQUEST)
    old = claim_next(store, "crashed")
    complete = DeepSeekClient.complete

    def crash(client, *args, **kwargs):
        if fault != "unknown":
            complete(client, *args, **kwargs)
        else:
            InvocationLedger(store.database, task.task_id).start(
                "deepseek-flash", "model", "deepseek"
            )
        raise Crash()

    with (
        claim_scope(old),
        patch.object(DeepSeekClient, "complete", crash),
        patch.object(CheckpointSession, "release", return_value=None),
        pytest.raises(Crash),
    ):
        worker = make()
        worker.service.run(task.task_id, worker.service.principals["alice"])
    if fault == "corrupt":
        with store._engine.begin() as connection:
            execute(
                connection,
                "UPDATE call_artifacts SET sha256=? WHERE task_id=? AND kind='model_response'",
                ("0" * 64, task.task_id.hex),
            )
    if fault == "missing":
        with store._engine.begin() as connection:
            execute(
                connection,
                "DELETE FROM call_artifacts WHERE task_id=? AND kind='model_response'",
                (task.task_id.hex,),
            )
    deadline = store.scheduling(task.task_id)["execution_deadline_at"]
    expire(store, task.task_id)
    decision = scan_expired(store)["decisions"][0]
    if fault == "unknown":
        assert decision["action"] == "review"
        assert decision["code"] == "unconfirmed_call"
        assert payloads == []
        return
    if fault in {"corrupt", "missing"}:
        assert decision["action"] == "review"
        assert decision["code"] == "invalid_model_response_evidence"
        assert len(payloads) == 1
        return
    assert decision["action"] == "resume"
    result = make().run_once()
    assert result["scheduling"]["execution_deadline_at"] == deadline
    assert result["task_status"] == "SUCCEEDED"
    assert len(payloads) == 2  # Original read request + final answer; no paid replay.
    assert any(
        e["kind"] == "model.response_reused"
        for e in TraceLog(store.database, task.task_id).read()
    )


def test_late_provider_response_cannot_dispatch_after_lease_loss(
    store, paid_worker, monkeypatch
):
    make, _ = paid_worker
    task, _ = store.submit("alice", REQUEST)
    entered, release = Event(), Event()
    claims = []

    def factory(credentials, ledger):
        claims.append(CURRENT_CLAIM.get())

        def transport(payload, timeout):
            entered.set()
            assert release.wait(10)
            return response(tool_call())

        return DeepSeekClient(credentials, ledger, transport=transport)

    monkeypatch.setattr("safeharness.service.DeepSeekClient", factory)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(make().run_once)
        try:
            assert entered.wait(8)
            claims[0].fenced.set()
            expire(store, task.task_id)
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "lease_lost"
    evidence = InvocationLedger(store.database, task.task_id).snapshot()
    assert all(call["kind"] == "model" for call in evidence["calls"])
    assert evidence["artifacts"] == []
    assert scan_expired(store)["decisions"][0]["code"] == "unconfirmed_call"


def test_queued_deepseek_is_admitted_only_for_ticket_tasks():
    assert TaskInput.model_validate(REQUEST).provider == "deepseek"
    assert canonical_request(REQUEST) == REQUEST
    with pytest.raises(ValidationError):
        TaskInput.model_validate({**REQUEST, "scenario": "coding_fix"})
    with pytest.raises(ValidationError):
        TaskInput.model_validate({**REQUEST, "endpoint": "https://example.invalid"})
    with pytest.raises(ValueError, match="known providers"):
        canonical_request({**REQUEST, "provider": "other"})


def test_disabled_worker_stops_queued_deepseek_before_credentials_or_network(store):
    task, _ = store.submit("alice", REQUEST)
    worker = QueueWorker(store, business(store), principals())

    result = worker.run_once()

    assert result["task_status"] == TaskStatus.FAILED.value
    assert result["last_runtime_error"]["code"] == "worker_setup_rejected"
    assert store.scheduling(task.task_id)["state"] == "FINISHED"


def test_expired_deepseek_task_without_invocations_is_safe_to_start(store):
    task, _ = store.submit("alice", REQUEST)
    claim = claim_next(store, "dead-deepseek-worker", lease_seconds=1)
    assert claim.task_id == task.task_id
    expire(store, task.task_id)

    report = scan_expired(store)

    assert report["decisions"][0]["action"] == "start"
    assert store.scheduling(task.task_id)["state"] == "READY"


def test_http_service_rejects_paid_provider_when_admin_disabled(tmp_path):
    store = TaskStore(tmp_path / "control.sqlite3")
    service = TaskService(store, TicketStore(store.database), principals())

    with pytest.raises(HTTPException, match="paid_provider_disabled") as error:
        service.create(TaskInput.model_validate(REQUEST), principals()[0])

    assert getattr(error.value, "status_code", None) == 403


def test_enabled_worker_uses_shared_budget_and_records_real_provider_contract(
    store, tmp_path, monkeypatch
):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    credentials = tmp_path / "provider-key.txt"
    credentials.write_text(FAKE_KEY, encoding="utf-8")
    budget_path = tmp_path / "shared-budget.sqlite3"
    task, _ = store.submit("alice", REQUEST)
    responses = []

    def transport(payload, timeout):
        messages = payload["messages"]
        if any(message["role"] == "tool" for message in messages):
            assistant = {"role": "assistant", "content": "IT-001 is open."}
            finish_reason = "stop"
        else:
            assistant = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-it-001",
                        "type": "function",
                        "function": {
                            "name": "read_ticket",
                            "arguments": '{"ticket_id":"IT-001"}',
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        responses.append(payload)
        return {
            "model": "deepseek-flash",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": finish_reason, "message": assistant}],
        }

    def client_factory(credentials, ledger, **kwargs):
        return DeepSeekClient(
            credentials, ledger, transport=transport, sleeper=lambda _: None, **kwargs
        )

    monkeypatch.setattr("safeharness.service.DeepSeekClient", client_factory)
    provider_settings = {
        "allow_deepseek": True,
        "credentials_path": credentials,
        "budget_path": budget_path,
        "budget_id": "queued-deepseek-test",
        "budget_cny": "0.50",
        "max_provider_calls": 10,
    }
    worker = QueueWorker(
        store,
        business(store),
        principals(),
        task_service_settings=provider_settings,
    )

    result = worker.run_once()

    assert result["task_status"] == TaskStatus.SUCCEEDED.value
    assert len(responses) == 2
    assert all(response["model"] == "deepseek-flash" for response in responses)
    assert all(response["max_tokens"] == 768 for response in responses)
    ledger = BudgetLedger(
        budget_path,
        budget_id="queued-deepseek-test",
        limit_cny=Decimal("0.50"),
        max_calls=10,
    )
    usage = ledger.usage(task.task_id)
    assert usage["calls"] == 2
    assert usage["unknown_usage_calls"] == 0
    assert usage["cost_upper_cny"] == "0.001"
    assert usage["pricing_version"] == BudgetLedger.PRICING_VERSION


def test_worker_budget_store_location_is_bound_into_resume_configuration(
    store, tmp_path
):
    task_service = QueueWorker(
        store,
        business(store),
        principals(),
        task_service_settings={
            "allow_deepseek": True,
            "budget_path": tmp_path / "a.sqlite",
        },
    ).service
    other_service = QueueWorker(
        store,
        business(store),
        principals(),
        task_service_settings={
            "allow_deepseek": True,
            "budget_path": tmp_path / "b.sqlite",
        },
    ).service

    assert (
        task_service.provider_configuration["budget_store_sha256"]
        != other_service.provider_configuration["budget_store_sha256"]
    )
    assert str(tmp_path) not in json.dumps(task_service.provider_configuration)
