"""Shared compatible HTTP framing; each vendor owns its capability/usage contract."""

from .base import ModelRequest, ModelResponse, ProviderError
from .http import HTTPProvider


class CompatibleChatProvider(HTTPProvider):
    request_path = "/chat/completions"
    allowed_parameters = frozenset({"max_tokens", "temperature", "top_p", "stop"})

    def _payload(self, request: ModelRequest, *, output_limit: int | None = None) -> dict:
        params = {"max_tokens": 256, **self.config.parameters}
        if output_limit is not None:
            params["max_tokens"] = output_limit
        return {
            "model": self.config.model,
            "messages": [{"role": "user", "content": self.prompt_text(request)}],
            "stream": False,
            **params,
        }

    def _chat_text(self, body: dict) -> str:
        text = body["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Provider returned no text output", code="empty_response")
        return text

    def _normalize(self, body: dict, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError("Each compatible vendor normalizes its own usage")
