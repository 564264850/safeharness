"""Task identity and allowed lifecycle transitions."""

from dataclasses import dataclass, field, replace
from enum import Enum
from uuid import UUID, uuid4


class TaskStatus(Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


_ALLOWED_TRANSITIONS = {
    TaskStatus.CREATED: (TaskStatus.RUNNING, TaskStatus.CANCELLED),
    TaskStatus.RUNNING: (
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.VERIFYING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.NEEDS_REVIEW,
    ),
    TaskStatus.WAITING_APPROVAL: (
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.VERIFYING: (
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.NEEDS_REVIEW,
    ),
}


@dataclass(frozen=True)
class Task:
    task_id: UUID = field(default_factory=uuid4)
    status: TaskStatus = TaskStatus.CREATED
    revision: int = 0

    def transition(self, status: TaskStatus) -> "Task":
        if status not in _ALLOWED_TRANSITIONS.get(self.status, ()):
            raise ValueError(
                f"Invalid task transition: {self.status.value} -> {status.value}"
            )
        return replace(self, status=status, revision=self.revision + 1)
