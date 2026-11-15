"""Persistent, transactional reservation of a small model API allowance."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID


class BudgetError(RuntimeError):
    pass


class BudgetLedger:
    # Peak cache-miss rates, rounded up from official USD pricing at an explicit
    # operator budget assumption of no more than CNY 8 per USD. Micro-CNY/token.
    INPUT_RATE = 3
    OUTPUT_RATE = 10
    LEGACY_PRICING_VERSION = "deepseek-flash-peak-20261111"
    PRICING_VERSION = "deepseek-flash-peak-usd-20260816-fx-ceiling-8"

    def __init__(
        self,
        path: Path,
        *,
        budget_id="m3-validation-v1",
        limit_cny=Decimal("0.50"),
        max_calls=80,
    ):
        self.path = path.resolve()
        self.budget_id = budget_id
        self.limit = int(Decimal(str(limit_cny)) * 1_000_000)
        if self.limit <= 0 or max_calls <= 0:
            raise ValueError("Budget limits must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS budgets (budget_id TEXT PRIMARY KEY, limit_micro INTEGER NOT NULL, max_calls INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS requests (attempt_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL REFERENCES budgets(budget_id), task_id TEXT NOT NULL, reserved_micro INTEGER NOT NULL, settled_micro INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL, pricing_version TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(requests)")
            }
            if "pricing_version" not in columns:
                connection.execute(
                    "ALTER TABLE requests ADD COLUMN pricing_version TEXT NOT NULL DEFAULT '"
                    + self.LEGACY_PRICING_VERSION
                    + "'"
                )
            # Revalue settled history at the current conservative ceiling. An old
            # reservation contains no token split, so scale it by the worst rate
            # ratio (new input / old input = 1.5) and round up.
            legacy_rows = connection.execute(
                "SELECT attempt_id,reserved_micro,settled_micro,prompt_tokens,completion_tokens,status,pricing_version FROM requests WHERE pricing_version<>?",
                (self.PRICING_VERSION,),
            ).fetchall()
            for (
                attempt,
                reserved,
                settled,
                prompt,
                completion,
                status,
                _,
            ) in legacy_rows:
                if (
                    status == "SETTLED"
                    and prompt is not None
                    and completion is not None
                ):
                    cost = prompt * self.INPUT_RATE + completion * self.OUTPUT_RATE
                    connection.execute(
                        "UPDATE requests SET settled_micro=?,pricing_version=? WHERE attempt_id=?",
                        (cost, self.PRICING_VERSION, attempt),
                    )
                elif status == "RESERVED":
                    conservative = (reserved * 3 + 1) // 2
                    connection.execute(
                        "UPDATE requests SET reserved_micro=?,pricing_version=? WHERE attempt_id=?",
                        (conservative, self.PRICING_VERSION, attempt),
                    )
                else:
                    connection.execute(
                        "UPDATE requests SET pricing_version=? WHERE attempt_id=?",
                        (self.PRICING_VERSION, attempt),
                    )
            connection.execute(
                "INSERT OR IGNORE INTO budgets VALUES (?,?,?)",
                (budget_id, self.limit, max_calls),
            )
            actual = connection.execute(
                "SELECT limit_micro,max_calls FROM budgets WHERE budget_id=?",
                (budget_id,),
            ).fetchone()
            if actual != (self.limit, max_calls):
                raise BudgetError(
                    "Existing budget configuration differs; choose an explicit new budget ID"
                )

    def _open(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def cny(micro):
        return str(Decimal(micro) / Decimal(1_000_000))

    def reserve(
        self,
        attempt_id: UUID,
        task_id: UUID,
        input_token_upper: int,
        output_tokens: int,
    ):
        if input_token_upper <= 0 or output_tokens <= 0:
            raise ValueError("Token reservations must be positive")
        reserve = input_token_upper * self.INPUT_RATE + output_tokens * self.OUTPUT_RATE
        with closing(self._open()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            limit, max_calls = connection.execute(
                "SELECT limit_micro,max_calls FROM budgets WHERE budget_id=?",
                (self.budget_id,),
            ).fetchone()
            used, count = connection.execute(
                "SELECT coalesce(sum(coalesce(settled_micro,reserved_micro)),0),count(*) FROM requests WHERE budget_id=?",
                (self.budget_id,),
            ).fetchone()
            if used + reserve > limit or count >= max_calls:
                raise BudgetError("Persistent provider budget exhausted")
            connection.execute(
                "INSERT INTO requests (attempt_id,budget_id,task_id,reserved_micro,settled_micro,prompt_tokens,completion_tokens,status,created_at,pricing_version) VALUES (?,?,?,?,NULL,NULL,NULL,'RESERVED',?,?)",
                (
                    attempt_id.hex,
                    self.budget_id,
                    task_id.hex,
                    reserve,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    self.PRICING_VERSION,
                ),
            )

    def settle(self, attempt_id: UUID, prompt_tokens: int, completion_tokens: int):
        if any(
            type(value) is not int or value < 0
            for value in (prompt_tokens, completion_tokens)
        ):
            raise ValueError("Invalid usage metadata")
        cost = prompt_tokens * self.INPUT_RATE + completion_tokens * self.OUTPUT_RATE
        with closing(self._open()) as connection, connection:
            row = connection.execute(
                "SELECT reserved_micro,status FROM requests WHERE attempt_id=? AND budget_id=?",
                (attempt_id.hex, self.budget_id),
            ).fetchone()
            if row is None or row[1] != "RESERVED":
                raise BudgetError("Missing or already settled reservation")
            if cost > row[0]:
                raise BudgetError(
                    "Reported usage exceeded its conservative reservation"
                )
            connection.execute(
                "UPDATE requests SET settled_micro=?,prompt_tokens=?,completion_tokens=?,status='SETTLED' WHERE attempt_id=?",
                (cost, prompt_tokens, completion_tokens, attempt_id.hex),
            )
        return self.cny(cost)

    def usage(self, task_id: UUID | None = None):
        where = "budget_id=?" + (" AND task_id=?" if task_id else "")
        values = (self.budget_id, task_id.hex) if task_id else (self.budget_id,)
        with closing(self._open()) as connection:
            row = connection.execute(
                "SELECT count(*),coalesce(sum(prompt_tokens),0),coalesce(sum(completion_tokens),0),coalesce(sum(coalesce(settled_micro,reserved_micro)),0),coalesce(sum(CASE WHEN status='RESERVED' THEN 1 ELSE 0 END),0) FROM requests WHERE "
                + where,
                values,
            ).fetchone()
            pricing_versions = [
                item[0]
                for item in connection.execute(
                    "SELECT DISTINCT pricing_version FROM requests WHERE "
                    + where
                    + " ORDER BY pricing_version",
                    values,
                ).fetchall()
            ]
        return {
            "budget_id": self.budget_id,
            "calls": row[0],
            "prompt_tokens": row[1],
            "completion_tokens": row[2],
            "cost_upper_cny": self.cny(row[3]),
            "unknown_usage_calls": row[4],
            "limit_cny": self.cny(self.limit),
            "pricing_version": self.PRICING_VERSION,
            "pricing_versions": pricing_versions,
        }
