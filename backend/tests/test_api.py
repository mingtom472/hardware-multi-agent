import asyncio
import hashlib
import time
from collections import namedtuple
from decimal import Decimal

import pytest
from app.config import Settings
from app.main import create_app
from app.providers import MockProvider
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path, max_upload_bytes=32, max_storage_bytes=128, _env_file=None
    )
    with TestClient(create_app(settings)) as instance:
        yield instance


def wait_for_task(client: TestClient, task_id: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in {"completed", "failed"}:
            return task
        time.sleep(0.01)
    pytest.fail("Task did not reach a terminal state before the deadline")


def upload(
    client: TestClient, content: bytes, filename="board.bin", mime="application/octet-stream"
):
    return client.post(
        "/api/uploads",
        params={"filename": filename},
        content=content,
        headers={"content-type": mime},
    )


def test_health_catalog_and_workflow_are_available_without_credentials(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["mode"] == "mock"
    providers = client.get("/api/providers").json()
    assert {item["id"] for item in providers} >= {
        "qwen",
        "openai",
        "claude",
        "moonshot",
        "deepseek",
    }
    assert all(item["mode"] == "mock" for item in providers)
    workflow = client.get("/api/workflows/default").json()
    assert len(workflow["reviewers"]) >= 1
    assert client.get("/api/capabilities").status_code == 200


@pytest.mark.parametrize("custom_workflow", [False, True])
def test_server_budget_defaults_stop_calls_even_with_custom_role_selection(tmp_path, custom_workflow):
    settings = Settings(
        data_dir=tmp_path, default_task_budget_usd=Decimal("0.001"),
        estimated_call_cost_usd=Decimal("0.02"), _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        defaults = client.get("/api/workflows/default").json()
        assert Decimal(defaults["max_cost_usd"]) == Decimal("0.001")
        assert Decimal(defaults["estimated_call_cost_usd"]) == Decimal("0.02")
        payload = {"prompt": "Review a Buck converter"}
        if custom_workflow:
            payload["workflow"] = {"strategy": "manual", "reviewers": ["deepseek"]}
        created = client.post("/api/tasks", json=payload)
        assert created.status_code == 202
        finished = wait_for_task(client, created.json()["id"])
        assert finished["status"] == "failed"
        assert "budget" in finished["error"].lower()
        assert finished["calls"] == []
        assert client.get("/api/costs", params={"task_id": finished["id"]}).json()["entries"] == []


def test_binary_upload_preserves_actual_size_digest_and_metadata(client):
    content = bytes(range(32))
    response = upload(client, content)
    assert response.status_code in {200, 201}
    metadata = response.json()
    assert metadata["size"] == len(content)
    assert metadata["sha256"] == hashlib.sha256(content).hexdigest()
    assert metadata["filename"] == "board.bin"
    fetched = client.get(f"/api/uploads/{metadata['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == metadata


def test_streamed_limit_uses_received_bytes_and_cleans_up_partial_upload(client, tmp_path):
    # Deliberately misleading header: the actual stream must still be bounded.
    response = client.post(
        "/api/uploads?filename=oversized.bin",
        content=iter([b"a" * 16, b"b" * 17]),
        headers={"content-type": "application/octet-stream", "content-length": "1"},
    )
    assert response.status_code == 413
    assert not any(path.is_file() for path in (tmp_path / "uploads").rglob("*"))
    # A rejected stream must not reserve disk quota for subsequent requests.
    assert upload(client, b"x" * 32).status_code in {200, 201}


def test_empty_upload_is_rejected_and_does_not_leave_files(client, tmp_path):
    assert upload(client, b"").status_code == 400
    assert not any(path.is_file() for path in (tmp_path / "uploads").rglob("*"))


def test_storage_quota_is_enforced_without_retaining_failed_upload(tmp_path):
    settings = Settings(data_dir=tmp_path, max_upload_bytes=8, max_storage_bytes=10)
    with TestClient(create_app(settings)) as client:
        first = upload(client, b"123456")
        assert first.status_code in {200, 201}
        assert upload(client, b"78901").status_code == 507
        assert upload(client, b"7890").status_code in {200, 201}
        assert client.get(f"/api/uploads/{first.json()['id']}").status_code == 200


def test_uploaded_image_runs_complete_mock_pipeline_with_consistent_usage(client):
    response = upload(client, b"mock-image-pixels", "schematic.png", "image/png")
    assert response.status_code in {200, 201}
    created = client.post(
        "/api/tasks",
        json={
            "prompt": "分析 24V 转 5V Buck 电源",
            "file_ids": [response.json()["id"]],
            "workflow": {
                "strategy": "manual",
                "profile": "professional",
                "reviewers": ["openai", "deepseek"],
                "vision_provider": "qwen",
                "validator": "claude",
                "summarizer": "openai",
                "orchestrator": "deepseek",
            },
        },
    )
    assert created.status_code == 202
    task = wait_for_task(client, created.json()["id"])
    assert task["status"] == "completed", task
    stages = {record["stage"] for record in task["calls"]}
    assert stages == {"classification", "vision", "hardware_analysis", "validation", "summary"}
    assert "模拟" in str(task["result"])
    assert task["result"]["summary"]
    assert task["usage"]["simulated"] is True
    assert task["usage"]["input_tokens"] == sum(record["input_tokens"] for record in task["calls"])
    assert task["usage"]["output_tokens"] == sum(
        record["output_tokens"] for record in task["calls"]
    )
    assert Decimal(str(task["usage"]["cost_usd"])) == sum(
        Decimal(str(record["cost_usd"])) for record in task["calls"]
    )


def test_text_only_task_skips_vision(client):
    created = client.post("/api/tasks", json={"prompt": "检查 BOM 参数"})
    assert created.status_code == 202
    task = wait_for_task(client, created.json()["id"])
    assert task["status"] == "completed"
    assert "vision" not in {record["stage"] for record in task["calls"]}


@pytest.mark.parametrize(
    "workflow",
    [
        {"strategy": "manual", "reviewers": ["unknown"]},
        {"reviewers": ["openai", "openai"]},
        {"max_retries": 6},
        {"max_parallel": 0},
        {"timeout_seconds": 0},
        {"unexpected": True},
    ],
)
def test_invalid_workflow_is_rejected_before_task_creation(client, workflow):
    response = client.post("/api/tasks", json={"prompt": "Check hardware", "workflow": workflow})
    assert response.status_code == 422


def test_missing_file_task_and_blank_prompt_are_rejected(client):
    assert client.post("/api/tasks", json={"prompt": "   "}).status_code == 422
    assert (
        client.post(
            "/api/tasks", json={"prompt": "Check", "file_ids": ["missing-file"]}
        ).status_code
        == 404
    )
    assert client.get("/api/uploads/nonexistent").status_code == 404
    assert client.get("/api/tasks/nonexistent").status_code == 404


def test_metadata_survives_application_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        metadata = upload(client, b"persistent bytes").json()
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/uploads/{metadata['id']}")
        assert response.status_code == 200
        assert response.json() == metadata


def test_active_task_limit_rejects_excess_work(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, max_active_tasks=1)
    original_complete = MockProvider.complete

    async def slower_complete(self, request):
        await asyncio.sleep(0.05)
        return await original_complete(self, request)

    monkeypatch.setattr(MockProvider, "complete", slower_complete)
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/tasks", json={"prompt": "Check converter"})
        assert first.status_code == 202
        assert client.post("/api/tasks", json={"prompt": "Too much work"}).status_code == 429
        assert wait_for_task(client, first.json()["id"])["status"] == "completed"


def test_default_upload_limits_are_resource_based_and_unknown_formats_are_preserved(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    assert settings.max_upload_bytes is None
    assert settings.max_storage_bytes is None
    with TestClient(create_app(settings)) as client:
        # No file extension whitelist; a future parser may consume the retained original.
        response = upload(client, b"arbitrary binary format", "measurement.vendor-custom")
        assert response.status_code == 201
        metadata = response.json()
        assert metadata["parser_supported"] is False
        assert metadata["parser_status"] in {"unparsed", "unsupported", "not_parsed"}
        assert metadata["sha256"] == hashlib.sha256(b"arbitrary binary format").hexdigest()


def test_disk_space_is_rechecked_while_writing_and_rejection_cleans_partial_file(
    tmp_path, monkeypatch
):
    usage = namedtuple("DiskUsage", "total used free")
    free_space = iter([4096, 8])
    monkeypatch.setattr("app.storage.shutil.disk_usage", lambda _: usage(8192, 0, next(free_space)))
    settings = Settings(
        data_dir=tmp_path,
        upload_chunk_bytes=1024,
        storage_reserve_bytes=16,
        _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        response = upload(client, b"x" * 2048)
        assert response.status_code == 507
        assert "disk space" in response.text.lower()
        assert "DATA_DIR" in response.text
        assert not any(path.is_file() for path in (tmp_path / "uploads").rglob("*"))
        monkeypatch.setattr("app.storage.shutil.disk_usage", lambda _: usage(8192, 0, 8192))
        assert upload(client, b"x" * 2048).status_code == 201
