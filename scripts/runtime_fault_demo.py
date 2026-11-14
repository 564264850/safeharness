"""Three synthetic live-run faults. No paid model or Docker required.

The failing Coding adapter is a host-side test double writing only inside this
demo's newly-created artifact directory, not a production execution path.
"""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from safeharness.audit import AuditLog
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.runtime import FinalAnswer, run_task
from safeharness.task_store import TaskStore
from safeharness.trace import TraceLog

ROOT = Path(__file__).resolve().parents[1]


def crash(*args):
    raise RuntimeError("synthetic private exception text")


def main():
    directory = ROOT / "artifacts/runtime-fault-demos" / str(uuid4())
    directory.mkdir(parents=True)
    database = directory / "tasks.sqlite3"
    store = TaskStore(database)
    store.initialize()
    rows = []
    for phase in ["model", "verification", "tool"]:
        task = store.create()
        writes = []

        class LostResultAdapter:
            def __init__(self, calls, destination):
                self.calls, self.destination = calls, destination

            def execute(self, action, arguments, **budgets):
                self.calls.append(action)
                self.destination.write_text(arguments["content"], encoding="utf-8")
                crash()

        gateway = ToolGateway(
            coding=LostResultAdapter(writes, directory / "candidate.py"),
            writable_files=frozenset({"solution.py"}),
            audit=AuditLog(database, task.task_id),
        )
        model = crash if phase == "model" else lambda _: FinalAnswer("ready")
        if phase == "tool":
            model = lambda _: ToolRequest(
                "write_file",
                {"path": "solution.py", "content": "# synthetic candidate\n"},
            )
        completed = run_task(
            model,
            gateway.execute,
            crash if phase == "verification" else lambda _: True,
            max_steps=2,
            store=store,
            task=task,
        )
        # The show command runs in a fresh process against the committed database.
        queried = subprocess.run(
            [
                sys.executable,
                "-m",
                "safeharness",
                "--db",
                str(database),
                "show",
                str(task.task_id),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            cwd=ROOT,
        )
        row = {
            "injected_phase": phase,
            **json.loads(queried.stdout),
            "adapter_write_count": len(writes),
        }
        expected = "NEEDS_REVIEW" if phase == "tool" else "FAILED"
        assert (
            row["task_status"] == expected and completed.task.status.value == expected
        )
        assert len(writes) == (1 if phase == "tool" else 0)
        (directory / (str(task.task_id) + ".trace.jsonl")).write_text(
            TraceLog(database, task.task_id).jsonl(), encoding="utf-8"
        )
        rows.append(row)
    report = {
        "scope": "synthetic live-process fault injection; Coding adapter is a test double",
        "database": str(database),
        "passed": True,
        "cases": rows,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
