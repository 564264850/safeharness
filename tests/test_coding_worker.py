import os
from unittest.mock import Mock
from uuid import uuid4

import pytest

from safeharness.coding import CodingWorkspace
from safeharness.sandbox import Preflight, SandboxCheck
from safeharness.sandbox_assets.contract import (
    WorkspaceError,
    validate_files,
    validate_path,
)

docker_test = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
        reason="Set SAFEHARNESS_DOCKER_TESTS=1",
    ),
]


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "C:/Users/key",
        "a\\b",
        "a/../b",
        "a//b",
        "a:stream",
        "./a",
        "",
        "a\x00b",
    ],
)
def test_paths_reject_cross_platform_escape(path):
    with pytest.raises(WorkspaceError):
        validate_path(path)


def test_workspace_import_has_file_and_total_limits():
    with pytest.raises(WorkspaceError, match="file_limit"):
        validate_files({"a.py": "a" * 8193})
    with pytest.raises(WorkspaceError, match="workspace_limit"):
        validate_files({f"a{i}.py": "a" * 8192 for i in range(5)})


def test_workspace_total_limit_rejection_preserves_snapshot_without_dispatch():
    sandbox = Mock()
    initial = {f"f{i}.py": "x" * 8192 for i in range(4)}
    workspace = CodingWorkspace(initial, uuid4(), sandbox=sandbox)
    reply = workspace.execute("write_file", {"path": "extra.py", "content": "x"})
    assert not reply.ok and reply.code == "workspace_limit_exceeded"
    assert not workspace.stopped and workspace.files == initial
    sandbox.preflight.assert_not_called()
    sandbox.run_container.assert_not_called()


def test_workspace_does_not_dispatch_when_docker_is_unavailable():
    sandbox = Mock()
    sandbox.preflight.return_value = Preflight(
        False, (SandboxCheck("engine", False, "offline"),)
    )
    workspace = CodingWorkspace({"a.py": "original"}, uuid4(), sandbox=sandbox)
    assert (
        workspace.execute("write_file", {"path": "a.py", "content": "changed"}).code
        == "sandbox_unavailable"
    )
    assert workspace.files == {"a.py": "original"}
    sandbox.run_container.assert_not_called()


def test_total_task_budget_blocks_new_dispatch():
    sandbox = Mock()
    workspace = CodingWorkspace(
        {"a.py": "original"}, uuid4(), sandbox=sandbox, task_timeout=0
    )
    assert workspace.execute("read_file", {"path": "a.py"}).code == "task_timeout"
    sandbox.preflight.assert_not_called()
    sandbox.run_container.assert_not_called()


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
    reason="Set SAFEHARNESS_DOCKER_TESTS=1",
)
class TestDockerWorker:
    def test_files_and_pytest_use_a_private_snapshot(self):
        workspace = CodingWorkspace(
            {
                "solution.py": "def add(a, b): return a-b\n",
                "tests/test_solution.py": "from solution import add\ndef test_add(): assert add(2, 3) == 5\n",
            },
            uuid4(),
        )
        original = workspace.execute("run_tests", {"paths": ["tests/test_solution.py"]})
        assert original.ok and original.value["exit_code"] == 1, original
        assert workspace.last_container.cleanup_confirmed
        assert workspace.execute(
            "write_file",
            {"path": "solution.py", "content": "def add(a, b): return a+b\n"},
        ).ok
        assert (
            workspace.execute("read_file", {"path": "solution.py"}).value
            == "def add(a, b): return a+b\n"
        )
        listed = workspace.execute("list_files", {})
        assert listed.value == ["solution.py", "tests/test_solution.py"]
        tested = workspace.execute("run_tests", {"paths": ["tests/test_solution.py"]})
        assert tested.code == "tests_passed", tested
        assert "1 passed" in tested.value["output"]
        assert workspace.last_container.cleanup_confirmed
        other = CodingWorkspace({"solution.py": "other task"}, uuid4())
        assert other.execute("read_file", {"path": "solution.py"}).value == "other task"

    @pytest.mark.parametrize("mode", ["timeout", "output"])
    def test_timeout_and_output_overflow_reclaim_entire_container(self, mode):
        attack = (
            "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\ntime.sleep(60)\n"
            if mode == "timeout"
            else "import os\nwhile True: os.write(1,b'x'*4096)\n"
        )
        files = {"tests/test_attack.py": attack}
        workspace = CodingWorkspace(
            files, uuid4(), operation_timeout=2 if mode == "timeout" else 10
        )
        result = workspace.execute("run_tests", {"paths": ["tests/test_attack.py"]})
        assert result.code == (
            "execution_timeout" if mode == "timeout" else "output_limit_exceeded"
        ), result
        assert workspace.stopped
        assert workspace.last_container.cleanup_confirmed
        assert workspace.files == files

    @pytest.mark.parametrize("entry", ["symlink", "hardlink", "fifo"])
    def test_unsafe_entries_are_not_imported_back_to_control_plane(self, entry):
        attack = {
            "symlink": "os.symlink('/etc/passwd', '/workspace/stolen')",
            "hardlink": "os.link('/workspace/seed.py','/workspace/stolen')",
            "fifo": "os.mkfifo('/workspace/stolen')",
        }[entry]
        files = {
            "seed.py": "seed",
            "tests/test_attack.py": f"import os\ndef test_attack(): {attack}\n",
        }
        workspace = CodingWorkspace(files, uuid4())
        result = workspace.execute("run_tests", {"paths": ["tests/test_attack.py"]})
        assert not result.ok and result.code == "unsafe_workspace_entry", result
        assert workspace.files == files
        assert workspace.last_container.cleanup_confirmed
