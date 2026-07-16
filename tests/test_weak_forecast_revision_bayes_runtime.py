import math

import pytest

from weak_forecast_revision_bayes_runtime import (
    apply_weak_revision_ratio,
    apply_weak_revision_ratio_to_joint_rows,
)


def test_revision_ratio_matches_recompute_with_new_weak_and_retained_strong() -> None:
    prior = {"34": 0.5, "36": 0.5}
    old_weak = {"34": math.log(0.8), "36": math.log(0.2)}
    new_weak = {"34": math.log(0.25), "36": math.log(0.75)}
    strong = {"34": 0.5, "36": 2.0}
    old_weights = {x: prior[x] * math.exp(old_weak[x]) * strong[x] for x in prior}
    old_total = sum(old_weights.values())
    current = {f"{x}|warming": value / old_total for x, value in old_weights.items()}

    updated = apply_weak_revision_ratio(
        current,
        previous_weak_log_likelihood_by_x=old_weak,
        revised_weak_log_likelihood_by_x=new_weak,
    )["posterior_joint"]
    expected_weights = {x: prior[x] * math.exp(new_weak[x]) * strong[x] for x in prior}
    expected_total = sum(expected_weights.values())

    assert updated["34|warming"] == pytest.approx(expected_weights["34"] / expected_total)
    assert updated["36|warming"] == pytest.approx(expected_weights["36"] / expected_total)


def test_revision_does_not_change_latent_mix_within_same_x() -> None:
    result = apply_weak_revision_ratio(
        {"35|warming": 0.3, "35|slowing": 0.2, "36|warming": 0.5},
        previous_weak_log_likelihood_by_x={"35": 0.0, "36": 0.0},
        revised_weak_log_likelihood_by_x={"35": 1.0, "36": -1.0},
    )["posterior_joint"]
    assert result["35|warming"] / result["35|slowing"] == pytest.approx(1.5)


def test_joint_row_revision_preserves_latent_mix_inside_temperature() -> None:
    result = apply_weak_revision_ratio_to_joint_rows(
        [
            {"temperature_c": 33.0, "state": "warming", "probability": 0.3},
            {"temperature_c": 33.0, "state": "terminated", "probability": 0.1},
            {"temperature_c": 34.0, "state": "warming", "probability": 0.3},
            {"temperature_c": 34.0, "state": "terminated", "probability": 0.3},
        ],
        previous_weak_likelihood_by_x={"33": 1.0, "34": 1.0},
        revised_weak_likelihood_by_x={"33": 2.0, "34": 0.5},
    )
    rows = result["posterior_joint"]
    by_key = {(row["temperature_c"], row["state"]): row["probability"] for row in rows}
    assert by_key[(33.0, "warming")] / by_key[(33.0, "terminated")] == pytest.approx(3.0)
    assert by_key[(34.0, "warming")] / by_key[(34.0, "terminated")] == pytest.approx(1.0)
    assert sum(by_key.values()) == pytest.approx(1.0)
