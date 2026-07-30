from __future__ import annotations

import pytest

from scripts.build_joint_weak_settlement_interval_candidate import settlement_interval


def test_settlement_interval_uses_rounding_bounds_without_exact_x_coercion() -> None:
    assert settlement_interval(37.8, "whole_degrees") == {
        "bucket_c": 38.0,
        "lower_c": 37.5,
        "upper_c": 38.5,
    }


def test_settlement_interval_fails_closed_for_unknown_rounding() -> None:
    with pytest.raises(ValueError, match="unsupported_settlement_rounding_rule"):
        settlement_interval(37.8, "truncate")
