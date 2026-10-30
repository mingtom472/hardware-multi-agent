"""Anthropic Messages: native authentication, content blocks and cache accounting."""

from .base import ModelRequest, ModelResponse, ProviderError
from .http import HTTPProvider


class ClaudeAdapter(HTTPProvider):
    provider_id = "claude"
    protocol = "Anthropic Messages"
    default_base_url = "https://api.anthropic.com/v1"
    request_path = "/messages"
    credential_variable = "HW_CLAUDE_API_KEY"
    allowed_parameters = frozenset(
        {
            "max_tokens",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "system",
        }
    )

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key.get_secret_value(),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _payload(self, request: ModelRequest, *, output_limit: int | None = None) -> dict:
        params = {"max_tokens": 256, **self.config.parameters}
        if output_limit is not None:
            params["max_tokens"] = output_limit
        return {
            "model": self.config.model,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": self.prompt_text(request)}]}
            ],
            "stream": False,
            **params,
        }

    def _normalize(self, body: dict, request: ModelRequest) -> ModelResponse:
        text = "\n".join(
            block["text"]
            for block in body.get("content", [])
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        ).strip()
        if not text:
            raise ProviderError("Provider returned no text output", code="empty_response")
        usage = body.get("usage") or {}
        uncached = usage.get("input_tokens")
        cached = usage.get("cache_read_input_tokens", 0)
        created = usage.get("cache_creation_input_tokens", 0)

        def valid_count(value):
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        total = (
            uncached + cached + created
            if all(valid_count(v) for v in (uncached, cached, created))
            else None
        )
        return self._response(
            text=text,
            request=request,
            input_tokens=total,
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=cached,
        )
