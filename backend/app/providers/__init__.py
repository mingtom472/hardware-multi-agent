"""Provider registry; model IDs and permission checks are configured separately."""

from .base import ModelRequest, ModelResponse, Provider, ProviderConfig, ProviderError
from .claude import ClaudeAdapter
from .deepseek import DeepSeekAdapter
from .doubao import DoubaoAdapter
from .glm import GLMAdapter
from .mock import MockProvider
from .moonshot import MoonshotAdapter
from .openai import OpenAIAdapter
from .qwen import QwenAdapter

ADAPTER_TYPES = (QwenAdapter, OpenAIAdapter, ClaudeAdapter, MoonshotAdapter, DeepSeekAdapter)
OPTIONAL_ADAPTER_TYPES = (DoubaoAdapter, GLMAdapter)
ALL_ADAPTER_TYPES = ADAPTER_TYPES + OPTIONAL_ADAPTER_TYPES
_NAMES = {
    "qwen": "阿里云通义千问",
    "openai": "OpenAI",
    "claude": "Claude",
    "moonshot": "Moonshot AI",
    "deepseek": "DeepSeek",
    "doubao": "豆包",
    "glm": "GLM",
}


def build_providers(
    mode: str = "mock",
    configs: dict[str, ProviderConfig] | None = None,
) -> dict[str, Provider]:
    if mode == "mock":
        return {
            adapter.provider_id: MockProvider(adapter.provider_id) for adapter in ALL_ADAPTER_TYPES
        }
    if mode != "real" or not configs:
        raise ProviderError(
            "Real mode requires explicit provider configurations; mock is the offline default.",
            retryable=False,
        )
    adapters = {adapter.provider_id: adapter for adapter in ADAPTER_TYPES}
    if set(configs) - set(adapters):
        raise ProviderError("Unknown or reserved real provider", code="unsupported_provider")
    return {provider_id: adapters[provider_id](config) for provider_id, config in configs.items()}


def provider_catalog(mode: str = "mock") -> list[dict]:
    return [
        {
            "id": adapter.provider_id,
            "name": _NAMES[adapter.provider_id],
            "protocol": adapter.protocol,
            "capabilities": ["text", "vision"]
            if mode == "mock"
            and adapter.provider_id
            in {
                "qwen",
                "openai",
                "claude",
                "doubao",
                "glm",
            }
            else list(adapter.capabilities),
            "configured": mode == "mock",
            "mode": mode,
            "implemented": adapter in ADAPTER_TYPES,
            "availability": "unverified",
        }
        for adapter in ALL_ADAPTER_TYPES
    ]


__all__ = [
    "ModelRequest",
    "ModelResponse",
    "Provider",
    "ProviderConfig",
    "ProviderError",
    "MockProvider",
    "OpenAIAdapter",
    "ClaudeAdapter",
    "DeepSeekAdapter",
    "DoubaoAdapter",
    "GLMAdapter",
    "QwenAdapter",
    "MoonshotAdapter",
    "ADAPTER_TYPES",
    "OPTIONAL_ADAPTER_TYPES",
    "build_providers",
    "provider_catalog",
]
