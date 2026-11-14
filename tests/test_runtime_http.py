import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_service import headers, principals

from safeharness.provider import ProviderError
from safeharness.runtime import FinalAnswer
from safeharness.service import create_app
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore


def crash(*args, **kwargs):
    raise RuntimeError("private provider or adapter failure")


@pytest.mark.parametrize(
    "stage,code",
    [("tool", "execution_adapter_error"), ("verification", "verification_error")],
)
def test_http_runtime_failure_returns_durable_safe_diagnostics(
    tmp_path, monkeypatch, stage, code
):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    business = TicketStore(store.database)
    if stage == "tool":
        monkeypatch.setattr("safeharness.execution.ToolGateway._dispatch", crash)
    else:
        monkeypatch.setattr(
            "safeharness.service.scripted_coding",
            lambda: lambda _: FinalAnswer("ready"),
        )
        monkeypatch.setattr("safeharness.service.verify_coding", crash)
    with TestClient(create_app(store, business, principals())) as client:
        task_id = client.post(
            "/v1/tasks",
            json={"scenario": "ticket_read" if stage == "tool" else "coding_fix"},
            headers=headers("alice"),
        ).json()["task_id"]
        response = client.post(f"/v1/tasks/{task_id}/run", headers=headers("alice"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["task_status"] == "FAILED"
        assert body["last_runtime_error"]["code"] == code
        assert "private" not in response.text
        assert (
            client.get(f"/v1/tasks/{task_id}", headers=headers("alice")).json() == body
        )
        assert (
            client.post(
                f"/v1/tasks/{task_id}/run", headers=headers("alice")
            ).status_code
            == 409
        )
        trace = client.get(
            f"/v1/tasks/{task_id}/trace", headers=headers("alice")
        ).json()
        assert trace[-1]["kind"] == "runtime.error"
        assert body["last_runtime_error"] == trace[-1]["payload"]
    assert business.ticket() == ("open", 0)


def test_http_storage_outage_does_not_claim_failure_was_saved(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    business = TicketStore(store.database)
    monkeypatch.setattr("safeharness.execution.ToolGateway._dispatch", crash)
    with TestClient(create_app(store, business, principals())) as client:
        task_id = client.post(
            "/v1/tasks", json={"scenario": "ticket_read"}, headers=headers("alice")
        ).json()["task_id"]
        with sqlite3.connect(store.database) as connection:
            connection.execute(
                "CREATE TRIGGER refuse_runtime BEFORE INSERT ON trace_events WHEN NEW.kind='runtime.error' BEGIN SELECT RAISE(ABORT,'test outage'); END"
            )
        response = client.post(f"/v1/tasks/{task_id}/run", headers=headers("alice"))
        assert response.status_code == 503
        assert response.json() == {
            "detail": "runtime_failure_not_persisted",
            "task_id": task_id,
            "recovery_action": "inspect_task_before_retry",
        }
        assert "private" not in response.text
        assert (
            client.get(f"/v1/tasks/{task_id}", headers=headers("alice")).json()[
                "task_status"
            ]
            == "RUNNING"
        )


def test_provider_setup_error_is_explicit_and_leaves_unstarted_task_retryable(
    tmp_path, monkeypatch
):
    def missing(*args):
        raise ProviderError("credentials_missing")

    monkeypatch.setattr("safeharness.service.load_credentials", missing)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    with TestClient(
        create_app(
            store,
            TicketStore(store.database),
            principals(),
            allow_deepseek=True,
            budget_path=tmp_path / "budget.sqlite3",
        )
    ) as client:
        task_id = client.post(
            "/v1/tasks",
            json={"scenario": "ticket_read", "provider": "deepseek"},
            headers=headers("alice"),
        ).json()["task_id"]
        response = client.post(f"/v1/tasks/{task_id}/run", headers=headers("alice"))
        assert (
            response.status_code == 503
            and response.json()["detail"] == "credentials_missing"
        )
        assert (
            client.get(f"/v1/tasks/{task_id}", headers=headers("alice")).json()[
                "task_status"
            ]
            == "CREATED"
        )
