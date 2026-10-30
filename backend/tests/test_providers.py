import asyncio
import json
from dataclasses import asdict
from decimal import Decimal

import httpx
import pytest
from app.providers import (
    ADAPTER_TYPES,
    OPTIONAL_ADAPTER_TYPES,
    ClaudeAdapter,
    DeepSeekAdapter,
    MockProvider,
    ModelRequest,
    MoonshotAdapter,
    OpenAIAdapter,
    ProviderConfig,
    ProviderError,
    QwenAdapter,
    build_providers,
    provider_catalog,
)

PLACEHOLDER = "placeholder-for-hermetic-tests-not-a-real-key"


def config(**overrides):
    return ProviderConfig(
        **{
            "api_key": PLACEHOLDER,
            "model": "user-supplied-model-id",
            "validated": True,
            **overrides,
        }
    )


def body_for(adapter_type, usage=None):
    if adapter_type is OpenAIAdapter:
        return {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "usage": usage if usage is not None else {"input_tokens": 20, "output_tokens": 2},
        }
    if adapter_type is ClaudeAdapter:
        return {
            "content": [{"type": "text", "text": "OK"}],
            "usage": usage if usage is not None else {"input_tokens": 20, "output_tokens": 2},
        }
    return {
        "choices": [{"message": {"content": "OK"}}],
        "usage": usage if usage is not None else {"prompt_tokens": 20, "completion_tokens": 2},
    }


def test_registry_exposes_five_required_and_two_optional_providers():
    providers = build_providers()
    catalog = provider_catalog()
    assert set(providers) == {"qwen", "openai", "claude", "moonshot", "deepseek", "doubao", "glm"}
    assert {a.provider_id for a in ADAPTER_TYPES} == {
        "qwen",
        "openai",
        "claude",
        "moonshot",
        "deepseek",
    }
    assert len({item["protocol"] for item in catalog}) == 7
    assert all(item["mode"] == "mock" and item["configured"] for item in catalog)
    assert all(item["availability"] == "unverified" for item in catalog)
    assert not next(item for item in catalog if item["id"] == "doubao")["implemented"]
    assert all(
        item["capabilities"] == ["text"] for item in provider_catalog("real") if item["implemented"]
    )


@pytest.mark.parametrize(
    "stage", ["classification", "vision", "hardware_analysis", "validation", "summary", "datasheet"]
)
def test_mock_stages_are_repeatable_with_estimated_usage(stage):
    provider = MockProvider("openai", latency_seconds=0)
    request = ModelRequest(stage, "分析 24V 转 5V Buck 电源", [{"content_type": "image/png"}])
    first = asyncio.run(provider.complete(request))
    second = asyncio.run(provider.complete(request))
    assert asdict(first) == asdict(second)
    assert first.text and first.input_tokens > 0 and first.output_tokens > 0
    assert first.cost_usd > Decimal("0") and first.simulated
    assert first.usage_source == first.cost_source == "estimate"


def test_mock_does_not_claim_real_image_or_pdf_parsing():
    provider = MockProvider("qwen", 0)
    image = asyncio.run(provider.complete(ModelRequest("vision", "图像")))
    pdf = asyncio.run(provider.complete(ModelRequest("datasheet", "手册")))
    assert "尚未读取像素" in image.text
    assert "尚未读取或解析 PDF 原文" in pdf.text


def test_embedded_software_is_explicitly_reserved():
    result = asyncio.run(
        MockProvider("claude", 0).complete(ModelRequest("classification", "编写 RTOS 固件"))
    )
    assert "embedded_software_interface_only" in result.text


@pytest.mark.parametrize("adapter_type", ADAPTER_TYPES)
def test_unverified_models_cannot_run_even_with_a_key(adapter_type):
    adapter = adapter_type(config(validated=False))
    assert PLACEHOLDER not in repr(adapter.config)
    with pytest.raises(ProviderError) as error:
        asyncio.run(adapter.complete(ModelRequest("analysis", "evaluate")))
    assert error.value.code == "unverified_model"
    assert not error.value.retryable and PLACEHOLDER not in str(error.value)


