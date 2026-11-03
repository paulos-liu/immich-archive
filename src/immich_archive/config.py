from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from croniter import croniter


@dataclass
class Config:
    access_key: str
    secret_key: str
    region: str
    bucket: str
    prefix: str = "immich"
    data_dir: Path = Path("/data")
    library_dir: Path | None = None
    upload_dir: Path | None = None
    profile_dir: Path | None = None
    backup_dir: Path | None = None
    state_dir: Path = Path("/state")
    tz: str = "UTC"
    cron: str = "0 3 * * *"
    concurrency: int = 2
    multipart_threshold: int = 104_857_600
    admin_password: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    endpoint_url: str | None = None

    def roots(self) -> list[tuple[str, Path]]:
        return [
            ("library", self.library_dir or self.data_dir / "library"),
            ("upload", self.upload_dir or self.data_dir / "upload"),
            ("profile", self.profile_dir or self.data_dir / "profile"),
        ]

    def backups(self) -> Path:
        return self.backup_dir or self.data_dir / "backups"

    def sqlite_path(self) -> Path:
        return self.state_dir / "state.sqlite"

    def lock_path(self) -> Path:
        return self.state_dir / "run.lock"

    def settings_path(self) -> Path:
        return self.state_dir / "config.json"

    def credentials_ready(self) -> bool:
        return bool(self.access_key and self.secret_key and self.region and self.bucket)


def _path_from(env: dict[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    if not value:
        return None
    return Path(value)


def load_config(env: dict[str, str] | None = None) -> Config:
    source = os.environ if env is None else env
    threshold = source.get("MULTIPART_THRESHOLD_BYTES", "").strip()
    concurrency = source.get("CONCURRENCY", "").strip()
    port = source.get("PORT", "").strip()
    return Config(
        access_key=source.get("AWS_ACCESS_KEY_ID", ""),
        secret_key=source.get("AWS_SECRET_ACCESS_KEY", ""),
        region=source.get("AWS_REGION", ""),
        bucket=source.get("S3_BUCKET", ""),
        prefix=source.get("S3_PREFIX", "immich"),
        data_dir=Path(source.get("DATA_DIR", "/data")),
        library_dir=_path_from(source, "LIBRARY_DIR"),
        upload_dir=_path_from(source, "UPLOAD_DIR"),
        profile_dir=_path_from(source, "PROFILE_DIR"),
        backup_dir=_path_from(source, "BACKUP_DIR"),
        state_dir=Path(source.get("STATE_DIR", "/state")),
        tz=source.get("TZ", "UTC") or "UTC",
        cron=source.get("CRON", "0 3 * * *") or "0 3 * * *",
        concurrency=int(concurrency) if concurrency else 2,
        multipart_threshold=int(threshold) if threshold else 104_857_600,
        admin_password=source.get("ADMIN_PASSWORD", ""),
        host=source.get("HOST", "0.0.0.0") or "0.0.0.0",
        port=int(port) if port else 8080,
        endpoint_url=source.get("AWS_ENDPOINT_URL", "").strip() or None,
    )


def runtime_settings(config: Config) -> tuple[str, int]:
    """Cron and concurrency, with `/state/config.json` overriding the environment."""
    cron = config.cron
    concurrency = config.concurrency
    path = config.settings_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
        saved_cron = data.get("cron")
        if isinstance(saved_cron, str) and saved_cron.strip():
            candidate = saved_cron.strip()
            try:
                croniter(candidate)
            except (ValueError, KeyError):
                candidate = ""
            if candidate:
                cron = candidate
        saved_concurrency = data.get("concurrency")
        if isinstance(saved_concurrency, int) and saved_concurrency >= 1:
            concurrency = saved_concurrency
    return cron, concurrency


def save_runtime_settings(config: Config, cron: str, concurrency: int) -> None:
    expression = cron.strip()
    if not expression or concurrency < 1:
        raise ValueError("cron and concurrency are required")
    croniter(expression)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.settings_path().write_text(
        json.dumps({"cron": expression, "concurrency": concurrency}) + "\n"
    )
