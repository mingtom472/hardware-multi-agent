import json

import httpx
import pytest
from app.config import Settings
from app.main import create_app
from app.persistence import SQLiteTaskStore
from app.providers import OpenAIAdapter, ProviderConfig
from app.registry import ModelEntry, ModelRegistry, RegistryError, RegistryUpdate
from fastapi.testclient import TestClient
from pydantic import ValidationError

SECRET_SENTINEL = "unit-test-secret-never-store-or-return"


def write_payload(public):
    return {
        "models": [
            {key: value for key, value in entry.items() if key != "credential_present"}
            for entry in public["models"]
        ],
        "roles": dict(public["roles"]),
    }


def registry_for(tmp_path):
    return ModelRegistry(SQLiteTaskStore(tmp_path / "studio.sqlite3"))


def test_default_eight_routes_reference_unverified_editable_target_models(tmp_path):
    registry = registry_for(tmp_path)
    public = registry.public("mock")
    assert set(public["roles"]) == {
        "schematic",
        "power_reasoning",
        "software",
        "datasheet",
        "calculation",
        "automation",
        "batch",
        "illustration",
    }
    assert {entry["provider"] for entry in public["models"]} >= {
        "qwen",
        "openai",
        "claude",
        "moonshot",
        "deepseek",
    }
    assert all(not entry["verified"] for entry in public["models"])
    assert all(model_id in registry.models for model_id in public["roles"].values())
    assert registry.get(public["roles"]["automation"]).kind == "agent_runtime"
    assert registry.get(public["roles"]["illustration"]).kind == "image_generation"


def test_registry_updates_routes_and_models_durably_without_storing_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HW_OPENAI_API_KEY", SECRET_SENTINEL)
    registry = registry_for(tmp_path)
    payload = write_payload(registry.public("mock"))
    entry = next(item for item in payload["models"] if item["id"] == "openai-power")
    entry["model"] = "account-specific-reviewed-model"
    entry["parameters"] = {"max_output_tokens": 1024}
    payload["roles"]["batch"] = entry["id"]
    registry.update(RegistryUpdate.model_validate(payload))
    restored = registry_for(tmp_path)
    assert restored.roles["batch"] == "openai-power"
    assert restored.get("openai-power").model == "account-specific-reviewed-model"
    assert restored.get("openai-power").parameters["max_output_tokens"] == 1024
    assert SECRET_SENTINEL not in json.dumps(restored.public("real"))
    assert SECRET_SENTINEL not in restored.store.get_setting("model_registry")
    assert (
        next(item for item in restored.public("real")["models"] if item["id"] == "openai-power")[
            "credential_present"
        ]
        is True
    )


def test_client_cannot_grant_verified_status(tmp_path):
    registry = registry_for(tmp_path)
    payload = write_payload(registry.public("mock"))
    payload["models"][0]["verified"] = True
    payload["models"][0]["verified_capabilities"] = ["text", "vision"]
    with pytest.raises(RegistryError, match="Verification"):
        registry.update(RegistryUpdate.model_validate(payload))
    assert not registry.get(payload["models"][0]["id"]).verified


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "different-model"),
        ("base_url", "https://alternative.example/v1"),
        ("parameters", {"max_output_tokens": 700}),
        ("capabilities", ["text", "vision"]),
        ("credential_env", "HW_OTHER_API_KEY"),
    ],
)
def test_changing_verified_connection_configuration_invalidates_verification(
    tmp_path, field, value
):
    registry = registry_for(tmp_path)
    registry.mark_verified("openai-power")
    payload = write_payload(registry.public("real"))
    entry = next(item for item in payload["models"] if item["id"] == "openai-power")
    entry[field] = value
    registry.update(RegistryUpdate.model_validate(payload))
    changed = registry.get("openai-power")
    assert changed.verified is False
    assert changed.verified_capabilities == []


def test_unchanged_verified_entry_keeps_only_server_verified_capabilities(tmp_path):
    registry = registry_for(tmp_path)
    registry.mark_verified("openai-power")
    payload = write_payload(registry.public("real"))
    entry = next(item for item in payload["models"] if item["id"] == "openai-power")
    entry["verified_capabilities"] = ["text", "vision", "image_generation"]
    registry.update(RegistryUpdate.model_validate(payload))
    assert registry.get("openai-power").verified
    assert registry.get("openai-power").verified_capabilities == ["text"]


@pytest.mark.parametrize(
    "parameters",
    [
        {"api_key": SECRET_SENTINEL},
        {"headers": {"Authorization": SECRET_SENTINEL}},
        {"nested": [{"access_token": SECRET_SENTINEL}]},
    ],
)
def test_registry_cannot_be_used_as_a_credential_store(tmp_path, parameters):
    entry = registry_for(tmp_path).get("openai-power").model_dump()
    entry["parameters"] = parameters
    with pytest.raises(ValidationError, match="Credentials"):
        ModelEntry.model_validate(entry)


