"""Opt-in paid acceptance against the real DeepSeek API and synthetic local ticket."""

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from m4_scan import wait_server

from safeharness.budget import BudgetLedger
from safeharness.invocations import InvocationLedger
from safeharness.service import initialize_auth
from safeharness.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials", type=Path, default=ROOT.parent / "pico-api.txt"
    )
    parser.add_argument("--run-paid", action="store_true", required=True)
    args = parser.parse_args()
    batch = uuid4().hex
    directory = ROOT / "artifacts" / "phase2-acceptance" / batch
    private = directory / "service-private"
    initialize_auth(private)
    tokens = json.loads((private / "service-tokens.json").read_text())
    budget_path = directory / "provider-budget.sqlite3"
    budget_id = "phase2-" + batch
    settings = [
        "--config-dir",
        str(private),
        "--allow-deepseek",
        "--credentials",
        str(args.credentials.resolve()),
        "--budget-db",
        str(budget_path),
        "--budget-id",
        budget_id,
        "--budget-cny",
        "5.00",
        "--max-provider-calls",
        "40",
    ]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    processes, logs = [], []
    report = {
        "passed": False,
        "provider": "deepseek",
        "real_api": True,
        "business": "synthetic-local-ticket",
        "manual_run_calls": 0,
        "manual_resume_calls": 0,
        "worker_restarted_while_waiting": False,
    }

    def launch(command, extra=()):
        log = (private / f"{command}-{len(logs)}.log").open("wb")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, "-m", "safeharness", command, *settings, *extra],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        processes.append(process)
        return process

    try:
        api = launch("serve", ["--port", str(port)])
        wait_server(port, api)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False
        ) as client:
            owner = {"Authorization": "Bearer " + tokens["alice"]}
            reviewer = {"Authorization": "Bearer " + tokens["reviewer"]}

            def get(route):
                result = client.get(route, headers=owner)
                result.raise_for_status()
                return result.json()

            def wait_task(expected):
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    result = get(f"/v1/tasks/{task_id}")
                    if result["task_status"] == expected:
                        return result
                    if result["task_status"] in {"FAILED", "NEEDS_REVIEW", "CANCELLED"}:
                        report["unexpected_task_state"] = result
                        raise RuntimeError("task_did_not_reach_expected_state")
                    time.sleep(0.2)
                raise RuntimeError("acceptance_wait_timeout")

            created = client.post(
                "/v1/tasks",
                headers=owner,
                json={
                    "scenario": "ticket_update_read",
                    "provider": "deepseek",
                    "execution_mode": "queued",
                },
            )
            created.raise_for_status()
            task_id = created.json()["task_id"]
            report["task_id"] = task_id
            first_worker = launch("worker", ["--poll-seconds", "0.1"])
            report["waiting"] = wait_task("WAITING_APPROVAL")
            report["ticket_before_approval"] = get("/v1/ticket")
            assert report["ticket_before_approval"]["write_count"] == 0
            stop(first_worker)
            approval = client.get(f"/v1/tasks/{task_id}/approval", headers=reviewer)
            approval.raise_for_status()
            approval = approval.json()
            assert approval["approval_status"] == "PENDING"
            assert approval["arguments"] == {
                "ticket_id": "IT-001",
                "status": "resolved",
            }
            decision = {
                "decision": "approve",
                "arguments_sha256": approval["arguments_sha256"],
            }
            self_decision = client.post(
                f"/v1/approvals/{approval['approval_id']}/decision",
                headers=owner,
                json=decision,
            )
            assert self_decision.status_code == 403
            report["self_approval_status"] = self_decision.status_code
            decided = client.post(
                f"/v1/approvals/{approval['approval_id']}/decision",
                headers=reviewer,
                json=decision,
            )
            decided.raise_for_status()
            report["approval"] = approval
            report["decision"] = decided.json()
            launch("worker", ["--poll-seconds", "0.1"])
            report["worker_restarted_while_waiting"] = True
            report["final"] = wait_task("SUCCEEDED")
            report["ticket_after"] = get("/v1/ticket")
            assert report["ticket_after"]["write_count"] == 1
            replay = client.post(
                f"/v1/approvals/{approval['approval_id']}/decision",
                headers=reviewer,
                json=decision,
            )
            assert replay.status_code == 409
            assert get("/v1/ticket")["write_count"] == 1
            report["duplicate_approval_status"] = replay.status_code
            trace = get(f"/v1/tasks/{task_id}/trace")
            (directory / "trace.json").write_text(
                json.dumps(trace, indent=2), encoding="utf-8"
            )
            ledger = InvocationLedger(
                private / "tasks.sqlite3", UUID(task_id)
            ).snapshot()
            report["calls"] = ledger["calls"]
            report["attempts"] = ledger["attempts"]
            report["business_receipt"] = TaskStore(
                private / "tasks.sqlite3"
            ).get_execution(UUID(task_id))
            assert report["business_receipt"]["status"] == "SUCCEEDED"
            assert report["business_receipt"]["operation_id"] == approval["approval_id"]
            assert report["final"]["scheduling"]["generation"] == 2
            assert (
                report["waiting"]["scheduling"]["claimed_by"]
                != report["final"]["scheduling"]["claimed_by"]
            )
            assert any(
                c["kind"] == "tool"
                and c["tool_name"] == "read_ticket"
                and c["status"] == "SUCCEEDED"
                for c in ledger["calls"]
            )
            report["usage"] = BudgetLedger(
                budget_path, budget_id=budget_id, limit_cny="5.00", max_calls=40
            ).usage()
            assert report["usage"]["unknown_usage_calls"] == 0
            report["passed"] = True
    except Exception as error:  # noqa: BLE001 - do not expose credentials or raw transport failures
        report["error_type"] = type(error).__name__
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        fingerprint = hashlib.sha256()
        for source in sorted((ROOT / "src").rglob("*.py")) + [Path(__file__)]:
            fingerprint.update(source.relative_to(ROOT).as_posix().encode())
            fingerprint.update(source.read_bytes())
        report["source_sha256"] = fingerprint.hexdigest()
        report["source_hash_scope"] = (
            "sorted src/**/*.py plus this acceptance script, relative paths and raw bytes"
        )
        (directory / "report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "report": str(directory / "report.json"),
                "usage": report.get("usage"),
                "error_type": report.get("error_type"),
            },
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
