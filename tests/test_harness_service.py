import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_service import headers, principals

from safeharness.service import create_app
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore


def test_http_paused_task_continues_after_app_restart_without_repeating_tool(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    business = TicketStore(store.database)
    with TestClient(create_app(store, business, principals())) as client:
        task_id = client.post(
            "/v1/tasks", json={"scenario": "ticket_read"}, headers=headers("alice")
        ).json()["task_id"]
        paused = client.post(
            f"/v1/tasks/{task_id}/run?pause_after=1", headers=headers("alice")
        )
        assert paused.status_code == 200 and paused.json()["task_status"] == "RUNNING"
        checkpoint = client.get(
            f"/v1/tasks/{task_id}/checkpoints", headers=headers("alice")
        )
        assert checkpoint.status_code == 200 and checkpoint.json()["state"] == "PAUSED"
        assert "content" not in checkpoint.text
        assert (
            client.get(
                f"/v1/tasks/{task_id}/checkpoints", headers=headers("bob")
            ).status_code
            == 404
        )
        assert client.get(f"/v1/tasks/{task_id}/checkpoints").status_code == 401
        assert (
            client.post(
                f"/v1/tasks/{task_id}/continue", json={}, headers=headers("bob")
            ).status_code
            == 404
        )
    with TestClient(
        create_app(TaskStore(store.database), business, principals())
    ) as restarted:
        done = restarted.post(
            f"/v1/tasks/{task_id}/continue", json={}, headers=headers("alice")
        )
        assert done.status_code == 200 and done.json()["task_status"] == "SUCCEEDED"
        assert (
            restarted.post(
                f"/v1/tasks/{task_id}/continue", json={}, headers=headers("alice")
            ).status_code
            == 409
        )
        invocations = restarted.get(
            f"/v1/tasks/{task_id}/invocations", headers=headers("alice")
        ).json()
        assert len(invocations["calls"]) == 1 and len(invocations["attempts"]) == 1
        state = restarted.get(
            f"/v1/tasks/{task_id}/checkpoints", headers=headers("alice")
        ).json()
        assert state["generation"] == 2 and state["state"] == "FINISHED"
        assert '"content"' not in json.dumps(state)


def test_agent_continue_cannot_bypass_business_approval(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    business = TicketStore(store.database)
    with TestClient(create_app(store, business, principals())) as client:
        task_id = client.post(
            "/v1/tasks", json={"scenario": "ticket_update"}, headers=headers("alice")
        ).json()["task_id"]
        assert (
            client.post(
                f"/v1/tasks/{task_id}/run?pause_after=1", headers=headers("alice")
            ).json()["task_status"]
            == "WAITING_APPROVAL"
        )
        assert (
            client.post(
                f"/v1/tasks/{task_id}/continue", json={}, headers=headers("alice")
            ).status_code
            == 409
        )
        assert business.ticket() == ("open", 0)


def test_changed_scope_cannot_restore_old_model_authority(tmp_path):
    from dataclasses import replace

    store = TaskStore(tmp_path / "tasks.sqlite3")
    business = TicketStore(store.database)
    with TestClient(create_app(store, business, principals())) as client:
        task_id = client.post(
            "/v1/tasks", json={"scenario": "ticket_read"}, headers=headers("alice")
        ).json()["task_id"]
        client.post(f"/v1/tasks/{task_id}/run?pause_after=1", headers=headers("alice"))
    actors = [
        replace(p, readable_tickets=frozenset()) if p.subject == "alice" else p
        for p in principals()
    ]
    with TestClient(create_app(store, business, actors)) as client:
        result = client.post(
            f"/v1/tasks/{task_id}/continue", json={}, headers=headers("alice")
        )
        assert (
            result.status_code == 409
            and result.json()["detail"] == "checkpoint_configuration_changed"
        )


@pytest.mark.parametrize("endpoint", ["continue", "resume"])
def test_http_approved_agent_reads_updated_state_after_restart(tmp_path, endpoint):
    store = TaskStore(tmp_path / "control.sqlite3")
    business = TicketStore(store.database)
    with TestClient(create_app(store, business, principals())) as client:
        task_id = client.post(
            "/v1/tasks",
            json={"scenario": "ticket_update_read"},
            headers=headers("alice"),
        ).json()["task_id"]
        assert (
            client.post(f"/v1/tasks/{task_id}/run", headers=headers("alice")).json()[
                "task_status"
            ]
            == "WAITING_APPROVAL"
        )
        request = client.get(
            f"/v1/tasks/{task_id}/approval", headers=headers("reviewer")
        ).json()
        response = client.post(
            f"/v1/approvals/{request['approval_id']}/decision",
            json={
                "decision": "approve",
                "arguments_sha256": request["arguments_sha256"],
            },
            headers=headers("reviewer"),
        )
        assert response.status_code == 200 and business.ticket() == ("open", 0)
    with TestClient(
        create_app(TaskStore(store.database), business, principals())
    ) as client:
        response = client.post(
            f"/v1/tasks/{task_id}/{endpoint}",
            json={} if endpoint == "continue" else None,
            headers=headers("alice"),
        )
        assert (
            response.status_code == 200
            and response.json()["task_status"] == "SUCCEEDED"
        )
        ledger = client.get(
            f"/v1/tasks/{task_id}/invocations", headers=headers("alice")
        ).json()
        assert [c["tool_name"] for c in ledger["calls"]] == [
            "update_ticket_status",
            "read_ticket",
        ]
        assert all(c["status"] == "SUCCEEDED" for c in ledger["calls"])
        assert business.ticket() == ("resolved", 1)


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
    reason="Enable Docker checkpoint recovery",
)
def test_coding_http_checkpoint_keeps_patch_across_restart(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    business = TicketStore(store.database)
    with TestClient(create_app(store, business, principals())) as client:
        task_id = client.post(
            "/v1/tasks", json={"scenario": "coding_fix"}, headers=headers("alice")
        ).json()["task_id"]
        assert (
            client.post(
                f"/v1/tasks/{task_id}/run?pause_after=3", headers=headers("alice")
            ).json()["task_status"]
            == "RUNNING"
        )
    with TestClient(
        create_app(TaskStore(store.database), business, principals())
    ) as client:
        done = client.post(
            f"/v1/tasks/{task_id}/continue", json={}, headers=headers("alice")
        )
        assert done.status_code == 200 and done.json()["task_status"] == "SUCCEEDED"
        calls = client.get(
            f"/v1/tasks/{task_id}/invocations", headers=headers("alice")
        ).json()["calls"]
        assert sum(c["tool_name"] == "write_file" for c in calls) == 1
        assert any(c["tool_name"] == "_verify_coding_contract" for c in calls)
