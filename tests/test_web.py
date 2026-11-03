"""Password-gated dashboard served by the shipped process."""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from immich_archive.decide import next_cron_time
from tests.support import BUCKET, aws_client, make_config, seed_tree
from tests.test_cli import ARCHIVE, _env, _read_available, _read_until, endpoint

SECRET = "SECRET-SENTINEL-do-not-leak"
LIBRARY_KEY = "immich/originals/library/user/IMG.jpg"


def _request(host: str, port: int, method: str, path: str, *, cookie: str = "", body: str = "") -> tuple[int, str, str]:
    connection = http.client.HTTPConnection(host, port, timeout=30)
    headers = {}
    if cookie:
        headers["Cookie"] = f"session={cookie}"
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body.encode()))
    connection.request(method, path, body=body if body else None, headers=headers)
    response = connection.getresponse()
    payload = response.read().decode()
    header_blob = "\n".join(f"{name}: {value}" for name, value in response.getheaders())
    status = response.status
    connection.close()
    return status, payload, header_blob


def _cookie(header_blob: str) -> str:
    match = re.search(r"session=([^;]+)", header_blob)
    assert match, header_blob
    assert "HttpOnly" in header_blob
    assert "SameSite=Lax" in header_blob
    return match.group(1)


def _fields(body: str) -> dict[str, str]:
    return dict(re.findall(r'data-field="([^"]+)">([^<]*)', body))


def _start(env: dict[str, str]) -> tuple[subprocess.Popen[str], str, int]:
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
        output = _read_until(process, "listening on ", timeout=30)
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise
    match = re.search(r"listening on (\S+):(\d+)", output)
    assert match, output
    host, port = match.group(1), int(match.group(2))
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return process, host, port


def _stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_startup(process: subprocess.Popen[str]) -> str:
    return _read_until(process, "scanned=", timeout=30)


def test_dashboard_hides_the_archive_until_a_password_is_supplied(tmp_path, endpoint) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    raw = aws_client(endpoint)
    raw.create_bucket(Bucket=BUCKET)
    seeded = subprocess.run(
        [str(ARCHIVE), "run"],
        env=_env(config, endpoint),
        text=True,
        capture_output=True,
        check=False,
    )
    assert seeded.returncode == 0, seeded.stderr

    closed = make_config(tmp_path / "closed", admin_password="")
    # Reuse the seeded library and checkpoint so a startup run has the same files.
    closed.data_dir = config.data_dir
    closed.state_dir = config.state_dir
    closed_env = _env(closed, endpoint, ADMIN_PASSWORD="", HOST="127.0.0.1", PORT="0", CRON="0 0 1 1 *")
    closed_env["ADMIN_PASSWORD"] = ""
    process, host, port = _start(closed_env)
    try:
        _wait_startup(process)
        status, body, _headers = _request(host, port, "GET", "/")
        assert status == 503
        assert "Admin password is not configured" in body
        assert "The dashboard is off until ADMIN_PASSWORD is set." in body
        assert LIBRARY_KEY not in body
        assert "data-field" not in body
        assert SECRET not in body
        assert "scanned=" not in body
    finally:
        _stop(process)

    env = _env(
        config,
        endpoint,
        ADMIN_PASSWORD="vault-password",
        HOST="127.0.0.1",
        PORT="0",
        CRON="0 0 1 1 *",
    )
    first, host, port = _start(env)
    try:
        _wait_startup(first)
        status, body, _headers = _request(host, port, "GET", "/")
        assert status == 401
        assert "Sign in" in body
        assert LIBRARY_KEY not in body
        assert "data-field" not in body
        assert SECRET not in body
        status, _body, _headers = _request(
            host,
            port,
            "POST",
            "/login",
            body=urlencode({"password": "wrong"}),
        )
        assert status == 401

        status, _body, headers = _request(
            host,
            port,
            "POST",
            "/login",
            body=urlencode({"password": "vault-password"}),
        )
        assert status == 303
        cookie = _cookie(headers)
        status, overview, _headers = _request(host, port, "GET", "/", cookie=cookie)
        assert status == 200
        assert SECRET not in overview
        fields = _fields(overview)
        for name in ("scanned", "uploaded", "skipped", "failed", "archive-files", "archive-bytes", "next-run", "dump-name"):
            assert fields[name], overview
        assert fields["uploaded"] == "0"
        assert int(fields["archive-files"]) >= 1
        assert fields["dump-name"] == "immich-db-backup-2026.sql.gz"
        assert "hidden" in overview
        assert "Run now" in overview
        assert "Dry run" in overview
        assert "Save schedule" in overview

        status, files, _headers = _request(host, port, "GET", "/files?q=IMG.jpg", cookie=cookie)
        assert status == 200
        assert LIBRARY_KEY in files
        assert "library/user/IMG.jpg" in files

        status, saved, _headers = _request(
            host,
            port,
            "POST",
            "/settings",
            cookie=cookie,
            body=urlencode({"cron": "0 0 2 1 *", "concurrency": "3"}),
        )
        assert status == 303
        status, overview, _headers = _request(host, port, "GET", "/", cookie=cookie)
        shown = _fields(overview)["next-run"]
        expected = next_cron_time("0 0 2 1 *", datetime.now(ZoneInfo("UTC")), "UTC")
        assert shown == expected.isoformat()
        counts = {name: _fields(overview)[name] for name in ("scanned", "uploaded", "skipped", "failed", "archive-files", "archive-bytes")}
    finally:
        _stop(first)

    second, host, port = _start(env)
    try:
        _wait_startup(second)
        _extra = _read_available(second, timeout=0.2)
        status, _body, headers = _request(
            host,
            port,
            "POST",
            "/login",
            body=urlencode({"password": "vault-password"}),
        )
        cookie = _cookie(headers)
        status, overview, _headers = _request(host, port, "GET", "/", cookie=cookie)
        again = {name: _fields(overview)[name] for name in counts}
        assert again == counts
        assert SECRET not in overview
    finally:
        _stop(second)


