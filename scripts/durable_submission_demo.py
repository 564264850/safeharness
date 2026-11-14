"""S1: submit over real HTTP, restart the API, replay the same queued task. No API cost."""

import argparse
import json
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from m4_scan import wait_server

from safeharness.service import initialize_auth
from safeharness.task_store import TaskStore
from safeharness.trace import TraceLog

ROOT = Path(__file__).resolve().parents[1]
BODY = {"scenario": "ticket_read", "execution_mode": "queued"}
KEY = "s1-restart-demo"


@contextmanager
def server(private):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (private / "service.log").open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "safeharness",
                "serve",
                "--config-dir",
                str(private),
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            wait_server(port, process)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=20, trust_env=False
            ) as client:
                yield client, process.pid
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main(args):
    if args.phase == "check" and args.demo_id is None:
        raise SystemExit("--phase check requires the --demo-id printed by prepare")
    if args.phase != "check" and args.demo_id is not None:
        raise SystemExit("Use --demo-id only with --phase check")
    directory = ROOT / "artifacts/scheduler-demos" / str(args.demo_id or uuid4())
    private = directory / "service-private"
    if args.phase != "check":
        initialize_auth(private)
    info_path = directory / "task.json"
    if not (private / "service-tokens.json").exists():
        raise SystemExit("Demo not found; run --phase prepare first")
    tokens = json.loads((private / "service-tokens.json").read_text())
    owner = {"Authorization": "Bearer " + tokens["alice"]}
    submit_headers = {**owner, "Idempotency-Key": KEY}
    info = {}
    if args.phase != "check":
        with server(private) as (client, pid):
            response = client.post("/v1/tasks", json=BODY, headers=submit_headers)
            response.raise_for_status()
            initial = response.json()
            assert response.status_code == 201
            assert (
                initial["task_status"] == "CREATED"
                and initial["scheduling"]["state"] == "QUEUED"
            )
            task_id = initial["task_id"]
            blocked = client.post(f"/v1/tasks/{task_id}/run", headers=owner)
            assert (
                blocked.status_code == 409
                and blocked.json()["detail"] == "queued_task_requires_worker"
            )
            info = {
                "initial": initial,
                "initial_api_pid": pid,
                "manual_run_blocked": True,
            }
            info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        if args.phase == "prepare":
            print(
                json.dumps(
                    {
                        "demo_id": directory.name,
                        "task_id": task_id,
                        "task_status": "CREATED",
                        "queue_state": "QUEUED",
                        "api_stopped": True,
                        "paid_api_calls": 0,
                        "next": f"python scripts/durable_submission_demo.py --phase check --demo-id {directory.name}",
                    },
                    indent=2,
                )
            )
            return 0
    else:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    task_id = info["initial"]["task_id"]
    with server(private) as (client, pid):
        after = client.get(f"/v1/tasks/{task_id}", headers=owner)
        after.raise_for_status()
        assert after.json() == info["initial"]
        replay = client.post("/v1/tasks", json=BODY, headers=submit_headers)
        replay.raise_for_status()
        assert (
            replay.status_code == 200
            and replay.headers["Idempotency-Replayed"] == "true"
        )
        assert replay.json() == info["initial"]
        ticket = client.get("/v1/ticket", headers=owner)
        ticket.raise_for_status()
        assert ticket.json()["write_count"] == 0
        events = TraceLog(
            TaskStore(private / "tasks.sqlite3").database, UUID(task_id)
        ).read()
        assert [e["kind"] for e in events].count("scheduling.enqueued") == 1
        report = {
            "stage": "S1-durable-submission",
            "passed": True,
            "paid_api_calls": 0,
            "backend": "sqlite",
            "task_id": task_id,
            "before_restart": info["initial"],
            "after_restart": after.json(),
            "initial_api_pid": info["initial_api_pid"],
            "restarted_api_pid": pid,
            "idempotency_replay_status": replay.status_code,
            "manual_run_blocked": info["manual_run_blocked"],
            "enqueue_events": 1,
            "business_write_count": 0,
            "worker_implemented": False,
            "events": events,
        }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "demo_id": directory.name,
                "task_id": task_id,
                "task_status": "CREATED",
                "queue_state": "QUEUED",
                "restart_verified": True,
                "paid_api_calls": 0,
                "report": str(path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("all", "prepare", "check"), default="all")
    parser.add_argument("--demo-id", type=UUID)
    raise SystemExit(main(parser.parse_args()))
