"""Explicit paid, bounded multi-file build using the real Harness and Docker tools."""

import argparse
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from safeharness.audit import AuditLog
from safeharness.budget import BudgetLedger
from safeharness.checkpoints import CheckpointSession
from safeharness.coding import CodingWorkspace
from safeharness.context import ContextPolicy
from safeharness.execution import ToolGateway
from safeharness.harness import Harness
from safeharness.invocations import InvocationLedger
from safeharness.provider import DeepSeekAdapter, DeepSeekClient, load_credentials
from safeharness.sandbox import DockerSandbox
from safeharness.task_store import TaskStore
from safeharness.trace import TraceLog
from safeharness.verification import BOOK_CONTRACT_VERSION, book_contract, evaluate_book

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples/budgetbook-task"
WRITABLE = frozenset(
    {
        "solution.py",
        "storage.py",
        "server.py",
        "index.html",
        "README.md",
        "tests/test_extra.py",
    }
)
TESTS = frozenset({"tests/test_contract.py", "tests/test_extra.py"})
CONFIG = {"application": "deepseek-budgetbook-v3", "verifier": BOOK_CONTRACT_VERSION}


def initial_files(seed=None):
    files = {
        **{name: "" for name in WRITABLE},
        "tests/test_contract.py": (TASK / "test_contract.py").read_text(
            encoding="utf-8"
        ),
    }
    if seed:
        for name in WRITABLE:
            source = ROOT / "artifacts/deepseek-builds" / seed / "project" / name
            if source.is_file():
                files[name] = source.read_text(encoding="utf-8")
    return files


def components(store, task_id, ledger, seed=None):
    files = initial_files(seed)
    workspace = CodingWorkspace(files, task_id, task_timeout=600)
    gateway = ToolGateway(
        coding=workspace,
        readable_files=frozenset(files),
        writable_files=WRITABLE,
        test_paths=TESTS,
        audit=AuditLog(store.database, task_id),
    )
    client = DeepSeekClient(
        load_credentials(ROOT.parent / "pico-api.txt"),
        ledger,
        timeout=90,
        max_tokens=4096,
        max_input_bytes=64000,
        max_attempts=2,
    )
    agent = DeepSeekAdapter(
        client,
        task_id=task_id,
        trace=TraceLog(store.database, task_id),
        environment="coding",
        deadline=time.monotonic() + 600,
        instruction=(
            (TASK / "SPEC.md").read_text(encoding="utf-8")
            + (
                "\nRead tests/test_contract.py first. "
                if not seed
                else "\nThis task is seeded with model-authored files. Run tests now, then fix only concrete failures; do not read or rewrite working files unnecessarily. "
            )
            + "Work efficiently within 18 tool actions and a small persistent API budget. "
            "Use separate calls for each file. Each response has at most 4096 output tokens; "
            "do not try to write all files in one response. "
            "Keep each source file BELOW 5000 UTF-8 bytes, use concise implementations. "
            "Split persistence into storage.py if needed. "
            "Write solution.py first, then server.py, index.html, tests/test_extra.py, README.md. "
            "An independent verifier will check the same documented contract after you finish."
            + (
                "\nExisting backend files are a previous model-generated partial build; keep/revise them. "
                "The prior build failed because index.html exceeded 8192 bytes. "
                "Write a SIMPLE COMPACT UI below 4500 UTF-8 bytes; do not reproduce the oversized page. "
                "HTTP handlers must read current persisted data per request, so corrupted storage returns 400. "
                "Complete missing files, then run the public and extra tests. "
                "Do not repeatedly read files that you already have."
                if seed
                else ""
            )
            + (
                "\nSeed file sizes in UTF-8 bytes: "
                + json.dumps({k: len(v.encode("utf-8")) for k, v in files.items()})
                + ". Write empty files only unless tests reveal a concrete failure. "
                "If only README.md is empty, write a concise README then run both public and extra tests and finish."
                if seed
                else ""
            )
        ),
        scope={
            "readable_files": sorted(files),
            "writable_files": sorted(WRITABLE),
            "test_paths": sorted(TESTS),
        },
        context_policy=ContextPolicy(max_bytes=48000, tool_result_bytes=8192),
    )
    verification = {}

    def execute(request):
        reply = gateway.execute(request)
        print(
            json.dumps(
                {"tool": reply.name, "status": reply.status, "code": reply.code}
            ),
            flush=True,
        )
        return reply

    def verify(result):
        if result.stop_reason != "model_finished" or workspace.stopped:
            return False
        last_write = max(
            (i for i, r in enumerate(result.tool_results) if r.name == "write_file"),
            default=-1,
        )
        if not any(
            r.name == "run_tests" and r.code == "tests_passed"
            for r in result.tool_results[last_write + 1 :]
        ):
            verification.update(
                {"passed": False, "code": "missing_final_test_evidence"}
            )
            return False
        candidate = {
            name: workspace.files[name]
            for name in ("solution.py", "storage.py", "server.py", "index.html")
        }
        # Only observation requests enter the sandbox. No expected answers or pytest oracle.
        independent = CodingWorkspace(candidate, task_id, task_timeout=60)
        verifier_gateway = ToolGateway(
            coding=independent,
            audit=AuditLog(store.database, task_id),
        )
        cases, expected = book_contract()
        reply = verifier_gateway.observe_book(cases)
        verdict = evaluate_book(reply, cases, expected)
        TraceLog(store.database, task_id).emit(
            "verification",
            {
                "passed": verdict.passed,
                "code": verdict.code,
                "case_count": verdict.case_count,
                "verifier_version": BOOK_CONTRACT_VERSION,
            },
        )
        verification.update(
            {
                "passed": verdict.passed,
                "code": verdict.code,
                "case_count": verdict.case_count,
                "contract_sha256": verdict.contract_sha256,
                "failures": verdict.failures,
                "value": json.loads(reply.value) if reply.value else None,
                "call_id": str(reply.call_id),
                "attempt_id": str(reply.attempt_id),
                "cleanup_confirmed": independent.last_container.cleanup_confirmed
                if independent.last_container
                else False,
            }
        )
        return verification["passed"]

    harness = Harness(
        store,
        model=agent,
        execute=execute,
        verify=verify,
        workspace=workspace,
        configuration={
            **CONFIG,
            "budget_id": ledger.budget_id,
            "budget_limit_micro": ledger.limit,
        },
        max_steps=18,
        timeout_seconds=600,
    )
    return harness, workspace, verification


