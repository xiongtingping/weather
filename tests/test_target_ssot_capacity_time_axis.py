"""A byte budget with no time axis is a fuel gauge with no speedometer.

On 2026-09-07 this card read ``within_budget``, 4.78 GB remaining, no
warning at all -- while the database grew 780 MB a day, which is 6.1 days
from the ceiling and about two days from the warning line.  Every writer
fail-closes at that ceiling, so "how much room is left" is unusable
without "how fast it is going", and the rate was published nowhere.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(
        "daemon_under_test", ROOT / "scripts" / "weather_basket_yes_daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "TARGET_SSOT_GROWTH_PATH", tmp_path / "growth.json"
    )
    return module


def _seed(module, *, hours_ago: float, db_bytes: int) -> None:
    stamp = (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    module.TARGET_SSOT_GROWTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    module.TARGET_SSOT_GROWTH_PATH.write_text(json.dumps({
        "schema": "target_ssot_growth_v1",
        "observed_at_utc": stamp,
        "database_bytes": db_bytes,
    }))


def _database(tmp_path: Path, size: int) -> Path:
    path = tmp_path / "ssot.db"
    path.write_bytes(b"\0" * size)
    return path


def test_the_first_pass_reports_no_rate_and_seeds_one(
    daemon, tmp_path: Path
) -> None:
    """One reading is not a rate.  It must say so rather than guess."""

    guard = daemon.target_ssot_capacity_guard(_database(tmp_path, 4096))
    assert guard["growth_bytes_per_day"] is None
    assert guard["days_to_ceiling"] is None
    assert daemon.TARGET_SSOT_GROWTH_PATH.is_file(), (
        "the next pass has to have something to measure against"
    )


def test_two_readings_give_the_rate_and_the_days(
    daemon, tmp_path: Path
) -> None:
    """The 2026-09-07 shape, scaled down: room in bytes, none in time."""

    size = 1_000_000
    _seed(daemon, hours_ago=24.0, db_bytes=size - 100_000)
    guard = daemon.target_ssot_capacity_guard(_database(tmp_path, size))

    assert guard["growth_bytes_per_day"] == pytest.approx(100_000, rel=0.05)
    expected = (daemon.MAX_TARGET_DB_BYTES - size) / 100_000
    assert guard["days_to_ceiling"] == pytest.approx(expected, rel=0.05)


def test_days_left_can_warn_while_bytes_left_does_not(
    daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point.  Byte headroom was fine on 2026-09-07 and the card
    was green; the time axis is what would have been amber."""

    monkeypatch.setattr(daemon, "MAX_TARGET_DB_BYTES", 10_000_000)
    monkeypatch.setattr(daemon, "TARGET_SSOT_WARN_REMAINING_BYTES", 1_000)
    size = 8_000_000
    _seed(daemon, hours_ago=24.0, db_bytes=size - 1_000_000)

    guard = daemon.target_ssot_capacity_guard(_database(tmp_path, size))
    assert guard["remaining_bytes"] > guard["warn_remaining_bytes"], (
        "bytes must still look healthy, or this proves nothing"
    )
    assert guard["days_to_ceiling"] == pytest.approx(2.0, rel=0.05)
    assert "target_database_days_to_ceiling_low" in guard["warnings"]
    assert guard["state"] == "within_budget_low_headroom"
    assert guard["ok"] is True, "a warning is not an outage"


def test_a_retention_pass_is_not_extrapolated_as_a_rate(
    daemon, tmp_path: Path
) -> None:
    """A shrinking file means something was retired, not that the ceiling
    receded.  Reporting a negative rate would print a reassuring number
    the moment the pressure was actually being managed by hand."""

    _seed(daemon, hours_ago=24.0, db_bytes=5_000_000)
    guard = daemon.target_ssot_capacity_guard(_database(tmp_path, 4_000_000))
    assert guard["growth_bytes_per_day"] is None
    assert guard["days_to_ceiling"] is None


def test_a_reading_minutes_apart_is_rounding_not_a_rate(
    daemon, tmp_path: Path
) -> None:
    """The probe runs on a short timer; two readings a minute apart would
    turn one WAL checkpoint into a terabyte a day."""

    _seed(daemon, hours_ago=0.1, db_bytes=1_000_000)
    guard = daemon.target_ssot_capacity_guard(_database(tmp_path, 1_100_000))
    assert guard["growth_bytes_per_day"] is None


def test_the_last_rate_stands_until_a_newer_one_replaces_it(
    daemon, tmp_path: Path
) -> None:
    """The rate needs a reading an hour old, and computing it moves the
    baseline to now -- so the first version reported a number once an hour
    and null for the fifty-nine minutes between.  A gauge that is blank
    almost always is not a gauge."""

    size = 1_000_000
    _seed(daemon, hours_ago=24.0, db_bytes=size - 100_000)
    first = daemon.target_ssot_capacity_guard(_database(tmp_path, size))
    assert first["growth_bytes_per_day"] == pytest.approx(100_000, rel=0.05)

    # Immediately again: too soon to measure, but the answer is still known.
    second = daemon.target_ssot_capacity_guard(_database(tmp_path, size))
    assert second["growth_bytes_per_day"] == pytest.approx(100_000, rel=0.05)
    assert second["days_to_ceiling"] == pytest.approx(
        first["days_to_ceiling"], rel=0.05
    )
    assert second["growth_measured_from_utc"], (
        "a carried rate has to say when it was measured"
    )


