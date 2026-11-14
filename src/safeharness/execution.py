"""One schema, authorization and audit entrance for ticket and Coding tools."""

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .approval import ApprovalIntent
from .audit import AuditLog
from .coding import CodingWorkspace
from .failures import ToolExecutionError
from .sandbox_assets.contract import WorkspaceError, validate_path
from .ticket_store import TICKET_ID

TICKET_POLICY_VERSION = "mock-ticket-v1"
CODING_POLICY_VERSION = "coding-workspace-v1"


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolResult:
    name: str
    status: Literal[
        "ok", "denied", "invalid", "not_found", "approval_required", "failed"
    ]
    code: str
    value: str | None = None
    approval: ApprovalIntent | None = None
    call_id: UUID | None = field(default=None, compare=False)
    attempt_id: UUID | None = field(default=None, compare=False)
    run_id: UUID | None = field(default=None, compare=False)


class ReadTicketArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket_id: str = Field(pattern=r"^IT-[0-9]{3}$")


class UpdateTicketStatusArgs(ReadTicketArgs):
    status: Literal["open", "in_progress", "resolved"]


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadFileArgs(EmptyArgs):
    path: str = Field(min_length=1, max_length=160)


class WriteFileArgs(ReadFileArgs):
    content: str = Field(
        max_length=8192,
        description="Full file text, at most 8192 UTF-8 bytes. Split larger source across authorized files.",
    )

    @field_validator("content")
    @classmethod
    def check_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 8192:
            raise ValueError("File contents exceed the byte budget")
        return value


class RunTestsArgs(EmptyArgs):
    paths: list[str] = Field(min_length=1, max_length=8)


class VerifyCasesArgs(EmptyArgs):
    cases: list[list[int]] = Field(min_length=1, max_length=64)


class AcceptanceCasesArgs(EmptyArgs):
    function: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
    cases: list[list] = Field(min_length=1, max_length=64)

    @field_validator("cases")
    @classmethod
    def bounded_cases(cls, value):
        if len(json.dumps(value, allow_nan=False).encode()) > 16000:
            raise ValueError("Acceptance cases exceed the byte budget")
        return value


class BookObservationArgs(EmptyArgs):
    cases: list[dict] = Field(min_length=1, max_length=64)

    @field_validator("cases")
    @classmethod
    def bounded_cases(cls, value):
        if len(json.dumps(value, allow_nan=False).encode()) > 32768:
            raise ValueError("Observation input exceeds its budget")
        return value


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: int
    input_schema: type[BaseModel]
    access: Literal["read", "write", "execute"]
    target: Literal["ticket", "coding"] = "ticket"
    timeout_seconds: int = 15
    max_output_bytes: int = 131072

    @property
    def requires_approval(self) -> bool:
        return self.target == "ticket" and self.access == "write"


_TOOL_SPECS = {
    spec.name: spec
    for spec in (
        ToolSpec("read_ticket", 1, ReadTicketArgs, "read"),
        ToolSpec("update_ticket_status", 1, UpdateTicketStatusArgs, "write"),
        ToolSpec("read_file", 1, ReadFileArgs, "read", "coding"),
        ToolSpec("write_file", 1, WriteFileArgs, "write", "coding"),
        ToolSpec("list_files", 1, EmptyArgs, "read", "coding"),
        ToolSpec("run_tests", 1, RunTestsArgs, "execute", "coding"),
        ToolSpec("_verify_coding_contract", 1, VerifyCasesArgs, "execute", "coding"),
        ToolSpec("_verify_acceptance", 1, AcceptanceCasesArgs, "execute", "coding"),
        ToolSpec("_verify_tests", 1, RunTestsArgs, "execute", "coding"),
        ToolSpec("_observe_book", 1, BookObservationArgs, "execute", "coding"),
    )
}


def public_tool_schemas(target: Literal["ticket", "coding"]) -> list[dict]:
    descriptions = {
        "read_ticket": "Read a ticket within the configured task scope.",
        "update_ticket_status": "Request a ticket status change; requires human approval before execution.",
        "list_files": "List readable task workspace files.",
        "read_file": "Read an authorized relative text file.",
        "write_file": "Write an authorized relative text file within its byte budget.",
        "run_tests": "Run pytest against authorized tests/*.py in the sandbox for development feedback only; this is not final task verification.",
    }
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": descriptions[spec.name],
                "parameters": spec.input_schema.model_json_schema(),
            },
        }
        for spec in _TOOL_SPECS.values()
        if spec.target == target and not spec.name.startswith("_")
    ]


