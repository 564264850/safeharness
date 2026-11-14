import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_service import headers, principals

from safeharness.service import Principal, create_app
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def complete(self, messages, **kwargs):
        if not kwargs.get("tools"):
            return {"role": "assistant", "content": "proxy-ok"}
        if messages[-1]["role"] == "tool":
            return {
                "role": "assistant",
                "content": json.loads(messages[-1]["content"]).get("value") or "Denied",
            }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "scan_call",
                    "type": "function",
                    "function": {
                        "name": "read_ticket",
                        "arguments": '{"ticket_id":"IT-002"}',
                    },
                }
            ],
        }


@pytest.mark.parametrize(
    "profile,expected", [("hardened", False), ("vulnerable-lab", True)]
)
def test_scan_reply_trace_and_independent_canary_evidence(
    tmp_path, monkeypatch, profile, expected
):
    monkeypatch.setattr("safeharness.scanning_service.DeepSeekClient", FakeClient)
    monkeypatch.setattr(
        "safeharness.scanning_service.load_credentials", lambda *_: None
    )
    store = TaskStore(tmp_path / "tasks.sqlite3")
    business = TicketStore(store.database)
    app = create_app(
        store,
        business,
        principals(),
        scan_profile=profile,
        allow_deepseek=True,
        budget_path=tmp_path / "budget.sqlite3",
    )
    regression = json.loads(
        (
            Path(__file__).parents[1]
            / "src/safeharness/datasets/m4-scan-regression.json"
        ).read_text()
    )
    scan_id = str(uuid4())
    with TestClient(app) as client:
        response = client.post(
            "/scan",
            json={"prompt": regression["prompt"], "scan_id": scan_id},
            headers=headers("scanner"),
        )
        assert response.status_code == 200, response.text
        assert response.json()["reply"] and response.json()["canary_seen"] is expected
        task_id = response.json()["task_id"]
        observations = client.get(
            f"/v1/scans/{scan_id}/runs", headers=headers("scanner")
        ).json()
        assert observations["runs"][0]["canary_seen"] is expected
        assert observations["runs"][0]["write_count"] == 0
        assert (
            observations["runs"][0]["prompt_sha256"]
            == hashlib.sha256(regression["prompt"].encode()).hexdigest()
        )
        trace = client.get(
            f"/v1/tasks/{task_id}/trace", headers=headers("scanner")
        ).json()
        assert any(event["kind"] == "tool.policy" for event in trace)
        assert "SH_SYNTHETIC_CANARY_" not in json.dumps(trace)
        assert (
            client.post(
                f"/v1/tasks/{task_id}/resume", headers=headers("scanner")
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/chat/completions",
                json={"model": "any", "messages": [{"role": "user", "content": "x"}]},
                headers=headers("scanner"),
            ).status_code
            == 403
        )
    assert business.ticket() == ("open", 0)


def test_scan_model_proxy_unexpected_exception_converges(tmp_path, monkeypatch):
    from uuid import UUID

    from safeharness.database import execute

    class BrokenClient(FakeClient):
        def complete(self, *args, **kwargs):
            raise RuntimeError("private proxy error")

    monkeypatch.setattr("safeharness.scanning_service.DeepSeekClient", BrokenClient)
    monkeypatch.setattr(
        "safeharness.scanning_service.load_credentials", lambda *_: None
    )
    actor = Principal(
        "model", hashlib.sha256(b"test-model").hexdigest(), frozenset({"model_proxy"})
    )
    store = TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        store,
        TicketStore(store.database),
        [*principals(), actor],
        scan_profile="hardened",
        allow_deepseek=True,
        budget_path=tmp_path / "budget.sqlite3",
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "x"}]},
            headers=headers("model"),
        )
        assert response.status_code == 400
        assert (
            response.json()["detail"] == "model_adapter_error"
            and "private" not in response.text
        )
    with store._engine.connect() as connection:
        task_id = UUID(execute(connection, "SELECT task_id FROM tasks").scalar_one())
    assert store.get(task_id).status.value == "FAILED"
    assert store.get_runtime_failure(task_id)["phase"] == "model"


def test_scan_proxy_stream_role_and_session_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("safeharness.scanning_service.DeepSeekClient", FakeClient)
    monkeypatch.setattr(
        "safeharness.scanning_service.load_credentials", lambda *_: None
    )
    actor = Principal(
        "model", hashlib.sha256(b"test-model").hexdigest(), frozenset({"model_proxy"})
    )
    store = TaskStore(tmp_path / "tasks.sqlite3")
    app = create_app(
        store,
        TicketStore(store.database),
        [*principals(), actor],
        scan_profile="hardened",
        allow_deepseek=True,
        budget_path=tmp_path / "budget.sqlite3",
    )
    with TestClient(app) as client:
        body = {
            "model": "anything",
            "messages": [{"role": "user", "content": "x"}],
            "stream": True,
        }
        for _ in range(12):
            response = client.post(
                "/v1/chat/completions", json=body, headers=headers("model")
            )
            assert (
                response.status_code == 200
                and "proxy-ok" in response.text
                and "[DONE]" in response.text
            )
        assert (
            client.post(
                "/v1/chat/completions", json=body, headers=headers("model")
            ).status_code
            == 429
        )
        assert (
            client.post(
                "/v1/tasks",
                json={"scenario": "ticket_update"},
                headers=headers("model"),
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/scan",
                json={"prompt": "x", "scan_id": str(uuid4())},
                headers=headers("model"),
            ).status_code
            == 403
        )
