"""Container-only file and test worker. All input is untrusted."""

import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager

sys.path.insert(0, "/opt/safeharness")
from contract import (
    MAX_FILE_BYTES,
    MAX_FILES,
    WorkspaceError,
    validate_files,
    validate_path,
)
from probe import probe


@contextmanager
def parent_fd(path, *, create=False):
    parts = validate_path(path).split("/")
    current = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
            )
            os.close(current)
            current = child
        yield current, parts[-1]
    finally:
        os.close(current)


def checked_file(fd):
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise WorkspaceError("unsafe_workspace_entry")


def read_file(path):
    with parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            checked_file(handle.fileno())
            data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise WorkspaceError("file_limit_exceeded")
            return data.decode("utf-8")


def write_file(path, text):
    validate_files({path: text})
    with parent_fd(path, create=True) as (parent, name):
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as handle:
            checked_file(handle.fileno())
            handle.truncate(0)
            handle.write(text.encode("utf-8"))


def snapshot():
    result = {}
    directories = 0

    def walk(fd, prefix=""):
        nonlocal directories
        directories += 1
        if directories > MAX_FILES:
            raise WorkspaceError("workspace_limit_exceeded")
        for name in sorted(os.listdir(fd)):
            path = validate_path(prefix + name)
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                try:
                    walk(child, path + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                result[path] = read_file(path)
                validate_files(result)
            else:
                raise WorkspaceError("unsafe_workspace_entry")

    fd = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd)
    finally:
        os.close(fd)
    return validate_files(result)


def run_tests(paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 8:
        raise WorkspaceError("invalid_test_paths")
    selected = []
    for path in paths:
        validate_path(path)
        if not path.startswith("tests/") or not path.endswith(".py"):
            raise WorkspaceError("test_path_out_of_scope")
        selected.append("/workspace/" + path)
    return run_pytest(selected)


def run_pytest(selected):
    response = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "/opt/safeharness/test_runner.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            "/opt/safeharness/pytest.ini",
            "--basetemp=/tmp/pytest",
            "--confcutdir=/workspace",
            "--",
            *selected,
        ],
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    try:
        report = json.loads(response.stdout)
        assert report["version"] == 1 and report["exit_code"] == response.returncode
        assert type(report["completed"]) is bool
        assert all(
            type(report[k]) is int and report[k] >= 0
            for k in ("collected", "passed", "failed", "skipped")
        )
    except (ValueError, KeyError, TypeError, AssertionError):
        return {
            "exit_code": response.returncode,
            "completed": False,
            "collected": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "evidence_level": "development_only",
        }
    return report


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(131073))
        if not all(probe().values()):
            raise WorkspaceError("isolation_probe_failed")
        for path, text in validate_files(request["files"]).items():
            write_file(path, text)
        args = request["arguments"]
        action = request["action"]
        if action == "read_file":
            value = read_file(args["path"])
        elif action == "write_file":
            write_file(args["path"], args["content"])
            value = "file_written"
        elif action == "list_files":
            value = sorted(snapshot())
        elif action == "run_tests":
            value = run_tests(args["paths"])
        elif action == "observe_book":
            response = subprocess.run(
                [sys.executable, "-I", "-B", "/opt/safeharness/book_observer.py"],
                input=json.dumps(args),
                text=True,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                check=False,
            )
            try:
                observations = json.loads(response.stdout)
            except ValueError:
                raise WorkspaceError("observation_incomplete") from None
            value = {
                "probe_exit_code": response.returncode,
                "observations": observations,
            }
        elif action == "verify_acceptance":
            observed = subprocess.run(
                [sys.executable, "-I", "-B", "/opt/safeharness/acceptance_probe.py"],
                input=json.dumps(args),
                text=True,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                check=False,
            )
            value = {
                "probe_exit_code": observed.returncode,
                "observed": json.loads(observed.stdout),
            }
        elif action == "verify_contract":
            test_exit = subprocess.call(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-c",
                    "/opt/safeharness/pytest.ini",
                    "--noconftest",
                    "--basetemp=/tmp/verifier",
                    "/opt/safeharness/verifier/test_contract.py",
                ],
                stdout=sys.stderr,
                stderr=sys.stderr,
                env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
            observed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "/opt/safeharness/candidate_probe.py",
                ],
                input=json.dumps(args["cases"]),
                text=True,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                check=False,
            )
            value = {
                "test_exit_code": test_exit,
                "probe_exit_code": observed.returncode,
                "observed": json.loads(observed.stdout),
            }
        else:
            raise WorkspaceError("unknown_worker_action")
        print(
            json.dumps(
                {"ok": True, "code": "completed", "value": value, "files": snapshot()}
            )
        )
    except WorkspaceError as exc:
        print(json.dumps({"ok": False, "code": str(exc)}))
    except FileNotFoundError:
        print(json.dumps({"ok": False, "code": "file_not_found"}))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        print(json.dumps({"ok": False, "code": "unsafe_or_invalid_workspace"}))


if __name__ == "__main__":
    main()