def _sqlite_with_free_pages(
    tmp_path: Path, *, rows: int, delete: int
) -> Path:
    """A real SQLite file carrying free pages, like the live one does.

    The retention contracts delete rows every day and SQLite keeps the
    pages, so new writes land in the freelist and the FILE does not move.
    That is the state this whole axis has to survive.
    """

    import sqlite3

    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE bulk (id INTEGER PRIMARY KEY, body BLOB)")
    conn.executemany(
        "INSERT INTO bulk (id, body) VALUES (?, ?)",
        [(i, b"x" * 3500) for i in range(rows)],
    )
    conn.commit()
    conn.execute("DELETE FROM bulk WHERE id < ?", (delete,))
    conn.commit()
    conn.close()
    return path


def test_a_flat_file_over_a_growing_database_still_reports_a_rate(
    daemon, tmp_path: Path
) -> None:
    """The defect this axis was blind to for three days.

    Between 2026-09-08 and 2026-09-11 the live SSOT sat at exactly
    38,184,861,696 bytes while 1.4 GB of new rows went into free pages the
    retention contracts had left behind.  Growth measured on the FILE was
    zero the whole time, so no rate was computed, ``days_to_ceiling``
    stayed ``None``, and the two-day warning cannot fire on a ``None`` --
    the gauge was blank for exactly the stretch it exists for.

    Measured on live bytes, the same file reports the rate that will
    consume the headroom the moment the free pages run out.
    """

    database = _sqlite_with_free_pages(tmp_path, rows=400, delete=300)
    live_now, free_now = daemon._target_ssot_live_bytes(database)
    assert live_now is not None and free_now > 0, (
        "the fixture must actually carry free pages"
    )

    stamp = (
        datetime.now(timezone.utc) - timedelta(hours=24.0)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    daemon.TARGET_SSOT_GROWTH_PATH.write_text(json.dumps({
        "schema": "target_ssot_growth_v2",
        "observed_at_utc": stamp,
        # The file has not moved; the data inside it has.
        "database_bytes": database.stat().st_size,
        "live_bytes": live_now - 100_000,
        "free_page_bytes": free_now + 100_000,
    }))

    guard = daemon.target_ssot_capacity_guard(database)
    assert guard["growth_axis"] == "live_bytes"
    assert guard["growth_bytes_per_day"] == pytest.approx(100_000, rel=0.05)
    assert guard["days_to_ceiling"] is not None
    # Free pages are headroom -- rows land there before the file grows --
    # while the ceiling itself stays on the file, which is what the
    # writers fail closed against.
    assert guard["free_page_bytes"] == free_now
    assert guard["remaining_bytes"] == (
        daemon.MAX_TARGET_DB_BYTES - database.stat().st_size
    )
    expected_days = (
        daemon.MAX_TARGET_DB_BYTES - database.stat().st_size + free_now
    ) / 100_000
    assert guard["days_to_ceiling"] == pytest.approx(expected_days, rel=0.05)


def test_the_baseline_moves_even_when_it_produced_no_rate(
    daemon, tmp_path: Path
) -> None:
    """A flat reading must not freeze the clock it is measured against.

    The baseline was only rewritten when a rate came out of it, so three
    days of flat readings kept comparing against 2026-09-08 -- and the
    first real measurement after that would have been an average over the
    whole flat stretch, understating the rate that mattered by however
    long the freeze had lasted.
    """

    database = _sqlite_with_free_pages(tmp_path, rows=200, delete=150)
    live_now, _free = daemon._target_ssot_live_bytes(database)
    stamp = (
        datetime.now(timezone.utc) - timedelta(hours=6.0)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    daemon.TARGET_SSOT_GROWTH_PATH.write_text(json.dumps({
        "schema": "target_ssot_growth_v2",
        "observed_at_utc": stamp,
        "database_bytes": database.stat().st_size,
        # Shrunk since: a retention pass, which yields no rate.
        "live_bytes": live_now + 500_000,
        "free_page_bytes": 0,
    }))

    guard = daemon.target_ssot_capacity_guard(database)
    assert guard["growth_bytes_per_day"] is None, "a shrink is not a rate"
    written = json.loads(daemon.TARGET_SSOT_GROWTH_PATH.read_text())
    assert written["observed_at_utc"] != stamp, (
        "the baseline has to move, or the next pass measures against a "
        "reading that only gets older"
    )
    assert written["live_bytes"] == live_now


def test_a_window_the_process_restarted_inside_yields_no_rate(
    daemon, tmp_path: Path
) -> None:
    """Coming up costs a burst, and this process comes up several times an hour.

    Measured 2026-09-10: the first window after two restarts read 2,393
    MB/day and put the store 5.6 days from its ceiling, while 25 clean
    minutes the same afternoon read 829 MB/day and 16 days.  Nearly three
    times, and it is the restart's catch-up -- replays, re-ingest, whatever
    queued while the process was down -- not the store's rate.  A gauge
    that triples after every deploy is one nobody believes on the day it is
    right.

    Compared by identity rather than by clock: the baseline records which
    generation wrote it, and a mismatch IS a restart.  A baseline from
    before the field existed claims nothing, because unknown is not a
    restart.
    """

    database = _sqlite_with_free_pages(tmp_path, rows=400, delete=300)
    live_now, free_now = daemon._target_ssot_live_bytes(database)
    stamp = (
        datetime.now(timezone.utc) - timedelta(hours=24.0)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")

    def seed(generation: str | None) -> None:
        blob = {
            "schema": "target_ssot_growth_v2",
            "observed_at_utc": stamp,
            "database_bytes": database.stat().st_size,
            "live_bytes": live_now - 100_000,
            "free_page_bytes": free_now,
        }
        if generation is not None:
            blob["generation_started_at"] = generation
        daemon.TARGET_SSOT_GROWTH_PATH.write_text(json.dumps(blob))

    # Same generation wrote the baseline: an ordinary window, a real rate.
    seed(daemon._resident_started_at_utc())
    same = daemon.target_ssot_capacity_guard(database)
    assert same["growth_bytes_per_day"] == pytest.approx(100_000, rel=0.05)
    assert same["growth_window_spans_restart"] is False

    # A different generation wrote it: the process restarted in between.
    seed("2000-01-01T00:00:00Z")
    across = daemon.target_ssot_capacity_guard(database)
    assert across["growth_bytes_per_day"] is None
    assert across["growth_window_spans_restart"] is True
    # And the baseline still moves, so the next clean window can measure.
    written = json.loads(daemon.TARGET_SSOT_GROWTH_PATH.read_text())
    assert written["observed_at_utc"] != stamp
    assert written["generation_started_at"] == daemon._resident_started_at_utc()

    # A baseline predating the field claims nothing either way.
    seed(None)
    unknown = daemon.target_ssot_capacity_guard(database)
    assert unknown["growth_window_spans_restart"] is False
    assert unknown["growth_bytes_per_day"] == pytest.approx(100_000, rel=0.05)


def _wal(tmp_path: Path, size: int) -> None:
    """A WAL file of the given size, sparse: st_size is all the guard reads."""

    with (tmp_path / "ssot.db-wal").open("wb") as handle:
        handle.truncate(size)


def test_a_wal_the_truncate_is_not_landing_on_is_a_warning(
    daemon, tmp_path: Path
) -> None:
    """2026-09-23: a render held its read snapshot five hours, the WAL went
    from 64 MB to 14.9 GB, the disk filled -- and this card was green.  From
    the daemon's own truncate trigger up, the WAL is a warning: the
    checkpoint it attempts every interval is not landing."""

    database = _database(tmp_path, 4096)
    _wal(tmp_path, daemon.TARGET_SSOT_WARN_WAL_BYTES)
    guard = daemon.target_ssot_capacity_guard(database)
    assert guard["wal_bytes"] == daemon.TARGET_SSOT_WARN_WAL_BYTES
    assert guard["warn_wal_bytes"] == daemon.TARGET_SSOT_WARN_WAL_BYTES
    assert "target_wal_bytes_growing" in guard["warnings"]
    assert guard["violations"] == []
    assert guard["ok"] is True, "a warning is not an outage"
    assert guard["state"] == "within_budget_low_headroom"


def test_a_wal_past_the_writer_bound_is_a_violation(
    daemon, tmp_path: Path
) -> None:
    """Past MAX_TARGET_WAL_BYTES any writer (re)open fails closed on the
    truncating checkpoint it cannot take; the card says so in red."""

    database = _database(tmp_path, 4096)
    _wal(tmp_path, daemon.MAX_TARGET_WAL_BYTES + 1)
    guard = daemon.target_ssot_capacity_guard(database)
    assert guard["max_wal_bytes"] == daemon.MAX_TARGET_WAL_BYTES
    assert "target_wal_bytes_exceeded" in guard["violations"]
    assert "target_wal_bytes_growing" not in guard["warnings"]
    assert guard["ok"] is False
    assert guard["state"] == "blocked_fail_closed"


def test_a_wal_under_the_trigger_is_neither(daemon, tmp_path: Path) -> None:
    database = _database(tmp_path, 4096)
    _wal(tmp_path, 4096)
    guard = daemon.target_ssot_capacity_guard(database)
    assert guard["wal_bytes"] == 4096
    assert "target_wal_bytes_growing" not in guard["warnings"]
    assert "target_wal_bytes_exceeded" not in guard["violations"]
    assert guard["ok"] is True


def test_the_warning_line_is_the_maintenance_trigger(daemon) -> None:
    """One number: a WAL at the line the daemon truncates from, still
    there, is the daemon saying its own truncate is not landing."""

    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert ") >= TARGET_SSOT_WARN_WAL_BYTES" in source
    assert "256 * 1024 * 1024\n                    and now_mono >= next_target_wal_maintenance" not in source
