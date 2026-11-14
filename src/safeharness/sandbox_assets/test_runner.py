"""Development pytest telemetry. Never an authority for final task acceptance."""

import contextlib
import json
import sys

import pytest


class Session:
    def __init__(self):
        self.collected = 0
        self.passed = set()
        self.failed = set()
        self.skipped = set()
        self.finished = False

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        if report.skipped:
            self.skipped.add(report.nodeid)
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        self.finished = True


if __name__ == "__main__":
    session = Session()
    with contextlib.redirect_stdout(sys.stderr):
        code = int(pytest.main(sys.argv[1:], plugins=[session]))
    print(
        json.dumps(
            {
                "version": 1,
                "exit_code": code,
                "completed": session.finished,
                "collected": session.collected,
                "passed": len(session.passed),
                "failed": len(session.failed),
                "skipped": len(session.skipped),
                "evidence_level": "development_only",
            }
        )
    )
    sys.exit(code)
