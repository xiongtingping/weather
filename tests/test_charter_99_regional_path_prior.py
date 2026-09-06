"""Charter 99: the regional curve stands in the ceiling's place at P1.

Operator ruling 2026-09-06 ("跳过度量直接接进 P1").  The ceiling answers "how
much mass belongs above the bucket already holding the high" from a pooled
clock rate and leaves "how far above" to the dawn model; a regional run
issued during the local day answers both from today's curve.
"""
from __future__ import annotations

import math

import pytest

from bayes_basket_target import regional_path_prior as R

ORDINALS = {"29°C or below": 0, "30°C": 1, "31°C": 2, "32°C": 3, "33°C or higher": 4}
UPPER = {"29°C or below": 29.5, "30°C": 30.5, "31°C": 31.5, "32°C": 32.5, "33°C or higher": None}
# The dawn model's shape: a峰 three rungs above the day's high.
POSTERIOR = {"29°C or below": 0.0, "30°C": 0.20, "31°C": 0.15, "32°C": 0.45, "33°C or higher": 0.20}
CELL = {"bias_c": 0.0, "sigma_c": 0.5, "source": "hours_cell", "sample_n": 150}


def test_the_predictive_collapses_the_lower_tail_onto_the_reported_high() -> None:
    out = R.predictive_probabilities(
        peak_c=30.2, sigma_c=0.5, ordinals=ORDINALS, upper_bounds_c=UPPER,
        running_high_bucket="30°C",
    )
    assert "29°C or below" not in out, "the day cannot end below what it reached"
    assert sum(out.values()) == pytest.approx(1.0)
    assert out["30°C"] > 0.7, "a peak of 30.2 with half a degree of spread mostly stays put"
    assert out["33°C or higher"] < 1e-4


def test_the_curve_takes_mass_off_a_dawn_peak_it_cannot_reach() -> None:
    """The day's high is 30.4; the curve tops out at 30.6; the model said 32."""

    blended, note = R.apply_regional_path_prior(
        POSTERIOR, ordinals=ORDINALS, upper_bounds_c=UPPER,
        running_high_bucket="30°C", running_high_c=30.4, remaining_peak_c=30.6,
        cell=CELL, weight=0.5, enabled=True,
    )
    assert note["regional_path_prior_applied"] == "true"
    assert float(note["regional_path_prior_mass_above_before"]) == pytest.approx(0.80)
    # Half weight means half the move: the model keeps an equal say, which is
    # what an unmeasured layer is entitled to.  At full weight the curve's own
    # 0.58 stands.
    assert float(note["regional_path_prior_mass_above_after"]) == pytest.approx(0.69, abs=0.01)
    _, full = R.apply_regional_path_prior(
        POSTERIOR, ordinals=ORDINALS, upper_bounds_c=UPPER,
        running_high_bucket="30°C", running_high_c=30.4, remaining_peak_c=30.6,
        cell=CELL, weight=1.0, enabled=True,
    )
    assert float(full["regional_path_prior_mass_above_after"]) == pytest.approx(0.58, abs=0.01)
    assert blended["30°C"] > POSTERIOR["30°C"]
    assert blended["32°C"] < POSTERIOR["32°C"]
    assert sum(blended.values()) == pytest.approx(1.0)
    assert note["regional_path_prior_rule"] == "regional_curve_remaining_peak_v2_per_system_cell"


def test_a_curve_that_says_it_will_climb_moves_mass_up() -> None:
    blended, note = R.apply_regional_path_prior(
        POSTERIOR, ordinals=ORDINALS, upper_bounds_c=UPPER,
        running_high_bucket="30°C", running_high_c=30.4, remaining_peak_c=32.2,
        cell=CELL, weight=0.5, enabled=True,
    )
    assert note["regional_path_prior_applied"] == "true"
    assert blended["32°C"] > POSTERIOR["32°C"]
    assert blended["30°C"] < POSTERIOR["30°C"]


def test_the_day_cannot_end_below_what_it_reached() -> None:
    """A curve whose remaining segment is colder than the reported high is
    floored at that high, not read as a forecast of cooling."""

    blended, _ = R.apply_regional_path_prior(
        POSTERIOR, ordinals=ORDINALS, upper_bounds_c=UPPER,
        running_high_bucket="30°C", running_high_c=30.4, remaining_peak_c=27.0,
        cell=CELL, weight=1.0, enabled=True,
    )
    assert blended["30°C"] > 0.55
    assert blended["29°C or below"] == pytest.approx(0.0)


