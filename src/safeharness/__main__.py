import argparse
import json
import os
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from .approval import ApprovalActor, ApprovalDecision, ApprovalOutcome
from .audit import AuditError, AuditLog
from .budget import BudgetError, BudgetLedger
from .checkpoints import CheckpointSession
from .coding_demo import read_report, run_coding_demo
from .dataset import load_dataset
from .evaluation import read_evaluation, run_evaluation
from .execution import TicketGateway, ToolRequest, ToolResult
from .failures import RuntimePersistenceError
from .harness import Harness
from .invocations import InvocationLedger
from .provider import DeepSeekClient, ProviderError, load_credentials
from .runtime import FinalAnswer, RunResult
from .sandbox import DockerSandbox, report_dict
from .task import TaskStatus
from .task_store import ApprovalConflictError, ExecutionConflictError, TaskStore
from .ticket_store import TICKET_ID, EvidenceUnavailable, TicketStore
from .trace import TraceLog


def scripted_model(results: tuple[ToolResult, ...]) -> ToolRequest | FinalAnswer:
    if not results:
        return ToolRequest("read_ticket", {"ticket_id": "IT-001"})
    result = results[-1]
    return FinalAnswer(f"Ticket IT-001: {result.value or result.code}")


def verify_demo(result: RunResult, *, expected_status: str = "open") -> bool:
    expected = ToolResult("read_ticket", "ok", "ticket_read", value=expected_status)
    return result.stop_reason == "model_finished" and result.tool_results == (expected,)


def scripted_update(_: tuple[ToolResult, ...]) -> ToolRequest:
    return ToolRequest(
        "update_ticket_status", {"ticket_id": "IT-001", "status": "resolved"}
    )


def run_demo(
    store: TaskStore, workspace: TicketStore, *, request_update: bool = False
) -> int:
    ticket_status, _ = store.read_ticket(workspace)
    tickets = {TICKET_ID: ticket_status}
    ticket_scope = frozenset(tickets)
    task = store.create()
    gateway = TicketGateway(
        tickets,
        ticket_scope,
        writable_tickets=ticket_scope if request_update else frozenset(),
        audit=AuditLog(store.database, task.task_id),
    )
    model = scripted_update if request_update else scripted_model
    task_result = Harness(
        store,
        model=model,
        execute=gateway.execute,
        verify=lambda result: verify_demo(result, expected_status=ticket_status),
        configuration={
            "demo": "ticket-v1",
            "ticket_status": ticket_status,
            "request_update": request_update,
        },
        approval_mode="legacy" if request_update else "agent",
        stateless_model=True,
        max_steps=2,
    ).run(task)
    result = task_result.run
    passed = task_result.task.status is TaskStatus.SUCCEEDED
    waiting = task_result.task.status is TaskStatus.WAITING_APPROVAL
    output = {
        "task_id": str(task_result.task.task_id),
        "task_status": task_result.task.status.value,
        "revision": task_result.task.revision,
        "stop_reason": result.stop_reason,
        "verification_passed": None if waiting else passed,
        "events": [
            {"tool": item.name, "status": item.status, "code": item.code}
            for item in result.tool_results
        ],
        "answer": result.answer,
    }
    if failure := store.get_runtime_failure(task_result.task.task_id):
        output["last_runtime_error"] = failure
    if task_result.approval_id is not None:
        output["approval_id"] = str(task_result.approval_id)
        output["ticket_status"] = tickets[TICKET_ID]
    print(json.dumps(output, indent=2))
    return 0 if passed or waiting else 1


def show_approval(store: TaskStore, task_id: UUID) -> int:
    record = store.get_approval(task_id)
    if record is None:
        print(f"No approval request for task {task_id}", file=sys.stderr)
        return 1

    approval = record.request
    output = {
        "approval_id": str(approval.approval_id),
        "task_id": str(approval.task_id),
        "approval_status": "PENDING",
        "tool_name": approval.intent.tool_name,
        "tool_version": approval.intent.tool_version,
        "arguments": json.loads(approval.intent.canonical_arguments),
        "arguments_sha256": approval.intent.arguments_sha256,
    }
    if record.decision is not None:
        output.update(decision_fields(record.decision))
    print(json.dumps(output, indent=2))
    return 0


def decision_fields(decision: ApprovalDecision) -> dict[str, str]:
    return {
        "approval_status": decision.outcome.value,
        "decided_by": decision.decided_by,
        "decided_at": decision.decided_at,
        "reviewed_arguments_sha256": decision.arguments_sha256,
    }


