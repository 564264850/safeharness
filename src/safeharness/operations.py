"""Small operational surface: durable queue counts, alerts and admission control."""

from .database import execute
from .scheduling import clock_sql


class QueueCapacityError(RuntimeError):
    pass


def admit(connection, limit):
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("Invalid queue capacity")
    if connection.dialect.name == "postgresql":
        execute(connection, "SELECT pg_advisory_xact_lock(734271902)")
    count = execute(
        connection,
        "SELECT count(*) FROM task_queue WHERE state IN ('QUEUED','READY','CLAIMED','WAITING')",
    ).scalar_one()
    if count >= limit:
        raise QueueCapacityError("queue_capacity_reached")


def metrics(store):
    with store._engine.connect() as c:
        states = {
            r[0]: r[1]
            for r in execute(
                c, "SELECT state,count(*) FROM task_queue GROUP BY state"
            ).all()
        }
        tasks = {
            r[0]: r[1]
            for r in execute(
                c, "SELECT status,count(*) FROM tasks GROUP BY status"
            ).all()
        }
        expired = execute(
            c,
            f"SELECT count(*) FROM task_queue WHERE state='CLAIMED' AND lease_expires_at<={clock_sql(c)}",
        ).scalar_one()
        unknown = execute(
            c, "SELECT count(*) FROM call_attempts WHERE status IN ('INTENT','UNKNOWN')"
        ).scalar_one()
    return {
        "queue": states,
        "tasks": tasks,
        "task_total": sum(tasks.values()),
        "queue_total": sum(states.values()),
        "expired_claims": expired,
        "unconfirmed_attempts": unknown,
        "alerts": (["expired_claims"] if expired else [])
        + (["needs_review"] if tasks.get("NEEDS_REVIEW", 0) else []),
    }
