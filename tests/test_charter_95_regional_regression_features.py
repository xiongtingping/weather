"""The regional curve's two features, read once for fit and serve.

Charter 95 step two put the regional curve inside the temperature path's
regression as two features instead of blending it into the finished
distribution (measured 2026-09-07: every metric improved on the clean
ablation).  Charter 150 (2026-09-19) kept exactly that shape and moved it:
the two features are covariates of P1's joint observation likelihood, the
only place the regional lanes reach P1.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target import regional_features as R

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


def test_the_feature_vector_makes_a_caller_say_none_rather_than_forget() -> None:
    signature = inspect.signature(OL.feature_vector)
    for name in ("regional_headroom_c", "regional_live_gap_c"):
        assert name in signature.parameters
        assert signature.parameters[name].default is inspect.Parameter.empty, (
            "a default is how a feature goes quietly missing on one side"
        )
    row = OL.feature_vector(
        remaining_hours=6.0, drawdown_from_high_c=0.0, momentum_c_per_hour=0.2,
        cloud_cover_fraction=0.3, precipitation_present=False,
        dewpoint_depression_c=8.0, wind_speed_mps=3.0, warming_so_far_c=4.0,
        curve_shape_realized=0.6, root_bias_recent_c=None,
        regional_headroom_c=2.5, regional_live_gap_c=None,
    )
    assert row["regional_headroom_c"] == 2.5
    assert row["regional_live_gap_c"] is None


def test_both_features_are_covariates_of_the_joint_likelihood() -> None:
    assert "regional_headroom_c" in OL.PHASE_FEATURES
    assert {"regional_headroom_c", "regional_live_gap_c"} <= set(OL.FULL_FEATURES)


def test_the_shipped_artifact_declares_the_features_it_was_fitted_with() -> None:
    payload = OL.load_artifact()
    if payload is None:
        pytest.skip("joint likelihood not fitted here")
    assert "regional_headroom_c" in payload["phase_features"]
    assert "regional_live_gap_c" in payload["full_features"]
    # Absent rows fall back to the fleet mean, not to zero.
    assert payload["feature_fallbacks"]["regional_headroom_c"] != 0.0


def test_the_joint_likelihood_computes_the_features_from_the_same_function() -> None:
    """A feature computed twice is a feature that will disagree.

    Charter 150: the four-layer shadow is gone; the serving feature builder
    is ``p1_compiler._observation_features_for_step`` and the sample builder
    calls the same ``regional_features_at``."""

    compiler = (ROOT / "bayes_basket_target" / "p1_compiler.py").read_text(
        encoding="utf-8")
    assert "regional_features_at(" in compiler
    assert "regional_runs=regional_runs," in compiler
    assert not (ROOT / "bayes_basket_target" / "four_layer_shadow.py").exists()
    builder = (ROOT / "scripts" / "build_metar_phase_hazard_samples.py").read_text(
        encoding="utf-8")
    assert "regional_features_at(" in builder, (
        "the sample builder and the serving builder must call one definition"
    )

