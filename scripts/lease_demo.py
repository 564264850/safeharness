"""Observe real heartbeat renewal, local failure and worker process death. No paid API."""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import UUID, uuid4

from safeharness import heartbeat
from safeharness.invocations import InvocationLedger
from safeharness.scheduling import CURRENT_CLAIM, claim_next
from safeharness.service import initialize_auth, load_principals
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.worker import QueueWorker

ROOT = Path(__file__).resolve().parents[1]
REQUEST = {"scenario": "ticket_read", "execution_mode": "queued"}


def components(directory):
    private = directory / "service-private"
    store = TaskStore(private / "tasks.sqlite3")
    store.initialize()
    worker = QueueWorker(
        store,
        TicketStore(store.database),
        load_principals(private / "service-auth.json"),
        lease_seconds=1.5,
        heartbeat_seconds=0.1,
    )
    return store, worker


def wait_until(check, timeout=8):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise RuntimeError("Demo observation timed out")
        time.sleep(0.05)


def emit(phase, store, task_id):
    result = {"phase": phase, **store.task_with_scheduling(task_id)[1]}
    print(json.dumps(result), flush=True)
    return result


def blocked_call(directory, fail_heartbeat=False):
    store, worker = components(directory)
    task, _ = store.submit("alice", REQUEST)
    entered, release, fail = Event(), Event(), Event()
    original_ticket = worker.service.business.ticket
    original_renew = heartbeat.renew_claim
    calls = [0]
    claims = []

    def ticket():
        calls[0] += 1
        if calls[0] == 2:
            claims.append(CURRENT_CLAIM.get())
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Demo gate timed out")
        return original_ticket()

    def renew(*args, **kwargs):
        if fail.is_set():
            raise RuntimeError("synthetic heartbeat transport failure")
        return original_renew(*args, **kwargs)

    with (
        patch.object(worker.service.business, "ticket", side_effect=ticket),
        patch.object(heartbeat, "renew_claim", side_effect=renew),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(worker.run_once)
        try:
            if not entered.wait(6):
                raise RuntimeError("Worker did not reach the tool")
            before = emit("tool_blocked", store, task.task_id)
            if fail_heartbeat:
                fail.set()
                if not claims[0].fenced.wait(5):
                    raise RuntimeError("Worker was not fenced")
            else:
                time.sleep(2.2)
                during = emit("active_after_original_lease", store, task.task_id)
                assert (
                    during["lease_status"] == "ACTIVE"
                    and during["heartbeat_count"] > before["heartbeat_count"]
                )
        finally:
            release.set()
        result = future.result(timeout=10)
    snapshot = InvocationLedger(store.database, task.task_id).snapshot()
    if fail_heartbeat:
        assert result["status"] == "lease_lost" and result["task_status"] == "RUNNING"
        assert (
            snapshot["attempts"][0]["status"] == "INTENT"
            and snapshot["artifacts"] == []
        )
        wait_until(lambda: store.scheduling(task.task_id)["lease_status"] == "EXPIRED")
        final = emit("heartbeat_failed_and_lease_expired", store, task.task_id)
        assert claim_next(store, "contender") is None
    else:
        assert result["task_status"] == "SUCCEEDED"
        final = emit("task_completed", store, task.task_id)
    return {
        "scenario": "heartbeat_failure" if fail_heartbeat else "long_tool",
        "task_id": str(task.task_id),
        "passed": True,
        "before": before,
        "final": final,
        "worker": result,
        "late_result_rejected": fail_heartbeat,
    }


def child_hold(directory):
    _store, worker = components(directory)
    original = worker.service.business.ticket
    calls = [0]

    def ticket():
        calls[0] += 1
        if calls[0] == 2:
            (directory / "service-private/tool-started").touch()
            time.sleep(30)
        return original()

    with patch.object(worker.service.business, "ticket", side_effect=ticket):
        worker.run_once()


def process_death(directory):
    store, _ = components(directory)
    task, _ = store.submit("alice", REQUEST)
    with (directory / "service-private/child.log").open("wb") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child-demo-id",
                directory.name,
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            wait_until(
                lambda: (
                    (directory / "service-private/tool-started").exists()
                    and store.scheduling(task.task_id)["heartbeat_count"] >= 3
                )
            )
            before = emit("worker_alive", store, task.task_id)
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    wait_until(lambda: store.scheduling(task.task_id)["lease_status"] == "EXPIRED")
    final = emit("worker_dead_lease_expired", store, task.task_id)
    assert final["heartbeat_count"] >= before["heartbeat_count"]
    count = final["heartbeat_count"]
    time.sleep(0.2)
    assert store.scheduling(task.task_id)["heartbeat_count"] == count
    assert final["state"] == "CLAIMED" and claim_next(store, "contender") is None
    return {
        "scenario": "process_death",
        "task_id": str(task.task_id),
        "passed": True,
        "worker_pid": child.pid,
        "before": before,
        "final": final,
        "automatically_reclaimed": False,
    }


def main(args):
    if args.child_demo_id:
        child_hold(ROOT / "artifacts/lease-demos" / str(args.child_demo_id))
        return 0
    directory = ROOT / "artifacts/lease-demos" / str(uuid4())
    initialize_auth(directory / "service-private")
    scenarios = []
    if args.scenario in {"all", "long-tool"}:
        scenarios.append(blocked_call(directory))
    if args.scenario in {"all", "heartbeat-failure"}:
        scenarios.append(blocked_call(directory, True))
    if args.scenario in {"all", "process-death"}:
        scenarios.append(process_death(directory))
    report = {
        "stage": "S3-leases-heartbeats",
        "passed": all(s["passed"] for s in scenarios),
        "paid_api_calls": 0,
        "backend": "sqlite",
        "lease_seconds": 1.5,
        "heartbeat_seconds": 0.1,
        "scenarios": scenarios,
        "automatic_recovery_implemented": False,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("all", "long-tool", "heartbeat-failure", "process-death"),
        default="all",
    )
    parser.add_argument("--child-demo-id", type=UUID, help=argparse.SUPPRESS)
    raise SystemExit(main(parser.parse_args()))
