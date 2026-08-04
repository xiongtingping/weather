"""Production-candidate pure YES basket planner.

The package is deliberately plan-only.  It contains no order gateway,
database writer, scheduler, or live activation path.
"""

from .runtime import (
    BASKET_STRATEGY_SCHEMA_VERSION,
    BasketStrategyConfig,
    build_basket_yes_plan,
    classify_regime_direction,
    extract_model_taf_inputs,
)
from .probability_adapter import extract_probability_projection
from .snapshot_adapter import build_plan_from_weather_snapshot
from .policy import (
    POLICY_ID,
    POLICY_SCHEMA_VERSION,
    allocate_profit_weighted,
    bucket_roles,
    compute_macro_topup,
    load_policy,
    role_map,
    split_micro_parent_order,
    validate_window_roles,
)
from .settlement_floor_allocator import (
    ALL_ROLES,
    DEFAULT_SETTLEMENT_FLOOR_POLICY_PATH,
    FLOOR_ROLES,
    REFERENCE_FUNDING_RATIOS,
    REBALANCE_PROTECTED_ROLES,
    REBALANCE_SELLABLE_ROLES,
    SETTLEMENT_FLOOR_POLICY_ID,
    SETTLEMENT_FLOOR_POLICY_SCHEMA,
    build_settlement_floor_plan,
    build_role_rebalance_plan,
    load_settlement_floor_policy,
)

__all__ = [
    "BASKET_STRATEGY_SCHEMA_VERSION",
    "BasketStrategyConfig",
    "build_basket_yes_plan",
    "classify_regime_direction",
    "extract_model_taf_inputs",
    "extract_probability_projection",
    "build_plan_from_weather_snapshot",
    "POLICY_ID",
    "POLICY_SCHEMA_VERSION",
    "allocate_profit_weighted",
    "bucket_roles",
    "compute_macro_topup",
    "load_policy",
    "role_map",
    "split_micro_parent_order",
    "validate_window_roles",
    "ALL_ROLES",
    "DEFAULT_SETTLEMENT_FLOOR_POLICY_PATH",
    "FLOOR_ROLES",
    "REFERENCE_FUNDING_RATIOS",
    "REBALANCE_PROTECTED_ROLES",
    "REBALANCE_SELLABLE_ROLES",
    "SETTLEMENT_FLOOR_POLICY_ID",
    "SETTLEMENT_FLOOR_POLICY_SCHEMA",
    "build_settlement_floor_plan",
    "build_role_rebalance_plan",
    "load_settlement_floor_policy",
]