def decide_approval(
    store: TaskStore, approval_id: UUID, action: str, arguments_sha256: str
) -> int:
    try:
        # Only the local operator CLI supplies this capability.
        subject = (
            f"windows:{os.getlogin()}" if os.name == "nt" else f"uid:{os.geteuid()}"
        )
    except OSError:
        print("Could not determine the local operator identity", file=sys.stderr)
        return 1

    outcome = (
        ApprovalOutcome.APPROVED if action == "approve" else ApprovalOutcome.REJECTED
    )
    try:
        decision = store.decide_approval(
            approval_id,
            outcome,
            arguments_sha256,
            actor=ApprovalActor(subject=subject, can_approve=True),
        )
    except (ApprovalConflictError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"approval_id": str(approval_id), **decision_fields(decision)}, indent=2
        )
    )
    return 0


def run_execution_command(args: argparse.Namespace, store: TaskStore) -> int:
    workspace = TicketStore(args.db)
    if args.command == "show-ticket":
        status, revision = store.read_ticket(workspace)
        print(
            json.dumps(
                {"ticket_id": TICKET_ID, "status": status, "write_count": revision}
            )
        )
        return 0
    if args.command == "show-execution":
        execution = store.get_execution(args.task_id)
        if execution is None:
            raise ExecutionConflictError("No execution intent for task")
        print(json.dumps(execution, indent=2))
        return 0

    if not args.db.is_file():
        raise ExecutionConflictError("Task not found")
    store.initialize()
    if args.command == "cancel":
        task = store.cancel(args.task_id, workspace)
    else:
        task = store.resume(
            args.task_id,
            workspace,
            after_business_commit=(lambda: os._exit(77))
            if args.simulate_crash_after_write
            else None,
        )
    print(
        json.dumps(
            {
                "task_id": str(task.task_id),
                "task_status": task.status.value,
                "revision": task.revision,
                "execution": store.get_execution(task.task_id),
            },
            indent=2,
        )
    )
    return 0 if task.status in (TaskStatus.SUCCEEDED, TaskStatus.CANCELLED) else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except RuntimePersistenceError as exc:
        print(
            json.dumps(
                {
                    "error_code": exc.code,
                    "task_id": str(exc.task_id),
                    "recovery_action": "inspect_task_before_retry",
                }
            ),
            file=sys.stderr,
        )
        return 1


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="safeharness")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("artifacts/tasks.sqlite3"),
        help="SQLite database path (default: artifacts/tasks.sqlite3)",
    )
    commands = parser.add_subparsers(dest="command")
    show = commands.add_parser("show", help="Read the last committed task state")
    show.add_argument("task_id", type=UUID)
    commands.add_parser(
        "request-update", help="Request approval for a mock ticket update"
    )
    approval = commands.add_parser(
        "show-approval", help="Read a task's approval request and decision"
    )
    approval.add_argument("task_id", type=UUID)
    decide = commands.add_parser("decide-approval", help="Approve or reject a request")
    decide.add_argument("approval_id", type=UUID)
    decide.add_argument("decision", choices=("approve", "reject"))
    decide.add_argument("--arguments-sha256", required=True)
    resume = commands.add_parser(
        "resume", help="Execute or reconcile an approved mock ticket operation"
    )
    resume.add_argument("task_id", type=UUID)
    resume.add_argument(
        "--simulate-crash-after-write",
        action="store_true",
        help="Exit after the mock service commits and before the control-plane receipt",
    )
    commands.add_parser("cancel", help="Cancel before the business write").add_argument(
        "task_id", type=UUID
    )
    commands.add_parser(
        "show-execution", help="Read the execution intent or receipt"
    ).add_argument("task_id", type=UUID)
    commands.add_parser(
        "show-ticket", help="Read the persistent mock ticket and write count"
    )
    commands.add_parser("migrate", help="Apply database migrations")
    for name in ("service-init", "postgres-up", "serve", "worker", "recover-queue"):
        service = commands.add_parser(
            name, help="Prepare or run the authenticated local service"
        )
        service.add_argument(
            "--config-dir", type=Path, default=Path("artifacts/m4-private")
        )
        if name in {"serve", "worker", "recover-queue"}:
            service.add_argument(
                "--backend", choices=("sqlite", "postgres"), default="sqlite"
            )
        if name in {"serve", "worker"}:
            service.add_argument("--business-config", type=Path)
            service.add_argument(
                "--allow-deepseek",
                action="store_true",
                help="Enable the fixed DeepSeek provider for authorized ticket tasks",
            )
            service.add_argument(
                "--credentials", type=Path, default=Path("../pico-api.txt")
            )
            service.add_argument(
                "--budget-db",
                type=Path,
                default=Path("artifacts/provider-budget.sqlite3"),
            )
            service.add_argument("--budget-id", default="service-deepseek-v2")
            service.add_argument("--budget-cny", type=Decimal, default=Decimal("5.00"))
            service.add_argument("--max-provider-calls", type=int, default=80)
        if name == "worker":
            modes = service.add_mutually_exclusive_group()
            modes.add_argument("--once", action="store_true")
            modes.add_argument("--drain", action="store_true")
            service.add_argument("--poll-seconds", type=float, default=1.0)
            service.add_argument("--worker-id")
            service.add_argument("--lease-seconds", type=float, default=30)
            service.add_argument("--heartbeat-seconds", type=float, default=5)
        if name == "recover-queue":
            service.add_argument("--watch", action="store_true")
            service.add_argument("--interval-seconds", type=float, default=1)
            service.add_argument("--batch-size", type=int, default=100)
            service.add_argument("--max-recoveries", type=int, default=3)
        if name == "serve":
            service.add_argument("--port", type=int, default=18081)
            service.add_argument("--max-pending", type=int, default=1000)
            service.add_argument(
                "--scan-profile", choices=("hardened", "vulnerable-lab")
            )
    commands.add_parser("eval-list", help="List versioned evaluation contracts")
    counterexample = commands.add_parser(
        "judge-counterexample",
        help="Create an independently failing tool-choice probe without paid calls",
    )
    counterexample.add_argument(
        "--output-dir", type=Path, default=Path("artifacts/judge-probes")
    )
    judge = commands.add_parser(
        "judge", help="Apply the locked OpenJudge graders to observed tool calls"
    )
    judge.add_argument("report", type=Path)
    judge.add_argument("--provider", choices=("fixture", "deepseek"), default="fixture")
    judge.add_argument("--case", dest="case_ids", action="append")
    judge.add_argument("--credentials", type=Path, default=Path("../pico-api.txt"))
    judge.add_argument(
        "--budget-db", type=Path, default=Path("artifacts/provider-budget.sqlite3")
    )
    judge.add_argument("--budget-id", default="m3-validation-v1")
    judge.add_argument("--budget-cny", type=Decimal, default=Decimal("0.50"))
    commands.add_parser(
        "show-eval", help="Read a persisted public case result"
    ).add_argument("task_id", type=UUID)
    for name in ("eval", "run-case", "provider-check", "budget-status"):
        evaluation = commands.add_parser(
            name, help="Run or inspect the bounded M3 evaluation"
        )
        if name == "run-case":
            evaluation.add_argument("case_id")
        elif name == "eval":
            evaluation.add_argument("--case", dest="case_ids", action="append")
        evaluation.add_argument(
            "--provider", choices=("scripted", "deepseek"), default="scripted"
        )
        evaluation.add_argument(
            "--output-dir", type=Path, default=Path("artifacts/evaluations")
        )
        evaluation.add_argument(
            "--credentials", type=Path, default=Path("../pico-api.txt")
        )
        evaluation.add_argument(
            "--budget-db", type=Path, default=Path("artifacts/provider-budget.sqlite3")
        )
        evaluation.add_argument("--budget-id", default="m3-validation-v1")
        evaluation.add_argument("--budget-cny", type=Decimal, default=Decimal("0.50"))
    commands.add_parser(
        "show-trace", help="Read the versioned public Trace"
    ).add_argument("task_id", type=UUID)
    commands.add_parser(
        "show-invocations",
        help="Read task runs, logical calls, attempts and artifact metadata",
    ).add_argument("task_id", type=UUID)
    commands.add_parser(
        "show-checkpoints", help="Read checkpoint metadata without private content"
    ).add_argument("task_id", type=UUID)
    export = commands.add_parser(
        "export-trace", help="Export content-minimized Trace as JSONL"
    )
    export.add_argument("task_id", type=UUID)
    export.add_argument("--output", type=Path)
    coding_demo = commands.add_parser(
        "coding-demo", help="Repair and independently verify a sandboxed Coding task"
    )
    coding_demo.add_argument(
        "--skip-fix",
        action="store_true",
        help="Keep the bug to demonstrate failed verification",
    )
    coding_demo.add_argument(
        "--attempt-escape",
        action="store_true",
        help="Include two denied requests before the legitimate repair",
    )
    commands.add_parser(
        "show-coding", help="Read a persisted Coding report"
    ).add_argument("task_id", type=UUID)
    commands.add_parser("sandbox-check", help="Check the Linux worker prerequisites")
    commands.add_parser("sandbox-build", help="Build the fixed worker image")
    commands.add_parser(
        "sandbox-smoke", help="Verify isolation in a temporary container"
    )
    commands.add_parser(
        "show-events", help="Read persisted Gateway invocation events"
    ).add_argument("task_id", type=UUID)
    args = parser.parse_args(argv)
    if args.command in {
        "service-init",
        "postgres-up",
        "serve",
        "worker",
        "recover-queue",
    }:
        from .infrastructure import postgres_up, postgres_url
        from .service import create_app, initialize_auth, load_principals

        if args.command == "postgres-up":
            print(json.dumps(postgres_up(args.config_dir), indent=2))
            return 0
        if args.command == "service-init":
            print(json.dumps(initialize_auth(args.config_dir), indent=2))
            return 0
        import uvicorn

        from .pg_ticket_store import PostgresTicketStore

        location = (
            postgres_url(args.config_dir)
            if args.backend == "postgres"
            else args.config_dir / "tasks.sqlite3"
        )
        service_store = TaskStore(location)
        if args.command == "recover-queue":
            from sqlalchemy.exc import SQLAlchemyError

            from .recovery import run_recovery

            try:
                service_store.initialize()
                return run_recovery(
                    service_store,
                    watch=args.watch,
                    interval=args.interval_seconds,
                    batch_size=args.batch_size,
                    max_recoveries=args.max_recoveries,
                )
            except (SQLAlchemyError, ValueError):
                print(
                    json.dumps(
                        {
                            "error_code": "recovery_scan_error",
                            "recovery_action": "inspect_task_before_retry",
                        }
                    ),
                    file=sys.stderr,
                )
                return 1
        business = (
            PostgresTicketStore(location)
            if args.backend == "postgres"
            else TicketStore(location)
        )
        if args.business_config is not None:
            from .http_ticket import load_http_business

            business = load_http_business(args.business_config)
        if args.command == "worker":
            from sqlalchemy.exc import SQLAlchemyError

            from .scheduling import QueueClaimError
            from .worker import run_worker

            service_store.initialize()
            try:
                return run_worker(
                    service_store,
                    business,
                    load_principals(args.config_dir / "service-auth.json"),
                    once=args.once,
                    drain=args.drain,
                    poll_seconds=args.poll_seconds,
                    worker_id=args.worker_id,
                    lease_seconds=args.lease_seconds,
                    heartbeat_seconds=args.heartbeat_seconds,
                    task_service_settings={
                        "allow_deepseek": args.allow_deepseek,
                        "credentials_path": args.credentials,
                        "budget_path": args.budget_db,
                        "budget_id": args.budget_id,
                        "budget_cny": str(args.budget_cny),
                        "max_provider_calls": args.max_provider_calls,
                    },
                )
            except (SQLAlchemyError, QueueClaimError, ValueError):
                print(
                    json.dumps(
                        {
                            "error_code": "worker_execution_error",
                            "recovery_action": "inspect_task_before_retry",
                        }
                    ),
                    file=sys.stderr,
                )
                return 1
        app = create_app(
            service_store,
            business,
            load_principals(args.config_dir / "service-auth.json"),
            allow_deepseek=args.allow_deepseek,
            credentials_path=args.credentials,
            budget_path=args.budget_db,
            budget_id=args.budget_id,
            budget_cny=str(args.budget_cny),
            max_provider_calls=args.max_provider_calls,
            scan_profile=args.scan_profile,
            max_pending=args.max_pending,
        )
        uvicorn.run(
            app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False
        )
        return 0
    if args.command in {"judge", "judge-counterexample"}:
        from .judging import counterexample_report, grade_report

        try:
            if args.command == "judge-counterexample":
                result = counterexample_report(args.output_dir)
            else:
                client = (
                    DeepSeekClient(
                        load_credentials(args.credentials),
                        BudgetLedger(
                            args.budget_db,
                            budget_id=args.budget_id,
                            limit_cny=args.budget_cny,
                        ),
                    )
                    if args.provider == "deepseek"
                    else None
                )
                result = grade_report(
                    args.report, client=client, case_ids=args.case_ids
                )
            print(json.dumps(result, indent=2))
            return 0
        except (ValueError, ProviderError, BudgetError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "eval-list":
        dataset, digest = load_dataset()
        print(
            json.dumps(
                {
                    "dataset_version": dataset.dataset_version,
                    "dataset_sha256": digest,
                    "cases": [
                        {
                            "case_id": case.case_id,
                            "category": case.category,
                            "environment": case.environment,
                            "contract": case.contract,
                        }
                        for case in dataset.cases
                    ],
                },
                indent=2,
            )
        )
        return 0
    if args.command in {"eval", "run-case", "provider-check", "budget-status"}:
        try:
            ledger = (
                BudgetLedger(
                    args.budget_db, budget_id=args.budget_id, limit_cny=args.budget_cny
                )
                if args.provider == "deepseek"
                or args.command in {"provider-check", "budget-status"}
                else None
            )
            if args.command == "budget-status":
                print(json.dumps(ledger.usage(), indent=2))
                return 0
            client = (
                DeepSeekClient(load_credentials(args.credentials), ledger)
                if ledger
                else None
            )
            if args.command == "provider-check":
                print(
                    json.dumps({**client.probe(), "budget": ledger.usage()}, indent=2)
                )
                return 0
            selected = [args.case_id] if args.command == "run-case" else args.case_ids
            summary = run_evaluation(
                args.output_dir,
                provider=args.provider,
                client=client,
                case_ids=selected,
            )
            print(json.dumps(summary, indent=2))
            return 0
        except (BudgetError, ProviderError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command in {"sandbox-check", "sandbox-build", "sandbox-smoke"}:
        sandbox = DockerSandbox()
        if args.command == "sandbox-build":
            try:
                built = sandbox.build()
            except OSError:
                print(json.dumps({"ready": False, "code": "docker_cli_missing"}))
                return 1
            if built.returncode or built.timed_out or built.output_limited:
                print(json.dumps({"ready": False, "code": "worker_image_build_failed"}))
                print(built.stderr, file=sys.stderr)
                return 1
        report = (
            sandbox.smoke() if args.command == "sandbox-smoke" else sandbox.preflight()
        )
        print(json.dumps(report_dict(report), indent=2))
        return (
            0
            if (
                report.status == "passed"
                if args.command == "sandbox-smoke"
                else report.ready
            )
            else 1
        )
    store = TaskStore(args.db)
    if args.command == "show-eval":
        result = read_evaluation(args.db, args.task_id)
        if result is None:
            print("Evaluation result not found", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "show-checkpoints":
        if store.get(args.task_id) is None:
            print("Task not found", file=sys.stderr)
            return 1
        metadata = CheckpointSession.metadata(store, args.task_id)
        if metadata is None:
            print("Checkpoint session not found", file=sys.stderr)
            return 1
        print(json.dumps(metadata, indent=2))
        return 0
    if args.command == "show-invocations":
        if TaskStore(args.db).get(args.task_id) is None:
            print("Task not found", file=sys.stderr)
            return 1
        print(json.dumps(InvocationLedger(args.db, args.task_id).snapshot(), indent=2))
        return 0
    if args.command in {"show-trace", "export-trace"}:
        trace = TraceLog(args.db, args.task_id)
        if args.command == "show-trace":
            print(json.dumps(trace.read(), indent=2))
        elif args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(trace.jsonl(), encoding="utf-8")
        else:
            print(trace.jsonl(), end="")
        return 0
    if args.command == "show-coding":
        report = read_report(args.db, args.task_id)
        if report is None:
            print("Coding report not found", file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2))
        return 0
    if args.command == "show-events":
        print(json.dumps(AuditLog(args.db, args.task_id).read(), indent=2))
        return 0

    if args.command == "show-approval":
        return show_approval(store, args.task_id)

    if args.command in ("resume", "cancel", "show-execution", "show-ticket"):
        try:
            return run_execution_command(args, store)
        except (ExecutionConflictError, EvidenceUnavailable) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.command == "show":
        task = store.get(args.task_id)
        if task is None:
            print(f"Task {args.task_id} not found", file=sys.stderr)
            return 1
        output = {
            "task_id": str(task.task_id),
            "task_status": task.status.value,
            "revision": task.revision,
        }
        if failure := store.get_runtime_failure(task.task_id):
            output["last_runtime_error"] = failure
        print(json.dumps(output, indent=2))
        return 0

    if args.command == "decide-approval" and not args.db.is_file():
        print("Approval request not found", file=sys.stderr)
        return 1
    store.initialize()
    if args.command == "coding-demo":
        try:
            report = run_coding_demo(
                store, skip_fix=args.skip_fix, attempt_escape=args.attempt_escape
            )
        except AuditError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2))
        return 0 if report["task_status"] == "SUCCEEDED" else 1
    if args.command == "decide-approval":
        return decide_approval(
            store, args.approval_id, args.decision, args.arguments_sha256
        )
    if args.command == "migrate":
        return 0
    try:
        return run_demo(
            store, TicketStore(args.db), request_update=args.command == "request-update"
        )
    except EvidenceUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
