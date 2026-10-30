from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server-only settings; credentials must never be exposed to the browser."""

    model_config = SettingsConfigDict(
        env_prefix="HW_", env_file=".env", extra="ignore", case_sensitive=False
    )

    model_mode: Literal["mock", "real"] = "mock"
    data_dir: Path = Path(".local/data")
    max_upload_bytes: int | None = Field(default=None, ge=1)
    max_storage_bytes: int | None = Field(default=None, ge=1)
    storage_reserve_bytes: int = Field(default=16 * 1024 * 1024, ge=0)
    max_stored_files: int = Field(default=1000, ge=1, le=100000)
    upload_chunk_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)
    max_files_per_task: int = Field(default=8, ge=1, le=100)
    max_active_tasks: int = Field(default=4, ge=1, le=32)
    max_tasks: int = Field(default=200, ge=1, le=10000)
    workflow_path: Path = Path(__file__).resolve().parent / "workflows/default.json"
    daily_budget_usd: Decimal = Field(default=Decimal("10"), gt=0)
    monthly_budget_usd: Decimal = Field(default=Decimal("100"), gt=0)
    default_task_budget_usd: Decimal = Field(default=Decimal("1"), gt=0)
    estimated_call_cost_usd: Decimal = Field(default=Decimal("0.01"), gt=0)
    cors_origins: list[str] = [
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:8501", "http://127.0.0.1:8501",
    ]
    database_url: str | None = None
    redis_url: str | None = None
    object_storage_endpoint: str | None = None
    object_storage_bucket: str | None = None
