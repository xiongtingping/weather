"""Charter 102: CMA GRAPES Global captured for the Chinese stations.

Eight roster stations had no regional model at all.  This lane is their
first.  Since charter 150 it reaches P1 the way every regional lane does --
as a covariate of the joint observation likelihood -- and the separate
serving admission list this file used to pin retired with the path prior.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from bayes_basket_target import cma_grapes_point_archive as CMA
from bayes_basket_target import regional_features as R
from bayes_basket_target import regional_point_lane as LANE
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.official_native_forward_packet import (
    POINT_FORECAST_SYSTEMS,
    REGIONAL_DETERMINISTIC_SYSTEM_IDS,
    REGIONAL_MODELS_STAGE,
)


def test_the_lane_covers_the_stations_that_had_nothing() -> None:
    """The eight mainland Chinese stations, and the reason they qualify."""

    captured = {station for station, _, _, _ in CMA.CAPTURE_STATIONS}
    assert captured == {
        "ZBAA", "ZGGG", "ZGSZ", "ZHHH", "ZSPD", "ZSQD", "ZUCK", "ZUUU",
    }
    # Two of them are also inside MSM's domain and are captured there too:
    # the same station-day read by two systems is what lets the admission
    # gate tell a model apart from a station.
    from bayes_basket_target.jma_msm_archive import CAPTURE_STATIONS as MSM

    assert {"ZSPD", "ZSQD"} <= {station for station, _, _, _ in MSM}


def test_the_lane_is_registered_and_reaches_p1_as_a_covariate() -> None:
    """Registered as a regional P1 system, and a good CMA row reaches the
    likelihood's covariates like any other lane's."""

    assert "CMA_GRAPES_GLOBAL_POINT" in REGIONAL_DETERMINISTIC_SYSTEM_IDS
    assert POINT_FORECAST_SYSTEMS["CMA_GRAPES_GLOBAL_POINT"]["provider_id"] == "CMA"
    assert LANE.SYSTEM_IDS["CMA"] == "CMA_GRAPES_GLOBAL_POINT"
    assert REGIONAL_MODELS_STAGE == "P1"
    run = {
        "ensemble_system_id": "CMA_GRAPES_GLOBAL_POINT",
        "run_init_utc": "2026-09-07T00:00:00Z",
        "knowable_at_utc": "2026-09-07T04:00:00Z",
        "hourly_c": {
            "2026-09-07T10:00:00Z": 31.0,
            "2026-09-07T12:00:00Z": 33.5,
        },
    }
    features = R.regional_features_at(
        [run], as_of_utc="2026-09-07T09:30:00Z", running_high_c=30.0,
        latest_observed_c=29.5,
    )
    assert features["regional_system_id"] == "CMA_GRAPES_GLOBAL_POINT"
    assert features["regional_headroom_c"] == pytest.approx(3.5)


def test_a_cycle_is_asked_for_by_name() -> None:
    """The label is the run (v3): the request names it."""

    run = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    url = CMA.source_url(30.576, 103.95, run)
    assert "run=2026-09-18T12:00" in url and "single-runs-api" in url
    assert LANE.EXTRACTION_VERSIONS["CMA"].endswith("single_run_v3")


def test_a_backfilled_relay_row_is_stamped_late_or_refused() -> None:
    """A row that cannot say when it became knowable is not written.

    Charter 201: runs are addressed by name (charter 152), so a backfilled
    CMA row is the run its label says; when the relay first held it is the
    measured holding lag -- ten to eleven hours after init -- so the stamp is
    init + 11 h, late rather than early.  Too late for the same-day P1
    question, and useful for the day-ahead P0 feature, which is what a
    backfill is for.  A relay with no measured lag is still refused.
    """

    import sqlite3  # noqa: PLC0415
    from datetime import timedelta  # noqa: PLC0415

    with pytest.raises(ContractError) as caught:
        LANE.capture_p1(
            None, product="UKMO10", now=datetime.now(timezone.utc),
            stations=CMA.CAPTURE_STATIONS, backfill=True,
        )
    assert "backfill_unsupported" in str(caught.value)
    conn = sqlite3.connect(":memory:")
    LANE.ensure_schema(conn)
    run = datetime(2026, 9, 1, 0, tzinfo=timezone.utc)
    LANE.write_run(
        conn, product="CMA", station_id="ZUUU", event_date=date(2026, 9, 2),
        settlement_timezone="Asia/Shanghai", run_init=run, hourly_c={20: 30.0},
        hours_wanted=1, archive_query_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        source_url="om://cma", backfill=True,
    )
    knowable = conn.execute(f"SELECT knowable_at_utc FROM {LANE.TABLE}").fetchone()[0]
    assert knowable == (run + timedelta(hours=11)).isoformat().replace("+00:00", "Z")


def test_local_day_hours_are_the_stations_day_not_the_runs() -> None:
    run_init = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)
    hours = CMA.local_day_hours(
        run_init=run_init, event_date=date(2026, 9, 7), utc_offset_hours=8
    )
    # Asia/Shanghai's 2026-09-07 starts at 2026-09-06T16:00Z, which is two
    # hours before this run, so the run covers from its own hour 0.
    assert hours[0] == 0
    assert (run_init.hour + hours[-1]) % 24 == 15  # 23:00 local
    assert len(hours) == 22
