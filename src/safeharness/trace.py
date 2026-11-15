"""Versioned public events. Raw prompts, arguments and responses are excluded."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import inspect

from .contracts import CURRENT_CALL
from .database import database_exists, engine_for, execute, normalize_database

TRACE_VERSION = 2
FIELDS = {
    "scheduling.enqueued": {"schema_version"},
    "scheduling.cancelled": {"schema_version"},
    "scheduling.claimed": {"worker_id", "generation"},
    "scheduling.finished": {"worker_id", "generation", "status"},
    "scheduling.recovery": {"action", "code", "generation", "recovery_count"},
    "scheduling.approval_ready": {"generation"},
    "runtime.verified": {"passed", "code"},
    "checkpoint.saved": {
        "checkpoint_id",
        "generation",
        "phase",
        "step_count",
        "sha256",
        "byte_count",
    },
    "checkpoint.restored": {
        "checkpoint_id",
        "generation",
        "phase",
        "step_count",
        "revision",
    },
    "context.prepared": {
        "input_bytes",
        "message_count",
        "omitted_groups",
        "context_sha256",
    },
    "task.created": {"status", "revision"},
    "task.transition": {"from_status", "status", "revision"},
    "tool.policy": {
        "call_id",
        "tool_name",
        "tool_version",
        "policy_version",
        "arguments_sha256",
        "status",
        "code",
        "elapsed_ms",
    },
    "tool.intent": {
        "call_id",
        "tool_name",
        "tool_version",
        "policy_version",
        "arguments_sha256",
        "status",
        "code",
        "elapsed_ms",
    },
    "tool.result": {
        "call_id",
        "tool_name",
        "tool_version",
        "policy_version",
        "arguments_sha256",
        "status",
        "code",
        "elapsed_ms",
    },
    "execution.intent": {
        "operation_id",
        "tool_name",
        "arguments_sha256",
        "policy_version",
    },
    "execution.result": {"operation_id", "status"},
    "model.request": {
        "attempt_id",
        "provider",
        "model",
        "prompt_sha256",
        "input_bytes",
        "max_output_tokens",
    },
    "model.result": {
        "attempt_id",
        "provider",
        "model",
        "response_sha256",
        "prompt_tokens",
        "completion_tokens",
        "elapsed_ms",
        "cost_upper_cny",
        "finish_reason",
    },
    "model.error": {
        "attempt_id",
        "provider",
        "code",
        "retryable",
        "attempt",
        "elapsed_ms",
    },
    "model.response_reused": {"provider", "model", "prompt_sha256", "response_sha256"},
    "verification": {"passed", "code", "case_count", "verifier_version"},
    "checkpoint": {"revision", "context_sha256", "completed_calls", "stop_reason"},
    "runtime.error": {
        "phase",
        "code",
        "outcome_unknown",
        "recorded_status",
        "recovery_action",
    },
}


for _kind in ("tool.policy", "tool.intent", "tool.result"):
    FIELDS[_kind].add("attempt_id")
for _kind in (
    "model.request",
    "model.result",
    "model.error",
    "model.response_reused",
    "execution.intent",
    "execution.result",
):
    FIELDS[_kind].add("call_id")
for _kind in ("execution.intent", "execution.result"):
    FIELDS[_kind].add("attempt_id")


def insert_event(connection, task_id: UUID, kind: str, payload: dict, *, run_id=None):
    if kind not in FIELDS or payload.keys() - FIELDS[kind]:
        raise ValueError("Unregistered Trace fields")
    for value in payload.values():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("Trace payloads only accept bounded scalar metadata")
        if isinstance(value, str) and (
            len(value) > 160 or re.search(r"sk-[A-Za-z0-9_-]{16,}", value)
        ):
            raise ValueError("Trace contains sensitive or oversized metadata")
    current = CURRENT_CALL.get()
    if run_id is None:
        if current is not None and current.task_id == task_id:
            run_id = current.run_id
        else:
            value = execute(
                connection,
                "SELECT run_id FROM task_runs WHERE task_id=? ORDER BY CASE WHEN status='RUNNING' THEN 0 ELSE 1 END,started_at DESC,run_id DESC LIMIT 1",
                (task_id.hex,),
            ).scalar_one_or_none()
            run_id = UUID(value) if value else None
    query = "INSERT INTO trace_events (schema_version,task_id,kind,payload_json,recorded_at,run_id) VALUES (?,?,?,?,?,?)"
    parameters = (
        TRACE_VERSION,
        task_id.hex,
        kind,
        json.dumps(payload, allow_nan=False),
        datetime.now(UTC).isoformat(timespec="milliseconds"),
        run_id.hex if run_id is not None else None,
    )
    execute(connection, query, parameters)


class TraceLog:
    def __init__(self, database: Path, task_id: UUID):
        self.database = normalize_database(database)
        self._engine = engine_for(self.database)
        self.task_id = task_id

    def emit(self, kind: str, payload: dict):
        from .database import lock_task
        from .scheduling import require_claim

        if not database_exists(self.database):
            raise ValueError("Trace database is not initialized")
        current = CURRENT_CALL.get()
        if (
            kind.startswith("model.")
            and current is not None
            and current.task_id == self.task_id
        ):
            payload = {**payload, "call_id": str(current.call_id)}
        with self._engine.begin() as connection:
            lock_task(connection, self.task_id)
            require_claim(connection, self.task_id)
            insert_event(connection, self.task_id, kind, payload)

    def read(self) -> list[dict]:
        if not database_exists(self.database):
            return []
        with self._engine.connect() as connection:
            if not inspect(connection).has_table("trace_events"):
                return []
            rows = (
                execute(
                    connection,
                    "SELECT * FROM trace_events WHERE task_id=? ORDER BY sequence",
                    (self.task_id.hex,),
                )
                .mappings()
                .all()
            )
        return [
            {
                "schema_version": row["schema_version"],
                "sequence": row["sequence"],
                "run_id": str(UUID(row["run_id"])) if row.get("run_id") else None,
                "task_id": str(self.task_id),
                "kind": row["kind"],
                "recorded_at": row["recorded_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def jsonl(self) -> str:
        return "".join(
            json.dumps(event, ensure_ascii=False) + "\n" for event in self.read()
        )
