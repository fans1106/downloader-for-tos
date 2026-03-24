from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download
from huggingface_hub.errors import RepositoryNotFoundError, RevisionNotFoundError
from huggingface_hub.utils import HfHubHTTPError

from app.config import Settings
from app.db import Database
from app.security import TokenCipher


logger = logging.getLogger(__name__)

DEFAULT_IGNORE_PATTERNS = [".gitattributes", ".gitignore"]


class TaskExecutionError(Exception):
    pass


class TaskCancelled(TaskExecutionError):
    pass


class TaskLogger:
    def __init__(self, db: Database, task_id: int) -> None:
        self.db = db
        self.task_id = task_id
        self._logger = logging.getLogger(f"task.{task_id}")

    def info(self, stage: str, message: str) -> None:
        self._logger.info("[%s] %s", stage, message)
        self.db.append_log(self.task_id, "INFO", stage, message)

    def warning(self, stage: str, message: str) -> None:
        self._logger.warning("[%s] %s", stage, message)
        self.db.append_log(self.task_id, "WARNING", stage, message)

    def error(self, stage: str, message: str) -> None:
        self._logger.error("[%s] %s", stage, message)
        self.db.append_log(self.task_id, "ERROR", stage, message)


def build_tos_target(base_target: str, file_path: str) -> str:
    base = base_target.rstrip("/")
    relative = file_path.lstrip("/")
    return f"{base}/{relative}"


