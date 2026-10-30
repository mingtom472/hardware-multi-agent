import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from app.providers.base import ModelRequest, ModelResponse, ProviderError
from app.schemas import TaskRecord, WorkflowConfig
from app.workflow import WorkflowEngine


@dataclass
class Calls:
    counts: Counter = field(default_factory=Counter)
    active: int = 0
    peak: int = 0
    fail_stage: str | None = None
    failures: int = 0
    retryable: bool = False
    unexpected: bool = False
    slow_stage: str | None = None


class ControlledProvider:
    """Deterministic substitute measuring concurrency and failure policy."""

    def __init__(self, provider_id: str, calls: Calls):
        self.provider_id = provider_id
        self.calls = calls

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.counts[(self.provider_id, request.stage)] += 1
        if request.stage == self.calls.fail_stage and self.calls.failures:
            self.calls.failures -= 1
            if self.calls.unexpected:
                raise RuntimeError("private-secret-that-must-not-be-disclosed")
            raise ProviderError("private-secret-that-must-not-be-disclosed", self.calls.retryable)
        if request.stage == self.calls.slow_stage:
            await asyncio.sleep(0.1)
        if request.stage == "hardware_analysis":
            self.calls.active += 1
            self.calls.peak = max(self.calls.peak, self.calls.active)
            try:
                await asyncio.sleep(0.01)
            finally:
                self.calls.active -= 1
        text = (
            json.dumps({"category": "power_electronics", "simulated": True})
            if request.stage == "classification"
            else f"simulated {request.stage} from {self.provider_id}"
        )
        return ModelResponse(text, 10, 5, Decimal("0.0003"), simulated=True)


def execute(calls: Calls, **workflow_options) -> TaskRecord:
    task = TaskRecord(
        id="test-task", prompt="Review Buck converter", workflow=WorkflowConfig(**workflow_options)
    )
    providers = {
        name: ControlledProvider(name, calls)
        for name in ("openai", "claude", "deepseek", "doubao", "glm")
    }
    asyncio.run(WorkflowEngine(providers).run(task, []))
    return task


def test_parallel_reviews_honor_limit_and_accumulate_all_usage():
    calls = Calls()
    task = execute(calls, max_parallel=2)
    assert task.status == "completed"
    assert calls.peak == 2
    assert all(calls.counts[(name, "hardware_analysis")] == 1 for name in task.workflow.reviewers)
    assert task.result["summary"]
    completed = [record for record in task.calls if record.status == "completed"]
    assert len(completed) == 6  # classification + three reviewers + validation + summary
    assert task.usage.input_tokens == 10 * len(completed)
    assert task.usage.output_tokens == 5 * len(completed)
    assert task.usage.cost_usd == Decimal("0.0003") * len(completed)
    assert task.usage.simulated


def test_parallel_limit_one_serializes_reviews():
    calls = Calls()
    task = execute(calls, max_parallel=1)
    assert task.status == "completed"
    assert calls.peak == 1


def test_retryable_errors_recover_without_inventing_failed_call_usage():
    task = execute(Calls(fail_stage="classification", failures=2, retryable=True), max_retries=2)
    assert task.status == "completed"
    attempts = [record for record in task.calls if record.stage == "classification"]
    assert [record.attempt for record in attempts] == [1, 2, 3]
    assert [record.status for record in attempts] == ["failed", "failed", "completed"]
    assert all(record.input_tokens == record.output_tokens == 0 for record in attempts[:2])
    assert task.usage.cost_usd == sum(record.cost_usd for record in task.calls)


@pytest.mark.parametrize("unexpected", [False, True])
def test_permanent_failure_stops_pipeline_and_sanitizes_errors(unexpected):
    calls = Calls(fail_stage="classification", failures=10, unexpected=unexpected)
    task = execute(calls, max_retries=3)
    assert task.status == "failed"
    assert len(task.calls) == 1
    assert task.error
    assert "summary" not in task.result
    assert "private-secret" not in task.model_dump_json()


def test_exhausted_timeout_is_terminal_and_bounded():
    calls = Calls(slow_stage="classification")
    task = execute(calls, max_retries=1, timeout_seconds=0.005)
    assert task.status == "failed"
    assert calls.counts[("openai", "classification")] == 2
    assert len(task.calls) == 2
    assert all(record.status == "failed" for record in task.calls)
    assert task.usage.input_tokens == task.usage.output_tokens == 0


def test_validation_failure_prevents_successful_summary():
    calls = Calls(fail_stage="validation", failures=1)
    task = execute(calls)
    assert task.status == "failed"
    assert len([call for call in task.calls if call.stage == "hardware_analysis"]) == 3
    assert not any(stage == "summary" for _, stage in calls.counts)
    assert "summary" not in task.result