class ToolGateway:
    def __init__(
        self,
        tickets: Mapping[str, str] | None = None,
        allowed_tickets: frozenset[str] = frozenset(),
        *,
        writable_tickets: frozenset[str] = frozenset(),
        coding: CodingWorkspace | None = None,
        readable_files: frozenset[str] = frozenset(),
        writable_files: frozenset[str] = frozenset(),
        test_paths: frozenset[str] = frozenset(),
        audit: AuditLog | None = None,
        ticket_reader=None,
    ):
        self._tickets = tickets if tickets is not None else {}
        self._ticket_scopes = {"read": allowed_tickets, "write": writable_tickets}
        self._coding = coding
        self._file_scopes = {
            "read": readable_files,
            "write": writable_files,
            "execute": test_paths,
        }
        self._audit = audit
        self._ticket_reader = ticket_reader

    def execute(self, request: ToolRequest) -> ToolResult:
        return self._execute(request, verification=False)

    def verify_contract(self, cases: list[list[int]]) -> ToolResult:
        return self._execute(
            ToolRequest("_verify_coding_contract", {"cases": cases}), verification=True
        )

    def verify_acceptance(self, function: str, cases: list[list]) -> ToolResult:
        return self._execute(
            ToolRequest("_verify_acceptance", {"function": function, "cases": cases}),
            verification=True,
        )

    def verify_tests(self, paths: list[str]) -> ToolResult:
        return self._execute(
            ToolRequest("_verify_tests", {"paths": paths}), verification=True
        )

    def observe_book(self, cases: list[dict]) -> ToolResult:
        return self._execute(
            ToolRequest("_observe_book", {"cases": cases}), verification=True
        )

    def _execute(self, request: ToolRequest, *, verification: bool) -> ToolResult:
        started = time.monotonic()
        spec = _TOOL_SPECS.get(request.name)
        args = None
        resources = ()
        early = None
        if spec is None:
            early = ToolResult(request.name, "denied", "unknown_tool")
        elif spec.name.startswith("_") and not verification:
            early = ToolResult(request.name, "denied", "control_plane_only")
        else:
            try:
                args = spec.input_schema.model_validate(request.arguments)
            except ValidationError:
                early = ToolResult(request.name, "invalid", "invalid_arguments")
                content = (
                    request.arguments.get("content")
                    if isinstance(request.arguments, dict)
                    else None
                )
                if spec.name == "write_file" and isinstance(content, str):
                    size = len(content.encode("utf-8"))
                    if size > 8192:
                        early = replace(
                            early,
                            value=json.dumps(
                                {
                                    "field": "content",
                                    "issue": "utf8_byte_limit",
                                    "actual_bytes": size,
                                    "max_bytes": 8192,
                                    "hint": "Shorten the file substantially or split it across authorized modules. Retrying the same content cannot succeed.",
                                }
                            ),
                        )
            if args is not None:
                resources, early = self._authorize(spec, args)
        canonical = (
            json.dumps(
                args.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if args is not None
            else None
        )
        if early is None and spec.requires_approval:
            early = ToolResult(
                request.name,
                "approval_required",
                "write_approval_required",
                approval=ApprovalIntent(spec.name, spec.version, canonical),
            )
        policy = (
            CODING_POLICY_VERSION
            if spec is not None and spec.target == "coding"
            else TICKET_POLICY_VERSION
        )
        call_id = None
        if self._audit is not None:
            decision = (
                "ALLOW"
                if early is None
                else "REQUIRE_APPROVAL"
                if early.status == "approval_required"
                else "DENY"
            )
            call_id = self._audit.begin(
                tool=request.name,
                version=spec.version if spec else None,
                policy=policy,
                digest=hashlib.sha256(canonical.encode()).hexdigest()
                if canonical is not None
                else None,
                resources=resources,
                decision=decision,
                code=early.code if early else "authorized",
                verification=verification,
            )
        try:
            result = early if early is not None else self._dispatch(spec, args)
        except Exception:  # noqa: BLE001 - classify arbitrary adapter failures at the trusted boundary
            uncertain = early is None and spec.access != "read"
            if self._audit is not None:
                try:
                    self._audit.finish(
                        call_id,
                        "failed",
                        "tool_outcome_unknown"
                        if uncertain
                        else "execution_adapter_error",
                        int((time.monotonic() - started) * 1000),
                    )
                except ToolExecutionError:
                    # The original dispatch may have committed. Never retry it here.
                    pass
            raise ToolExecutionError(
                "execution_adapter_error", outcome_unknown=uncertain
            ) from None
        if self._audit is not None:
            try:
                self._audit.finish(
                    call_id,
                    result.status,
                    result.code,
                    int((time.monotonic() - started) * 1000),
                    value=result.value,
                )
            except ToolExecutionError as exc:
                # Even a received result is not a durable receipt until journaled.
                exc.outcome_unknown = early is None and spec.access != "read"
                raise
            identity = self._audit.identity(call_id)
            return replace(
                result,
                call_id=identity.call_id,
                attempt_id=identity.attempt_id,
                run_id=identity.run_id,
            )
        return result

    def _authorize(self, spec, args):
        if spec.target == "ticket":
            resources = (args.ticket_id,)
            if args.ticket_id not in self._ticket_scopes[spec.access]:
                return resources, ToolResult(
                    spec.name, "denied", "resource_out_of_scope"
                )
            return resources, None
        if self._coding is None:
            return (), ToolResult(spec.name, "denied", "execution_target_unavailable")
        if spec.name in {
            "list_files",
            "_verify_coding_contract",
            "_verify_acceptance",
            "_observe_book",
        }:
            return ("workspace",), None
        resources = tuple(args.paths) if spec.access == "execute" else (args.path,)
        try:
            for path in resources:
                validate_path(path)
        except WorkspaceError:
            return (), ToolResult(spec.name, "denied", "path_out_of_scope")
        if any(path not in self._file_scopes[spec.access] for path in resources):
            return resources, ToolResult(spec.name, "denied", "resource_out_of_scope")
        if spec.access == "execute" and any(
            not path.startswith("tests/") or not path.endswith(".py")
            for path in resources
        ):
            return resources, ToolResult(spec.name, "denied", "command_not_allowed")
        return resources, None

    def _dispatch(self, spec, args):
        if spec.target == "coding":
            if spec.name == "_verify_tests":
                return ToolResult(spec.name, "failed", "trusted_contract_required")
            elif spec.name == "_observe_book":
                reply = self._coding.observe_book(args.cases)
            elif spec.name == "_verify_coding_contract":
                reply = self._coding.verify_contract(
                    args.cases,
                    timeout=spec.timeout_seconds,
                    output_limit=spec.max_output_bytes,
                )
            elif spec.name == "_verify_acceptance":
                reply = self._coding.verify_acceptance(args.function, args.cases)
            else:
                reply = self._coding.execute(
                    spec.name,
                    args.model_dump(),
                    timeout=spec.timeout_seconds,
                    output_limit=spec.max_output_bytes,
                )
            if not reply.ok:
                return ToolResult(spec.name, "failed", reply.code)
            value = reply.value
            if spec.name == "list_files":
                value = [path for path in value if path in self._file_scopes["read"]]
            return ToolResult(
                spec.name,
                "failed" if reply.code == "tests_failed" else "ok",
                reply.code,
                value=value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False),
            )
        status = (
            self._ticket_reader(args.ticket_id)
            if self._ticket_reader is not None
            else self._tickets.get(args.ticket_id)
        )
        if status is None:
            return ToolResult(spec.name, "not_found", "ticket_not_found")
        return ToolResult(spec.name, "ok", "ticket_read", value=status)


class TicketGateway(ToolGateway):
    """Compatibility name for existing callers, using the shared implementation."""


def verify_update_intent(intent: ApprovalIntent) -> UpdateTicketStatusArgs:
    """Reapply the current write policy and compare the versioned tool request."""
    args = UpdateTicketStatusArgs.model_validate_json(intent.canonical_arguments)
    gateway = TicketGateway({}, frozenset(), writable_tickets=frozenset({TICKET_ID}))
    checked = gateway.execute(ToolRequest(intent.tool_name, args.model_dump()))
    if checked.approval != intent:
        raise ValueError(
            "Approved request no longer matches the tool schema, version or scope"
        )
    return args
