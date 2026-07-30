from __future__ import annotations

import pytest

from bayes_basket_target.basket_kernel.contracts import Direction
from bayes_basket_target.continuous_core import (
    ContinuousCorePolicy,
    CoreResolutionState,
    CoreTransition,
    DirectionTransition,
    ModelCenter,
    PeripheralRiskCandidate,
    build_model_anchor_range,
    resolve_continuous_core,
)


ORDINALS = {f"{value}C": value for value in range(30, 44)}
PROBABILITIES = {
    "30C": 0.01,
    "31C": 0.01,
    "32C": 0.01,
    "33C": 0.02,
    "34C": 0.03,
    "35C": 0.05,
    "36C": 0.12,
    "37C": 0.28,
    "38C": 0.25,
    "39C": 0.12,
    "40C": 0.05,
    "41C": 0.03,
    "42C": 0.01,
    "43C": 0.01,
}
MEMBERS = (
    ModelCenter("ecmwf", "38C", 0.60),
    ModelCenter("gfs", "35C", 0.25),
    ModelCenter("icon", "37C", 0.15),
)
DOWN_MEMBERS = (
    ModelCenter("ecmwf", "35C", 0.60),
    ModelCenter("gfs", "38C", 0.25),
    ModelCenter("icon", "36C", 0.15),
)
DOWN_PROBABILITIES = {
    bucket: PROBABILITIES[f"{73 - int(bucket[:-1])}C"]
    for bucket in PROBABILITIES
}
POLICY = ContinuousCorePolicy(
    max_peripheral_layers=2,
    min_peripheral_probability_mass=0.01,
    min_peripheral_model_reliability=0.60,
    min_peripheral_price_coverage_value=0.01,
)


def resolve(direction: Direction, **overrides):
    arguments = {
        "bucket_ordinals": ORDINALS,
        "probabilities": PROBABILITIES,
        "members": MEMBERS,
        "stable_center_bucket": "37C",
        "center_stable": True,
        "direction": direction,
        "policy": POLICY,
        "core_probability_mass_target": 0.60,
        "proposed_core_buckets": ("36C", "37C", "38C"),
    }
    arguments.update(overrides)
    return resolve_continuous_core(**arguments)


@pytest.mark.parametrize("layers", (-1, 3, 9))
def test_peripheral_expansion_layers_are_hard_capped_at_two(
    layers: int,
) -> None:
    with pytest.raises(
        ValueError,
        match="continuous_core_layer_policy_invalid",
    ):
        ContinuousCorePolicy(max_peripheral_layers=layers)


def test_formal_positive_weight_members_alone_define_anchor() -> None:
    anchor = build_model_anchor_range(
        ORDINALS,
        (
            *MEMBERS,
            ModelCenter("zero_weight", "30C", 0.0),
            ModelCenter("diagnostic_outlier", "99C", 1.0, formal_member=False),
        ),
    )

    assert anchor.lower_bucket == "35C"
    assert anchor.upper_bucket == "38C"
    assert anchor.buckets == ("35C", "36C", "37C", "38C")
    assert anchor.member_ids == ("ecmwf", "gfs", "icon")


def test_combination_probability_core_is_not_model_extrema_hull() -> None:
    result = resolve(Direction.UP)

    assert result.resolution_state is CoreResolutionState.APPLIED
    assert result.effective_direction is Direction.UP
    assert result.anchor_range.buckets == ("35C", "36C", "37C", "38C")
    assert result.core_interval.buckets == ("36C", "37C", "38C")
    assert result.trimmed_buckets == ()
    assert all(value > 0 for _, value in result.core_interval.target_inventory)
    assert result.transition is CoreTransition.INITIALIZE


def test_down_is_exact_mirror_of_up() -> None:
    result = resolve(
        Direction.DOWN,
        stable_center_bucket="36C",
        probabilities=DOWN_PROBABILITIES,
        members=DOWN_MEMBERS,
        proposed_core_buckets=("35C", "36C", "37C"),
    )

    assert result.resolution_state is CoreResolutionState.APPLIED
    assert result.core_interval.buckets == ("35C", "36C", "37C")
    assert result.trimmed_buckets == ()


