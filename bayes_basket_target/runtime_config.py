"""Target-owned runtime configuration for the production-no-live worker.

The target worker must never infer a budget from the resident service config
or from a shared capital pool.  This file defines the explicit no-live
runtime contract; the checked-in target-owned amount is authoritative, while
the parser's zero fallback prevents an incomplete config from silently
borrowing capital.
"""

from __future__ import annotations

import os

import math
from pathlib import Path
from typing import Any, Mapping

from .resource_bounds import load_bounded_json


ROOT = Path(__file__).resolve().parents[1]
TARGET_CONFIG_ROOT = (ROOT / "config" / "bayes_basket_target").resolve()
DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG = TARGET_CONFIG_ROOT / "no_live_runtime_v1.json"
SCHEMA = "bayes_basket_target_no_live_runtime_v1"
DEFAULT_TARGET_REMAINING_BUDGET_USDC = 0.0


class TargetRuntimeConfigError(ValueError):
    """Raised when the target-owned no-live runtime contract is invalid."""


def _nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TargetRuntimeConfigError(f"{name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TargetRuntimeConfigError(f"{name}_invalid") from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise TargetRuntimeConfigError(f"{name}_invalid")
    return parsed


def load_target_no_live_runtime_config(
    path: Path | str = DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG,
) -> dict[str, Any]:
    """Load the target-only no-live budget without reading resident config."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TargetRuntimeConfigError("target_runtime_config_symlink_forbidden")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(TARGET_CONFIG_ROOT)
    except ValueError as exc:
        raise TargetRuntimeConfigError("target_runtime_config_outside_target_config") from exc

    raw = load_bounded_json(resolved)
    if not isinstance(raw, Mapping):
        raise TargetRuntimeConfigError("target_runtime_config_not_object")
    if raw.get("schema") != SCHEMA:
        raise TargetRuntimeConfigError("target_runtime_config_schema_invalid")
    if raw.get("state") != "ENABLED_FOR_PRODUCTION_NO_LIVE":
        raise TargetRuntimeConfigError("target_runtime_config_state_invalid")
    if raw.get("execution_mode") != "production_no_live":
        raise TargetRuntimeConfigError("target_runtime_config_execution_mode_invalid")
    if raw.get("target_only") is not True:
        raise TargetRuntimeConfigError("target_runtime_config_target_only_required")
    if raw.get("capital_scope") != "target_owned_only":
        raise TargetRuntimeConfigError("target_runtime_config_capital_scope_invalid")
    for key in (
        "live_submit_authorized",
        "places_orders",
        "writes_production_db",
        "calls_order_gateway",
        "reads_shared_live_capital",
    ):
        if raw.get(key) is not False:
            raise TargetRuntimeConfigError(f"target_runtime_config_{key}_boundary_invalid")

    budget = _nonnegative_number(
        raw.get("target_remaining_budget_usdc", DEFAULT_TARGET_REMAINING_BUDGET_USDC),
        "target_remaining_budget_usdc",
    )
    return {
        "schema": SCHEMA,
        "state": str(raw["state"]),
        "execution_mode": str(raw["execution_mode"]),
        "target_only": True,
        "capital_scope": "target_owned_only",
        "target_remaining_budget_usdc": budget,
        "source": str(resolved),
        "live_submit_authorized": False,
        "places_orders": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "reads_shared_live_capital": False,
    }


LIVE_SCHEMA = "bayes_basket_target_live_runtime_v1"
DEFAULT_TARGET_LIVE_RUNTIME_CONFIG = TARGET_CONFIG_ROOT / "live_runtime_v1.json"


def load_target_live_runtime_config(
    path: Path | str = DEFAULT_TARGET_LIVE_RUNTIME_CONFIG,
) -> dict[str, Any]:
    """The live budget contract, loaded by its own function.

    ``load_target_no_live_runtime_config`` above returns the five live
    boundaries as literal ``False`` regardless of what the file says, which is
    why editing that JSON could never produce a live runtime and why this is a
    separate function rather than a flag on that one.  Here the three that an
    order needs are literal ``True`` for the same reason in reverse: the file
    declares intent, the loader decides what the lane is, and neither can be
    talked out of its answer by a typo.

    ``writes_production_db`` and ``reads_shared_live_capital`` stay false in
    both.  Going live authorises orders, not a second writer and not the
    shared capital pool -- the target lane funds itself.
    """

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TargetRuntimeConfigError("target_runtime_config_symlink_forbidden")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(TARGET_CONFIG_ROOT)
    except ValueError as exc:
        raise TargetRuntimeConfigError(
            "target_runtime_config_outside_target_config"
        ) from exc

    raw = load_bounded_json(resolved)
    if not isinstance(raw, Mapping):
        raise TargetRuntimeConfigError("target_runtime_config_not_object")
    if raw.get("schema") != LIVE_SCHEMA:
        raise TargetRuntimeConfigError("live_runtime_config_schema_invalid")
    if raw.get("state") != "ENABLED_FOR_PRODUCTION_LIVE":
        raise TargetRuntimeConfigError("live_runtime_config_state_invalid")
    if raw.get("execution_mode") != "production_live":
        raise TargetRuntimeConfigError("live_runtime_config_execution_mode_invalid")
    if raw.get("target_only") is not True:
        raise TargetRuntimeConfigError("live_runtime_config_target_only_required")
    if raw.get("capital_scope") != "target_owned_only":
        raise TargetRuntimeConfigError("live_runtime_config_capital_scope_invalid")
    for key in ("live_submit_authorized", "places_orders", "calls_order_gateway"):
        if raw.get(key) is not True:
            raise TargetRuntimeConfigError(f"live_runtime_config_{key}_required")
    for key in ("writes_production_db", "reads_shared_live_capital"):
        if raw.get(key) is not False:
            raise TargetRuntimeConfigError(f"live_runtime_config_{key}_boundary_invalid")

    budget = _nonnegative_number(
        raw.get("target_remaining_budget_usdc", DEFAULT_TARGET_REMAINING_BUDGET_USDC),
        "target_remaining_budget_usdc",
    )
    if budget <= 0.0:
        # A live lane with a zero budget is not a safety state, it is a
        # silently dead one: every cycle would compute a plan and size it to
        # nothing.  Refuse, so the operator sets a number on purpose.
        raise TargetRuntimeConfigError("live_runtime_config_budget_must_be_positive")
    return {
        "schema": LIVE_SCHEMA,
        "state": str(raw["state"]),
        "execution_mode": str(raw["execution_mode"]),
        "target_only": True,
        "capital_scope": "target_owned_only",
        "target_remaining_budget_usdc": budget,
        "source": str(resolved),
        "live_submit_authorized": True,
        "places_orders": True,
        "calls_order_gateway": True,
        "writes_production_db": False,
        "reads_shared_live_capital": False,
    }


__all__ = [
    "DEFAULT_TARGET_LIVE_RUNTIME_CONFIG",
    "DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG",
    "LIVE_SCHEMA",
    "load_target_live_runtime_config",
    "DEFAULT_TARGET_REMAINING_BUDGET_USDC",
    "SCHEMA",
    "TargetRuntimeConfigError",
    "load_target_no_live_runtime_config",
]


def resolve_target_remaining_budget(*, execution_mode: str) -> float:
    """The target lane's remaining-budget dial, from one place.

    An explicit ``PM_TARGET_REMAINING_BUDGET_USDC`` is a controlled
    rehearsal's override; otherwise the live runtime config under
    ``production_live`` and the no-live config under any other mode.  The
    resident daemon config and activation ``basket`` fields are never a
    fallback: that could borrow a live-line bankroll into the target lane.
    Until 2026-09-17 the worker resolved this with the override and the
    account read in ``production_io`` without it -- two answers to one dial.
    """

    raw_budget = os.environ.get("PM_TARGET_REMAINING_BUDGET_USDC", "").strip()
    if raw_budget:
        try:
            remaining_budget = float(raw_budget)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("target_remaining_budget_usdc_invalid") from exc
        if not math.isfinite(remaining_budget) or remaining_budget < 0.0:
            raise RuntimeError("target_remaining_budget_usdc_invalid")
        return remaining_budget
    if str(execution_mode) == "production_live":
        return float(
            load_target_live_runtime_config()["target_remaining_budget_usdc"]
        )
    return float(
        load_target_no_live_runtime_config(DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG)[
            "target_remaining_budget_usdc"
        ]
    )
