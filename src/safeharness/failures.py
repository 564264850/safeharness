"""Bounded runtime diagnostics, independent of model/provider and storage code."""

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

FailurePhase = Literal[
    "model",
    "tool",
    "loop",
    "verification",
    "approval",
    "completion",
    "transition",
    "checkpoint",
]
PHASES = {
    "model",
    "tool",
    "loop",
    "verification",
    "approval",
    "completion",
    "transition",
    "checkpoint",
}


def machine_code(value: str) -> str:
    # Error text, credentials and exception reprs must never enter public Trace.
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
        else "unclassified_error"
    )


@dataclass(frozen=True)
class RuntimeFailure:
    phase: FailurePhase
    code: str
    outcome_unknown: bool = False

    def __post_init__(self):
        if (
            self.phase not in PHASES
            or machine_code(self.code) != self.code
            or type(self.outcome_unknown) is not bool
        ):
            raise ValueError("Invalid runtime failure metadata")


class ToolExecutionError(RuntimeError):
    """Trusted dispatch boundary describes whether a side effect may have occurred."""

    def __init__(self, code="tool_execution_error", *, outcome_unknown=True):
        self.code = machine_code(code)
        self.outcome_unknown = outcome_unknown
        super().__init__(self.code)


class RuntimePersistenceError(RuntimeError):
    """Failure could not be durably recorded; callers must not blindly retry."""

    code = "runtime_failure_not_persisted"

    def __init__(self, task_id: UUID, failure: RuntimeFailure):
        self.task_id, self.failure = task_id, failure
        super().__init__(self.code)
