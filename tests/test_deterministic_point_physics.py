"""Charter 267 补记八: the F systems' physics lane.

The GRIB forms each publisher was probed to serve (2026-10-18) are read as
GRIB states them; an unknown form fails the run closed; an integral is only a
record spanning two read steps or the difference of two sharing a start; a
local day is summed only when its intervals cover all 24 hours; the table is
append-only, and a pass writes, commits, then fetches the next run.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from bayes_basket_target import deterministic_point_physics as P
from bayes_basket_target.contracts import ContractError

RUN = datetime(2026, 10, 18, 0, tzinfo=timezone.utc)

GFS_IDX = "\n".join([
    "1:0:d=2026101800:TMP:2 m above ground:24 hour fcst:",
    "2:100:d=2026101800:APCP:surface:18-24 hour acc fcst:",
    "3:200:d=2026101800:APCP:surface:0-1 day acc fcst:",
    "4:300:d=2026101800:TCDC:entire atmosphere:24 hour fcst:",
    "5:400:d=2026101800:TCDC:entire atmosphere:18-24 hour ave fcst:",
    "6:500:d=2026101800:DSWRF:surface:18-24 hour ave fcst:",
])


def test_noaa_span_picks_the_record_of_the_asked_kind():
    assert P.noaa_span(GFS_IDX, field="TCDC:entire atmosphere", kind="instant") == (300, 100)
    assert P.noaa_span(GFS_IDX, field="TCDC:entire atmosphere", kind="avg") == (400, 100)
    assert P.noaa_span(GFS_IDX, field="APCP:surface", kind="accum") == (100, 100)
    assert P.noaa_span(GFS_IDX, field="DSWRF:surface", kind="avg") == (500, None)
    assert P.noaa_span(GFS_IDX, field="DSWRF:surface", kind="instant") is None


@pytest.mark.parametrize(
    ("var", "meta", "value", "expected"),
    [
        ("sw", {"step_type": "avg", "units": "W m**-2", "start": 18, "end": 21}, 100.0, [18, 21, "integral", 1.08e6]),
        ("sw", {"step_type": "accum", "units": "J m**-2", "start": 0, "end": 21}, 5e6, [0, 21, "integral", 5e6]),
        ("sw_dir", {"step_type": "avg", "units": "W m**-2", "start": 0, "end": 3}, 10.0, [0, 3, "integral", 108000.0]),
        ("pr", {"step_type": "accum", "units": "m", "start": 0, "end": 24}, 0.002, [0, 24, "integral", 2.0]),
        ("pr", {"step_type": "accum", "units": "kg m**-2", "start": 18, "end": 24}, 1.5, [18, 24, "integral", 1.5]),
        ("tcc", {"step_type": "instant", "units": "(0 - 1)", "start": 21, "end": 21}, 0.25, [21, 21, "instant", 25.0]),
        ("tcc", {"step_type": "instant", "units": "%", "start": 21, "end": 21}, 60.0, [21, 21, "instant", 60.0]),
        ("td", {"step_type": "instant", "units": "K", "start": 21, "end": 21}, 283.15, [21, 21, "instant", 10.0]),
    ],
)
def test_normalise_reads_each_published_form(var, meta, value, expected):
    got = P.normalise(var, meta, value)
    assert got[:3] == expected[:3]
    assert got[3] == pytest.approx(expected[3])


@pytest.mark.parametrize(
    ("var", "meta"),
    [
        ("sw", {"step_type": "instant", "units": "W m**-2", "start": 3, "end": 3}),
        ("pr", {"step_type": "avg", "units": "kg m**-2", "start": 0, "end": 3}),
        ("tcc", {"step_type": "instant", "units": "okta", "start": 3, "end": 3}),
        ("td", {"step_type": "instant", "units": "C", "start": 3, "end": 3}),
        ("td", {"step_type": "avg", "units": "K", "start": 0, "end": 3}),
    ],
)
def test_normalise_fails_closed_on_an_unknown_form(var, meta):
    with pytest.raises(ContractError):
        P.normalise(var, meta, 1.0)


def test_normalise_drops_an_implausible_value_rather_than_guessing():
    assert P.normalise("td", {"step_type": "instant", "units": "K", "start": 3, "end": 3}, 9999.0) is None
    assert P.normalise("pr", {"step_type": "accum", "units": "kg m**-2", "start": 0, "end": 3}, 9999.0) is None


def test_intervals_from_since_start_accumulations():
    records = [[0, 3, "integral", 1.0], [0, 6, "integral", 3.0], [0, 9, "integral", 6.0]]
    assert P.intervals(records) == [(0, 3, 1.0), (3, 6, 2.0), (6, 9, 3.0)]


def test_intervals_from_gfs_six_hour_buckets():
    # GFS: f21 is the 18-21 bucket, f24 the 18-24 bucket, f27 restarts at 24.
    records = [[18, 21, "integral", 3.0], [18, 24, "integral", 7.0], [24, 27, "integral", 2.0]]
    assert P.intervals(records) == [(18, 21, 3.0), (21, 24, 4.0), (24, 27, 2.0)]


def test_intervals_leave_a_gap_they_cannot_prove():
    # f24 missing: 21->24 cannot be formed from what was read; 24->27 is its own bucket.
    assert P.intervals([[18, 21, "integral", 3.0], [24, 27, "integral", 2.0]]) == [(18, 21, 3.0), (24, 27, 2.0)]
    assert P.intervals([[0, 21, "integral", 3.0], [0, 27, "integral", 5.0]]) == [(0, 21, 3.0), (21, 27, 2.0)]


def _day_fields(step_hours: int, first: int, last: int):
    steps = list(range(first, last + 1, step_hours))
    return {
        "sw": [[0, s, "integral", 1e6 * s] for s in steps],
        "pr": [[0, s, "integral", 0.5 * s] for s in steps],
        "tcc": [[s, s, "instant", float(s)] for s in steps],
        "td": [[s, s, "instant", 1.0] for s in steps],
    }


def test_local_day_sums_only_a_fully_covered_day():
    # UTC+0 day 2026-10-19 = steps 24..48 of the 00Z 10-18 run.
    full = P.local_day(_day_fields(3, 21, 51), run_init=RUN, event_date=date(2026, 10, 19), offset_hours=0)
    assert full["sw_hours"] == 24.0
    assert full["sw_mj"] == pytest.approx(24.0)
    assert full["pr_mm"] == pytest.approx(12.0)
    short = P.local_day(_day_fields(3, 30, 51), run_init=RUN, event_date=date(2026, 10, 19), offset_hours=0)
    assert short["sw_mj"] is None and short["pr_mm"] is None


def test_local_day_daytime_means_read_local_nine_to_seventeen():
    daily = P.local_day(_day_fields(3, 21, 51), run_init=RUN, event_date=date(2026, 10, 19), offset_hours=0)
    # Local 09, 12, 15 -> steps 33, 36, 39 (cloud carries the step number).
    assert daily["tcc_day_n"] == 3
    assert daily["tcc_day_mean"] == pytest.approx((33 + 36 + 39) / 3)
    assert daily["td_max"] == 1.0


def test_icon_shortwave_is_direct_plus_diffuse():
    fields = _day_fields(3, 21, 51)
    sw = fields.pop("sw")
    fields["sw_dir"] = sw
    fields["sw_dif"] = [[a, b, k, v / 2] for a, b, k, v in sw]
    daily = P.local_day(fields, run_init=RUN, event_date=date(2026, 10, 19), offset_hours=0)
    assert daily["sw_mj"] == pytest.approx(36.0)


def test_wanted_steps_take_one_step_either_side_of_the_day():
    steps = P.wanted_steps("GFS", RUN, date(2026, 10, 19), 8)
    # Local day 10-19 at UTC+8 = 16Z 10-18 .. 16Z 10-19 -> steps 18..39, plus 15 and 42.
    assert steps[0] == 15 and steps[1] == 18 and steps[-2] == 39 and steps[-1] == 42


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_regional_point_run (ensemble_system_id TEXT, run_init_utc TEXT, station_id TEXT,"
        " target_local_date TEXT, settlement_timezone TEXT, knowable_at_utc TEXT)"
    )
    P.ensure_schema(conn)
    return conn


def _captured():
    return {"fields": _day_fields(3, 21, 51), "steps": list(range(21, 52, 3)),
            "cell": {"latitude": 1.0, "longitude": 2.0, "moved": False}}


def test_table_is_append_only_and_rows_are_idempotent():
    conn = _conn()
    kwargs = dict(product="GFS", station_id="EGLC", event_date=date(2026, 10, 19), settlement_timezone="UTC",
                  run_init=RUN, knowable_at_utc="2026-10-18T04:30:00Z", captured_at=RUN, capture_mode="forward",
                  captured=_captured())
    assert P.write_row(conn, **kwargs) is True
    assert P.write_row(conn, **kwargs) is False
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(f"UPDATE {P.TABLE} SET station_id='X'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(f"DELETE FROM {P.TABLE}")


def test_plan_takes_temperature_rows_without_physics_and_read_daily_is_as_of(monkeypatch):
    conn = _conn()
    system = P.SYSTEM_BY_PRODUCT["GFS"]
    conn.execute("INSERT INTO calibration_regional_point_run VALUES (?,?,?,?,?,?)",
                 (system, "2026-10-18T00:00:00Z", "EGLC", "2026-10-19", "UTC", "2026-10-18T04:30:00Z"))
    conn.commit()
    items = P.plan(conn, products=["GFS"], since=date(2026, 10, 17), until=date(2026, 10, 21),
                   stations={"EGLC": (51.5, 0.05)})
    assert [(i["product"], len(i["targets"])) for i in items] == [("GFS", 1)]
    target = items[0]["targets"][0]
    assert target["knowable_at_utc"] == "2026-10-18T04:30:00Z" and target["utc_offset_hours"] == 0

    fetched = []

    def fake_fetch_run(*, product, run_init, targets):
        fetched.append((product, run_init))
        # Commit-before-fetch: nothing of an earlier run is still pending.
        assert not conn.in_transaction
        return {(t["station_id"], t["event_date"].isoformat()): _captured() for t in targets}

    monkeypatch.setattr(P, "fetch_run", fake_fetch_run)
    receipt = P.capture(conn, items, now=RUN, capture_mode="forward", log=lambda _line: None)
    assert receipt["failed"] == [] and receipt["written"] == 1 and not conn.in_transaction
    assert P.plan(conn, products=["GFS"], since=date(2026, 10, 17), until=date(2026, 10, 21),
                  stations={"EGLC": (51.5, 0.05)}) == []
    assert P.read_daily(conn, station_id="EGLC", event_date="2026-10-19", as_of_utc="2026-10-18T04:00:00Z") == {}
    got = P.read_daily(conn, station_id="EGLC", event_date="2026-10-19", as_of_utc="2026-10-18T05:00:00Z")
    assert got[system]["sw_mj"] == pytest.approx(24.0)
    row = conn.execute(f"SELECT reference_only, capture_mode, fields_json FROM {P.TABLE}").fetchone()
    assert row[0] == 1 and row[1] == "forward" and "sw" in json.loads(row[2])
