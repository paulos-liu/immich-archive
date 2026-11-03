"""Unit tests for path, skip, dump, manifest, and cron decisions."""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from immich_archive.decide import (
    FRESHNESS_NS,
    DumpChoice,
    PathEscape,
    archive_relative,
    choose_dump,
    dump_key,
    escapes,
    file_too_new,
    format_summary,
    manifest_csv,
    manifest_key,
    next_cron_time,
    object_key,
    originals_prefix,
    relative_from_object_key,
    safe_relative,
    unchanged,
)


def test_safe_relative_uses_forward_slashes_and_rejects_parents(tmp_path: Path) -> None:
    root = tmp_path / "library"
    nested = root / "user" / "2024"
    nested.mkdir(parents=True)
    photo = nested / "IMG.jpg"
    photo.write_bytes(b"pixels")
    via_dotdot = root / "user" / ".." / "user" / "2024" / "IMG.jpg"

    assert safe_relative(root, photo) == "user/2024/IMG.jpg"
    assert safe_relative(root, via_dotdot) == "user/2024/IMG.jpg"
    assert archive_relative("library", root, photo) == "library/user/2024/IMG.jpg"
    assert not archive_relative("library", root, photo).startswith("/")

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"nope")
    with pytest.raises(PathEscape):
        safe_relative(root, outside)
    with pytest.raises(PathEscape):
        safe_relative(root, root / ".." / "outside.jpg")


