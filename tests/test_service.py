import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from safeharness.service import Principal, create_app, initialize_auth, load_principals
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore


def principals():
    return [
        Principal(
            subject,
            hashlib.sha256(("test-" + subject).encode()).hexdigest(),
            frozenset(roles),
            writable_tickets=frozenset({"IT-001"}) if write else frozenset(),
        )
        for subject, roles, write in [
            ("alice", ["requester", "approver"], True),
            ("bob", ["requester"], True),
            ("reviewer", ["approver"], True),
            ("scanner", ["scanner"], False),
        ]
    ]


def headers(subject):
    return {"Authorization": "Bearer test-" + subject}


@pytest.fixture
def http_service(tmp_path):
    store = TaskStore(tmp_path / "control.sqlite3")
    business = TicketStore(store.database)
    app = create_app(store, business, principals())
    with TestClient(app) as client:
        yield client, store, business


def create_run(client, scenario="ticket_update", subject="alice", **values):
    created = client.post(
        "/v1/tasks", json={"scenario": scenario, **values}, headers=headers(subject)
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]
    result = client.post(f"/v1/tasks/{task_id}/run", headers=headers(subject))
    assert result.status_code == 200, result.text
    return task_id, result.json()


def test_http_approval_restart_resume_and_trace(http_service):
    client, store, business = http_service
    task_id, pending = create_run(client)
    assert pending["task_status"] == "WAITING_APPROVAL"
    request = client.get(
        f"/v1/tasks/{task_id}/approval", headers=headers("reviewer")
    ).json()
    assert business.ticket() == ("open", 0)
    decision = client.post(
        f"/v1/approvals/{request['approval_id']}/decision",
        json={"decision": "approve", "arguments_sha256": request["arguments_sha256"]},
        headers=headers("reviewer"),
    )
    assert decision.status_code == 200 and decision.json()["decided_by"] == "reviewer"
    restarted = create_app(
        TaskStore(store.database), TicketStore(store.database), principals()
    )
    with TestClient(restarted) as other:
        for _ in range(2):
            assert (
                other.post(
                    f"/v1/tasks/{task_id}/resume", headers=headers("alice")
                ).json()["task_status"]
                == "SUCCEEDED"
            )
        assert (
            other.get("/v1/ticket", headers=headers("alice")).json()["write_count"] == 1
        )
        trace = other.get(f"/v1/tasks/{task_id}/trace", headers=headers("alice")).json()
        assert any(event["kind"] == "execution.result" for event in trace)
        assert "test-alice" not in json.dumps(trace)
        ledger = other.get(f"/v1/tasks/{task_id}/invocations", headers=headers("alice"))
        assert ledger.status_code == 200
        assert len(ledger.json()["calls"]) == 1 and len(ledger.json()["attempts"]) == 2
        assert "content" not in json.dumps(ledger.json()["artifacts"])
        assert ledger.json()["calls"][0]["operation_id"] == request["approval_id"]


def test_http_identity_ownership_and_self_approval(http_service):
    client, _, business = http_service
    assert client.post("/v1/tasks", json={"scenario": "ticket_read"}).status_code == 401
    task_id, _ = create_run(client)
    for suffix in ["", "/approval", "/trace", "/trace.jsonl", "/invocations"]:
        assert (
            client.get(
                f"/v1/tasks/{task_id}" + suffix, headers=headers("bob")
            ).status_code
            == 404
        )
    assert (
        client.post(f"/v1/tasks/{task_id}/resume", headers=headers("bob")).status_code
        == 404
    )
    request = client.get(
        f"/v1/tasks/{task_id}/approval", headers=headers("alice")
    ).json()
    body = {"decision": "approve", "arguments_sha256": request["arguments_sha256"]}
    assert (
        client.post(
            f"/v1/approvals/{request['approval_id']}/decision",
            json=body,
            headers=headers("alice"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/approvals/{request['approval_id']}/decision",
            json=body,
            headers=headers("bob"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/tasks", json={"scenario": "ticket_update"}, headers=headers("scanner")
        ).status_code
        == 403
    )
    assert business.ticket() == ("open", 0)


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"owner_id": "reviewer"},
        {"database_url": "postgresql://elsewhere"},
        {"roles": ["approver"]},
    ],
)
def test_http_extra_fields_cannot_choose_authority(http_service, extra):
    client, _, business = http_service
    result = client.post(
        "/v1/tasks",
        json={"scenario": "ticket_update", **extra},
        headers=headers("alice"),
    )
    assert result.status_code == 422
    assert business.ticket() == ("open", 0)


