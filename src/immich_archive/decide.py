"""Pure decisions for paths, skip rules, dumps, manifests, and the next cron tick."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

FRESHNESS_NS = 60 * 1_000_000_000


class PathEscape(Exception):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"path escapes root: {path}")


def _is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def safe_relative(root: Path, path: Path) -> str:
    """Posix path of `path` relative to `root`.

    Symlinks are not followed. A path whose resolved location is outside `root`
    raises PathEscape.
    """
    root_resolved = root.resolve()
    if path.is_symlink():
        try:
            target = path.resolve()
        except OSError as exc:
            raise PathEscape(path) from exc
        if not _is_inside(root_resolved, target):
            raise PathEscape(path)
        raise PathEscape(path)
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise PathEscape(path) from exc
    if not _is_inside(root_resolved, resolved):
        raise PathEscape(path)
    relative = resolved.relative_to(root_resolved).as_posix()
    if relative in ("", ".") or relative.startswith("../") or "/../" in f"/{relative}/":
        raise PathEscape(path)
    return relative


def archive_relative(root_name: str, root: Path, path: Path) -> str:
    """`library/...` style key fragment. No leading slash."""
    relative = safe_relative(root, path)
    return f"{root_name}/{relative}"


def escapes(root: Path, path: Path) -> bool:
    """True when `path` resolves outside `root`. In-tree symlinks are not escapes."""
    root_resolved = root.resolve()
    if path.is_symlink():
        try:
            target = path.resolve()
        except OSError:
            return True
        return not _is_inside(root_resolved, target)
    try:
        safe_relative(root, path)
    except PathEscape:
        return True
    return False


def strip_prefix(prefix: str) -> str:
    return prefix.strip().strip("/")


def object_key(prefix: str, relative_path: str) -> str:
    tail = f"originals/{relative_path.lstrip('/')}"
    cleaned = strip_prefix(prefix)
    if cleaned:
        return f"{cleaned}/{tail}"
    return tail


def originals_prefix(prefix: str) -> str:
    cleaned = strip_prefix(prefix)
    if cleaned:
        return f"{cleaned}/originals/"
    return "originals/"


def manifest_key(prefix: str) -> str:
    cleaned = strip_prefix(prefix)
    if cleaned:
        return f"{cleaned}/manifest.csv"
    return "manifest.csv"


def dump_key(prefix: str) -> str:
    cleaned = strip_prefix(prefix)
    if cleaned:
        return f"{cleaned}/db/immich-latest.sql.gz"
    return "db/immich-latest.sql.gz"


def relative_from_object_key(prefix: str, key: str) -> str | None:
    base = originals_prefix(prefix)
    if not key.startswith(base) or key.endswith("/"):
        return None
    relative = key[len(base) :]
    if not relative or relative.startswith("/"):
        return None
    return relative


def file_too_new(mtime_ns: int, run_started_ns: int) -> bool:
    """True when the file is in the future or was modified less than 60s before the run."""
    if mtime_ns > run_started_ns:
        return True
    return (run_started_ns - mtime_ns) < FRESHNESS_NS


def unchanged(stored_size: int, stored_mtime_ns: int | None, file_size: int, file_mtime_ns: int) -> bool:
    if stored_mtime_ns is None:
        return False
    return stored_size == file_size and stored_mtime_ns == file_mtime_ns


@dataclass(frozen=True)
class DumpChoice:
    name: str
    mtime_ns: int
    size: int


def choose_dump(candidates: list[DumpChoice]) -> DumpChoice | None:
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.mtime_ns, item.name))


def manifest_csv(rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["relative_path", "size", "sha256", "s3_key", "uploaded_at"])
    for row in sorted(rows, key=lambda item: str(item["relative_path"])):
        writer.writerow(
            [
                row["relative_path"],
                row["size"],
                row["sha256"],
                row["s3_key"],
                row["uploaded_at"],
            ]
        )
    return buffer.getvalue()


def next_cron_time(expression: str, after: datetime, tz_name: str) -> datetime:
    """Next cron instant strictly after `after`. Missed ticks are not returned."""
    zone = ZoneInfo(tz_name)
    if after.tzinfo is None:
        local = after.replace(tzinfo=zone)
    else:
        local = after.astimezone(zone)
    naive = local.replace(tzinfo=None)
    schedule = croniter(expression, naive)
    upcoming = schedule.get_next(datetime)
    if upcoming <= naive:
        upcoming = schedule.get_next(datetime)
    if upcoming.tzinfo is None:
        return upcoming.replace(tzinfo=zone)
    return upcoming.astimezone(zone)


def format_summary(
    *,
    scanned: int,
    uploaded: int,
    skipped: int,
    failed: int,
    dump_copied: bool,
    duration: float,
    dry_run: bool = False,
) -> str:
    line = (
        f"scanned={scanned} uploaded={uploaded} skipped={skipped} "
        f"failed={failed} dump_copied={'yes' if dump_copied else 'no'} "
        f"duration={duration:.2f}s"
    )
    if dry_run:
        line += " dry_run=yes"
    return line
