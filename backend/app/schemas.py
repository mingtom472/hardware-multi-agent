from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="hardware_review", min_length=1, max_length=80)
    reviewers: list[str] = Field(
        default_factory=lambda: ["openai", "deepseek", "doubao"], min_length=1, max_length=5
    )
    vision_provider: str = "openai"
    validator: str = "claude"
    summarizer: str = "glm"
    strategy: Literal["auto", "manual", "hybrid"] = "auto"
    profile: Literal["quick", "professional", "expert"] = "professional"
    role_overrides: dict[str, str] = Field(default_factory=dict)
    locked_roles: list[str] = Field(default_factory=list)
    role_models: dict[str, str] = Field(default_factory=dict)
    orchestrator: str | None = None
    max_calls: int = Field(default=16, ge=1, le=100)
    max_cost_usd: Decimal = Field(default=Decimal("1"), gt=0)
    estimated_call_cost_usd: Decimal = Field(default=Decimal("0.01"), gt=0)
    max_rounds: int = Field(default=1, ge=1, le=3)
    require_human_review: bool = False
    max_retries: int = Field(default=2, ge=0, le=5)
    max_parallel: int = Field(default=3, ge=1, le=5)
    timeout_seconds: float = Field(default=20, gt=0, le=120)

    @field_validator("reviewers")
    @classmethod
    def distinct_reviewers(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("reviewers must be unique")
        return value


class FileMetadata(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    uploaded_at: datetime = Field(default_factory=utc_now)
    parser_status: str = "unparsed"
    parser_supported: bool = False
    storage_location: str = "local"


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=20000)
    file_ids: list[str] = Field(default_factory=list, max_length=100)
    workflow: WorkflowConfig | None = None

    @field_validator("prompt")
    @classmethod
    def meaningful_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt cannot be blank")
        return value.strip()


class TaskEvent(BaseModel):
    stage: str
    message: str
    timestamp: datetime = Field(default_factory=utc_now)


class CallRecord(BaseModel):
    stage: str
    provider: str
    model: str | None = None
    attempt: int
    status: Literal["completed", "failed"]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: float = 0
    simulated: bool = True
    error: str | None = None
    output: str | None = None
    stage_input: dict = Field(default_factory=dict)
    stage_output: dict = Field(default_factory=dict)
    cached_input_tokens: int = 0
    usage_source: str = "estimate"
    cost_source: str = "estimate"
    estimated_cost_usd: Decimal | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    simulated: bool = True
    cached_input_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    usage_source: str = "estimate"
    cost_source: str = "estimate"


class TaskRecord(BaseModel):
    id: str
    prompt: str
    file_ids: list[str] = Field(default_factory=list)
    status: Literal["queued", "running", "completed", "failed", "cancelled", "awaiting_review"] = "queued"
    mode: Literal["mock", "real"] = "mock"
    parent_task_id: str | None = None
    recoverable: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    workflow: WorkflowConfig
    events: list[TaskEvent] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    result: dict = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    stages: list[dict] = Field(default_factory=list)
    plan: list[dict] = Field(default_factory=list)
