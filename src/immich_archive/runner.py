"""One backup run."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

from immich_archive.checkpoint import Checkpoint, probe
from immich_archive.config import Config, runtime_settings
from immich_archive.decide import (
    DumpChoice,
    choose_dump,
    dump_key,
    file_too_new,
    format_summary,
    manifest_csv,
    manifest_key,
    object_key,
    unchanged,
)
from immich_archive.hashing import sha256_file
from immich_archive.locks import acquire_run_lock
from immich_archive.s3store import head_sha256, list_archived, make_client, preflight, upload_bytes, upload_file
from immich_archive.scan import FoundEscape, FoundFile, walk_roots

ATTEMPTS = 3


@dataclass
class RunResult:
    exit_code: int
    skipped_lock: bool = False
    scanned: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    dump_copied: bool = False
    duration: float = 0.0
    summary: str = ""
    failures: list[tuple[str, str]] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list_dumps(directory: Path) -> list[DumpChoice]:
    if not directory.is_dir():
        return []
    choices: list[DumpChoice] = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if not path.name.endswith(".sql.gz"):
            continue
        stat = path.stat()
        choices.append(DumpChoice(name=path.name, mtime_ns=stat.st_mtime_ns, size=stat.st_size))
    return choices


def _needs_s3(dry_run: bool, state: str) -> bool:
    """A dry-run with no checkpoint reports every eligible file and does not call S3."""
    return not (dry_run and state == "missing")


def _process_file(client, config: Config, item: FoundFile, *, dry_run: bool) -> tuple[str, str, int, str]:
    """Hash, HEAD, and maybe upload. Returns (sha256, key, size, action).

    action is 'head' when the stored checksum already matches, otherwise 'put'.
    A dry-run never uploads. The file is retried up to three times.
    """
    last_error: Exception | None = None
    for _attempt in range(ATTEMPTS):
        try:
            digest = sha256_file(item.path)
            key = object_key(config.prefix, item.relative_path)
            stored = head_sha256(client, config.bucket, key)
            if stored == digest:
                return digest, key, item.size, "head"
            if dry_run:
                return digest, key, item.size, "put"
            actual = upload_file(
                client,
                bucket=config.bucket,
                key=key,
                path=item.path,
                storage_class="DEEP_ARCHIVE",
                sha256=digest,
                multipart_threshold=config.multipart_threshold,
            )
            return actual, key, item.path.stat().st_size, "put"
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def run_backup(config: Config, *, dry_run: bool = False, client=None) -> RunResult:
    started = time.perf_counter()
    started_iso = _now_iso()
    run_started_ns = time.time_ns()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock = acquire_run_lock(config.lock_path())
    if lock is None:
        print("skipped lock held", flush=True)
        return RunResult(exit_code=0, skipped_lock=True)

    try:
        return _run_locked(
            config,
            dry_run=dry_run,
            client=client,
            started=started,
            started_iso=started_iso,
            run_started_ns=run_started_ns,
        )
    finally:
        lock.release()


def _run_locked(
    config: Config,
    *,
    dry_run: bool,
    client,
    started: float,
    started_iso: str,
    run_started_ns: int,
) -> RunResult:
    sqlite_path = config.sqlite_path()
    state = probe(sqlite_path)
    if state == "corrupt":
        print(
            f"checkpoint unreadable: delete {sqlite_path} and rerun. The archive was not modified.",
            flush=True,
        )
        return RunResult(exit_code=1)

    needs_s3 = _needs_s3(dry_run, state)
    if needs_s3 and client is None:
        if not config.credentials_ready():
            print("missing credentials or bucket name", flush=True)
            return RunResult(exit_code=1)
        client = make_client(config)
    if needs_s3:
        try:
            preflight(client, config.bucket)
        except ClientError:
            print("bucket missing or access denied", flush=True)
            return RunResult(exit_code=1)

    checkpoint: Checkpoint | None = None
    try:
        if state == "missing" and not dry_run:
            checkpoint = Checkpoint.open(sqlite_path)
            for relative, key, size, checksum in list_archived(client, config.bucket, config.prefix):
                checkpoint.upsert_file(
                    relative_path=relative,
                    size=size,
                    mtime_ns=None,
                    sha256=checksum,
                    s3_key=key,
                    uploaded_at=started_iso,
                )
        elif state == "ok":
            checkpoint = Checkpoint.open_readonly(sqlite_path) if dry_run else Checkpoint.open(sqlite_path)

        _, concurrency = runtime_settings(config)
        scanned, skipped, failures, pending = _classify(config, checkpoint, run_started_ns)
        uploaded = 0
        if dry_run and state == "missing":
            uploaded = len(pending)
            pending = []
        else:
            uploaded, skipped, failures = _upload_pending(
                config,
                client=client,
                checkpoint=checkpoint,
                dry_run=dry_run,
                concurrency=concurrency,
                started_iso=started_iso,
                pending=pending,
                skipped=skipped,
                failures=failures,
            )

        dump_copied, dump_error, failures = _copy_dump(
            config,
            client=client,
            checkpoint=checkpoint,
            dry_run=dry_run,
            failures=failures,
        )
        manifest_error, failures = _copy_manifest(
            config,
            client=client,
            checkpoint=checkpoint,
            dry_run=dry_run,
            failures=failures,
        )
        failed = len(failures)
        duration = time.perf_counter() - started
        summary = format_summary(
            scanned=scanned,
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
            dump_copied=dump_copied,
            duration=duration,
            dry_run=dry_run,
        )
        exit_code = 1 if failures or dump_error or manifest_error else 0
        if checkpoint is not None and not dry_run:
            checkpoint.add_run(
                started_at=started_iso,
                finished_at=_now_iso(),
                status="failed" if exit_code else "ok",
                scanned=scanned,
                uploaded=uploaded,
                skipped=skipped,
                failed=failed,
                dump_copied=dump_copied,
                duration_seconds=duration,
                summary=summary,
                failures=failures,
            )
        result = RunResult(
            exit_code=exit_code,
            scanned=scanned,
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
            dump_copied=dump_copied,
            duration=duration,
            summary=summary,
            failures=failures,
        )
    finally:
        if checkpoint is not None:
            checkpoint.close()
    # Print only after the checkpoint connection is closed. A stop signal that
    # arrives once this line is visible must not leave a hot rollback journal.
    print(result.summary, flush=True)
    return result


def _classify(
    config: Config,
    checkpoint: Checkpoint | None,
    run_started_ns: int,
) -> tuple[int, int, list[tuple[str, str]], list[FoundFile]]:
    scanned = 0
    skipped = 0
    failures: list[tuple[str, str]] = []
    pending: list[FoundFile] = []
    for item in walk_roots(config.roots()):
        scanned += 1
        if isinstance(item, FoundEscape):
            failures.append((str(item.path), "path escapes root"))
            continue
        if file_too_new(item.mtime_ns, run_started_ns):
            skipped += 1
            continue
        row = checkpoint.get_file(item.relative_path) if checkpoint is not None else None
        if row is not None and unchanged(int(row["size"]), row["mtime_ns"], item.size, item.mtime_ns):
            skipped += 1
            continue
        pending.append(item)
    return scanned, skipped, failures, pending


def _upload_pending(
    config: Config,
    *,
    client,
    checkpoint: Checkpoint | None,
    dry_run: bool,
    concurrency: int,
    started_iso: str,
    pending: list[FoundFile],
    skipped: int,
    failures: list[tuple[str, str]],
) -> tuple[int, int, list[tuple[str, str]]]:
    uploaded = 0
    if not pending:
        return uploaded, skipped, failures

    def _one(item: FoundFile):
        try:
            digest, key, size, action = _process_file(client, config, item, dry_run=dry_run)
            return item, digest, key, size, action, None
        except Exception as error:
            return item, "", "", 0, "error", error

    workers = max(1, concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, item) for item in pending]
        for future in as_completed(futures):
            item, digest, key, size, action, error = future.result()
            if error is not None:
                failures.append((item.relative_path, str(error) or error.__class__.__name__))
                continue
            if dry_run:
                if action == "put":
                    uploaded += 1
                else:
                    skipped += 1
                continue
            assert checkpoint is not None
            previous = checkpoint.get_file(item.relative_path)
            uploaded_at = started_iso
            if action == "head" and previous is not None:
                uploaded_at = str(previous["uploaded_at"])
            checkpoint.upsert_file(
                relative_path=item.relative_path,
                size=size,
                mtime_ns=item.path.stat().st_mtime_ns,
                sha256=digest,
                s3_key=key,
                uploaded_at=uploaded_at,
            )
            if action == "put":
                uploaded += 1
            else:
                skipped += 1
    return uploaded, skipped, failures


def _dump_matches(checkpoint: Checkpoint, choice: DumpChoice) -> bool:
    current = checkpoint.get_dump()
    return (
        current is not None
        and current["source_name"] == choice.name
        and int(current["size"]) == choice.size
        and int(current["mtime_ns"]) == choice.mtime_ns
    )


def _copy_dump(
    config: Config,
    *,
    client,
    checkpoint: Checkpoint | None,
    dry_run: bool,
    failures: list[tuple[str, str]],
) -> tuple[bool, bool, list[tuple[str, str]]]:
    choice = choose_dump(_list_dumps(config.backups()))
    if choice is None:
        return False, True, failures
    if dry_run or checkpoint is None:
        return False, False, failures
    if _dump_matches(checkpoint, choice):
        return False, False, failures
    try:
        body = (config.backups() / choice.name).read_bytes()
        upload_bytes(
            client,
            bucket=config.bucket,
            key=dump_key(config.prefix),
            body=body,
            storage_class="STANDARD",
            metadata={"source-name": choice.name},
        )
        checkpoint.set_dump(
            source_name=choice.name,
            size=choice.size,
            mtime_ns=choice.mtime_ns,
            uploaded_at=_now_iso(),
        )
    except Exception as error:
        failures.append((choice.name, str(error) or error.__class__.__name__))
        return False, True, failures
    return True, False, failures


def _copy_manifest(
    config: Config,
    *,
    client,
    checkpoint: Checkpoint | None,
    dry_run: bool,
    failures: list[tuple[str, str]],
) -> tuple[bool, list[tuple[str, str]]]:
    if dry_run or checkpoint is None:
        return False, failures
    try:
        body = manifest_csv(checkpoint.all_files()).encode("utf-8")
        upload_bytes(
            client,
            bucket=config.bucket,
            key=manifest_key(config.prefix),
            body=body,
            storage_class="STANDARD",
            metadata={},
        )
    except Exception as error:
        failures.append(("manifest.csv", str(error) or error.__class__.__name__))
        return True, failures
    return False, failures
