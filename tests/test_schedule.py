"""The scheduler runs at startup and then waits for the next future cron match.

The cron here is every minute so the simulated clock does not step through a whole day.
`next_cron_time` itself is tested against the daily 03:00 cron.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from immich_archive.config import save_runtime_settings
from immich_archive.schedule import scheduler_loop
from tests.support import make_config

UTC = ZoneInfo("UTC")
EVERY_MINUTE = "* * * * *"


def test_startup_run_then_the_next_future_cron_not_an_immediate_second_run(tmp_path) -> None:
    config = make_config(tmp_path, cron=EVERY_MINUTE, tz="UTC")
    runs: list[datetime] = []
    clock = {"now": datetime(2026, 11, 2, 2, 0, 30, tzinfo=UTC)}

    def now():
        return clock["now"]

    def run():
        runs.append(clock["now"])

    def sleep(seconds):
        clock["now"] = clock["now"] + timedelta(seconds=seconds)

    scheduler_loop(config, run=run, sleep=sleep, now=now, max_runs=2)
    assert runs == [
        datetime(2026, 11, 2, 2, 0, 30, tzinfo=UTC),
        datetime(2026, 11, 2, 2, 1, 0, tzinfo=UTC),
    ]


def test_tick_that_elapsed_during_a_run_is_not_replayed(tmp_path) -> None:
    config = make_config(tmp_path, cron=EVERY_MINUTE, tz="UTC")
    runs: list[datetime] = []
    clock = {"now": datetime(2026, 11, 2, 2, 0, 0, tzinfo=UTC)}

    def now():
        return clock["now"]

    def run():
        runs.append(clock["now"])
        if len(runs) == 1:
            # The 02:01 tick passes while this startup run is still going.
            clock["now"] = datetime(2026, 11, 2, 2, 1, 30, tzinfo=UTC)

    def sleep(seconds):
        clock["now"] = clock["now"] + timedelta(seconds=seconds)

    scheduler_loop(config, run=run, sleep=sleep, now=now, max_runs=2)
    assert runs == [
        datetime(2026, 11, 2, 2, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 2, 2, 0, tzinfo=UTC),
    ]


def test_lock_held_at_the_tick_does_not_replay_that_tick(tmp_path) -> None:
    config = make_config(tmp_path, cron=EVERY_MINUTE, tz="UTC")
    runs: list[datetime] = []
    clock = {"now": datetime(2026, 11, 2, 2, 0, 30, tzinfo=UTC)}

    def now():
        return clock["now"]

    def run():
        # A held lock returns immediately, so the clock stays on the tick.
        runs.append(clock["now"])

    def sleep(seconds):
        clock["now"] = clock["now"] + timedelta(seconds=seconds)

    scheduler_loop(config, run=run, sleep=sleep, now=now, max_runs=3)
    assert runs == [
        datetime(2026, 11, 2, 2, 0, 30, tzinfo=UTC),
        datetime(2026, 11, 2, 2, 1, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 2, 2, 0, tzinfo=UTC),
    ]


def test_saved_cron_is_what_the_next_wake_uses(tmp_path) -> None:
    config = make_config(tmp_path, cron=EVERY_MINUTE, tz="UTC")
    runs: list[datetime] = []
    clock = {"now": datetime(2026, 11, 2, 2, 0, 0, tzinfo=UTC)}
    switched = {"done": False}

    def now():
        return clock["now"]

    def run():
        runs.append(clock["now"])

    def sleep(seconds):
        clock["now"] = clock["now"] + timedelta(seconds=seconds)
        if not switched["done"] and clock["now"] >= datetime(2026, 11, 2, 2, 0, 10, tzinfo=UTC):
            save_runtime_settings(config, "2 * * * *", 1)
            switched["done"] = True

    scheduler_loop(config, run=run, sleep=sleep, now=now, max_runs=2)
    assert runs == [
        datetime(2026, 11, 2, 2, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 2, 2, 0, tzinfo=UTC),
    ]
