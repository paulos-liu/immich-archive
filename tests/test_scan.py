"""The walk stays inside library, upload, and profile and does not follow symlinks."""

from __future__ import annotations

from immich_archive.scan import FoundEscape, FoundFile, walk_roots
from tests.support import make_config, write_bytes


def test_walk_skips_ignored_directories_and_rejects_escapes(tmp_path) -> None:
    config = make_config(tmp_path)
    write_bytes(config.data_dir / "library" / "keep.jpg", b"keep")
    write_bytes(config.data_dir / "upload" / "clip.mp4", b"clip")
    write_bytes(config.data_dir / "profile" / "face.jpg", b"face")
    write_bytes(config.data_dir / "thumbs" / "keep.jpg", b"thumb")
    write_bytes(config.data_dir / "encoded-video" / "clip.mp4", b"encoded")
    write_bytes(config.backups() / "notes.jpg", b"not-original")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    (config.data_dir / "library" / "escape.jpg").symlink_to(outside)
    real = config.data_dir / "library" / "keep.jpg"
    (config.data_dir / "library" / "alias.jpg").symlink_to(real)
    linked = config.data_dir / "library" / "linked-thumbs"
    linked.symlink_to(config.data_dir / "thumbs", target_is_directory=True)

    found = walk_roots(config.roots())
    files = [item for item in found if isinstance(item, FoundFile)]
    escapes = [item for item in found if isinstance(item, FoundEscape)]
    relatives = sorted(item.relative_path for item in files)

    assert relatives == ["library/keep.jpg", "profile/face.jpg", "upload/clip.mp4"]
    assert all("/" in item.relative_path and not item.relative_path.startswith("/") for item in files)
    assert len(escapes) == 2
    assert not any("thumbs" in item.relative_path or "encoded-video" in item.relative_path for item in files)


def test_missing_root_is_skipped(tmp_path) -> None:
    config = make_config(tmp_path)
    (config.data_dir / "library").rmdir()
    write_bytes(config.data_dir / "upload" / "only.jpg", b"only")
    found = walk_roots(config.roots())
    assert [item.relative_path for item in found] == ["upload/only.jpg"]