def main(args):
    preflight = DockerSandbox().preflight()
    if not preflight.ready:
        print(json.dumps({"ready": False, "reason": "sandbox_unavailable"}))
        return 2
    directory = ROOT / "artifacts/deepseek-builds" / (args.build_id or str(uuid4()))
    info = (
        json.loads((directory / "task.json").read_text(encoding="utf-8"))
        if args.build_id
        else {}
    )
    seed = info.get("seed_build") if args.build_id else args.seed_build
    ledger = BudgetLedger(
        ROOT / "artifacts/provider-budget.sqlite3",
        budget_id=info.get("budget_id", args.budget_id),
        limit_cny=Decimal(info.get("budget_cny", args.budget_cny)),
        max_calls=info.get("max_calls", 12) if args.build_id else args.max_calls,
    )
    if args.prepare_only:
        print(
            json.dumps(
                {
                    "ready": True,
                    "paid_calls": 0,
                    "files": sorted(initial_files(seed)),
                    "budget": ledger.usage(),
                },
                indent=2,
            )
        )
        return 0
    if args.build_id:
        task_id = UUID(info["task_id"])
        store = TaskStore(directory / "control.sqlite3")
    else:
        directory.mkdir(parents=True)
        store = TaskStore(directory / "control.sqlite3")
        store.initialize()
        task = store.create()
        task_id = task.task_id
        (directory / "task.json").write_text(
            json.dumps(
                {
                    "task_id": str(task_id),
                    "seed_build": args.seed_build,
                    "budget_id": args.budget_id,
                    "budget_cny": args.budget_cny,
                    "max_calls": args.max_calls,
                }
            ),
            encoding="utf-8",
        )
    print(
        json.dumps(
            {"build_id": directory.name, "task_id": str(task_id), "phase": "running"}
        ),
        flush=True,
    )
    harness, workspace, verification = components(store, task_id, ledger, seed)
    if args.build_id:
        done = harness.resume(task_id)
    else:
        done = harness.run(task, pause_after=1)
        if done.run.stop_reason == "paused":
            # Discard in-memory adapters, reconstruct them and restore persisted state.
            harness, workspace, verification = components(store, task_id, ledger, seed)
            done = harness.resume(task_id)
    project = directory / "project"
    project.mkdir(exist_ok=True)
    for name, content in workspace.files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    report = {
        "build_id": directory.name,
        "task_id": str(task_id),
        "provider": "deepseek",
        "task_status": done.task.status.value,
        "stop_reason": done.run.stop_reason,
        "error_code": done.run.error_code,
        "answer": done.run.answer,
        "verification": verification,
        "usage": ledger.usage(task_id),
        "budget": ledger.usage(),
        "checkpoints": CheckpointSession.metadata(store, task_id),
        "invocations": InvocationLedger(store.database, task_id).snapshot(),
        "tool_results": [
            {"tool": r.name, "status": r.status, "code": r.code, "value": r.value}
            for r in done.run.tool_results
        ],
        "files": [
            {
                "path": name,
                "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            for name, text in workspace.files.items()
        ],
    }
    (directory / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "task_status": report["task_status"],
                "usage": report["usage"],
                "verification": verification,
                "project": str(project),
                "report": str(directory / "report.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if done.task.status.value == "SUCCEEDED" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Check prerequisites without paid calls",
    )
    parser.add_argument(
        "--build-id",
        type=lambda value: str(UUID(value)),
        help="Continue a previously paused build",
    )
    parser.add_argument(
        "--seed-build",
        type=lambda value: str(UUID(value)),
        help="Start a new task from a prior exported partial build; its terminal task is unchanged",
    )
    parser.add_argument("--budget-id", default="deepseek-program-v2")
    parser.add_argument("--budget-cny", default="5.00")
    parser.add_argument("--max-calls", type=int, default=80)
    sys.exit(main(parser.parse_args()))
