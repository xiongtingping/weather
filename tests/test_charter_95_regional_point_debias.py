"""The regional models' bias calibration, fitted the way the global models' is.

User ruling 2026-09-18: "把所有区域模型也做偏差校准（和全球模型方式一样）".
Three things are load bearing here.  The fit IS the global layer's -- the
same fitting function, the same resolver, the same window, half-life,
terciles and minimum -- so the two cannot drift.  Every regional lane is
fitted, CMA included, every day.  And, by the global layer's own rule that a
calibration serves only once it beats the incumbent out of sample, both
places a regional curve could be corrected in P1 stay switched off: each was
measured, and each read worse, because P1 compares the curve against the
day's own observations and they have already absorbed the offset.
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target import native_ensemble_oos as O
from bayes_basket_target import regional_path_prior as R
from bayes_basket_target import regional_point_debias as D
from bayes_basket_target import regional_point_lane as LANE

ROOT = Path(__file__).resolve().parents[1]


def _run(system, station, day, fx, *, cycle=0, lead=-8.0, complete=1,
         extraction="x"):
    return {
        "ensemble_system_id": system, "station_id": station,
        "target_local_date": day, "run_init_utc": f"{day}T{cycle:02d}:00:00Z",
        "run_cycle_hour": cycle, "lead_hours_to_local_day_start": lead,
        "forecast_tmax_c": fx, "remaining_peak_complete": complete,
        "extraction_version": extraction,
    }


def _days(start: str, count: int) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


DAYS = _days("2026-09-01", 10)
# MSM at RCSS runs 3C cold at 00Z and 1C cold at 06Z, every day.
RUNS = (
    [_run("JMA_MSM_POINT", "RCSS", d, 30.0 + i * 0.1, cycle=0) for i, d in enumerate(DAYS)]
    + [_run("JMA_MSM_POINT", "RCSS", d, 32.0 + i * 0.1, cycle=6, lead=-14.0) for i, d in enumerate(DAYS)]
)
TRUTH = {("RCSS", d): 33.0 + i * 0.1 for i, d in enumerate(DAYS)}


def test_the_fit_is_the_global_layers_own_function() -> None:
    """Not a copy that resembles it: the same function, on the same cases."""

    fitted = D.fit_calibration(RUNS, TRUTH, before_date="2026-09-11")
    scope = fitted["systems"]["JMA_MSM_POINT"]["stations"]["RCSS"]
    cycle_00 = scope["cycles"]["00|intraday"]
    direct = O._fit_parameters([
        {"values": [30.0 + i * 0.1], "actual_tmax_c": 33.0 + i * 0.1,
         "event_date": d}
        for i, d in enumerate(DAYS)
    ])
    for key in ("bias_c", "bias_bin_cuts_c", "bias_bin_shifts_c",
                "train_residual_sigma_c", "train_n_effective", "correction_form"):
        assert cycle_00[key] == direct[key]
    assert cycle_00["bias_c"] == pytest.approx(3.0)
    assert scope["cycles"]["06|intraday"]["bias_c"] == pytest.approx(1.0)
    # The station scope: one case per day, the day's runs as a lagged
    # ensemble -- 30.x and 32.x average 31.x against a 33.x truth, 2C cold.
    assert scope["station"]["bias_c"] == pytest.approx(2.0)
    assert scope["station"]["train_days"] == 10
    assert "climate_samples_c" not in cycle_00, "the retired blend's bytes stay out"


def test_the_constants_are_the_global_ones() -> None:
    assert D.MIN_TRAIN_DAYS == O.POINT_FORECAST_MIN_TRAIN_DAYS
    assert D.WINDOW_DAYS == O.ROLLING_TRAIN_WINDOW_DAYS


def test_every_regional_lane_is_calibrated() -> None:
    assert D.CALIBRATED_SYSTEMS == frozenset(LANE.SYSTEM_IDS.values())
    assert "CMA_GRAPES_GLOBAL_POINT" in D.CALIBRATED_SYSTEMS
    assert "NCEP_HRRR_POINT" in D.CALIBRATED_SYSTEMS, (
        "the first fit held HRRR out because shrinking its opposite-signed "
        "station offsets toward the lane pool destroyed them; the global "
        "form never pools across stations"
    )


def test_only_strictly_earlier_days_inside_the_window_train() -> None:
    fitted = D.fit_calibration(RUNS, TRUTH, before_date="2026-09-08")
    scope = fitted["systems"]["JMA_MSM_POINT"]["stations"]["RCSS"]["station"]
    assert scope["fit_through_date"] == "2026-09-07"
    assert scope["train_days"] == 7
    late = D.fit_calibration(RUNS, TRUTH, before_date="2026-11-09")
    assert late["systems"] == {}, "sixty days on, every case has aged out"


def test_five_days_or_the_station_scope_or_silence() -> None:
    thin = RUNS + [
        _run("JMA_MSM_POINT", "RCSS", d, 20.0, cycle=12, lead=4.0)
        for d in DAYS[:4]
    ]
    fitted = D.fit_calibration(thin, TRUTH, before_date="2026-09-11")
    scope = fitted["systems"]["JMA_MSM_POINT"]["stations"]["RCSS"]
    assert "12|h00_12" not in scope["cycles"], "four days is not a scope"
    served = D.resolve_correction(
        fitted, system_id="JMA_MSM_POINT", station_id="RCSS",
        run_init_utc="2026-09-11T12:00:00Z", lead_hours_to_local_day_start=4.0,
        forecast_level_c=31.0,
    )
    assert served["source"] == "station"
    cycle = D.resolve_correction(
        fitted, system_id="JMA_MSM_POINT", station_id="RCSS",
        run_init_utc="2026-09-11T00:00:00Z", lead_hours_to_local_day_start=-8.0,
        forecast_level_c=31.0,
    )
    assert cycle["source"] == "cycle_lead" and cycle["bias_c"] == pytest.approx(3.0)
    early = D.fit_calibration(RUNS, TRUTH, before_date="2026-09-05")
    assert early["systems"] == {}, "four settled days calibrate nothing"


def test_a_lane_is_never_corrected_from_another_lanes_residuals() -> None:
    fitted = D.fit_calibration(RUNS, TRUTH, before_date="2026-09-11")
    for system, station in (("NCEP_NBM_POINT", "RCSS"), ("JMA_MSM_POINT", "RJTT")):
        assert D.resolve_correction(
            fitted, system_id=system, station_id=station,
            run_init_utc="2026-09-11T00:00:00Z",
            lead_hours_to_local_day_start=-8.0, forecast_level_c=31.0,
        ) is None


def test_a_run_that_missed_its_peak_window_never_trains() -> None:
    broken = [dict(run, remaining_peak_complete=0, forecast_tmax_c=10.0)
              for run in RUNS]
    assert D.fit_calibration(broken, TRUTH, before_date="2026-09-11")["systems"] == {}


def test_cma_rows_with_an_inferred_cycle_train_the_station_only() -> None:
    cma = [
        _run("CMA_GRAPES_GLOBAL_POINT", "ZUUU", d, 30.0, cycle=12, lead=4.0,
             extraction="cma_grapes_global_open_meteo_derived_cycle_v1")
        for d in DAYS
    ]
    truth = {("ZUUU", d): 31.0 for d in DAYS}
    scope = D.fit_calibration(cma, truth, before_date="2026-09-11")[
        "systems"]["CMA_GRAPES_GLOBAL_POINT"]["stations"]["ZUUU"]
    assert scope["cycles"] == {}
    assert scope["station"]["bias_c"] == pytest.approx(1.0)


def test_an_absurd_shift_is_refused() -> None:
    runs = [_run("JMA_MSM_POINT", "RCSS", d, 10.0) for d in DAYS]
    fitted = D.fit_calibration(runs, TRUTH, before_date="2026-09-11")
    assert D.resolve_correction(
        fitted, system_id="JMA_MSM_POINT", station_id="RCSS",
        run_init_utc="2026-09-11T00:00:00Z", lead_hours_to_local_day_start=-8.0,
        forecast_level_c=10.0,
    ) is None


# ---------------------------------------------------------------------------
# serving: both arms, both off by measurement
# ---------------------------------------------------------------------------

FITTED = D.fit_calibration(RUNS, TRUTH, before_date="2026-09-11")
# Curve peak 33.2 at 05:00Z: ahead of a 04:30Z clock, behind a 05:30Z one.
CURVE_RUN = {
    "ensemble_system_id": "JMA_MSM_POINT", "run_init_utc": "2026-09-11T00:00:00Z",
    "archive_query_at_utc": "2026-09-11T01:00:00Z",
    "knowable_at_utc": "2026-09-11T01:00:00Z",
    "lead_hours_to_local_day_start": -8.0, "forecast_tmax_c": 33.2,
    "hourly_c": {"2026-09-11T04:00:00Z": 32.0, "2026-09-11T05:00:00Z": 33.2,
                 "2026-09-11T07:00:00Z": 31.0},
}


def test_the_regression_arm_raises_only_a_peak_still_ahead() -> None:
    kwargs = dict(running_high_c=31.5, latest_observed_c=31.4,
                  station_id="RCSS", calibration=FITTED, calibrate=True)
    ahead = R.regional_features_at(
        [CURVE_RUN], as_of_utc="2026-09-11T04:30:00Z", **kwargs)
    assert ahead["regional_headroom_c"] == pytest.approx(33.2 + 3.0 - 31.5)
    assert ahead["regional_calibration_bias_c"] == pytest.approx(3.0)
    assert ahead["regional_live_gap_c"] == pytest.approx(31.4 - 32.0), (
        "the live gap is today's error at this hour, and stays raw"
    )
    behind = R.regional_features_at(
        [CURVE_RUN], as_of_utc="2026-09-11T05:30:00Z", **kwargs)
    assert behind["regional_headroom_c"] == pytest.approx(31.0 - 31.5)
    assert behind["regional_calibration_bias_c"] is None
    unfitted = R.regional_features_at(
        [dict(CURVE_RUN, ensemble_system_id="NCEP_NBM_POINT")],
        as_of_utc="2026-09-11T04:30:00Z", **kwargs)
    assert unfitted["regional_headroom_c"] == pytest.approx(33.2 - 31.5), (
        "a run with no fitted scope is read raw, not dropped"
    )


def test_the_regression_arm_off_is_the_raw_feature_byte_for_byte() -> None:
    raw = R.regional_features_at(
        [CURVE_RUN], as_of_utc="2026-09-11T04:30:00Z", running_high_c=31.5,
        latest_observed_c=31.4, station_id="RCSS", calibration=FITTED,
        calibrate=False)
    legacy = R.regional_features_at(
        [CURVE_RUN], as_of_utc="2026-09-11T04:30:00Z", running_high_c=31.5,
        latest_observed_c=31.4)
    assert raw == legacy
    assert raw["regional_headroom_c"] == pytest.approx(1.7)
    assert raw["regional_calibration_bias_c"] is None


def test_both_arms_are_off_and_the_readings_ride_with_the_switches() -> None:
    """The global layer's rule: a calibration serves once it beats the incumbent.

    Path prior, 2026-09-07: debiasing before ``max(peak, running_high)``
    cost +0.212 nat.  Regression, 2026-09-18: raising the remaining peak
    before the headroom is read cost 0.0080 nat on the rows a regional run
    reaches (95% -0.014..-0.002), 0.046 where the shift is 1.5C or more.
    """

    assert D.ENABLED is False
    assert D.FEATURE_CALIBRATION is False
    assert D.SCALE == 1.0
    source = (ROOT / "bayes_basket_target" / "regional_point_debias.py").read_text(
        encoding="utf-8")
    assert "+0.212 nat" in source
    assert "-0.0080 [-0.014,-0.002]" in source


def test_every_feature_caller_hands_in_the_calibration() -> None:
    """Production, the sample builder: the arm cannot reach one and miss the other."""

    def keywords(path: Path, function: str) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                for call in ast.walk(node):
                    if (isinstance(call, ast.Call)
                            and getattr(call.func, "id", "") == "regional_features_at"):
                        found |= {k.arg for k in call.keywords if k.arg}
        return found

    compiler = keywords(
        ROOT / "bayes_basket_target" / "p1_compiler.py", "_four_layer_inputs_for_step")
    builder = keywords(
        ROOT / "scripts" / "build_metar_phase_hazard_samples.py", "build_station_day")
    assert {"station_id", "calibration"} <= compiler
    assert {"station_id", "calibration", "calibrate"} <= builder
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import build_metar_phase_hazard_samples as B  # noqa: PLC0415

    assert B.regional_calibration_replay is D.replay_curve, (
        "a sample reads the calibration its own morning could have known"
    )
    assert B.FEATURE_CALIBRATION is D.FEATURE_CALIBRATION


def test_the_path_prior_arm_stops_where_the_curve_stops_claiming_warming() -> None:
    correction = {"bias_c": 1.87, "source": "station", "scope": "station",
                  "train_days": 20, "fit_system_id": "JMA_MSM_POINT"}
    ahead, note = D.apply_remaining_peak_debias(
        33.2, curve_peak_c=33.2, correction=correction, enabled=True)
    assert ahead == pytest.approx(35.07)
    assert note["regional_point_debias_applied"] == "true"
    behind, note = D.apply_remaining_peak_debias(
        31.0, curve_peak_c=33.2, correction=correction, enabled=True)
    assert behind == pytest.approx(31.0)
    assert note["regional_point_debias_skip_reason"] == "PEAK_BEHIND"
    missing, note = D.apply_remaining_peak_debias(
        33.2, curve_peak_c=33.2, correction=None, enabled=True)
    assert note["regional_point_debias_skip_reason"] == "NO_FITTED_OFFSET"


PATH_ARTIFACT = {
    "schema": R.SCHEMA,
    "cells": {str(i): {"hours_from": 0.0, "hours_to": 1e9, "bias_c": 0.0,
                       "sigma_c": 0.9, "sample_n": 200, "backed_off": False}
              for i in range(10)},
    "stations": {}, "systems": {},
}


def test_the_path_prior_arm_reaches_the_fusion_through_the_one_seam() -> None:
    kwargs = dict(
        station_id="RCSS", event_date="2026-09-11",
        settlement_timezone="Asia/Taipei", artifact=PATH_ARTIFACT,
        systems={"JMA_MSM_POINT"}, debias_artifact=FITTED,
    )
    on = R.resolve_serving_input(
        [CURVE_RUN], as_of_utc="2026-09-11T04:30:00Z", debias_enabled=True, **kwargs)
    assert on["remaining_peak_c"] == pytest.approx(36.2)
    assert on["lineage"]["regional_path_prior_remaining_peak_raw_c"] == "33.2000"
    assert on["lineage"]["regional_point_debias_source"] == "cycle_lead"
    off = R.resolve_serving_input(
        [CURVE_RUN], as_of_utc="2026-09-11T04:30:00Z", debias_enabled=False, **kwargs)
    assert off["remaining_peak_c"] == pytest.approx(33.2)
    assert not [k for k in off["lineage"] if k.startswith("regional_point_debias")], (
        "a switched-off layer leaves the lineage it produced before it existed"
    )
    hrrr_run = dict(CURVE_RUN, ensemble_system_id="NCEP_HRRR_POINT")
    hrrr = R.resolve_serving_input(
        [hrrr_run], as_of_utc="2026-09-11T04:30:00Z", debias_enabled=True,
        **{**kwargs, "systems": {"NCEP_HRRR_POINT"}})
    assert hrrr["lineage"]["regional_point_debias_skip_reason"] == "NO_FITTED_OFFSET", (
        "the path prior arm keeps its own admission list"
    )


def test_the_residual_table_is_fitted_on_the_input_production_serves() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import fit_regional_path_prior as F  # noqa: PLC0415

    assert F.DEBIAS_ENABLED is D.ENABLED
    sample = {
        "system_id": "JMA_MSM_POINT", "station_id": "RCSS",
        "event_date": "2026-09-11", "hours_left": 7.0, "raw_peak_c": 33.2,
        "curve_peak_c": 33.2, "running_c": 30.0, "actual_c": 34.0,
        "run_age_hours": 4.5, "run_init_utc": "2026-09-11T00:00:00Z",
        "lead_hours_to_local_day_start": -8.0,
    }
    assert F.anchored([sample], None)[0]["error_c"] == pytest.approx(0.8)
    assert F.anchored([sample], FITTED)[0]["error_c"] == pytest.approx(34.0 - 36.2)


def test_production_and_the_ledger_price_the_same_curve() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import paper_trading_ledger as L  # noqa: PLC0415

    assert L.regional_point_debias is D
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert "debias=%s" in source
    assert "debias_artifact=regional_point_debias.replay_curve(event_date)" in source
    worker = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(
        encoding="utf-8")
    assert "load_regional_point_debias_artifact()" in worker


def test_the_daily_refit_keeps_the_calibration_current() -> None:
    sweep = (ROOT / "scripts" / "calibration_refit_sweep.sh").read_text(encoding="utf-8")
    assert "scripts/fit_regional_point_debias.py" in sweep


def test_the_fitted_artifact_is_on_disk_and_covers_every_lane() -> None:
    payload = D.load_artifact()
    assert payload is not None, "the fit is a deliverable, not a scratch run"
    assert payload["rule_version"] == D.RULE_VERSION
    assert set(payload["systems"]) == set(D.CALIBRATED_SYSTEMS)
    assert "ZUUU" in payload["systems"]["CMA_GRAPES_GLOBAL_POINT"]["stations"]
    table = D.load_replay_table()
    assert table is not None and table["dates"]
    first = min(table["dates"])
    assert D.replay_curve(first) is not None
    json.dumps(payload)
