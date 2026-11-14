import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import IntegrityError

from safeharness.audit import AuditLog
from safeharness.coding import CodingWorkspace
from safeharness.coding_demo import BROKEN, FIXED, fixture, verify_coding
from safeharness.execution import ToolGateway, ToolRequest, ToolResult
from safeharness.runtime import RunResult
from safeharness.task import TaskStatus
from safeharness.task_store import TaskStore


def test_model_claim_needs_tool_evidence():
    workspace = Mock(stopped=False)
    gateway = Mock()
    result = verify_coding(
        workspace, RunResult("model_finished", "All tests pass", ()), gateway
    )
    assert not result.passed and result.code == "missing_test_evidence"
    gateway.verify_contract.assert_not_called()


def test_model_cannot_invoke_control_plane_verification():
    coding = Mock()
    result = ToolGateway(coding=coding).execute(
        ToolRequest("_verify_coding_contract", {"cases": [[1, 0, 2]]})
    )
    assert result.code == "control_plane_only"
    coding.execute.assert_not_called()
    coding.verify_contract.assert_not_called()


def test_coding_outcome_and_report_roll_back_together(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = store.transition(store.create(), TaskStatus.RUNNING)
    task = store.transition(task, TaskStatus.VERIFYING)
    report = {
        "task_id": str(task.task_id),
        "task_status": "SUCCEEDED",
        "verification": {"passed": True},
    }
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER refuse_report BEFORE INSERT ON coding_reports BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
    with pytest.raises(IntegrityError):
        store.complete_coding(task, report)
    assert store.get(task.task_id) == task
    with closing(sqlite3.connect(store.database)) as connection, connection:
        assert (
            connection.execute("SELECT count(*) FROM coding_reports").fetchone()[0] == 0
        )
        connection.execute("DROP TRIGGER refuse_report")
    assert store.complete_coding(task, report).status is TaskStatus.SUCCEEDED


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
    reason="Set SAFEHARNESS_DOCKER_TESTS=1",
)
class TestCodingDemo:
    def test_cross_process_demo_report_events_and_negative_run(self, tmp_path):
        command = [
            sys.executable,
            "-m",
            "safeharness",
            "--db",
            str(tmp_path / "tasks.sqlite3"),
        ]

        def invoke(*args, expected=0):
            process = subprocess.run(
                [*command, *args],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            assert process.returncode == expected, process.stderr
            return json.loads(process.stdout)

        report = invoke("coding-demo", "--attempt-escape")
        assert report["task_status"] == "SUCCEEDED"
        assert report["verification"]["passed"]
        assert report["verification"]["case_count"] == 26
        assert report["before_sha256"] != report["after_sha256"]
        assert report["cleanup_confirmed"]
        assert "+    return min(upper" in report["patch"]
        codes = [item["code"] for item in report["tool_results"]]
        assert codes[:2] == ["path_out_of_scope", "unknown_tool"]
        assert "tests_failed" in codes and codes[-1] == "tests_passed"
        assert invoke("show-coding", report["task_id"]) == report
        assert invoke("show", report["task_id"])["task_status"] == "SUCCEEDED"
        events = invoke("show-events", report["task_id"])
        assert events[-1]["tool_name"] == "_verify_coding_contract"
        assert events[-1]["phase"] == "RESULT"
        failed = invoke("coding-demo", "--skip-fix", expected=1)
        assert failed["task_status"] == "FAILED"
        assert not failed["verification"]["passed"]

    @pytest.mark.parametrize(
        "tamper", ["pass_tests", "exit_zero", "verifier_write", "conftest"]
    )
    def test_independent_contract_rejects_false_success(self, tmp_path, tamper):
        store = TaskStore(tmp_path / "tasks.sqlite3")
        store.initialize()
        task = store.transition(store.create(), TaskStatus.RUNNING)
        files = fixture()
        files["tests/test_solution.py"] = "def test_fake(): assert True\n"
        if tamper == "conftest":
            files["tests/conftest.py"] = (
                "import pytest\ndef pytest_sessionstart(session): pytest.exit('fake success', returncode=0)\n"
            )
        if tamper == "exit_zero":
            files["solution.py"] = "import os\nos._exit(0)\n"
        elif tamper == "verifier_write":
            files["solution.py"] = (
                "from pathlib import Path\nPath('/opt/safeharness/verifier/test_contract.py').write_text('def test_fake(): pass')\n"
                + FIXED
            )
        else:
            files["solution.py"] = BROKEN
        workspace = CodingWorkspace(files, task.task_id)
        gateway = ToolGateway(
            coding=workspace,
            test_paths=frozenset({"tests/test_solution.py"}),
            audit=AuditLog(store.database, task.task_id),
        )
        tested = gateway.execute(
            ToolRequest("run_tests", {"paths": ["tests/test_solution.py"]})
        )
        assert tested.code == (
            "tests_failed" if tamper == "conftest" else "tests_passed"
        )
        store.transition(task, TaskStatus.VERIFYING)
        result = verify_coding(
            workspace,
            RunResult(
                "model_finished",
                "Success",
                (ToolResult("run_tests", "ok", "tests_passed"),),
            ),
            gateway,
        )
        assert not result.passed, result
        assert workspace.last_container.cleanup_confirmed
