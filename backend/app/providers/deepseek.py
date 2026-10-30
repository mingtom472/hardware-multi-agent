"""DeepSeek text API: independently normalize its cache-hit token fields."""

from .base import ModelRequest, ModelResponse
from .chat import CompatibleChatProvider


class DeepSeekAdapter(CompatibleChatProvider):
    provider_id = "deepseek"
    protocol = "DeepSeek Chat Completions"
    default_base_url = "https://api.deepseek.com"
    credential_variable = "HW_DEEPSEEK_API_KEY"
    allowed_parameters = CompatibleChatProvider.allowed_parameters | {
        "presence_penalty",
        "frequency_penalty",
    }

    def _normalize(self, body: dict, request: ModelRequest) -> ModelResponse:
        usage = body.get("usage") or {}
        return self._response(
            text=self._chat_text(body),
            request=request,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cached_input_tokens=usage.get("prompt_cache_hit_tokens", 0),
        )
