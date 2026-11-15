"""Local authenticated HTTP control plane using the existing Runtime and Gateway."""

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from .approval import ApprovalActor, ApprovalOutcome
from .audit import AuditError, AuditLog
from .budget import BudgetLedger
from .checkpoints import CheckpointConflict, CheckpointSession
from .coding import CodingWorkspace
from .coding_demo import fixture, scripted_coding, verify_coding
from .contracts import InvocationConflictError, SubmissionConflictError
from .execution import (
    CODING_POLICY_VERSION,
    TICKET_POLICY_VERSION,
    ToolGateway,
    ToolRequest,
)
from .failures import RuntimePersistenceError
from .harness import Harness
from .invocations import InvocationLedger
from .provider import (
    ENDPOINT,
    MODEL,
    DeepSeekAdapter,
    DeepSeekClient,
    ProviderError,
    load_credentials,
)
from .runtime import FinalAnswer
from .scheduling import CURRENT_CLAIM, require_claim
from .task import TaskStatus
from .task_store import ApprovalConflictError, ExecutionConflictError, TaskConflictError
from .ticket_store import EvidenceUnavailable
from .trace import TraceLog


@dataclass(frozen=True)
class Principal:
    subject: str
    token_sha256: str
    roles: frozenset[str]
    readable_tickets: frozenset[str] = frozenset({"IT-001"})
    writable_tickets: frozenset[str] = frozenset()


def initialize_auth(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    auth_path, credentials_path = (
        directory / "service-auth.json",
        directory / "service-tokens.json",
    )
    if auth_path.exists() or credentials_path.exists():
        if not auth_path.exists() or not credentials_path.exists():
            raise ValueError("Incomplete existing local auth configuration")
        return {
            "auth_file": str(auth_path),
            "tokens_file": str(credentials_path),
            "reused": True,
        }
    records, tokens = [], {}
    for subject, roles, write in [
        ("alice", ["requester"], True),
        ("bob", ["requester"], True),
        ("reviewer", ["approver"], True),
        ("auditor", ["auditor"], False),
        ("scanner", ["scanner"], False),
        ("scanner-model", ["model_proxy"], False),
    ]:
        token = secrets.token_urlsafe(32)
        tokens[subject] = token
        records.append(
            {
                "subject": subject,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "roles": roles,
                "readable_tickets": ["IT-001"],
                "writable_tickets": ["IT-001"] if write else [],
            }
        )
    auth_path.write_text(json.dumps({"actors": records}, indent=2), encoding="utf-8")
    credentials_path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    auth_path.chmod(0o600)
    credentials_path.chmod(0o600)
    return {
        "auth_file": str(auth_path),
        "tokens_file": str(credentials_path),
        "reused": False,
    }


def load_principals(path: Path):
    records = json.loads(path.read_text(encoding="utf-8"))["actors"]
    principals = [
        Principal(
            record["subject"],
            record["token_sha256"],
            frozenset(record["roles"]),
            frozenset(record["readable_tickets"]),
            frozenset(record["writable_tickets"]),
        )
        for record in records
    ]
    if len({principal.subject for principal in principals}) != len(principals):
        raise ValueError("Duplicate configured subjects")
    return principals


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scenario: Literal[
        "ticket_read", "ticket_update", "ticket_update_read", "coding_fix"
    ]
    provider: Literal["scripted", "deepseek"] = "scripted"
    ticket_id: str = Field(default="IT-001", pattern=r"^IT-[0-9]{3}$")
    status: Literal["open", "in_progress", "resolved"] = "resolved"
    execution_mode: Literal["manual", "queued"] = "manual"

    @model_validator(mode="after")
    def supported_queue(self):
        if self.execution_mode == "queued" and (
            self.scenario not in {"ticket_read", "ticket_update", "ticket_update_read"}
        ):
            raise ValueError("Queued mode supports ticket scenarios only")
        return self


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "reject"]
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContinueInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recover_abandoned: bool = False
    expected_generation: int | None = Field(default=None, ge=1)


