import asyncio
from decimal import Decimal

import httpx
import pytest
from app.budget import BudgetLedger
from app.config import Settings
from app.main import create_app
from app.providers import OpenAIAdapter, ProviderConfig
from app.registry import ModelRegistry
from app.schemas import TaskRecord, WorkflowConfig
from app.workflow import WorkflowEngine


@pytest.mark.parametrize("response_kind", ["invalid_json", "empty_output", "invalid_structure"])
def test_accepted_but_unusable_response_retains_unknown_billing(tmp_path, response_kind):
    def handler(_request):
        if response_kind == "invalid_json":
            return httpx.Response(200, text="not JSON")
        body = {"output": [] if response_kind == "empty_output" else [123]}
        return httpx.Response(200, json=body)

    provider = OpenAIAdapter(
        ProviderConfig(api_key="test-only-credential", model="test-model", validated=True),
        transport=httpx.MockTransport(handler),
    )
    ledger = BudgetLedger(tmp_path / "billing.sqlite3")
    task = TaskRecord(
        id="accepted-response", prompt="Check hardware", mode="real",
        workflow=WorkflowConfig(
            reviewers=["openai"], vision_provider="openai", validator="openai",
            summarizer="openai", max_retries=0, estimated_call_cost_usd=Decimal("0.01"),
        ),
    )
    asyncio.run(WorkflowEngine({"openai": provider}, ledger=ledger).run(task, []))
    assert task.status == "failed"
    assert len(task.calls) == 1
    assert task.calls[0].cost_source == "unknown"
    assert task.calls[0].simulated is False
    entry, = ledger.export_rows(task.id)
    assert entry["status"] == "unknown"
    assert Decimal(entry["accounted_usd"]) == Decimal("0.01")
    assert entry["actual_cost_usd"] is None


def test_connection_test_cannot_verify_a_configuration_changed_during_the_request(tmp_path, monkeypatch):
    async def check():
        request_started = asyncio.Event()
        finish_response = asyncio.Event()

        async def handler(_request):
            request_started.set()
            await finish_response.wait()
            return httpx.Response(200, json={
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            })

        def adapter_for(_registry, entry, **_kwargs):
            return OpenAIAdapter(
                ProviderConfig(api_key="test-only-credential", model=entry.model),
                transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr(ModelRegistry, "adapter_for", adapter_for)
        app = create_app(Settings(data_dir=tmp_path, _env_file=None))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                in_flight = asyncio.create_task(client.post("/api/models/openai-power/test"))
                try:
                    await asyncio.wait_for(request_started.wait(), timeout=2)
                    registry = app.state.registry
                    models = [entry.model_dump(mode="json") for entry in registry.models.values()]
                    next(item for item in models if item["id"] == "openai-power")["model"] = "changed-unverified"
                    updated = await client.put("/api/models", json={"models": models, "roles": registry.roles})
                    assert updated.status_code == 200
                finally:
                    finish_response.set()
                result = await asyncio.wait_for(in_flight, timeout=2)
                assert result.status_code == 422
                assert "changed during the test" in result.json()["detail"]
                current = app.state.registry.get("openai-power")
                assert current.model == "changed-unverified"
                assert current.verified is False
                assert len(app.state.ledger.export_rows()) == 1

    asyncio.run(check())
