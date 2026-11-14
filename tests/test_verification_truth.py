import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from safeharness.audit import AuditLog
from safeharness.coding import CodingWorkspace
from safeharness.execution import ToolGateway, ToolRequest, ToolResult
from safeharness.harness import Harness
from safeharness.invocations import InvocationLedger
from safeharness.runtime import FinalAnswer
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore
from safeharness.verification import book_contract, evaluate_book


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"passed": True},
        {"probe_exit_code": 0, "observations": {"version": 1, "observations": []}},
        {
            "probe_exit_code": 0,
            "observations": {
                "version": 1,
                "observations": [{"id": "fake", "observed": []}],
            },
        },
    ],
)
def test_missing_or_forged_success_is_not_a_contract_result(payload):
    cases, expected = book_contract()
    reply = ToolResult("_observe_book", "ok", "completed", json.dumps(payload))
    assert not evaluate_book(reply, cases, expected).passed


def test_duplicate_ids_wrong_types_or_wrong_values_fail():
    cases, expected = book_contract()
    rows = [{"id": c["id"], "observed": v} for c, v in zip(cases, expected)]
    for change in ("duplicate", "boolean", "wrong"):
        copy = json.loads(json.dumps(rows))
        if change == "duplicate":
            copy[1]["id"] = copy[0]["id"]
        else:
            copy[0]["observed"][1]["value"]["count"] = (
                False if change == "boolean" else 7
            )
        reply = ToolResult(
            "_observe_book",
            "ok",
            "completed",
            json.dumps(
                {
                    "probe_exit_code": 0,
                    "observations": {"version": 1, "observations": copy},
                }
            ),
        )
        assert not evaluate_book(reply, cases, expected).passed


docker = pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
    reason="Enable real isolated verifier tests",
)


@pytest.mark.docker
@docker
@pytest.mark.parametrize(
    "source",
    [
        "import os\nos._exit(0)\n",
        "raise SystemExit(0)\n",
        'import os\nos.write(1,b\'{"version":1,"completed":true,"exit_code":0,"collected":1,"passed":1,"failed":0,"skipped":0}\')\nos._exit(0)\n',
    ],
)
def test_early_exit_or_forged_pytest_report_cannot_pass_final_verification(
    tmp_path, source
):
    store = TaskStore(tmp_path / "control.sqlite3")
    store.initialize()
    task = store.transition(
        store.transition(store.create(), TaskStatus.RUNNING), TaskStatus.VERIFYING
    )
    files = {
        "solution.py": source,
        "tests/test_private.py": "import solution\n\ndef test_never_passes():\n    assert False\n",
    }
    workspace = CodingWorkspace(files, task.task_id)
    gateway = ToolGateway(
        coding=workspace,
        test_paths=frozenset({"tests/test_private.py"}),
        audit=AuditLog(store.database, task.task_id),
    )
    assert (
        gateway.verify_tests(["tests/test_private.py"]).code
        == "trusted_contract_required"
    )
    assert workspace.last_container is None
    cases, expected = book_contract()
    reply = gateway.observe_book(cases)
    assert not evaluate_book(reply, cases, expected).passed
    assert workspace.last_container.cleanup_confirmed
    calls = InvocationLedger(store.database, task.task_id).snapshot()["calls"]
    assert calls[-1]["kind"] == "verification"


@pytest.mark.docker
@docker
def test_plain_early_exit_is_not_development_tests_passed():
    w = CodingWorkspace(
        {
            "solution.py": "import os\nos._exit(0)\n",
            "tests/test_x.py": "import solution\n\ndef test_fail(): assert False\n",
        },
        uuid4(),
    )
    reply = w.execute("run_tests", {"paths": ["tests/test_x.py"]})
    assert reply.code == "tests_failed" and reply.value["completed"] is False
    assert w.last_container.cleanup_confirmed


