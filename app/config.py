from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    app_name: str = "HF Downloader"
    base_dir: Path = Path.cwd()
    data_dir: Path = Path.cwd() / "data"
    db_path: Path = Path.cwd() / "data" / "hf_downloader.db"
    log_dir: Path = Path.cwd() / "logs"
    download_root: Path = Path.cwd() / "downloads"
    tosutil_path: str = os.getenv("HF_DOWNLOADER_TOSUTIL_PATH", "tosutil")
    tosutil_config: str = os.getenv(
        "HF_DOWNLOADER_TOSUTIL_CONFIG",
        "/home/downloader/HongbangFan/.tosutilconfig",
    )
    app_secret: str = os.getenv("HF_DOWNLOADER_APP_SECRET", "change-me-in-production")
    auth_password: str = os.getenv(
        "HF_DOWNLOADER_AUTH_PASSWORD",
        os.getenv("HF_DOWNLOADER_APP_SECRET", "change-me-in-production"),
    )
    auth_cookie_name: str = "hf_downloader_auth"
    host: str = os.getenv("HF_DOWNLOADER_HOST", "0.0.0.0")
    port: int = int(os.getenv("HF_DOWNLOADER_PORT", "8000"))
    queue_poll_interval: float = float(os.getenv("HF_DOWNLOADER_QUEUE_POLL", "2.0"))
    log_level: str = os.getenv("HF_DOWNLOADER_LOG_LEVEL", "INFO")
    default_batch_size_gb: int = int(os.getenv("HF_DOWNLOADER_DEFAULT_BATCH_SIZE_GB", "800"))
    default_max_retries: int = int(os.getenv("HF_DOWNLOADER_DEFAULT_MAX_RETRIES", "3"))

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.download_root.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_directories()
