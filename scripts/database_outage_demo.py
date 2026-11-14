"""Stop/restart only a newly owned PostgreSQL container; verify heartbeat fencing.

Uses no existing database or paid API. The old worker is gated at a saved safe
checkpoint, not in the middle of an external write. Inflight writes are separate tests.
"""

import json
import secrets
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import httpx
from m4_scan import wait_server
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from safeharness import worker as worker_module
from safeharness.checkpoints import CheckpointSession
from safeharness.database import engine_for, execute
from safeharness.heartbeat import LeaseHeartbeat
from safeharness.infrastructure import POSTGRES_IMAGE
from safeharness.invocations import InvocationLedger
from safeharness.pg_ticket_store import PostgresTicketStore
from safeharness.service import initialize_auth, load_principals
from safeharness.task_store import TaskStore
from safeharness.worker import QueueWorker

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(
        ["docker", *args], check=False, capture_output=True, timeout=45
    )
    if result.returncode:
        raise RuntimeError("Owned test container operation failed")
    return result.stdout.decode().strip()


def wait_for_db(url):
    engine = engine_for(url, connect_timeout_seconds=1)
    until = time.monotonic() + 30
    while True:
        try:
            with engine.connect() as connection:
                execute(connection, "SELECT 1")
            return
        except SQLAlchemyError:
            if time.monotonic() > until:
                raise RuntimeError("Dedicated database readiness timed out") from None
            time.sleep(0.2)


def main():
    demo_id = uuid4().hex
    directory = ROOT / "artifacts/database-outage-demos" / demo_id
    private = directory / "service-private"
    initialize_auth(private)
    name = "safeharness-scheduler-fault-" + demo_id
    password = secrets.token_urlsafe(32)
    env_path = private / "postgres.env"
    env_path.write_text(
        "POSTGRES_USER=safeharness\nPOSTGRES_DB=safeharness_test\nPOSTGRES_PASSWORD="
        + password
        + "\n"
    )
    env_path.chmod(0o600)
    created = False
    api = None
    cleanup = False
    started = time.monotonic()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        database_port = listener.getsockname()[1]
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "safeharness.fault-demo=" + demo_id,
            "--publish",
            f"127.0.0.1:{database_port}:5432",
            "--env-file",
            str(env_path),
            "--memory=512m",
            "--cpus=1",
            "--pids-limit=128",
            POSTGRES_IMAGE,
        )
        created = True
        binding = json.loads(
            docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name)
        )["5432/tcp"][0]
        assert binding["HostIp"] == "127.0.0.1"
        url = URL.create(
            "postgresql+psycopg",
            username="safeharness",
            password=password,
            host="127.0.0.1",
            port=int(binding["HostPort"]),
            database="safeharness_test",
        )
        config = private / "postgres.json"
        config.write_text(
            json.dumps({"database_url": url.render_as_string(hide_password=False)})
        )
        config.chmod(0o600)
        wait_for_db(url)
        store = TaskStore(url)
        store.initialize()
        business = PostgresTicketStore(url)
        old = QueueWorker(
            store,
            business,
            load_principals(private / "service-auth.json"),
            lease_seconds=1.5,
            heartbeat_seconds=0.1,
        )
        task, _ = store.submit(
            "alice", {"scenario": "ticket_read", "execution_mode": "queued"}
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        entered, release = Event(), Event()
        hearts = []
        original = CheckpointSession.save

        def heart(*args, **kwargs):
            instance = LeaseHeartbeat(*args, **kwargs)
            hearts.append(instance)
            return instance

        def save(session, current, phase, *args, **kwargs):
            original(session, current, phase, *args, **kwargs)
            if phase == "after_tool" and not entered.is_set():
                entered.set()
                if not release.wait(60):
                    raise RuntimeError("Old worker test gate timed out")

        with (private / "processes.log").open("ab") as log:
            api = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "safeharness",
                    "serve",
                    "--backend",
                    "postgres",
                    "--config-dir",
                    str(private),
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            wait_server(port, api)
            with (
                httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False
                ) as client,
                patch.object(worker_module, "LeaseHeartbeat", side_effect=heart),
                patch.object(CheckpointSession, "save", save),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                future = pool.submit(old.run_once)
                try:
                    assert entered.wait(10)
                    before = store.scheduling(task.task_id)
                    docker("stop", "--time", "1", name)
                    assert hearts[0].failed.wait(10) and hearts[0].claim.fenced.is_set()
                    unavailable = client.get("/ready")
                    assert unavailable.status_code == 503 and unavailable.json() == {
                        "detail": "storage_unavailable"
                    }
                    assert client.get("/health").status_code == 200
                    docker("start", name)
                    wait_for_db(url)
                    until = time.monotonic() + 10
                    while store.scheduling(task.task_id)["lease_status"] != "EXPIRED":
                        assert time.monotonic() < until
                        time.sleep(0.1)
                    assert client.get("/ready").status_code == 200
                    healthy_at = time.monotonic()
                    results = []
                    for command in ("recover-queue", "worker"):
                        args = [
                            sys.executable,
                            "-m",
                            "safeharness",
                            command,
                            "--backend",
                            "postgres",
                            "--config-dir",
                            str(private),
                        ]
                        if command == "worker":
                            args.append("--once")
                        child = subprocess.run(
                            args, check=False, capture_output=True, cwd=ROOT, timeout=30
                        )
                        log.write(child.stderr)
                        assert child.returncode == 0
                        results.append(json.loads(child.stdout))
                    recovery_seconds = time.monotonic() - healthy_at
                    assert results[0]["decisions"][0]["action"] == "resume"
                    assert results[1]["task_status"] == "SUCCEEDED"
                    ledger = InvocationLedger(store.database, task.task_id).snapshot()
                finally:
                    release.set()
                late = future.result(timeout=10)
                assert (
                    late["status"] == "lease_lost"
                    and late["code"] == "ownership_changed"
                )
                assert (
                    InvocationLedger(store.database, task.task_id).snapshot()["calls"]
                    == ledger["calls"]
                )
                assert business.ticket() == ("open", 0)
            report = {
                "stage": "S7-real-database-outage",
                "passed": True,
                "paid_api_calls": 0,
                "task_id": str(task.task_id),
                "container": name,
                "only_new_container_stopped": True,
                "before": before,
                "readiness_during_outage": 503,
                "liveness_during_outage": 200,
                "heartbeat_locally_fenced": True,
                "after_restore": results,
                "old_worker": late,
                "business_writes": 0,
                "healthy_db_to_completion_seconds": round(recovery_seconds, 3),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
    finally:
        if api is not None:
            if api.poll() is None:
                api.terminate()
            api.wait(timeout=10)
        if created:
            label = docker(
                "inspect",
                "--format",
                '{{index .Config.Labels "safeharness.fault-demo"}}',
                name,
            )
            if label != demo_id:
                raise RuntimeError("Refusing cleanup of an unowned container")
            docker("rm", "--force", "--volumes", name)
            result = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True,
                check=False,
                timeout=10,
            )
            cleanup = result.returncode != 0
    report["cleanup_confirmed"] = cleanup
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"passed": True, "cleanup_confirmed": cleanup, "report": str(path)},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
