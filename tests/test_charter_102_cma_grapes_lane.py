"""Charter 102: CMA GRAPES Global captured for the Chinese stations.

Eight roster stations had no regional model at all.  This lane is their
first, and it is capture only -- the serving admission list is a separate
decision, taken on measurement, and this test pins that separation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from bayes_basket_target import cma_grapes_point_archive as CMA
from bayes_basket_target import regional_path_prior as R
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


def test_capture_is_registered_but_not_admitted_to_serving() -> None:
    """Charter 99's rule, applied to the newest lane on its first day.

    A lane exists so a system can be measured.  Admission is a separate
    decision and today only the MSM has passed it, so CMA rows must reach
    the table and no served distribution.
    """

    assert "CMA_GRAPES_GLOBAL_POINT" in REGIONAL_DETERMINISTIC_SYSTEM_IDS
    assert POINT_FORECAST_SYSTEMS["CMA_GRAPES_GLOBAL_POINT"]["provider_id"] == "CMA"
    assert LANE.SYSTEM_IDS["CMA"] == "CMA_GRAPES_GLOBAL_POINT"
    assert REGIONAL_MODELS_STAGE == "P1"
    assert "CMA_GRAPES_GLOBAL_POINT" not in R.SERVING_SYSTEMS


def test_a_cma_run_reaching_the_serving_input_is_refused_by_admission() -> None:
    """Even with a perfectly good row, the layer must not read it."""

    run = {
        "ensemble_system_id": "CMA_GRAPES_GLOBAL_POINT",
        "run_init_utc": "2026-09-07T00:00:00Z",
        "knowable_at_utc": "2026-09-07T04:00:00Z",
        "archive_query_at_utc": "2026-09-07T04:00:00Z",
        # Both instants are after the decision clock below and inside
        # Shanghai's 2026-09-07, which ends at 16:00Z.
        "hourly_c": {
            "2026-09-07T10:00:00Z": 31.0,
            "2026-09-07T12:00:00Z": 33.5,
        },
    }
    artifact = {
        "cells": {
            "3": {"hours_from": 6.0, "hours_to": 8.0, "bias_c": -0.1,
                  "sigma_c": 0.44, "sample_n": 200, "backed_off": False},
        },
        "stations": {},
    }
    kwargs = dict(
        station_id="ZUUU", event_date="2026-09-07",
        settlement_timezone="Asia/Shanghai",
        as_of_utc="2026-09-07T09:30:00Z", artifact=artifact,
    )
    assert R.resolve_serving_input([run], **kwargs) is None, (
        "captured, not admitted"
    )
    assert R.resolve_serving_input(
        [run], systems={"CMA_GRAPES_GLOBAL_POINT"}, **kwargs
    ) is not None, "the admission gate must still be able to score it"


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