def test_invalid_route_update_does_not_replace_good_saved_configuration(tmp_path):
    registry = registry_for(tmp_path)
    good = registry.public("mock")
    payload = write_payload(good)
    payload["roles"]["power_reasoning"] = "not-a-registered-model"
    with pytest.raises(RegistryError, match="existing model"):
        registry.update(RegistryUpdate.model_validate(payload))
    assert registry_for(tmp_path).public("mock") == good


def test_unverified_real_model_is_rejected_even_when_credentials_are_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HW_OPENAI_API_KEY", SECRET_SENTINEL)
    registry = registry_for(tmp_path)
    with pytest.raises(RegistryError, match="connection test"):
        registry.adapter_for(registry.get("openai-power"))


def mock_openai_connection(monkeypatch, handler):
    original = ModelRegistry.adapter_for

    def adapter_for(self, entry, *, verified=True):
        if entry.id != "openai-power":
            return original(self, entry, verified=verified)
        # This is the production HTTP adapter with an HTTP-level test transport.
        # The registry still checks credential presence and verification itself.
        return OpenAIAdapter(
            ProviderConfig(
                api_key=self.credential(entry),
                model=entry.model,
                base_url=entry.base_url,
                validated=entry.verified,
                parameters=entry.parameters,
                input_price_per_million=entry.pricing.input_per_million,
                output_price_per_million=entry.pricing.output_per_million,
            ),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(ModelRegistry, "adapter_for", adapter_for)


def test_api_connection_test_uses_real_adapter_http_and_persists_exact_text_verification(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HW_OPENAI_API_KEY", SECRET_SENTINEL)
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == f"Bearer {SECRET_SENTINEL}"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-astra"
        assert body["max_output_tokens"] == 8
        return httpx.Response(
            200,
            json={
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        )

    mock_openai_connection(monkeypatch, handler)
    settings = Settings(data_dir=tmp_path, _env_file=None)
    with TestClient(create_app(settings)) as client:
        tested = client.post("/api/models/openai-power/test")
        assert tested.status_code == 200, tested.text
        assert tested.json()["ok"] is True
        assert tested.json()["usage_source"] == "provider"
        assert tested.json()["capabilities_verified"] == ["text"]
        assert len(requests) == 1
        public = client.get("/api/models").json()
        entry = next(item for item in public["models"] if item["id"] == "openai-power")
        assert entry["verified"] is True
        assert entry["verified_capabilities"] == ["text"]
        assert SECRET_SENTINEL not in json.dumps(public)
        assert SECRET_SENTINEL not in tested.text
    with TestClient(create_app(settings)) as client:
        restored = client.get("/api/models").json()
        assert next(item for item in restored["models"] if item["id"] == "openai-power")["verified"]
        assert len(requests) == 1  # App restarts must not initiate paid API calls.


def test_failed_api_connection_does_not_verify_or_echo_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HW_OPENAI_API_KEY", SECRET_SENTINEL)

    def rejected(request):
        return httpx.Response(401, json={"error": {"message": f"invalid secret {SECRET_SENTINEL}"}})

    mock_openai_connection(monkeypatch, rejected)
    with TestClient(create_app(Settings(data_dir=tmp_path, _env_file=None))) as client:
        tested = client.post("/api/models/openai-power/test")
        assert tested.status_code in {400, 422, 502}
        assert SECRET_SENTINEL not in tested.text
        public = client.get("/api/models").json()
        assert not next(item for item in public["models"] if item["id"] == "openai-power")[
            "verified"
        ]


def test_missing_api_credentials_are_actionable_and_never_attempt_network(tmp_path, monkeypatch):
    monkeypatch.delenv("HW_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("app.registry.dotenv_values", lambda _: {})
    with TestClient(create_app(Settings(data_dir=tmp_path, _env_file=None))) as client:
        response = client.post("/api/models/openai-power/test")
        assert response.status_code == 422
        assert "HW_OPENAI_API_KEY" in response.text


def test_api_rejects_secret_fields_without_echoing_them_in_validation_errors(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, _env_file=None))) as client:
        original = client.get("/api/models").json()
        payload = write_payload(original)
        payload["models"][0]["parameters"] = {"headers": {"Authorization": SECRET_SENTINEL}}
        response = client.put("/api/models", json=payload)
        assert response.status_code == 422
        assert SECRET_SENTINEL not in response.text
        assert client.get("/api/models").json() == original


def test_real_mode_starts_without_keys_but_refuses_unverified_paid_work(tmp_path, monkeypatch):
    monkeypatch.setattr("app.registry.dotenv_values", lambda _: {})
    for name in ("HW_QWEN_API_KEY", "HW_OPENAI_API_KEY", "HW_CLAUDE_API_KEY", "HW_MOONSHOT_API_KEY", "HW_DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(Settings(data_dir=tmp_path, model_mode="real", _env_file=None))) as client:
        assert client.get("/health").json()["mode"] == "real"
        response = client.post("/api/tasks", json={"prompt": "Analyse converter"})
        assert response.status_code == 422
        assert client.get("/api/tasks").json() == []
