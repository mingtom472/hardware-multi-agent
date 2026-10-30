"""GLM extension boundary: Zhipu model capabilities and usage are explicit."""

from .base import ReservedAdapter


class GLMAdapter(ReservedAdapter):
    provider_id = "glm"
    protocol = "Zhipu GLM Chat Completions"
    endpoint = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    credential_variable = "HW_GLM_API_KEY"
    capabilities = ("text", "vision")
    # Some GLM models are text-only; negotiate actual model capabilities first.
