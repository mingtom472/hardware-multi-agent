"""Reserved infrastructure ports. These are contracts, not active integrations.

The MVP uses local files and in-memory tasks. Supplying a PostgreSQL, Redis, or
S3 endpoint does not switch backends until an implementation is supplied.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from .schemas import FileMetadata, TaskRecord


class TaskRepository(Protocol):
    """Future PostgreSQL implementation, including transactional task state."""

    async def save(self, task: TaskRecord) -> None: ...

    async def get(self, task_id: str) -> TaskRecord | None: ...


class TaskQueue(Protocol):
    """Future Redis-backed worker dispatch and restart-safe job processing."""

    async def enqueue(self, task_id: str) -> None: ...


class ObjectStorage(Protocol):
    async def put(self, chunks: AsyncIterator[bytes], filename: str) -> FileMetadata: ...

    async def delete(self, object_id: str) -> None: ...


class MultipartStorage(Protocol):
    """Reserved resumable multipart API; not implemented by the local backend."""

    async def initiate(self, filename: str, total_bytes: int) -> str: ...

    async def upload_part(self, upload_id: str, number: int, data: bytes) -> str: ...

    async def complete(self, upload_id: str, part_etags: list[str]) -> FileMetadata: ...

    async def abort(self, upload_id: str) -> None: ...
