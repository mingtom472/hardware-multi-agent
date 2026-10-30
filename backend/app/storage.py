import asyncio
import errno
import hashlib
import re
import shutil
import unicodedata
from collections.abc import AsyncIterator
from uuid import uuid4

from .config import Settings
from .schemas import FileMetadata


class StorageError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    name = name.strip()[:255]
    if not name or name in {".", ".."}:
        raise StorageError(422, "A valid filename is required")
    return name


class LocalFileStore:
    """Bounded streaming storage. Files are inert; no parsing or execution occurs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.directory = settings.data_dir / "uploads"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write_lock = asyncio.Lock()

    def list(self) -> list[FileMetadata]:
        result = []
        for path in self.directory.glob("*.json"):
            metadata = self.get(path.stem)
            if metadata is not None:
                result.append(metadata)
        return sorted(result, key=lambda metadata: metadata.uploaded_at, reverse=True)

    def disk_status(self) -> dict:
        usage = shutil.disk_usage(self.directory)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "reserve_bytes": self.settings.storage_reserve_bytes,
            "stored_bytes": sum(path.stat().st_size for path in self.directory.glob("*.bin")),
        }

    async def delete(self, file_id: str) -> bool:
        async with self._write_lock:
            if self.get(file_id) is None:
                return False
            try:
                (self.directory / f"{file_id}.bin").unlink()
                (self.directory / f"{file_id}.json").unlink()
            except OSError:
                raise StorageError(500, "Unable to delete file; check local filesystem permissions") from None
            return True

    def get(self, file_id: str) -> FileMetadata | None:
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            return None
        path = self.directory / f"{file_id}.json"
        if not path.exists() or not (self.directory / f"{file_id}.bin").is_file():
            return None
        try:
            return FileMetadata.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise StorageError(500, "Stored file metadata is unavailable") from None

    async def put(
        self, chunks: AsyncIterator[bytes], filename: str, content_type: str
    ) -> FileMetadata:
        filename = safe_filename(filename)
        content_type = content_type.split(";", 1)[0].strip()[:128]
        if not content_type or any(c in content_type for c in "\r\n"):
            content_type = "application/octet-stream"
        file_id = uuid4().hex
        partial = self.directory / f"{file_id}.part"
        binary = self.directory / f"{file_id}.bin"
        metadata_path = self.directory / f"{file_id}.json"
        metadata_partial = self.directory / f"{file_id}.json.part"
        committed = False
        async with self._write_lock:
            if sum(1 for _ in self.directory.glob("*.bin")) >= self.settings.max_stored_files:
                raise StorageError(507, "Local stored-file count limit exceeded")
            used = sum(
                path.stat().st_size
                for path in self.directory.iterdir()
                if path.suffix in {".bin", ".part"} and path.is_file()
            )
            size = 0
            digest = hashlib.sha256()
            try:
                with partial.open("xb") as handle:
                    async for chunk in chunks:
                        if not chunk:
                            continue
                        if self.settings.max_upload_bytes is not None and size + len(chunk) > self.settings.max_upload_bytes:
                            raise StorageError(413, "File exceeds configured upload limit")
                        if self.settings.max_storage_bytes is not None and used + size + len(chunk) > self.settings.max_storage_bytes:
                            raise StorageError(507, "Local storage quota exceeded")
                        for start in range(0, len(chunk), self.settings.upload_chunk_bytes):
                            piece = chunk[start : start + self.settings.upload_chunk_bytes]
                            free = shutil.disk_usage(self.directory).free
                            if free - len(piece) < self.settings.storage_reserve_bytes:
                                raise StorageError(507, "Insufficient free disk space; delete local files or choose a larger DATA_DIR volume")
                            handle.write(piece)
                            digest.update(piece)
                        size += len(chunk)
                if size == 0:
                    raise StorageError(400, "Empty uploads are not supported")
                metadata = FileMetadata(
                    id=file_id,
                    filename=filename,
                    content_type=content_type,
                    size=size,
                    sha256=digest.hexdigest(),
                    storage_location=f"local:{file_id}",
                )
                metadata_partial.write_text(metadata.model_dump_json(), encoding="utf-8")
                partial.replace(binary)
                metadata_partial.replace(metadata_path)
                committed = True
                return metadata
            except OSError as exc:
                if exc.errno in {errno.ENOSPC, errno.EDQUOT, errno.EFBIG}:
                    raise StorageError(507, "Filesystem space or file-size limit reached; free disk space or choose another DATA_DIR volume") from None
                raise StorageError(500, "Local file operation failed; check filesystem permissions and disk health") from None
            finally:
                partial.unlink(missing_ok=True)
                metadata_partial.unlink(missing_ok=True)
                if not committed:
                    binary.unlink(missing_ok=True)
                    metadata_path.unlink(missing_ok=True)
