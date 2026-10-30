"""Doubao extension boundary: Volcengine Ark endpoints/models and usage."""

from .base import ReservedAdapter


class DoubaoAdapter(ReservedAdapter):
    provider_id = "doubao"
    protocol = "Volcengine Ark Chat Completions"
    endpoint = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    credential_variable = "HW_DOUBAO_API_KEY"
    capabilities = ("text", "vision")
    # Verify Ark endpoint/model identifiers and image limits before implementation.
