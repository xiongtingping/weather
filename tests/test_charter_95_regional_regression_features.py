"""Charter 95 step two: the regional curve joins the temperature path's regression.

Step one -- correct the curve, then blend it into the finished distribution --
was measured twice on 2026-09-07 and refused twice, because the fusion it fed
already anchors on ``max(remaining_peak, running_high)`` and that max is a
better debias than any fitted constant.  Step two puts the curve where there is
no such anchor to duplicate: inside the regression, as two features among the
clock and the reports, so the fit decides what the curve is worth instead of a
weight set by hand outside it.

Measured on the walk-forward gate, every day fitted only on strictly earlier
days, over 2948 samples a regional run actually reaches:

    arm             hit    +-1     log     Brier   centre MAE
    hard+clock     0.687  0.938  -0.811   0.392    0.577      (production today)
    four_layer     0.715  0.937  -0.731   0.363    0.513
    covered        0.697  0.901  -0.791   0.364    0.537      (same rows, no features)
    +regional      0.726  0.935  -0.747   0.342    0.477      (same rows, features)
    +regional_full 0.721  0.946  -0.699   0.345    0.477      (whole fleet, features)

``covered`` against ``+regional`` is the clean ablation -- one training set,
one difference -- and every metric improves.  ``+regional_full`` is what ships:
restricting the fit to covered rows costs more on the pre-peak subset than the
features gain there, so the features go in and the training set stays whole.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

from bayes_basket_target import regional_path_prior as R
from bayes_basket_target.metar_four_layer import feature_row

ROOT = Path(__file__).resolve().parents[1]
RUN = {
    "ensemble_system_id": "NCEP_NBM_POINT", "run_init_utc": "2026-08-25T18:00:00Z",
    "archive_query_at_utc": "2026-08-25T19:00:00Z",
    "knowable_at_utc": "2026-08-25T19:00:00Z",
    "hourly_c": {"2026-08-25T19:00:00Z": 30.0, "2026-08-25T20:00:00Z": 31.5,
                 "2026-08-25T22:00:00Z": 29.0},
}


def test_the_two_features_are_one_definition_shared_by_fit_and_serving() -> None:
    got = R.regional_features_at(
        [RUN], as_of_utc="2026-08-25T19:20:00Z",
        running_high_c=29.0, latest_observed_c=28.6,
    )
    # 31.5 is the highest the curve still claims; the day has reported 29.0.
    assert got["regional_headroom_c"] == pytest.approx(2.5)
    # The station reads 28.6 where this run says 30.0 for the hour in
    # progress: the model is running 1.4C warm today.
    assert got["regional_live_gap_c"] == pytest.approx(-1.4)
    assert got["regional_system_id"] == "NCEP_NBM_POINT"


def test_absent_is_none_and_never_zero() -> None:
    """Zero is a claim; absent is the absence of one.

    ``regional_headroom_c`` of zero says the model promises nothing more
    today, which on the fitted phase coefficient (+0.535 per degree) is a
    strong and specific statement.  Most of the 45-station fleet has no
    regional lane at all, and it must not be made to say that.
    """

    for kwargs in (
        dict(as_of_utc="2026-08-25T04:54:00Z"),   # run not issued yet
        dict(as_of_utc="2026-08-25T23:30:00Z"),   # curve exhausted
    ):
        got = R.regional_features_at(
            [RUN], running_high_c=29.0, latest_observed_c=28.6, **kwargs)
        assert got["regional_headroom_c"] is None
        assert got["regional_live_gap_c"] is None
    empty = R.regional_features_at(
        (), as_of_utc="2026-08-25T19:20:00Z",
        running_high_c=29.0, latest_observed_c=28.6)
    assert empty["regional_headroom_c"] is None
    # The live gap alone may be absent while the headroom is known: the lane
    # stores the local peak window, so a pre-dawn instant has a remaining
    # peak but no value for the hour in progress.
    got = R.regional_features_at(
        [RUN], as_of_utc="2026-08-25T21:10:00Z",
        running_high_c=29.0, latest_observed_c=None)
    assert got["regional_headroom_c"] is not None
    assert got["regional_live_gap_c"] is None


def test_the_feature_row_makes_a_caller_say_none_rather_than_forget() -> None:
    signature = inspect.signature(feature_row)
    for name in ("regional_headroom_c", "regional_live_gap_c"):
        assert name in signature.parameters
        assert signature.parameters[name].default is inspect.Parameter.empty, (
            "a default is how a feature goes quietly missing on one side"
        )
    row = feature_row(
        remaining_hours=6.0, drawdown_from_high_c=0.0, momentum_c_per_hour=0.2,
        cloud_cover_fraction=0.3, precipitation_present=False,
        dewpoint_depression_c=8.0, wind_speed_mps=3.0, warming_so_far_c=4.0,
        curve_shape_realized=0.6, regional_headroom_c=2.5,
        regional_live_gap_c=None,
    )
    assert row["regional_headroom_c"] == 2.5
    assert row["regional_live_gap_c"] is None


def test_the_fitter_separates_the_features_from_the_training_set() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import fit_metar_phase_hazard_artifact as F  # noqa: PLC0415

    assert F.REGIONAL_FEATURES == (
        "regional_headroom_c", "regional_live_gap_c")
    source = (ROOT / "scripts" / "fit_metar_phase_hazard_artifact.py").read_text(
        encoding="utf-8")
    assert "if args.covered_only:" in source, (
        "--regional must not imply --covered-only: the ablation and the "
        "shipping fit are different runs"
    )


def test_the_walk_forward_gate_scores_the_ablation_and_the_shipping_arm() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import evaluate_metar_four_layer_walk_forward as W  # noqa: PLC0415

    assert W.REGIONAL_ARMS == ("covered", "+regional", "+regional_full")
    assert "regional_headroom_c" in inspect.getsource(W.regional_artifacts)


def test_the_shipped_artifact_declares_the_features_it_was_fitted_with() -> None:
    path = (ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
            / "metar_phase_hazard_artifact.json")
    if not path.exists():
        pytest.skip("hazard artifact not fitted here")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "regional_headroom_c" in payload["phase_features"]
    assert "regional_headroom_c" in payload["full_features"]
    assert payload["covered_only"] is False, "the fit keeps the whole fleet"
    coefficient = payload["phase"]["coefficients"]["regional_headroom_c"]
    assert coefficient > 0.2, (
        "a degree the model still claims should raise the odds the day rises"
    )
    # Absent rows fall back to the fleet mean, not to zero.
    assert payload["feature_fallbacks"]["regional_headroom_c"] != 0.0


def test_the_shadow_computes_the_features_from_the_same_function() -> None:
    """A feature computed twice is a feature that will disagree."""

    compiler = (ROOT / "bayes_basket_target" / "p1_compiler.py").read_text(
        encoding="utf-8")
    assert "regional_features_at(" in compiler
    assert "regional_runs=regional_runs," in compiler
    shadow = (ROOT / "bayes_basket_target" / "four_layer_shadow.py").read_text(
        encoding="utf-8")
    assert "regional_headroom_c=regional_headroom_c," in shadow
    builder = (ROOT / "scripts" / "build_metar_phase_hazard_samples.py").read_text(
        encoding="utf-8")
    assert "regional_features_at(" in builder, (
        "the sample builder and the shadow must call one definition"
    )


def test_the_shadow_records_and_still_moves_no_probability() -> None:
    """On since 2026-09-07, and on is not the same as promoted.

    The chain now writes two lineage keys on every P1 it can compile, which is
    how thirty event days of evidence get made; it still multiplies into
    nothing, and the served distribution is what it was.  Promoting it to the
    distribution anyone trades is a separate ruling.
    """

    from bayes_basket_target import four_layer_shadow as S  # noqa: PLC0415

    assert S.SHADOW_ENABLED is True
    assert set(S.load_shadow_artifacts()) == {"hazard", "reanchor"}, (
        "a shadow swallows its own failures, so an artifact that will not "
        "load shows up only as a key that never appears"
    )
    materialization = (
        ROOT / "bayes_basket_target" / "materialization.py"
    ).read_text(encoding="utf-8")
    assert "p1_shadow" not in materialization, (
        "the shadow reaches the lineage, never the distribution"
    )
