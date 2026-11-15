import json
import multiprocessing
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from safeharness.audit import AuditLog
from safeharness.budget import BudgetError, BudgetLedger
from safeharness.execution import ToolGateway, public_tool_schemas
from safeharness.invocations import InvocationLedger
from safeharness.provider import (
    Credentials,
    DeepSeekAdapter,
    DeepSeekClient,
    ProviderError,
    load_credentials,
)
from safeharness.runtime import run_task
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore
from safeharness.trace import TraceLog


def _process_reserve_budget(path, task_id, output):
    try:
        ledger = BudgetLedger(path, budget_id="process-shared", max_calls=3)
        ledger.reserve(uuid4(), UUID(task_id), 100, 20)
        output.put("reserved")
    except BudgetError:
        output.put("exhausted")


KEY = "sk-" + "synthetic-not-a-real-key-12345678"


@pytest.fixture
def provider_context(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = store.create()
    ledger = BudgetLedger(tmp_path / "budget.sqlite3")
    return store, task, TraceLog(store.database, task.task_id), ledger


def response(message=None):
    return {
        "model": "deepseek-flash",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "choices": [
            {
                "finish_reason": "tool_calls"
                if message and message.get("tool_calls")
                else "stop",
                "message": message or {"role": "assistant", "content": "OK"},
            }
        ],
    }


def tool_call(ticket="IT-001"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_example",
                "type": "function",
                "function": {
                    "name": "read_ticket",
                    "arguments": json.dumps({"ticket_id": ticket}),
                },
            }
        ],
    }


def test_budget_persists_and_reserves_failed_unknown_usage(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = BudgetLedger(path, limit_cny=Decimal("0.001"), max_calls=3)
    attempt, task = uuid4(), uuid4()
    ledger.reserve(attempt, task, 100, 20)
    reopened = BudgetLedger(path, limit_cny=Decimal("0.001"), max_calls=3)
    assert reopened.usage()["cost_upper_cny"] == "0.0005"
    assert reopened.usage()["unknown_usage_calls"] == 1
    assert reopened.settle(attempt, 20, 5) == "0.00011"
    assert ledger.usage()["cost_upper_cny"] == "0.00011"
    with pytest.raises(BudgetError):
        ledger.settle(attempt, 20, 5)
    with pytest.raises(BudgetError):
        BudgetLedger(path, limit_cny=Decimal(1), max_calls=3)


def test_budget_reprices_legacy_settled_usage_and_unknown_reservations(tmp_path):
    path = tmp_path / "legacy-budget.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE budgets (budget_id TEXT PRIMARY KEY, limit_micro INTEGER NOT NULL, max_calls INTEGER NOT NULL);"
        "CREATE TABLE requests (attempt_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL, task_id TEXT NOT NULL, reserved_micro INTEGER NOT NULL, settled_micro INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL);"
        "INSERT INTO budgets VALUES ('legacy',1000000,10);"
        "INSERT INTO requests VALUES ('settled','legacy','task-a',360,360,100,20,'SETTLED','2026-11-01T00:00:00+00:00');"
        "INSERT INTO requests VALUES ('unknown','legacy','task-b',360,NULL,NULL,NULL,'RESERVED','2026-11-01T00:00:00+00:00');"
    )
    connection.close()

    ledger = BudgetLedger(path, budget_id="legacy", limit_cny="1.00", max_calls=10)

    usage = ledger.usage()
    assert usage["calls"] == 2
    assert usage["cost_upper_cny"] == "0.00104"
    assert usage["unknown_usage_calls"] == 1
    assert usage["pricing_versions"] == [BudgetLedger.PRICING_VERSION]


def test_concurrent_reservations_cannot_exceed_call_limit(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", max_calls=3)

    def reserve(_):
        try:
            ledger.reserve(uuid4(), uuid4(), 100, 20)
            return True
        except BudgetError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(12))) == 3
    assert ledger.usage()["calls"] == 3


