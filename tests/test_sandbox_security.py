import os
from uuid import uuid4

import pytest

from safeharness.coding import CodingWorkspace
from safeharness.execution import ToolGateway, ToolRequest


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
    reason="Set SAFEHARNESS_DOCKER_TESTS=1",
)
class TestSandboxSecurity:
    def test_indirect_shell_cannot_read_host_credentials_or_use_network(
        self, tmp_path, monkeypatch
    ):
        sentinel = tmp_path / "control-plane-secret.txt"
        sentinel.write_text("synthetic-host-secret")
        monkeypatch.setenv("SAFEHARNESS_HOST_SECRET", "synthetic-host-secret")
        attack = """import os, subprocess, sys, pytest
from pathlib import Path

def test_host_boundary():
    assert os.environ.get('SAFEHARNESS_HOST_SECRET') is None
    assert not Path('/var/run/docker.sock').exists()
    with pytest.raises((FileNotFoundError, PermissionError)):
        Path('/root/.docker/config.json').read_text()
    assert not Path('/workspace/tasks.sqlite3').exists()
    assert not Path(HOST_PATH).exists()
    assert not Path('/run/host_mnt/c/Users/25587/.codex').exists()
    assert not Path('/mnt/c/Users/25587/.codex').exists()
    child = subprocess.run(['/bin/sh', '-c', 'id -u; cat /proc/self/status'], capture_output=True, text=True, check=True)
    assert child.stdout.splitlines()[0] == '65534'
    assert 'NoNewPrivs:\\t1' in child.stdout
    script = "import socket; s=socket.socket(); s.settimeout(.3); s.connect(('198.51.100.1',80))"
    network = subprocess.run([sys.executable, '-c', script], capture_output=True, check=False)
    assert network.returncode != 0
    try:
        Path('/opt/safeharness/verifier/test_contract.py').write_text('tampered')
    except OSError:
        pass
    else:
        raise AssertionError('trusted verifier was writable')
""".replace("HOST_PATH", repr(str(sentinel)))
        workspace = CodingWorkspace({"tests/test_boundary.py": attack}, uuid4())
        gateway = ToolGateway(
            coding=workspace, test_paths=frozenset({"tests/test_boundary.py"})
        )
        tested = gateway.execute(
            ToolRequest("run_tests", {"paths": ["tests/test_boundary.py"]})
        )
        assert tested.code == "tests_passed", tested
        assert workspace.last_container.cleanup_confirmed
        assert sentinel.read_text() == "synthetic-host-secret"

    @pytest.mark.parametrize("resource", ["memory", "pids", "disk"])
    def test_indirect_code_is_bound_by_kernel_budgets(self, resource):
        attacks = {
            "memory": """import subprocess, sys
def test_budget():
    child = subprocess.run([sys.executable, '-c', 'bytearray(400*1024*1024)'], capture_output=True, check=False)
    assert child.returncode != 0
""",
            "pids": """import errno, subprocess, sys
def test_budget():
    children = []
    refused = False
    try:
        for _ in range(80):
            try:
                children.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']))
            except OSError as exc:
                assert exc.errno == errno.EAGAIN
                refused = True
                break
    finally:
        for child in children:
            child.kill()
        for child in children:
            child.wait()
    assert refused and len(children) < 64
""",
            "disk": """import errno
from pathlib import Path
def test_budget():
    path = Path('/workspace/fill.bin')
    refused = False
    try:
        with path.open('wb') as handle:
            for _ in range(20):
                handle.write(b'x' * 1024 * 1024)
    except OSError as exc:
        assert exc.errno == errno.ENOSPC
        refused = True
    finally:
        path.unlink(missing_ok=True)
    assert refused
""",
        }
        workspace = CodingWorkspace(
            {"tests/test_budget.py": attacks[resource]}, uuid4(), operation_timeout=15
        )
        reply = workspace.execute("run_tests", {"paths": ["tests/test_budget.py"]})
        assert reply.code == "tests_passed", reply
        assert workspace.last_container.cleanup_confirmed

    def test_successful_test_cannot_leave_background_process(self):
        test = "import subprocess,sys\ndef test_background():\n    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        workspace = CodingWorkspace({"tests/test_child.py": test}, uuid4())
        reply = workspace.execute("run_tests", {"paths": ["tests/test_child.py"]})
        assert reply.code == "tests_passed", reply
        assert workspace.last_container.cleanup_confirmed
