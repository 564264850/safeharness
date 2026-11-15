"""Run native and real local MCP stdio tools through one unchanged Harness."""

import json
from pathlib import Path
from uuid import uuid4

from safeharness.agent import StatelessAgent
from safeharness.applications import ApplicationRegistry
from safeharness.audit import AuditLog
from safeharness.document_app import document_application
from safeharness.execution import ToolGateway, ToolRequest
from safeharness.harness import Harness
from safeharness.invocations import InvocationLedger
from safeharness.runtime import FinalAnswer
from safeharness.task_store import TaskStore


def main():
    directory = Path("artifacts/application-demos") / uuid4().hex
    directory.mkdir(parents=True)
    store = TaskStore(directory / "tasks.sqlite3")
    store.initialize()
    task = store.create()
    registry = ApplicationRegistry()
    registry.register(document_application())
    app = registry.get("ticket-docs")
    gateway = ToolGateway(
        {"IT-001": "open"},
        frozenset({"IT-001"}),
        application=app,
        audit=AuditLog(store.database, task.task_id),
    )

    def planner(results):
        if not results:
            return ToolRequest("read_ticket", {"ticket_id": "IT-001"})
        if len(results) == 1:
            return ToolRequest("kb_read_document", {"document_id": "KB-001"})
        return FinalAnswer("Inspect the deployment log. Source: synthetic://kb/KB-001.")

    harness = Harness(
        store,
        model=StatelessAgent(planner),
        execute=gateway.execute,
        verify=app.verify,
        configuration=app.configuration,
        max_steps=4,
    )
    result = harness.run(task)
    evidence = InvocationLedger(store.database, task.task_id).snapshot()
    report = {
        "task_id": str(task.task_id),
        "status": result.task.status.value,
        "application": app.configuration,
        "application_sha256": app.fingerprint,
        "mcp_transport": "actual-local-stdio",
        "model": "scripted",
        "business": "synthetic",
        "calls": evidence["calls"],
        "attempts": evidence["attempts"],
    }
    (directory / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(directory.resolve() / "report.json"),
            }
        )
    )
    return 0 if report["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
