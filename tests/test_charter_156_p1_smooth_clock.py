"""Charter 156: P1's alpha is a curve in the hours left, and the rise rung's
clock is read between bin midpoints (2026-09-19, 「平滑时钟」→「上线」).

Both used to be steps with an edge at 10h.  On ZUUU 2026-09-19 the same
reports read rise 0.797 and alpha 0.25 at 10.05h, then rise 0.379 and alpha
1.375 at 9.92h, and 23C moved from 0.33 to 0.84 in the minutes between.
These pin the serving arithmetic, the fall-back to steps for artifacts (and
code) without the curve, and the refit's curve choice.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from bayes_basket_target import metar_four_layer as M4
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target.contracts import ContractError
from tests.test_charter_150_p1_joint_likelihood import _artifact

ROOT = Path(__file__).resolve().parents[1]


def _fitter():
    spec = importlib.util.spec_from_file_location(
        "_fit_p1_charter156", ROOT / "scripts" / "fit_p1_observation_likelihood.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _curved(**overrides):
    return _artifact(
        alpha_knots_hours=[0.0, 6.0, 10.0, 14.0],
        alpha_knot_values=[2.0, 1.5, 1.0, 0.0],
        **overrides,
    )


def test_alpha_reads_the_curve_linearly_and_flat_past_its_ends() -> None:
    art = _curved()
    assert OL.alpha_for(6.0, art) == pytest.approx(1.5)
    assert OL.alpha_for(8.0, art) == pytest.approx(1.25)
    assert OL.alpha_for(12.0, art) == pytest.approx(0.5)
    assert OL.alpha_for(-1.0, art) == pytest.approx(2.0)
    assert OL.alpha_for(30.0, art) == pytest.approx(0.0)
    # No edge: a minute either side of 10h reads the same, where the steps
    # read 0.5 and 1.0.
    assert abs(OL.alpha_for(10.02, art) - OL.alpha_for(9.98, art)) < 0.01
    steps = _artifact()
    assert OL.alpha_for(10.02, steps) == 0.5 and OL.alpha_for(9.98, steps) == 1.0


def test_an_artifact_without_a_curve_serves_its_steps() -> None:
    art = _artifact()
    assert OL.alpha_curve(art) is None
    assert [OL.alpha_for(h, art) for h in (12.0, 8.0, 3.0)] == [0.5, 1.0, 1.5]


@pytest.mark.parametrize("knots, values", [
    ([0.0, 10.0, 6.0], [1.0, 1.0, 1.0]),   # not increasing
    ([0.0, 10.0], [1.0]),                   # lengths differ
    ([0.0, 10.0], [1.0, -0.5]),             # negative alpha
    ([0.0], [1.0]),                         # one knot is not a curve
    ([0.0, float("nan")], [1.0, 1.0]),
])
def test_a_malformed_curve_is_refused(knots, values) -> None:
    with pytest.raises(ContractError):
        OL.alpha_for(5.0, _artifact(alpha_knots_hours=knots, alpha_knot_values=values))


def _features(hours: float) -> dict:
    feats = {name: None for name in OL.FULL_FEATURES}
    feats.update({"remaining_hours": hours, "cloud_cover_fraction": 0.4, "drawdown_from_high_c": 0.0})
    return feats


def test_the_clock_is_continuous_across_a_bin_edge_and_exact_at_midpoints() -> None:
    art = _artifact()
    art["phase"]["clock"] = M4.CLOCK_MIDPOINT_LINEAR
    below, above = (M4.phase_rise_probability(_features(h), art) for h in (9.99, 10.01))
    assert abs(below - above) < 0.01
    steps = _artifact()
    jump = abs(M4.phase_rise_probability(_features(9.99), steps) - M4.phase_rise_probability(_features(10.01), steps))
    assert jump > 0.2
    # At a bin's midpoint the reading is the fitted intercept itself.
    midpoint = M4.phase_rise_probability(_features(8.0), art)   # bin [6, 10)
    assert midpoint == pytest.approx(M4.phase_rise_probability(_features(8.0), steps))
    # And the open last bin is flat past its read point.
    assert M4.phase_rise_probability(_features(20.0), art) == pytest.approx(
        M4.phase_rise_probability(_features(40.0), art)
    )


def test_the_receipt_says_which_reading_served() -> None:
    art = _curved()
    art["phase"]["clock"] = M4.CLOCK_MIDPOINT_LINEAR
    buckets = {"20": 0, "21": 1, "22": 2}
    centres = {"20": 20.0, "21": 21.0, "22": 22.0}
    _, receipt = OL.compile_observation_likelihood(
        _features(9.9), art, bucket_ordinals=buckets, bucket_centers_c=centres,
        running_high_c=20.6, running_high_bucket="21",
    )
    assert receipt["alpha_mode"] == "curve" and receipt["clock_mode"] == "midpoint_linear"
    assert receipt["alpha"] == pytest.approx(OL.alpha_for(9.9, art))
    _, old = OL.compile_observation_likelihood(
        _features(9.9), _artifact(), bucket_ordinals=buckets, bucket_centers_c=centres,
        running_high_c=20.6, running_high_bucket="21",
    )
    assert old["alpha_mode"] == "bins" and old["clock_mode"] == "bins"


def _synthetic_arrays(seed: int = 7) -> dict:
    """Evidence that tells the truth when few hours are left and lies with many."""

    rng = np.random.default_rng(seed)
    rows = 1200
    hours = rng.uniform(0.0, 24.0, rows)
    truth = rng.integers(0, 3, rows)
    prior = np.full((rows, 3), 1.0 / 3.0)
    evidence = np.full((rows, 3), 0.5)
    for i in range(rows):
        if hours[i] < 8.0:
            evidence[i, truth[i]] = 0.9          # informative late in the day
        else:
            evidence[i, rng.integers(0, 3)] = 0.9  # noise early
    day_index = np.array([i % 10 for i in range(rows)])
    days = np.array([f"2026-09-{1 + d:02d}" for d in day_index])
    # A report at (24 - hours) into its day; the truth commits two hours
    # after the day ends.
    day_start = 1788220800.0 + 86400.0 * day_index        # 2026-09-01T00:00Z
    return {"prior": prior, "evidence": evidence, "truth": truth, "hours": hours,
            "weight": np.ones(rows), "day": days,
            "live": np.ones((rows, 3), dtype=bool),
            "as_of": day_start + 3600.0 * (24.0 - hours),
            "known_at": day_start + 86400.0 + 7200.0,
            "incumbent": np.full(rows, np.nan)}


def test_the_refit_curve_trusts_the_reports_late_and_not_early() -> None:
    fitter = _fitter()
    arrays = _synthetic_arrays()
    values = fitter.fit_alpha_curve(arrays, np.ones(len(arrays["truth"]), dtype=bool))
    knots = list(OL.ALPHA_KNOTS_HOURS)
    late = np.interp(3.0, knots, values)
    early = np.interp(18.0, knots, values)
    assert late > 1.0 and early < 0.5
    report = fitter.score_alpha_curve(arrays)
    # Walked forward: a day is scored only once four earlier days' truth is
    # in (days 1-4 train day 6's curve; day 5's truth lands after day 6 starts).
    assert sorted(report["forward_values_by_day"]) == [
        f"2026-09-{d:02d}" for d in range(6, 11)
    ]
    assert report["forward_nat"] > report["forward_root_nat"]
    assert report["forward_incumbent_nat"] is None


def test_the_refit_publishes_the_midpoint_clock() -> None:
    fitter = _fitter()
    rng = np.random.default_rng(3)
    samples = []
    for i in range(400):
        hours = float(rng.uniform(0.0, 20.0))
        samples.append({
            "station_id": "KAAA", "event_date": f"2026-09-{1 + i % 20:02d}",
            "remaining_hours": hours, "station_day_weight": 1.0,
            "crossed_bucket": int(rng.random() < min(0.9, hours / 20.0)),
            "delta_c": int(rng.integers(0, 4)),
            **{name: float(rng.normal()) for name in OL.FULL_FEATURES if name != "remaining_hours"},
        })
    body = fitter.fit_g(samples)
    assert body["phase"]["clock"] == M4.CLOCK_MIDPOINT_LINEAR
    assert math.isfinite(M4.phase_rise_probability({**_features(7.0)}, body))