def test_run_now_is_refused_while_the_lock_is_held(tmp_path, endpoint) -> None:
    import fcntl

    config = make_config(tmp_path)
    seed_tree(config)
    raw = aws_client(endpoint)
    raw.create_bucket(Bucket=BUCKET)
    env = _env(config, endpoint, ADMIN_PASSWORD="vault-password", HOST="127.0.0.1", PORT="0", CRON="0 0 1 1 *")
    process, host, port = _start(env)
    lock_handle = None
    try:
        _wait_startup(process)
        status, _body, headers = _request(
            host,
            port,
            "POST",
            "/login",
            body=urlencode({"password": "vault-password"}),
        )
        cookie = _cookie(headers)
        before = raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/")
        lock_handle = open(config.lock_path(), "a+")
        acquired = False
        for _attempt in range(50):
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.05)
        assert acquired, "startup run never released the lock"
        status, body, _headers = _request(host, port, "POST", "/run", cookie=cookie)
        assert status == 409
        assert "A backup is already running." in body
        after = raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/")
        assert before.get("Contents") == after.get("Contents")
    finally:
        if lock_handle is not None:
            lock_handle.close()
        _stop(process)


def test_pages_do_not_walk_the_library(tmp_path, monkeypatch) -> None:
    import os

    from immich_archive.checkpoint import Checkpoint
    from immich_archive.web import make_server

    config = make_config(tmp_path, admin_password="vault-password", host="127.0.0.1", port=0)
    checkpoint = Checkpoint.open(config.sqlite_path())
    checkpoint.upsert_file(
        relative_path="library/user/IMG.jpg",
        size=4,
        mtime_ns=1,
        sha256="abc",
        s3_key=LIBRARY_KEY,
        uploaded_at="2026-11-02T03:00:00Z",
    )
    checkpoint.add_run(
        started_at="2026-11-02T03:00:00Z",
        finished_at="2026-11-02T03:00:01Z",
        status="ok",
        scanned=1,
        uploaded=1,
        skipped=0,
        failed=0,
        dump_copied=True,
        duration_seconds=1,
        summary="scanned=1 uploaded=1 skipped=0 failed=0 dump_copied=yes duration=1.00s",
        failures=[],
    )
    checkpoint.close()
    real_walk = os.walk

    def walk(top, *args, **kwargs):
        if Path(top) == config.data_dir or config.data_dir in Path(top).parents:
            raise AssertionError(f"dashboard walked {top}")
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    server = make_server(config)
    thread_host, thread_port = server.server_address[:2]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _body, headers = _request(
            thread_host,
            thread_port,
            "POST",
            "/login",
            body=urlencode({"password": "vault-password"}),
        )
        cookie = _cookie(headers)
        status, overview, _headers = _request(thread_host, thread_port, "GET", "/", cookie=cookie)
        assert status == 200
        assert _fields(overview)["archive-files"] == "1"
        status, files, _headers = _request(thread_host, thread_port, "GET", "/files?q=IMG", cookie=cookie)
        assert LIBRARY_KEY in files
    finally:
        server.shutdown()
        server.server_close()
