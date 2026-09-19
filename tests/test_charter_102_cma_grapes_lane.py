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


def test_the_derived_cycle_is_the_one_already_published() -> None:
    """No run stamp reaches us, so the cycle is derived from the cadence."""

    # 09:30Z, four hours after the 06Z cycle: that is the newest one out.
    assert CMA.derived_run_init(
        datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)
    ) == datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)
    # 03:00Z is inside the 00Z cycle's publication lag, so the run in force
    # is the previous day's last one, not a cycle that is not there yet.
    assert CMA.derived_run_init(
        datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)
    ) == datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)


def test_backfill_is_refused_rather_than_guessed() -> None:
    """A row that cannot say when it became knowable is not written.

    The archive is reachable and its accuracy is ordinary, but its series
    is not resolved by run.  Stamping such a row early leaks the afternoon
    into the morning; stamping it late enough to be safe makes it useless
    for the same-day question.  So the lane refuses instead of choosing.
    """

    with pytest.raises(ContractError) as caught:
        LANE.capture_p1(
            None, product="CMA", now=datetime.now(timezone.utc),
            stations=CMA.CAPTURE_STATIONS, backfill=True,
        )
    assert "backfill_unsupported" in str(caught.value)


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
