"""This product's archive is not addressable by run, so a pass stores one row.

The aggregator serves whatever forecast it currently holds and publishes no
run stamp.  ``run_init_utc`` is therefore derived from the cadence, which the
extraction version has said since the lane shipped -- and "derived from the
cadence" is singular: exactly one cycle is in force at any clock.

The planner did not read it that way.  It is shared with HRRR, the NBM, MSM
and ICON-EU, whose archives ARE addressable by run, so it enumerated every
unstored cycle in the window and fetched each one.  Against this transport
each of those fetches returned the same current forecast, and the lane stored
a copy under every label.

Measured 2026-09-16 over 2026-09-11..16, every pass without exception:

    pass 2026-09-11T07:02   labels 09-10T18 + 09-11T00   16/16 identical tmax
    pass 2026-09-13T08:03   labels 09-11T18 + 09-13T00    8/8  identical tmax
    pass 2026-09-15T07:54   labels 09-13T18 + 09-15T00    8/8  identical tmax
    pass 2026-09-16T00:31   labels 09-15T12 + 09-15T18    8/8  identical tmax

Hour by hour the twins agree wherever their windows overlap; the later label
is simply the earlier one with its first hours cut off, because
``hourly_valid_map`` keys the same series from a later origin.  Two of those
labels are fiction outright -- 2026-09-13T18Z was written by the pass of
2026-09-15T07:54Z, a stamp 38 hours older than the forecast under it.

That is not cosmetic.  ``regional_path_prior`` refuses a row whose
``as_of - run_init`` exceeds ``MAX_RUN_AGE_HOURS``, so the fresh forecast
wearing the stale label reads as too old to serve, while ``runs_for`` orders
by run_init and hands out the freshest LABEL -- the twin truncated hardest.
Anything that counts runs counts each fetch twice.

So: one cycle planned, one row per station-day, and the version bumped so
the rows written under the old reading stay distinguishable rather than
being deleted -- their curves are real and their knowable_at_utc is exact.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import cma_grapes_point_archive as archive  # noqa: E402
from bayes_basket_target import regional_point_lane as lane  # noqa: E402

SHANGHAI = ("ZSPD", 31.14, 121.81, "Asia/Shanghai")


def test_exactly_one_cycle_is_planned_however_many_have_elapsed():
    """The window holds several elapsed cycles; only one is in force."""

    now = datetime(2026, 9, 16, 3, 30, tzinfo=timezone.utc)
    runs = lane._candidate_runs("CMA", now, date(2026, 9, 16), "Asia/Shanghai")
    assert runs == [archive.derived_run_init(now)]
    assert len(runs) == 1

    # And the enumeration really would have offered more than one, or this
    # test would pass against a window that happened to hold a single cycle.
    elapsed = [
        stamp for stamp in (
            datetime(2026, 9, 15, hour, tzinfo=timezone.utc)
            for hour in archive.RUN_CYCLE_HOURS
        )
        if stamp <= now - timedelta(hours=archive.PUBLICATION_LAG_HOURS)
    ]
    assert len(elapsed) > 1


def test_the_run_addressable_products_still_enumerate_their_cadence():
    """The change is about one transport, not about the shared planner."""

    now = datetime(2026, 9, 16, 3, 30, tzinfo=timezone.utc)
    for product, station in (
        ("HRRR", ("KATL", 33.63, -84.44, "America/New_York")),
        ("MSM", SHANGHAI),
        ("ICON_EU", ("EDDM", 48.35, 11.79, "Europe/Berlin")),
    ):
        runs = lane._candidate_runs(product, now, date(2026, 9, 16), station[3])
        assert len(runs) > 1, (product, runs)


def test_a_cycle_outside_the_event_day_window_is_not_planned():
    """The window filter survives: a run after the local day has begun is
    not a forecast of it, and the enumeration's bounds were load-bearing."""

    now = datetime(2026, 9, 16, 3, 30, tzinfo=timezone.utc)
    # Three days back, the cycle in force is long past that day's end.
    assert lane._candidate_runs(
        "CMA", now, date(2026, 9, 12), "Asia/Shanghai"
    ) == []


