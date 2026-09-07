"""Charter 95: the single-system correction, fitted per station, before fusion.

Charter 95 wrote the correction down as a plan and marked it 待度量后落地.
Charter 99 then fused the raw curve.  This is the plan landing -- built,
fitted, wired through the one function every caller reaches the fusion by --
and the measurement that decided it does not serve.  Both halves are load
bearing: the layer has to exist for the reading to mean anything, and the
switch has to default off for production to keep the envelope the reading
says is better.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bayes_basket_target import regional_path_prior as R
from bayes_basket_target import regional_point_debias as D

ROOT = Path(__file__).resolve().parents[1]

ARTIFACT = {
    "schema": D.SCHEMA,
    "rule_version": D.RULE_VERSION,
    "systems": {
        "JMA_MSM_POINT": {
            "sample_n": 981,
            "pooled_bias_c": 1.46,
            "stations": {
                "RCSS": {"bias_c": 1.87, "own_bias_c": 2.04, "sample_n": 242,
                         "lead_cells": {"0": {"hours_from": 0.0, "hours_to": 3.0,
                                              "bias_c": 1.71, "sample_n": 130}}},
                # Thin: shrinkage should pull it most of the way to the lane.
                "RJTT": {"bias_c": 1.40, "own_bias_c": 0.20, "sample_n": 12,
                         "lead_cells": {}},
            },
        },
        "NCEP_HRRR_POINT": {
            "sample_n": 3287, "pooled_bias_c": 0.48,
            "stations": {"KSFO": {"bias_c": 2.25, "own_bias_c": 2.74,
                                  "sample_n": 362, "lead_cells": {}}},
        },
        "THIN_LANE": {"sample_n": 4, "pooled_bias_c": 9.0, "stations": {}},
        "ABSURD_LANE": {
            "sample_n": 400, "pooled_bias_c": 0.0,
            "stations": {"XXXX": {"bias_c": 7.5, "own_bias_c": 7.5,
                                  "sample_n": 400, "lead_cells": {}}},
        },
    },
}
# Curve peak 33.2 at 20:00Z: ahead of a 19:30Z clock, behind a 20:30Z one.
RUN = {
    "ensemble_system_id": "JMA_MSM_POINT", "run_init_utc": "2026-08-23T18:00:00Z",
    "archive_query_at_utc": "2026-08-23T19:00:00Z",
    "hourly_c": {"2026-08-23T20:00:00Z": 33.2, "2026-08-23T22:00:00Z": 31.0},
}
PATH_ARTIFACT = {
    "schema": R.SCHEMA,
    "cells": {str(i): {"hours_from": 0.0, "hours_to": 1e9, "bias_c": 0.0,
                       "sigma_c": 0.9, "sample_n": 200, "backed_off": False}
              for i in range(10)},
    "stations": {}, "systems": {},
}


def test_the_correction_is_the_station_offset_shrunk_toward_its_own_lane() -> None:
    thick = D.resolve_correction(
        ARTIFACT, system_id="JMA_MSM_POINT", station_id="RCSS", run_age_hours=1.5)
    assert thick["bias_c"] == pytest.approx(1.87)
    assert thick["source"] == "station"
    thin = D.resolve_correction(
        ARTIFACT, system_id="JMA_MSM_POINT", station_id="RJTT", run_age_hours=1.5)
    assert thin["bias_c"] == pytest.approx(1.40), (
        "twelve samples buys a station almost none of its own offset"
    )
    unknown = D.resolve_correction(
        ARTIFACT, system_id="JMA_MSM_POINT", station_id="ZZZZ", run_age_hours=1.5)
    assert unknown["bias_c"] == pytest.approx(1.46) and unknown["source"] == "pooled"


def test_shrinkage_is_the_lane_until_the_station_has_paid_for_itself() -> None:
    assert D.shrunk([], 1.46) == pytest.approx(1.46)
    assert D.shrunk([3.0] * D.SHRINK_K.__int__(), 1.0) == pytest.approx(2.0), (
        "n == K is exactly half way between the lane and the station"
    )
    assert D.shrunk([3.0] * 10_000, 1.0) == pytest.approx(3.0, abs=0.03)


def test_a_lane_is_never_corrected_from_another_lanes_residuals() -> None:
    """Charter 99 allows a borrowed dispersion; a borrowed level is different.

    A sigma says how sure any model of this kind is at this hour, and
    ICON-EU keeps no archive to fit one.  A level offset is the single
    number that is entirely about this model at this station -- borrowing it
    is how a lane inherits a bias it does not have.  Silence costs nothing
    here: the fusion simply serves the raw curve.
    """

    assert D.resolve_correction(
        ARTIFACT, system_id="DWD_ICON_EU_POINT", station_id="RCSS",
        run_age_hours=1.5) is None
    assert D.resolve_correction(
        ARTIFACT, system_id="THIN_LANE", station_id="RCSS",
        run_age_hours=1.5, systems={"THIN_LANE"}) is None, (
        "a lane with four samples has not measured anything"
    )
    assert D.resolve_correction(
        ARTIFACT, system_id="ABSURD_LANE", station_id="XXXX",
        run_age_hours=1.5, systems={"ABSURD_LANE"}) is None, (
        "7.5C is not a bias, and serving it would put the layer's whole "
        "weight on a bucket no evidence reached"
    )


def test_hrrr_is_out_by_ruling_and_the_gate_can_still_ask() -> None:
    assert "NCEP_HRRR_POINT" not in D.DEBIAS_SYSTEMS
    assert {"JMA_MSM_POINT", "NCEP_NBM_POINT"} <= D.DEBIAS_SYSTEMS
    assert D.resolve_correction(
        ARTIFACT, system_id="NCEP_HRRR_POINT", station_id="KSFO",
        run_age_hours=1.5) is None
    asked = D.resolve_correction(
        ARTIFACT, system_id="NCEP_HRRR_POINT", station_id="KSFO",
        run_age_hours=1.5, systems={"NCEP_HRRR_POINT"})
    assert asked["bias_c"] == pytest.approx(2.25), (
        "an admission list that could not be overridden would refuse the "
        "measurement the list is supposed to be made of"
    )


def test_the_correction_stops_where_the_curve_stops_claiming_warming() -> None:
    """Past the curve's own maximum the gap is arithmetic, not error.

    Pooled over every instant the residual reads +3.3C for the MSM, and
    almost none of that is bias: near local midnight the day's high is
    already behind and the remaining segment is legitimately low.  Split on
    whether the curve's peak is still ahead, the ahead half reads +1.46C and
    the behind half +3.91C.  Correcting the behind half would lift the peak,
    and the mass above the running high with it, exactly when the day has
    stopped warming.
    """

    correction = {"bias_c": 1.87, "source": "station", "sample_n": 242,
                  "fit_system_id": "JMA_MSM_POINT", "lead_bin": 0}
    ahead, note = D.apply_remaining_peak_debias(
        33.2, curve_peak_c=33.2, correction=correction, enabled=True)
    assert ahead == pytest.approx(35.07)
    assert note["regional_point_debias_applied"] == "true"
    assert note["regional_point_debias_raw_peak_c"] == "33.2000"
    behind, note = D.apply_remaining_peak_debias(
        31.0, curve_peak_c=33.2, correction=correction, enabled=True)
    assert behind == pytest.approx(31.0)
    assert note["regional_point_debias_skip_reason"] == "PEAK_BEHIND"
    missing, note = D.apply_remaining_peak_debias(
        33.2, curve_peak_c=33.2, correction=None, enabled=True)
    assert missing == pytest.approx(33.2)
    assert note["regional_point_debias_skip_reason"] == "NO_FITTED_OFFSET"


def test_the_layer_reaches_the_fusion_through_the_one_seam() -> None:
    kwargs = dict(
        # 08-24 in Taipei closes at 16:00Z that day, so a 19:30Z clock on
        # 08-23 is inside the day the run is forecasting.
        station_id="RCSS", event_date="2026-08-24",
        settlement_timezone="Asia/Taipei", artifact=PATH_ARTIFACT,
        systems={"JMA_MSM_POINT"}, debias_artifact=ARTIFACT,
    )
    on = R.resolve_serving_input(
        [RUN], as_of_utc="2026-08-23T19:30:00Z", debias_enabled=True, **kwargs)
    assert on["remaining_peak_c"] == pytest.approx(35.07)
    assert on["lineage"]["regional_path_prior_remaining_peak_raw_c"] == "33.2000"
    assert on["lineage"]["regional_point_debias_applied"] == "true"
    assert on["lineage"]["regional_point_debias_fit_system_id"] == "JMA_MSM_POINT"
    off = R.resolve_serving_input(
        [RUN], as_of_utc="2026-08-23T19:30:00Z", debias_enabled=False, **kwargs)
    assert off["remaining_peak_c"] == pytest.approx(33.2)
    assert not [k for k in off["lineage"] if k.startswith("regional_point_debias")], (
        "a switched-off layer leaves the lineage it produced before it existed"
    )


def test_production_serves_the_raw_curve_because_that_is_what_measured_better() -> None:
    """The reading that set the default, kept where it cannot drift silently.

    Measured on the TOO_EARLY window -- where the served vector IS the
    layer's input, so the arms differ by exactly the switch -- debiasing the
    MSM's curve costs +0.212 nat against fusing it raw, 95% CI +0.024 to
    +0.400 over 46 station-days, argmax 61.8% -> 57.9%.  The fusion already
    anchors on ``max(remaining_peak, running_high)``, which is a debias made
    of today's observations rather than a fitted constant, and it leaves the
    anchor at -0.10C with MAE 0.621; this correction in front of it lifts
    that to +0.554C and 0.825.
    """

    assert D.ENABLED is False, (
        "charter 95's correction is built and fitted, not served"
    )
    assert D.SCALE == 1.0, "the strength knob is only meaningful once it is on"
    source = (ROOT / "bayes_basket_target" / "regional_point_debias.py").read_text(
        encoding="utf-8")
    assert "+0.212 nat" in source, "the reading that set the default rides with it"


def test_the_residual_table_is_fitted_on_the_input_production_serves() -> None:
    """Debias, then fit the residual of what is left -- or neither.

    A residual table fitted on debiased peaks and served against raw ones is
    a correction applied to a quantity it never saw, and the two layers would
    each remove a share of the same cold offset.  The fitter follows the
    serving switch so the pair cannot come apart.
    """

    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import fit_regional_path_prior as F  # noqa: PLC0415

    assert F.DEBIAS_ENABLED is D.ENABLED
    source = (ROOT / "scripts" / "fit_regional_path_prior.py").read_text(
        encoding="utf-8")
    assert "not DEBIAS_ENABLED" in source
    sample = {
        "system_id": "JMA_MSM_POINT", "station_id": "RCSS",
        "event_date": "2026-08-23", "hours_left": 7.0, "raw_peak_c": 33.2,
        "curve_peak_c": 33.2, "running_c": 30.0, "actual_c": 34.0,
        "run_age_hours": 1.5,
    }
    raw = F.anchored([sample], None)[0]["error_c"]
    assert raw == pytest.approx(0.8), "34.0 - max(30.0, 33.2)"
    debiased = F.anchored([sample], ARTIFACT)[0]["error_c"]
    assert debiased == pytest.approx(-1.07), "34.0 - max(30.0, 33.2 + 1.87)"


def test_production_and_the_ledger_price_the_same_curve() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import paper_trading_ledger as L  # noqa: PLC0415

    assert L.regional_point_debias is D
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert "debias=%s" in source, (
        "a book priced on a debiased curve is not the book priced on the raw "
        "one, and the two must not share a fingerprint"
    )
    assert "debias_artifact=regional_point_debias.replay_curve(event_date)" in source
    compiler = (ROOT / "bayes_basket_target" / "p1_compiler.py").read_text(encoding="utf-8")
    assert "debias_artifact=regional_debias_artifact," in compiler
    worker = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(
        encoding="utf-8")
    assert "load_regional_point_debias_artifact()" in worker


def test_the_fitted_artifact_is_on_disk_and_says_what_it_measured() -> None:
    path = D.DEFAULT_ARTIFACT
    assert path.exists(), "the measurement is a deliverable, not a scratch run"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == D.SCHEMA
    msm = payload["systems"]["JMA_MSM_POINT"]
    assert msm["pooled_bias_c"] > 1.0, "every regional lane measured cold"
    assert msm["debiased_mae_c"] < msm["raw_mae_c"], (
        "the correction is right about the model even though it is wrong "
        "about the path"
    )
    assert D.replay_curve("2026-09-06") is not None, (
        "the ledger reads the as-of table, fitted only on earlier days"
    )