class BodyLimit:
    def __init__(self, app, limit=16384):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        try:
            while True:
                message = await asyncio.wait_for(receive(), timeout=5)
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.limit:
                    return await JSONResponse(
                        {"detail": "request_too_large"}, status_code=413
                    )(scope, receive, send)
                if not message.get("more_body", False):
                    break
        except TimeoutError:
            return await JSONResponse({"detail": "request_timeout"}, status_code=408)(
                scope, receive, send
            )
        first = True

        async def replay():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class TaskService:
    def __init__(
        self,
        store,
        business,
        principals,
        *,
        allow_deepseek=False,
        credentials_path=Path("../pico-api.txt"),
        budget_path=Path("artifacts/provider-budget.sqlite3"),
        budget_id="service-deepseek-v2",
        budget_cny="5.00",
        max_provider_calls=80,
        max_pending=1000,
    ):
        if type(max_pending) is not int or not 1 <= max_pending <= 10000:
            raise ValueError("Queue capacity must be between 1 and 10000")
        self.store, self.business = store, business
        self.principals = {principal.subject: principal for principal in principals}
        self.allow_deepseek, self.credentials_path, self.budget_path = (
            allow_deepseek,
            credentials_path,
            budget_path,
        )
        self.inflight = threading.BoundedSemaphore(2)
        self.max_pending = max_pending
        self.provider_budget = {
            "budget_id": budget_id,
            "limit_cny": budget_cny,
            "max_calls": max_provider_calls,
        }
        self.provider_configuration = {
            "provider": "deepseek",
            "endpoint": ENDPOINT,
            "model": MODEL,
            "policy_version": "queued-deepseek-v1",
            "max_output_tokens": 768,
            "timeout_seconds": 20,
            "max_attempts": 2,
            "pricing_version": BudgetLedger.PRICING_VERSION,
            "budget": self.provider_budget,
            "budget_store_sha256": hashlib.sha256(
                str(Path(budget_path).resolve()).encode("utf-8")
            ).hexdigest(),
        }
        self.provider_ledger = (
            BudgetLedger(self.budget_path, **self.provider_budget)
            if self.allow_deepseek
            else None
        )
        self.business.initialize()

    def owned(self, task_id, principal, *, review=False):
        metadata = self.store.service_task(task_id)
        if metadata is None or (
            metadata["owner_id"] != principal.subject
            and not (review and principal.roles & {"approver", "auditor"})
        ):
            raise HTTPException(404, "task_not_found")
        return metadata

    def snapshot(self, task_id):
        task, scheduling = self.store.task_with_scheduling(task_id)
        result = {
            "task_id": str(task.task_id),
            "task_status": task.status.value,
            "revision": task.revision,
        }
        if failure := self.store.get_runtime_failure(task_id):
            result["last_runtime_error"] = failure
        if scheduling is not None:
            result["scheduling"] = scheduling
        return result

    def create(self, request, principal, idempotency_key=None):
        if "requester" not in principal.roles:
            raise HTTPException(403, "requester_role_required")
        if request.provider == "deepseek" and not self.allow_deepseek:
            raise HTTPException(403, "paid_provider_disabled")
        from .operations import QueueCapacityError

        try:
            return self.store.submit(
                principal.subject,
                request.model_dump(),
                idempotency_key,
                max_pending=self.max_pending,
            )
        except QueueCapacityError:
            raise HTTPException(429, "queue_capacity_reached") from None

    def run(
        self,
        task_id,
        principal,
        *,
        pause_after=None,
        resume=False,
        recover_abandoned=False,
        expected_generation=None,
    ):
        metadata = self.owned(task_id, principal)
        if "requester" not in principal.roles:
            raise HTTPException(403, "requester_role_required")
        task = self.store.get(task_id)
        if self.store.scheduling(task_id) is not None:
            if CURRENT_CLAIM.get() is None:
                raise HTTPException(409, "queued_task_requires_worker")
            with self.store._engine.connect() as connection:
                require_claim(connection, task_id)
        claim = CURRENT_CLAIM.get()
        initialize_running = claim is not None and claim.recovery_mode == "initialize"
        if not resume and task.status is not (
            TaskStatus.RUNNING if initialize_running else TaskStatus.CREATED
        ):
            raise HTTPException(409, "task_already_started")
        if not self.inflight.acquire(blocking=False):
            raise HTTPException(429, "worker_capacity_reached")
        try:
            request = TaskInput.model_validate(metadata["request"])
            status, _ = self.business.ticket()
            last_check, last_cancelled = 0, False

            def cancelled():
                nonlocal last_check, last_cancelled
                if time.monotonic() - last_check >= 0.2:
                    last_cancelled = (
                        self.store.get(task_id).status is TaskStatus.CANCELLED
                    )
                    last_check = time.monotonic()
                return last_cancelled

            workspace = (
                CodingWorkspace(fixture(), task_id, cancel_check=cancelled)
                if request.scenario == "coding_fix"
                else None
            )
            gateway = ToolGateway(
                {"IT-001": status},
                principal.readable_tickets,
                writable_tickets=principal.writable_tickets,
                coding=workspace,
                readable_files=frozenset(fixture()) if workspace else frozenset(),
                writable_files=frozenset({"solution.py"}) if workspace else frozenset(),
                test_paths=frozenset({"tests/test_solution.py"})
                if workspace
                else frozenset(),
                audit=AuditLog(self.store.database, task_id),
                ticket_reader=lambda ticket_id: (
                    self.business.ticket()[0] if ticket_id == "IT-001" else None
                ),
            )

            def scripted(results):
                if not results:
                    return (
                        ToolRequest(
                            "update_ticket_status",
                            {"ticket_id": request.ticket_id, "status": request.status},
                        )
                        if request.scenario in {"ticket_update", "ticket_update_read"}
                        else ToolRequest(
                            "read_ticket", {"ticket_id": request.ticket_id}
                        )
                    )
                if request.scenario == "ticket_update_read" and len(results) == 1:
                    return ToolRequest("read_ticket", {"ticket_id": request.ticket_id})
                return FinalAnswer(results[-1].value or results[-1].code)

            model = scripted_coding() if workspace else scripted
            if request.provider == "deepseek":
                if not self.allow_deepseek:
                    raise HTTPException(403, "paid_provider_disabled")
                client = DeepSeekClient(
                    load_credentials(self.credentials_path),
                    self.provider_ledger,
                )
                instruction = (
                    "Fix clamp in solution.py, including ValueError for reversed bounds, then run tests/test_solution.py."
                    if workspace
                    else f"{'Request setting' if request.scenario in {'ticket_update', 'ticket_update_read'} else 'Read'} ticket {request.ticket_id}"
                    + (
                        f" to {request.status}. Human approval is required."
                        if request.scenario in {"ticket_update", "ticket_update_read"}
                        else "."
                    )
                )
                if request.scenario == "ticket_update_read":
                    instruction += " After approval and execution, read the ticket and report the observed status."
                model = DeepSeekAdapter(
                    client,
                    instruction=instruction,
                    environment="coding" if workspace else "ticket",
                    scope={
                        "readable_tickets": sorted(principal.readable_tickets),
                        "writable_tickets": sorted(principal.writable_tickets),
                        "writable_files": ["solution.py"] if workspace else [],
                    },
                    task_id=task_id,
                    trace=TraceLog(self.store.database, task_id),
                    deadline=time.monotonic() + 120,
                )

            def verify(run):
                if workspace:
                    return verify_coding(workspace, run, gateway).passed
                if request.scenario in {"ticket_update", "ticket_update_read"}:
                    updated = any(
                        r.name == "update_ticket_status"
                        and r.status == "ok"
                        and r.code == "approved_tool_executed"
                        and json.loads(r.value).get("status") == request.status
                        for r in run.tool_results
                    )
                    read = any(
                        r.name == "read_ticket"
                        and r.status == "ok"
                        and r.value == request.status
                        for r in run.tool_results
                    )
                    return (
                        updated
                        and self.business.ticket()[0] == request.status
                        and (read or request.scenario == "ticket_update")
                    )
                return (
                    request.scenario == "ticket_read"
                    and request.ticket_id == "IT-001"
                    and request.ticket_id in principal.readable_tickets
                ) and any(
                    result.name == "read_ticket"
                    and result.status == "ok"
                    and result.value == status
                    for result in run.tool_results
                )

            try:
                harness = Harness(
                    self.store,
                    model=model,
                    execute=gateway.execute,
                    verify=verify,
                    workspace=workspace,
                    stateless_model=request.provider == "scripted",
                    approval_workspace=self.business,
                    approval_mode=(
                        CheckpointSession.metadata(self.store, task_id) or {}
                    ).get("approval_mode", "agent"),
                    max_steps=8,
                    configuration={
                        "service": "agent-v1",
                        "request": request.model_dump(),
                        "subject": principal.subject,
                        "readable_tickets": sorted(principal.readable_tickets),
                        "writable_tickets": sorted(principal.writable_tickets),
                        "ticket_policy": TICKET_POLICY_VERSION,
                        "coding_policy": CODING_POLICY_VERSION,
                        "verifier": "clamp-contract-v1/ticket-v1",
                        **(
                            {"provider": self.provider_configuration}
                            if request.provider == "deepseek"
                            else {}
                        ),
                    },
                )
                if resume:
                    harness.resume(
                        task_id,
                        pause_after=pause_after,
                        recover_abandoned=recover_abandoned,
                        expected_generation=expected_generation,
                    )
                else:
                    harness.run(
                        task,
                        pause_after=pause_after,
                        initialize_running=initialize_running,
                    )
            except (TaskConflictError, AuditError):
                if self.store.get(task_id).status is not TaskStatus.CANCELLED:
                    raise
            return self.snapshot(task_id)
        finally:
            self.inflight.release()

    def decide(self, approval_id, decision, principal):
        if "approver" not in principal.roles:
            raise HTTPException(403, "approver_role_required")
        task_id = self.store.approval_task_id(approval_id)
        if task_id is None:
            raise HTTPException(404, "approval_not_found")
        metadata = self.owned(task_id, principal, review=True)
        if metadata["owner_id"] == principal.subject:
            raise HTTPException(403, "self_approval_forbidden")
        owner = self.principals.get(metadata["owner_id"])
        ticket_id = metadata["request"]["ticket_id"]
        if (
            owner is None
            or ticket_id not in owner.writable_tickets
            or ticket_id not in principal.writable_tickets
        ):
            raise HTTPException(403, "approval_scope_denied")
        return self.store.decide_approval(
            approval_id,
            ApprovalOutcome.APPROVED
            if decision.decision == "approve"
            else ApprovalOutcome.REJECTED,
            decision.arguments_sha256,
            actor=ApprovalActor(principal.subject, can_approve=True),
        )


