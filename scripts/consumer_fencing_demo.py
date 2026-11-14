"""Real PG row-lock heartbeat timeout; same persistent process recovers and continues.

The child waits at an explicit before-start gate. Parent locks only that isolated
queue row, waits for the actual bounded heartbeat query to fail, then releases it.
No database/container outage, mocked renewal error or paid model is used.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from scheduler_soak import fingerprint, stop
from sqlalchemy import text
from sqlalchemy.engine import make_url

from safeharness import worker as worker_module
from safeharness.database import engine_for, execute
from safeharness.heartbeat import LeaseHeartbeat
from safeharness.infrastructure import postgres_url
from safeharness.invocations import InvocationLedger
from safeharness.pg_ticket_store import PostgresTicketStore
from safeharness.service import TaskService, initialize_auth, load_principals
from safeharness.task_store import TaskStore
from safeharness.worker import run_worker

ROOT = Path(__file__).resolve().parents[1]


def wait(check, children=(), timeout=20):
    until = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= until or any(p.poll() is not None for p in children):
            raise RuntimeError("Consumer fault gate failed; inspect private log")
        time.sleep(0.05)


def child(directory):
    private = directory / "service-private"
    store = TaskStore(postgres_url(private))
    store.initialize()
    hearts = []
    original = TaskService.run
    gate, failed, release = [
        private / name for name in ("entered", "fenced", "release")
    ]

    def heartbeat(*args, **kwargs):
        instance = LeaseHeartbeat(*args, **kwargs)
        hearts.append(instance)
        return instance

    def before_start(service, task_id, principal, **kwargs):
        if not gate.exists():
            gate.write_text(str(task_id))
            if not hearts[-1].failed.wait(20):
                raise RuntimeError("Actual heartbeat did not fail")
            failed.touch()
            wait(release.exists)
        return original(service, task_id, principal, **kwargs)

    with (
        patch.object(worker_module, "LeaseHeartbeat", side_effect=heartbeat),
        patch.object(TaskService, "run", before_start),
    ):
        return run_worker(
            store,
            PostgresTicketStore(store.database),
            load_principals(private / "service-auth.json"),
            worker_id="persistent-fence-demo",
            lease_seconds=1.5,
            heartbeat_seconds=0.1,
            poll_seconds=0.05,
        )


def main(args):
    directory = (
        ROOT / "artifacts/consumer-fencing-demos" / str(args.child_demo_id or uuid4())
    )
    if args.child_demo_id:
        return child(directory)
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
        raise ValueError("Only the dedicated local database is allowed")
    schema = "sh_consumer_" + uuid4().hex
    with engine_for(base).begin() as c:
        c.execute(text('CREATE SCHEMA "' + schema + '"'))
    url = base.update_query_dict({"options": "-csearch_path=" + schema})
    config = private / "postgres.json"
    config.write_text(
        json.dumps({"database_url": url.render_as_string(hide_password=False)})
    )
    config.chmod(0o600)
    store = TaskStore(url)
    store.initialize()
    tasks = [
        store.submit("alice", {"scenario": "ticket_read", "execution_mode": "queued"})[
            0
        ]
        for _ in range(2)
    ]
    processes = []
    with (private / "processes.log").open("ab") as log:
        worker = subprocess.Popen(
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
        processes.append(worker)
        try:
            wait((private / "entered").exists, [worker])
            lost_id = UUID((private / "entered").read_text())
            with store._engine.begin() as c:
                execute(
                    c,
                    "SELECT task_id FROM task_queue WHERE task_id=? FOR UPDATE",
                    (lost_id.hex,),
                ).fetchone()
                wait((private / "fenced").exists, [worker])
            (private / "release").touch()
            scanner = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "safeharness",
                    "recover-queue",
                    "--watch",
                    "--interval-seconds",
                    "0.1",
                    "--backend",
                    "postgres",
                    "--config-dir",
                    str(private),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            processes.append(scanner)
            wait(
                lambda: all(
                    store.get(task.task_id).status.value == "SUCCEEDED"
                    for task in tasks
                ),
                processes,
                timeout=30,
            )
            assert worker.poll() is None
            final = [
                {
                    "task_id": str(task.task_id),
                    "status": store.get(task.task_id).status.value,
                    "scheduling": store.scheduling(task.task_id),
                    "calls": len(
                        InvocationLedger(store.database, task.task_id).snapshot()[
                            "calls"
                        ]
                    ),
                }
                for task in tasks
            ]
            assert all(
                item["calls"] == 1 and item["scheduling"]["state"] == "FINISHED"
                for item in final
            )
            assert store.scheduling(lost_id)["generation"] == 2
            assert store.scheduling(lost_id)["recovery_count"] == 1
            assert PostgresTicketStore(url).ticket() == ("open", 0)
        finally:
            for process in reversed(processes):
                stop(process)
    events = []
    for line in (private / "processes.log").read_text().splitlines():
        try:
            event = json.loads(line)
            if event.get("status") == "lease_lost":
                events.append(event)
        except ValueError:
            continue
    assert len(events) == 1 and events[0]["code"] == "heartbeat_failed"
    report = {
        "stage": "S7-persistent-consumer-fence-recovery",
        "passed": True,
        "source_fingerprint": fingerprint(),
        "paid_api_calls": 0,
        "worker_pid": worker.pid,
        "scanner_pid": scanner.pid,
        "same_persistent_worker_stayed_alive": True,
        "real_pg_heartbeat_timeout": True,
        "fault_window": "after claim, before task start, queue row locked until heartbeat fence",
        "lost_task_id": str(lost_id),
        "lease_losses": events,
        "tasks": final,
        "business_writes": 0,
        "retained_schema": schema,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "same_worker_alive": True,
                "tasks_succeeded": 2,
                "report": str(path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-demo-id", type=UUID, help=argparse.SUPPRESS)
    raise SystemExit(main(parser.parse_args()))
