from __future__ import annotations

import pytest

from bayes_basket_target.basket_kernel.contracts import BasketState, ContractError
from bayes_basket_target.basket_kernel.state_machine import (
    basket_lifecycle_projection,
    resolve_basket_state,
)


ORDINALS = {str(value): value for value in range(33, 41)}


def resolve(**overrides):
    arguments = {
        "bucket_ordinals": ORDINALS,
        "desired_support": ("36", "37", "38"),
        "selected_before": (),
        "positive_gap_buckets": ("36", "37", "38"),
        "execution_authorized": True,
        "order_state_known": True,
        "stop_adding": False,
    }
    arguments.update(overrides)
    return resolve_basket_state(**arguments)


def test_seed_requires_complete_authorized_initial_gap() -> None:
    result = resolve()

    assert result.state is BasketState.SEED
    assert result.added_buckets == ("36", "37", "38")
    assert result.positive_gap_buckets == ("36", "37", "38")


@pytest.mark.parametrize(
    ("desired", "previous", "expected"),
    [
        (("35", "36", "37"), ("36", "37"), BasketState.EXTEND_LOW),
        (("36", "37", "38"), ("36", "37"), BasketState.EXTEND_HIGH),
        (("35", "36", "37", "39"), ("36", "37"), BasketState.FILL_GAP),
        (("36", "37", "38"), ("36", "38"), BasketState.FILL_GAP),
    ],
)
def test_support_changes_have_explicit_direction_or_gap_state(
    desired: tuple[str, ...],
    previous: tuple[str, ...],
    expected: BasketState,
) -> None:
    result = resolve(
        desired_support=desired,
        selected_before=previous,
        positive_gap_buckets=tuple(
            bucket for bucket in desired if bucket not in previous
        ),
    )

    assert result.state is expected


def test_existing_support_gap_is_replenish_not_new_seed() -> None:
    result = resolve(
        selected_before=("36", "37", "38"),
        positive_gap_buckets=("37",),
    )

    assert result.state is BasketState.REPLENISH


def test_hold_and_stop_precedence_is_fail_closed() -> None:
    unknown = resolve(order_state_known=False)
    unauthorized = resolve(execution_authorized=False)
    satisfied = resolve(
        selected_before=("36", "37", "38"),
        positive_gap_buckets=(),
    )
    stopped = resolve(stop_adding=True)

    assert unknown.state is BasketState.HOLD
    assert unauthorized.state is BasketState.HOLD
    assert satisfied.state is BasketState.HOLD
    assert stopped.state is BasketState.STOP


def test_persisted_lifecycle_projection_rejects_support_drift() -> None:
    resolution = resolve()
    plan = {
        "snapshot_sha256": "a" * 64,
        "candidate_support": ["36", "37", "38"],
        "final_support": ["36", "37", "38"],
        "basket_state": resolution.state.value,
        "basket_state_reason": resolution.reason,
        "basket_state_transition": resolution.to_dict(),
    }

    projected = basket_lifecycle_projection(plan)
    assert projected["state"] == "SEED"
    assert len(projected["support_version_sha256"]) == 64

    plan["final_support"] = ["36", "37"]
    with pytest.raises(
        ContractError,
        match="basket_lifecycle_support_drift",
    ):
        basket_lifecycle_projection(plan)


def test_low_high_low_and_stop_resume_are_history_independent() -> None:
    low = resolve(
        desired_support=("35", "36", "37"),
        selected_before=("36", "37"),
        positive_gap_buckets=("35",),
    )
    high = resolve(
        desired_support=("36", "37", "38"),
        selected_before=("36", "37"),
        positive_gap_buckets=("38",),
    )
    low_replay = resolve(
        desired_support=("35", "36", "37"),
        selected_before=("36", "37"),
        positive_gap_buckets=("35",),
    )
    stopped = resolve(stop_adding=True)
    resumed = resolve()

    assert low == low_replay
    assert low.state is BasketState.EXTEND_LOW
    assert high.state is BasketState.EXTEND_HIGH
    assert stopped.state is BasketState.STOP
    assert resumed.state is BasketState.SEED


def test_gap_outside_desired_support_is_rejected() -> None:
    with pytest.raises(
        ContractError,
        match="basket_state_gap_outside_desired_support",
    ):
        resolve(positive_gap_buckets=("35",))
