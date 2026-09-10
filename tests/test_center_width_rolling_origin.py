"""The admission gate walks the origin instead of cutting once.

A single trailing window makes the verdict a property of which days land in
it, and this layer's gain changes sign by day.  Measured 2026-09-10 on P0's
own served cells, the same lane on the same ten days decided differently
depending only on where the last day fell:

    ends 09-09  forward 09-06..09-09  pick lambda 0.700  gain -0.0894  refuse
    ends 09-08  forward 09-05..09-08  pick lambda 0.725  gain -0.0407  refuse
    ends 09-07  forward 09-05..09-07  pick lambda 0.725  gain -0.0422  refuse
    ends 09-06  forward 09-04..09-06  pick lambda 0.725  gain +0.0054  ADMIT

Under a rolling origin each held-out day is scored by a fit that saw only the
days before it, so the estimate averages several independent fit-and-test
events.  The fold list is the evidence a reviewer reads; the single window's
number is kept beside it because the two disagreeing is the point.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import center_width_store as store  # noqa: E402

KEYS = ["20", "21", "22"]
CENTRES = [20.0, 21.0, 22.0]


def _cell(day: str, settled: str, sharp: bool) -> dict:
    """A cell whose settled bucket is favoured (sharp) or not (blunt)."""

    weights = {"20": 0.2, "21": 0.6, "22": 0.2} if sharp else {
        "20": 0.34, "21": 0.33, "22": 0.33
    }
    return {
        "station_id": "X",
        "event_date": day,
        "decision_at_utc": f"{day}T09:00:00Z",
        "probabilities": dict(weights),
        "ordered_keys": list(KEYS),
        "key_centers_c": list(CENTRES),
        "excluded_buckets": (),
        "settled_bucket": settled,
        "model_center_c": 21.0,
    }


def _by_day(days, *, sharp_days=()) -> dict:
    return {
        day: [
            _cell(day, "21" if index % 2 else "20", day in sharp_days)
            for index in range(6)
        ]
        for day in days
    }


DAYS = [f"2026-09-{day:02d}" for day in range(1, 9)]


def test_a_fold_is_scored_only_by_days_before_it() -> None:
    out = store._rolling_origin_admission(_by_day(DAYS), DAYS)
    assert out["fold_count"] >= 2
    for fold in out["folds"]:
        assert fold["train_end_day"] < fold["scored_day"]
        assert fold["train_day_count"] == DAYS.index(fold["scored_day"])
    # Consecutive held-out days, ending on the newest.
    assert [fold["scored_day"] for fold in out["folds"]][-1] == DAYS[-1]


def test_the_fold_count_is_bounded_and_needs_a_few_days_to_fit() -> None:
    short = store._rolling_origin_admission(_by_day(DAYS[:4]), DAYS[:4])
    assert short["fold_count"] == 1          # only 09-04 has 3 days before it
    long_days = [f"2026-08-{day:02d}" for day in range(1, 21)]
    long = store._rolling_origin_admission(_by_day(long_days), long_days)
    assert long["fold_count"] == store.ROLLING_ORIGIN_MAX_FOLDS
    assert long["folds"][-1]["scored_day"] == long_days[-1]


def test_the_gain_is_cell_weighted_not_fold_weighted() -> None:
    """A thin day must not outvote a full one."""

    by_day = _by_day(DAYS)
    by_day[DAYS[-1]] = by_day[DAYS[-1]][:1]
    out = store._rolling_origin_admission(by_day, DAYS)
    total = sum(f["gain"] * f["cell_count"] for f in out["folds"])
    cells = sum(f["cell_count"] for f in out["folds"])
    assert out["scored_cell_count"] == cells
    assert abs(out["gain"] - total / cells) < 1e-12


def test_identity_is_the_baseline_because_the_cells_carry_the_incumbent() -> None:
    source = inspect.getsource(store._rolling_origin_admission)
    assert "RESIDUAL_IDENTITY" in source
    assert "sigma_floor_rungs=0.0" in source


def test_disagreeing_folds_are_visible_in_the_receipt() -> None:
    out = store._rolling_origin_admission(_by_day(DAYS), DAYS)
    assert out["distinct_picks"] >= 1
    assert out["folds_positive"] <= out["fold_count"]
    assert {"delta_c", "width_lambda", "gain"} <= set(out["folds"][0])


def test_the_fit_reports_both_verdicts_and_admits_on_the_rolling_one() -> None:
    source = inspect.getsource(store.fit_center_width)
    assert "_rolling_origin_admission(" in source
    assert '"single_window_nll_gain": single_window_gain' in source
    assert 'gain = float(rolling["gain"])' in source
    assert '"basis": "ROLLING_ORIGIN"' in source
    # The hurdle itself is unchanged; what moved is the number it judges
    # and the second condition beside it.
    assert "gain > MIN_FORWARD_NLL_GAIN" in source


def test_the_sub_lanes_keep_the_single_window() -> None:
    """Each compares candidate against candidate on the same days."""

    source = inspect.getsource(store.fit_center_width)
    assert "_sigma_floor_lane(\n        forward=forward," in source
    assert "_width_tier_lane(\n        forward=forward," in source


def test_one_hard_day_cannot_carry_the_admission() -> None:
    """The reading from the first live run, as a rule.

    P0's ten days to 2026-09-09: folds picked four different pairs, two of
    five gained, and the cell-weighted mean cleared the hurdle at +0.0066
    only because 09-09 -- identity NLL 2.02, the hardest day in the window --
    swung +0.0987.  A mean alone would have admitted lambda 0.525 on that.
    """

    folds = [
        {"gain": -0.06, "cell_count": 100},
        {"gain": -0.01, "cell_count": 100},
        {"gain": -0.04, "cell_count": 100},
        {"gain": +0.02, "cell_count": 100},
        {"gain": +0.30, "cell_count": 100},
    ]
    positive = sum(1 for fold in folds if fold["gain"] > 0.0)
    mean = sum(f["gain"] * f["cell_count"] for f in folds) / sum(
        f["cell_count"] for f in folds
    )
    assert mean > store.MIN_FORWARD_NLL_GAIN      # the mean would admit
    assert positive * 2 <= len(folds)             # the majority rule refuses


def test_the_majority_flag_is_computed_and_gated_on() -> None:
    out = store._rolling_origin_admission(_by_day(DAYS), DAYS)
    assert "majority_positive" in out
    assert out["majority_positive"] == (
        out["folds_positive"] * 2 > out["fold_count"]
    )
    source = inspect.getsource(store.fit_center_width)
    assert 'bool(rolling["majority_positive"])' in source


def test_no_folds_is_not_a_majority() -> None:
    out = store._rolling_origin_admission({}, [])
    assert out["fold_count"] == 0
    assert out["majority_positive"] is False
    assert out["gain"] == 0.0
