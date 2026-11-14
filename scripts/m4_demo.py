"""Two actual HTTP demos using a dedicated, disposable SQLite service directory."""

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from m4_scan import wait_server

from safeharness.service import initialize_auth

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["ticket", "coding"])
    args = parser.parse_args()
    directory = ROOT / "artifacts/m4-demos" / str(uuid4())
    private = directory / "service-private"
    initialize_auth(private)
    tokens = json.loads((private / "service-tokens.json").read_text())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (private / "service.log").open("wb") as log:
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
                base_url=f"http://127.0.0.1:{port}", timeout=180, trust_env=False
            ) as client:
                owner = {"Authorization": "Bearer " + tokens["alice"]}
                reviewer = {"Authorization": "Bearer " + tokens["reviewer"]}
                response = client.post(
                    "/v1/tasks",
                    json={
                        "scenario": "ticket_update"
                        if args.scenario == "ticket"
                        else "coding_fix"
                    },
                    headers=owner,
                )
                response.raise_for_status()
                task_id = response.json()["task_id"]
                result = client.post(f"/v1/tasks/{task_id}/run", headers=owner)
                result.raise_for_status()
                waiting = result.json()
                if args.scenario == "ticket":
                    assert waiting["task_status"] == "WAITING_APPROVAL"
                    before = client.get("/v1/ticket", headers=owner).json()
                    approval = client.get(
                        f"/v1/tasks/{task_id}/approval", headers=reviewer
                    ).json()
                    decided = client.post(
                        f"/v1/approvals/{approval['approval_id']}/decision",
                        json={
                            "decision": "approve",
                            "arguments_sha256": approval["arguments_sha256"],
                        },
                        headers=reviewer,
                    )
                    decided.raise_for_status()
                    for _ in range(2):
                        result = client.post(
                            f"/v1/tasks/{task_id}/resume", headers=owner
                        )
                        result.raise_for_status()
                    after = client.get("/v1/ticket", headers=owner).json()
                    assert before["write_count"] == 0 and after["write_count"] == 1
                else:
                    before = after = None
                assert result.json()["task_status"] == "SUCCEEDED"
                trace = client.get(f"/v1/tasks/{task_id}/trace", headers=owner)
                trace.raise_for_status()
                (directory / "trace.json").write_text(
                    json.dumps(trace.json(), indent=2), encoding="utf-8"
                )
                report = {
                    "scenario": args.scenario,
                    "task_id": task_id,
                    "initial_run": waiting,
                    "final": result.json(),
                    "ticket_before": before,
                    "ticket_after": after,
                }
                (directory / "report.json").write_text(
                    json.dumps(report, indent=2), encoding="utf-8"
                )
                print(
                    json.dumps(
                        {
                            "scenario": args.scenario,
                            "task_id": task_id,
                            "task_status": result.json()["task_status"],
                            "report": str(directory / "report.json"),
                        },
                        indent=2,
                    )
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
