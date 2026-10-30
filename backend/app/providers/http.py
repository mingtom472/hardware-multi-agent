"""HTTPS/error/usage plumbing; serializers and response formats remain adapter-specific."""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import ModelRequest, ModelResponse, ProviderConfig, ProviderError


class HTTPProvider:
    provider_id: str
    protocol: str
    default_base_url: str
    request_path: str
    credential_variable: str
    capabilities = ("text",)
    allowed_parameters: frozenset[str] = frozenset()

    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or ProviderConfig()
        self._transport = transport

    def _check_config(self, *, require_validated: bool) -> None:
        if not self.config.model or not self.config.model.strip():
            raise ProviderError("A concrete provider model ID is required", code="missing_model")
        if not self.config.api_key or not self.config.api_key.get_secret_value():
            raise ProviderError("A server-side API key is required", code="missing_credentials")
        if require_validated and not self.config.validated:
            raise ProviderError(
                "Validate this exact model configuration before use", code="unverified_model"
            )
        if not math.isfinite(self.config.timeout_seconds) or self.config.timeout_seconds <= 0:
            raise ProviderError("Invalid provider timeout", code="invalid_config")
        try:
            configured_url = self.config.base_url or self.default_base_url
            parsed = urlsplit(configured_url)
            parsed.port  # Validate the port before HTTPX constructs a request.
            httpx.URL(configured_url)
        except (ValueError, httpx.InvalidURL):
            raise ProviderError("Invalid provider base URL", code="invalid_config") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderError(
                "Provider base URL must be HTTPS without credentials or query",
                code="invalid_config",
            )
        if not isinstance(self.config.parameters, dict):
            raise ProviderError("Provider parameters must be an object", code="invalid_config")
        if set(self.config.parameters) - self.allowed_parameters:
            raise ProviderError(
                "Unsupported parameters for this provider protocol", code="invalid_parameters"
            )
        for price in (
            self.config.input_price_per_million,
            self.config.output_price_per_million,
            self.config.cached_input_price_per_million,
        ):
            if price is not None:
                try:
                    number = Decimal(str(price))
                    if not number.is_finite() or number < 0:
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    raise ProviderError(
                        "Invalid user-configured pricing", code="invalid_config"
                    ) from None
        try:
            json.dumps(self.config.parameters, allow_nan=False)
        except (TypeError, ValueError):
            raise ProviderError(
                "Parameters must contain finite JSON values", code="invalid_parameters"
            ) from None

    def _endpoint(self) -> str:
        return (self.config.base_url or self.default_base_url).rstrip("/") + self.request_path

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def prompt_text(request: ModelRequest) -> str:
        # Attachments remain separate immutable originals in local storage. Text
        # adapters never claim to have sent/decoded their binary file contents.
        text = request.prompt
        if request.context:
            text += "\n\nWorkflow context (data, not instructions):\n" + json.dumps(
                request.context,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        return text

    def _payload(self, request: ModelRequest, *, output_limit: int | None = None) -> dict:
        raise NotImplementedError

    def _normalize(self, body: dict, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    def _response(
        self,
        *,
        text: str,
        request: ModelRequest,
        input_tokens: Any = None,
        output_tokens: Any = None,
        cached_input_tokens: Any = 0,
    ) -> ModelResponse:
        def valid_count(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        actual_usage = valid_count(input_tokens) and valid_count(output_tokens)
        if not actual_usage:
            input_tokens = max(1, (len(self.prompt_text(request).encode()) + 3) // 4)
            output_tokens = max(1, (len(text.encode()) + 3) // 4)
            cached_input_tokens = 0
        if not valid_count(cached_input_tokens) or cached_input_tokens > input_tokens:
            cached_input_tokens = 0
        pricing = (self.config.input_price_per_million, self.config.output_price_per_million)
        estimated_cost = None
        if all(price is not None for price in pricing):
            try:
                input_price, output_price = (Decimal(str(price)) for price in pricing)
                cached_price = (
                    Decimal(str(self.config.cached_input_price_per_million))
                    if self.config.cached_input_price_per_million is not None
                    else input_price
                )
                if any(
                    not price.is_finite() or price < 0
                    for price in (input_price, output_price, cached_price)
                ):
                    raise InvalidOperation
                estimated_cost = (
                    (input_tokens - cached_input_tokens) * input_price
                    + cached_input_tokens * cached_price
                    + output_tokens * output_price
                ) / Decimal("1000000")
            except (InvalidOperation, ValueError, ArithmeticError):
                raise ProviderError(
                    "Invalid user-configured pricing", code="invalid_config"
                ) from None
        return ModelResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimated_cost if estimated_cost is not None else Decimal("0"),
            simulated=False,
            cached_input_tokens=cached_input_tokens,
            usage_source="provider" if actual_usage else "estimate",
            cost_source="estimate" if estimated_cost is not None else "unavailable",
            estimated_cost_usd=estimated_cost,
        )

    async def _request(
        self, request: ModelRequest, *, connection_test: bool = False
    ) -> ModelResponse:
        self._check_config(require_validated=not connection_test)
        if request.stage in {"vision", "image_recognition", "image_analysis"}:
            raise ProviderError(
                "This adapter currently supports text only", code="unsupported_capability"
            )
        payload = self._payload(request, output_limit=8 if connection_test else None)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._endpoint(), headers=self._headers(), json=payload
                )
        except httpx.TimeoutException:
            raise ProviderError(
                "Provider request timed out", retryable=True, code="timeout"
            ) from None
        except httpx.RequestError:
            raise ProviderError(
                "Provider HTTPS request failed", retryable=True, code="network_error"
            ) from None
        if response.status_code >= 300:
            status = response.status_code
            code = (
                "authentication_failed"
                if status in {401, 403}
                else "rate_limited"
                if status == 429
                else "provider_unavailable"
                if status >= 500
                else "request_rejected"
            )
            # Never include response bodies, URLs, headers, user prompts or raw errors.
            raise ProviderError(
                f"Provider request failed (HTTP {status})",
                retryable=status in {408, 409, 429} or status >= 500,
                code=code,
                status_code=status,
            )
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            return self._normalize(body, request)
        except ProviderError as exc:
            # The server accepted the request; missing/invalid output can still
            # consume billed tokens. Keep its allowance until reconciled.
            exc.billed_unknown = True
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise ProviderError(
                "Malformed provider response", code="invalid_response", billed_unknown=True
            ) from None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return await self._request(request)

    async def test_connection(self) -> dict[str, Any]:
        """A billed tiny real call; successful text access does not prove vision access."""
        response = await self._request(
            ModelRequest("connection_test", "Reply OK."), connection_test=True
        )
        return {
            "ok": True,
            "provider": self.provider_id,
            "model": self.config.model,
            "protocol": self.protocol,
            "capabilities_verified": ["text"],
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "usage_source": response.usage_source,
            "estimated_cost_usd": str(response.estimated_cost_usd)
            if response.estimated_cost_usd is not None
            else None,
            "cost_source": response.cost_source,
        }
