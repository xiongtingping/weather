"""Deterministic basket lifecycle semantics for one target checkpoint.

Probability chooses the desired support and DeltaBasketScore authorizes the
complete execution-gap bundle.  This module only names the resulting basket
lifecycle step; it cannot change probability, support, target inventory, or
execution authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import BasketState, ContractError, content_sha256


@dataclass(frozen=True)
class BasketStateResolution:
    state: BasketState
    reason: str
    desired_support: tuple[str, ...]
    selected_before: tuple[str, ...]
    added_buckets: tuple[str, ...]
    retired_buckets: tuple[str, ...]
    positive_gap_buckets: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "desired_support": list(self.desired_support),
            "selected_before": list(self.selected_before),
            "added_buckets": list(self.added_buckets),
            "retired_buckets": list(self.retired_buckets),
            "positive_gap_buckets": list(self.positive_gap_buckets),
        }


def _ordered_unique(
    name: str,
    values: Sequence[str],
    bucket_ordinals: Mapping[str, int],
) -> tuple[str, ...]:
    parsed = tuple(str(value or "").strip() for value in values)
    if any(not value for value in parsed):
        raise ContractError(f"{name}_bucket_required")
    if len(set(parsed)) != len(parsed):
        raise ContractError(f"{name}_bucket_duplicate")
    unknown = sorted(set(parsed) - set(bucket_ordinals))
    if unknown:
        raise ContractError(
            f"{name}_bucket_unknown:" + ",".join(unknown)
        )
    return tuple(sorted(parsed, key=lambda item: (bucket_ordinals[item], item)))


def resolve_basket_state(
    *,
    bucket_ordinals: Mapping[str, int],
    desired_support: Sequence[str],
    selected_before: Sequence[str],
    positive_gap_buckets: Sequence[str],
    execution_authorized: bool,
    order_state_known: bool,
    stop_adding: bool,
) -> BasketStateResolution:
    """Resolve SEED/EXTEND/FILL/REPLENISH/HOLD/STOP without hidden state."""

    if not bucket_ordinals:
        raise ContractError("basket_state_bucket_contract_empty")
    if any(
        not isinstance(bucket, str)
        or not bucket
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        for bucket, ordinal in bucket_ordinals.items()
    ):
        raise ContractError("basket_state_bucket_contract_invalid")
    if len(set(bucket_ordinals.values())) != len(bucket_ordinals):
        raise ContractError("basket_state_bucket_ordinal_duplicate")

    desired = _ordered_unique(
        "basket_state_desired_support",
        desired_support,
        bucket_ordinals,
    )
    previous = _ordered_unique(
        "basket_state_selected_before",
        selected_before,
        bucket_ordinals,
    )
    gaps = _ordered_unique(
        "basket_state_positive_gap",
        positive_gap_buckets,
        bucket_ordinals,
    )
    if set(gaps) - set(desired):
        raise ContractError("basket_state_gap_outside_desired_support")

    added = tuple(bucket for bucket in desired if bucket not in previous)
    retired = tuple(bucket for bucket in previous if bucket not in desired)

    if stop_adding:
        state, reason = BasketState.STOP, "terminal_stop_adding"
    elif not order_state_known:
        state, reason = BasketState.HOLD, "order_state_unknown_reconcile_required"
    elif not gaps:
        state, reason = BasketState.HOLD, "target_inventory_satisfied"
    elif not execution_authorized:
        state, reason = BasketState.HOLD, "complete_gap_bundle_not_authorized"
    elif not previous:
        state, reason = BasketState.SEED, "initial_complete_support_build"
    elif added:
        previous_lower = min(bucket_ordinals[bucket] for bucket in previous)
        previous_upper = max(bucket_ordinals[bucket] for bucket in previous)
        lower = tuple(
            bucket for bucket in added
            if bucket_ordinals[bucket] < previous_lower
        )
        upper = tuple(
            bucket for bucket in added
            if bucket_ordinals[bucket] > previous_upper
        )
        interior = tuple(
            bucket for bucket in added
            if previous_lower <= bucket_ordinals[bucket] <= previous_upper
        )
        if lower and not upper and not interior:
            state, reason = BasketState.EXTEND_LOW, "support_extended_lower"
        elif upper and not lower and not interior:
            state, reason = BasketState.EXTEND_HIGH, "support_extended_upper"
        else:
            state, reason = BasketState.FILL_GAP, "support_gap_or_two_sided_change"
    else:
        state, reason = BasketState.REPLENISH, "existing_support_positive_gap"

    return BasketStateResolution(
        state=state,
        reason=reason,
        desired_support=desired,
        selected_before=previous,
        added_buckets=added,
        retired_buckets=retired,
        positive_gap_buckets=gaps,
    )


def basket_lifecycle_projection(
    plan: Mapping[str, Any],
    *,
    required: bool = True,
) -> dict[str, Any] | None:
    """Return the sole persisted lifecycle projection from a runtime plan."""

    raw_state = str(plan.get("basket_state") or "").strip()
    raw_reason = str(plan.get("basket_state_reason") or "").strip()
    transition = plan.get("basket_state_transition")
    if not raw_state and not raw_reason and transition is None and not required:
        return None
    try:
        state = BasketState(raw_state)
    except ValueError as exc:
        raise ContractError("basket_lifecycle_state_invalid") from exc
    if not raw_reason:
        raise ContractError("basket_lifecycle_reason_missing")
    if not isinstance(transition, Mapping):
        raise ContractError("basket_lifecycle_transition_mapping_required")
    transition_copy = dict(transition)
    if (
        str(transition_copy.get("state") or "") != state.value
        or str(transition_copy.get("reason") or "") != raw_reason
    ):
        raise ContractError("basket_lifecycle_transition_drift")
    desired_support = list(transition_copy.get("desired_support") or ())
    if desired_support != list(plan.get("final_support") or ()):
        raise ContractError("basket_lifecycle_support_drift")
    identity = {
        "schema": "basket_lifecycle_projection_v1",
        "authority": "delta_v2_plan",
        "state": state.value,
        "reason": raw_reason,
        "transition": transition_copy,
        "candidate_support": list(plan.get("candidate_support") or ()),
        "final_support": desired_support,
        "snapshot_sha256": str(plan.get("snapshot_sha256") or ""),
    }
    identity["support_version_sha256"] = content_sha256(identity)
    return identity


__all__ = [
    "BasketStateResolution",
    "basket_lifecycle_projection",
    "resolve_basket_state",
]
