"""Executable trade-exit reason contract.

This module is the code SSOT for reason-family classification.  A prefix is
never authority.  Market-alpha is accepted by documentation but remains
default-deny until its separate activation contract is implemented.
"""

from __future__ import annotations

from typing import Any, FrozenSet


TARGET_REALIZATION_FAMILY = "target_realization"
MARKET_ALPHA_TAKE_PROFIT_FAMILY = "market_alpha_take_profit"

TARGET_REALIZATION_EXIT_REASON_CODES: FrozenSet[str] = frozenset(
    {
        # Current v3 canonical invalidation semantics.
        "target_realization_yes_target_invalidated",
        "target_realization_no_invalidated",
        # Current dynamic three-leg semantics.
        "target_realization_top_yes_invalidated",
        "target_realization_second_yes_invalidated",
        "target_realization_hard_zero_no_invalidated",
        "target_realization_settled",
    }
)

MARKET_ALPHA_TAKE_PROFIT_REASON_CODES: FrozenSet[str] = frozenset(
    {"market_alpha_take_profit_top_yes_wave_completed"}
)

# Target Realization v6 has its own generation-bound exact closed set.  These
# codes are deliberately not unioned into the current production predicates:
# the v6 Transaction C writer calls the generation-specific functions below,
# and the managed cutover changes generation authority atomically.
V6_TARGET_REALIZATION_EXIT_REASON_CODES: FrozenSet[str] = frozenset(
    {
        "target_realization_main_yes_upside_risk_stage_1",
        "target_realization_main_yes_upside_risk_stage_2",
        "target_realization_main_yes_upside_risk_stage_3",
        "target_realization_main_yes_upside_risk_stage_4",
        "target_realization_main_yes_invalidated",
    }
)

V6_MARKET_ALPHA_TAKE_PROFIT_REASON_CODES: FrozenSet[str] = frozenset(
    {
        "market_alpha_take_profit_main_yes_999",
        "market_alpha_take_profit_second_yes_999",
        "market_alpha_take_profit_trajectory_no_999",
        "market_alpha_take_profit_hard_exclusion_no_999",
    }
)

# Documentation acceptance is not live activation.  Do not replace this with
# an environment toggle; activation requires the dedicated schema, manifest,
# fault-injection packet, and an accepted cutover decision.
MARKET_ALPHA_TAKE_PROFIT_RUNTIME_ENABLED = False


def normalize_exit_reason_code(value: Any) -> str:
    return str(value or "").strip().lower()


def exit_reason_family_for_code(value: Any) -> str:
    code = normalize_exit_reason_code(value)
    if code in TARGET_REALIZATION_EXIT_REASON_CODES:
        return TARGET_REALIZATION_FAMILY
    if code in MARKET_ALPHA_TAKE_PROFIT_REASON_CODES:
        return MARKET_ALPHA_TAKE_PROFIT_FAMILY
    return ""


def is_executable_exit_reason_code(value: Any) -> bool:
    code = normalize_exit_reason_code(value)
    if code in TARGET_REALIZATION_EXIT_REASON_CODES:
        return True
    return bool(
        MARKET_ALPHA_TAKE_PROFIT_RUNTIME_ENABLED
        and code in MARKET_ALPHA_TAKE_PROFIT_REASON_CODES
    )


def v6_exit_reason_family_for_code(value: Any) -> str:
    code = normalize_exit_reason_code(value)
    if code in V6_TARGET_REALIZATION_EXIT_REASON_CODES:
        return TARGET_REALIZATION_FAMILY
    if code in V6_MARKET_ALPHA_TAKE_PROFIT_REASON_CODES:
        return MARKET_ALPHA_TAKE_PROFIT_FAMILY
    return ""


def is_v6_executable_exit_reason_code(value: Any) -> bool:
    """Return membership only; v6 activation remains a separate hard gate."""

    code = normalize_exit_reason_code(value)
    return bool(
        code in V6_TARGET_REALIZATION_EXIT_REASON_CODES
        or code in V6_MARKET_ALPHA_TAKE_PROFIT_REASON_CODES
    )
