"""Configurable, non-secret model capabilities and role routes.

Target model identifiers are user-editable suggestions, not claims of provider
availability. Only an explicit live text connection test may verify an entry.
"""

import json
import os
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .persistence import SQLiteTaskStore
from .providers import MockProvider, ProviderConfig, build_providers


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_per_million: Decimal | None = Field(default=None, ge=0)
    output_per_million: Decimal | None = Field(default=None, ge=0)
    cached_input_per_million: Decimal | None = Field(default=None, ge=0)


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,50}$")
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(max_length=500)
    credential_env: str = Field(pattern=r"^HW_[A-Z0-9_]+_API_KEY$")
    capabilities: list[str] = Field(default_factory=lambda: ["text"], max_length=16)
    enabled: bool = True
    kind: Literal["text", "agent_runtime", "image_generation"] = "text"
    verified: bool = False
    verified_capabilities: list[str] = Field(default_factory=list)
    parameters: dict = Field(default_factory=dict)
    pricing: Pricing = Field(default_factory=Pricing)

    @field_validator("base_url")
    @classmethod
    def secure_endpoint(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("base_url must be an HTTPS API prefix without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("parameters")
    @classmethod
    def no_credentials(cls, value: dict) -> dict:
        forbidden = {"api_key", "api-key", "authorization", "password", "secret", "token", "access_token", "headers"}
        def inspect(item):
            if isinstance(item, dict):
                if any(str(key).lower() in forbidden for key in item):
                    raise ValueError("Credentials and HTTP headers cannot be stored as model parameters")
                for child in item.values():
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)
        inspect(value)
        if len(json.dumps(value)) > 16000:
            raise ValueError("Model parameter configuration is too large")
        return value


class RegistryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[ModelEntry] = Field(min_length=1, max_length=100)
    roles: dict[str, str] | None = None


ROLE_ROUTES = {
    "schematic": "qwen-schematic",
    "power_reasoning": "openai-power",
    "software": "claude-software",
    "datasheet": "moonshot-datasheet",
    "calculation": "openai-calculation",
    "automation": "claude-automation",
    "batch": "deepseek-batch",
    "illustration": "openai-illustration",
}


def default_models() -> list[ModelEntry]:
    entries = [
        ("qwen-schematic", "qwen", "qwen3.8-max", "https://dashscope.aliyuncs.com/compatible-mode/v1", "HW_QWEN_API_KEY", ["text", "vision"], "text"),
        ("openai-power", "openai", "gpt-6-astra", "https://api.openai.com/v1", "HW_OPENAI_API_KEY", ["text"], "text"),
        ("claude-software", "claude", "claude-opus-5-5", "https://api.anthropic.com/v1", "HW_CLAUDE_API_KEY", ["text"], "text"),
        ("moonshot-datasheet", "moonshot", "kimi-k3.1-pro", "https://api.moonshot.cn/v1", "HW_MOONSHOT_API_KEY", ["text", "documents"], "text"),
        ("openai-calculation", "openai", "gpt-6.1-sol", "https://api.openai.com/v1", "HW_OPENAI_API_KEY", ["text"], "text"),
        ("claude-automation", "claude", "agent-sdk-runtime", "https://api.anthropic.com/v1", "HW_CLAUDE_API_KEY", ["agent_runtime"], "agent_runtime"),
        ("deepseek-batch", "deepseek", "deepseek-flash", "https://api.deepseek.com", "HW_DEEPSEEK_API_KEY", ["text"], "text"),
        ("openai-illustration", "openai", "gpt-image-model-id-unverified", "https://api.openai.com/v1", "HW_OPENAI_API_KEY", ["image_generation"], "image_generation"),
    ]
    return [ModelEntry(id=i, provider=p, model=m, base_url=u, credential_env=e, capabilities=c, kind=k) for i, p, m, u, e, c, k in entries]


class RegistryError(ValueError):
    pass


class ModelRegistry:
    def __init__(self, store: SQLiteTaskStore):
        self.store = store
        saved = store.get_setting("model_registry")
        if saved:
            state = json.loads(saved)
            self.models = {item["id"]: ModelEntry.model_validate(item) for item in state["models"]}
            self.roles = state["roles"]
        else:
            self.models = {entry.id: entry for entry in default_models()}
            self.roles = dict(ROLE_ROUTES)
            self._save()

    def _save(self) -> None:
        self.store.save_setting("model_registry", json.dumps({
            "models": [entry.model_dump(mode="json") for entry in self.models.values()],
            "roles": self.roles,
        }))

    @staticmethod
    def credential(entry: ModelEntry) -> str | None:
        # dotenv is read only in the server process; values never enter registry snapshots.
        return os.environ.get(entry.credential_env) or dotenv_values(".env").get(entry.credential_env)

    def public(self, mode: str) -> dict:
        return {
            "models": [dict(entry.model_dump(mode="json"), credential_present=bool(self.credential(entry))) for entry in self.models.values()],
            "roles": dict(self.roles), "mode": mode,
        }

    def update(self, payload: RegistryUpdate) -> None:
        proposed = {entry.id: entry.model_copy(deep=True) for entry in payload.models}
        if len(proposed) != len(payload.models):
            raise RegistryError("Model registry IDs must be unique")
        roles = self.roles if payload.roles is None else payload.roles
        if set(roles) != set(ROLE_ROUTES):
            raise RegistryError("Role configuration must retain the eight engineering role keys")
        if any(model_id not in proposed for model_id in roles.values()):
            raise RegistryError("Every role must reference an existing model entry")
        for model_id, entry in proposed.items():
            previous = self.models.get(model_id)
            if entry.verified and (previous is None or not previous.verified):
                raise RegistryError("Verification can only be granted by an explicit live connection test")
            ignore = {"verified", "verified_capabilities"}
            same = previous and previous.model_dump(exclude=ignore) == entry.model_dump(exclude=ignore)
            if not same or not entry.verified:
                entry.verified = False
                entry.verified_capabilities = []
            else:
                entry.verified_capabilities = list(previous.verified_capabilities)
        self.models = proposed
        self.roles = dict(roles)
        self._save()

    def get(self, model_id: str) -> ModelEntry:
        entry = self.models.get(model_id)
        if entry is None:
            raise RegistryError("Unknown model registry ID")
        return entry

    def adapter_for(self, entry: ModelEntry, *, verified: bool = True):
        if not entry.enabled or entry.kind != "text":
            raise RegistryError("This model role is reserved and is not a text-model runtime")
        credential = self.credential(entry)
        if not credential:
            raise RegistryError(f"Missing credential environment variable: {entry.credential_env}")
        if verified and not entry.verified:
            raise RegistryError("Model has not passed an explicit live connection test")
        config = ProviderConfig(
            api_key=credential, model=entry.model, base_url=entry.base_url,
            validated=entry.verified if verified else False,
            parameters=entry.parameters,
            input_price_per_million=entry.pricing.input_per_million,
            output_price_per_million=entry.pricing.output_per_million,
            cached_input_price_per_million=entry.pricing.cached_input_per_million,
        )
        try:
            return build_providers("real", {entry.provider: config})[entry.provider]
        except (KeyError, ValueError):
            raise RegistryError("No implemented adapter exists for this provider") from None

    def mark_verified(self, model_id: str, *, expected_config: dict | None = None) -> None:
        entry = self.get(model_id)
        if expected_config is not None and entry.model_dump(
            mode="json", exclude={"verified", "verified_capabilities"}
        ) != expected_config:
            raise RegistryError("Model configuration changed during the test; test the new configuration again")
        entry.verified = True
        # A tiny text test says nothing about image/document/SDK compatibility.
        entry.verified_capabilities = ["text"]
        self._save()

    def runtime_providers(self, mode: str) -> dict:
        if mode == "mock":
            aliases = build_providers("mock")
            aliases.update({entry.id: MockProvider(entry.provider) for entry in self.models.values() if entry.enabled and entry.kind == "text"})
            return aliases
        return {
            entry.id: self.adapter_for(entry)
            for entry in self.models.values()
            if entry.enabled and entry.kind == "text" and entry.verified and self.credential(entry)
        }
