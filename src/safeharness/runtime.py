"""A bounded loop with explicit error and side-effect convergence boundaries."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from .approval import ApprovalIntent
from .checkpoints import CheckpointError, CheckpointSession
from .execution import ToolRequest, ToolResult
from .failures import (
    RuntimeFailure,
    RuntimePersistenceError,
    ToolExecutionError,
    machine_code,
)
from .invocations import InvocationLedger
from .task import Task, TaskStatus
from .task_store import TaskStore
from .trace import TraceLog


class RunInterrupted(RuntimeError):
    def __init__(self, code: str):
        self.code = machine_code(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class FinalAnswer:
    text: str


@dataclass(frozen=True)
class RunResult:
    stop_reason: Literal[
        "model_finished",
        "step_limit",
        "approval_required",
        "model_error",
        "tool_error",
        "runtime_error",
        "paused",
    ]
    answer: str | None
    tool_results: tuple[ToolResult, ...]
    error_code: str | None = None
    failure: RuntimeFailure | None = None


@dataclass(frozen=True)
class TaskResult:
    task: Task
    run: RunResult
    approval_id: UUID | None = None
    run_id: UUID | None = None


def _stopped(reason, results, failure):
    return RunResult(reason, None, tuple(results), failure.code, failure)


def _valid_tool_result(result, request):
    if not isinstance(result, ToolResult) or result.name != request.name:
        return False
    if not isinstance(result.status, str) or result.status not in {
        "ok",
        "denied",
        "invalid",
        "not_found",
        "approval_required",
        "failed",
    }:
        return False
    if not isinstance(result.code, str) or machine_code(result.code) != result.code:
        return False
    if result.value is not None and not isinstance(result.value, str):
        return False
    if result.status != "approval_required":
        return result.approval is None
    intent = result.approval
    if (
        not isinstance(intent, ApprovalIntent)
        or intent.tool_name != request.name
        or type(intent.tool_version) is not int
        or intent.tool_version < 1
    ):
        return False
    try:
        canonical = json.dumps(
            request.arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return canonical == intent.canonical_arguments
    except (TypeError, ValueError):
        return False


def run(
    next_action: Callable[[tuple[ToolResult, ...]], ToolRequest | FinalAnswer],
    execute: Callable[[ToolRequest], ToolResult],
    *,
    max_steps: int,
    recorder: InvocationLedger | None = None,
    checkpoint: CheckpointSession | None = None,
    task: Task | None = None,
    initial_results: tuple[ToolResult, ...] = (),
    initial_steps: int = 0,
    pending_action=None,
    pause_after: int | None = None,
    max_repeated_failures: int | None = None,
) -> RunResult:
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps must be a positive integer")
    results: list[ToolResult] = list(initial_results)
    steps, completed_here = initial_steps, 0
    while pending_action is not None or steps < max_steps:
        if pending_action is not None:
            action, pending_action = pending_action, None
        else:
            steps += 1
            if checkpoint:
                checkpoint.save(task, "before_model", results, steps)
            try:
                action = next_action(tuple(results))
            except RunInterrupted as exc:
                return _stopped(
                    "model_error", results, RuntimeFailure("model", exc.code)
                )
            except Exception:  # noqa: BLE001 - arbitrary model callbacks must converge safely
                return _stopped(
                    "model_error",
                    results,
                    RuntimeFailure("model", "model_adapter_error"),
                )
        if isinstance(action, FinalAnswer):
            if not isinstance(action.text, str) or not action.text.strip():
                return _stopped(
                    "model_error",
                    results,
                    RuntimeFailure("model", "invalid_final_answer"),
                )
            if checkpoint:
                checkpoint.save(task, "final", results, steps, action)
            return RunResult("model_finished", action.text, tuple(results))
        if (
            not isinstance(action, ToolRequest)
            or not isinstance(action.name, str)
            or not 1 <= len(action.name) <= 80
            or not isinstance(action.arguments, dict)
        ):
            return _stopped(
                "model_error", results, RuntimeFailure("model", "invalid_model_action")
            )
        if checkpoint:
            checkpoint.save(task, "after_model", results, steps, action)
            checkpoint.save(task, "before_tool", results, steps, action)
        try:
            result = (
                recorder.invoke_tool(action, execute, _valid_tool_result)
                if recorder is not None
                else execute(action)
            )
        except ToolExecutionError as exc:
            return _stopped(
                "tool_error",
                results,
                RuntimeFailure("tool", exc.code, exc.outcome_unknown),
            )
        except Exception:  # noqa: BLE001 - unknown adapter outcomes require review
            return _stopped(
                "tool_error",
                results,
                RuntimeFailure("tool", "tool_outcome_unknown", True),
            )
        if not _valid_tool_result(result, action):
            return _stopped(
                "tool_error",
                results,
                RuntimeFailure("tool", "invalid_tool_result", True),
            )
        results.append(result)
        completed_here += 1
        if checkpoint:
            checkpoint.save(task, "after_tool", results, steps)
        if result.status == "failed" and result.code == "container_cleanup_failed":
            return _stopped(
                "tool_error",
                results,
                RuntimeFailure("tool", "container_cleanup_failed", True),
            )
        if result.status == "approval_required":
            return RunResult("approval_required", None, tuple(results))
        if max_repeated_failures is not None and len(results) >= max_repeated_failures:
            recent = results[-max_repeated_failures:]
            if result.status in {"denied", "invalid", "failed", "not_found"} and all(
                (r.name, r.status, r.code) == (result.name, result.status, result.code)
                for r in recent
            ):
                return _stopped(
                    "runtime_error",
                    results,
                    RuntimeFailure("loop", "repeated_tool_failure"),
                )
        if pause_after is not None and completed_here >= pause_after:
            return RunResult("paused", None, tuple(results))
    return RunResult(
        "step_limit", None, tuple(results), failure=RuntimeFailure("loop", "step_limit")
    )


def _converge(task, result, failure, store, run_id=None):
    if store is None:
        target = (
            TaskStatus.NEEDS_REVIEW if failure.outcome_unknown else TaskStatus.FAILED
        )
        return TaskResult(task.transition(target), result)
    try:
        current = store.record_runtime_failure(task, failure)
    except Exception:  # noqa: BLE001 - never claim that a failed persistence succeeded
        raise RuntimePersistenceError(task.task_id, failure) from None
    return TaskResult(current, result, run_id=run_id)


def run_task(
    next_action: Callable[[tuple[ToolResult, ...]], ToolRequest | FinalAnswer],
    execute: Callable[[ToolRequest], ToolResult],
    verify: Callable[[RunResult], bool],
    *,
    max_steps: int,
    store: TaskStore | None = None,
    task: Task | None = None,
    complete: Callable[[Task, RunResult, bool], Task] | None = None,
    checkpoint: CheckpointSession | None = None,
    resume: bool = False,
    recover_abandoned: bool = False,
    expected_generation: int | None = None,
    pause_after: int | None = None,
    approval_workspace=None,
    after_business_commit=None,
    max_repeated_failures: int | None = None,
    initialize_running: bool = False,
) -> TaskResult:
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps must be a positive integer")
    if max_repeated_failures is not None and (
        type(max_repeated_failures) is not int or max_repeated_failures < 2
    ):
        raise ValueError("Repeated failure limit must be at least two")
    if pause_after is not None and (
        type(pause_after) is not int or pause_after < 1 or checkpoint is None
    ):
        raise ValueError("pause_after needs a checkpoint and a positive tool count")
    if checkpoint is not None and (
        store is None
        or checkpoint.model is not next_action
        or checkpoint.store.database != store.database
    ):
        raise ValueError("Checkpoint must belong to this runtime and model")
    if resume and checkpoint is None:
        raise ValueError("Resume requires a checkpoint session")
    if resume:
        task = checkpoint.acquire(
            max_steps=max_steps,
            recover_abandoned=recover_abandoned,
            expected_generation=expected_generation,
            allow_approval=approval_workspace is not None,
        )
    if task is None:
        task = store.create() if store is not None else Task()
    elif not resume and (
        task.status
        is not (TaskStatus.RUNNING if initialize_running else TaskStatus.CREATED)
        or (store is not None and store.get(task.task_id) != task)
    ):
        raise ValueError("A supplied task must be a current CREATED task")
    transition = store.transition if store is not None else Task.transition
    # A failed claim must not terminate an already-concurrent runner.
    if initialize_running:
        from .scheduling import require_claim

        if store is None or checkpoint is None:
            raise ValueError(
                "Initializing an owned task requires persistence and checkpoints"
            )
        with store._engine.connect() as connection:
            claim = require_claim(connection, task.task_id)
            if claim is None or claim.recovery_mode != "initialize":
                raise ValueError("Task initialization requires a recovery claim")
    elif not resume:
        task = transition(task, TaskStatus.RUNNING)
    recorder = (
        InvocationLedger(store.database, task.task_id) if store is not None else None
    )
    run_id = None
    result = RunResult("runtime_error", None, ())
    phase = "loop"
    failure = None
    try:
        run_id = recorder.active_run_id() if recorder is not None else None
        previous = ()
        if checkpoint:
            phase = "checkpoint"
            if checkpoint.task_id != task.task_id:
                raise ValueError("Checkpoint task mismatch")
            if resume:
                previous = checkpoint.restore()
                task, previous = checkpoint.resolve_approval(
                    task,
                    previous,
                    approval_workspace,
                    after_business_commit=after_business_commit,
                )
                run_id = recorder.active_run_id()
            else:
                checkpoint.begin(task, max_steps)
        phase = "loop"
        if task.status is TaskStatus.VERIFYING:
            action = checkpoint.pending_action
            if not isinstance(action, FinalAnswer):
                raise CheckpointError("checkpoint_final_answer_missing")
            result = RunResult("model_finished", action.text, previous)
        else:
            result = run(
                next_action,
                execute,
                max_steps=max_steps,
                recorder=recorder,
                checkpoint=checkpoint,
                task=task,
                initial_results=previous,
                initial_steps=checkpoint.steps if checkpoint else 0,
                pending_action=checkpoint.pending_action if checkpoint else None,
                pause_after=pause_after,
                max_repeated_failures=max_repeated_failures,
            )
        if result.stop_reason == "paused":
            return TaskResult(task, result, run_id=run_id)
        if result.stop_reason == "approval_required":
            phase = "approval"
            intent = result.tool_results[-1].approval
            if store is None:
                failure = RuntimeFailure("approval", "approval_store_required")
            else:
                waiting, approval = store.pause_for_approval(
                    task, intent, result.tool_results[-1].call_id
                )
                return TaskResult(waiting, result, approval.approval_id, run_id)
        elif result.stop_reason != "model_finished":
            failure = result.failure
        else:
            phase = "transition"
            if task.status is not TaskStatus.VERIFYING:
                task = transition(task, TaskStatus.VERIFYING)
            phase = "verification"
            passed = verify(result)
            if type(passed) is not bool:
                failure = RuntimeFailure("verification", "invalid_verification_result")
            else:
                if store is not None:
                    TraceLog(store.database, task.task_id).emit(
                        "runtime.verified",
                        {
                            "passed": passed,
                            "code": "verifier_passed" if passed else "verifier_failed",
                        },
                    )
                target = TaskStatus.SUCCEEDED if passed else TaskStatus.FAILED
                phase = "completion" if complete else "transition"
                finished = (
                    complete(task, result, passed)
                    if complete
                    else transition(task, target)
                )
                if (
                    not isinstance(finished, Task)
                    or finished.task_id != task.task_id
                    or finished.status is not target
                    or finished.revision != task.revision + 1
                ):
                    failure = RuntimeFailure(
                        "completion", "invalid_completion_result", True
                    )
                elif store is not None and store.get(task.task_id) != finished:
                    failure = RuntimeFailure(
                        "completion", "completion_not_persisted", True
                    )
                else:
                    return TaskResult(finished, result, run_id=run_id)
    except CheckpointError as exc:
        failure = RuntimeFailure("checkpoint", exc.code, exc.outcome_unknown)
    except ToolExecutionError as exc:
        failure = RuntimeFailure(phase, exc.code, exc.outcome_unknown)
    except Exception:  # noqa: BLE001 - final live-run boundary for trusted callbacks
        failure = RuntimeFailure(
            phase,
            {
                "loop": "runtime_loop_error",
                "verification": "verification_error",
                "approval": "approval_persistence_error",
                "completion": "completion_error",
                "transition": "state_transition_error",
                "checkpoint": "checkpoint_state_error",
            }[phase],
            phase in {"loop", "completion"},
        )
    finally:
        if (
            checkpoint is not None
            and checkpoint.token is not None
            and (failure is not None or result.stop_reason != "runtime_error")
        ):
            try:
                current = store.get(task.task_id)
                state = (
                    "PAUSED"
                    if result.stop_reason == "paused"
                    else (
                        "NEEDS_REVIEW"
                        if failure is not None and failure.outcome_unknown
                        else (
                            current.status.value
                            if current.status
                            in {TaskStatus.WAITING_APPROVAL, TaskStatus.NEEDS_REVIEW}
                            else "FINISHED"
                        )
                    )
                )
                checkpoint.release(state)
            except Exception:  # noqa: BLE001 - never conceal a failed checkpoint settlement
                raise RuntimePersistenceError(
                    task.task_id,
                    RuntimeFailure("checkpoint", "checkpoint_release_error", True),
                ) from None
    if result.failure is None:
        result = replace(
            result,
            stop_reason="runtime_error",
            error_code=failure.code,
            failure=failure,
        )
    return _converge(task, result, failure, store, run_id)
