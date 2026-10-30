"""Moonshot text compatibility adapter with independent request/usage policy."""

from .base import ModelRequest, ModelResponse
from .chat import CompatibleChatProvider


class MoonshotAdapter(CompatibleChatProvider):
    provider_id = "moonshot"
    protocol = "Moonshot Chat Completions"
    default_base_url = "https://api.moonshot.cn/v1"
    credential_variable = "HW_MOONSHOT_API_KEY"

    def _normalize(self, body: dict, request: ModelRequest) -> ModelResponse:
        usage = body.get("usage") or {}
        # Cache detail is optional; absent usage never becomes claimed API usage.
        return self._response(
            text=self._chat_text(body),
            request=request,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        )
