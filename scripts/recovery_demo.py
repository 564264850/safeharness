"""Kill an actual worker; scanner + standby resume a safe checkpoint or stop for review."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from safeharness import worker as worker_module
from safeharness.checkpoints import CheckpointSession
from safeharness.heartbeat import LeaseHeartbeat
from safeharness.invocations import InvocationLedger
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


def wait_until(check, children=(), timeout=15):
    until = time.monotonic() + timeout
    while not check():
        if time.monotonic() > until or any(p.poll() is not None for p in children):
            raise RuntimeError(
                "Demo did not reach its target; inspect the private logs"
            )
        time.sleep(0.05)


def child(directory, scenario):
    _, worker = components(directory)
    gate = directory / "service-private/window-reached"
    if scenario in {"safe", "stale"}:
        original = CheckpointSession.save
        hearts = []

        def heartbeat(*args, **kwargs):
            heart = LeaseHeartbeat(*args, **kwargs)
            hearts.append(heart)
            return heart

        def save(session, task, phase, *args, **kwargs):
            original(session, task, phase, *args, **kwargs)
            if phase == "after_tool":
                if scenario == "stale":
                    hearts[0].stopped.set()
                    hearts[0].thread.join(timeout=5)
                gate.touch()
                if scenario == "stale":
                    wait_until(
                        lambda: (gate.parent / "release-old").exists(), timeout=45
                    )
                else:
                    time.sleep(30)

        with (
            patch.object(CheckpointSession, "save", save),
            patch.object(worker_module, "LeaseHeartbeat", side_effect=heartbeat),
        ):
            result = worker.run_once()
            if scenario == "stale":
                (gate.parent / "old-result.json").write_text(
                    json.dumps(result), encoding="utf-8"
                )
    else:
        original = worker.service.business.ticket
        calls = [0]

        def ticket():
            calls[0] += 1
            if calls[0] == 2:
                gate.touch()
                time.sleep(30)
            return original()

        with patch.object(worker.service.business, "ticket", side_effect=ticket):
            worker.run_once()


def snapshot(store, task_id):
    task, queue = store.task_with_scheduling(task_id)
    return {
        "task_id": str(task_id),
        "task_status": task.status.value,
        "scheduling": queue,
    }


def scenario(directory, name, demo_id):
    private = directory / "service-private"
    initialize_auth(private)
    store, _ = components(directory)
    task, _ = store.submit("alice", REQUEST)
    processes = []
    with (private / "processes.log").open("wb") as log:
        try:
            victim = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child-demo-id",
                    str(demo_id),
                    "--child-scenario",
                    name,
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            processes.append(victim)
            wait_until(lambda: (private / "window-reached").exists(), [victim])
            before = snapshot(store, task.task_id)
            print(
                json.dumps({"phase": "before_worker_exit", "scenario": name, **before}),
                flush=True,
            )
            deadline = before["scheduling"]["execution_deadline_at"]
            calls_before = InvocationLedger(store.database, task.task_id).snapshot()
            if name != "stale":
                victim.kill()
                victim.wait(timeout=5)
            wait_until(
                lambda: store.scheduling(task.task_id)["lease_status"] == "EXPIRED"
            )
            print(
                json.dumps(
                    {
                        "phase": "lease_expired",
                        "scenario": name,
                        **snapshot(store, task.task_id),
                    }
                ),
                flush=True,
            )
            commands = [
                ["recover-queue", "--watch", "--interval-seconds", "0.1"],
                ["worker", "--worker-id", "standby", "--poll-seconds", "0.1"],
            ]
            live = []
            for args in commands:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "safeharness",
                        *args,
                        "--config-dir",
                        str(private),
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                )
                live.append(process)
                processes.append(process)
            expected = "NEEDS_REVIEW" if name == "unknown" else "SUCCEEDED"
            wait_until(lambda: store.get(task.task_id).status.value == expected, live)
            after = snapshot(store, task.task_id)
            calls_after = InvocationLedger(store.database, task.task_id).snapshot()
            assert (
                after["scheduling"]["generation"] == 2
                and after["scheduling"]["recovery_count"] == 1
            )
            assert after["scheduling"]["execution_deadline_at"] == deadline
            assert len(calls_before["calls"]) == len(calls_after["calls"]) == 1
            assert (
                calls_before["calls"][0]["call_id"]
                == calls_after["calls"][0]["call_id"]
            )
            if name == "unknown":
                assert (
                    calls_after["attempts"][0]["status"] == "INTENT"
                    and calls_after["artifacts"] == []
                )
            assert TicketStore(store.database).ticket() == ("open", 0)
            old_result = None
            if name == "stale":
                (private / "release-old").touch()
                victim.wait(timeout=15)
                assert victim.returncode == 0
                old_result = json.loads((private / "old-result.json").read_text())
                assert (
                    old_result["status"] == "lease_lost"
                    and old_result["code"] == "ownership_changed"
                )
                assert (
                    InvocationLedger(store.database, task.task_id).snapshot()["calls"]
                    == calls_after["calls"]
                )
                assert snapshot(store, task.task_id) == after
            print(
                json.dumps({"phase": "recovery_decided", "scenario": name, **after}),
                flush=True,
            )
            return {
                "scenario": name,
                "passed": True,
                "victim_pid": victim.pid,
                "scanner_pid": live[0].pid,
                "standby_pid": live[1].pid,
                "before": before,
                "after": after,
                "tool_calls_before": 1,
                "tool_calls_after": 1,
                "original_call_id_preserved": True,
                "duplicate_business_writes": 0,
                "old_worker_result": old_result,
            }
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main(args):
    if args.child_demo_id:
        child(
            ROOT
            / "artifacts/recovery-demos"
            / str(args.child_demo_id)
            / args.child_scenario,
            args.child_scenario,
        )
        return 0
    demo_id = uuid4()
    directory = ROOT / "artifacts/recovery-demos" / str(demo_id)
    rows = [
        scenario(directory / name, name, demo_id)
        for name in (args.scenario or ("safe", "unknown", "stale"))
    ]
    report = {
        "stage": "S4-S5-expired-task-recovery-and-stale-worker",
        "passed": True,
        "paid_api_calls": 0,
        "backend": "sqlite",
        "scenarios": rows,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=("safe", "unknown", "stale"), action="append"
    )
    parser.add_argument("--child-demo-id", type=UUID, help=argparse.SUPPRESS)
    parser.add_argument(
        "--child-scenario",
        choices=("safe", "unknown", "stale"),
        default="safe",
        help=argparse.SUPPRESS,
    )
    raise SystemExit(main(parser.parse_args()))
