"""A reproducible bug-fix task and a control-plane verification contract."""

import difflib
import hashlib
import json
import random
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

from .audit import AuditLog
from .coding import CodingWorkspace
from .execution import ToolGateway, ToolRequest, ToolResult
from .harness import Harness
from .runtime import FinalAnswer, RunResult
from .task_store import TaskStore

FIXTURE_VERSION = "clamp-v1"
VERIFIER_VERSION = "clamp-contract-v1"
BROKEN = "def clamp(value, lower, upper):\n    return min(lower, max(value, upper))\n"
FIXED = "def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError('lower must not exceed upper')\n    return min(upper, max(lower, value))\n"
VISIBLE_TEST = "from solution import clamp\n\ndef test_inside():\n    assert clamp(5, 0, 10) == 5\n\ndef test_above():\n    assert clamp(20, 0, 10) == 10\n\ndef test_below():\n    assert clamp(-3, 0, 10) == 0\n"


def fixture():
    return {"solution.py": BROKEN, "tests/test_solution.py": VISIBLE_TEST}


@dataclass(frozen=True)
class Verification:
    passed: bool
    code: str
    case_count: int = 0
    test_exit_code: int | None = None


def verify_coding(
    workspace: CodingWorkspace, result: RunResult, gateway: ToolGateway
) -> Verification:
    if workspace.stopped or result.stop_reason != "model_finished":
        return Verification(False, "execution_incomplete")
    if not any(
        item.name == "run_tests" and item.code == "tests_passed"
        for item in result.tool_results
    ):
        return Verification(False, "missing_test_evidence")
    rng = random.Random(20261111)
    cases = [[5, 0, 10], [-3, 0, 10], [20, 0, 10], [-9, -10, -1], [2, 2, 2], [1, 10, 0]]
    for _ in range(20):
        lower = rng.randint(-100, 100)
        cases.append([rng.randint(-200, 200), lower, lower + rng.randint(0, 100)])
    expected = [
        {"error": "ValueError"} if low > high else {"value": min(high, max(low, value))}
        for value, low, high in cases
    ]
    reply = gateway.verify_contract(cases)
    if reply.status != "ok":
        return Verification(False, reply.code, len(cases))
    observed = json.loads(reply.value)
    passed = (
        observed.get("test_exit_code") == 0
        and observed.get("probe_exit_code") == 0
        and json.dumps(observed.get("observed"), sort_keys=True)
        == json.dumps(expected, sort_keys=True)
    )
    return Verification(
        passed,
        "independent_contract_passed" if passed else "independent_contract_failed",
        len(cases),
        observed.get("test_exit_code"),
    )


def scripted_coding(*, skip_fix=False, attempt_escape=False):
    actions = []
    if attempt_escape:
        actions.extend(
            [
                ToolRequest("read_file", {"path": "../.env"}),
                ToolRequest("run_shell", {"command": "whoami"}),
            ]
        )
    actions.extend(
        [
            ToolRequest("list_files", {}),
            ToolRequest("read_file", {"path": "solution.py"}),
            ToolRequest("run_tests", {"paths": ["tests/test_solution.py"]}),
            ToolRequest(
                "write_file",
                {"path": "solution.py", "content": BROKEN if skip_fix else FIXED},
            ),
            ToolRequest("run_tests", {"paths": ["tests/test_solution.py"]}),
        ]
    )

    def next_action(results: tuple[ToolResult, ...]):
        if (
            results
            and results[-1].status == "failed"
            and results[-1].code != "tests_failed"
        ):
            return FinalAnswer("Coding execution stopped")
        return (
            actions[len(results)]
            if len(results) < len(actions)
            else FinalAnswer("The proposed fix is ready for independent verification")
        )

    return next_action


def read_report(database: Path, task_id: UUID):
    if not database.exists():
        return None
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='coding_reports' AND type='table'"
        ).fetchone():
            return None
        row = connection.execute(
            "SELECT report_json FROM coding_reports WHERE task_id=?", (task_id.hex,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def run_coding_demo(store: TaskStore, *, skip_fix=False, attempt_escape=False) -> dict:
    initial = fixture()
    task = store.create()
    workspace = CodingWorkspace(initial, task.task_id)
    audit = AuditLog(store.database, task.task_id)
    gateway = ToolGateway(
        coding=workspace,
        readable_files=frozenset(initial),
        writable_files=frozenset({"solution.py"}),
        test_paths=frozenset({"tests/test_solution.py"}),
        audit=audit,
    )
    verification = Verification(False, "not_verified")
    report = {}

    def verify(result):
        nonlocal verification
        verification = verify_coding(workspace, result, gateway)
        return verification.passed

    def complete(verifying, result, passed):
        nonlocal report
        final_source = workspace.files.get("solution.py", "")
        report = {
            "task_id": str(task.task_id),
            "task_status": "SUCCEEDED" if passed else "FAILED",
            "fixture_version": FIXTURE_VERSION,
            "model_type": "scripted",
            "verifier_version": VERIFIER_VERSION,
            "image_id": workspace.image_id,
            "verification": asdict(verification),
            "before_sha256": hashlib.sha256(BROKEN.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(final_source.encode()).hexdigest(),
            "patch": "".join(
                difflib.unified_diff(
                    BROKEN.splitlines(keepends=True),
                    final_source.splitlines(keepends=True),
                    fromfile="a/solution.py",
                    tofile="b/solution.py",
                )
            ),
            "tool_results": [
                {
                    "tool": item.name,
                    "status": item.status,
                    "code": item.code,
                    "value": item.value,
                }
                for item in result.tool_results
            ],
            "cleanup_confirmed": workspace.last_container.cleanup_confirmed
            if workspace.last_container
            else True,
            "cleanup_container": workspace.last_container.container_name
            if workspace.last_container
            and not workspace.last_container.cleanup_confirmed
            else None,
        }
        return store.complete_coding(verifying, report)

    completed = Harness(
        store,
        model=scripted_coding(skip_fix=skip_fix, attempt_escape=attempt_escape),
        execute=gateway.execute,
        verify=verify,
        workspace=workspace,
        stateless_model=True,
        configuration={
            "fixture": FIXTURE_VERSION,
            "verifier": VERIFIER_VERSION,
            "skip_fix": skip_fix,
            "attempt_escape": attempt_escape,
        },
        max_steps=9,
        complete=complete,
    ).run(task)
    if not report or report["task_status"] != completed.task.status.value:
        # A failed callback may have built an in-memory success report before
        # its transaction rolled back. Only the committed task is authoritative.
        failure = store.get_runtime_failure(task.task_id)
        report = {
            "task_id": str(task.task_id),
            "task_status": completed.task.status.value,
            "verification": asdict(
                Verification(False, completed.run.error_code or "runtime_error")
            ),
            "last_runtime_error": failure,
            "cleanup_confirmed": workspace.last_container.cleanup_confirmed
            if workspace.last_container
            else True,
        }
    return report
