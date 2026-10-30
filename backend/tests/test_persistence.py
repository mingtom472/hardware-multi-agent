import asyncio
import time
from decimal import Decimal
from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from app.persistence import SQLiteTaskStore
from app.providers import MockProvider
from app.schemas import CallRecord, TaskEvent, TaskRecord, WorkflowConfig
from fastapi.testclient import TestClient


def task_record(task_id: str, status="completed", file_ids=None):
    return TaskRecord(
        id=task_id,
        prompt="检查 Buck 输入与输出条件",
        status=status,
        file_ids=file_ids or [],
        workflow=WorkflowConfig(),
        calls=[
            CallRecord(
                stage="hardware_analysis",
                provider="openai",
                attempt=1,
                status="completed",
                input_tokens=120,
                output_tokens=35,
                cost_usd=Decimal("0.00015"),
                output="待实际测量验证",
                stage_input={"prompt": "检查 Buck", "context": {"voltage": 24}},
                stage_output={"text": "待实际测量验证"},
            )
        ],
        events=[TaskEvent(stage="hardware_analysis", message="Analysis finished")],
        result={"summary": "理论推导，尚未经测量确认"},
    )


def test_sqlite_roundtrip_preserves_stage_evidence_and_decimal_usage(tmp_path: Path):
    path = tmp_path / "nested" / "tasks.sqlite3"
    original = task_record("task-one", file_ids=["original-image"])
    SQLiteTaskStore(path).save(original)
    # A fresh instance must read the on-disk snapshot, not an in-process cache.
    restored = SQLiteTaskStore(path).get(original.id)
    assert restored is not None
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")
    assert restored.calls[0].cost_usd == Decimal("0.00015")
    assert restored.calls[0].stage_input["context"]["voltage"] == 24
    assert restored.file_ids == ["original-image"]