def test_multiprocess_reservations_share_one_persistent_budget(tmp_path):
    path = tmp_path / "multiprocess-budget.sqlite3"
    BudgetLedger(path, budget_id="process-shared", max_calls=3)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    task_id = uuid4().hex
    processes = [
        context.Process(target=_process_reserve_budget, args=(path, task_id, output))
        for _ in range(8)
    ]

    for process in processes:
        process.start()
    results = [output.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert results.count("reserved") == 3
    assert results.count("exhausted") == 5
    assert (
        BudgetLedger(path, budget_id="process-shared", max_calls=3).usage()["calls"]
        == 3
    )


def test_credentials_are_not_represented_or_echoed(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "key.txt"
    path.write_text(KEY)
    credentials = load_credentials(path)
    assert credentials.api_key == KEY and KEY not in repr(credentials)
    path.write_text(KEY + "\nsk-second-credential-1234567890")
    with pytest.raises(ProviderError, match="credentials_ambiguous"):
        load_credentials(path)


def test_retry_is_bounded_and_each_attempt_is_reserved(provider_context):
    _, task, trace, ledger = provider_context
    transport = Mock(
        side_effect=[ProviderError("rate_limited", retryable=True), response()]
    )
    client = DeepSeekClient(
        Credentials(KEY), ledger, transport=transport, sleeper=Mock()
    )
    assert (
        client.complete(
            [{"role": "user", "content": "hello"}], task_id=task.task_id, trace=trace
        )["content"]
        == "OK"
    )
    assert transport.call_count == 2
    assert ledger.usage()["calls"] == 2 and ledger.usage()["unknown_usage_calls"] == 1
    kinds = [item["kind"] for item in trace.read()]
    assert kinds.count("model.request") == 2 and kinds.count("model.error") == 1
    assert KEY not in trace.jsonl() and "hello" not in trace.jsonl()
    invocations = InvocationLedger(trace.database, task.task_id).snapshot()
    assert len(invocations["calls"]) == 1 and len(invocations["attempts"]) == 2
    assert len({a["attempt_id"] for a in invocations["attempts"]}) == 2
    assert {a["call_id"] for a in invocations["attempts"]} == {
        invocations["calls"][0]["call_id"]
    }
    assert invocations["calls"][0]["status"] == "SUCCEEDED"
    assert invocations["runs"][0]["status"] == "SUCCEEDED"
    events = [e for e in trace.read() if e["kind"].startswith("model.")]
    assert {e["payload"]["call_id"] for e in events} == {
        invocations["calls"][0]["call_id"]
    }
    assert {e["payload"]["attempt_id"] for e in events} == {
        a["attempt_id"] for a in invocations["attempts"]
    }
    assert all(e["run_id"] == invocations["runs"][0]["run_id"] for e in events)


def test_normal_identical_prompts_are_not_cached(
    provider_context,
):
    _, task, trace, ledger = provider_context
    transport = Mock(return_value=response())
    client = DeepSeekClient(Credentials(KEY), ledger, transport=transport)
    messages = [{"role": "user", "content": "hello"}]

    first = client.complete(messages, task_id=task.task_id, trace=trace)
    replayed = client.complete(messages, task_id=task.task_id, trace=trace)

    assert first == replayed
    assert transport.call_count == 2
    assert ledger.usage()["calls"] == 2
    assert not any(event["kind"] == "model.response_reused" for event in trace.read())


@pytest.mark.parametrize(
    "code,retryable,expected",
    [
        ("authentication_failed", False, 1),
        ("insufficient_balance", False, 1),
        ("provider_connection_error", False, 1),
        ("provider_timeout", False, 1),
    ],
)
def test_provider_failures_do_not_retry_forever(
    provider_context, code, retryable, expected
):
    _, task, trace, ledger = provider_context
    transport = Mock(side_effect=ProviderError(code, retryable=retryable))
    client = DeepSeekClient(
        Credentials(KEY), ledger, transport=transport, sleeper=Mock()
    )
    with pytest.raises(ProviderError, match=code):
        client.complete(
            [{"role": "user", "content": "hello"}], task_id=task.task_id, trace=trace
        )
    assert transport.call_count == expected
    if code in {"provider_connection_error", "provider_timeout"}:
        assert ledger.usage()["calls"] == 1
        assert ledger.usage()["unknown_usage_calls"] == 1


def test_exhausted_budget_never_dispatches_http(provider_context, tmp_path):
    _, task, trace, _ = provider_context
    ledger = BudgetLedger(tmp_path / "tiny.sqlite3", limit_cny=Decimal("0.000001"))
    transport = Mock()
    client = DeepSeekClient(Credentials(KEY), ledger, transport=transport)
    with pytest.raises(ProviderError, match="provider_budget_exhausted"):
        client.complete(
            [{"role": "user", "content": "hello"}], task_id=task.task_id, trace=trace
        )
    transport.assert_not_called()


def test_bad_usage_and_truncation_are_not_executed(provider_context):
    _, task, trace, ledger = provider_context
    value = response(tool_call())
    value["choices"][0]["finish_reason"] = "length"
    client = DeepSeekClient(
        Credentials(KEY), ledger, transport=Mock(return_value=value)
    )
    with pytest.raises(ProviderError, match="provider_output_truncated"):
        client.complete([], task_id=task.task_id, trace=trace)
    value["usage"]["completion_tokens"] = 100000
    with pytest.raises(ProviderError, match="provider_invalid_usage"):
        client.complete([], task_id=task.task_id, trace=trace)


def test_adapter_returns_tool_feedback_and_uses_gateway(provider_context):
    store, task, trace, ledger = provider_context
    transport = Mock(
        side_effect=[response(tool_call("IT-002")), response(tool_call()), response()]
    )
    client = DeepSeekClient(Credentials(KEY), ledger, transport=transport)
    adapter = DeepSeekAdapter(
        client,
        instruction="Read IT-001",
        environment="ticket",
        scope={"readable_tickets": ["IT-001"]},
        task_id=task.task_id,
        trace=trace,
        deadline=time.monotonic() + 10,
    )
    gateway = ToolGateway(
        {"IT-001": "open", "IT-002": "protected"},
        frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    result = run_task(
        adapter,
        gateway.execute,
        lambda run: run.tool_results[-1].value == "open",
        max_steps=5,
        store=store,
        task=task,
    )
    assert result.task.status is TaskStatus.SUCCEEDED
    assert result.run.tool_results[0].status == "denied"
    assert any(
        message["role"] == "tool" and "resource_out_of_scope" in message["content"]
        for message in adapter.messages
    )
    assert not any(
        schema["function"]["name"].startswith("_")
        for schema in public_tool_schemas("coding")
    )


def test_duplicate_call_ids_fail_before_tools_and_persist_failed_state(
    provider_context,
):
    store, task, trace, ledger = provider_context
    message = tool_call()
    message["tool_calls"].append(dict(message["tool_calls"][0]))
    client = DeepSeekClient(
        Credentials(KEY), ledger, transport=Mock(return_value=response(message))
    )
    adapter = DeepSeekAdapter(
        client,
        instruction="Read",
        environment="ticket",
        scope={},
        task_id=task.task_id,
        trace=trace,
        deadline=time.monotonic() + 10,
    )
    execute = Mock()
    result = run_task(adapter, execute, Mock(), max_steps=3, store=store, task=task)
    assert result.task.status is TaskStatus.FAILED
    assert result.run.error_code == "provider_invalid_tool_call"
    execute.assert_not_called()


def test_total_transport_deadline_and_context_cap(provider_context):
    _, task, trace, ledger = provider_context
    transport = Mock(side_effect=lambda *_: (time.sleep(0.5), response())[1])
    client = DeepSeekClient(
        Credentials(KEY), ledger, transport=transport, timeout=0.02, max_attempts=2
    )
    started = time.monotonic()
    with pytest.raises(ProviderError, match="provider_timeout"):
        client.complete([], task_id=task.task_id, trace=trace)
    assert time.monotonic() - started < 0.35
    assert transport.call_count == 1
    assert ledger.usage()["calls"] == 1
    assert ledger.usage()["unknown_usage_calls"] == 1
    with pytest.raises(ProviderError, match="provider_context_limit"):
        client.complete(
            [{"role": "user", "content": "x" * 30000}],
            task_id=task.task_id,
            trace=trace,
        )
    assert transport.call_count == 1


def test_model_batch_is_serialized_through_gateway_with_separate_steps(
    provider_context,
):
    store, task, trace, ledger = provider_context
    message = tool_call()
    second = dict(message["tool_calls"][0])
    second["id"] = "call_second"
    message["tool_calls"].append(second)
    transport = Mock(side_effect=[response(message), response()])
    client = DeepSeekClient(Credentials(KEY), ledger, transport=transport)
    adapter = DeepSeekAdapter(
        client,
        instruction="Read",
        environment="ticket",
        scope={},
        task_id=task.task_id,
        trace=trace,
        deadline=time.monotonic() + 10,
    )
    gateway = ToolGateway(
        {"IT-001": "open"},
        frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    result = run_task(
        adapter,
        gateway.execute,
        lambda run: len(run.tool_results) == 2,
        max_steps=3,
        store=store,
        task=task,
    )
    assert result.task.status is TaskStatus.SUCCEEDED
    assert transport.call_count == 2
    assert [
        message["tool_call_id"]
        for message in adapter.messages
        if message["role"] == "tool"
    ] == ["call_example", "call_second"]
    intents = [event for event in trace.read() if event["kind"] == "tool.intent"]
    assert (
        len(intents) == 2
        and intents[0]["payload"]["call_id"] != intents[1]["payload"]["call_id"]
    )


def test_approval_pauses_before_remaining_queued_calls(provider_context):
    store, task, trace, ledger = provider_context
    message = tool_call()
    update = {
        "id": "call_update",
        "type": "function",
        "function": {
            "name": "update_ticket_status",
            "arguments": json.dumps({"ticket_id": "IT-001", "status": "resolved"}),
        },
    }
    message["tool_calls"].insert(0, update)
    transport = Mock(return_value=response(message))
    client = DeepSeekClient(Credentials(KEY), ledger, transport=transport)
    adapter = DeepSeekAdapter(
        client,
        instruction="Request update",
        environment="ticket",
        scope={},
        task_id=task.task_id,
        trace=trace,
        deadline=time.monotonic() + 10,
    )
    gateway = ToolGateway(
        {"IT-001": "open"},
        frozenset({"IT-001"}),
        writable_tickets=frozenset({"IT-001"}),
        audit=AuditLog(store.database, task.task_id),
    )
    result = run_task(
        adapter, gateway.execute, lambda _: False, max_steps=3, store=store, task=task
    )
    assert result.task.status is TaskStatus.WAITING_APPROVAL
    assert len(result.run.tool_results) == 1 and len(adapter.queued) == 1
    assert transport.call_count == 1
