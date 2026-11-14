"""Real HTTP + PostgreSQL + independent workers; synthetic approval, no paid model.

Use --duration-seconds 3600 for the one-hour gate. Shorter runs are labelled smoke.
Creates and retains an isolated sh_soak_* schema; never stops the shared database.
"""

import argparse
import ctypes
import hashlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from m4_scan import wait_server
from sqlalchemy import text
from sqlalchemy.engine import make_url

from safeharness.database import engine_for, execute
from safeharness.operations import metrics
from safeharness.service import initialize_auth
from safeharness.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"}


def fingerprint():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/safeharness").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    digest.update((ROOT / "uv.lock").read_bytes())
    return digest.hexdigest()


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def rss(pid):
    """Windows working set, in bytes; null elsewhere (never labelled Python heap)."""
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_rss",
                "rss",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    handle = kernel.OpenProcess(0x1000 | 0x0010, False, pid)
    if not handle:
        return None
    try:
        get_info = ctypes.WinDLL("psapi").GetProcessMemoryInfo
        get_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        return (
            counters.rss
            if get_info(handle, ctypes.byref(counters), counters.cb)
            else None
        )
    finally:
        kernel.CloseHandle(handle)


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main(args):
    if not 5 <= args.duration_seconds <= 86400 or not 0.1 <= args.submit_interval <= 30:
        raise ValueError("Duration 5..86400 seconds, submission interval 0.1..30")
    directory = ROOT / "artifacts/scheduler-soak" / str(uuid4())
    private = directory / "service-private"
    initialize_auth(private)
    base = make_url(
        json.loads((ROOT / "artifacts/m4-private/postgres.json").read_text())[
            "database_url"
        ]
    )
    if base.host not in {"127.0.0.1", "localhost"} or base.database not in {
        "safeharness",
        "safeharness_test",
    }:
        raise ValueError("Only the dedicated local PostgreSQL is allowed")
    schema = "sh_soak_" + uuid4().hex
    with engine_for(base).begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    url = base.update_query_dict({"options": "-csearch_path=" + schema})
    write(
        private / "postgres.json",
        {"database_url": url.render_as_string(hide_password=False)},
    )
    (private / "postgres.json").chmod(0o600)
    store = TaskStore(url)
    store.initialize()
    tokens = json.loads((private / "service-tokens.json").read_text())
    owner = {"Authorization": "Bearer " + tokens["alice"]}
    reviewer = {"Authorization": "Bearer " + tokens["reviewer"]}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    children, workers, snapshots, tasks, finished, approved = [], [], [], {}, {}, set()
    source = fingerprint()
    runner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    started_at = time.time()
    start = time.monotonic()
    last_submit = last_snapshot = -float("inf")
    rejection_count = replay_count = 0
    error = None
    error_code = None

    with (private / "processes.log").open("ab") as log:

        def launch(command, *extra):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "safeharness",
                    command,
                    "--backend",
                    "postgres",
                    "--config-dir",
                    str(private),
                    *extra,
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            children.append(child)
            return child

        api = launch("serve", "--port", str(port), "--max-pending", "20")
        scanner = launch("recover-queue", "--watch", "--interval-seconds", "0.2")
        try:
            wait_server(port, api)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False
            ) as client:
                while True:
                    elapsed = time.monotonic() - start
                    accepting = elapsed < args.duration_seconds
                    # Sequential stages exercise one, two, then four independent consumers.
                    slots = (
                        1
                        if elapsed < args.duration_seconds / 3
                        else 2
                        if elapsed < args.duration_seconds * 2 / 3
                        else 4
                    )
                    while len(workers) < slots:
                        workers.append(
                            launch(
                                "worker",
                                "--worker-id",
                                f"soak-{len(workers) + 1}",
                                "--poll-seconds",
                                "0.1",
                                "--lease-seconds",
                                "10",
                                "--heartbeat-seconds",
                                "1",
                            )
                        )
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError(
                            "A registered service process exited unexpectedly"
                        )
                    if accepting and elapsed - last_submit >= args.submit_interval:
                        index = len(tasks)
                        kind = ("read", "approve", "reject", "cancel")[index % 4]
                        body = {
                            "scenario": "ticket_read"
                            if kind == "read"
                            else "ticket_update_read",
                            "status": "open",
                            "execution_mode": "queued",
                        }
                        headers = {**owner, "Idempotency-Key": f"soak-{index}"}
                        response = client.post("/v1/tasks", headers=headers, json=body)
                        if response.status_code == 429:
                            rejection_count += 1
                        else:
                            response.raise_for_status()
                            task_id = response.json()["task_id"]
                            tasks[task_id] = {
                                "kind": kind,
                                "submitted": time.monotonic(),
                                "expected": {
                                    "read": "SUCCEEDED",
                                    "approve": "SUCCEEDED",
                                    "reject": "FAILED",
                                    "cancel": "CANCELLED",
                                }[kind],
                            }
                            replay = client.post(
                                "/v1/tasks", headers=headers, json=body
                            )
                            replay.raise_for_status()
                            if (
                                replay.json()["task_id"] != task_id
                                or replay.status_code != 200
                            ):
                                raise RuntimeError(
                                    "Idempotency replay changed the logical task"
                                )
                            replay_count += 1
                            if kind == "cancel":
                                client.post(
                                    f"/v1/tasks/{task_id}/cancel", headers=owner
                                ).raise_for_status()
                        last_submit = elapsed
                    for task_id, info in tasks.items():
                        if task_id in finished:
                            continue
                        response = client.get(f"/v1/tasks/{task_id}", headers=owner)
                        response.raise_for_status()
                        state = response.json()["task_status"]
                        if (
                            state == "WAITING_APPROVAL"
                            and task_id not in approved
                            and info["kind"] != "cancel"
                        ):
                            record = client.get(
                                f"/v1/tasks/{task_id}/approval", headers=reviewer
                            )
                            record.raise_for_status()
                            record = record.json()
                            decision = client.post(
                                f"/v1/approvals/{record['approval_id']}/decision",
                                headers=reviewer,
                                json={
                                    "decision": "reject"
                                    if info["kind"] == "reject"
                                    else "approve",
                                    "arguments_sha256": record["arguments_sha256"],
                                },
                            )
                            decision.raise_for_status()
                            approved.add(task_id)
                        elif state in TERMINAL:
                            finished[task_id] = {
                                "status": state,
                                "latency_ms": round(
                                    (time.monotonic() - info["submitted"]) * 1000, 3
                                ),
                            }
                            if state != info["expected"]:
                                raise RuntimeError(
                                    "Task did not reach its expected terminal state"
                                )
                    if elapsed - last_snapshot >= min(60, args.duration_seconds / 3):
                        snapshot = {
                            "elapsed_seconds": round(elapsed, 3),
                            "worker_slots": slots,
                            "accepted": len(tasks),
                            "finished": len(finished),
                            "operations": metrics(store),
                            "working_set_bytes": {
                                str(child.pid): rss(child.pid) for child in children
                            },
                        }
                        snapshots.append(snapshot)
                        write(directory / "progress.json", snapshot)
                        print(
                            json.dumps(
                                {
                                    key: snapshot[key]
                                    for key in (
                                        "elapsed_seconds",
                                        "worker_slots",
                                        "accepted",
                                        "finished",
                                    )
                                }
                            ),
                            flush=True,
                        )
                        last_snapshot = elapsed
                    if not accepting and len(finished) == len(tasks):
                        break
                    if elapsed > args.duration_seconds + 120:
                        raise RuntimeError("Drain deadline exceeded")
                    time.sleep(0.1)
                ready = client.get("/ready")
                ready.raise_for_status()
                ticket = client.get("/v1/ticket", headers=owner)
                ticket.raise_for_status()
                writes = ticket.json()["write_count"]
        except Exception as exc:  # noqa: BLE001 -- export a sanitized failed-run report
            error = type(
                exc
            ).__name__  # Never export HTTP credentials/connection strings.
            error_code = (
                str(exc)
                if str(exc)
                in {
                    "A registered service process exited unexpectedly",
                    "Idempotency replay changed the logical task",
                    "Task did not reach its expected terminal state",
                    "Drain deadline exceeded",
                }
                else "inspect_private_run_log"
            )
            writes = None
        finally:
            for child in reversed(children):
                stop(child)
    elapsed = time.monotonic() - start
    final = metrics(store)
    expected_writes = sum(info["kind"] == "approve" for info in tasks.values())
    with store._engine.connect() as connection:
        receipts = execute(
            connection, "SELECT count(*) FROM mock_receipts"
        ).scalar_one()
        logical_writes = execute(
            connection,
            "SELECT count(DISTINCT operation_id) FROM tool_executions WHERE status='SUCCEEDED'",
        ).scalar_one()
        recovery_count = execute(
            connection, "SELECT coalesce(sum(recovery_count),0) FROM task_queue"
        ).scalar_one()
    lease_losses = []
    for line in (
        (private / "processes.log")
        .read_text(encoding="utf-8", errors="replace")
        .splitlines()
    ):
        try:
            record = json.loads(line)
            if record.get("status") == "lease_lost":
                lease_losses.append(
                    {key: record.get(key) for key in ("task_id", "worker_id", "code")}
                )
        except (ValueError, AttributeError):
            continue
    passed = (
        error is None
        and elapsed >= args.duration_seconds
        and len(finished) == len(tasks) == final["task_total"] == final["queue_total"]
        and writes == receipts == logical_writes == expected_writes
        and final["expired_claims"] == final["unconfirmed_attempts"] == 0
        and not final["alerts"]
        and fingerprint() == source
    )
    latencies = sorted(value["latency_ms"] for value in finished.values())
    report = {
        "stage": "S7",
        "kind": ("one_hour_soak" if elapsed >= 3600 else "incomplete_one_hour_attempt")
        if args.duration_seconds >= 3600
        else "short_smoke",
        "passed": passed,
        "source_fingerprint": source,
        "runner_sha256": runner_sha256,
        "source_unchanged": fingerprint() == source,
        "started_at_unix": started_at,
        "requested_seconds": args.duration_seconds,
        "measured_seconds": round(elapsed, 3),
        "backend": "postgres",
        "retained_schema": schema,
        "paid_api_calls": 0,
        "synthetic_reviewer": True,
        "worker_slots_planned": [1, 2, 4],
        "worker_slots": sorted({s["worker_slots"] for s in snapshots} | {len(workers)}),
        "independent_worker_pids": [p.pid for p in workers],
        "scanner_pid": scanner.pid,
        "workload": {
            "submit_interval_seconds": args.submit_interval,
            "max_pending": 20,
            "lease_seconds": 10,
            "heartbeat_seconds": 1,
            "scanner_interval_seconds": 0.2,
        },
        "accepted": len(tasks),
        "finished": len(finished),
        "idempotency_replays": replay_count,
        "capacity_rejections": rejection_count,
        "business_writes": writes,
        "business_receipts": receipts,
        "distinct_completed_operations": logical_writes,
        "expected_writes": expected_writes,
        "latency_ms": {
            "p50": statistics.median(latencies) if latencies else None,
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            if latencies
            else None,
        },
        "final_operations": final,
        "error_type": error,
        "error_code": error_code,
        "registered_process_exit_codes": {
            str(child.pid): child.returncode for child in children
        },
        "observed_worker_lease_losses": lease_losses,
        "automatic_recovery_count": recovery_count,
        "snapshots": snapshots,
        "outcomes": [
            {
                "task_id": task_id,
                "kind": info["kind"],
                **finished.get(task_id, {"status": "unfinished"}),
            }
            for task_id, info in tasks.items()
        ],
        "limits": [
            "Local deterministic workload; not a paid-model or coding scheduler benchmark",
            "No database/process outage injected in this soak; named fault windows covered separately",
            "Working-set samples are observations, not a leak proof",
            "24-hour operation and backup/restore remain separate gates",
        ],
    }
    path = directory / "report.json"
    write(path, report)
    print(
        json.dumps(
            {
                "passed": passed,
                "kind": report["kind"],
                "measured_seconds": report["measured_seconds"],
                "report": str(path),
            }
        ),
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=3600)
    parser.add_argument("--submit-interval", type=float, default=3)
    raise SystemExit(main(parser.parse_args()))
