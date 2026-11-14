"""Repeat four named scheduler races/crash windows on SQLite and real PostgreSQL."""

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

from scheduler_soak import fingerprint

ROOT = Path(__file__).resolve().parents[1]
SELECTION = "frozen_old_worker or commit_crash_reconciles or approval_and_wakeup_roll_back or concurrent_submitters_cannot_overbook"


def main(args):
    if not 1 <= args.repetitions <= 100:
        raise ValueError("Repetitions must be 1..100")
    directory = ROOT / "artifacts/scheduler-fault-repeats" / str(uuid4())
    private = directory / "service-private"
    private.mkdir(parents=True)
    source = fingerprint()
    start = time.monotonic()
    rows = []
    for index in range(args.repetitions):
        xml = directory / f"round-{index + 1:02}.xml"
        with (private / f"round-{index + 1:02}.log").open("wb") as log:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_stale_worker.py",
                    "tests/test_queued_approval.py",
                    "tests/test_operations.py",
                    "-k",
                    SELECTION,
                    f"--junitxml={xml}",
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
                check=False,
                timeout=180,
                env={**os.environ, "SAFEHARNESS_POSTGRES_TESTS": "1"},
            )
        suites = ET.parse(xml).getroot()
        cases = list(suites.iter("testcase"))
        row = {
            "round": index + 1,
            "returncode": process.returncode,
            "tests": len(cases),
            "failures": len(list(suites.iter("failure"))),
            "errors": len(list(suites.iter("error"))),
            "skipped": len(list(suites.iter("skipped"))),
            "xml": str(xml.relative_to(ROOT)),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
        if process.returncode or len(cases) != 8 or row["skipped"]:
            break
    passed = (
        len(rows) == args.repetitions
        and all(
            r["returncode"] == r["failures"] == r["errors"] == r["skipped"] == 0
            and r["tests"] == 8
            for r in rows
        )
        and fingerprint() == source
    )
    report = {
        "passed": passed,
        "source_fingerprint": source,
        "repetitions_requested": args.repetitions,
        "repetitions_completed": len(rows),
        "tests": sum(r["tests"] for r in rows),
        "named_windows": [
            "old worker released after replacement completes",
            "business committed before control receipt persisted",
            "approval wake transaction rollback",
            "two submitters competing for one capacity slot",
        ],
        "backends": ["sqlite", "postgres"],
        "paid_api_calls": 0,
        "measured_seconds": round(time.monotonic() - start, 3),
        "rounds": rows,
        "limit": "These four windows were repeated; other matrix cases have separate coverage, not 30 repeats each.",
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": passed, "report": str(path)}), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=30)
    raise SystemExit(main(parser.parse_args()))
