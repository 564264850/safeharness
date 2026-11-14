"""Checkpoint recovery at a safe boundary and an ambiguous boundary. No paid API."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

from safeharness.agent import ToolCallingAgent
from safeharness.audit import AuditLog
from safeharness.checkpoints import CheckpointSession
from safeharness.coding import CodingWorkspace
from safeharness.coding_demo import FIXED, fixture, verify_coding
from safeharness.context import ContextManager, ContextPolicy
from safeharness.execution import ToolGateway, ToolResult
from safeharness.harness import Harness
from safeharness.invocations import InvocationLedger
from safeharness.runtime import run_task
from safeharness.task_store import TaskStore
from safeharness.trace import TraceLog

ROOT = Path(__file__).resolve().parents[1]


def assistant(actions):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "synthetic_" + str(i),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for i, (name, arguments) in enumerate(actions)
        ],
    }


class SyntheticBackend:
    def __init__(self, coding):
        self.coding = coding
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if any(m["role"] == "tool" for m in messages):
            return {
                "role": "assistant",
                "content": "Ready for independent verification",
            }
        return assistant(
            [
                ("read_file", {"path": "solution.py"}),
                ("write_file", {"path": "solution.py", "content": FIXED}),
                ("run_tests", {"paths": ["tests/test_solution.py"]}),
            ]
            if self.coding
            else [("read_ticket", {"ticket_id": "IT-001"})] * 2
        )


def components(store, task_id, coding):
    workspace = CodingWorkspace(fixture(), task_id) if coding else None
    gateway = ToolGateway(
        {"IT-001": "open"},
        frozenset({"IT-001"}),
        coding=workspace,
        readable_files=frozenset(fixture()) if coding else frozenset(),
        writable_files=frozenset({"solution.py"}),
        test_paths=frozenset({"tests/test_solution.py"}),
        audit=AuditLog(store.database, task_id),
    )
    backend = SyntheticBackend(coding)
    model = ToolCallingAgent(
        backend,
        instruction="Fix clamp and run tests" if coding else "Read IT-001 twice",
        environment="coding" if coding else "ticket",
        scope={"files": ["solution.py"], "tickets": ["IT-001"]},
        task_id=task_id,
        trace=TraceLog(store.database, task_id),
        deadline=time.monotonic() + 120,
        model_identity="synthetic-checkpoint-v1",
    )

    def verify(result):
        return (
            verify_coding(workspace, result, gateway).passed
            if coding
            else (
                len(result.tool_results) == 2
                and all(r.value == "open" for r in result.tool_results)
            )
        )

    return model, gateway, workspace, verify, backend


def worker(args):
    store = TaskStore(Path(args.directory) / "control.sqlite3")
    store.initialize()
    task_id = UUID(args.task_id)
    model, gateway, workspace, verify, _ = components(store, task_id, args.coding)

    class ProcessLoss(CheckpointSession):
        def save(self, task, phase, results, steps, action=None):
            if args.boundary == "uncertain" and phase == "after_tool" and steps == 2:
                os._exit(78)
            super().save(task, phase, results, steps, action)
            if args.boundary == "safe" and phase == "after_tool" and steps == 2:
                os._exit(78)

    checkpoint = ProcessLoss(
        store,
        task_id,
        {
            "application": {"demo": "harness-v1", "coding": args.coding},
            "max_repeated_failures": 3,
        },
        model=model,
        workspace=workspace,
    )
    run_task(
        model,
        gateway.execute,
        verify,
        max_steps=6,
        store=store,
        task=store.get(task_id),
        checkpoint=checkpoint,
    )
    raise RuntimeError("Fault point was not reached")


def compact_demo():
    context = ContextManager(
        "trusted scope",
        "keep this task",
        policy=ContextPolicy(
            max_bytes=2200, max_messages=8, summary_bytes=512, tool_result_bytes=512
        ),
    )
    for index in range(10):
        message = assistant([("read_ticket", {"ticket_id": "IT-001"})])
        message["tool_calls"][0]["id"] = str(index)
        context.append_assistant(message)
        context.append_result(
            str(index),
            ToolResult("read_ticket", "ok", "ticket_read", "合成工具输出" * 200),
        )
        context.build()
    assert context.last_metadata["omitted_groups"] > 0
    return context.last_metadata


def main(args):
    directory = ROOT / "artifacts/harness-demos" / str(uuid4())
    directory.mkdir(parents=True)
    results = []
    for boundary in ("safe", "uncertain"):
        folder = directory / boundary
        folder.mkdir()
        store = TaskStore(folder / "control.sqlite3")
        store.initialize()
        task = store.create()
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--directory",
            str(folder),
            "--task-id",
            str(task.task_id),
            "--boundary",
            boundary,
        ]
        if args.coding:
            command.append("--coding")
        crashed = subprocess.run(
            command, cwd=ROOT, capture_output=True, timeout=90, check=False
        )
        assert crashed.returncode == 78, (
            "Synthetic worker did not reach its expected fault point"
        )
        before = InvocationLedger(store.database, task.task_id).snapshot()
        model, gateway, workspace, verify, backend = components(
            store, task.task_id, args.coding
        )
        done = Harness(
            store,
            model=model,
            execute=gateway.execute,
            verify=verify,
            workspace=workspace,
            max_steps=6,
            configuration={"demo": "harness-v1", "coding": args.coding},
        ).resume(task.task_id, recover_abandoned=True, expected_generation=1)
        after = InvocationLedger(store.database, task.task_id).snapshot()
        expected = "SUCCEEDED" if boundary == "safe" else "NEEDS_REVIEW"
        assert done.task.status.value == expected
        assert {c["call_id"] for c in before["calls"]} <= {
            c["call_id"] for c in after["calls"]
        }
        assert all(
            len([a for a in after["attempts"] if a["call_id"] == c["call_id"]]) == 1
            for c in before["calls"]
        )
        if boundary == "safe":
            assert len(done.run.tool_results) == (3 if args.coding else 2)
            if args.coding:
                assert workspace.files["solution.py"] == FIXED
        else:
            assert backend.calls == 0
            assert all(
                after[key] == before[key] for key in ("calls", "attempts", "artifacts")
            )
        results.append(
            {
                "boundary": boundary,
                "task_id": str(task.task_id),
                "task_status": expected,
                "process_exit": crashed.returncode,
                "resumed_model_requests": backend.calls,
                "checkpoint": CheckpointSession.metadata(store, task.task_id),
                "invocations": after,
            }
        )
    report = {
        "passed": True,
        "coding": args.coding,
        "paid_provider_calls": 0,
        "context": compact_demo(),
        "cases": results,
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": True,
                "coding": args.coding,
                "states": [r["task_status"] for r in results],
                "context_omitted_groups": report["context"]["omitted_groups"],
                "report": str(path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--coding", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--directory", help=argparse.SUPPRESS)
    parser.add_argument("--task-id", help=argparse.SUPPRESS)
    parser.add_argument(
        "--boundary", choices=("safe", "uncertain"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    worker(args) if args.worker else main(args)
