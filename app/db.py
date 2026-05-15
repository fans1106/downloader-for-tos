from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def init(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id TEXT NOT NULL,
                    repo_type TEXT NOT NULL CHECK(repo_type IN ('model', 'dataset')),
                    hf_token_encrypted TEXT NOT NULL,
                    tos_target TEXT NOT NULL,
                    revision TEXT,
                    allow_patterns_json TEXT NOT NULL,
                    ignore_patterns_json TEXT NOT NULL,
                    batch_size_limit_bytes INTEGER NOT NULL,
                    max_retries INTEGER NOT NULL,
                    cleanup_local_files INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    queue_position INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT NOT NULL DEFAULT 'queued',
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    completed_download_files INTEGER NOT NULL DEFAULT 0,
                    completed_upload_files INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    local_download_dir TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_queue ON tasks(status, queue_position, id);

                CREATE TABLE IF NOT EXISTS task_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    download_status TEXT NOT NULL DEFAULT 'pending',
                    upload_status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, file_path),
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_task_files_task ON task_files(task_id, id);

                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
                """
            )

    def _task_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        task = dict(row)
        task["allow_patterns"] = json.loads(task.pop("allow_patterns_json") or "[]")
        task["ignore_patterns"] = json.loads(task.pop("ignore_patterns_json") or "[]")
        task["cleanup_local_files"] = bool(task["cleanup_local_files"])
        task["cancel_requested"] = bool(task["cancel_requested"])
        return task

    def _log_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def append_log(self, task_id: int, level: str, stage: str, message: str) -> None:
        created_at = utcnow()
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO task_logs(task_id, level, stage, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, level, stage, message, created_at),
            )

    def list_logs(self, task_id: int, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, created_at, level, stage, message
                FROM task_logs
                WHERE task_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (task_id, after_id, limit),
            ).fetchall()
        return [self._log_from_row(row) for row in rows]

    def next_queue_position(self) -> int:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(queue_position), 0) + 1 AS next_position FROM tasks").fetchone()
        return int(row["next_position"])

    def create_task(
        self,
        payload: dict[str, Any],
        encrypted_token: str,
        local_download_dir: str,
        queue_position: int | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        queue_position = queue_position or self.next_queue_position()
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    repo_id, repo_type, hf_token_encrypted, tos_target, revision,
                    allow_patterns_json, ignore_patterns_json, batch_size_limit_bytes,
                    max_retries, cleanup_local_files, status, queue_position,
                    current_stage, created_at, local_download_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 'queued', ?, ?)
                """,
                (
                    payload["repo_id"],
                    payload["repo_type"],
                    encrypted_token,
                    payload["tos_target"],
                    payload.get("revision"),
                    json.dumps(payload.get("allow_patterns", []), ensure_ascii=False),
                    json.dumps(payload.get("ignore_patterns", []), ensure_ascii=False),
                    payload["batch_size_limit_bytes"],
                    payload["max_retries"],
                    int(payload.get("cleanup_local_files", True)),
                    queue_position,
                    now,
                    local_download_dir,
                ),
            )
            task_id = int(cursor.lastrowid)
        return self.get_task(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, queue_position ASC, id DESC"
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_from_row(row)

    def get_task_files(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, file_path, file_size, download_status, upload_status, last_error, created_at, updated_at FROM task_files WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_task_manifest(self, task_id: int, files: list[dict[str, Any]]) -> None:
        now = utcnow()
        with self._lock, self.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS temp.temp_task_manifest_paths")
            conn.execute("CREATE TEMP TABLE temp_task_manifest_paths(file_path TEXT PRIMARY KEY)")
            conn.executemany(
                """
                INSERT INTO task_files(task_id, file_path, file_size, download_status, upload_status, last_error, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', 'pending', NULL, ?, ?)
                ON CONFLICT(task_id, file_path) DO UPDATE SET
                    file_size = excluded.file_size,
                    updated_at = excluded.updated_at
                """,
                [(task_id, item["file_path"], item["file_size"], now, now) for item in files],
            )
            conn.executemany(
                "INSERT INTO temp_task_manifest_paths(file_path) VALUES (?)",
                [(item["file_path"],) for item in files],
            )
            conn.execute(
                """
                DELETE FROM task_files
                WHERE task_id = ?
                  AND file_path NOT IN (SELECT file_path FROM temp_task_manifest_paths)
                """,
                (task_id,),
            )
            conn.execute(
                """
                UPDATE tasks
                SET total_bytes = (
                        SELECT COALESCE(SUM(file_size), 0) FROM task_files WHERE task_id = ?
                    ),
                    total_files = (
                        SELECT COUNT(*) FROM task_files WHERE task_id = ?
                    )
                WHERE id = ?
                """,
                (task_id, task_id, task_id),
            )
            conn.execute("DROP TABLE temp.temp_task_manifest_paths")
        self.refresh_progress(task_id)

    def claim_next_task(self) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status = 'queued' ORDER BY queue_position ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = utcnow()
            conn.execute(
                """
                UPDATE tasks
                SET status = 'running', current_stage = 'preparing', started_at = COALESCE(started_at, ?),
                    finished_at = NULL, cancel_requested = 0, last_error = NULL, attempt_count = attempt_count + 1
                WHERE id = ?
                """,
                (now, row["id"]),
            )
        return self.get_task(int(row["id"]))

    def set_task_stage(self, task_id: int, stage: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE tasks SET current_stage = ? WHERE id = ?", (stage, task_id))

    def finalize_task(self, task_id: int, status: str, error: str | None = None) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, last_error = ?, current_stage = ?, finished_at = ? WHERE id = ?",
                (status, error, status, utcnow(), task_id),
            )

    def request_cancel(self, task_id: int) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        now = utcnow()
        with self._lock, self.connect() as conn:
            if task["status"] == "queued":
                conn.execute(
                    "UPDATE tasks SET status = 'cancelled', current_stage = 'cancelled', finished_at = ?, last_error = 'Cancelled before execution' WHERE id = ?",
                    (now, task_id),
                )
            elif task["status"] == "running":
                conn.execute("UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,))
        return self.get_task(task_id)

    def should_cancel(self, task_id: int) -> bool:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def retry_task(self, task_id: int) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        if task["status"] not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("Task is not retryable")
        queue_position = self.next_queue_position()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE task_files
                SET download_status = CASE
                        WHEN download_status = 'completed' THEN download_status
                        ELSE 'pending'
                    END,
                    upload_status = CASE
                        WHEN upload_status = 'completed' THEN upload_status
                        ELSE 'pending'
                    END,
                    last_error = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (utcnow(), task_id),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = 'queued', queue_position = ?, cancel_requested = 0,
                    current_stage = 'queued', last_error = NULL, finished_at = NULL,
                    started_at = NULL
                WHERE id = ?
                """,
                (queue_position, task_id),
            )
        self.refresh_progress(task_id)
        return self.get_task(task_id)

    def delete_finished_task(self, task_id: int) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False
        if task["status"] not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise ValueError("Only finished tasks can be deleted")
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return True

    def mark_running_tasks_interrupted(self) -> list[int]:
        with self._lock, self.connect() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                now = utcnow()
                conn.execute(
                    "UPDATE tasks SET status = 'interrupted', current_stage = 'interrupted', finished_at = ?, last_error = 'Service restarted before task completed' WHERE status = 'running'",
                    (now,),
                )
        return ids

    def update_file_status(self, task_id: int, file_path: str, download_status: str | None = None, upload_status: str | None = None, error: str | None = None) -> None:
        now = utcnow()
        clauses = ["updated_at = ?"]
        params: list[Any] = [now]
        if download_status is not None:
            clauses.append("download_status = ?")
            params.append(download_status)
        if upload_status is not None:
            clauses.append("upload_status = ?")
            params.append(upload_status)
        clauses.append("last_error = ?")
        params.append(error)
        params.extend([task_id, file_path])
        sql = f"UPDATE task_files SET {', '.join(clauses)} WHERE task_id = ? AND file_path = ?"
        with self._lock, self.connect() as conn:
            conn.execute(sql, params)
        self.refresh_progress(task_id)

    def refresh_progress(self, task_id: int) -> None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(file_size), 0) AS total_bytes,
                    COALESCE(SUM(CASE WHEN download_status = 'completed' THEN file_size ELSE 0 END), 0) AS downloaded_bytes,
                    COALESCE(SUM(CASE WHEN upload_status = 'completed' THEN file_size ELSE 0 END), 0) AS uploaded_bytes,
                    COUNT(*) AS total_files,
                    SUM(CASE WHEN download_status = 'completed' THEN 1 ELSE 0 END) AS completed_download_files,
                    SUM(CASE WHEN upload_status = 'completed' THEN 1 ELSE 0 END) AS completed_upload_files
                FROM task_files
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE tasks
                SET total_bytes = ?, downloaded_bytes = ?, uploaded_bytes = ?, total_files = ?,
                    completed_download_files = ?, completed_upload_files = ?
                WHERE id = ?
                """,
                (
                    int(row["total_bytes"] or 0),
                    int(row["downloaded_bytes"] or 0),
                    int(row["uploaded_bytes"] or 0),
                    int(row["total_files"] or 0),
                    int(row["completed_download_files"] or 0),
                    int(row["completed_upload_files"] or 0),
                    task_id,
                ),
            )

    def list_pending_files(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT file_path, file_size, download_status, upload_status, last_error FROM task_files WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reorder_queued_tasks(self, task_ids: list[int]) -> list[dict[str, Any]]:
        with self._lock, self.connect() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY queue_position ASC, id ASC").fetchall()
            queued_ids = [int(row["id"]) for row in rows]
            requested = [task_id for task_id in task_ids if task_id in queued_ids]
            remaining = [task_id for task_id in queued_ids if task_id not in requested]
            final_order = requested + remaining
            for index, task_id in enumerate(final_order, start=1):
                conn.execute("UPDATE tasks SET queue_position = ? WHERE id = ?", (index, task_id))
        return self.list_tasks()

    def move_queued_task(self, task_id: int, direction: str) -> None:
        queued = [task for task in self.list_tasks() if task["status"] == "queued"]
        ids = [task["id"] for task in queued]
        if task_id not in ids:
            raise ValueError("Task is not queued")
        current_index = ids.index(task_id)
        if direction == "up" and current_index > 0:
            ids[current_index - 1], ids[current_index] = ids[current_index], ids[current_index - 1]
        elif direction == "down" and current_index < len(ids) - 1:
            ids[current_index + 1], ids[current_index] = ids[current_index], ids[current_index + 1]
        self.reorder_queued_tasks(ids)
