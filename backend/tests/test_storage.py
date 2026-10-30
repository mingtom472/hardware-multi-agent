import asyncio
from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from app.storage import LocalFileStore
from fastapi.testclient import TestClient


def test_stored_file_count_quota_preserves_existing_upload(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, max_stored_files=1, _env_file=None)
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/uploads?filename=first.bin", content=b"first")
        assert first.status_code == 201
        second = client.post("/api/uploads?filename=second.bin", content=b"second")
        assert second.status_code == 507
        assert "count limit" in second.json()["detail"]
        assert client.get(f"/api/uploads/{first.json()['id']}").json() == first.json()
    assert len(list((tmp_path / "uploads").glob("*.bin"))) == 1
    assert not list((tmp_path / "uploads").glob("*.part"))


def test_interrupted_stream_removes_partial_file(tmp_path: Path):
    async def check():
        store = LocalFileStore(Settings(data_dir=tmp_path, _env_file=None))

        async def interrupted_chunks():
            yield b"partial bytes"
            raise RuntimeError("client connection interrupted")

        with pytest.raises(RuntimeError, match="interrupted"):
            await store.put(interrupted_chunks(), "interrupted.bin", "application/octet-stream")
        assert list(store.directory.iterdir()) == []

        async def healthy_chunks():
            yield b"complete bytes"

        metadata = await store.put(healthy_chunks(), "healthy.bin", "application/octet-stream")
        assert store.get(metadata.id) == metadata
        assert metadata.size == len(b"complete bytes")

    asyncio.run(check())