@pytest.mark.docker
@docker
def test_real_generated_book_passes_host_contract_with_audited_observations(tmp_path):
    root = Path(__file__).resolve().parents[1] / "apps/budgetbook"
    files = {
        name: (root / name).read_text(encoding="utf-8")
        for name in ("solution.py", "storage.py", "server.py", "index.html")
    }
    store = TaskStore(tmp_path / "control.sqlite3")
    store.initialize()
    task = store.transition(
        store.transition(store.create(), TaskStatus.RUNNING), TaskStatus.VERIFYING
    )
    w = CodingWorkspace(files, task.task_id)
    gateway = ToolGateway(coding=w, audit=AuditLog(store.database, task.task_id))
    cases, expected = book_contract()
    reply = gateway.observe_book(cases)
    result = evaluate_book(reply, cases, expected)
    assert result.passed, (result, reply)
    assert result.case_count == len(cases) and w.last_container.cleanup_confirmed
    assert reply.call_id is not None
    # JSON booleans must not compare equal to numeric values in HTTP results.
    payload = json.loads(reply.value)
    payload["observations"]["observations"][-1]["observed"][5]["body"] = {"deleted": 1}
    forged = ToolResult(reply.name, reply.status, reply.code, json.dumps(payload))
    assert not evaluate_book(forged, cases, expected).passed


@pytest.mark.docker
@docker
@pytest.mark.parametrize(
    "source",
    ["# no test functions\n", "import pytest\ndef test_x(): pytest.skip('skip')\n"],
)
def test_zero_or_skipped_assertions_are_not_development_success(source):
    w = CodingWorkspace({"tests/test_x.py": source}, uuid4())
    reply = w.execute("run_tests", {"paths": ["tests/test_x.py"]})
    assert reply.code == "tests_failed"
    assert reply.value["passed"] == 0
    assert w.last_container.cleanup_confirmed


def test_observation_endpoint_is_not_a_model_tool(tmp_path):
    store = TaskStore(tmp_path / "db.sqlite3")
    store.initialize()
    task = store.transition(store.create(), TaskStatus.RUNNING)
    g = ToolGateway(audit=AuditLog(store.database, task.task_id))
    cases, _ = book_contract()
    assert (
        g.execute(ToolRequest("_observe_book", {"cases": cases})).code
        == "control_plane_only"
    )


def test_empty_host_expectations_cannot_silently_skip_checks():
    cases, _ = book_contract()
    reply = ToolResult("_observe_book", "ok", "completed", "{}")
    assert evaluate_book(reply, cases, []).code == "invalid_host_contract"


@pytest.mark.docker
@docker
def test_forged_development_success_still_finishes_task_failed(tmp_path):
    store = TaskStore(tmp_path / "db.sqlite3")
    store.initialize()
    task = store.create()
    fake = {
        "version": 1,
        "exit_code": 0,
        "completed": True,
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
    }
    source = (
        "import os\nseen=set()\nfor name in os.listdir('/proc/self/fd'):\n try:\n  target=os.readlink('/proc/self/fd/'+name)\n  if target.startswith('pipe:') and target not in seen:\n   os.write(int(name),"
        + repr(json.dumps(fake).encode())
        + ")\n   seen.add(target)\n except OSError: pass\nos._exit(0)\n"
    )
    files = {
        "solution.py": source,
        "tests/test_x.py": "import solution\n\ndef test_fail(): assert False\n",
    }
    w = CodingWorkspace(files, task.task_id)
    g = ToolGateway(
        coding=w,
        test_paths=frozenset({"tests/test_x.py"}),
        audit=AuditLog(store.database, task.task_id),
    )

    def verify(_):
        cases, expected = book_contract()
        return evaluate_book(g.observe_book(cases), cases, expected).passed

    done = Harness(
        store,
        model=lambda r: (
            FinalAnswer("Finished")
            if r
            else ToolRequest("run_tests", {"paths": ["tests/test_x.py"]})
        ),
        execute=g.execute,
        verify=verify,
        configuration={"test": "forged-development-report"},
        stateless_model=True,
    ).run(task)
    assert (
        done.run.tool_results[0].code == "tests_passed"
    )  # Explicitly untrusted development feedback.
    assert done.task.status is TaskStatus.FAILED
