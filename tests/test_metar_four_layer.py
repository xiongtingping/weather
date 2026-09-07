"""The four layers, and the property that lets each of them be priced.

Every layer is written so that switching it off recovers the layer below
exactly.  That is not a nicety: it is what makes the acceptance arms the
production code path with one constant moved, rather than a second
implementation of it standing in for one.  An arm that is a reimplementation
measures the reimplementation -- the ordinal-zero falsy trap of 2026-08-26 is
the standing case in this repository -- so these tests pin the exactness.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.metar_four_layer import (  # noqa: E402
    apply_phase_split,
    compile_four_layer_posterior,
    feature_row,
    reanchor_shift_c,
    translate_distribution,
)

BUCKETS = ["28°C", "29°C", "30°C", "31°C", "32°C or higher"]
ORDINALS = {name: index for index, name in enumerate(BUCKETS)}
CENTERS = {
    "28°C": 28.0, "29°C": 29.0, "30°C": 30.0, "31°C": 31.0,
    "32°C or higher": 32.0,
}
INTERVALS = {
    "28°C": [27.5, 28.5], "29°C": [28.5, 29.5], "30°C": [29.5, 30.5],
    "31°C": [30.5, 31.5], "32°C or higher": [31.5, None],
}
ROOT_PROBABILITIES = {
    "28°C": 0.1, "29°C": 0.2, "30°C": 0.4, "31°C": 0.2,
    "32°C or higher": 0.1,
}


def _features(**overrides):
    values = {
        "remaining_hours": 6.0,
        "drawdown_from_high_c": 0.0,
        "momentum_c_per_hour": 0.2,
        "cloud_cover_fraction": 0.3,
        "precipitation_present": False,
        "dewpoint_depression_c": 8.0,
        "wind_speed_mps": 3.0,
        "warming_so_far_c": 4.0,
        "curve_shape_realized": 0.6,
        "regional_headroom_c": None,
        "regional_live_gap_c": None,
    }
    values.update(overrides)
    return feature_row(**values)


def _hazard(*, phase_intercept: float = 0.0, shape_weight: float = -1.0):
    return {
        "content_sha256": "h" * 64,
        "feature_fallbacks": {
            "remaining_hours": 6.0, "drawdown_from_high_c": 0.0,
            "momentum_c_per_hour": 0.0, "cloud_cover_fraction": 0.3,
            "precipitation_present": 0.0, "dewpoint_depression_c": 8.0,
            "wind_speed_mps": 3.0, "warming_so_far_c": 4.0,
            "curve_shape_realized": 0.5,
        },
        "phase": {
            "hours_edges": [0.0, 3.0, 6.0, 12.0, 1e9],
            "bin_intercepts": [-4.0, -2.0, phase_intercept, 2.0],
            "coefficients": {"cloud_cover_fraction": -0.8},
        },
        "continuation_full": {
            "rungs": [2, 3, 4],
            "rung_intercepts": [0.5, 0.0, -0.5],
            "coefficients": {"curve_shape_realized": shape_weight},
        },
        "continuation_phase_only": {
            "rungs": [2, 3, 4],
            "rung_intercepts": [0.5, 0.0, -0.5],
            "coefficients": {"cloud_cover_fraction": 0.0},
        },
    }


REANCHOR = {
    "content_sha256": "r" * 64,
    "hours_edges": [0.0, 6.0, 12.0, 1e9],
    "max_shift_c": 3.0,
    "minimum_bias_reports": 2,
    "bins": {
        "0": {"slope": 0.1}, "1": {"slope": 0.6}, "2": {"slope": 0.2},
    },
    "pooled": {"slope": 0.3},
}


def _compile(**overrides):
    arguments = {
        "root_probabilities": ROOT_PROBABILITIES,
        "bucket_ordinals": ORDINALS,
        "bucket_centers_c": CENTERS,
        "bucket_intervals_c": INTERVALS,
        "hard_excluded_buckets": ["28°C"],
        "running_high_c": 29.0,
        "running_high_bucket": "29°C",
        "features": _features(),
        "hazard_artifact": _hazard(),
        "reanchor_artifact": REANCHOR,
        "bias_recent_c": 1.5,
        "bias_report_count": 4,
    }
    arguments.update(overrides)
    return compile_four_layer_posterior(**arguments)


def test_all_three_layers_off_is_the_root_under_the_hard_bound() -> None:
    """With nothing switched on the chain is what production serves today."""

    posterior, receipts = _compile(
        reanchor_enabled=False, remaining_enabled=False, phase_enabled=False
    )
    survivors = {
        bucket: value for bucket, value in ROOT_PROBABILITIES.items()
        if bucket != "28°C"
    }
    total = sum(survivors.values())
    for bucket, value in survivors.items():
        assert posterior[bucket] == pytest.approx(value / total)
    assert posterior["28°C"] == 0.0
    assert receipts["phase"]["state"] == "DISABLED"
    assert receipts["reanchor"]["state"] == "DISABLED"
    assert receipts["remaining_warming"]["state"] == "DISABLED"


def test_a_zero_shift_returns_the_prior_bit_for_bit() -> None:
    """The ablation arm has to be exact, not merely close."""

    unchanged = translate_distribution(
        ROOT_PROBABILITIES, bucket_intervals_c=INTERVALS, shift_c=0.0
    )
    assert unchanged == {
        bucket: float(value) for bucket, value in ROOT_PROBABILITIES.items()
    }


def test_a_translation_moves_mass_up_and_keeps_all_of_it() -> None:
    """Mass shifted off the ladder joins the edge bucket it left through.

    The open-ended bucket is the ladder's statement about everything beyond
    it, so dropping that mass would quietly renormalise the distribution
    toward its middle.
    """

    shifted = translate_distribution(
        ROOT_PROBABILITIES, bucket_intervals_c=INTERVALS, shift_c=1.0
    )
    assert sum(shifted.values()) == pytest.approx(1.0)
    assert shifted["32°C or higher"] > ROOT_PROBABILITIES["32°C or higher"]
    assert shifted["28°C"] < ROOT_PROBABILITIES["28°C"]
    # A whole-degree shift on whole-degree buckets is a clean relabelling.
    assert shifted["30°C"] == pytest.approx(ROOT_PROBABILITIES["29°C"])


def test_the_reanchor_declines_rather_than_guessing() -> None:
    """No curve, too few reports, or a missing bin: no shift, and a reason.

    Declining is a first-class outcome -- the execution gate has to tell a
    layer that declined from a layer that was absent.
    """

    assert reanchor_shift_c(
        bias_recent_c=None, bias_report_count=9, remaining_hours=6.0,
        artifact=REANCHOR,
    ) == (0.0, "NO_CURVE_BIAS")
    assert reanchor_shift_c(
        bias_recent_c=2.0, bias_report_count=1, remaining_hours=6.0,
        artifact=REANCHOR,
    ) == (0.0, "BIAS_REPORTS_INSUFFICIENT")
    shift, state = reanchor_shift_c(
        bias_recent_c=2.0, bias_report_count=4, remaining_hours=7.0,
        artifact=REANCHOR,
    )
    assert state == "APPLIED"
    assert shift == pytest.approx(1.2)


def test_the_shift_is_capped_both_ways() -> None:
    for bias in (40.0, -40.0):
        shift, _ = reanchor_shift_c(
            bias_recent_c=bias, bias_report_count=4, remaining_hours=7.0,
            artifact=REANCHOR,
        )
        assert abs(shift) == pytest.approx(REANCHOR["max_shift_c"])


def test_the_phase_split_sets_the_mass_above_the_high_and_nothing_else() -> None:
    """One number moves; the shape inside each side is untouched."""

    base = {
        "28°C": 0.0, "29°C": 0.5, "30°C": 0.3, "31°C": 0.15,
        "32°C or higher": 0.05,
    }
    split, state = apply_phase_split(
        base, bucket_ordinals=ORDINALS, running_high_bucket="29°C",
        rise_probability=0.8,
    )
    assert state == "APPLIED"
    above = sum(
        value for bucket, value in split.items() if ORDINALS[bucket] > 1
    )
    assert above == pytest.approx(0.8)
    # Ratios within the upper side are preserved exactly.
    assert split["30°C"] / split["31°C"] == pytest.approx(0.3 / 0.15)


def test_the_phase_split_declines_on_a_degenerate_split() -> None:
    """At zero or one there is no ratio to rescale without inventing support."""

    nothing_above = {
        "28°C": 0.0, "29°C": 1.0, "30°C": 0.0, "31°C": 0.0,
        "32°C or higher": 0.0,
    }
    result, state = apply_phase_split(
        nothing_above, bucket_ordinals=ORDINALS, running_high_bucket="29°C",
        rise_probability=0.9,
    )
    assert state == "SPLIT_DEGENERATE"
    assert result == nothing_above


def test_no_layer_may_revive_a_bucket_the_observation_killed() -> None:
    """The hard bound is the only layer entitled to write a zero."""

    posterior, _ = _compile()
    assert posterior["28°C"] == 0.0
    assert sum(posterior.values()) == pytest.approx(1.0)


def test_the_phase_layer_reads_no_model_quantity() -> None:
    """Rung one is model-free, so the curve's shape cannot move the split."""

    left, left_receipts = _compile(
        features=_features(curve_shape_realized=0.1)
    )
    right, right_receipts = _compile(
        features=_features(curve_shape_realized=0.95)
    )
    assert left_receipts["phase"]["rise_probability"] == pytest.approx(
        right_receipts["phase"]["rise_probability"]
    )
    # The shape does reach the remaining-warming layer, which is the point of
    # keeping the two apart rather than fitting one model.
    assert left_receipts["remaining_warming"][
        "likelihood_ratio_by_bucket"
    ] != right_receipts["remaining_warming"]["likelihood_ratio_by_bucket"]


def test_buckets_at_or_below_the_high_carry_no_likelihood_ratio() -> None:
    """The chain is conditional on the day rising, so it says nothing there."""

    _, receipts = _compile()
    ratios = receipts["remaining_warming"]["likelihood_ratio_by_bucket"]
    assert ratios["28°C"] == 1.0
    assert ratios["29°C"] == 1.0
    assert any(value != 1.0 for value in ratios.values())


def test_an_empty_support_is_refused_not_renormalised() -> None:
    with pytest.raises(ContractError, match="posterior_zero_mass"):
        _compile(hard_excluded_buckets=list(BUCKETS))