def create_app(store, business, principals, *, scan_profile=None, **settings):
    store.initialize()
    service = TaskService(store, business, principals, **settings)
    app = FastAPI(title="SafeHarness", version="0.1.0", debug=False)
    app.add_middleware(BodyLimit, limit=65536 if scan_profile else 16384)
    app.state.service = service
    bearer = HTTPBearer(auto_error=False)

    def authenticated(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                401, "bearer_token_required", headers={"WWW-Authenticate": "Bearer"}
            )
        digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
        for principal in principals:
            if hmac.compare_digest(digest, principal.token_sha256):
                return principal
        raise HTTPException(
            401, "invalid_bearer_token", headers={"WWW-Authenticate": "Bearer"}
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, exc):
        return JSONResponse(
            {
                "detail": [
                    {"loc": error["loc"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
            status_code=422,
        )

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(_, exc):
        return JSONResponse({"detail": "storage_unavailable"}, status_code=503)

    @app.exception_handler(RuntimePersistenceError)
    async def runtime_persistence_error(_, exc):
        return JSONResponse(
            {
                "detail": exc.code,
                "task_id": str(exc.task_id),
                "recovery_action": "inspect_task_before_retry",
            },
            status_code=503,
        )

    @app.exception_handler(ProviderError)
    async def provider_setup_error(_, exc):
        return JSONResponse({"detail": exc.code}, status_code=503)

    for exception in (
        TaskConflictError,
        ApprovalConflictError,
        ExecutionConflictError,
        AuditError,
        EvidenceUnavailable,
        SubmissionConflictError,
        InvocationConflictError,
        CheckpointConflict,
    ):

        async def conflict(_, exc):
            return JSONResponse(
                {
                    "detail": exc.code
                    if isinstance(exc, CheckpointConflict)
                    else type(exc).__name__
                },
                status_code=409,
            )

        app.add_exception_handler(exception, conflict)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "safeharness"}

    @app.get("/ready")
    def ready():
        from .database import execute

        with store._engine.connect() as c:
            execute(c, "SELECT 1").scalar_one()
        return {"ready": True, "dependency": "database"}

    @app.get("/v1/operations")
    def operations(principal: Annotated[Principal, Depends(authenticated)]):
        if "auditor" not in principal.roles:
            raise HTTPException(403, "auditor_role_required")
        from .operations import metrics

        return metrics(store)

    @app.post("/v1/tasks", status_code=201)
    def submit(
        request: TaskInput,
        principal: Annotated[Principal, Depends(authenticated)],
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
            ),
        ] = None,
    ):
        task, replayed = service.create(request, principal, idempotency_key)
        return JSONResponse(
            service.snapshot(task.task_id),
            status_code=200 if replayed else 201,
            headers={"Idempotency-Replayed": str(replayed).lower()}
            if idempotency_key is not None
            else {},
        )

    @app.post("/v1/tasks/{task_id}/run")
    def run(
        task_id: UUID,
        principal: Annotated[Principal, Depends(authenticated)],
        pause_after: int | None = None,
    ):
        if pause_after is not None and (pause_after < 1 or pause_after > 64):
            raise HTTPException(422, "invalid_pause_after")
        return service.run(task_id, principal, pause_after=pause_after)

    @app.post("/v1/tasks/{task_id}/continue")
    def continue_agent(
        task_id: UUID,
        request: ContinueInput,
        principal: Annotated[Principal, Depends(authenticated)],
    ):
        return service.run(
            task_id,
            principal,
            resume=True,
            recover_abandoned=request.recover_abandoned,
            expected_generation=request.expected_generation,
        )

    @app.get("/v1/tasks/{task_id}/checkpoints")
    def checkpoints(
        task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]
    ):
        service.owned(task_id, principal, review=True)
        result = CheckpointSession.metadata(store, task_id)
        if result is None:
            raise HTTPException(404, "checkpoint_session_not_found")
        return result

    @app.get("/v1/tasks/{task_id}")
    def task(task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]):
        service.owned(task_id, principal, review=True)
        return service.snapshot(task_id)

    @app.get("/v1/tasks/{task_id}/approval")
    def approval(
        task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]
    ):
        service.owned(task_id, principal, review=True)
        record = store.get_approval(task_id)
        if record is None:
            raise HTTPException(404, "approval_not_found")
        return {
            "approval_id": str(record.request.approval_id),
            "task_id": str(task_id),
            "arguments": json.loads(record.request.intent.canonical_arguments),
            "arguments_sha256": record.request.intent.arguments_sha256,
            "approval_status": record.decision.outcome.value
            if record.decision
            else "PENDING",
            "decided_by": record.decision.decided_by if record.decision else None,
        }

    @app.post("/v1/approvals/{approval_id}/decision")
    def decide(
        approval_id: UUID,
        request: DecisionInput,
        principal: Annotated[Principal, Depends(authenticated)],
    ):
        result = service.decide(approval_id, request, principal)
        return {
            "approval_status": result.outcome.value,
            "decided_by": result.decided_by,
        }

    @app.post("/v1/tasks/{task_id}/resume")
    def resume(task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]):
        metadata = service.owned(task_id, principal)
        if (
            "requester" not in principal.roles
            or metadata["request"].get("ticket_id") not in principal.writable_tickets
        ):
            raise HTTPException(403, "write_scope_denied")
        session = CheckpointSession.metadata(store, task_id)
        if (
            session
            and session["approval_mode"] == "agent"
            and store.get(task_id).status
            in {TaskStatus.WAITING_APPROVAL, TaskStatus.RUNNING}
        ):
            service.run(task_id, principal, resume=True)
        else:
            store.resume(task_id, business)
        return service.snapshot(task_id)

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel(task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]):
        service.owned(task_id, principal)
        store.cancel(task_id, business)
        return service.snapshot(task_id)

    @app.get("/v1/tasks/{task_id}/trace")
    def trace(task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]):
        service.owned(task_id, principal, review=True)
        return TraceLog(store.database, task_id).read()

    @app.get("/v1/tasks/{task_id}/invocations")
    def invocations(
        task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]
    ):
        service.owned(task_id, principal, review=True)
        return InvocationLedger(store.database, task_id).snapshot()

    @app.get("/v1/tasks/{task_id}/trace.jsonl")
    def trace_jsonl(
        task_id: UUID, principal: Annotated[Principal, Depends(authenticated)]
    ):
        service.owned(task_id, principal, review=True)
        return Response(
            TraceLog(store.database, task_id).jsonl(), media_type="application/x-ndjson"
        )

    @app.get("/v1/ticket")
    def ticket(principal: Annotated[Principal, Depends(authenticated)]):
        if "IT-001" not in principal.readable_tickets:
            raise HTTPException(403, "read_scope_denied")
        status, revision = business.ticket()
        return {"ticket_id": "IT-001", "status": status, "write_count": revision}

    if scan_profile:
        from .scanning_service import register_scanning

        register_scanning(app, service, authenticated, profile=scan_profile)
    return app