def chunk_files(files: list[dict[str, Any]], batch_size_limit: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current_chunk: list[dict[str, Any]] = []
    current_size = 0

    for item in files:
        file_size = int(item["file_size"])
        if current_chunk and current_size + file_size > batch_size_limit:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(item)
        current_size += file_size
        if file_size >= batch_size_limit:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0

    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def clean_empty_directories(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                continue


class TaskRunner:
    def __init__(self, db: Database, settings: Settings, cipher: TokenCipher) -> None:
        self.db = db
        self.settings = settings
        self.cipher = cipher
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="task-runner", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def wake(self) -> None:
        self._wake_event.set()

    def _run_loop(self) -> None:
        logger.info("Task runner started")
        while not self._stop_event.is_set():
            task = self.db.claim_next_task()
            if task is None:
                self._wake_event.wait(timeout=self.settings.queue_poll_interval)
                self._wake_event.clear()
                continue
            task_logger = TaskLogger(self.db, task["id"])
            try:
                self._execute_task(task, task_logger)
                self.db.finalize_task(task["id"], "succeeded")
                task_logger.info("completed", "Task completed successfully")
            except TaskCancelled as exc:
                self.db.finalize_task(task["id"], "cancelled", str(exc))
                task_logger.warning("cancelled", str(exc))
            except Exception as exc:  # noqa: BLE001
                message = str(exc) or exc.__class__.__name__
                logger.exception("Task %s failed", task["id"])
                self.db.finalize_task(task["id"], "failed", message)
                task_logger.error("failed", message)
            finally:
                self._wake_event.clear()
        logger.info("Task runner stopped")

    def _execute_task(self, task: dict[str, Any], task_logger: TaskLogger) -> None:
        task_id = task["id"]
        task_logger.info("preparing", f"Preparing task for {task['repo_type']} repo {task['repo_id']}")
        token = self.cipher.decrypt(task["hf_token_encrypted"])
        download_dir = Path(task["local_download_dir"])
        download_dir.mkdir(parents=True, exist_ok=True)

        manifest = self._load_manifest(task, token, task_logger)
        self.db.save_task_manifest(task_id, manifest)
        self.db.refresh_progress(task_id)

        if not manifest:
            task_logger.info("completed", "Repository contains no files after pattern filtering")
            return

        batch_limit = int(task["batch_size_limit_bytes"])
        batches = chunk_files(manifest, batch_limit)
        for batch_index, batch in enumerate(batches, start=1):
            self._ensure_not_cancelled(task_id)
            readable_size = sum(item["file_size"] for item in batch) / (1024 ** 3)
            task_logger.info(
                "download",
                f"Starting batch {batch_index}/{len(batches)} with {len(batch)} files ({readable_size:.2f} GB)",
            )
            self.db.set_task_stage(task_id, "download")
            for file_info in batch:
                self._ensure_not_cancelled(task_id)
                self._download_file(task, token, download_dir, file_info, task_logger)

            self.db.set_task_stage(task_id, "upload")
            for file_info in batch:
                self._ensure_not_cancelled(task_id)
                self._upload_file(task, download_dir, file_info, task_logger)

        if task["cleanup_local_files"]:
            clean_empty_directories(download_dir)
            task_logger.info("cleanup", "Removed empty task directories")

    def _load_manifest(self, task: dict[str, Any], token: str, task_logger: TaskLogger) -> list[dict[str, Any]]:
        self.db.set_task_stage(task["id"], "manifest")
        ignore_patterns = list(task["ignore_patterns"])
        for pattern in DEFAULT_IGNORE_PATTERNS:
            if pattern not in ignore_patterns:
                ignore_patterns.append(pattern)

        task_logger.info("manifest", "Fetching repository file manifest from Hugging Face")
        try:
            dry_run_items = snapshot_download(
                repo_id=task["repo_id"],
                repo_type=task["repo_type"],
                local_dir=task["local_download_dir"],
                token=token,
                revision=task.get("revision"),
                allow_patterns=task["allow_patterns"] or None,
                ignore_patterns=ignore_patterns or None,
                dry_run=True,
            )
        except RepositoryNotFoundError as exc:
            raise TaskExecutionError("Repository not found or token has no access") from exc
        except RevisionNotFoundError as exc:
            raise TaskExecutionError("Requested revision was not found") from exc
        except HfHubHTTPError as exc:
            raise self._wrap_hf_error(exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise TaskExecutionError(f"Unable to fetch repository manifest: {exc}") from exc

        manifest = [
            {
                "file_path": item.filename,
                "file_size": int(getattr(item, "file_size", 0) or 0),
            }
            for item in dry_run_items
        ]
        total_bytes = sum(entry["file_size"] for entry in manifest)
        task_logger.info(
            "manifest",
            f"Manifest loaded with {len(manifest)} files ({total_bytes / (1024 ** 3):.2f} GB)",
        )
        return manifest

    def _download_file(
        self,
        task: dict[str, Any],
        token: str,
        download_dir: Path,
        file_info: dict[str, Any],
        task_logger: TaskLogger,
    ) -> None:
        task_id = task["id"]
        file_path = file_info["file_path"]
        self.db.update_file_status(task_id, file_path, download_status="downloading", error=None)
        task_logger.info("download", f"Downloading {file_path}")

        def operation() -> None:
            ignore_patterns = list(task["ignore_patterns"])
            for pattern in DEFAULT_IGNORE_PATTERNS:
                if pattern not in ignore_patterns:
                    ignore_patterns.append(pattern)
            snapshot_download(
                repo_id=task["repo_id"],
                repo_type=task["repo_type"],
                local_dir=str(download_dir),
                token=token,
                revision=task.get("revision"),
                allow_patterns=[file_path],
                ignore_patterns=ignore_patterns or None,
            )

        try:
            self._run_with_retries(task, "download", file_path, operation, task_logger)
        except Exception as exc:
            self.db.update_file_status(task_id, file_path, download_status="failed", error=str(exc))
            raise
        self.db.update_file_status(task_id, file_path, download_status="completed", error=None)
        task_logger.info("download", f"Downloaded {file_path}")

    def _upload_file(
        self,
        task: dict[str, Any],
        download_dir: Path,
        file_info: dict[str, Any],
        task_logger: TaskLogger,
    ) -> None:
        task_id = task["id"]
        file_path = file_info["file_path"]
        local_file = download_dir / file_path
        if not local_file.exists():
            raise TaskExecutionError(f"Downloaded file missing before upload: {file_path}")

        self.db.update_file_status(task_id, file_path, upload_status="uploading", error=None)
        tos_target = build_tos_target(task["tos_target"], file_path)
        task_logger.info("upload", f"Uploading {file_path} to {tos_target}")

        def operation() -> None:
            cmd = [
                self.settings.tosutil_path,
                "cp",
                "-u",
                "-flat",
                str(local_file),
                tos_target,
                "-conf",
                self.settings.tosutil_config,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                stderr = (result.stderr or result.stdout or "tosutil upload failed").strip()
                raise TaskExecutionError(f"tosutil upload failed ({result.returncode}): {stderr[:400]}")

        try:
            self._run_with_retries(task, "upload", file_path, operation, task_logger)
        except Exception as exc:
            self.db.update_file_status(task_id, file_path, upload_status="failed", error=str(exc))
            raise
        self.db.update_file_status(task_id, file_path, upload_status="completed", error=None)
        task_logger.info("upload", f"Uploaded {file_path}")

        if task["cleanup_local_files"]:
            try:
                local_file.unlink(missing_ok=True)
            except OSError as exc:
                task_logger.warning("cleanup", f"Unable to remove local file {file_path}: {exc}")

    def _run_with_retries(
        self,
        task: dict[str, Any],
        stage: str,
        file_path: str,
        operation: Any,
        task_logger: TaskLogger,
    ) -> None:
        retries = int(task["max_retries"])
        for attempt in range(retries + 1):
            try:
                operation()
                return
            except TaskExecutionError as exc:
                if attempt >= retries:
                    raise
                task_logger.warning(stage, f"Retrying {file_path} after task error: {exc}")
                time.sleep(min(2 ** attempt, 10))
            except RepositoryNotFoundError as exc:
                raise TaskExecutionError("Repository not found or token has no access") from exc
            except RevisionNotFoundError as exc:
                raise TaskExecutionError("Requested revision was not found") from exc
            except HfHubHTTPError as exc:
                wrapped = self._wrap_hf_error(exc)
                if self._is_non_retryable_hf_error(exc):
                    raise wrapped from exc
                if attempt >= retries:
                    raise wrapped from exc
                task_logger.warning(stage, f"Retrying {file_path} after Hugging Face error: {wrapped}")
                time.sleep(min(2 ** attempt, 10))
            except subprocess.SubprocessError as exc:
                if attempt >= retries:
                    raise TaskExecutionError(f"Subprocess failed for {file_path}: {exc}") from exc
                task_logger.warning(stage, f"Retrying {file_path} after subprocess error: {exc}")
                time.sleep(min(2 ** attempt, 10))
            except Exception as exc:  # noqa: BLE001
                if attempt >= retries:
                    raise TaskExecutionError(f"{stage} failed for {file_path}: {exc}") from exc
                task_logger.warning(stage, f"Retrying {file_path} after error: {exc}")
                time.sleep(min(2 ** attempt, 10))

    def _ensure_not_cancelled(self, task_id: int) -> None:
        if self.db.should_cancel(task_id):
            raise TaskCancelled("Task cancelled by user")

    def _is_non_retryable_hf_error(self, exc: HfHubHTTPError) -> bool:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return status_code in {401, 403, 404}

    def _wrap_hf_error(self, exc: HfHubHTTPError) -> TaskExecutionError:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {401, 403}:
            return TaskExecutionError("Hugging Face token is invalid or lacks permission")
        if status_code == 404:
            return TaskExecutionError("Repository, file, or revision was not found")
        return TaskExecutionError(f"Hugging Face API error: {exc}")


class RuntimeInspector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def inspect(self) -> dict[str, bool]:
        return {
            "tosutil_available": shutil.which(self.settings.tosutil_path) is not None,
            "tosutil_config_exists": Path(self.settings.tosutil_config).exists(),
        }