def test_saving_updates_one_snapshot_and_delete_does_not_remove_other_history(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    first, second = task_record("one"), task_record("two")
    store.save(first)
    store.save(second)
    first.result["summary"] = "updated conclusion"
    store.save(first)
    assert len(store.list()) == 2
    assert store.get("one").result["summary"] == "updated conclusion"
    assert store.delete("one") is True
    assert store.delete("one") is False
    assert store.get("one") is None
    assert store.get("two").model_dump() == second.model_dump()
    assert len(SQLiteTaskStore(tmp_path / "tasks.db").list()) == 1


def test_restart_recovery_marks_only_interrupted_work_and_is_idempotent(tmp_path):
    path = tmp_path / "tasks.db"
    store = SQLiteTaskStore(path)
    for status in ("queued", "running", "completed", "failed", "cancelled", "awaiting_review"):
        store.save(task_record(status, status=status))
    restarted = SQLiteTaskStore(path)
    assert restarted.recover_interrupted() == 2
    for task_id in ("queued", "running"):
        restored = restarted.get(task_id)
        assert restored.status == "failed"
        assert restored.recoverable is True
        assert "restart" in restored.error.lower()
        assert any(event.stage == "recovery" for event in restored.events)
        assert restored.calls[0].stage_input["context"]["voltage"] == 24
    for status in ("completed", "failed", "cancelled", "awaiting_review"):
        assert restarted.get(status).status == status
    assert restarted.recover_interrupted() == 0


def test_active_file_references_include_human_review_and_exclude_finished_tasks(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    store.save(task_record("finished", file_ids=["completed-file"]))
    store.save(task_record("review", status="awaiting_review", file_ids=["review-file"]))
    assert not store.references_file("completed-file", active_only=True)
    assert store.references_file("completed-file", active_only=False)
    assert store.references_file("review-file", active_only=True)
    assert not store.references_file("unknown")


def test_task_ids_are_sql_parameters_and_cannot_delete_unrelated_records(tmp_path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    store.save(task_record("normal"))
    unusual = "x'; DELETE FROM tasks; --"
    store.save(task_record(unusual))
    assert store.get(unusual).id == unusual
    assert store.delete(unusual) is True
    assert store.get("normal") is not None


def test_nonsecret_settings_survive_restart_and_replace_the_same_key(tmp_path):
    path = tmp_path / "tasks.db"
    store = SQLiteTaskStore(path)
    store.save_setting("routing", '{"analysis":"openai"}')
    store.save_setting("routing", '{"analysis":"deepseek"}')
    restarted = SQLiteTaskStore(path)
    assert restarted.get_setting("routing") == '{"analysis":"deepseek"}'
    assert restarted.get_setting("missing") is None


def wait_terminal(client, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in {"completed", "failed", "cancelled", "awaiting_review"}:
            return task
        time.sleep(0.01)
    pytest.fail("Task did not settle")


def test_completed_task_history_and_original_upload_survive_api_restart(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    with TestClient(create_app(settings)) as client:
        uploaded = client.post(
            "/api/uploads?filename=unparsed.custom-binary", content=b"original raw data"
        )
        assert uploaded.status_code == 201
        metadata = uploaded.json()
        created = client.post(
            "/api/tasks",
            json={
                "prompt": "检查 Buck 条件与所需补充信息",
                "file_ids": [metadata["id"]],
            },
        )
        assert created.status_code == 202
        finished = wait_terminal(client, created.json()["id"])
        assert finished["status"] == "completed", finished
        assert finished["calls"]
        assert all(
            call["stage_input"] for call in finished["calls"] if call["status"] == "completed"
        )
        assert all(
            call["stage_output"] for call in finished["calls"] if call["status"] == "completed"
        )
    with TestClient(create_app(settings)) as client:
        restored = client.get(f"/api/tasks/{finished['id']}")
        assert restored.status_code == 200
        assert restored.json() == finished
        assert finished["id"] in {task["id"] for task in client.get("/api/tasks").json()}
        assert client.get(f"/api/uploads/{metadata['id']}").json() == metadata
        assert metadata["id"] in {file["id"] for file in client.get("/api/uploads").json()}
        assert metadata["parser_supported"] is False


def test_cancel_retry_creates_new_task_and_preserves_original_history(tmp_path, monkeypatch):
    original_complete = MockProvider.complete

    async def slow_complete(self, request):
        await asyncio.sleep(0.5)
        return await original_complete(self, request)

    monkeypatch.setattr(MockProvider, "complete", slow_complete)
    settings = Settings(data_dir=tmp_path, _env_file=None)
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/tasks", json={"prompt": "Review power converter"})
        assert created.status_code == 202
        task_id = created.json()["id"]
        assert client.delete(f"/api/tasks/{task_id}").status_code == 409
        cancelled_response = client.post(f"/api/tasks/{task_id}/cancel")
        assert cancelled_response.status_code == 200
        cancelled = wait_terminal(client, task_id)
        assert cancelled["status"] == "cancelled"
        monkeypatch.setattr(MockProvider, "complete", original_complete)
        retried = client.post(f"/api/tasks/{task_id}/retry")
        assert retried.status_code == 202
        new_task = retried.json()
        assert new_task["id"] != task_id
        assert new_task["parent_task_id"] == task_id
        assert wait_terminal(client, new_task["id"])["status"] == "completed"
        assert client.get(f"/api/tasks/{task_id}").json() == cancelled
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/tasks/{task_id}").json() == cancelled
        assert client.get(f"/api/tasks/{new_task['id']}").json()["status"] == "completed"


def test_delete_history_and_upload_are_separate_persistent_operations(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    with TestClient(create_app(settings)) as client:
        uploaded = client.post("/api/uploads?filename=original.bin", content=b"original")
        assert uploaded.status_code == 201
        file_id = uploaded.json()["id"]
        response = client.post(
            "/api/tasks", json={"prompt": "Review hardware", "file_ids": [file_id]}
        )
        assert response.status_code == 202
        task = wait_terminal(client, response.json()["id"])
        assert task["status"] == "completed"
        assert client.delete(f"/api/tasks/{task['id']}").status_code in {200, 204}
        assert client.get(f"/api/tasks/{task['id']}").status_code == 404
        assert client.get(f"/api/uploads/{file_id}").status_code == 200
        assert client.delete(f"/api/uploads/{file_id}").status_code in {200, 204}
        assert client.get(f"/api/uploads/{file_id}").status_code == 404
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/tasks/{task['id']}").status_code == 404
        assert client.get(f"/api/uploads/{file_id}").status_code == 404


def test_upload_referenced_by_active_task_cannot_be_deleted(tmp_path, monkeypatch):
    original_complete = MockProvider.complete

    async def slow_complete(self, request):
        await asyncio.sleep(0.5)
        return await original_complete(self, request)

    monkeypatch.setattr(MockProvider, "complete", slow_complete)
    with TestClient(create_app(Settings(data_dir=tmp_path, _env_file=None))) as client:
        file_id = client.post("/api/uploads?filename=original.bin", content=b"original").json()[
            "id"
        ]
        created = client.post(
            "/api/tasks", json={"prompt": "Review hardware", "file_ids": [file_id]}
        )
        assert created.status_code == 202
        task_id = created.json()["id"]
        assert client.delete(f"/api/uploads/{file_id}").status_code == 409
        assert client.get(f"/api/uploads/{file_id}").status_code == 200
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
        assert wait_terminal(client, task_id)["status"] == "cancelled"
        assert client.delete(f"/api/uploads/{file_id}").status_code in {200, 204}