@pytest.mark.parametrize("adapter_type", ADAPTER_TYPES)
def test_transport_contracts_and_usage_are_vendor_specific(adapter_type):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body_for(adapter_type))

    adapter = adapter_type(config(), transport=httpx.MockTransport(handler))
    result = asyncio.run(
        adapter.complete(ModelRequest("hardware_analysis", "Review buck", context={"vin": 24}))
    )
    request = requests[0]
    payload = json.loads(request.content)
    assert result.text == "OK" and not result.simulated
    assert result.input_tokens == 20 and result.output_tokens == 2
    assert result.usage_source == "provider"
    assert result.cost_source == "unavailable" and result.estimated_cost_usd is None
    assert payload["model"] == "user-supplied-model-id" and payload["stream"] is False
    if adapter_type is OpenAIAdapter:
        assert request.url.path == "/v1/responses"
        assert payload["input"][0]["content"][0]["type"] == "input_text"
        assert "Workflow context" in payload["input"][0]["content"][0]["text"]
        assert payload["store"] is False and "messages" not in payload
    elif adapter_type is ClaudeAdapter:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == PLACEHOLDER
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        assert payload["messages"][0]["content"][0]["type"] == "text"
    else:
        assert request.url.path.endswith("/chat/completions")
        assert isinstance(payload["messages"][0]["content"], str)
        if adapter_type is QwenAdapter:
            assert payload["enable_thinking"] is False
    if adapter_type is not ClaudeAdapter:
        assert request.headers["authorization"] == f"Bearer {PLACEHOLDER}"


@pytest.mark.parametrize("adapter_type", ADAPTER_TYPES)
def test_connection_uses_tiny_actual_request_without_changing_verification(adapter_type):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body_for(adapter_type))

    parameters = {"max_output_tokens" if adapter_type is OpenAIAdapter else "max_tokens": 999}
    adapter = adapter_type(
        config(validated=False, parameters=parameters), transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(adapter.test_connection())
    assert result["ok"] is True and result["capabilities_verified"] == ["text"]
    assert adapter.config.validated is False
    assert sent[0].get("max_output_tokens", sent[0].get("max_tokens")) == 8
    assert PLACEHOLDER not in json.dumps(result)


@pytest.mark.parametrize(
    "status,retryable,code",
    [
        (400, False, "request_rejected"),
        (401, False, "authentication_failed"),
        (403, False, "authentication_failed"),
        (404, False, "request_rejected"),
        (408, True, "request_rejected"),
        (429, True, "rate_limited"),
        (500, True, "provider_unavailable"),
        (502, True, "provider_unavailable"),
        (307, False, "request_rejected"),
    ],
)
def test_http_errors_are_classified_without_leaking_body_or_key(status, retryable, code):
    def handler(request):
        return httpx.Response(
            status,
            json={"error": {"message": PLACEHOLDER}},
            headers={"location": "https://other.example/"},
        )

    adapter = OpenAIAdapter(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        asyncio.run(adapter.complete(ModelRequest("analysis", "test")))
    assert error.value.retryable is retryable
    assert error.value.status_code == status and error.value.code == code
    assert PLACEHOLDER not in str(error.value)


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_errors_are_sanitized(exception):
    def handler(request):
        raise exception(PLACEHOLDER, request=request)

    adapter = OpenAIAdapter(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        asyncio.run(adapter.complete(ModelRequest("analysis", "test")))
    assert error.value.retryable and PLACEHOLDER not in str(error.value)


def test_anthropic_cache_tokens_are_added_to_total_input():
    usage = {
        "input_tokens": 500,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 50,
        "output_tokens": 50,
    }
    adapter = ClaudeAdapter(
        config(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=body_for(ClaudeAdapter, usage))
        ),
    )
    result = asyncio.run(adapter.complete(ModelRequest("analysis", "test")))
    assert result.input_tokens == 650 and result.cached_input_tokens == 100


@pytest.mark.parametrize(
    "adapter_type,usage",
    [
        (
            OpenAIAdapter,
            {
                "input_tokens": 1000,
                "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 200},
            },
        ),
        (
            QwenAdapter,
            {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 200},
            },
        ),
        (
            MoonshotAdapter,
            {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 200},
            },
        ),
        (
            DeepSeekAdapter,
            {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 200},
        ),
    ],
)
def test_cache_usage_and_user_supplied_price_estimates(adapter_type, usage):
    adapter = adapter_type(
        config(
            input_price_per_million=Decimal("2"),
            output_price_per_million=Decimal("4"),
            cached_input_price_per_million=Decimal("0.5"),
        ),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=body_for(adapter_type, usage))
        ),
    )
    result = asyncio.run(adapter.complete(ModelRequest("analysis", "test")))
    assert result.cached_input_tokens == 200
    assert result.cost_usd == result.estimated_cost_usd == Decimal("0.0021")
    assert result.usage_source == "provider" and result.cost_source == "estimate"


