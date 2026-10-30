import asyncio
import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.budget import BudgetExceeded, BudgetLedger
from app.providers.base import ModelResponse
from app.schemas import TaskRecord, WorkflowConfig
from app.workflow import WorkflowEngine

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def reserve(ledger, task_id="task", **overrides):
    values = {
        "task_id": task_id,
        "provider": "configured-model",
        "stage": "analysis",
        "attempt": 1,
        "estimate_usd": Decimal("0.10"),
        "task_limit_usd": Decimal("1"),
        "daily_limit_usd": Decimal("10"),
        "monthly_limit_usd": Decimal("100"),
        "max_calls": 10,
        "now": NOW,
    }
    values.update(overrides)
    return ledger.reserve(**values)


@pytest.fixture
def ledger(tmp_path):
    return BudgetLedger(tmp_path / "studio.sqlite3")


def test_reservation_is_durable_before_call_and_reconciles_decimal_usage(ledger):
    reservation_id = reserve(ledger)
    report = BudgetLedger(ledger.path).report("task", now=NOW)
    assert report["task"]["spent_usd"] == "0"
    assert Decimal(report["task"]["reserved_usd"]) == Decimal("0.10")
    ledger.settle(
        reservation_id,
        actual_cost_usd=Decimal("0.01234567"),
        input_tokens=100,
        output_tokens=25,
        cached_input_tokens=30,
        cost_source="estimate",
        usage_source="provider",
    )
    report = BudgetLedger(ledger.path).report("task", now=NOW)
    assert Decimal(report["task"]["spent_usd"]) == Decimal("0.01234567")
    assert report["task"]["reserved_usd"] == "0"
    assert report["task"]["cached_input_tokens"] == 30
    assert report["entries"][0]["usage_source"] == "provider"


@pytest.mark.parametrize("period", ["task", "day", "month"])
def test_each_budget_prevents_the_next_call_reservation(ledger, period):
    limits = {
        "task_limit_usd": Decimal("10"),
        "daily_limit_usd": Decimal("10"),
        "monthly_limit_usd": Decimal("10"),
    }
    limits[{"task": "task_limit_usd", "day": "daily_limit_usd", "month": "monthly_limit_usd"}[period]] = Decimal("0.10")
    reserve(ledger, **limits)
    with pytest.raises(BudgetExceeded):
        reserve(ledger, task_id="task" if period == "task" else "another-task", **limits)
    assert len(ledger.export_rows()) == 1


def test_concurrent_different_tasks_cannot_oversubscribe_shared_daily_budget(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    ledgers = [BudgetLedger(path) for _ in range(12)]

    def attempt(index):
        try:
            reserve(ledgers[index], str(index), daily_limit_usd=Decimal("0.30"))
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(attempt, range(12)))
    assert sum(outcomes) == 3
    assert Decimal(ledgers[0].report(now=NOW)["day"]["total_usd"]) == Decimal("0.30")


def test_retry_attempt_limit_counts_known_nonbillable_failures(ledger):
    first = reserve(ledger, max_calls=1)
    ledger.release(first)
    assert Decimal(ledger.report("task", now=NOW)["task"]["total_usd"]) == 0
    with pytest.raises(BudgetExceeded, match="call-count|call count"):
        reserve(ledger, max_calls=1)


def test_timeout_or_unpriced_call_retains_quote_until_reconciled(ledger):
    reservation = reserve(ledger, task_limit_usd=Decimal("0.10"))
    ledger.settle(reservation, actual_cost_usd=None, usage_source="unknown")
    ledger.release(reservation)  # Unknown charges cannot be silently made free.
    report = ledger.report("task", now=NOW)
    assert report["task"]["unknown_calls"] == 1
    assert Decimal(report["task"]["reserved_usd"]) == Decimal("0.10")
    with pytest.raises(BudgetExceeded):
        reserve(ledger, task_limit_usd=Decimal("0.10"))
    ledger.settle(reservation, actual_cost_usd=Decimal("0.005"), cost_source="provider")
    reserve(ledger, estimate_usd=Decimal("0.09"), task_limit_usd=Decimal("0.10"))


def test_actual_charge_over_quote_is_honest_and_blocks_later_calls(ledger):
    reservation = reserve(ledger, task_limit_usd=Decimal("0.10"))
    ledger.settle(reservation, actual_cost_usd=Decimal("0.20"), cost_source="provider")
    assert Decimal(ledger.report("task", now=NOW)["task"]["spent_usd"]) == Decimal("0.20")
    with pytest.raises(BudgetExceeded):
        reserve(ledger, estimate_usd=Decimal("0.001"), task_limit_usd=Decimal("0.10"))


