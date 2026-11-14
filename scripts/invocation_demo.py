"""Local HTTP replay + real process loss + receipt recovery, without model API calls."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from safeharness.service import Principal, create_app
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore

ROOT = Path(__file__).resolve().parents[1]


def main():
    directory = ROOT / "artifacts/invocation-demos" / str(uuid4())
    directory.mkdir(parents=True)
    store = TaskStore(directory / "control.sqlite3")
    business = TicketStore(store.database)
    principals = [
        Principal(
            name,
            hashlib.sha256(("synthetic-demo-" + name).encode()).hexdigest(),
            frozenset({role}),
            writable_tickets=frozenset({"IT-001"}),
        )
        for name, role in [("owner", "requester"), ("reviewer", "approver")]
    ]
    owner = {
        "Authorization": "Bearer synthetic-demo-owner",
        "Idempotency-Key": "learn-invocations-1",
    }
    reviewer = {"Authorization": "Bearer synthetic-demo-reviewer"}
    with TestClient(create_app(store, business, principals)) as client:
        responses = [
            client.post(
                "/v1/tasks", json={"scenario": "ticket_update_read"}, headers=owner
            )
            for _ in range(2)
        ]
        conflict = client.post(
            "/v1/tasks", json={"scenario": "ticket_read"}, headers=owner
        )
        assert [r.status_code for r in responses] == [
            201,
            200,
        ] and conflict.status_code == 409
        assert responses[0].json()["task_id"] == responses[1].json()["task_id"]
        task_id = responses[0].json()["task_id"]
        run = client.post(f"/v1/tasks/{task_id}/run", headers=owner)
        assert run.json()["task_status"] == "WAITING_APPROVAL"
        request = client.get(f"/v1/tasks/{task_id}/approval", headers=reviewer).json()
        assert (
            client.post(
                f"/v1/approvals/{request['approval_id']}/decision",
                json={
                    "decision": "approve",
                    "arguments_sha256": request["arguments_sha256"],
                },
                headers=reviewer,
            ).status_code
            == 200
        )
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "safeharness",
            "--db",
            str(store.database),
            "resume",
            task_id,
            "--simulate-crash-after-write",
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 77 and business.ticket() == ("resolved", 1)
    with TestClient(
        create_app(TaskStore(store.database), business, principals)
    ) as client:
        resumed = client.post(f"/v1/tasks/{task_id}/resume", headers=owner)
        assert resumed.json()["task_status"] == "SUCCEEDED"
        snapshot = client.get(f"/v1/tasks/{task_id}/invocations", headers=owner).json()
        trace = client.get(f"/v1/tasks/{task_id}/trace", headers=owner).json()
    assert (
        len(snapshot["runs"]) == 3
        and len(snapshot["calls"]) == 2
        and len(snapshot["attempts"]) == 4
    )
    assert snapshot["calls"][0]["operation_id"] == request["approval_id"]
    assert business.ticket() == ("resolved", 1)
    report = {
        "passed": True,
        "task_id": task_id,
        "task_status": "SUCCEEDED",
        "submission_statuses": [201, 200, 409],
        "real_process_exit": crashed.returncode,
        "business_write_count": 1,
        "paid_provider_calls": 0,
        "invocations": snapshot,
        "trace": trace,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "task_id": task_id,
                "runs": 3,
                "calls": 2,
                "attempts": 4,
                "business_write_count": 1,
                "report": str(path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