def test_probability_core_must_contain_stable_center() -> None:
    result = resolve(
        Direction.UP,
        proposed_core_buckets=("38C", "39C"),
    )

    assert result.resolution_state is CoreResolutionState.BLOCKED
    assert result.reason == "proposed_core_not_probability_selected"
    assert result.core_interval is None
    assert result.transition is CoreTransition.HOLD


def test_neutral_and_unstable_center_retain_probability_core() -> None:
    neutral = resolve(Direction.NEUTRAL)
    unstable = resolve(Direction.UP, center_stable=False)

    for result in (neutral, unstable):
        assert result.resolution_state is CoreResolutionState.NEUTRAL
        assert result.effective_direction is Direction.NEUTRAL
        assert result.core_interval.buckets == ("36C", "37C", "38C")
    assert unstable.reason == "center_unstable_neutralized"


def test_candidate_boundary_does_not_equal_sparse_final_support() -> None:
    result = resolve(
        Direction.NEUTRAL,
        members=(
            ModelCenter("ecmwf", "34C", 0.60),
            ModelCenter("gfs", "40C", 0.40),
        ),
        risk_candidates=(
            PeripheralRiskCandidate("33C", 0.02, 0.80, 0.02, 0.5),
            PeripheralRiskCandidate("34C", 0.03, 0.50, 0.02, 0.5),
            PeripheralRiskCandidate("35C", 0.05, 0.80, 0.02, 0.5),
            PeripheralRiskCandidate("39C", 0.12, 0.80, 0.00, 0.5),
            PeripheralRiskCandidate("40C", 0.05, 0.80, 0.02, 0.5),
            PeripheralRiskCandidate("41C", 0.03, 0.99, 0.50, 0.5),
        ),
    )

    assert result.candidate_boundary.buckets == (
        "34C",
        "35C",
        "36C",
        "37C",
        "38C",
        "39C",
        "40C",
    )
    assert result.support_set.core_buckets == ("36C", "37C", "38C")
    assert result.support_set.optional_risk_buckets == ("35C", "40C")
    assert result.support_set.buckets == (
        "35C",
        "36C",
        "37C",
        "38C",
        "40C",
    )
    assert ("33C", "outside_candidate_boundary") in result.rejected_risk_buckets
    assert ("34C", "model_reliability_gate_failed") in result.rejected_risk_buckets
    assert ("39C", "price_coverage_gate_failed") in result.rejected_risk_buckets
    assert ("41C", "outside_candidate_boundary") in result.rejected_risk_buckets
    assert "34C" not in result.support_set.buckets
    assert "39C" not in result.support_set.buckets


@pytest.mark.parametrize("direction", list(Direction))
def test_direction_changes_bias_state_not_probability_core(
    direction: Direction,
) -> None:
    result = resolve(direction)
    assert result.core_interval.buckets == ("36C", "37C", "38C")
    assert result.effective_direction is direction


def test_unrepresentable_center_blocks_and_remote_anchor_is_layer_capped() -> None:
    missing_center = resolve(Direction.UP, stable_center_bucket="99C")
    edge = resolve(
        Direction.UP,
        members=(
            ModelCenter("ecmwf", "42C", 0.6),
            ModelCenter("gfs", "43C", 0.4),
        ),
    )

    assert missing_center.resolution_state is CoreResolutionState.UNREPRESENTABLE
    assert missing_center.reason == "stable_center_unrepresentable"
    assert edge.resolution_state is CoreResolutionState.APPLIED
    assert edge.core_interval.buckets == ("36C", "37C", "38C")
    assert edge.candidate_boundary.buckets == (
        "36C",
        "37C",
        "38C",
        "39C",
        "40C",
    )


def test_missing_any_core_maker_leg_holds_complete_bundle() -> None:
    result = resolve(
        Direction.UP,
        maker_available_by_bucket={"38C": False},
    )

    assert result.resolution_state is CoreResolutionState.HOLD
    assert result.transition is CoreTransition.HOLD
    assert result.positive_inventory_gap == ()
    assert result.reason == "complete_core_maker_bundle_missing:38C"