def test_http_digest_replay_scope_and_body_budget(http_service):
    client, _, business = http_service
    task_id, _ = create_run(client)
    request = client.get(
        f"/v1/tasks/{task_id}/approval", headers=headers("reviewer")
    ).json()
    url = f"/v1/approvals/{request['approval_id']}/decision"
    assert (
        client.post(
            url,
            json={"decision": "approve", "arguments_sha256": "0" * 64},
            headers=headers("reviewer"),
        ).status_code
        == 409
    )
    assert (
        client.post(
            url,
            json={
                "decision": "reject",
                "arguments_sha256": request["arguments_sha256"],
            },
            headers=headers("reviewer"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            url,
            json={
                "decision": "approve",
                "arguments_sha256": request["arguments_sha256"],
            },
            headers=headers("reviewer"),
        ).status_code
        == 409
    )
    assert (
        client.post(f"/v1/tasks/{task_id}/resume", headers=headers("alice")).status_code
        == 409
    )
    assert (
        client.post(
            "/v1/tasks",
            content=b"x" * 20000,
            headers={**headers("alice"), "Content-Type": "application/json"},
        ).status_code
        == 413
    )
    _, outside = create_run(client, ticket_id="IT-002")
    assert outside["task_status"] == "FAILED" and business.ticket() == ("open", 0)


def test_local_credentials_are_generated_once_without_printing_tokens(tmp_path):
    first = initialize_auth(tmp_path)
    before = (tmp_path / "service-tokens.json").read_text()
    assert initialize_auth(tmp_path)["reused"]
    assert (tmp_path / "service-tokens.json").read_text() == before
    assert len(load_principals(tmp_path / "service-auth.json")) == 6
    assert all(token not in json.dumps(first) for token in json.loads(before).values())


def test_cancel_created_task_prevents_dispatch(http_service):
    client, _, business = http_service
    task_id = client.post(
        "/v1/tasks", json={"scenario": "ticket_read"}, headers=headers("alice")
    ).json()["task_id"]
    assert (
        client.post(f"/v1/tasks/{task_id}/cancel", headers=headers("alice")).json()[
            "task_status"
        ]
        == "CANCELLED"
    )
    assert (
        client.post(f"/v1/tasks/{task_id}/run", headers=headers("alice")).status_code
        == 409
    )
    assert business.ticket() == ("open", 0)


def test_read_verifier_binds_requested_ticket_even_if_model_reads_another(
    tmp_path, monkeypatch
):
    from safeharness.agent import StatelessAgent
    from safeharness.execution import ToolRequest
    from safeharness.runtime import FinalAnswer

    monkeypatch.setattr("safeharness.service.load_credentials", lambda *_: None)
    monkeypatch.setattr(
        "safeharness.service.DeepSeekAdapter",
        lambda *args, **kwargs: StatelessAgent(
            lambda results: (
                ToolRequest("read_ticket", {"ticket_id": "IT-001"})
                if not results
                else FinalAnswer("open")
            )
        ),
    )
    store = TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        store,
        TicketStore(store.database),
        principals(),
        allow_deepseek=True,
        budget_path=tmp_path / "budget.sqlite3",
    )
    with TestClient(app) as client:
        _, result = create_run(
            client, "ticket_read", provider="deepseek", ticket_id="IT-002"
        )
        assert result["task_status"] == "FAILED"


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1", reason="Enable real Docker tests"
)
def test_http_coding_uses_existing_independent_verifier(http_service):
    client, _, business = http_service
    task_id, result = create_run(client, "coding_fix")
    assert result["task_status"] == "SUCCEEDED"
    trace = client.get(f"/v1/tasks/{task_id}/trace", headers=headers("alice")).json()
    assert any(
        event["payload"].get("tool_name") == "_verify_coding_contract"
        for event in trace
    )
    assert business.ticket() == ("open", 0)


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1", reason="Enable real Docker tests"
)
def test_cancel_running_coding_reclaims_worker(http_service, monkeypatch):
    client, _, _ = http_service
    from safeharness.coding_demo import fixture

    files = fixture()
    files["tests/test_solution.py"] = "import time\ndef test_slow(): time.sleep(60)\n"
    monkeypatch.setattr("safeharness.service.fixture", lambda: files)
    task_id = client.post(
        "/v1/tasks", json={"scenario": "coding_fix"}, headers=headers("alice")
    ).json()["task_id"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            client.post, f"/v1/tasks/{task_id}/run", headers=headers("alice")
        )
        deadline = time.monotonic() + 15
        found = False
        while time.monotonic() < deadline:
            events = client.get(
                f"/v1/tasks/{task_id}/trace", headers=headers("alice")
            ).json()
            if any(
                event["kind"] == "tool.intent"
                and event["payload"]["tool_name"] == "run_tests"
                for event in events
            ):
                found = True
                break
            time.sleep(0.1)
        assert found
        assert (
            client.post(f"/v1/tasks/{task_id}/cancel", headers=headers("alice")).json()[
                "task_status"
            ]
            == "CANCELLED"
        )
        assert running.result(timeout=10).json()["task_status"] == "CANCELLED"
    remaining = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=safeharness.task=" + task_id],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert not remaining.stdout.strip()
