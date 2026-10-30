"""Alibaba Model Studio compatible text API, not the native DashScope protocol."""

from .base import ModelRequest, ModelResponse
from .chat import CompatibleChatProvider


class QwenAdapter(CompatibleChatProvider):
    provider_id = "qwen"
    protocol = "Alibaba Model Studio compatible Chat Completions"
    default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    credential_variable = "HW_QWEN_API_KEY"
    allowed_parameters = CompatibleChatProvider.allowed_parameters | {
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "enable_thinking",
    }

    def _payload(self, request: ModelRequest, *, output_limit: int | None = None) -> dict:
        # Qwen reasoning support is model-dependent. Explicitly disable thinking
        # for this bounded, non-streaming text implementation unless configured.
        return {"enable_thinking": False, **super()._payload(request, output_limit=output_limit)}

    def _normalize(self, body: dict, request: ModelRequest) -> ModelResponse:
        usage = body.get("usage") or {}
        return self._response(
            text=self._chat_text(body),
            request=request,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        )
