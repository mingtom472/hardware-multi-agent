"""Provider-neutral requests/results; HTTP protocols stay inside each adapter."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class ModelRequest:
    stage: str
    prompt: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal = Decimal("0")
    simulated: bool = True
    cached_input_tokens: int = 0
    usage_source: Literal["estimate", "provider"] = "estimate"
    cost_source: Literal["estimate", "unavailable"] = "estimate"
    estimated_cost_usd: Decimal | None = None


class ProviderError(Exception):
    """An adapter failure with an explicit retry policy, never a credential value."""

    def __init__(
        self,
        message: str,
        retryable: bool = False,
        *,
        code: str = "provider_error",
        status_code: int | None = None,
        billed_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.status_code = status_code
        self.billed_unknown = billed_unknown


@runtime_checkable
class Provider(Protocol):
    provider_id: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one call and normalize that provider's response/usage."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Inject credentials from a secret manager/environment; never serialize this."""

    api_key: SecretStr | str | None = field(default=None, repr=False)
    model: str | None = None
    base_url: str | None = None
    validated: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None
    cached_input_price_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        if isinstance(self.api_key, str):
            object.__setattr__(self, "api_key", SecretStr(self.api_key))


class ReservedAdapter:
    """Fail closed until a provider-specific implementation is tested.

    Sharing this failure path does not imply interchangeable request formats,
    streaming events, multimodal payloads, errors, authentication or usage fields.
    """

    provider_id: str
    protocol: str
    endpoint: str
    credential_variable: str
    capabilities: tuple[str, ...] = ("text",)

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or ProviderConfig()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ProviderError(
            f"{self.provider_id}: real {self.protocol} adapter is reserved, "
            "not implemented in this MVP; select HW_MODEL_MODE=mock.",
            retryable=False,
        )
