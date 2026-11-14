import json
import os
import sys
from unittest.mock import Mock

import pytest

from safeharness.__main__ import main
from safeharness.process import CommandResult, run_bounded
from safeharness.sandbox import DockerSandbox, source_digest


def engine_info(**changes):
    return {
        "OSType": "linux",
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsPeriod": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        **changes,
    }


def image_info():
    return [
        {
            "Id": "sha256:fixture",
            "Os": "linux",
            "Config": {
                "Labels": {"safeharness.source-sha256": source_digest()},
                "Volumes": None,
            },
        }
    ]


def ok_json(value):
    return CommandResult(0, json.dumps(value), "")


@pytest.mark.parametrize(
    "changes",
    [
        {"OSType": "windows"},
        {"MemoryLimit": False},
        {"SwapLimit": False},
        {"CpuCfsQuota": False},
        {"PidsLimit": False},
        {"SecurityOptions": []},
    ],
)
def test_preflight_requires_linux_and_enforced_limits(changes):
    sandbox = DockerSandbox()
    sandbox._executable = "docker"
    sandbox._call = Mock(
        side_effect=[ok_json(engine_info(**changes)), ok_json(image_info())]
    )
    assert not sandbox.preflight().ready


def test_preflight_rejects_stale_image():
    sandbox = DockerSandbox()
    sandbox._executable = "docker"
    image = image_info()
    image[0]["Config"]["Labels"] = {}
    sandbox._call = Mock(side_effect=[ok_json(engine_info()), ok_json(image)])
    assert not sandbox.preflight().ready


def test_unavailable_engine_never_creates_container_or_host_fallback(
    tmp_path, monkeypatch, capsys
):
    sandbox = DockerSandbox()
    sandbox._executable = "docker"
    sandbox._call = Mock(return_value=CommandResult(1, "{}", "engine offline"))
    report = sandbox.smoke()
    assert report.status == "denied"
    assert report.code == "sandbox_unavailable"
    assert sandbox._call.call_count == 1
    monkeypatch.setattr("safeharness.__main__.DockerSandbox", lambda: sandbox)
    monkeypatch.chdir(tmp_path)
    assert main(["sandbox-check"]) == 1
    assert not json.loads(capsys.readouterr().out)["ready"]
    assert not (tmp_path / "artifacts").exists()


def test_missing_cli_is_explicit():
    sandbox = DockerSandbox()
    sandbox._executable = None
    assert sandbox.preflight().checks[0].code == "docker_cli_missing"


@pytest.mark.parametrize("mode", ["timeout", "output"])
def test_cli_capture_enforces_budgets(mode):
    script = (
        "import time; time.sleep(10)"
        if mode == "timeout"
        else "import os; os.write(1, b'x'*1000000); os.write(2, b'y'*1000000)"
    )
    result = run_bounded(
        [sys.executable, "-c", script],
        timeout=0.3 if mode == "timeout" else 5,
        output_limit=1024,
    )
    assert result.timed_out if mode == "timeout" else result.output_limited
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024


def test_capture_sends_input_without_shell():
    result = run_bounded(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        input_bytes=b"$(echo secret);literal",
    )
    assert result.stdout.strip() == "$(echo secret);literal"


@pytest.mark.parametrize("mode", ["already_absent", "transient_query", "unconfirmed"])
def test_cleanup_uses_positive_absence_evidence_and_bounded_rechecks(mode):
    sandbox = DockerSandbox()
    sandbox._isolated = Mock(return_value=True)
    probes = 0
    removals = []

    def call(*args, **kwargs):
        nonlocal probes
        if args[0] == "inspect":
            return ok_json([{}])
        if args[0] == "rm":
            removals.append(args[-1])
            return CommandResult(1 if mode == "already_absent" else 0, "", "")
        if args[0] == "ps":
            probes += 1
            fail = mode == "unconfirmed" or mode == "transient_query" and probes == 1
            return CommandResult(1 if fail else 0, "", "", timed_out=fail)
        return CommandResult(0, "{}", "")

    sandbox._call = call
    result = sandbox.run_container("synthetic-image", "worker.py")
    assert result.cleanup_confirmed == (mode != "unconfirmed")
    assert result.code == (
        "container_cleanup_failed" if mode == "unconfirmed" else "completed"
    )
    assert len(removals) == (1 if mode == "already_absent" else 2)
    assert all(name == result.container_name for name in removals)


@pytest.mark.docker
@pytest.mark.skipif(
    os.getenv("SAFEHARNESS_DOCKER_TESTS") != "1",
    reason="Set SAFEHARNESS_DOCKER_TESTS=1 to run real Docker tests",
)
def test_real_sandbox_smoke():
    report = DockerSandbox().smoke()
    assert report.status == "passed", report
    assert report.cleanup_confirmed
    assert all(report.probe.values())
