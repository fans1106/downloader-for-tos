from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


RepoType = Literal["model", "dataset"]
TaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]

TOS_TARGET_RE = re.compile(r"^tos://(?P<bucket>[A-Za-z0-9._-]+)(?:/(?P<prefix>.*))?$")


def normalize_patterns(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = value.replace(",", "\n").splitlines()
    return [item.strip() for item in raw_items if item and item.strip()]


class TaskCreate(BaseModel):
    repo_id: str = Field(min_length=3, max_length=255)
    repo_type: RepoType
    hf_token: str = Field(min_length=1, max_length=4096)
    tos_target: str = Field(min_length=6, max_length=1024)
    revision: str | None = Field(default=None, max_length=255)
    allow_patterns: list[str] = Field(default_factory=list)
    ignore_patterns: list[str] = Field(default_factory=list)
    batch_size_limit_gb: int = Field(default=800, ge=1, le=10240)
    max_retries: int = Field(default=3, ge=0, le=10)
    cleanup_local_files: bool = True

    @field_validator("repo_id")
    @classmethod
    def strip_repo_id(cls, value: str) -> str:
        cleaned = value.strip()
        if "/" not in cleaned:
            raise ValueError("repo_id must be in the form owner/name")
        return cleaned

    @field_validator("tos_target")
    @classmethod
    def validate_tos_target(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not TOS_TARGET_RE.match(cleaned):
            raise ValueError("tos_target must look like tos://bucket/prefix")
        return cleaned

    @field_validator("revision")
    @classmethod
    def normalize_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("allow_patterns", "ignore_patterns", mode="before")
    @classmethod
    def parse_patterns(cls, value: list[str] | str | None) -> list[str]:
        return normalize_patterns(value)


class TaskSummary(BaseModel):
    id: int
    repo_id: str
    repo_type: RepoType
    tos_target: str
    revision: str | None
    status: TaskStatus
    queue_position: int
    attempt_count: int
    cancel_requested: bool
    current_stage: str
    total_bytes: int
    downloaded_bytes: int
    uploaded_bytes: int
    total_files: int
    completed_download_files: int
    completed_upload_files: int
    last_error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    cleanup_local_files: bool


class TaskDetail(TaskSummary):
    allow_patterns: list[str]
    ignore_patterns: list[str]
    batch_size_limit_bytes: int
    max_retries: int
    local_download_dir: str | None
    hf_token_masked: str


class TaskLogEvent(BaseModel):
    id: int
    task_id: int
    created_at: str
    level: str
    stage: str
    message: str


class TaskLogsResponse(BaseModel):
    task_id: int
    items: list[TaskLogEvent]


class HealthResponse(BaseModel):
    status: str
    tosutil_available: bool
    tosutil_config_exists: bool
    worker_alive: bool


class ReorderRequest(BaseModel):
    task_ids: list[int] = Field(min_length=1)
