"""Execute and retain every outcome in the fixed 120-instance development set."""

import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


def interval(numerator, denominator):
    if not denominator:
        return None
    z = 1.959963984540054
    p = numerator / denominator
    center = (p + z * z / (2 * denominator)) / (1 + z * z / denominator)
    margin = (
        z
        * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator**2))
        / (1 + z * z / denominator)
    )
    return [max(0, center - margin), min(1, center + margin)]


class Results:
    def __init__(self):
        self.rows = {}

    def pytest_runtest_logreport(self, report):
        row = self.rows.setdefault(
            report.nodeid, {"case_id": report.nodeid, "duration_ms": 0}
        )
        row["duration_ms"] += round(report.duration * 1000, 3)
        if report.when == "call" or report.outcome != "passed":
            row["outcome"] = report.outcome
            if report.failed:
                row["failure"] = str(report.longrepr)
            if report.skipped:
                row["skip_reason"] = str(report.longrepr)
        for name, value in report.user_properties:
            if name == "acceptance":
                row["evidence"] = json.loads(value)


def main():
    os.chdir(ROOT)
    # Both resources must be explicitly enabled by the operator/CI environment.
    directory = ROOT / "artifacts/m4-acceptance" / str(uuid4())
    directory.mkdir(parents=True)
    plugin = Results()
    code = pytest.main(
        [
            "-q",
            "tests/test_m4_acceptance.py",
            "--tb=short",
            "-o",
            "junit_family=xunit1",
            "--junitxml=" + str(directory / "junit.xml"),
        ],
        plugins=[plugin],
    )
    rows = list(plugin.rows.values())
    groups = {}
    expected_counts = {"coding": 30, "it": 30, "attack": 40, "fault": 20}
    for group, expected in expected_counts.items():
        selected = [
            row for row in rows if row.get("evidence", {}).get("group") == group
        ]
        groups[group] = {
            "expected_instances": expected,
            "evidence_instances": len(selected),
            "passed": sum(row.get("outcome") == "passed" for row in selected),
            "families": sorted({row["evidence"]["family"] for row in selected}),
        }
    faults = [row for row in rows if row.get("evidence", {}).get("group") == "fault"]
    recoverable = [row for row in faults if row["evidence"]["recoverable"]]
    recovered = sum(row.get("outcome") == "passed" for row in recoverable)
    attacks = [row for row in rows if row.get("evidence", {}).get("group") == "attack"]
    normal = [
        row for row in rows if row.get("evidence", {}).get("group") in {"coding", "it"}
    ]
    legitimate = [
        event
        for row in normal
        if row["evidence"]["family"] != "read_only"
        for event in row["evidence"]["trace"]
        if event["kind"] == "tool.policy"
        and not event["payload"]["tool_name"].startswith("_")
    ]
    false_rejections = sum(event["payload"]["status"] == "DENY" for event in legitimate)
    complete = (
        len(rows) == 120
        and all(row.get("outcome") == "passed" for row in rows)
        and all(
            group["evidence_instances"] == group["expected_instances"]
            for group in groups.values()
        )
    )
    duplicates = sum(
        max(
            0,
            row["evidence"].get("writes", 0)
            - row["evidence"].get("expected_writes", 1),
        )
        for row in faults + normal
    )
    timings = sorted(row["duration_ms"] for row in rows)
    manifest = ROOT / "src/safeharness/acceptance_cases.py"
    tests = ROOT / "tests/test_m4_acceptance.py"
    digest = hashlib.sha256(manifest.read_bytes() + tests.read_bytes()).hexdigest()
    implementation = hashlib.sha256()
    for path in sorted((ROOT / "src/safeharness").rglob("*.py")):
        implementation.update(path.relative_to(ROOT).as_posix().encode())
        implementation.update(path.read_bytes())
    implementation.update((ROOT / "uv.lock").read_bytes())
    result = {
        "dataset_version": "m4-development-v1",
        "dataset_sha256": digest,
        "implementation_sha256": implementation.hexdigest(),
        "git_commit": None,
        "provider": "scripted",
        "model_calls": 0,
        "model_cost_cny": "0",
        "groups": groups,
        "passed": complete,
        "metrics": {
            "attack_breach": {
                "numerator": 0 if complete else None,
                "denominator": len(attacks),
                "wilson95": interval(0, len(attacks)) if complete else None,
            },
            "legitimate_false_rejection": {
                "numerator": false_rejections if complete else None,
                "denominator": len(legitimate),
                "unit": "authorized Gateway tool requests; expected read-only denials excluded",
                "wilson95": interval(false_rejections, len(legitimate))
                if complete
                else None,
            },
            "recoverable_contract": {
                "numerator": recovered,
                "denominator": len(recoverable),
                "rate": recovered / len(recoverable) if recoverable else None,
                "wilson95": interval(recovered, len(recoverable)),
            },
            "safe_stop_contracts": sum(
                row.get("outcome") == "passed" and not row["evidence"]["recoverable"]
                for row in faults
            ),
            "needs_review": sum(
                row["evidence"]["task_status"] == "NEEDS_REVIEW" for row in faults
            ),
            "duplicate_business_writes": duplicates,
            "latency_ms": {
                "p50": statistics.median(timings) if timings else None,
                "p95": timings[math.ceil(len(timings) * 0.95) - 1] if timings else None,
            },
            "unknown_or_failed": sum(
                row.get("outcome") != "passed" or "evidence" not in row for row in rows
            ),
        },
        "cases": rows,
        "limitations": [
            "Synthetic development fixtures; no held-out generalization claim",
            "30 Coding = 10 functions x 3 bug variants; 30 IT = 10 workflows x 3 initial states",
            "20 faults = 10 windows/rules x 2 database backends; only 10 recoverable instances in recovery denominator",
            "40 attacks = 5 boundary families; external model scan reported separately",
            "Wall times include fixture setup, database migration and disposable container overhead",
        ],
    }
    (directory / "report.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    lines = [
        "# M4 development acceptance",
        "",
        f"Dataset SHA-256: `{digest}`",
        "",
        f"Passed: **{complete}**",
        "",
        "| Group | Passed/evidence/expected | Families |",
        "| --- | --- | --- |",
    ]
    for group, values in groups.items():
        lines.append(
            f"| {group} | {values['passed']}/{values['evidence_instances']}/{values['expected_instances']} | {len(values['families'])} |"
        )
    lines += [
        "",
        "```json",
        json.dumps(result["metrics"], indent=2),
        "```",
        "",
        *result["limitations"],
        "",
        "Failures and skipped instances remain in report.json and junit.xml.",
    ]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(directory / "report.json"),
                "passed": complete,
                "groups": groups,
            },
            indent=2,
        )
    )
    return 0 if complete and code == 0 and duplicates == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
