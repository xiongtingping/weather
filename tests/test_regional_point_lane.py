"""Charter 95 (re-ruled): the regional models' P1 lane -- its own table, every cycle."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, date

import pytest

from bayes_basket_target import regional_point_lane as L


def _conn():
    conn = sqlite3.connect(":memory:")
    assert L.ensure_schema(conn) is True
    assert L.ensure_schema(conn) is False
    return conn


def test_a_run_is_stored_once_with_its_curve_on_valid_instants() -> None:
    conn = _conn()
    run = datetime(2026, 9, 6, 15, tzinfo=timezone.utc)  # 11:00 New York
    curve = {0: 27.0, 1: 28.1, 2: 29.0, 3: 29.6, 4: 29.9, 5: 29.7, 6: 29.0, 7: 28.2, 8: 27.5}
    assert L.write_run(
        conn, product="HRRR", station_id="KATL", event_date=date(2026, 9, 6),
        settlement_timezone="America/New_York", run_init=run, hourly_c=curve,
        hours_wanted=9, archive_query_at=run.replace(hour=16, minute=40), source_url="s3://hrrr",
    ) is True
    assert L.write_run(
        conn, product="HRRR", station_id="KATL", event_date=date(2026, 9, 6),
        settlement_timezone="America/New_York", run_init=run, hourly_c=curve,
        hours_wanted=9, archive_query_at=run.replace(hour=17), source_url="s3://hrrr",
    ) is False, "same run, same station-day: stored once"
    row = conn.execute(
        f"SELECT ensemble_system_id, run_cycle_hour, lead_hours_to_local_day_start, forecast_tmax_c,"
        f" remaining_peak_complete, hours_covered, hourly_c_json FROM {L.TABLE}"
    ).fetchone()
    assert row[0] == "NCEP_HRRR_POINT" and row[1] == 15
    assert row[2] == pytest.approx(-11.0), "issued eleven hours into the local day"
    assert row[3] == pytest.approx(29.9) and row[5] == 9
    assert row[4] == 1, "11:00..18:00 local are all on the curve"
    valid = json.loads(row[6])
    assert list(valid)[0] == "2026-09-06T15:00:00Z" and valid["2026-09-06T19:00:00Z"] == 29.9
    assert L.stored_run_inits(conn, system_id="NCEP_HRRR_POINT", station_id="KATL", event_date="2026-09-06") == {"2026-09-06T15:00:00Z"}


def test_runs_for_reads_only_what_was_knowable_freshest_first() -> None:
    conn = _conn()
    for hour, q in ((12, 14), (15, 17), (18, 20)):
        run = datetime(2026, 9, 6, hour, tzinfo=timezone.utc)
        L.write_run(
            conn, product="NBM", station_id="KATL", event_date=date(2026, 9, 6),
            settlement_timezone="America/New_York", run_init=run,
            hourly_c={0: 28.0 + hour / 10, 1: 29.0}, hours_wanted=2,
            archive_query_at=run.replace(hour=q), source_url="s3://nbm",
        )
    rows = L.runs_for(conn, station_id="KATL", event_date="2026-09-06", as_of_utc="2026-09-06T18:30:00Z")
    assert [r["run_init_utc"] for r in rows] == ["2026-09-06T15:00:00Z", "2026-09-06T12:00:00Z"], (
        "the 18Z run is archived at 20Z and is not knowable at 18:30Z"
    )
    assert rows[0]["ensemble_system_id"] == "NCEP_NBM_POINT"


def test_candidate_runs_cover_every_cycle_from_a_day_before_to_the_days_end() -> None:
    now = datetime(2026, 9, 6, 20, tzinfo=timezone.utc)
    runs = L._candidate_runs("HRRR", now, date(2026, 9, 6), "America/New_York")
    hours = [r.hour for r in runs]
    assert runs[0] == datetime(2026, 9, 5, 4, tzinfo=timezone.utc), "a day before the local midnight (04Z)"
    assert runs[-1] == datetime(2026, 9, 6, 18, tzinfo=timezone.utc), "18Z is the last run 1.5h old at 20Z"
    assert len(hours) == 39 and all(0 <= h <= 23 for h in hours)
    msm = L._candidate_runs("MSM", now, date(2026, 9, 7), "Asia/Tokyo")
    assert all(r.hour % 3 == 0 for r in msm)
    assert msm[-1] <= now - __import__("datetime").timedelta(hours=3)


def test_the_plan_skips_stored_runs_and_caps_newest_first() -> None:
    conn = _conn()
    now = datetime(2026, 9, 6, 20, tzinfo=timezone.utc)
    stations = [("KATL", 33.63, -84.44, "America/New_York")]
    L.write_run(
        conn, product="HRRR", station_id="KATL", event_date=date(2026, 9, 6),
        settlement_timezone="America/New_York",
        run_init=datetime(2026, 9, 6, 18, tzinfo=timezone.utc), hourly_c={0: 30.0},
        hours_wanted=1, archive_query_at=now, source_url="s3://hrrr",
    )
    plan = L.plan_p1(conn, product="HRRR", now=now, stations=stations, day_offsets=(0,), max_runs=3)
    assert [item["run_init"].hour for item in plan][:3] == [17, 16, 15], "18Z stored, then newest first"
    assert len(plan) == 12, "four tries per productive slot, so an unpublished newest run cannot starve the rest"
    assert plan[0]["targets"][0]["event_date"] == date(2026, 9, 6)


def test_an_unpublished_newest_run_does_not_consume_the_cap(monkeypatch) -> None:
    conn = _conn()
    now = datetime(2026, 9, 6, 23, tzinfo=timezone.utc)
    stations = [("KATL", 33.63, -84.44, "America/New_York")]
    seen: list[int] = []

    def fake_fetch(*, product, run_init, targets):
        seen.append(run_init.hour)
        if run_init.hour >= 20:
            return {(t["station_id"], t["event_date"].isoformat()): {"hourly_c": {}, "hours_wanted": 4, "source_url": "s3"} for t in targets}
        return {(t["station_id"], t["event_date"].isoformat()): {"hourly_c": {0: 25.0, 1: 26.0}, "hours_wanted": 2, "source_url": "s3"} for t in targets}

    monkeypatch.setattr(L, "fetch_noaa_run_for_targets", fake_fetch)
    receipt = L.capture_p1(conn, product="HRRR", now=now, stations=stations, day_offsets=(0,), max_runs=2)
    assert receipt["runs_with_data"] == 2 and receipt["written"] == 2
    assert receipt["unpublished"] == ["HRRR 2026-09-06T21Z", "HRRR 2026-09-06T20Z"]
    assert seen == [21, 20, 19, 18], "two misses, then two productive runs, then stop"


def test_remaining_peak_completeness_is_judged_from_the_run_hour() -> None:
    zone = "America/New_York"
    run = datetime(2026, 9, 6, 19, tzinfo=timezone.utc)  # 15:00 local: 15..18 remain
    assert L.remaining_peak_complete(run, date(2026, 9, 6), zone, {0: 1, 1: 1, 2: 1, 3: 1}) is True
    assert L.remaining_peak_complete(run, date(2026, 9, 6), zone, {0: 1, 1: 1}) is False
    late = datetime(2026, 9, 7, 0, tzinfo=timezone.utc)  # 20:00 local: nothing of the peak remains
    assert L.remaining_peak_complete(late, date(2026, 9, 6), zone, {0: 1}) is False


def test_a_backfilled_run_is_knowable_when_the_archive_held_it() -> None:
    """A pass reaching weeks back asserts the run was on the archive then.

    Stamping such a row at the fetch time hides every backfilled run behind
    its own repair date: the first replay after the backfill resolved nothing
    at all, because 2026-08-25's runs read as knowable on 2026-09-06.
    """
    conn = _conn()
    run = datetime(2026, 8, 25, 18, tzinfo=timezone.utc)
    fetched = datetime(2026, 9, 6, 13, 34, tzinfo=timezone.utc)
    L.write_run(
        conn, product="HRRR", station_id="KATL", event_date=date(2026, 8, 25),
        settlement_timezone="America/New_York", run_init=run, hourly_c={0: 30.0, 1: 31.0},
        hours_wanted=2, archive_query_at=fetched, source_url="s3://hrrr", backfill=True,
    )
    row = conn.execute(
        f"SELECT archive_query_at_utc, knowable_at_utc FROM {L.TABLE}"
    ).fetchone()
    assert row[0].startswith("2026-09-06"), "the fetch is recorded as it happened"
    assert row[1] == "2026-08-25T19:30:00Z", "HRRR is on the bucket 90 minutes after its hour"
    assert L.runs_for(conn, station_id="KATL", event_date="2026-08-25", as_of_utc="2026-08-25T20:00:00Z")
    assert not L.runs_for(conn, station_id="KATL", event_date="2026-08-25", as_of_utc="2026-08-25T19:00:00Z")


def test_a_live_capture_is_knowable_when_it_was_fetched() -> None:
    conn = _conn()
    run = datetime(2026, 9, 6, 6, tzinfo=timezone.utc)
    fetched = datetime(2026, 9, 6, 11, 35, tzinfo=timezone.utc)
    L.write_run(
        conn, product="ICON_EU", station_id="LTAC", event_date=date(2026, 9, 6),
        settlement_timezone="Europe/Istanbul", run_init=run, hourly_c={5: 28.0},
        hours_wanted=1, archive_query_at=fetched, source_url="dwd",
    )
    row = conn.execute(f"SELECT archive_query_at_utc, knowable_at_utc FROM {L.TABLE}").fetchone()
    assert row[0] == row[1], "a live sweep's fetch time is the honest answer"


def test_the_repair_reads_how_far_behind_the_fetch_was() -> None:
    """No wall-clock boundary: the backfill ran at 12:03Z and a live sweep at
    11:35Z the same morning, so only the gap to the run tells them apart."""
    conn = _conn()
    for product, run_hour, fetch_day, fetch_hour in (
        ("HRRR", 18, 25, 19), ("NBM", 6, 6, 12),
    ):
        L.write_run(
            conn, product=product, station_id="KATL",
            event_date=date(2026, 8, 25) if fetch_day == 25 else date(2026, 9, 6),
            settlement_timezone="America/New_York",
            run_init=datetime(2026, 8 if fetch_day == 25 else 9, 25 if fetch_day == 25 else 6, run_hour, tzinfo=timezone.utc),
            hourly_c={0: 30.0}, hours_wanted=1,
            archive_query_at=datetime(2026, 8 if fetch_day == 25 else 9, fetch_day, fetch_hour, tzinfo=timezone.utc),
            source_url="s3",
        )
    conn.execute(f"UPDATE {L.TABLE} SET knowable_at_utc=''")
    assert L.repair_knowable_at(conn) == 2
    rows = dict(conn.execute(
        f"SELECT ensemble_system_id, knowable_at_utc FROM {L.TABLE}"
    ).fetchall())
    assert rows["NCEP_HRRR_POINT"] == "2026-08-25T19:00:00Z", "fetched an hour later: a live sweep"
    assert rows["NCEP_NBM_POINT"] == "2026-09-06T12:00:00Z", "six hours behind is still a live sweep, so the fetch is the answer"
