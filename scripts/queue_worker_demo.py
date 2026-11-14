"""Real HTTP submission, API shutdown, two independent CLI workers, HTTP verification."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

from durable_submission_demo import server

from safeharness.service import initialize_auth

ROOT = Path(__file__).resolve().parents[1]


def main(args):
    if args.phase == "work" and args.demo_id is None:
        raise SystemExit("work requires --demo-id from prepare")
    if args.phase != "work" and args.demo_id is not None:
        raise SystemExit("Use --demo-id only with work")
    if not 1 <= args.jobs <= 20:
        raise SystemExit("--jobs must be between 1 and 20")
    directory = ROOT / "artifacts/queue-worker-demos" / str(args.demo_id or uuid4())
    private = directory / "service-private"
    if args.phase != "work":
        initialize_auth(private)
    if not (private / "service-tokens.json").is_file():
        raise SystemExit("Demo not found")
    tokens = json.loads((private / "service-tokens.json").read_text())
    owner = {"Authorization": "Bearer " + tokens["alice"]}
    info_path = directory / "task.json"
    if args.phase != "work":
        with server(private) as (client, pid):

            def submit(queued):
                response = client.post(
                    "/v1/tasks",
                    headers=owner,
                    json={
                        "scenario": "ticket_read",
                        "execution_mode": "queued" if queued else "manual",
                    },
                )
                response.raise_for_status()
                return response.json()

            tasks = [submit(True) for _ in range(args.jobs)]
            manual, cancelled = submit(False), submit(True)
            response = client.post(
                f"/v1/tasks/{cancelled['task_id']}/cancel", headers=owner
            )
            response.raise_for_status()
            info = {
                "before": tasks,
                "manual": manual["task_id"],
                "cancelled": cancelled["task_id"],
                "initial_api_pid": pid,
            }
            info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        if args.phase == "prepare":
            print(
                json.dumps(
                    {
                        "demo_id": directory.name,
                        "task_ids": [r["task_id"] for r in tasks],
                        "task_status": "CREATED",
                        "queue_state": "QUEUED",
                        "api_stopped": True,
                    },
                    indent=2,
                )
            )
            return 0
    else:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    children = []
    outcomes = []
    try:
        with (private / "workers.log").open("ab") as log:
            for worker_id in ("demo-a", "demo-b"):
                children.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "safeharness",
                            "worker",
                            "--config-dir",
                            str(private),
                            "--worker-id",
                            worker_id,
                            "--drain",
                        ],
                        cwd=ROOT,
                        stdout=subprocess.PIPE,
                        stderr=log,
                    )
                )
            for child in children:
                stdout, _ = child.communicate(timeout=60)
                if child.returncode:
                    raise RuntimeError("Worker failed; inspect private worker log")
                outcomes.extend(
                    json.loads(line) for line in stdout.decode().splitlines()
                )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
    processed = [r for r in outcomes if r["status"] == "processed"]
    expected = {r["task_id"] for r in info["before"]}
    assert (
        len(processed) == len(expected)
        and {r["task_id"] for r in processed} == expected
    )
    assert all(r["task_status"] == "SUCCEEDED" for r in processed)
    verified = []
    with server(private) as (client, pid):
        for task_id in sorted(expected):
            response = client.get(f"/v1/tasks/{task_id}", headers=owner)
            response.raise_for_status()
            result = response.json()
            assert (
                result["task_status"] == "SUCCEEDED"
                and result["scheduling"]["state"] == "FINISHED"
            )
            events = client.get(f"/v1/tasks/{task_id}/trace", headers=owner)
            events.raise_for_status()
            kinds = [e["kind"] for e in events.json()]
            assert (
                kinds.count("scheduling.claimed")
                == kinds.count("scheduling.finished")
                == 1
            )
            ledger = client.get(f"/v1/tasks/{task_id}/invocations", headers=owner)
            ledger.raise_for_status()
            assert (
                len(ledger.json()["calls"]) == 1
                and ledger.json()["calls"][0]["tool_name"] == "read_ticket"
            )
            verified.append(result)
        for name, status in (("manual", "CREATED"), ("cancelled", "CANCELLED")):
            response = client.get(f"/v1/tasks/{info[name]}", headers=owner)
            response.raise_for_status()
            assert response.json()["task_status"] == status
        ticket = client.get("/v1/ticket", headers=owner)
        ticket.raise_for_status()
        assert ticket.json()["write_count"] == 0
    report = {
        "stage": "S2-independent-worker",
        "passed": True,
        "backend": "sqlite",
        "paid_api_calls": 0,
        "api_stopped_during_worker_execution": True,
        "initial_api_pid": info["initial_api_pid"],
        "restarted_api_pid": pid,
        "worker_pids": [p.pid for p in children],
        "worker_results": outcomes,
        "before": info["before"],
        "after": verified,
        "manual_task_untouched": True,
        "cancelled_task_not_executed": True,
        "business_write_count": 0,
        "automatic_takeover_implemented": False,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "demo_id": directory.name,
                "processed_tasks": len(processed),
                "task_status": "SUCCEEDED",
                "queue_state": "FINISHED",
                "api_stopped_during_execution": True,
                "report": str(path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("all", "prepare", "work"), default="all")
    parser.add_argument("--demo-id", type=UUID)
    parser.add_argument("--jobs", type=int, default=1)
    raise SystemExit(main(parser.parse_args()))
