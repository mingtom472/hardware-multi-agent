import asyncio
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from .budget import BudgetExceeded, BudgetLedger
from .config import Settings
from .persistence import SQLiteTaskStore
from .providers import ProviderError, build_providers, provider_catalog
from .registry import ModelRegistry, RegistryError, RegistryUpdate
from .routing import resolve_workflow
from .schemas import CreateTask, FileMetadata, TaskRecord, WorkflowConfig, utc_now
from .storage import LocalFileStore, StorageError
from .workflow import WorkflowEngine


def create_app(settings: Settings | None = None, providers: dict | None = None) -> FastAPI:
    settings = settings or Settings()
    tasks: dict[str, TaskRecord] = {}
    jobs: dict[str, asyncio.Task] = {}
    cancelled_by_user: set[str] = set()

    def make_engine():
        runtime = providers if providers is not None else app.state.registry.runtime_providers(settings.model_mode)
        return WorkflowEngine(
            runtime, ledger=app.state.ledger,
            daily_budget_usd=settings.daily_budget_usd,
            monthly_budget_usd=settings.monthly_budget_usd,
            estimated_call_cost_usd=settings.estimated_call_cost_usd,
            checkpoint=app.state.store.save,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_path = settings.data_dir / "studio.sqlite3"
        app.state.store = SQLiteTaskStore(db_path)
        app.state.store.recover_interrupted()
        tasks.update({task.id: task for task in reversed(app.state.store.list(settings.max_tasks))})
        app.state.registry = ModelRegistry(app.state.store)
        app.state.ledger = BudgetLedger(db_path)
        app.state.files = LocalFileStore(settings)
        default_workflow = json.loads(settings.workflow_path.read_text(encoding="utf-8"))
        default_workflow.setdefault("max_cost_usd", settings.default_task_budget_usd)
        default_workflow.setdefault("estimated_call_cost_usd", settings.estimated_call_cost_usd)
        app.state.workflow = WorkflowConfig.model_validate(default_workflow)
        app.state.engine = make_engine()
        try:
            yield
        finally:
            running = list(jobs.values())
            for job in running:
                job.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    app = FastAPI(
        title="Engineering AI Studio", version="0.1.0",
        description="Local engineering workflows with explicit mock/live provenance and bounded budgets.",
        lifespan=lifespan,
    )
    app.state.settings, app.state.tasks, app.state.jobs = settings, tasks, jobs
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        # FastAPI's default errors include rejected inputs, which could contain keys.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
            for error in exc.errors()
        ]})

    @app.exception_handler(StorageError)
    async def storage_error_handler(_request: Request, exc: StorageError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(RegistryError)
    async def registry_error_handler(_request: Request, exc: RegistryError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": settings.model_mode}

    @app.get("/api/providers")
    async def providers_info():
        return provider_catalog(settings.model_mode)

    @app.get("/api/models")
    async def models_info():
        return app.state.registry.public(settings.model_mode)

    @app.put("/api/models")
    async def update_models(payload: RegistryUpdate):
        app.state.registry.update(payload)
        return app.state.registry.public(settings.model_mode)

    @app.post("/api/models/{model_id}/test")
    async def test_model(model_id: str):
        entry = app.state.registry.get(model_id)
        expected_config = entry.model_dump(mode="json", exclude={"verified", "verified_capabilities"})
        adapter = app.state.registry.adapter_for(entry, verified=False)
        try:
            reservation = app.state.ledger.reserve(
                task_id=f"connection-test-{model_id}-{uuid4().hex}", provider=entry.provider,
                stage="connection_test", attempt=1,
                estimate_usd=settings.estimated_call_cost_usd,
                task_limit_usd=settings.default_task_budget_usd,
                daily_limit_usd=settings.daily_budget_usd,
                monthly_limit_usd=settings.monthly_budget_usd, max_calls=1,
            )
        except BudgetExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        try:
            result = await adapter.test_connection()
        except ProviderError as exc:
            if exc.retryable or exc.billed_unknown:
                app.state.ledger.settle(reservation, actual_cost_usd=None, usage_source="unknown")
            else:
                app.state.ledger.release(reservation)
            raise HTTPException(status_code=502, detail={
                "message": "Provider connection test failed; check configured model ID, endpoint and account access",
                "code": exc.code, "status_code": exc.status_code,
            }) from None
        except BaseException:
            app.state.ledger.settle(reservation, actual_cost_usd=None, usage_source="unknown")
            raise
        estimated = result.get("estimated_cost_usd")
        app.state.ledger.settle(
            reservation, actual_cost_usd=Decimal(estimated) if estimated is not None else None,
            input_tokens=result.get("input_tokens", 0), output_tokens=result.get("output_tokens", 0),
            cached_input_tokens=result.get("cached_input_tokens", 0),
            cost_source=result.get("cost_source", "unavailable"), usage_source=result.get("usage_source", "unknown"),
        )
        app.state.registry.mark_verified(model_id, expected_config=expected_config)
        return result

    @app.get("/api/workflows/default", response_model=WorkflowConfig)
    async def default_workflow():
        return app.state.workflow

    @app.get("/api/capabilities")
    async def capabilities():
        return {
            "mode": settings.model_mode,
            "implemented": ["mock_and_live_text_workflow", "role_based_model_routing", "streaming_upload", "sqlite_task_history", "retry_cancel_budget_tracking"],
            "reserved": ["postgresql", "redis_queue", "object_storage", "resumable_multipart_upload", "real_vision", "datasheet_parsing", "rag", "ngspice", "embedded_software_agent", "agent_sdk_runtime", "image_generation", "human_review"],
            "limits": {name: getattr(settings, name) for name in [
                "max_upload_bytes", "max_storage_bytes", "max_stored_files", "upload_chunk_bytes",
                "max_files_per_task", "max_active_tasks", "max_tasks",
            ]},
            "storage": app.state.files.disk_status(),
            "storage_backend": "local", "task_backend": "sqlite",
            "uploads_are_executed": False,
            "vision_input": "mock_metadata_only; live_vision_unimplemented",
            "authentication": "not_implemented_local_development_only",
        }

    @app.get("/api/costs")
    async def costs(task_id: str | None = None):
        return app.state.ledger.report(task_id)

    @app.get("/api/costs/export", response_class=PlainTextResponse)
    async def export_costs(task_id: str | None = None):
        return PlainTextResponse(app.state.ledger.export_csv(task_id), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="cost-report.csv"'})

    @app.post("/api/uploads", response_model=FileMetadata, status_code=201)
    async def upload(request: Request, filename: Annotated[str, Query(min_length=1, max_length=1024)]):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
            if declared_size < 0:
                raise HTTPException(status_code=400, detail="Invalid Content-Length")
            if settings.max_upload_bytes is not None and declared_size > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="File exceeds administrator-configured upload limit")
        return await app.state.files.put(request.stream(), filename, request.headers.get("content-type", "application/octet-stream"))

    @app.get("/api/uploads", response_model=list[FileMetadata])
    async def list_files():
        return app.state.files.list()

    @app.get("/api/uploads/{file_id}", response_model=FileMetadata)
    async def get_file(file_id: str):
        metadata = app.state.files.get(file_id)
        if metadata is None:
            raise HTTPException(status_code=404, detail="File not found")
        return metadata

    @app.delete("/api/uploads/{file_id}")
    async def delete_file(file_id: str):
        if app.state.store.references_file(file_id, active_only=True):
            raise HTTPException(status_code=409, detail="File is referenced by an active task; cancel the task first")
        if not await app.state.files.delete(file_id):
            raise HTTPException(status_code=404, detail="File not found")
        return {"deleted": True, "id": file_id}

    def get_record(task_id: str) -> TaskRecord:
        record = tasks.get(task_id) or app.state.store.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return record

    async def execute(record: TaskRecord, attachments: list[dict], engine: WorkflowEngine):
        try:
            await engine.run(record, attachments)
        finally:
            if record.id in cancelled_by_user:
                record.status, record.error, record.recoverable = "cancelled", "Task cancelled by user", True
                cancelled_by_user.discard(record.id)
            elif record.status in {"queued", "running"}:
                record.status, record.error, record.recoverable = "failed", "Service stopped during execution; retry explicitly", True
            elif record.status in {"failed", "cancelled"}:
                record.recoverable = True
            record.updated_at = utc_now()
            app.state.store.save(record)

    async def submit(payload: CreateTask, parent_task_id: str | None = None):
        if len(payload.file_ids) > settings.max_files_per_task:
            raise HTTPException(status_code=422, detail="Too many files attached to task")
        if len(payload.file_ids) != len(set(payload.file_ids)):
            raise HTTPException(status_code=422, detail="Duplicate file IDs are not allowed")
        attachments = []
        for file_id in payload.file_ids:
            metadata = app.state.files.get(file_id)
            if metadata is None:
                raise HTTPException(status_code=404, detail="Attached file not found")
            attachments.append(metadata.model_dump(mode="json"))
        if len(jobs) >= settings.max_active_tasks:
            raise HTTPException(status_code=429, detail="Too many active tasks; retry later")
        workflow = (payload.workflow or app.state.workflow).model_copy(deep=True)
        if payload.workflow is not None:
            if "max_cost_usd" not in payload.workflow.model_fields_set:
                workflow.max_cost_usd = app.state.workflow.max_cost_usd
            if "estimated_call_cost_usd" not in payload.workflow.model_fields_set:
                workflow.estimated_call_cost_usd = app.state.workflow.estimated_call_cost_usd
        if workflow.require_human_review:
            raise HTTPException(status_code=422, detail="Human-review execution is reserved, not implemented in V0.1")
        known = set(app.state.registry.models) | set(build_providers("mock"))
        if providers is not None:
            known |= set(providers)
        explicit = workflow.reviewers + [workflow.vision_provider, workflow.validator, workflow.summarizer]
        if any(model_id not in known for model_id in explicit):
            raise HTTPException(status_code=422, detail="Workflow references an unknown model registry ID")
        if providers is None:
            workflow = resolve_workflow(workflow, payload.prompt, attachments, app.state.registry)
        record = TaskRecord(id=uuid4().hex, prompt=payload.prompt, file_ids=payload.file_ids,
                            workflow=workflow, mode=settings.model_mode, parent_task_id=parent_task_id)
        record.plan = WorkflowEngine.build_plan(record, attachments)
        required = {model_id for node in record.plan for model_id in node["models"]}
        if providers is None and settings.model_mode == "real":
            if any(item["content_type"].startswith("image/") for item in attachments):
                raise HTTPException(status_code=422, detail="Live image inputs are not implemented; original images remain stored locally")
            for model_id in required:
                entry = app.state.registry.get(model_id)
                app.state.registry.adapter_for(entry)
                if entry.pricing.input_per_million is None or entry.pricing.output_per_million is None:
                    raise HTTPException(status_code=422, detail="Real task models require configured input/output pricing for budget estimates")
        engine = make_engine()
        if required - set(engine.providers):
            raise HTTPException(status_code=422, detail="Selected workflow model is disabled, reserved or unavailable")
        app.state.engine = engine
        if len(tasks) >= settings.max_tasks:
            oldest = next((key for key, value in tasks.items() if value.status in {"completed", "failed", "cancelled"}), None)
            if oldest is not None:
                del tasks[oldest]
        tasks[record.id] = record
        app.state.store.save(record)
        job = asyncio.create_task(execute(record, attachments, engine))
        jobs[record.id] = job

        def done(completed):
            jobs.pop(record.id, None)
            if not completed.cancelled():
                completed.exception()
        job.add_done_callback(done)
        return record.model_copy(deep=True)

    @app.post("/api/tasks", response_model=TaskRecord, status_code=202)
    async def create_task(payload: CreateTask):
        return await submit(payload)

    @app.get("/api/tasks", response_model=list[TaskRecord])
    async def list_tasks():
        return app.state.store.list(settings.max_tasks)

    @app.get("/api/tasks/{task_id}", response_model=TaskRecord)
    async def get_task(task_id: str):
        return get_record(task_id)

    @app.post("/api/tasks/{task_id}/cancel", response_model=TaskRecord)
    async def cancel_task(task_id: str):
        record = get_record(task_id)
        job = jobs.get(task_id)
        if job is None or record.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="Only an active task can be cancelled")
        cancelled_by_user.add(task_id)
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        if record.status in {"queued", "running"}:
            record.status, record.error, record.recoverable = "cancelled", "Task cancelled by user", True
            record.updated_at = utc_now()
            app.state.store.save(record)
        cancelled_by_user.discard(task_id)
        return record

    @app.post("/api/tasks/{task_id}/retry", response_model=TaskRecord, status_code=202)
    async def retry_task(task_id: str):
        previous = get_record(task_id)
        if previous.status not in {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="Only a failed or cancelled task can be retried")
        return await submit(CreateTask(prompt=previous.prompt, file_ids=previous.file_ids, workflow=previous.workflow), parent_task_id=task_id)

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        record = get_record(task_id)
        if record.status in {"queued", "running", "awaiting_review"} or task_id in jobs:
            raise HTTPException(status_code=409, detail="Cancel the active task before deleting it")
        app.state.store.delete(task_id)
        tasks.pop(task_id, None)
        return {"deleted": True, "id": task_id, "uploaded_files_preserved": True}

    return app


app = create_app()
