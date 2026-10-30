"""OpenAI text Responses API: its request/output/usage shape is independent."""

from .base import ModelRequest, ModelResponse, ProviderError
from .http import HTTPProvider


class OpenAIAdapter(HTTPProvider):
    provider_id = "openai"
    protocol = "OpenAI Responses"
    default_base_url = "https://api.openai.com/v1"
    request_path = "/responses"
    credential_variable = "HW_OPENAI_API_KEY"
    allowed_parameters = frozenset(
        {
            "temperature",
            "top_p",
            "max_output_tokens",
            "reasoning",
            "text",
            "truncation",
            "service_tier",
        }
    )

    def _payload(self, request: ModelRequest, *, output_limit: int | None = None) -> dict:
        params = {"max_output_tokens": 256, **self.config.parameters}
        if output_limit is not None:
            params["max_output_tokens"] = output_limit
        return {
            "model": self.config.model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": self.prompt_text(request)}],
                }
            ],
            "stream": False,
            "store": False,
            **params,
        }

    def _normalize(self, body: dict, request: ModelRequest) -> ModelResponse:
        text = "\n".join(
            content["text"]
            for item in body.get("output", [])
            if item.get("type") == "message"
            for content in item.get("content", [])
            if content.get("type") == "output_text" and isinstance(content.get("text"), str)
        ).strip()
        if not text:
            raise ProviderError("Provider returned no text output", code="empty_response")
        usage = body.get("usage") or {}
        return self._response(
            text=text,
            request=request,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
        )