def test_symlink_inside_root_is_not_an_escape_and_is_not_archived(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    target = root / "real.jpg"
    target.write_bytes(b"real")
    link = root / "alias.jpg"
    link.symlink_to(target)

    assert escapes(root, link) is False
    with pytest.raises(PathEscape):
        safe_relative(root, link)


def test_symlink_that_leaves_the_root_is_an_escape(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    link = root / "escape.jpg"
    link.symlink_to(outside)
    broken = root / "broken.jpg"
    broken.symlink_to(tmp_path / "missing.jpg")

    assert escapes(root, link) is True
    assert escapes(root, broken) is True
    with pytest.raises(PathEscape):
        safe_relative(root, link)


def test_directory_symlink_outside_is_an_escape(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "encoded-video"
    outside.mkdir()
    link = root / "linked"
    link.symlink_to(outside, target_is_directory=True)
    assert escapes(root, link) is True


def test_object_keys_mirror_the_relative_path() -> None:
    assert object_key("immich", "library/user/IMG.jpg") == "immich/originals/library/user/IMG.jpg"
    assert object_key(" /immich/ ", "upload/a.jpg") == "immich/originals/upload/a.jpg"
    assert object_key("", "profile/a.jpg") == "originals/profile/a.jpg"
    assert originals_prefix("immich") == "immich/originals/"
    assert originals_prefix("") == "originals/"
    assert manifest_key("immich") == "immich/manifest.csv"
    assert dump_key("immich") == "immich/db/immich-latest.sql.gz"
    assert relative_from_object_key("immich", "immich/originals/library/user/IMG.jpg") == "library/user/IMG.jpg"
    assert relative_from_object_key("immich", "immich/originals/") is None
    assert relative_from_object_key("immich", "immich/manifest.csv") is None
    assert relative_from_object_key("immich", "immich/originals/library/folder/") is None


def test_file_too_new_is_sixty_seconds_and_any_future_mtime() -> None:
    start = 10_000_000_000_000
    assert file_too_new(start, start) is True
    assert file_too_new(start + 1, start) is True
    assert file_too_new(start - (FRESHNESS_NS - 1), start) is True
    assert file_too_new(start - FRESHNESS_NS, start) is False
    assert file_too_new(start - FRESHNESS_NS - 1, start) is False
    assert FRESHNESS_NS == 60 * 1_000_000_000


def test_unchanged_requires_size_and_mtime_and_null_mtime_never_matches() -> None:
    assert unchanged(10, 20, 10, 20) is True
    assert unchanged(10, None, 10, 20) is False
    assert unchanged(10, 20, 11, 20) is False
    assert unchanged(10, 20, 10, 21) is False


def test_choose_dump_prefers_mtime_then_greater_filename() -> None:
    older = DumpChoice("zzz.sql.gz", 10, 5)
    low_name = DumpChoice("a.sql.gz", 50, 5)
    high_name = DumpChoice("m.sql.gz", 50, 1)
    assert choose_dump([]) is None
    assert choose_dump([older, low_name]).name == "a.sql.gz"
    assert choose_dump([low_name, high_name, older]) == high_name


def test_manifest_is_quoted_csv_ordered_by_path_and_header_only_when_empty() -> None:
    assert manifest_csv([]) == "relative_path,size,sha256,s3_key,uploaded_at\n"

    body = manifest_csv(
        [
            {
                "relative_path": 'upload/b, "c".jpg',
                "size": 2,
                "sha256": "bb",
                "s3_key": 'immich/originals/upload/b, "c".jpg',
                "uploaded_at": "2026-11-02T03:00:00Z",
            },
            {
                "relative_path": "library/a.jpg",
                "size": 1,
                "sha256": "aa",
                "s3_key": "immich/originals/library/a.jpg",
                "uploaded_at": "2026-11-02T03:00:00Z",
            },
        ]
    )
    assert "\r" not in body
    lines = body.split("\n")
    assert lines[0] == "relative_path,size,sha256,s3_key,uploaded_at"
    assert lines[1] == "library/a.jpg,1,aa,immich/originals/library/a.jpg,2026-11-02T03:00:00Z"
    parsed = list(csv.DictReader(io.StringIO(body)))
    assert [row["relative_path"] for row in parsed] == ["library/a.jpg", 'upload/b, "c".jpg']
    assert parsed[1]["s3_key"] == 'immich/originals/upload/b, "c".jpg'
    assert parsed[1]["size"] == "2"


def test_next_cron_time_is_strictly_after_and_does_not_replay_missed_ticks() -> None:
    zone = ZoneInfo("UTC")
    cron = "0 3 * * *"

    at_tick = datetime(2026, 11, 2, 3, 0, tzinfo=zone)
    assert next_cron_time(cron, at_tick, "UTC") == datetime(2026, 11, 3, 3, 0, tzinfo=zone)

    before = datetime(2026, 11, 2, 2, 59, tzinfo=zone)
    assert next_cron_time(cron, before, "UTC") == datetime(2026, 11, 2, 3, 0, tzinfo=zone)

    # Startup at 02:00 waits for the coming 03:00, not a second immediate run.
    startup = datetime(2026, 11, 2, 2, 0, tzinfo=zone)
    assert next_cron_time(cron, startup, "UTC") == datetime(2026, 11, 2, 3, 0, tzinfo=zone)

    # A 03:00 tick that elapsed while a run was still going is not replayed.
    finished = datetime(2026, 11, 2, 3, 10, tzinfo=zone)
    assert next_cron_time(cron, finished, "UTC") == datetime(2026, 11, 3, 3, 0, tzinfo=zone)

    # A tick observed while a lock is held is skipped; the next wake is still the next future match.
    held = datetime(2026, 11, 2, 3, 0, 5, tzinfo=zone)
    assert next_cron_time(cron, held, "UTC") == datetime(2026, 11, 3, 3, 0, tzinfo=zone)

    naive = datetime(2026, 11, 2, 2, 0)
    assert next_cron_time(cron, naive, "UTC") == datetime(2026, 11, 2, 3, 0, tzinfo=zone)


def test_next_cron_time_uses_the_configured_timezone() -> None:
    # 10:00 UTC on 2026-11-02 is 02:00 in Los Angeles (PST, UTC-8). Next 03:00 local is that morning.
    after = datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo("UTC"))
    nxt = next_cron_time("0 3 * * *", after, "America/Los_Angeles")
    assert nxt == datetime(2026, 11, 2, 3, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert nxt.utcoffset() == timedelta(hours=-8)


def test_format_summary_lists_the_required_counters() -> None:
    line = format_summary(
        scanned=4,
        uploaded=1,
        skipped=2,
        failed=1,
        dump_copied=True,
        duration=1.2,
    )
    assert line == "scanned=4 uploaded=1 skipped=2 failed=1 dump_copied=yes duration=1.20s"
    dry = format_summary(
        scanned=1,
        uploaded=1,
        skipped=0,
        failed=0,
        dump_copied=False,
        duration=0,
        dry_run=True,
    )
    assert dry == "scanned=1 uploaded=1 skipped=0 failed=0 dump_copied=no duration=0.00s dry_run=yes"


def test_future_mtime_on_disk_is_too_new(tmp_path: Path) -> None:
    path = tmp_path / "fresh.jpg"
    path.write_bytes(b"x")
    now = time.time_ns()
    future = now + 3600 * 1_000_000_000
    os.utime(path, ns=(future, future))
    assert file_too_new(path.stat().st_mtime_ns, now) is True