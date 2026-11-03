"""The installed immich-archive command, launched twice against moto."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from moto.core.base_backend import BackendDict
from moto.server import ThreadedMotoServer

from immich_archive.decide import dump_key, manifest_key, object_key
from tests.support import BUCKET, aws_client, make_config, seed_tree

ARCHIVE = Path(sys.executable).with_name("immich-archive")


@pytest.fixture
def endpoint():
    BackendDict.reset()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    _host, port = server.get_host_and_port()
    url = f"http://127.0.0.1:{port}"
    try:
        yield url
    finally:
        server.stop()
        BackendDict.reset()


def _env(config, endpoint: str, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        env.pop(name, None)
    env.update(
        {
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ACCESS_KEY_ID": config.access_key,
            "AWS_SECRET_ACCESS_KEY": config.secret_key,
            "AWS_REGION": config.region,
            "AWS_ENDPOINT_URL": endpoint,
            "S3_BUCKET": config.bucket,
            "S3_PREFIX": config.prefix,
            "DATA_DIR": str(config.data_dir),
            "STATE_DIR": str(config.state_dir),
            "TZ": "UTC",
            "CRON": extra.get("CRON", "0 0 1 1 *"),
            "CONCURRENCY": "2",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(extra)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    if not ARCHIVE.exists():
        pytest.fail(f"{ARCHIVE} is missing. Install the package with pip install -e .")
    return subprocess.run(
        [str(ARCHIVE), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_one_shot_command_skips_originals_on_the_second_run(tmp_path, endpoint) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    raw = aws_client(endpoint)
    raw.create_bucket(Bucket=BUCKET)
    env = _env(config, endpoint)
    first = _run(["run"], env)
    assert first.returncode == 0, first.stderr
    assert "uploaded=3" in first.stdout
    assert "dump_copied=yes" in first.stdout
    library_key = object_key("immich", "library/user/IMG.jpg")
    upload_key = object_key("immich", "upload/user/VID.mp4")
    profile_key = object_key("immich", "profile/face.jpg")
    originals = {
        key: raw.head_object(Bucket=BUCKET, Key=key)["LastModified"]
        for key in (library_key, upload_key, profile_key)
    }
    assert raw.head_object(Bucket=BUCKET, Key=library_key)["StorageClass"] == "DEEP_ARCHIVE"
    dump_modified = raw.head_object(Bucket=BUCKET, Key=dump_key("immich"))["LastModified"]
    manifest = raw.get_object(Bucket=BUCKET, Key=manifest_key("immich"))["Body"].read()
    dump = raw.get_object(Bucket=BUCKET, Key=dump_key("immich"))["Body"].read()
    # LastModified on moto is second-resolution. Wait so a second PUT would move it.
    time.sleep(1.1)

    second = _run(["run"], env)
    assert second.returncode == 0, second.stderr
    assert "uploaded=0" in second.stdout
    assert "skipped=3" in second.stdout
    assert "dump_copied=no" in second.stdout
    assert raw.get_object(Bucket=BUCKET, Key=manifest_key("immich"))["Body"].read() == manifest
    assert raw.get_object(Bucket=BUCKET, Key=dump_key("immich"))["Body"].read() == dump
    for key, modified in originals.items():
        assert raw.head_object(Bucket=BUCKET, Key=key)["LastModified"] == modified
    assert raw.head_object(Bucket=BUCKET, Key=dump_key("immich"))["LastModified"] == dump_modified


def test_long_running_process_backs_up_once_at_startup(tmp_path, endpoint) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    raw = aws_client(endpoint)
    raw.create_bucket(Bucket=BUCKET)
    env = _env(config, endpoint, CRON="0 0 1 1 *", ADMIN_PASSWORD="vault-password", HOST="127.0.0.1", PORT="0")
    if not ARCHIVE.exists():
        pytest.fail(f"{ARCHIVE} is missing. Install the package with pip install -e .")
    process = subprocess.Popen(
        [str(ARCHIVE), "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    try:
        output = _read_until(process, "scanned=", timeout=30)
        assert "listening on " in output
        assert output.count("scanned=") == 1
        extra = _read_available(process, timeout=2)
        combined = output + extra
        assert combined.count("scanned=") == 1
        assert "uploaded=3" in combined
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _read_until(process: subprocess.Popen[bytes], needle: str, timeout: float) -> str:
    """Read stdout until `needle` appears. Do not block past `timeout`."""
    collected = _collect(process, timeout, needle)
    if needle not in collected:
        raise AssertionError(f"did not see {needle!r} in {collected!r}; returncode={process.poll()}")
    return collected


def _read_available(process: subprocess.Popen[bytes], timeout: float) -> str:
    """Return output that arrives within `timeout`. A quiet process yields ''."""
    return _collect(process, timeout, None)


def _collect(process: subprocess.Popen[bytes], timeout: float, needle: str | None) -> str:
    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        ready, _unused, _errors = select.select([fd], [], [], min(0.2, remaining))
        if not ready:
            if process.poll() is not None:
                break
            continue
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if not data:
            break
        chunks.append(data.decode())
        if needle is not None and needle in "".join(chunks):
            return "".join(chunks)
    return "".join(chunks)
