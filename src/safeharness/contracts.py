"""Trusted invocation identities. Provider-supplied call IDs are not authorities."""

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class CallIdentity:
    task_id: UUID
    run_id: UUID
    call_id: UUID
    attempt_id: UUID
    attempt_index: int


CURRENT_CALL: ContextVar[CallIdentity | None] = ContextVar(
    "safeharness_call", default=None
)


class InvocationConflictError(RuntimeError):
    """A logical identity, payload or settled result cannot be changed."""


class SubmissionConflictError(RuntimeError):
    """An owner's idempotency key was already bound to another request."""
