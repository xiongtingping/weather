"""The fusion panel re-solves served weights with production's own functions.

Until 2026-09-17 the panel carried its own minimum-variance solver and
compared every seat against it.  The copy had drifted three ways from
production -- train RMSE where production scales by the residual sigma, no
family caps, no single-system active set -- and charter 134 had since shrunk
every pool seat halfway toward family balance.  Served weights sat about 20%
from that optimum by design, and the panel painted it red.

The panel now rebuilds, from what the SSOT keeps, the inputs production read
and hands them to production's functions unchanged.  These tests seat a
blend the way production does and require the panel to recover it:

* exactly, when the shared train window was long enough for a covariance
  (the correlation and the inverse-variance shadow pin the covariance);
* exactly, when the window was short and the seat fell back to inverse
  error, given the scales and day counts that fallback read;
* and not by accident: dropping the pool shrinkage must move the answer,
  so the fixtures are not already family-balanced.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import calibration_dashboard as C
from bayes_basket_target import board_readings as BR  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.stable_pool import (  # noqa: E402
    STABLE_POOL_KAPPA,
    stable_pool_weights,
)

FAMILIES = {"A": "fa", "B": "fb", "C": "fc"}
CAPS = {"fa": 0.75, "fb": 0.75, "fc": 0.75}


def _residuals(days: int, seed: int = 7) -> dict[str, dict[str, float]]:
    """A and B share most of their error; C is independent and noisier."""

    rng = random.Random(seed)
    out: dict[str, dict[str, float]] = {"A": {}, "B": {}, "C": {}}
    for index in range(days):
        day = f"2026-08-{index + 1:02d}"
        shared = rng.gauss(0.0, 1.0)
        out["A"][day] = 0.4 + shared + rng.gauss(0.0, 0.35)
        out["B"][day] = -1.1 + 0.9 * shared + rng.gauss(0.0, 0.5)
        out["C"][day] = 2.0 + rng.gauss(0.0, 1.3)
    return out


def _stored_correlation(
    residuals: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """What the evidence row persists: pairwise ``_correlation``."""

    systems = sorted(residuals)
    return {
        left: {
            right: oos._correlation(
                [residuals[left][day] for day in sorted(
                    set(residuals[left]) & set(residuals[right])
                )],
                [residuals[right][day] for day in sorted(
                    set(residuals[left]) & set(residuals[right])
                )],
            )
            for right in systems
        }
        for left in systems
    }


def _population_covariance(series: dict[str, dict[str, float]]):
    systems = sorted(series)
    days = sorted(set.intersection(*(set(series[s]) for s in systems)))
    means = {s: sum(series[s][d] for d in days) / len(days) for s in systems}
    return [
        [
            sum(
                (series[a][d] - means[a]) * (series[b][d] - means[b])
                for d in days
            ) / len(days)
            for b in systems
        ]
        for a in systems
    ]


def test_synthetic_series_carry_exactly_the_stored_covariance():
    correlation = {
        "A": {"A": 1.0, "B": 0.83, "C": 0.12},
        "B": {"A": 0.83, "B": 1.0, "C": -0.05},
        "C": {"A": 0.12, "B": -0.05, "C": 1.0},
    }
    sigmas = [1.3, 0.8, 2.1]
    series = BR._fusion_synthetic_residuals(["A", "B", "C"], correlation, sigmas)
    assert series is not None
    got = _population_covariance(series)
    for row, left in enumerate("ABC"):
        for column, right in enumerate("ABC"):
            want = correlation[left][right] * sigmas[row] * sigmas[column]
            assert got[row][column] == pytest.approx(want, abs=1e-9)
    assert len(next(iter(series.values()))) >= oos.MINIMUM_VARIANCE_MIN_DAYS


def test_a_correlation_with_no_covariance_is_refused_not_guessed():
    # Pairwise day sets can store this; no three series have it.
    impossible = {
        "A": {"A": 1.0, "B": 0.95, "C": -0.95},
        "B": {"A": 0.95, "B": 1.0, "C": 0.95},
        "C": {"A": -0.95, "B": 0.95, "C": 1.0},
    }
    assert BR._fusion_synthetic_residuals(
        ["A", "B", "C"], impossible, [1.0, 1.0, 1.0]
    ) is None


def _served_pool_seat(residuals, scale, train_n):
    """The seat exactly as ``_select_minimum_crps_subset`` builds a pool seat."""

    skill, rule = oos._seat_weights(
        residuals_by_system=residuals,
        train_error_scale_by_system=scale,
        family_by_system=FAMILIES,
        family_caps=CAPS,
        train_n_by_system=train_n,
    )
    return stable_pool_weights(skill, FAMILIES, kappa=STABLE_POOL_KAPPA), rule


def _distance(left, right):
    return 0.5 * sum(abs(left[s] - right.get(s, 0.0)) for s in left)


def test_a_covariance_seat_resolves_exactly_from_what_the_ssot_keeps():
    residuals = _residuals(days=24)
    scale = {"A": 1.0, "B": 1.0, "C": 1.0}  # unused on this branch
    served, rule = _served_pool_seat(residuals, scale, None)
    assert rule == oos.SEAT_WEIGHT_RULE_COVARIANCE
    shadow = oos._minimum_variance_weights(
        residuals_by_system=residuals,
        family_by_system=FAMILIES,
        family_caps=CAPS,
        shrinkage=1.0,
    )
    assert shadow is not None

    rebuilt = BR._fusion_reconstruction(
        served=served,
        correlation=_stored_correlation(residuals),
        inverse_variance=shadow,
        # Deliberately wrong: the exact path must not read the member scale.
        member_scale={"A": 9.0, "B": 0.1, "C": 4.0},
        member_train_n={"A": 3, "B": 30, "C": 3},
        family_by_system=FAMILIES,
        family_caps=CAPS,
        kappa=STABLE_POOL_KAPPA,
    )
    assert rebuilt["basis"] == "exact"
    assert rebuilt["skill_rule"] == oos.SEAT_WEIGHT_RULE_COVARIANCE
    for system in served:
        assert rebuilt["weights"][system] == pytest.approx(
            served[system], abs=1e-9
        )

    # Not an accident of the fixture: without the pool shrinkage, or with
    # train RMSE standing in for the recovered variances, the answer moves.
    unshrunk = BR._fusion_reconstruction(
        served=served,
        correlation=_stored_correlation(residuals),
        inverse_variance=shadow,
        member_scale=scale,
        member_train_n={},
        family_by_system=FAMILIES,
        family_caps=CAPS,
        kappa=None,
    )
    assert _distance(served, unshrunk["weights"]) > 0.02


def test_a_short_window_seat_resolves_through_the_inverse_error_fallback():
    residuals = _residuals(days=oos.MINIMUM_VARIANCE_MIN_DAYS - 3)
    scale = {"A": 1.4, "B": 0.9, "C": 2.2}
    train_n = {"A": 9, "B": 30, "C": 14}
    served, rule = _served_pool_seat(residuals, scale, train_n)
    assert rule == oos.SEAT_WEIGHT_RULE_INVERSE_RMSE
    assert oos._minimum_variance_weights(
        residuals_by_system=residuals,
        family_by_system=FAMILIES,
        family_caps=CAPS,
        shrinkage=1.0,
    ) is None

    rebuilt = BR._fusion_reconstruction(
        served=served,
        correlation=_stored_correlation(residuals),
        inverse_variance=None,
        member_scale=scale,
        member_train_n=train_n,
        family_by_system=FAMILIES,
        family_caps=CAPS,
        kappa=STABLE_POOL_KAPPA,
    )
    assert rebuilt["basis"] == "fallback"
    for system in served:
        assert rebuilt["weights"][system] == pytest.approx(
            served[system], abs=1e-12
        )

    # The day counts matter on this branch: a mature ramp gives other shares.
    mature = BR._fusion_reconstruction(
        served=served,
        correlation=_stored_correlation(residuals),
        inverse_variance=None,
        member_scale=scale,
        member_train_n={},
        family_by_system=FAMILIES,
        family_caps=CAPS,
        kappa=STABLE_POOL_KAPPA,
    )
    assert _distance(served, mature["weights"]) > 0.02


def test_a_shadow_pinned_at_a_family_cap_is_marked_approximate():
    shadow = {"A": 0.75, "B": 0.15, "C": 0.10}
    rebuilt = BR._fusion_reconstruction(
        served={"A": 0.5, "B": 0.3, "C": 0.2},
        correlation={
            s: {t: (1.0 if s == t else 0.2) for t in "ABC"} for s in "ABC"
        },
        inverse_variance=shadow,
        member_scale={"A": 1.0, "B": 1.2, "C": 1.5},
        member_train_n={},
        family_by_system=FAMILIES,
        family_caps=CAPS,
        kappa=None,
    )
    assert rebuilt["basis"] == "capped"
    assert rebuilt["weights"] is not None
    assert math.isclose(sum(rebuilt["weights"].values()), 1.0)


def test_the_panel_no_longer_carries_a_solver_of_its_own():
    assert not hasattr(C, "_minimum_variance_from_correlation")
    assert not hasattr(C, "_solve")


def test_the_board_grades_what_the_backend_resolved(tmp_path, monkeypatch) -> None:
    """User rulings 2026-10-02: the re-solve runs in the backend board-readings
    job; the panel grades the rows that job wrote and computes nothing."""

    import sqlite3

    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    row = {"station_id": "KAAA", "season_bin": "PEAK", "lead_bucket": "h24_48",
           "run_cycle": "00Z", "rule": "稳定池", "basis": "exact",
           "max_correlation": 0.3, "distance": 0.02,
           "served": {"A": 0.5, "B": 0.5}, "resolved": {"A": 0.51, "B": 0.49}}
    BR.write_reading("fusion_resolve", {"rows": [row], "unread": 0}, code="x", build_sec=1.0)
    out = C._collect_fusion_layer(sqlite3.connect(":memory:"))
    assert out["summary"]["exact_count"] == 1
    assert out["pairs"][0]["distance"] == 0.02
    assert out["health"] == "green"


def test_correlated_models_are_a_reading_not_the_lamp(tmp_path, monkeypatch) -> None:
    """User ruling 2026-10-15 「改」: the lamp grades only the weight re-solve;
    how alike the models' errors are is shown as effective independent models."""

    import sqlite3

    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    correlation = {a: {b: (1.0 if a == b else 0.64) for b in "ABCDE"} for a in "ABCDE"}
    mean_r, effective = BR.effective_independent_models(correlation, list("ABCDE"))
    assert abs(mean_r - 0.64) < 1e-12
    assert abs(effective - 5 / (1 + 4 * 0.64)) < 1e-12
    row = {"station_id": "KAAA", "season_bin": "PEAK", "lead_bucket": "h24_48",
           "run_cycle": "00Z", "rule": "稳定池", "basis": "exact",
           "max_correlation": 0.95, "distance": 0.04, "system_count": 5,
           "mean_correlation": mean_r, "effective_models": effective,
           "served": {"A": 0.2}, "resolved": {"A": 0.2}}
    BR.write_reading("fusion_resolve", {"rows": [row], "unread": 0}, code="x", build_sec=1.0)
    out = C._collect_fusion_layer(sqlite3.connect(":memory:"))
    assert out["health"] == "green"
    assert out["independent_models"]["text"] == "有效独立模型 1.4/5 家"
    assert "有效独立模型 1.4/5 家" in out["detail"]
    assert "residual_correlation" not in C.SCORECARD_THRESHOLDS
    # Negative correlation never counts more models than are seated.
    anti = {a: {b: (1.0 if a == b else -0.2) for b in "AB"} for a in "AB"}
    assert BR.effective_independent_models(anti, ["A", "B"])[1] == 2.0
