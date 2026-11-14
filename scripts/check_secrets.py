"""Check distributable sources and public evidence without printing secret values."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def index_files():
    """Inspect Git's publication content, including an index older than the worktree."""
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    indexed = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        _, oid, stage = metadata.split()
        if stage != b"0":
            raise ValueError("Unmerged index")
        indexed.append((name.decode("utf-8"), oid))
    blobs = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        input=b"".join(oid + b"\n" for _, oid in indexed),
        capture_output=True,
        check=True,
    ).stdout
    offset = 0
    for name, oid in indexed:
        header_end = blobs.index(b"\n", offset)
        object_type, size_text = blobs[offset:header_end].split()[1:]
        if object_type != b"blob":
            raise ValueError("Index object is not a blob")
        size = int(size_text)
        start = header_end + 1
        end = start + size
        if blobs[end : end + 1] != b"\n":
            raise ValueError("Invalid Git object response")
        yield name, blobs[start:end]
        offset = end + 1


def forbidden_public_path(name):
    path = Path(name)
    private_names = {
        "postgres.env",
        "postgres.json",
        "service-auth.json",
        "service-tokens.json",
        "pico-api.txt",
    }
    return (
        any(
            part.lower()
            in {"artifacts", ".venv", ".tools", "build", "dist", "__pycache__"}
            or part.lower().endswith("-private")
            for part in path.parts
        )
        or path.name.lower() in private_names
        or (path.name.startswith(".env") and path.name != ".env.example")
        or path.suffix.lower()
        in {".sqlite", ".sqlite3", ".db", ".pem", ".key", ".pfx", ".p12"}
        or bool(
            re.search(r"\.(sqlite3?|db)-(wal|shm|journal)$", path.name, re.IGNORECASE)
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Scan all Git index blobs and reject private paths",
    )
    args = parser.parse_args(argv)
    known = set()
    credential = ROOT.parent / "pico-api.txt"
    if credential.exists():
        known.update(
            re.findall(
                r"sk-[A-Za-z0-9_-]{16,}", credential.read_text(encoding="utf-8-sig")
            )
        )
    for path in (ROOT / "artifacts").rglob("service-tokens.json"):
        known.update(json.loads(path.read_text()).values())
    for settings in (ROOT / "artifacts").rglob("postgres.json"):
        if not any(part.endswith("-private") for part in settings.parts):
            continue
        password = urlsplit(json.loads(settings.read_text())["database_url"]).password
        if password:
            known.add(unquote(password))
    paths = [
        path
        for folder in [
            "src",
            "tests",
            "scripts",
            "docs",
            "deploy",
            ".github",
            "examples",
            "apps",
        ]
        for path in (ROOT / folder).rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and ".egg-info" not in str(path)
    ]
    paths += [
        ROOT / "README.md",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
    ]
    for folder in [
        "m4-scans",
        "m4-acceptance",
        "m4-demos",
        "runtime-fault-demos",
        "invocation-demos",
        "harness-demos",
        "approval-loop-demos",
        "verifier-demos",
        "scheduler-demos",
        "queue-worker-demos",
        "lease-demos",
        "recovery-demos",
        "scheduler-soak",
        "queued-approval-demos",
        "database-outage-demos",
        "scheduler-fault-repeats",
        "consumer-fencing-demos",
    ]:
        for path in (ROOT / "artifacts" / folder).rglob("*"):
            if path.is_file() and not any(
                part.endswith("-private") for part in path.parts
            ):
                paths.append(path)
    findings = []
    paths += [
        path
        for path in (ROOT / "artifacts/deepseek-builds").rglob("*")
        if path.is_file()
        and path.suffix in {".json", ".py", ".html", ".md"}
        and "__pycache__" not in path.parts
    ]
    try:
        contents = (
            list(index_files())
            if args.staged
            else [(str(path.relative_to(ROOT)), path.read_bytes()) for path in paths]
        )
    except (subprocess.CalledProcessError, OSError, ValueError):
        print(json.dumps({"status": "failed", "code": "git_index_unavailable"}))
        return 1
    if args.staged and not contents:
        print(json.dumps({"status": "failed", "code": "empty_git_index"}))
        return 1
    for name, data in contents:
        # Actual known credentials and generic long sk-keys, not synthetic fixtures.
        generic_keys = re.findall(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{24,}", data)
        # These two literal values are fake-transport fixtures, never valid keys.
        fixtures = {
            b"sk-synthetic-test-credential-00000",
            b"sk-synthetic-judge-credential-000",
        }
        if (
            (args.staged and forbidden_public_path(name))
            or any(value.encode() in data for value in known)
            or any(value not in fixtures for value in generic_keys)
        ):
            findings.append(name)
    print(
        json.dumps(
            {
                "status": "failed" if findings else "passed",
                "mode": "git_index"
                if args.staged
                else "working_tree_and_public_evidence",
                "files_checked": len(contents),
                "credential_values_checked": len(known),
                "findings": findings,
            },
            indent=2,
        )
    )
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
