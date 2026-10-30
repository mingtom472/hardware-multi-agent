"""Transactional, persistent budget reservations in USD.

Reservations are estimates made *before* calls. A vendor can charge more than an
estimate; reconciliation records that honestly and prevents subsequent calls.
Timeout/cancellation charges remain unknown and conservatively retain the quote.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4


class BudgetExceeded(Exception):
    """A pre-call budget or attempt-count guard rejected a new reservation."""


def _money(value: Decimal | str | int | float, name: str, *, positive: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite USD amount") from None
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} USD amount")
    return amount


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class BudgetLedger:
    """One SQLite transaction protects task, UTC-day and UTC-month allowances.

    Connections are short-lived so multiple API workers/processes can share the
    guard. Workflow task execution remains single-process until a real queue is
    introduced. Decimal strings avoid SQLite's binary-floating-point sums.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS budget_entries (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    day TEXT NOT NULL,
                    month TEXT NOT NULL,
                    estimate_usd TEXT NOT NULL,
                    accounted_usd TEXT NOT NULL,
                    actual_cost_usd TEXT,
                    status TEXT NOT NULL,
                    cost_source TEXT NOT NULL DEFAULT 'estimated',
                    usage_source TEXT NOT NULL DEFAULT 'unknown',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS budget_task_idx ON budget_entries(task_id);
                CREATE INDEX IF NOT EXISTS budget_day_idx ON budget_entries(day);
                CREATE INDEX IF NOT EXISTS budget_month_idx ON budget_entries(month);
                """
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _total(rows) -> Decimal:
        return sum((Decimal(row["accounted_usd"]) for row in rows), Decimal("0"))

    def reserve(
        self,
        *,
        task_id: str,
        provider: str,
        stage: str,
        attempt: int,
        estimate_usd: Decimal,
        task_limit_usd: Decimal,
        daily_limit_usd: Decimal,
        monthly_limit_usd: Decimal,
        max_calls: int,
        now: datetime | None = None,
    ) -> str:
        estimate = _money(estimate_usd, "estimate_usd", positive=True)
        limits = {
            "task": _money(task_limit_usd, "task_limit_usd"),
            "day": _money(daily_limit_usd, "daily_limit_usd"),
            "month": _money(monthly_limit_usd, "monthly_limit_usd"),
        }
        if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        timestamp = _utc(now)
        day, month = timestamp.date().isoformat(), timestamp.strftime("%Y-%m")
        reservation_id = uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task_rows = connection.execute(
                    "SELECT accounted_usd FROM budget_entries WHERE task_id = ?", (task_id,)
                ).fetchall()
                if len(task_rows) >= max_calls:
                    raise BudgetExceeded("Task maximum model-call count reached")
                rows_by_period = {
                    "task": task_rows,
                    "day": connection.execute(
                        "SELECT accounted_usd FROM budget_entries WHERE day = ?", (day,)
                    ).fetchall(),
                    "month": connection.execute(
                        "SELECT accounted_usd FROM budget_entries WHERE month = ?", (month,)
                    ).fetchall(),
                }
                for period, rows in rows_by_period.items():
                    if self._total(rows) + estimate > limits[period]:
                        raise BudgetExceeded(f"{period.capitalize()} budget cannot reserve another call")
                connection.execute(
                    """INSERT INTO budget_entries
                    (id, task_id, provider, stage, attempt, created_at, day, month,
                     estimate_usd, accounted_usd, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')""",
                    (
                        reservation_id, task_id, provider, stage, attempt,
                        timestamp.isoformat(), day, month, str(estimate), str(estimate),
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return reservation_id

    def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_usd: Decimal | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        cost_source: str = "estimated",
        usage_source: str = "estimated",
    ) -> None:
        actual = None if actual_cost_usd is None else _money(actual_cost_usd, "actual_cost_usd")
        tokens = (input_tokens, output_tokens, cached_input_tokens)
        if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in tokens):
            raise ValueError("Token counts must be nonnegative integers")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM budget_entries WHERE id = ?", (reservation_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("Unknown budget reservation")
                if row["status"] == "released":
                    raise ValueError("Released budget reservations cannot be reconciled")
                accounted = Decimal(row["estimate_usd"]) if actual is None else actual
                if row["status"] == "settled":
                    if actual is not None and Decimal(row["actual_cost_usd"]) == actual:
                        connection.execute("COMMIT")
                        return
                    raise ValueError("Settled costs are immutable")
                connection.execute(
                    """UPDATE budget_entries SET status = ?, accounted_usd = ?, actual_cost_usd = ?,
                       cost_source = ?, usage_source = ?, input_tokens = ?, output_tokens = ?,
                       cached_input_tokens = ? WHERE id = ?""",
                    (
                        "unknown" if actual is None else "settled", str(accounted),
                        None if actual is None else str(actual),
                        "unknown" if actual is None else cost_source,
                        usage_source, input_tokens, output_tokens, cached_input_tokens, reservation_id,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def release(self, reservation_id: str) -> None:
        """Release a known non-billable failure; the attempted call still counts."""
        with self._connection() as connection:
            connection.execute(
                """UPDATE budget_entries SET status = 'released', accounted_usd = '0',
                   actual_cost_usd = '0', cost_source = 'not_billed', usage_source = 'unknown'
                   WHERE id = ? AND status = 'reserved'""", (reservation_id,)
            )

    @staticmethod
    def _aggregate(rows: list[dict]) -> dict:
        spent = sum(
            (Decimal(row["accounted_usd"]) for row in rows if row["status"] == "settled"),
            Decimal("0"),
        )
        reserved = sum(
            (Decimal(row["accounted_usd"]) for row in rows if row["status"] in {"reserved", "unknown"}),
            Decimal("0"),
        )
        return {
            "spent_usd": str(spent),
            "reserved_usd": str(reserved),
            "total_usd": str(spent + reserved),
            "calls": len(rows),
            "unknown_calls": sum(row["status"] == "unknown" for row in rows),
            "input_tokens": sum(row["input_tokens"] for row in rows),
            "output_tokens": sum(row["output_tokens"] for row in rows),
            "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows),
        }

    def export_rows(self, task_id: str | None = None) -> list[dict]:
        with self._connection() as connection:
            if task_id is None:
                rows = connection.execute("SELECT * FROM budget_entries ORDER BY created_at, id").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM budget_entries WHERE task_id = ? ORDER BY created_at, id", (task_id,)
                ).fetchall()
        return [dict(row) for row in rows]

    def report(self, task_id: str | None = None, now: datetime | None = None) -> dict:
        timestamp = _utc(now)
        rows = self.export_rows()
        task_rows = [row for row in rows if task_id is None or row["task_id"] == task_id]
        return {
            "currency": "USD", "generated_at": timestamp.isoformat(), "task_id": task_id,
            "task": self._aggregate(task_rows),
            "day": self._aggregate([row for row in rows if row["day"] == timestamp.date().isoformat()]),
            "month": self._aggregate([row for row in rows if row["month"] == timestamp.strftime("%Y-%m")]),
            "entries": task_rows,
        }

    def summary(self, now: datetime | None = None) -> dict:
        return self.report(now=now)

    def export_csv(self, task_id: str | None = None) -> str:
        rows = self.export_rows(task_id)
        columns = [
            "id", "task_id", "provider", "stage", "attempt", "created_at", "day", "month",
            "estimate_usd", "accounted_usd", "actual_cost_usd", "status", "cost_source", "usage_source",
            "input_tokens", "output_tokens", "cached_input_tokens",
        ]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()
