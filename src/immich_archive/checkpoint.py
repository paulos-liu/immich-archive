"""Sqlite checkpoint. The archive in S3 is the source of truth; this file is disposable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  relative_path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime_ns INTEGER,
  sha256 TEXT NOT NULL,
  s3_key TEXT NOT NULL,
  uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dump_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  source_name TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  status TEXT NOT NULL,
  scanned INTEGER NOT NULL,
  uploaded INTEGER NOT NULL,
  skipped INTEGER NOT NULL,
  failed INTEGER NOT NULL,
  dump_copied INTEGER NOT NULL,
  duration_seconds REAL NOT NULL,
  summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failures (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  relative_path TEXT NOT NULL,
  error TEXT NOT NULL
);
"""


def probe(path: Path) -> str:
    """Return 'missing', 'ok', or 'corrupt'. Does not create or delete the database file.

    A killed run can leave a hot rollback journal. A read-only open cannot finish
    that rollback, so probe opens the same file read-write and lets sqlite roll
    the journal back. A file that is not a database is left untouched.
    """
    if not path.exists():
        return "missing"
    if _readable(path, "ro"):
        return "ok"
    journal = Path(f"{path}-journal")
    if journal.exists() and _readable(path, "rw"):
        return "ok"
    return "corrupt"


def _readable(path: Path, mode: str) -> bool:
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1")
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return False
    return True


class Checkpoint:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> Checkpoint:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        connection.commit()
        return cls(connection)

    @classmethod
    def open_readonly(cls, path: Path) -> Checkpoint:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return cls(connection)

    def close(self) -> None:
        self.connection.close()

    def get_file(self, relative_path: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM files WHERE relative_path = ?",
            (relative_path,),
        ).fetchone()

    def upsert_file(
        self,
        *,
        relative_path: str,
        size: int,
        mtime_ns: int | None,
        sha256: str,
        s3_key: str,
        uploaded_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO files (relative_path, size, mtime_ns, sha256, s3_key, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
              size = excluded.size,
              mtime_ns = excluded.mtime_ns,
              sha256 = excluded.sha256,
              s3_key = excluded.s3_key,
              uploaded_at = excluded.uploaded_at
            """,
            (relative_path, size, mtime_ns, sha256, s3_key, uploaded_at),
        )
        self.connection.commit()

    def all_files(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT relative_path, size, sha256, s3_key, uploaded_at FROM files ORDER BY relative_path"
        ).fetchall()
        return [dict(row) for row in rows]

    def search_files(self, query: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT relative_path, size, sha256, s3_key, uploaded_at
            FROM files
            WHERE relative_path LIKE ? ESCAPE '\\'
            ORDER BY relative_path
            """,
            (f"%{_like(query)}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def totals(self) -> tuple[int, int]:
        count, total = self.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files"
        ).fetchone()
        return int(count), int(total)

    def get_dump(self) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM dump_state WHERE id = 1").fetchone()

    def set_dump(self, *, source_name: str, size: int, mtime_ns: int, uploaded_at: str) -> None:
        self.connection.execute("DELETE FROM dump_state")
        self.connection.execute(
            "INSERT INTO dump_state (id, source_name, size, mtime_ns, uploaded_at) VALUES (1, ?, ?, ?, ?)",
            (source_name, size, mtime_ns, uploaded_at),
        )
        self.connection.commit()

    def add_run(
        self,
        *,
        started_at: str,
        finished_at: str,
        status: str,
        scanned: int,
        uploaded: int,
        skipped: int,
        failed: int,
        dump_copied: bool,
        duration_seconds: float,
        summary: str,
        failures: list[tuple[str, str]],
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO runs (
              started_at, finished_at, status, scanned, uploaded, skipped, failed,
              dump_copied, duration_seconds, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                status,
                scanned,
                uploaded,
                skipped,
                failed,
                1 if dump_copied else 0,
                duration_seconds,
                summary,
            ),
        )
        run_id = int(cursor.lastrowid)
        self.connection.executemany(
            "INSERT INTO failures (run_id, relative_path, error) VALUES (?, ?, ?)",
            [(run_id, path, error) for path, error in failures],
        )
        self.connection.commit()
        return run_id

    def last_run(self) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def failures_for_run(self, run_id: int) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT relative_path, error FROM failures
            WHERE run_id = ? ORDER BY relative_path
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def last_failures(self, query: str = "") -> list[dict[str, object]]:
        last = self.last_run()
        if last is None:
            return []
        if not query:
            return self.failures_for_run(int(last["id"]))
        rows = self.connection.execute(
            """
            SELECT relative_path, error FROM failures
            WHERE run_id = ? AND relative_path LIKE ? ESCAPE '\\'
            ORDER BY relative_path
            """,
            (int(last["id"]), f"%{_like(query)}%"),
        ).fetchall()
        return [dict(row) for row in rows]


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
