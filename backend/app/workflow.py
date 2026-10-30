"""Bounded engineering plans, explicit conditional stages and pre-call budgets."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from .budget import BudgetExceeded, BudgetLedger
from .hardware import calculate_buck_duty
from .providers.base import ModelRequest, ProviderError
from .schemas import CallRecord, TaskEvent, TaskRecord, utc_now


class WorkflowFailure(Exception):
    """A required stage exhausted its configured attempts or allowance."""


class WorkflowEngine:
    def __init__(
        self,
        providers: dict,
        *,
        ledger: BudgetLedger | None = None,
        daily_budget_usd: Decimal = Decimal("10"),
        monthly_budget_usd: Decimal = Decimal("100"),
        estimated_call_cost_usd: Decimal = Decimal("0.01"),
        checkpoint: Callable[[TaskRecord], None] | None = None,
    ):
        self.providers = providers
        self._temporary_ledger_dir = None
        if ledger is None:
            # Tests/embedders get an isolated ledger. The application always
            # supplies its durable DATA_DIR/studio.sqlite3 ledger explicitly.
            self._temporary_ledger_dir = tempfile.TemporaryDirectory(prefix="engineering-budget-")
            ledger = BudgetLedger(Path(self._temporary_ledger_dir.name) / "ledger.sqlite3")
        self.ledger = ledger
        self.daily_budget_usd = daily_budget_usd
        self.monthly_budget_usd = monthly_budget_usd
        self.estimated_call_cost_usd = estimated_call_cost_usd
        self.checkpoint = checkpoint

    def save(self, task: TaskRecord) -> None:
        task.updated_at = utc_now()
        if self.checkpoint is not None:
            self.checkpoint(task)

    def event(self, task: TaskRecord, stage: str, message: str) -> None:
        task.events.append(TaskEvent(stage=stage, message=message))
        self.save(task)

    @staticmethod
    def build_plan(task: TaskRecord, attachments: list[dict]) -> list[dict]:
        """Simple, inspectable V0.1 planner; no unbounded model discussion.

        Routing is supplied by the model registry. This planner contains no
        vendor/model IDs. Profiles adjust depth; manual/hybrid selections have
        already been resolved by the registry before execution.
        """
        workflow = task.workflow
        roles = getattr(workflow, "role_models", {})
        reviewers = list(workflow.reviewers)
        if getattr(workflow, "profile", "professional") == "quick":
            reviewers = reviewers[:1]
        nodes = []

        def node(stage: str, models: list[str], depends_on: list[str], **extra):
            nodes.append({
                "id": stage, "stage": stage, "models": models,
                "depends_on": depends_on, "status": "pending", **extra,
            })

        node("classification", [getattr(workflow, "orchestrator", None) or reviewers[0]], [])
        dependencies = ["classification"]
        if any(str(file.get("content_type", "")).startswith("image/") for file in attachments):
            node("vision", [workflow.vision_provider], dependencies.copy())
            dependencies.append("vision")
        has_pdf = any(
            file.get("content_type") == "application/pdf"
            or str(file.get("filename", "")).lower().endswith(".pdf")
            for file in attachments
        )
        document_intent = any(
            term in task.prompt.lower() for term in ("datasheet", "手册", "资料", "pdf", "document")
        )
        if has_pdf and (document_intent or getattr(workflow, "profile", "professional") == "expert"):
            node("datasheet", [roles.get("datasheet", reviewers[0])], dependencies.copy())
            dependencies.append("datasheet")
        # Only this allow-listed, deterministic command is executed locally.
        # Uploaded programs and arbitrary expressions never become executable.
        if re.search(r"tool:buck_duty\s*\(", task.prompt, re.IGNORECASE):
            node("calculation", [], dependencies.copy(), tool="buck_duty")
            dependencies.append("calculation")
        node("hardware_analysis", reviewers, dependencies, execution="parallel" if workflow.max_parallel > 1 else "serial")
        needs_validation = getattr(workflow, "profile", "professional") != "quick" or (
            "validator" in getattr(workflow, "locked_roles", [])
        )
        if needs_validation:
            node("validation", [workflow.validator], ["hardware_analysis"])
        node("summary", [workflow.summarizer], ["validation" if needs_validation else "hardware_analysis"])
        return nodes

    @staticmethod
    def _source(previous: str, current: str, has_previous: bool) -> str:
        return current if not has_previous else previous if previous == current else "mixed"

    async def run(self, task: TaskRecord, attachments: list[dict]) -> None:
        task.status = "running"
        task.error = None
        task.plan = self.build_plan(task, attachments)
        self.event(task, "workflow", "Bounded engineering plan started; inspect each call's mock/real provenance")
        semaphore = asyncio.Semaphore(task.workflow.max_parallel)
        rounds = 0

        def update_plan(stage: str, status: str) -> None:
            for node in task.plan:
                if node["stage"] == stage:
                    node["status"] = status
            self.save(task)

        async def call(provider_id: str, stage: str, context: dict | None = None) -> str:
            nonlocal rounds
            request = ModelRequest(stage=stage, prompt=task.prompt, attachments=attachments, context=context or {})
            for attempt in range(1, task.workflow.max_retries + 2):
                started = perf_counter()
                retryable = False
                billing_unknown = False
                error = "Provider call failed"
                reservation_id = None
                stage_record = None
                response = None
                try:
                    async with semaphore:
                        # Guard and reserve atomically immediately before every
                        # attempt, including retries and concurrent reviewers.
                        quote = getattr(task.workflow, "estimated_call_cost_usd", self.estimated_call_cost_usd)
                        reservation_id = self.ledger.reserve(
                            task_id=task.id, provider=provider_id, stage=stage, attempt=attempt,
                            estimate_usd=quote,
                            task_limit_usd=getattr(task.workflow, "max_cost_usd", Decimal("1")),
                            daily_limit_usd=self.daily_budget_usd,
                            monthly_limit_usd=self.monthly_budget_usd,
                            max_calls=getattr(task.workflow, "max_calls", 16),
                        )
                        stage_input = {"prompt": task.prompt, "attachments": attachments, "context": context or {}}
                        stage_record = {
                            "id": uuid4().hex, "stage": stage, "model": provider_id,
                            "attempt": attempt, "round": max(1, rounds), "status": "running",
                            "input": stage_input, "output": None, "started_at": utc_now().isoformat(),
                            "reservation_id": reservation_id, "estimated_cost_usd": str(quote),
                        }
                        task.stages.append(stage_record)
                        self.event(task, stage, f"{provider_id} started (attempt {attempt}); estimated allowance reserved")
                        response = await asyncio.wait_for(
                            self.providers[provider_id].complete(request), timeout=task.workflow.timeout_seconds,
                        )
                    cost_source = getattr(response, "cost_source", "estimate")
                    usage_source = getattr(response, "usage_source", "estimate")
                    unavailable = cost_source in {"unavailable", "unknown"}
                    cost = Decimal(str(response.cost_usd))
                    self.ledger.settle(
                        reservation_id,
                        actual_cost_usd=None if unavailable else cost,
                        input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                        cached_input_tokens=getattr(response, "cached_input_tokens", 0),
                        cost_source=cost_source, usage_source=usage_source,
                    )
                    stage_record.update({
                        "status": "completed", "output": {"text": response.text},
                        "finished_at": utc_now().isoformat(),
                        "usage_source": usage_source, "cost_source": cost_source,
                        "billing_status": "unknown" if unavailable else "reconciled",
                    })
                    provider = self.providers[provider_id]
                    record = CallRecord(
                        stage=stage, provider=getattr(provider, "provider_id", provider_id), model=provider_id,
                        attempt=attempt, status="completed", input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens, cached_input_tokens=getattr(response, "cached_input_tokens", 0),
                        cost_usd=cost, estimated_cost_usd=quote, usage_source=usage_source, cost_source=cost_source,
                        latency_ms=round((perf_counter() - started) * 1000, 3),
                        simulated=response.simulated, output=response.text,
                        stage_input=stage_input, stage_output={"text": response.text},
                    )
                    prior_calls = any(item.status == "completed" for item in task.calls)
                    task.calls.append(record)
                    task.usage.input_tokens += record.input_tokens
                    task.usage.output_tokens += record.output_tokens
                    task.usage.cached_input_tokens += record.cached_input_tokens
                    task.usage.cost_usd += record.cost_usd
                    task.usage.estimated_cost_usd += record.estimated_cost_usd
                    task.usage.simulated = task.usage.simulated and record.simulated
                    task.usage.usage_source = self._source(task.usage.usage_source, usage_source, prior_calls)
                    task.usage.cost_source = self._source(task.usage.cost_source, cost_source, prior_calls)
                    self.event(task, stage, f"{provider_id} completed (attempt {attempt})")
                    return response.text
                except BudgetExceeded as exc:
                    self.event(task, stage, str(exc))
                    raise WorkflowFailure(str(exc)) from None
                except asyncio.CancelledError:
                    if reservation_id is not None:
                        self.ledger.settle(reservation_id, actual_cost_usd=None, usage_source="unknown")
                    if stage_record is not None:
                        stage_record.update(status="cancelled", error="Cancelled; billing may be unknown", finished_at=utc_now().isoformat())
                        self.save(task)
                    raise
                except TimeoutError:
                    retryable = True
                    billing_unknown = True
                    error = "Provider timeout; billing unknown"
                    if reservation_id is not None:
                        self.ledger.settle(reservation_id, actual_cost_usd=None, usage_source="unknown")
                except ProviderError as exc:
                    retryable = exc.retryable
                    billing_unknown = retryable or exc.billed_unknown
                    error = "Provider request rejected"
                    if reservation_id is not None:
                        if billing_unknown:
                            self.ledger.settle(reservation_id, actual_cost_usd=None, usage_source="unknown")
                        else:
                            self.ledger.release(reservation_id)
                except Exception:
                    # Error messages can contain authorization headers or URLs.
                    # An unexpected failure may have reached the provider.
                    error = "Provider call failed; billing unknown"
                    billing_unknown = True
                    if reservation_id is not None:
                        self.ledger.settle(reservation_id, actual_cost_usd=None, usage_source="unknown")
                if stage_record is not None:
                    stage_record.update(status="failed", error=error, finished_at=utc_now().isoformat())
                if reservation_id is not None:
                    task.calls.append(CallRecord(
                        stage=stage, provider=provider_id, model=provider_id, attempt=attempt, status="failed",
                        latency_ms=round((perf_counter() - started) * 1000, 3), error=error,
                        simulated=task.mode == "mock",
                        usage_source="unknown", cost_source="unknown" if billing_unknown else "not_billed",
                        estimated_cost_usd=quote,
                        stage_input=stage_record["input"] if stage_record else {}, stage_output={"error": error},
                    ))
                    task.usage.estimated_cost_usd += quote
                    self.save(task)
                if not retryable or attempt > task.workflow.max_retries:
                    raise WorkflowFailure(f"{stage}/{provider_id}: {error}")
                self.event(task, stage, f"{provider_id} retry scheduled after attempt {attempt}")
                await asyncio.sleep(min(0.05 * 2 ** (attempt - 1), 0.8))
            raise WorkflowFailure("Required model call did not complete")

        try:
            classification_node = next(node for node in task.plan if node["stage"] == "classification")
            update_plan("classification", "running")
            classification = await call(classification_node["models"][0], "classification")
            task.result["classification"] = classification
            update_plan("classification", "completed")
            if any(node["stage"] == "vision" for node in task.plan):
                update_plan("vision", "running")
                task.result["image_analysis"] = await call(task.workflow.vision_provider, "vision", {
                    "classification": classification,
                    "original_file_ids": [file["id"] for file in attachments],
                    "mock_metadata_only": getattr(task, "mode", "mock") == "mock",
                })
                update_plan("vision", "completed")
            document_node = next((node for node in task.plan if node["stage"] == "datasheet"), None)
            if document_node is not None:
                update_plan("datasheet", "running")
                task.result["datasheet"] = await call(document_node["models"][0], "datasheet", {
                    "classification": classification,
                    "original_file_ids": [file["id"] for file in attachments],
                    "scope": "Metadata-only in mock mode; no unsourced parameters may be fabricated",
                })
                update_plan("datasheet", "completed")
            if any(node["stage"] == "calculation" for node in task.plan):
                update_plan("calculation", "running")
                match = re.search(
                    r"tool:buck_duty\s*\(\s*vin\s*=\s*([0-9.eE+-]+)\s*,\s*vout\s*=\s*([0-9.eE+-]+)\s*\)",
                    task.prompt, re.IGNORECASE,
                )
                if match is None:
                    raise WorkflowFailure("Use the allow-listed command tool:buck_duty(vin=24,vout=5)")
                try:
                    task.result["calculation"] = calculate_buck_duty(match.group(1), match.group(2))
                except ValueError:
                    raise WorkflowFailure("Buck calculation inputs are invalid; require finite Vin > Vout > 0") from None
                task.stages.append({
                    "id": uuid4().hex, "stage": "calculation", "tool": "buck_duty", "status": "completed",
                    "input": {"vin_v": match.group(1), "vout_v": match.group(2)},
                    "output": task.result["calculation"], "provenance": "deterministic_python_calculation",
                })
                update_plan("calculation", "completed")
            reviewers = next(node["models"] for node in task.plan if node["stage"] == "hardware_analysis")
            max_rounds = getattr(task.workflow, "max_rounds", 1)
            while rounds < max_rounds:
                rounds += 1
                update_plan("hardware_analysis", "running")
                self.event(task, "hardware_analysis", f"Hardware reviews started (bounded round {rounds}/{max_rounds})")
                results = await asyncio.gather(*[
                    call(provider_id, "hardware_analysis", {
                        "classification": classification,
                        "image_analysis": task.result.get("image_analysis"),
                        "datasheet": task.result.get("datasheet"),
                        "calculation": task.result.get("calculation"),
                        "previous_validation": task.result.get("validation"),
                        "original_file_ids": [file["id"] for file in attachments],
                        "evidence_policy": "Separate facts, theory, simulation, measurements and unverified hypotheses; never majority-vote physical truth",
                    }) for provider_id in reviewers
                ], return_exceptions=True)
                task.result["analyses"] = [
                    {"provider": provider_id, "output": result}
                    for provider_id, result in zip(reviewers, results, strict=True) if isinstance(result, str)
                ]
                task.result.setdefault("analysis_rounds", []).append({"round": rounds, "analyses": task.result["analyses"]})
                failure = next((item for item in results if isinstance(item, BaseException)), None)
                if failure is not None:
                    raise WorkflowFailure(str(failure))
                update_plan("hardware_analysis", "completed")
                if not any(node["stage"] == "validation" for node in task.plan):
                    break
                update_plan("validation", "running")
                validation = await call(task.workflow.validator, "validation", {
                    "analyses": task.result["analyses"], "calculation": task.result.get("calculation"),
                    "original_file_ids": [file["id"] for file in attachments],
                    "evidence_policy": "Prefer physical laws, measured data and cited sources; list unresolved differences",
                })
                task.result["validation"] = validation
                update_plan("validation", "completed")
                try:
                    revision = json.loads(validation).get("needs_revision") is True
                except (ValueError, AttributeError):
                    revision = False
                if not revision:
                    break
                if rounds >= max_rounds:
                    raise WorkflowFailure("Independent review requested revision but the maximum round limit was reached")
            update_plan("summary", "running")
            task.result["summary"] = await call(task.workflow.summarizer, "summary", {
                "classification": classification, "analyses": task.result["analyses"],
                "validation": task.result.get("validation"), "calculation": task.result.get("calculation"),
                "datasheet": task.result.get("datasheet"),
                "completion_criteria": "Deliver findings with evidence provenance and explicit unresolved assumptions; no unsupported hardware approval",
            })
            update_plan("summary", "completed")
            task.status = "completed"
            task.recoverable = False
            self.event(task, "workflow", "Workflow completed; inspect provenance and unresolved engineering assumptions")
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.error = "Task cancelled; in-flight billing reservations remain conservative until reconciled"
            task.recoverable = True
            for node in task.plan:
                if node["status"] == "running":
                    node["status"] = "cancelled"
            self.event(task, "workflow", task.error)
            raise
        except WorkflowFailure as exc:
            task.status = "failed"
            task.error = str(exc)
            task.recoverable = True
            for node in task.plan:
                if node["status"] == "running":
                    node["status"] = "failed"
            self.event(task, "workflow", task.error)
        except Exception:
            task.status = "failed"
            task.error = "Workflow failed unexpectedly"
            task.recoverable = True
            for node in task.plan:
                if node["status"] == "running":
                    node["status"] = "failed"
            self.event(task, "workflow", task.error)
