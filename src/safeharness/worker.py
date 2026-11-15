"""Independent lease-fenced consumer of queued starts and approved recoveries."""

import json
import sys
from threading import Event
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from .checkpoints import CheckpointError
from .failures import RuntimeFailure
from .heartbeat import LeaseHeartbeat, validate_intervals
from .scheduling import QueueClaimError, claim_next, claim_scope
from .service import TaskService
from .task import TaskStatus


class QueueWorker:
    def __init__(
        self,
        store,
        business,
        principals,
        *,
        worker_id=None,
        lease_seconds=30,
        heartbeat_seconds=5,
        task_service_settings=None,
    ):
        validate_intervals(lease_seconds, heartbeat_seconds)
        self.store = store
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds
        self.worker_id = worker_id or "worker-" + uuid4().hex[:16]
        self.service = TaskService(
            store, business, principals, **(task_service_settings or {})
        )

    def run_once(self):
        claim = claim_next(self.store, self.worker_id, lease_seconds=self.lease_seconds)
        if claim is None:
            return {"status": "idle", "worker_id": self.worker_id}
        with (
            claim_scope(claim),
            LeaseHeartbeat(self.store, claim, self.heartbeat_seconds) as heartbeat,
        ):
            try:
                result = self._execute(claim)
            except Exception:
                result = self.service.snapshot(claim.task_id)
                if result["task_status"] == "CANCELLED":
                    pass
                elif (
                    claim.fenced.is_set()
                    or heartbeat.failed.is_set()
                    or result["scheduling"]["lease_status"] == "EXPIRED"
                    or result["scheduling"]["generation"] != claim.generation
                ):
                    return {
                        "status": "lease_lost",
                        "worker_id": self.worker_id,
                        "code": "ownership_changed"
                        if result["scheduling"]["generation"] != claim.generation
                        else heartbeat.failure_code or "lease_expired",
                        **result,
                    }
                else:
                    raise
            # Approval may hand the task to a new generation during old cleanup.
            if result["scheduling"]["generation"] != claim.generation:
                return {
                    "status": "lease_lost",
                    "worker_id": self.worker_id,
                    "code": "ownership_changed",
                    **result,
                }
        if result["task_status"] not in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "NEEDS_REVIEW",
            "WAITING_APPROVAL",
        }:
            raise QueueClaimError(
                "Worker returned an unfinished task; inspect before retry"
            )
        return {
            "status": "waiting_approval"
            if result["task_status"] == "WAITING_APPROVAL"
            else "processed",
            "worker_id": self.worker_id,
            **result,
        }

    def _execute(self, claim):
        # Called only within the claim scope and live heartbeat context.
        metadata = self.store.service_task(claim.task_id)
        principal = self.service.principals.get(metadata["owner_id"])
        try:
            if principal is None or "requester" not in principal.roles:
                raise HTTPException(403, "owner_not_authorized")
            result = self.service.run(
                claim.task_id,
                principal,
                resume=claim.recovery_mode in {"resume", "approval"},
                recover_abandoned=claim.recovery_mode in {"resume", "approval"},
                expected_generation=claim.resume_generation,
            )
        except CheckpointError:
            current = self.store.get(claim.task_id)
            if current.status is TaskStatus.WAITING_APPROVAL:
                self.store.reject_queued_approval(
                    current, "recovery_checkpoint_rejected"
                )
            elif current.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                self.store.record_runtime_failure(
                    current,
                    RuntimeFailure("checkpoint", "recovery_checkpoint_rejected", True),
                )
            result = self.service.snapshot(claim.task_id)
        except (HTTPException, ValidationError):
            current = self.store.get(claim.task_id)
            if current.status is TaskStatus.WAITING_APPROVAL:
                self.store.reject_queued_approval(current, "worker_setup_rejected")
            elif current.status in {
                TaskStatus.CREATED,
                TaskStatus.RUNNING,
                TaskStatus.VERIFYING,
            }:
                running = (
                    self.store.transition(current, TaskStatus.RUNNING)
                    if current.status is TaskStatus.CREATED
                    else current
                )
                self.store.record_runtime_failure(
                    running,
                    RuntimeFailure(
                        "transition",
                        "worker_setup_rejected",
                        claim.recovery_mode in {"resume", "approval"},
                    ),
                )
            result = self.service.snapshot(claim.task_id)
        except Exception:
            if self.store.get(claim.task_id).status is not TaskStatus.CANCELLED:
                raise
            result = self.service.snapshot(claim.task_id)
        return result


def run_worker(
    store,
    business,
    principals,
    *,
    once=False,
    drain=False,
    poll_seconds=1.0,
    worker_id=None,
    lease_seconds=30,
    heartbeat_seconds=5,
    task_service_settings=None,
):
    if not 0.05 <= poll_seconds <= 30:
        raise ValueError("Worker poll interval must be between 0.05 and 30 seconds")
    worker = QueueWorker(
        store,
        business,
        principals,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        task_service_settings=task_service_settings,
    )
    stopped = Event()
    try:
        while True:
            result = worker.run_once()
            if result["status"] != "idle" or once or drain:
                print(json.dumps(result), flush=True)
            if result["status"] == "lease_lost":
                if once or drain:
                    return 1
                # Fencing belongs to this claim, not the entire consumer. A fresh
                # claim has a new token; only the scanner may recover the lost task.
                stopped.wait(poll_seconds)
                continue
            if once or (drain and result["status"] == "idle"):
                return 0
            if result["status"] == "idle":
                stopped.wait(poll_seconds)
    except KeyboardInterrupt:
        print(
            json.dumps({"status": "stopped", "worker_id": worker.worker_id}),
            file=sys.stderr,
        )
        return 0
