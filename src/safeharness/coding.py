"""A task-owned, bounded snapshot transported to disposable Linux workers."""

import json
import time
from dataclasses import dataclass
from uuid import UUID

from .invocations import digest
from .sandbox import DockerSandbox
from .sandbox_assets.contract import WorkspaceError, validate_files


@dataclass(frozen=True)
class CodingReply:
    ok: bool
    code: str
    value: object = None


class CodingWorkspace:
    def __init__(
        self,
        files: dict[str, str],
        task_id: UUID,
        *,
        sandbox=None,
        operation_timeout: float = 15,
        task_timeout: float = 120,
        cancel_check=None,
    ):
        self._files = dict(validate_files(files))
        self.identity = {"version": 1, "baseline_sha256": digest(self._files)}
        self.task_id = task_id
        self.sandbox = sandbox or DockerSandbox()
        self.operation_timeout = operation_timeout
        self.deadline = time.monotonic() + task_timeout
        self._image_id = None
        self.stopped = False
        self.last_container = None
        self.cancel_check = cancel_check

    @property
    def files(self) -> dict[str, str]:
        return dict(self._files)

    @property
    def image_id(self) -> str | None:
        return self._image_id

    def snapshot(self):
        return {
            "version": 1,
            "files": self.files,
            "image_id": self._image_id,
            "stopped": self.stopped,
        }

    def restore(self, state):
        if state.get("version") != 1 or type(state.get("stopped")) is not bool:
            raise ValueError("Invalid workspace checkpoint")
        files = dict(validate_files(state["files"]))
        if set(files) != set(self._files):
            raise ValueError("Workspace manifest has changed")
        self._files, self._image_id, self.stopped = (
            files,
            state["image_id"],
            state["stopped"],
        )

    def execute(
        self,
        action: str,
        arguments: dict,
        *,
        timeout: float = 15,
        output_limit: int = 131072,
    ) -> CodingReply:
        if action == "write_file":
            try:
                validate_files({**self._files, arguments["path"]: arguments["content"]})
            except WorkspaceError as exc:
                # Reject a known oversized candidate before dispatch, preserving the
                # last valid snapshot so the agent can shorten it and retry.
                return CodingReply(False, str(exc))
            except (KeyError, TypeError):
                return CodingReply(False, "invalid_arguments")
        return self._invoke(
            action, arguments, commit=True, timeout=timeout, output_limit=output_limit
        )

    def verify_contract(
        self, cases: list[list[int]], *, timeout: float = 15, output_limit: int = 131072
    ) -> CodingReply:
        return self._invoke(
            "verify_contract",
            {"cases": cases},
            commit=False,
            timeout=timeout,
            output_limit=output_limit,
        )

    def verify_acceptance(self, function: str, cases: list[list]) -> CodingReply:
        """Trusted runner only. Expected results remain outside the worker."""
        if not function.isidentifier() or not 1 <= len(cases) <= 64:
            return CodingReply(False, "invalid_acceptance_contract")
        arguments = {"function": function, "cases": cases}
        if len(json.dumps(arguments).encode()) > 16384:
            return CodingReply(False, "invalid_acceptance_contract")
        return self._invoke(
            "verify_acceptance",
            arguments,
            commit=False,
            timeout=15,
            output_limit=131072,
        )

    def observe_book(self, cases):
        if (
            not isinstance(cases, list)
            or not 1 <= len(cases) <= 64
            or len(json.dumps(cases, allow_nan=False).encode()) > 32768
        ):
            return CodingReply(False, "invalid_observation_contract")
        return self._invoke(
            "observe_book",
            {"cases": cases},
            commit=False,
            timeout=15,
            output_limit=131072,
        )

    def _invoke(
        self,
        action: str,
        arguments: dict,
        *,
        commit: bool,
        timeout: float,
        output_limit: int,
    ) -> CodingReply:
        if self.cancel_check and self.cancel_check():
            self.stopped = True
            return CodingReply(False, "execution_cancelled")
        if self.stopped:
            return CodingReply(False, "workspace_stopped")
        if time.monotonic() >= self.deadline:
            self.stopped = True
            return CodingReply(False, "task_timeout")
        preflight = self.sandbox.preflight()
        if not preflight.ready:
            self.stopped = True
            return CodingReply(False, "sandbox_unavailable")
        if self._image_id is not None and self._image_id != preflight.image_id:
            self.stopped = True
            return CodingReply(False, "worker_image_changed")
        self._image_id = preflight.image_id
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.stopped = True
            return CodingReply(False, "task_timeout")
        payload = json.dumps(
            {"files": self._files, "action": action, "arguments": arguments}
        ).encode()
        result = self.sandbox.run_container(
            self._image_id,
            "worker.py",
            payload=payload,
            timeout=min(timeout, self.operation_timeout, remaining),
            task_id=str(self.task_id),
            output_limit=output_limit,
            cancel_check=self.cancel_check,
        )
        self.last_container = result
        if result.code != "completed":
            self.stopped = True
            return CodingReply(False, result.code)
        try:
            reply = json.loads(result.command.stdout)
            if reply["ok"] is not True:
                self.stopped = True
                return CodingReply(False, reply["code"])
            files = dict(validate_files(reply["files"]))
            value = reply["value"]
            if action in {"run_tests", "verify_contract"}:
                value["output"] = result.command.stderr
            if action == "run_tests":
                value["evidence_level"] = "development_only"
            if commit:
                self._files = files
            elif files != self._files:
                return CodingReply(False, "verification_mutated_workspace")
            return CodingReply(
                True,
                "tests_passed"
                if action == "run_tests"
                and value["exit_code"] == 0
                and value.get("completed") is True
                and type(value.get("collected")) is int
                and value["collected"] > 0
                and value.get("passed") == value["collected"]
                and value.get("failed") == 0
                and value.get("skipped") == 0
                else "tests_failed"
                if action == "run_tests"
                else "completed",
                value,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            self.stopped = True
            return CodingReply(False, "invalid_worker_response")
