"""Fixtures and a recording S3 client for tests. Nothing here talks to AWS."""

from __future__ import annotations

import os
import time
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from moto import mock_aws

from immich_archive.config import Config

OLD_NS = 120 * 1_000_000_000
BUCKET = "immich-archive-test"


def write_bytes(path: Path, data: bytes, *, age_ns: int = OLD_NS) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time_ns() - age_ns
    os.utime(path, ns=(stamp, stamp))
    return stamp


def make_config(tmp_path: Path, **overrides) -> Config:
    data = tmp_path / "data"
    for name in ("library", "upload", "profile", "backups", "thumbs", "encoded-video"):
        (data / name).mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config = Config(
        access_key="AKIATESTKEY",
        secret_key="SECRET-SENTINEL-do-not-leak",
        region="us-east-1",
        bucket=BUCKET,
        prefix="immich",
        data_dir=data,
        state_dir=state,
        tz="UTC",
        cron="0 3 * * *",
        concurrency=2,
        multipart_threshold=104_857_600,
        admin_password="vault-password",
        host="127.0.0.1",
        port=0,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def seed_tree(config: Config) -> dict[str, bytes]:
    """A small library, plus files the walk must ignore."""
    files = {
        "library": b"library-original",
        "upload": b"upload-original",
        "profile": b"profile-original",
    }
    write_bytes(config.data_dir / "library" / "user" / "IMG.jpg", files["library"])
    write_bytes(config.data_dir / "upload" / "user" / "VID.mp4", files["upload"])
    write_bytes(config.data_dir / "profile" / "face.jpg", files["profile"])
    write_bytes(config.data_dir / "thumbs" / "IMG.jpg", b"thumb-should-stay-local")
    write_bytes(config.data_dir / "encoded-video" / "VID.mp4", b"encoded-should-stay-local")
    write_bytes(config.backups() / "notes.jpg", b"not-a-dump")
    write_bytes(config.backups() / "immich-db-backup-2026.sql.gz", b"sql-dump-v1")
    return files


class RecordingS3:
    """Forwards to moto and fails the test if the backup tries to delete an object."""

    def __init__(self, client, *, fail_put_keys: set[str] | None = None, fail_parts: bool = False):
        self._client = client
        self.events: list[tuple[str, dict]] = []
        self.fail_put_keys = fail_put_keys or set()
        self.fail_parts = fail_parts

    def calls(self, name: str, key: str | None = None) -> list[dict]:
        found = []
        for event, kwargs in self.events:
            if event != name:
                continue
            if key is not None and kwargs.get("Key") != key:
                continue
            found.append(kwargs)
        return found

    def put_object(self, **kwargs):
        self.events.append(("put_object", dict(kwargs)))
        if kwargs.get("Key") in self.fail_put_keys:
            raise ClientError(
                {
                    "Error": {"Code": "InternalError", "Message": "put failed"},
                    "ResponseMetadata": {"HTTPStatusCode": 500},
                },
                "PutObject",
            )
        return self._client.put_object(**kwargs)

    def upload_part(self, **kwargs):
        self.events.append(("upload_part", dict(kwargs)))
        if self.fail_parts:
            raise ClientError(
                {
                    "Error": {"Code": "InternalError", "Message": "part failed"},
                    "ResponseMetadata": {"HTTPStatusCode": 500},
                },
                "UploadPart",
            )
        return self._client.upload_part(**kwargs)

    def delete_object(self, **kwargs):
        raise AssertionError("delete_object was called")

    def delete_objects(self, **kwargs):
        raise AssertionError("delete_objects was called")

    def __getattr__(self, name):
        if name.startswith("delete"):

            def banned(*_args, **_kwargs):
                raise AssertionError(f"{name} was called")

            return banned
        target = getattr(self._client, name)
        if not callable(target):
            return target

        def wrapped(*_args, **kwargs):
            self.events.append((name, dict(kwargs)))
            return target(**kwargs)

        return wrapped


def aws_client(endpoint_url: str | None = None):
    kwargs = {
        "region_name": "us-east-1",
        "aws_access_key_id": "AKIATESTKEY",
        "aws_secret_access_key": "SECRET-SENTINEL-do-not-leak",
        "config": BotoConfig(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "path"},
        ),
    }
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("s3", **kwargs)


class MotoBucket:
    def __init__(self):
        self._mock = mock_aws()

    def __enter__(self):
        self._mock.__enter__()
        self.raw = aws_client()
        self.raw.create_bucket(Bucket=BUCKET)
        self.s3 = RecordingS3(self.raw)
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._mock.__exit__(exc_type, exc, tb)