def test_fill_gap_is_positive_only_and_pending_counts_as_inventory() -> None:
    targets = {f"{value}C": 3.0 for value in range(36, 39)}
    result = resolve(
        Direction.UP,
        target_inventory_by_bucket=targets,
        filled_inventory_by_bucket={
            "36C": 3.0,
            "37C": 1.0,
            "38C": 4.0,
            "39C": 0.0,
            "40C": 2.0,
        },
        pending_inventory_by_bucket={"37C": 1.0, "39C": 1.5},
        current_core_buckets=("36C", "37C", "38C"),
    )

    assert result.transition is CoreTransition.FILL_GAP
    assert dict(result.positive_inventory_gap) == {"37C": 1.0}
    assert "38C" not in dict(result.positive_inventory_gap)


def test_direction_migration_is_separate_from_core_migration() -> None:
    migrated = resolve(
        Direction.DOWN,
        current_core_buckets=("36C", "37C", "38C"),
        current_direction=Direction.UP,
    )
    neutralized = resolve(
        Direction.UP,
        center_stable=False,
        current_core_buckets=("36C", "37C", "38C"),
        current_direction=Direction.UP,
    )
    replay = resolve(
        Direction.DOWN,
        current_core_buckets=("36C", "37C", "38C"),
        current_direction=Direction.UP,
    )

    assert migrated.core_interval.buckets == ("36C", "37C", "38C")
    assert migrated.transition is CoreTransition.FILL_GAP
    assert migrated.direction_transition is DirectionTransition.MIGRATE
    assert neutralized.transition is CoreTransition.FILL_GAP
    assert (
        neutralized.direction_transition
        is DirectionTransition.NEUTRALIZE
    )
    assert migrated == replay


def test_low_high_low_and_direction_round_trip_are_history_independent() -> None:
    low_first = resolve(
        Direction.DOWN,
        stable_center_bucket="36C",
        members=DOWN_MEMBERS,
        probabilities=DOWN_PROBABILITIES,
        proposed_core_buckets=("35C", "36C", "37C"),
    )
    high = resolve(Direction.UP)
    low_again = resolve(
        Direction.DOWN,
        stable_center_bucket="36C",
        members=DOWN_MEMBERS,
        probabilities=DOWN_PROBABILITIES,
        proposed_core_buckets=("35C", "36C", "37C"),
    )
    neutral = resolve(Direction.NEUTRAL)
    down_again = resolve(
        Direction.DOWN,
        stable_center_bucket="36C",
        members=DOWN_MEMBERS,
        probabilities=DOWN_PROBABILITIES,
        proposed_core_buckets=("35C", "36C", "37C"),
    )

    assert low_first == low_again == down_again
    assert high.core_interval.buckets == ("36C", "37C", "38C")
    assert neutral.core_interval.buckets == ("36C", "37C", "38C")
    assert low_again.core_interval.buckets == ("35C", "36C", "37C")


def test_repeated_up_neutral_down_cycles_have_no_hidden_state() -> None:
    sequence = [
        resolve(Direction.UP),
        resolve(Direction.NEUTRAL),
        resolve(
            Direction.DOWN,
            stable_center_bucket="36C",
            members=DOWN_MEMBERS,
            probabilities=DOWN_PROBABILITIES,
            proposed_core_buckets=("35C", "36C", "37C"),
        ),
    ]
    replay = [
        resolve(Direction.UP),
        resolve(Direction.NEUTRAL),
        resolve(
            Direction.DOWN,
            stable_center_bucket="36C",
            members=DOWN_MEMBERS,
            probabilities=DOWN_PROBABILITIES,
            proposed_core_buckets=("35C", "36C", "37C"),
        ),
    ]

    assert sequence == replay
    assert [item.effective_direction for item in sequence] == [
        Direction.UP,
        Direction.NEUTRAL,
        Direction.DOWN,
    ]


def test_stop_adding_and_unknown_order_state_rewind_deterministically() -> None:
    stopped = resolve(Direction.UP, stop_adding=True)
    resumed = resolve(Direction.UP, stop_adding=False)
    unknown = resolve(Direction.UP, order_state_known=False)
    replay = resolve(Direction.UP, stop_adding=False)

    assert stopped.transition is CoreTransition.STOP_ADDING
    assert stopped.positive_inventory_gap == ()
    assert resumed == replay
    assert unknown.resolution_state is CoreResolutionState.HOLD
    assert unknown.reason == "order_state_unknown_reconcile_required"