def test_off_and_zero_weight_return_the_posterior_untouched() -> None:
    for kwargs, reason in (
        ({"enabled": False}, "DISABLED"),
        ({"enabled": True, "weight": 0.0}, "ZERO_WEIGHT"),
    ):
        out, note = R.apply_regional_path_prior(
            POSTERIOR, ordinals=ORDINALS, upper_bounds_c=UPPER,
            running_high_bucket="30°C", running_high_c=30.4,
            remaining_peak_c=32.0, cell=CELL, **kwargs,
        )
        assert out == POSTERIOR
        assert note["regional_path_prior_skip_reason"] == reason


ARTIFACT = {
    "schema": R.SCHEMA,
    "cells": {
        "3": {"hours_from": 6.0, "hours_to": 8.0, "bias_c": -0.1, "sigma_c": 0.44,
              "sample_n": 122, "backed_off": False},
        "9": {"hours_from": 24.0, "hours_to": 1e9, "bias_c": 0.0, "sigma_c": 1.0,
              "sample_n": 2, "backed_off": True},
    },
    "stations": {"KATL": {"bias_c": -0.4, "sample_n": 90}},
    # Charter 99, 2026-09-06: one system carries its own fit, the other does
    # not, so both halves of the borrow rule are visible in one fixture.
    "systems": {
        "NCEP_HRRR_POINT": {
            "cells": {
                "3": {"hours_from": 6.0, "hours_to": 8.0, "bias_c": -0.7,
                      "sigma_c": 0.61, "sample_n": 140, "backed_off": False},
            },
            "stations": {"KATL": {"bias_c": -0.9, "sample_n": 95}},
        },
    },
}
RUN = {
    "ensemble_system_id": "NCEP_HRRR_POINT", "run_init_utc": "2026-08-23T18:00:00Z",
    "archive_query_at_utc": "2026-08-23T19:00:00Z",
    "hourly_c": {"2026-08-23T20:00:00Z": 33.2, "2026-08-23T22:00:00Z": 31.0},
}


def test_the_station_bias_serves_where_the_station_earned_one() -> None:
    cell = R.resolve_cell(ARTIFACT, hours_left=7.0, station_id="KATL")
    assert cell["bias_c"] == -0.4 and cell["source"] == "pooled_hours_cell_station_bias"
    other = R.resolve_cell(ARTIFACT, hours_left=7.0, station_id="KMIA")
    assert other["bias_c"] == -0.1 and other["source"] == "pooled_hours_cell"
    assert R.resolve_cell(ARTIFACT, hours_left=30.0, station_id="KATL") is None, (
        "a backed-off cell is silence, not a pooled guess"
    )


def test_a_systems_own_cell_beats_the_pool_and_a_borrow_says_so() -> None:
    """Charter 99, 2026-09-06: whose errors the parameters were fitted on.

    The first artifact had no system dimension, and on 2026-09-06 it served
    seven European ICON-EU station-days from cells fitted on ten US
    HRRR/NBM stations -- a borrowed sigma is exactly the number that
    decides how much mass sits above the running high.  The borrow is
    still allowed, because ICON-EU keeps no archive to fit; what changed
    is that it can no longer happen silently.
    """

    own = R.resolve_cell(
        ARTIFACT, hours_left=7.0, station_id="KATL", system_id="NCEP_HRRR_POINT"
    )
    assert own["sigma_c"] == 0.61 and own["bias_c"] == -0.9
    assert own["source"] == "hours_cell_station_bias"
    assert own["fit_system_id"] == "NCEP_HRRR_POINT"

    borrowed = R.resolve_cell(
        ARTIFACT, hours_left=7.0, station_id="KATL", system_id="DWD_ICON_EU_POINT"
    )
    assert borrowed["sigma_c"] == 0.44, "no ICON-EU history: the pool serves"
    assert borrowed["source"] == "pooled_hours_cell_station_bias"
    assert borrowed["fit_system_id"] == R.POOLED_FIT_SYSTEM_ID

    # And the borrow reaches the envelope, not just the resolver.
    _, note = R.apply_regional_path_prior(
        {"30°C": 0.5, "31°C": 0.3, "32°C+": 0.2},
        ordinals={"30°C": 0, "31°C": 1, "32°C+": 2},
        upper_bounds_c={"30°C": 30.9, "31°C": 31.9, "32°C+": None},
        running_high_bucket="30°C", running_high_c=30.4,
        remaining_peak_c=31.2, cell=borrowed,
    )
    assert note["regional_path_prior_applied"] == "true"
    assert note["regional_path_prior_cell_fit_system_id"] == R.POOLED_FIT_SYSTEM_ID