def test_missing_usage_is_estimated_instead_of_claimed_provider_data():
    adapter = OpenAIAdapter(
        config(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=body_for(OpenAIAdapter, {}))
        ),
    )
    result = asyncio.run(adapter.complete(ModelRequest("analysis", "test")))
    assert result.usage_source == "estimate" and result.input_tokens > 0


@pytest.mark.parametrize("body", [{}, {"output": []}, {"output": "invalid"}, ["invalid"]])
def test_malformed_success_responses_are_not_connection_success(body):
    adapter = OpenAIAdapter(
        config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )
    with pytest.raises(ProviderError):
        asyncio.run(adapter.test_connection())


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": None},
        {"model": None},
        {"base_url": "http://api.example.com/v1"},
        {"base_url": "https://user:password@api.example.com/v1"},
        {"base_url": "https://api.example.com/v1?key=hidden"},
        {"base_url": "https://api.example.com:bad/v1"},
        {"parameters": {"model": "override"}},
        {"parameters": {"temperature": float("nan")}},
        {"timeout_seconds": -1},
        {"input_price_per_million": Decimal("-1")},
    ],
)
def test_invalid_config_is_rejected_before_any_request(overrides):
    def handler(request):
        pytest.fail("Invalid config must not send credentials")

    adapter = OpenAIAdapter(config(**overrides), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        asyncio.run(adapter.test_connection())


def test_custom_https_gateway_keeps_native_protocol():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body_for(ClaudeAdapter))

    adapter = ClaudeAdapter(
        config(base_url="https://gateway.example/anthropic/v1/"),
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(adapter.complete(ModelRequest("analysis", "test")))
    assert str(requests[0].url) == "https://gateway.example/anthropic/v1/messages"
    assert requests[0].headers["x-api-key"] == PLACEHOLDER


@pytest.mark.parametrize("adapter_type", ADAPTER_TYPES)
def test_real_vision_is_explicitly_unsupported(adapter_type):
    with pytest.raises(ProviderError) as error:
        asyncio.run(adapter_type(config()).complete(ModelRequest("vision", "test")))
    assert error.value.code == "unsupported_capability"


def test_factory_real_requires_configs_and_retains_selected_model():
    with pytest.raises(ProviderError):
        build_providers("real")
    providers = build_providers("real", {"openai": config(model="specific-account-model")})
    assert providers["openai"].config.model == "specific-account-model"
    with pytest.raises(ProviderError):
        build_providers("real", {"unknown": config()})


@pytest.mark.parametrize("adapter_type", OPTIONAL_ADAPTER_TYPES)
def test_optional_adapters_remain_reserved(adapter_type):
    with pytest.raises(ProviderError, match="not implemented"):
        asyncio.run(adapter_type(config()).complete(ModelRequest("analysis", "test")))


def test_unknown_mock_stage_is_nonretryable():
    with pytest.raises(ProviderError) as error:
        asyncio.run(MockProvider("openai", 0).complete(ModelRequest("unknown", "")))
    assert not error.value.retryable


@pytest.mark.parametrize("latency", [-1, float("nan"), float("inf")])
def test_mock_rejects_invalid_latency(latency):
    with pytest.raises(ValueError):
        MockProvider("openai", latency)
