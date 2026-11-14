"""HTTP approval + fresh CLI worker, no manual resume and no paid API calls.

The reviewer is a synthetic test actor. This script must not be used to approve
real production tasks. All state belongs to a new isolated demo directory.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

from durable_submission_demo import server

from safeharness.service import initialize_auth

ROOT = Path(__file__).resolve().parents[1]


def run_worker(private):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "safeharness",
            "worker",
            "--config-dir",
            str(private),
            "--once",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=60,
    )
    (private / "worker.log").write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError("Worker failed; inspect private log")
    return json.loads(result.stdout)


def main(args):
    if (args.phase == "finish") != (args.demo_id is not None):
        raise ValueError("Only --phase finish requires --demo-id")
    directory = ROOT / "artifacts/queued-approval-demos" / str(args.demo_id or uuid4())
    private = directory / "service-private"
    if args.phase != "finish":
        initialize_auth(private)
    tokens = json.loads((private / "service-tokens.json").read_text())
    owner = {"Authorization": "Bearer " + tokens["alice"]}
    reviewer = {"Authorization": "Bearer " + tokens["reviewer"]}
    info_path = directory / "task.json"
    if args.phase != "finish":
        with server(private) as (client, initial_pid):
            response = client.post(
                "/v1/tasks",
                headers=owner,
                json={"scenario": "ticket_update_read", "execution_mode": "queued"},
            )
            response.raise_for_status()
            task_id = response.json()["task_id"]
        waiting = run_worker(private)
        assert waiting["status"] == "waiting_approval"
        assert waiting["scheduling"]["state"] == "WAITING"
        assert run_worker(private)["status"] == "idle"
        info = {"task_id": task_id, "initial_api_pid": initial_pid, "waiting": waiting}
        info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        if args.phase == "prepare":
            print(
                json.dumps(
                    {
                        "demo_id": directory.name,
                        "task_id": task_id,
                        "task_status": "WAITING_APPROVAL",
                        "queue_state": "WAITING",
                        "api_and_worker_stopped": True,
                        "finish_command": f"python scripts/queued_approval_demo.py --phase finish --demo-id {directory.name}",
                    },
                    indent=2,
                )
            )
            return 0
    else:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        task_id = info["task_id"]
    with server(private) as (client, restarted_pid):
        record = client.get(f"/v1/tasks/{task_id}/approval", headers=reviewer)
        record.raise_for_status()
        record = record.json()
        assert record["approval_status"] == "PENDING"
        before = client.get("/v1/ticket", headers=owner).json()
        response = client.post(
            f"/v1/approvals/{record['approval_id']}/decision",
            headers=reviewer,
            json={
                "decision": "approve",
                "arguments_sha256": record["arguments_sha256"],
            },
        )
        response.raise_for_status()
        ready = client.get(f"/v1/tasks/{task_id}", headers=owner).json()
        assert ready["scheduling"]["state"] == "READY"
        completed = run_worker(private)
        assert completed["task_status"] == "SUCCEEDED"
        assert run_worker(private)["status"] == "idle"
        after = client.get("/v1/ticket", headers=owner).json()
        assert (
            after["status"] == "resolved"
            and after["write_count"] == before["write_count"] + 1
        )
        invocations = client.get(f"/v1/tasks/{task_id}/invocations", headers=owner)
        invocations.raise_for_status()
        calls = invocations.json()["calls"]
        assert [r["tool_name"] for r in calls] == [
            "update_ticket_status",
            "read_ticket",
        ]
    report = {
        "stage": "S6-queued-approval",
        "passed": True,
        "synthetic_reviewer": True,
        "paid_api_calls": 0,
        "task_id": task_id,
        "initial_api_pid": info["initial_api_pid"],
        "restarted_api_pid": restarted_pid,
        "before": info["waiting"],
        "after_approval": ready,
        "after": completed,
        "business_before": before,
        "business_after": after,
        "manual_resume_calls": 0,
        "calls": calls,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "task_id": task_id,
                "task_status": "SUCCEEDED",
                "business_writes_added": 1,
                "manual_resume_calls": 0,
                "report": str(path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("all", "prepare", "finish"), default="all")
    parser.add_argument("--demo-id", type=UUID)
    raise SystemExit(main(parser.parse_args()))
