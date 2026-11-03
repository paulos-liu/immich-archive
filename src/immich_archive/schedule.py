"""Startup run, then sleep until the next future cron match."""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from immich_archive.config import Config, runtime_settings
from immich_archive.decide import next_cron_time
from immich_archive.runner import run_backup
from immich_archive.web import make_server


def _default_now(config: Config):
    def now() -> datetime:
        return datetime.now(ZoneInfo(config.tz))

    return now


def _default_run(config: Config):
    def run() -> None:
        try:
            run_backup(config)
        except Exception:
            traceback.print_exc()

    return run


def scheduler_loop(
    config: Config,
    *,
    run=None,
    sleep=None,
    now=None,
    max_runs: int | None = None,
) -> None:
    """Run once immediately, then on each future cron match.

    A tick that elapsed during a run is not replayed. When a run returns at the
    tick (including because the lock was held), the following wake is the next
    future match. Saved cron and concurrency are read again about once a second.
    """
    if run is None:
        run = _default_run(config)
    if sleep is None:
        sleep = time.sleep
    if now is None:
        now = _default_now(config)

    completed = 0

    def once() -> bool:
        nonlocal completed
        run()
        completed += 1
        return max_runs is not None and completed >= max_runs

    if once():
        return

    while True:
        cron, _concurrency = runtime_settings(config)
        target = next_cron_time(cron, now(), config.tz)
        while True:
            current_cron, _concurrency = runtime_settings(config)
            current = now()
            if current_cron != cron:
                cron = current_cron
                target = next_cron_time(cron, current, config.tz)
            if current >= target:
                break
            delay = (target - current).total_seconds()
            sleep(min(1.0, max(0.0, delay)))
        if once():
            return


def serve(config: Config) -> None:
    """Serve the admin dashboard and run the schedule in this process."""
    httpd = make_server(config)
    host, port = httpd.server_address[:2]
    print(f"listening on {host}:{port}", flush=True)
    # The scheduler owns the main thread so a startup failure still leaves HTTP up.
    thread = threading.Thread(target=httpd.serve_forever, name="immich-archive-http", daemon=True)
    thread.start()
    try:
        scheduler_loop(config)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