def test_the_resolver_refuses_a_run_the_clock_could_not_know() -> None:
    kwargs = dict(
        station_id="KATL", event_date="2026-08-23",
        settlement_timezone="America/New_York", artifact=ARTIFACT,
    )
    early = R.resolve_serving_input([RUN], as_of_utc="2026-08-23T18:30:00Z", **kwargs)
    assert early is None, "archived at 19:00Z, not knowable at 18:30Z"
    served = R.resolve_serving_input([RUN], as_of_utc="2026-08-23T20:30:00Z", **kwargs)
    assert served["remaining_peak_c"] == 31.0, "only the curve after the clock counts"
    assert served["cell"]["source"] == "hours_cell_station_bias"
    assert served["cell"]["fit_system_id"] == "NCEP_HRRR_POINT", (
        "the serving run's own system decides whose cell applies"
    )
    assert served["lineage"]["regional_path_prior_system_id"] == "NCEP_HRRR_POINT"
    exhausted = R.resolve_serving_input([RUN], as_of_utc="2026-08-23T23:00:00Z", **kwargs)
    assert exhausted is None, "a curve with nothing left says nothing"


def test_a_day_inside_the_fit_is_refused_but_the_asof_replay_is_not() -> None:
    """Charter 99, 2026-09-06: the guard the ceiling has always had.

    Production serves from one fitted artifact.  Once a refit reaches an
    event date, serving that date reads a curve calibrated on its own
    outcome.  The ledger is unaffected: it reads the per-date as-of table,
    fitted only on strictly earlier days, which declares an empty
    trained_through precisely so this check cannot silence it.
    """

    kwargs = dict(
        station_id="KATL", settlement_timezone="America/New_York",
        as_of_utc="2026-08-23T20:30:00Z",
    )
    inside = dict(ARTIFACT, trained_through="2026-08-23")
    assert R.resolve_serving_input(
        [RUN], event_date="2026-08-23", artifact=inside, **kwargs
    ) is None, "the fit already saw this day's outcome"

    after = dict(ARTIFACT, trained_through="2026-08-22")
    assert R.resolve_serving_input(
        [RUN], event_date="2026-08-23", artifact=after, **kwargs
    ) is not None, "a day after the fit is what the artifact is for"

    replay = dict(ARTIFACT, trained_through="")
    assert R.resolve_serving_input(
        [RUN], event_date="2026-08-23", artifact=replay, **kwargs
    ) is not None, "the as-of table is leak-free by construction"


def test_an_old_run_is_not_todays_curve() -> None:
    stale = dict(RUN, run_init_utc="2026-08-22T18:00:00Z",
                 archive_query_at_utc="2026-08-22T19:00:00Z")
    assert R.resolve_serving_input(
        [stale], station_id="KATL", event_date="2026-08-23",
        as_of_utc="2026-08-23T20:30:00Z", settlement_timezone="America/New_York",
        artifact=ARTIFACT,
    ) is None


def test_production_and_the_ledger_read_one_layer() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as L  # noqa: PLC0415

    assert L.regional_path_prior is R
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert '"regional_path_prior": (' in source, "the layer is a rule of the book"
    materialization = (root / "bayes_basket_target" / "materialization.py").read_text(encoding="utf-8")
    assert "apply_regional_path_prior(" in materialization
    assert 'posterior_version: str = "p1_metar_trajectory_target_v8"' in materialization
    assert "if ceiling is not None and not regional_spoke:" in materialization, (
        "the curve stands in the ceiling's place, it does not double-correct"
    )
