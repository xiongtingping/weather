from decimal import Decimal

import pytest

from bayes_basket_target.reserve_price import (
    ReservePriceError,
    compute_final_reserve_price,
    presize_reserve_price,
    tick_down,
)


def test_learned_cap_tightens_math_cap_and_ticks_down_exactly() -> None:
    result = presize_reserve_price(
        r_math="0.871",
        p_eligible="0.845",
        learned_margin="0.012",
        tick_size="0.01",
    )

    assert result.learned_cap == Decimal("0.833")
    assert result.reserve_price == Decimal("0.83")
    assert result.price_changed is True
    assert result.requires_depth_resizing is True
    assert result.requires_sizing_re_evaluation is True
    assert result.requires_re_evaluation is True


def test_math_cap_remains_binding_when_learned_cap_is_looser() -> None:
    result = compute_final_reserve_price(
        r_math=Decimal("0.57"),
        p_eligible=Decimal("0.91"),
        learned_margin=Decimal("0.10"),
        tick_size=Decimal("0.01"),
    )

    assert result.learned_cap == Decimal("0.81")
    assert result.reserve_price == Decimal("0.57")


def test_exact_tick_is_not_pushed_down_an_extra_tick() -> None:
    assert tick_down("0.83", "0.01") == Decimal("0.83")
    result = presize_reserve_price(
        r_math="0.83",
        p_eligible="0.90",
        learned_margin="0.07",
        tick_size="0.01",
        previous_reserve_price="0.83",
    )
    assert result.reserve_price == Decimal("0.83")
    assert result.price_changed is False
    assert result.requires_re_evaluation is False


def test_off_tick_value_is_floored_not_rounded_up() -> None:
    assert tick_down("0.839999", "0.01") == Decimal("0.83")
    assert tick_down("1.005", "0.01") == Decimal("1.00")
    assert tick_down("0.0101", "0.005") == Decimal("0.010")


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("r_math_negative", {"r_math": "-0.01", "p_eligible": "0.5", "learned_margin": "0", "tick_size": "0.01"}),
        ("eligible_negative", {"r_math": "0.5", "p_eligible": "-0.01", "learned_margin": "0", "tick_size": "0.01"}),
        ("margin_negative", {"r_math": "0.5", "p_eligible": "0.5", "learned_margin": "-0.01", "tick_size": "0.01"}),
        ("learned_cap_negative", {"r_math": "0.5", "p_eligible": "0.01", "learned_margin": "0.02", "tick_size": "0.01"}),
        ("math_nonfinite", {"r_math": "NaN", "p_eligible": "0.5", "learned_margin": "0", "tick_size": "0.01"}),
        ("eligible_nonfinite", {"r_math": "0.5", "p_eligible": "Infinity", "learned_margin": "0", "tick_size": "0.01"}),
        ("margin_nonfinite", {"r_math": "0.5", "p_eligible": "0.5", "learned_margin": "-Infinity", "tick_size": "0.01"}),
        ("tick_nonpositive", {"r_math": "0.5", "p_eligible": "0.5", "learned_margin": "0", "tick_size": "0"}),
    ],
)
def test_invalid_inputs_fail_closed(name: str, kwargs: dict[str, str]) -> None:
    with pytest.raises(ReservePriceError, match="invalid|negative|nonfinite|nonpositive"):
        presize_reserve_price(**kwargs)


def test_zero_is_a_valid_closed_cap_and_zero_tick_is_not() -> None:
    result = presize_reserve_price(
        r_math="0",
        p_eligible="0",
        learned_margin="0",
        tick_size="0.01",
    )
    assert result.reserve_price == Decimal("0.00")
    assert result.requires_re_evaluation is True


def test_decimal_round_trip_preserves_price_and_change_obligation() -> None:
    first = presize_reserve_price(
        r_math=Decimal("0.875"),
        p_eligible=Decimal("0.875"),
        learned_margin=Decimal("0.005"),
        tick_size=Decimal("0.01"),
    )
    second = presize_reserve_price(
        r_math=str(first.reserve_price),
        p_eligible=str(first.reserve_price),
        learned_margin="0",
        tick_size=str(first.tick_size),
        previous_reserve_price=str(first.reserve_price),
    )

    assert second.reserve_price == first.reserve_price
    assert second.price_changed is False
    assert second.requires_depth_resizing is False
    assert second.requires_sizing_re_evaluation is False