def test_utc_day_reset_does_not_reset_shared_month_allowance(ledger):
    reserve(ledger, daily_limit_usd=Decimal("0.10"), monthly_limit_usd=Decimal("0.10"))
    with pytest.raises(BudgetExceeded, match="Month"):
        reserve(
            ledger, "tomorrow", now=datetime(2026, 10, 31, tzinfo=UTC),
            daily_limit_usd=Decimal("0.10"), monthly_limit_usd=Decimal("0.10"),
        )
    reserve(
        ledger, "next-month", now=datetime(2026, 11, 1, tzinfo=UTC),
        daily_limit_usd=Decimal("0.10"), monthly_limit_usd=Decimal("0.10"),
    )
    assert len(ledger.export_rows()) == 2


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-0.1", "0"])
def test_invalid_estimates_are_rejected_without_ledger_side_effects(ledger, amount):
    with pytest.raises(ValueError):
        reserve(ledger, estimate_usd=Decimal(amount))
    assert ledger.export_rows() == []


def test_csv_export_preserves_provenance_and_exact_costs(ledger):
    reservation = reserve(ledger)
    ledger.settle(reservation, actual_cost_usd=Decimal("0.00000001"), cost_source="estimate", usage_source="provider")
    rows = list(csv.DictReader(io.StringIO(ledger.export_csv())))
    assert len(rows) == 1
    assert Decimal(rows[0]["actual_cost_usd"]) == Decimal("0.00000001")
    assert rows[0]["cost_source"] == "estimate"
    assert rows[0]["usage_source"] == "provider"


class CountingProvider:
    provider_id = "test"

    def __init__(self, *, unpriced=False, slow=False):
        self.requests = []
        self.unpriced = unpriced
        self.slow = slow
        self.started = asyncio.Event()

    async def complete(self, request):
        self.requests.append(request)
        self.started.set()
        if self.slow:
            await asyncio.sleep(10)
        return ModelResponse(
            "mock result", 5, 2, Decimal("0") if self.unpriced else Decimal("0.001"),
            cost_source="unavailable" if self.unpriced else "estimate",
            usage_source="provider", simulated=False,
        )


def task(**options):
    return TaskRecord(id="budgeted-task", prompt="Review hardware", workflow=WorkflowConfig(**options))


def engine_for(ledger, provider, **kwargs):
    return WorkflowEngine(
        {key: provider for key in ("openai", "claude", "deepseek", "doubao", "glm")},
        ledger=ledger, **kwargs,
    )


def test_task_preflight_stops_provider_before_any_call_if_quote_exceeds_budget(ledger):
    provider = CountingProvider()
    record = task(max_cost_usd=Decimal("0.001"), estimated_call_cost_usd=Decimal("0.01"))
    asyncio.run(engine_for(ledger, provider).run(record, []))
    assert record.status == "failed"
    assert provider.requests == []
    assert "budget" in record.error.lower()


def test_max_call_limit_includes_parallel_attempts_and_blocks_summary(ledger):
    provider = CountingProvider()
    record = task(max_calls=2)
    asyncio.run(engine_for(ledger, provider).run(record, []))
    assert record.status == "failed"
    assert len(provider.requests) == 2
    assert len(ledger.export_rows(record.id)) == 2
    assert "summary" not in record.result


def test_missing_pricing_cannot_create_free_unlimited_calls(ledger):
    provider = CountingProvider(unpriced=True)
    record = task(max_cost_usd=Decimal("0.01"), estimated_call_cost_usd=Decimal("0.01"))
    asyncio.run(engine_for(ledger, provider).run(record, []))
    assert record.status == "failed"
    assert len(provider.requests) == 1
    assert ledger.report(record.id)["task"]["unknown_calls"] == 1


def test_quick_plan_uses_only_necessary_roles_and_saves_stage_inputs_outputs(ledger):
    provider = CountingProvider()
    record = task(profile="quick")
    snapshots = []
    asyncio.run(engine_for(ledger, provider, checkpoint=lambda state: snapshots.append(state.model_dump())).run(record, []))
    assert record.status == "completed"
    assert [request.stage for request in provider.requests] == ["classification", "hardware_analysis", "summary"]
    assert all(stage["input"]["prompt"] == record.prompt for stage in record.stages)
    assert all(stage["output"]["text"] for stage in record.stages)
    assert snapshots[0]["status"] == "running"
    assert snapshots[-1]["status"] == "completed"


def test_cancellation_persists_terminal_state_and_conservative_reservation(ledger):
    async def execute():
        provider = CountingProvider(slow=True)
        record = task()
        job = asyncio.create_task(engine_for(ledger, provider).run(record, []))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        return record

    record = asyncio.run(execute())
    assert record.status == "cancelled"
    assert record.recoverable
    assert record.stages[0]["status"] == "cancelled"
    assert ledger.report(record.id)["task"]["unknown_calls"] == 1
