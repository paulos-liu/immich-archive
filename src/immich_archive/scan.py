"""Walk the three Immich original directories. Never follows symlinks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from immich_archive.decide import archive_relative, escapes


@dataclass(frozen=True)
class FoundFile:
    path: Path
    relative_path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class FoundEscape:
    path: Path


def walk_roots(roots: list[tuple[str, Path]]) -> list[FoundFile | FoundEscape]:
    found: list[FoundFile | FoundEscape] = []
    for root_name, root in roots:
        if not root.is_dir():
            continue
        root_resolved = root.resolve()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            kept: list[str] = []
            for dirname in dirnames:
                candidate = Path(dirpath) / dirname
                if candidate.is_symlink():
                    if escapes(root_resolved, candidate):
                        found.append(FoundEscape(candidate))
                    continue
                kept.append(dirname)
            dirnames[:] = kept
            for filename in filenames:
                candidate = Path(dirpath) / filename
                if candidate.is_symlink():
                    if escapes(root_resolved, candidate):
                        found.append(FoundEscape(candidate))
                    continue
                if not candidate.is_file():
                    continue
                try:
                    relative = archive_relative(root_name, root, candidate)
                except Exception:
                    found.append(FoundEscape(candidate))
                    continue
                stat = candidate.stat()
                found.append(
                    FoundFile(
                        path=candidate,
                        relative_path=relative,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                    )
                )
    return found
