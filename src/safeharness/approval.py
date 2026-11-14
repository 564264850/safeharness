"""The exact tool request held for human approval."""

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from uuid import UUID


@dataclass(frozen=True)
class ApprovalIntent:
    tool_name: str
    tool_version: int
    canonical_arguments: str

    @property
    def arguments_sha256(self) -> str:
        return sha256(self.canonical_arguments.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PendingApproval:
    approval_id: UUID
    task_id: UUID
    intent: ApprovalIntent
    policy_version: str = "mock-ticket-v1"


@dataclass(frozen=True)
class ApprovalActor:
    """Identity and capability supplied by the trusted control plane."""

    subject: str
    can_approve: bool = False


class ApprovalOutcome(Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ApprovalDecision:
    outcome: ApprovalOutcome
    decided_by: str
    arguments_sha256: str
    decided_at: str


@dataclass(frozen=True)
class ApprovalRecord:
    request: PendingApproval
    decision: ApprovalDecision | None
