"""One backup run against moto. These tests call the shipped run, not a reimplementation."""

from __future__ import annotations

import csv
import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from immich_archive.checkpoint import Checkpoint, probe
from immich_archive.locks import acquire_run_lock
from immich_archive.runner import run_backup
from moto import mock_aws
from tests.support import BUCKET, MotoBucket, aws_client, make_config, seed_tree, write_bytes

LIBRARY_KEY = "immich/originals/library/user/IMG.jpg"
UPLOAD_KEY = "immich/originals/upload/user/VID.mp4"
PROFILE_KEY = "immich/originals/profile/face.jpg"
DUMP_KEY = "immich/db/immich-latest.sql.gz"
MANIFEST_KEY = "immich/manifest.csv"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(client) -> str:
    return client.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read().decode()


def _rows(client) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(_manifest(client))))


class _Explode:
    def __getattr__(self, name):
        def banned(*_args, **_kwargs):
            raise AssertionError(f"S3 call {name} is not allowed")

        return banned


def test_run_stores_originals_dump_and_quoted_manifest(tmp_path, capsys) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    write_bytes(config.data_dir / "library" / 'we, "ird".jpg', b"comma-name")
    with MotoBucket() as bucket:
        result = run_backup(config, client=bucket.s3)

        assert result.exit_code == 0
        assert result.dump_copied is True
        assert result.uploaded == 4
        assert result.skipped == 0
        assert result.failed == 0
        assert result.scanned == result.uploaded + result.skipped + result.failed
        output = capsys.readouterr().out
        assert result.summary in output
        assert "dump_copied=yes" in output
        assert "duration=" in output

        expected = {
            LIBRARY_KEY: b"library-original",
            UPLOAD_KEY: b"upload-original",
            PROFILE_KEY: b"profile-original",
            'immich/originals/library/we, "ird".jpg': b"comma-name",
        }
        for key, body in expected.items():
            puts = bucket.s3.calls("put_object", key)
            assert len(puts) == 1
            assert puts[0]["StorageClass"] == "DEEP_ARCHIVE"
            assert puts[0]["Metadata"]["sha256"] == _sha(body)
            head = bucket.raw.head_object(Bucket=BUCKET, Key=key)
            assert head["StorageClass"] == "DEEP_ARCHIVE"
            assert head["Metadata"]["sha256"] == _sha(body)

        listed = [item["Key"] for item in bucket.raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/")["Contents"]]
        assert not any("thumbs" in key or "encoded-video" in key or key.endswith("notes.jpg") for key in listed)
        assert bucket.s3.calls("create_multipart_upload") == []
        assert bucket.s3.calls("get_object") == []
        assert not any(name.startswith("delete") for name, _kwargs in bucket.s3.events)

        dump_put = bucket.s3.calls("put_object", DUMP_KEY)[0]
        assert dump_put["StorageClass"] == "STANDARD"
        assert dump_put["Metadata"]["source-name"] == "immich-db-backup-2026.sql.gz"
        assert dump_put["Metadata"].get("sha256") in (None, "")
        assert bucket.raw.get_object(Bucket=BUCKET, Key=DUMP_KEY)["Body"].read() == b"sql-dump-v1"
        manifest_put = bucket.s3.calls("put_object", MANIFEST_KEY)[0]
        assert manifest_put["StorageClass"] == "STANDARD"
        paths = [row["relative_path"] for row in _rows(bucket.raw)]
        assert paths == sorted(paths)
        assert paths == [
            "library/user/IMG.jpg",
            'library/we, "ird".jpg',
            "profile/face.jpg",
            "upload/user/VID.mp4",
        ]
        comma = next(row for row in _rows(bucket.raw) if row["relative_path"].startswith("library/we"))
        assert comma["s3_key"] == 'immich/originals/library/we, "ird".jpg'
        assert comma["sha256"] == _sha(b"comma-name")
        assert comma["size"] == str(len(b"comma-name"))


def test_second_run_skips_unchanged_files_without_hashing_or_putting(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    with MotoBucket() as bucket:
        first = run_backup(config, client=bucket.s3)
        assert first.exit_code == 0
        first_manifest = _manifest(bucket.raw)
        first_dump = bucket.raw.get_object(Bucket=BUCKET, Key=DUMP_KEY)["Body"].read()
        bucket.s3.events.clear()

        def boom(path):
            raise AssertionError(f"hashed {path}")

        monkeypatch.setattr("immich_archive.runner.sha256_file", boom)
        second = run_backup(config, client=bucket.s3)

        assert second.exit_code == 0
        assert second.uploaded == 0
        assert second.skipped == first.scanned
        assert second.dump_copied is False
        assert _manifest(bucket.raw) == first_manifest
        assert bucket.raw.get_object(Bucket=BUCKET, Key=DUMP_KEY)["Body"].read() == first_dump
        assert bucket.s3.calls("put_object", LIBRARY_KEY) == []
        assert bucket.s3.calls("put_object", UPLOAD_KEY) == []
        assert bucket.s3.calls("put_object", PROFILE_KEY) == []
        assert bucket.s3.calls("put_object", DUMP_KEY) == []
        assert len(bucket.s3.calls("put_object", MANIFEST_KEY)) == 1


def test_changed_mtime_with_matching_checksum_skips_put_and_updates_sqlite(tmp_path) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    with MotoBucket() as bucket:
        assert run_backup(config, client=bucket.s3).exit_code == 0
        checkpoint = Checkpoint.open(config.sqlite_path())
        before = dict(checkpoint.get_file("library/user/IMG.jpg"))
        checkpoint.close()
        stamp = time.time_ns() - 400 * 1_000_000_000
        photo = config.data_dir / "library" / "user" / "IMG.jpg"
        os_utime(photo, stamp)
        bucket.s3.events.clear()

        result = run_backup(config, client=bucket.s3)

        assert result.exit_code == 0
        assert result.uploaded == 0
        assert bucket.s3.calls("put_object", LIBRARY_KEY) == []
        assert bucket.s3.calls("head_object", LIBRARY_KEY)
        checkpoint = Checkpoint.open(config.sqlite_path())
        row = checkpoint.get_file("library/user/IMG.jpg")
        assert row["sha256"] == before["sha256"]
        assert row["uploaded_at"] == before["uploaded_at"]
        assert row["mtime_ns"] == stamp
        checkpoint.close()


def test_changed_bytes_are_uploaded_again(tmp_path) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    with MotoBucket() as bucket:
        assert run_backup(config, client=bucket.s3).exit_code == 0
        photo = config.data_dir / "library" / "user" / "IMG.jpg"
        write_bytes(photo, b"replaced-bytes")
        bucket.s3.events.clear()

        result = run_backup(config, client=bucket.s3)

        assert result.exit_code == 0
        assert result.uploaded == 1
        puts = bucket.s3.calls("put_object", LIBRARY_KEY)
        assert len(puts) == 1
        assert puts[0]["Metadata"]["sha256"] == _sha(b"replaced-bytes")
        assert puts[0]["StorageClass"] == "DEEP_ARCHIVE"


def test_fresh_and_future_files_are_skipped(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "old.jpg", b"old")
    write_bytes(config.data_dir / "library" / "fresh.jpg", b"fresh", age_ns=10 * 1_000_000_000)
    future = config.data_dir / "library" / "future.jpg"
    write_bytes(future, b"future")
    later = time.time_ns() + 3600 * 1_000_000_000
    os_utime(future, later)
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    with MotoBucket() as bucket:
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 0
        assert result.uploaded == 1
        assert result.skipped == 2
        listed = [item["Key"] for item in bucket.raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/originals/")["Contents"]]
        assert listed == ["immich/originals/library/old.jpg"]


def test_escape_is_rejected_and_other_files_continue(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    (config.data_dir / "library" / "escape.jpg").symlink_to(outside)
    (config.data_dir / "library" / "alias.jpg").symlink_to(config.data_dir / "library" / "keep.jpg")
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    with MotoBucket() as bucket:
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 1
        assert result.failed == 1
        assert result.failures[0][1] == "path escapes root"
        listed = [item["Key"] for item in bucket.raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/originals/")["Contents"]]
        assert listed == ["immich/originals/library/keep.jpg"]


def test_failed_put_is_absent_from_the_manifest_and_retried_three_times(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "good.jpg", b"good")
    write_bytes(config.data_dir / "library" / "bad.jpg", b"bad")
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    with MotoBucket() as bucket:
        bucket.s3.fail_put_keys.add("immich/originals/library/bad.jpg")
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 1
        assert len(bucket.s3.calls("put_object", "immich/originals/library/bad.jpg")) == 3
        listed = [item["Key"] for item in bucket.raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/originals/")["Contents"]]
        assert listed == ["immich/originals/library/good.jpg"]
        paths = [row["relative_path"] for row in _rows(bucket.raw)]
        assert paths == ["library/good.jpg"]
        assert "library/bad.jpg" not in _manifest(bucket.raw)

    checkpoint = Checkpoint.open(config.sqlite_path())
    try:
        assert checkpoint.get_file("library/bad.jpg") is None
        assert checkpoint.get_file("library/good.jpg") is not None
        failures = checkpoint.last_failures()
        assert [item["relative_path"] for item in failures] == ["library/bad.jpg"]
        last = checkpoint.last_run()
        assert last["failed"] == 1
        assert last["status"] == "failed"
    finally:
        checkpoint.close()


def test_multipart_upload_and_aborted_failure(tmp_path) -> None:
    config = make_config(tmp_path, multipart_threshold=32)
    write_bytes(config.data_dir / "library" / "big.bin", b"B" * 80)
    write_bytes(config.data_dir / "library" / "small.jpg", b"small")
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    with MotoBucket() as bucket:
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 0
        created = bucket.s3.calls("create_multipart_upload", "immich/originals/library/big.bin")
        assert len(created) == 1
        assert created[0]["StorageClass"] == "DEEP_ARCHIVE"
        assert created[0]["Metadata"]["sha256"] == _sha(b"B" * 80)
        assert bucket.s3.calls("put_object", "immich/originals/library/big.bin") == []
        assert bucket.s3.calls("abort_multipart_upload") == []
        head = bucket.raw.head_object(Bucket=BUCKET, Key="immich/originals/library/big.bin")
        assert head["StorageClass"] == "DEEP_ARCHIVE"
        assert head["Metadata"]["sha256"] == _sha(b"B" * 80)
        small = bucket.s3.calls("put_object", "immich/originals/library/small.jpg")
        assert small[0]["StorageClass"] == "DEEP_ARCHIVE"

    config.sqlite_path().unlink()
    write_bytes(config.data_dir / "upload" / "again.bin", b"C" * 90)
    with MotoBucket() as bucket:
        bucket.s3.fail_parts = True
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 1
        assert len(bucket.s3.calls("abort_multipart_upload")) == 6
        listed = [item["Key"] for item in bucket.raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/originals/")["Contents"]]
        assert listed == ["immich/originals/library/small.jpg"]
        checkpoint = Checkpoint.open(config.sqlite_path())
        try:
            assert checkpoint.get_file("library/big.bin") is None
            assert checkpoint.get_file("upload/again.bin") is None
            assert checkpoint.get_file("library/small.jpg") is not None
        finally:
            checkpoint.close()


def test_missing_checkpoint_is_rebuilt_from_list_and_head(tmp_path) -> None:
    config = make_config(tmp_path)
    kept = b"already-there"
    bare = b"needs-a-checksum"
    orphan = b"local-file-is-gone"
    write_bytes(config.data_dir / "library" / "kept.jpg", kept)
    write_bytes(config.data_dir / "library" / "bare.jpg", bare)
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    with MotoBucket() as bucket:
        bucket.raw.put_object(
            Bucket=BUCKET,
            Key="immich/originals/library/kept.jpg",
            Body=kept,
            StorageClass="DEEP_ARCHIVE",
            Metadata={"sha256": _sha(kept)},
        )
        bucket.raw.put_object(
            Bucket=BUCKET,
            Key="immich/originals/library/bare.jpg",
            Body=bare,
            StorageClass="DEEP_ARCHIVE",
        )
        bucket.raw.put_object(
            Bucket=BUCKET,
            Key="immich/originals/library/orphan.jpg",
            Body=orphan,
            StorageClass="DEEP_ARCHIVE",
            Metadata={"sha256": _sha(orphan)},
        )
        bucket.raw.put_object(Bucket=BUCKET, Key="immich/manifest.csv", Body=b"stale")
        bucket.s3.events.clear()

        result = run_backup(config, client=bucket.s3)

        assert result.exit_code == 0
        assert bucket.s3.calls("put_object", "immich/originals/library/kept.jpg") == []
        assert bucket.s3.calls("put_object", "immich/originals/library/orphan.jpg") == []
        bare_puts = bucket.s3.calls("put_object", "immich/originals/library/bare.jpg")
        assert len(bare_puts) == 1
        assert bare_puts[0]["Metadata"]["sha256"] == _sha(bare)
        paths = [row["relative_path"] for row in _rows(bucket.raw)]
        assert paths == ["library/bare.jpg", "library/kept.jpg", "library/orphan.jpg"]
        checkpoint = Checkpoint.open(config.sqlite_path())
        try:
            assert checkpoint.get_file("library/kept.jpg")["mtime_ns"] is not None
            assert checkpoint.get_file("library/orphan.jpg")["mtime_ns"] is None
        finally:
            checkpoint.close()
        assert not any(name.startswith("delete") for name, _kwargs in bucket.s3.events)


def test_removed_local_file_stays_in_the_archive_and_the_manifest(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    write_bytes(config.data_dir / "library" / "gone.jpg", b"gone")
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    with MotoBucket() as bucket:
        assert run_backup(config, client=bucket.s3).exit_code == 0
        (config.data_dir / "library" / "gone.jpg").unlink()
        bucket.s3.events.clear()
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 0
        assert result.uploaded == 0
        paths = [row["relative_path"] for row in _rows(bucket.raw)]
        assert paths == ["library/gone.jpg", "library/keep.jpg"]
        assert bucket.raw.head_object(Bucket=BUCKET, Key="immich/originals/library/gone.jpg")["ContentLength"] == 4
        assert not any(name.startswith("delete") for name, _kwargs in bucket.s3.events)


def test_missing_dump_exits_after_photos(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    write_bytes(config.backups() / "notes.jpg", b"not-a-dump")
    with MotoBucket() as bucket:
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 1
        assert result.dump_copied is False
        assert result.uploaded == 1
        assert bucket.raw.head_object(Bucket=BUCKET, Key="immich/originals/library/keep.jpg")
        with pytest.raises(ClientError):
            bucket.raw.head_object(Bucket=BUCKET, Key=DUMP_KEY)
        assert _manifest(bucket.raw).splitlines()[1].startswith("library/keep.jpg")


def test_empty_library_still_uploads_a_header_only_manifest(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    with MotoBucket() as bucket:
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 0
        assert result.scanned == 0
        assert _manifest(bucket.raw) == "relative_path,size,sha256,s3_key,uploaded_at\n"
        assert bucket.s3.calls("put_object", MANIFEST_KEY)[0]["StorageClass"] == "STANDARD"


def test_newer_dump_wins_and_the_same_dump_is_not_copied_twice(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    stamp = time.time_ns() - 500 * 1_000_000_000
    for name in ("a.sql.gz", "m.sql.gz"):
        path = config.backups() / name
        path.write_bytes(name.encode())
        os_utime(path, stamp)
    with MotoBucket() as bucket:
        first = run_backup(config, client=bucket.s3)
        assert first.exit_code == 0
        assert first.dump_copied is True
        head = bucket.raw.head_object(Bucket=BUCKET, Key=DUMP_KEY)
        assert head["Metadata"]["source-name"] == "m.sql.gz"
        assert bucket.raw.get_object(Bucket=BUCKET, Key=DUMP_KEY)["Body"].read() == b"m.sql.gz"
        bucket.s3.events.clear()
        second = run_backup(config, client=bucket.s3)
        assert second.exit_code == 0
        assert second.dump_copied is False
        assert bucket.s3.calls("put_object", DUMP_KEY) == []


def test_directory_overrides_replace_the_data_dir_roots(tmp_path) -> None:
    config = make_config(tmp_path)
    library = tmp_path / "custom-library"
    backups = tmp_path / "custom-backups"
    write_bytes(library / "from-override.jpg", b"override")
    write_bytes(config.data_dir / "library" / "ignored.jpg", b"ignored")
    write_bytes(backups / "immich-db-backup.sql.gz", b"dump-override")
    config.library_dir = library
    config.backup_dir = backups
    with MotoBucket() as bucket:
        result = run_backup(config, client=bucket.s3)
        assert result.exit_code == 0
        listed = [item["Key"] for item in bucket.raw.list_objects_v2(Bucket=BUCKET, Prefix="immich/originals/")["Contents"]]
        assert listed == ["immich/originals/library/from-override.jpg"]
        assert bucket.raw.get_object(Bucket=BUCKET, Key=DUMP_KEY)["Body"].read() == b"dump-override"


def test_saved_concurrency_is_the_pool_size(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path, concurrency=2)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")
    from immich_archive.config import save_runtime_settings

    save_runtime_settings(config, "0 3 * * *", 4)
    seen: list[int] = []
    real = ThreadPoolExecutor

    class Spy(real):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen.append(max_workers)
            super().__init__(max_workers, *args, **kwargs)

    monkeypatch.setattr("immich_archive.runner.ThreadPoolExecutor", Spy)
    with MotoBucket() as bucket:
        assert run_backup(config, client=bucket.s3).exit_code == 0
    assert seen == [4]


def test_missing_credentials_exit_before_any_upload(tmp_path, capsys) -> None:
    config = make_config(tmp_path, access_key="", secret_key="", region="", bucket="")
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    result = run_backup(config)
    assert result.exit_code == 1
    assert not config.sqlite_path().exists()
    assert "missing credentials" in capsys.readouterr().out


def test_missing_bucket_and_access_denied_mark_nothing(tmp_path, capsys) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    with mock_aws():
        result = run_backup(config, client=aws_client())
    assert result.exit_code == 1
    assert not config.sqlite_path().exists()
    assert "bucket missing or access denied" in capsys.readouterr().out

    class Deny:
        def head_bucket(self, **_kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": "denied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "HeadBucket",
            )

        def put_object(self, **_kwargs):
            raise AssertionError("put")

    result = run_backup(config, client=Deny())
    assert result.exit_code == 1
    assert not config.sqlite_path().exists()


def test_corrupt_checkpoint_exits_without_deleting_anything(tmp_path, capsys) -> None:
    config = make_config(tmp_path)
    config.sqlite_path().write_bytes(b"this is not a sqlite database")
    before = config.sqlite_path().read_bytes()
    result = run_backup(config, client=_Explode())
    assert result.exit_code == 1
    assert config.sqlite_path().read_bytes() == before
    message = capsys.readouterr().out
    assert "delete" in message
    assert str(config.sqlite_path()) in message
    assert probe(config.sqlite_path()) == "corrupt"


def test_held_lock_skips_and_exits_zero(tmp_path, capsys) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    lock = acquire_run_lock(config.lock_path())
    assert lock is not None
    try:
        result = run_backup(config, client=_Explode())
    finally:
        lock.release()
    assert result.exit_code == 0
    assert result.skipped_lock is True
    assert "skipped lock held" in capsys.readouterr().out
    assert not config.sqlite_path().exists()


def test_dry_run_writes_nothing_and_reports_would_be_uploads(tmp_path, monkeypatch, capsys) -> None:
    config = make_config(tmp_path, access_key="", secret_key="")
    write_bytes(config.data_dir / "library" / "old.jpg", b"old")
    write_bytes(config.data_dir / "upload" / "fresh.jpg", b"fresh", age_ns=5 * 1_000_000_000)
    write_bytes(config.backups() / "immich-db-backup.sql.gz", b"dump")

    def opened(*_args, **_kwargs):
        raise AssertionError("dry-run created a checkpoint")

    monkeypatch.setattr(Checkpoint, "open", opened)
    result = run_backup(config, dry_run=True, client=_Explode())
    assert result.exit_code == 0
    assert result.uploaded == 1
    assert result.skipped == 1
    assert result.dump_copied is False
    assert "dry_run=yes" in result.summary
    assert "dry_run=yes" in capsys.readouterr().out
    assert not config.sqlite_path().exists()


def test_dry_run_with_checkpoint_hashes_but_does_not_put_or_write(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    seed_tree(config)
    with MotoBucket() as bucket:
        assert run_backup(config, client=bucket.s3).exit_code == 0
        photo = config.data_dir / "library" / "user" / "IMG.jpg"
        # Same bytes, different mtime: a dry-run must HEAD and then skip the PUT.
        os_utime(photo, time.time_ns() - 900 * 1_000_000_000)
        before = config.sqlite_path().read_bytes()
        bucket.s3.events.clear()

        def refuse_write(*_args, **_kwargs):
            raise AssertionError("dry-run wrote the checkpoint")

        monkeypatch.setattr(Checkpoint, "upsert_file", refuse_write)
        monkeypatch.setattr(Checkpoint, "set_dump", refuse_write)
        monkeypatch.setattr(Checkpoint, "add_run", refuse_write)
        result = run_backup(config, dry_run=True, client=bucket.s3)

        assert result.exit_code == 0
        assert result.uploaded == 0
        assert result.skipped == 3
        assert "dry_run=yes" in result.summary
        assert bucket.s3.calls("put_object") == []
        assert bucket.s3.calls("create_multipart_upload") == []
        assert bucket.s3.calls("head_object", LIBRARY_KEY)
        assert config.sqlite_path().read_bytes() == before


def test_probe_missing_does_not_create_the_file(tmp_path) -> None:
    path = tmp_path / "state.sqlite"
    assert probe(path) == "missing"
    assert not path.exists()


def test_probe_recovers_a_hot_journal_without_deleting_the_database(tmp_path) -> None:
    import signal
    import sqlite3
    import subprocess
    import sys

    path = tmp_path / "state.sqlite"
    code = f"""
import sqlite3, time
path = {str(path)!r}
conn = sqlite3.connect(path)
conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY)")
conn.commit()
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO files (id) VALUES (1)")
print("HOT", flush=True)
time.sleep(30)
"""
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "HOT"
        assert Path(f"{path}-journal").exists()
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert probe(path) == "ok"
    assert path.exists()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    finally:
        connection.close()


def os_utime(path: Path, stamp: int) -> None:
    import os

    os.utime(path, ns=(stamp, stamp))