def _drive_one_pass(conn, monkeypatch, *, now, fetched):
    """One CMA pass with the network replaced by a counter."""

    def fake_fetch(*, run_init, event_date, utc_offset_hours, latitude, longitude):
        fetched.append((run_init, event_date))
        return {
            "hourly_c": {hour: 18.0 + hour / 10.0 for hour in range(24)},
            "hours_wanted": list(range(24)),
        }

    monkeypatch.setattr(archive, "fetch_day_maximum", fake_fetch)
    monkeypatch.setattr(
        archive, "source_url", lambda lat, lon: "https://example.invalid/cma"
    )
    return lane.capture_p1(
        conn, product="CMA", now=now, stations=[SHANGHAI], max_runs=2,
    )


def test_one_pass_stores_one_row_per_station_day(tmp_path, monkeypatch):
    """The behavioural half: one fetch in, one row per station-day out."""

    conn = sqlite3.connect(tmp_path / "ssot.db")
    lane.ensure_schema(conn)
    conn.commit()
    now = datetime(2026, 9, 16, 3, 30, tzinfo=timezone.utc)
    fetched: list[tuple] = []
    receipt = _drive_one_pass(conn, monkeypatch, now=now, fetched=fetched)

    stored = list(conn.execute(
        f"SELECT station_id, target_local_date, run_init_utc FROM {lane.TABLE}"
        " ORDER BY target_local_date, run_init_utc"
    ))
    assert receipt["written"] == len(stored)
    # One label across the whole pass, and one row per station-day under it.
    labels = {row[2] for row in stored}
    assert len(labels) == 1, stored
    assert len({(row[0], row[1]) for row in stored}) == len(stored), stored
    # Every fetch that was paid for produced a row nobody else duplicates.
    assert len(fetched) == len(stored), (fetched, stored)


def test_a_second_pass_inside_the_same_cycle_makes_no_request(tmp_path, monkeypatch):
    """What pays for halving the interval.

    The lane now fires twice per cycle so that a blocked or starved pass has
    a second chance.  That is only affordable because the second pass, when
    the first succeeded, is refused by ``plan_p1`` before any request: the
    cycle in force is already stored.
    """

    conn = sqlite3.connect(tmp_path / "ssot.db")
    lane.ensure_schema(conn)
    conn.commit()
    # Just after 04:00Z, where the 00Z cycle comes into force: three hours
    # on is still inside its window, seven is not.
    first = datetime(2026, 9, 16, 4, 30, tzinfo=timezone.utc)
    fetched: list[tuple] = []
    _drive_one_pass(conn, monkeypatch, now=first, fetched=fetched)
    assert fetched, "the first pass must actually fetch, or this proves nothing"

    # Three hours later, still inside the same cycle's window.
    second = first + timedelta(hours=3)
    assert archive.derived_run_init(second) == archive.derived_run_init(first)
    before = len(fetched)
    receipt = _drive_one_pass(conn, monkeypatch, now=second, fetched=fetched)
    assert len(fetched) == before, "an already-stored cycle must cost no request"
    assert receipt["runs_planned"] == 0 and receipt["written"] == 0

    # And the pass after the cycle turns over does fetch again.
    third = first + timedelta(hours=7)
    assert archive.derived_run_init(third) != archive.derived_run_init(first)
    _drive_one_pass(conn, monkeypatch, now=third, fetched=fetched)
    assert len(fetched) > before


def test_the_old_labelling_is_marked_rather_than_left_to_blend_in():
    """Rows written under the enumerated cadence must stay tellable apart.

    They are not deleted: the curve is a real forecast and knowable_at_utc
    is the true fetch clock.  Only the cycle stamp is wrong, and only a
    reader that counts runs or measures run age can be misled by it -- so
    the version is what has to move.
    """

    assert lane.EXTRACTION_VERSIONS["CMA"].endswith("_v2")
    assert "derived_cycle" in lane.EXTRACTION_VERSIONS["CMA"]


def test_the_lane_fires_twice_inside_one_cycle_of_its_product(daemon_module):
    """The interval and the cadence are one decision, so check them together."""

    intervals = {
        name: interval
        for name, _script, interval, _timeout in daemon_module.HOSTED_SWEEPS
    }
    cycle_sec = lane.RUN_CYCLE_HOURS["CMA"]
    spacing_hours = 24 / len(cycle_sec)
    interval_hours = intervals["capture_cma_grapes_forward"] / 3600.0
    assert interval_hours <= spacing_hours / 2, (
        "a transport that serves only its current run needs a second attempt"
        f" inside each cycle: {interval_hours}h against a {spacing_hours}h cadence"
    )


@pytest.fixture(scope="module")
def daemon_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_cma_interval_daemon_under_test",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
