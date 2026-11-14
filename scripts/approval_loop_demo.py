"""Persistent approval -> Agent continuation demo; scripted unless DeepSeek is explicit."""

import argparse
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

from safeharness.agent import ToolCallingAgent
from safeharness.approval import ApprovalActor, ApprovalOutcome
from safeharness.audit import AuditLog
from safeharness.budget import BudgetLedger
from safeharness.checkpoints import CheckpointSession
from safeharness.execution import ToolGateway
from safeharness.harness import Harness
from safeharness.invocations import InvocationLedger
from safeharness.provider import DeepSeekAdapter, DeepSeekClient, load_credentials
from safeharness.task_store import TaskStore
from safeharness.ticket_store import TicketStore
from safeharness.trace import TraceLog

ROOT = Path(__file__).resolve().parents[1]
BUDGET_ID = "harness-approval-r4-v1"
INSTRUCTION = (
    "Use the tools to update IT-001 to in_progress, then read its observed status. "
    "Next update it to resolved and read its observed status again. Each write needs human approval. "
    "The host will pause for review and later return the execution result. "
    "Do not claim success or finish until both writes have executed and both reads confirm the requested states."
)


class ScriptedClient:
    def complete(self, messages, **kwargs):
        if any(m["role"] == "tool" for m in messages):
            return {
                "role": "assistant",
                "content": "Both approved updates and subsequent reads are complete.",
            }
        actions = [
            ("update_ticket_status", {"ticket_id": "IT-001", "status": "in_progress"}),
            ("read_ticket", {"ticket_id": "IT-001"}),
            ("update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}),
            ("read_ticket", {"ticket_id": "IT-001"}),
        ]
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": str(i),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for i, (name, args) in enumerate(actions)
            ],
        }


def components(store, business, task_id, provider):
    ledger = (
        BudgetLedger(
            ROOT / "artifacts/provider-budget.sqlite3",
            budget_id=BUDGET_ID,
            limit_cny="5.00",
            max_calls=80,
        )
        if provider == "deepseek"
        else None
    )
    client = (
        DeepSeekClient(
            load_credentials(ROOT.parent / "pico-api.txt"),
            ledger,
            timeout=60,
            max_tokens=1536,
            max_attempts=2,
        )
        if ledger
        else ScriptedClient()
    )
    agent_class = DeepSeekAdapter if ledger else ToolCallingAgent
    model = agent_class(
        client,
        instruction=INSTRUCTION,
        environment="ticket",
        scope={"read": ["IT-001"], "write": ["IT-001"]},
        task_id=task_id,
        trace=TraceLog(store.database, task_id),
        deadline=time.monotonic() + 600,
    )
    gateway = ToolGateway(
        allowed_tickets=frozenset({"IT-001"}),
        writable_tickets=frozenset({"IT-001"}),
        ticket_reader=lambda _: business.ticket()[0],
        audit=AuditLog(store.database, task_id),
    )
    verification = {}

    def verify(run):
        observed = [
            ("write", json.loads(r.value)["status"])
            if r.name == "update_ticket_status" and r.code == "approved_tool_executed"
            else ("read", r.value)
            for r in run.tool_results
            if r.status == "ok"
        ]
        expected = iter(
            [
                ("write", "in_progress"),
                ("read", "in_progress"),
                ("write", "resolved"),
                ("read", "resolved"),
            ]
        )
        required = next(expected, None)
        for item in observed:
            if item == required:
                required = next(expected, None)
        passed = required is None and business.ticket() == ("resolved", 2)
        verification.update({"called": True, "passed": passed, "observed": observed})
        return passed

    return (
        Harness(
            store,
            model=model,
            execute=gateway.execute,
            verify=verify,
            approval_workspace=business,
            configuration={
                "demo": "approval-loop-v1",
                "provider": provider,
                "budget_id": BUDGET_ID if ledger else None,
            },
            max_steps=12,
            timeout_seconds=600,
        ),
        ledger,
        verification,
    )


def main(args):
    folder = (
        ROOT / "artifacts/approval-loop-demos" / (args.continue_demo or str(uuid4()))
    )
    if args.continue_demo:
        info = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    else:
        folder.mkdir(parents=True)
        info = {"provider": args.provider}
    store = TaskStore(folder / "control.sqlite3")
    store.initialize()
    business = TicketStore(store.database)
    business.initialize()
    if not args.continue_demo:
        info["task_id"] = str(store.create().task_id)
        (folder / "task.json").write_text(json.dumps(info), encoding="utf-8")
    task_id = UUID(info["task_id"])
    harness, ledger, verification = components(
        store, business, task_id, info["provider"]
    )
    done = (
        harness.resume(task_id)
        if args.continue_demo
        else harness.run(store.get(task_id))
    )
    approvals = []
    while done.task.status.value == "WAITING_APPROVAL":
        request = store.get_approval(task_id).request
        approvals.append(str(request.approval_id))
        if not args.auto_review_for_test:
            print(
                json.dumps(
                    {
                        "task_status": "WAITING_APPROVAL",
                        "demo_id": folder.name,
                        "task_id": str(task_id),
                        "database": str(store.database),
                        "approval_id": str(request.approval_id),
                        "arguments": json.loads(request.intent.canonical_arguments),
                        "arguments_sha256": request.intent.arguments_sha256,
                        "next": "Use decide-approval, then --continue-demo with this demo_id.",
                    },
                    indent=2,
                )
            )
            return 0
        store.decide_approval(
            request.approval_id,
            ApprovalOutcome.APPROVED,
            request.intent.arguments_sha256,
            actor=ApprovalActor("synthetic-test-reviewer", can_approve=True),
        )
        harness, ledger, verification = components(
            store, business, task_id, info["provider"]
        )
        done = harness.resume(task_id)
    report = {
        "task_id": str(task_id),
        "task_status": done.task.status.value,
        "provider": info["provider"],
        "review_source": "synthetic_test_operator"
        if args.auto_review_for_test
        else "manual_operator",
        "approvals_in_this_invocation": approvals,
        "business": business.ticket(),
        "verification": verification,
        "error_code": done.run.error_code,
        "answer": done.run.answer,
        "usage": ledger.usage(task_id)
        if ledger
        else {"calls": 0, "cost_upper_cny": "0"},
        "invocations": InvocationLedger(store.database, task_id).snapshot(),
        "checkpoint": CheckpointSession.metadata(store, task_id),
    }
    path = folder / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "task_status": report["task_status"],
                "verification": verification,
                "usage": report["usage"],
                "report": str(path),
            },
            indent=2,
        )
    )
    return 0 if done.task.status.value == "SUCCEEDED" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", choices=["scripted", "deepseek"], default="scripted"
    )
    parser.add_argument("--continue-demo", type=lambda v: str(UUID(v)))
    parser.add_argument(
        "--auto-review-for-test",
        action="store_true",
        help="Trusted synthetic test reviewer, never a model tool",
    )
    raise SystemExit(main(parser.parse_args()))
