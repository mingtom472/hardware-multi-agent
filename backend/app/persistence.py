"""Small durable SQLite snapshots, independent from live asyncio processes."""

import sqlite3
import threading
from pathlib import Path

from .schemas import TaskEvent, TaskRecord, utc_now


class SQLiteTaskStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL, snapshot TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def save(self, task: TaskRecord) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO tasks(id,status,updated_at,snapshot) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,snapshot=excluded.snapshot",
                (task.id, task.status, task.updated_at.isoformat(), task.model_dump_json()),
            )

    def get(self, task_id: str) -> TaskRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT snapshot FROM tasks WHERE id=?", (task_id,)).fetchone()
        return TaskRecord.model_validate_json(row[0]) if row else None

    def list(self, limit: int | None = None) -> list[TaskRecord]:
        query = "SELECT snapshot FROM tasks ORDER BY updated_at DESC"
        params = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        with self._connect() as db:
            return [TaskRecord.model_validate_json(row[0]) for row in db.execute(query, params)]

    def delete(self, task_id: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount > 0

    def recover_interrupted(self) -> int:
        recovered = 0
        with self._connect() as db:
            rows = db.execute("SELECT snapshot FROM tasks WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            task = TaskRecord.model_validate_json(row[0])
            task.status = "failed"
            task.recoverable = True
            task.error = "Service restarted before task finished; retry explicitly to recover"
            task.updated_at = utc_now()
            task.events.append(TaskEvent(stage="recovery", message=task.error))
            self.save(task)
            recovered += 1
        return recovered

    def references_file(self, file_id: str, active_only: bool = True) -> bool:
        query = "SELECT snapshot FROM tasks"
        if active_only:
            query += " WHERE status IN ('queued','running','awaiting_review')"
        with self._connect() as db:
            return any(file_id in TaskRecord.model_validate_json(row[0]).file_ids for row in db.execute(query))

    def get_setting(self, name: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def save_setting(self, name: str, value: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
