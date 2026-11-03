"""Environment defaults and the saved schedule."""

from __future__ import annotations

from pathlib import Path

import pytest

from immich_archive.config import load_config, runtime_settings, save_runtime_settings


def test_defaults_and_directory_overrides() -> None:
    config = load_config({})
    assert config.prefix == "immich"
    assert config.data_dir == Path("/data")
    assert config.state_dir == Path("/state")
    assert config.tz == "UTC"
    assert config.cron == "0 3 * * *"
    assert config.concurrency == 2
    assert config.multipart_threshold == 104_857_600
    assert config.host == "0.0.0.0"
    assert config.port == 8080
    assert config.endpoint_url is None
    assert config.roots() == [
        ("library", Path("/data/library")),
        ("upload", Path("/data/upload")),
        ("profile", Path("/data/profile")),
    ]
    assert config.backups() == Path("/data/backups")
    assert config.sqlite_path() == Path("/state/state.sqlite")
    assert not config.credentials_ready()

    overridden = load_config(
        {
            "AWS_ACCESS_KEY_ID": "ak",
            "AWS_SECRET_ACCESS_KEY": "sk",
            "AWS_REGION": "us-west-2",
            "S3_BUCKET": "bkt",
            "S3_PREFIX": "custom",
            "DATA_DIR": "/mnt/immich",
            "LIBRARY_DIR": "/libs",
            "UPLOAD_DIR": "/ups",
            "PROFILE_DIR": "/pros",
            "BACKUP_DIR": "/baks",
            "STATE_DIR": "/var/state",
            "TZ": "America/Los_Angeles",
            "CRON": "15 4 * * *",
            "CONCURRENCY": "4",
            "MULTIPART_THRESHOLD_BYTES": "32",
            "ADMIN_PASSWORD": "pw",
            "HOST": "127.0.0.1",
            "PORT": "9090",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
        }
    )
    assert overridden.credentials_ready()
    assert overridden.prefix == "custom"
    assert overridden.tz == "America/Los_Angeles"
    assert overridden.cron == "15 4 * * *"
    assert overridden.concurrency == 4
    assert overridden.multipart_threshold == 32
    assert overridden.port == 9090
    assert overridden.endpoint_url == "http://127.0.0.1:9"
    assert overridden.roots() == [
        ("library", Path("/libs")),
        ("upload", Path("/ups")),
        ("profile", Path("/pros")),
    ]
    assert overridden.backups() == Path("/baks")
    assert overridden.sqlite_path() == Path("/var/state/state.sqlite")


def test_saved_schedule_overrides_the_environment(tmp_path) -> None:
    config = load_config({"STATE_DIR": str(tmp_path), "CRON": "0 3 * * *", "CONCURRENCY": "2"})
    assert runtime_settings(config) == ("0 3 * * *", 2)
    save_runtime_settings(config, "0 4 * * *", 5)
    assert runtime_settings(config) == ("0 4 * * *", 5)
    with pytest.raises(ValueError):
        save_runtime_settings(config, "not a cron", 1)
    with pytest.raises(ValueError):
        save_runtime_settings(config, "0 4 * * *", 0)
    assert runtime_settings(config) == ("0 4 * * *", 5)
