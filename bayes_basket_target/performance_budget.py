"""Bounded performance budget for the isolated target no-live lane.

This is an engineering budget for read-only/no-live evidence work.  It is not
an order, capital, service-cutover, or live-submit authorization.  Keeping the
contract in a target-owned module prevents the audit from silently treating a
missing number as an approval or borrowing limits from the resident line.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from .resource_bounds import (
    MAX_TARGET_DB_BYTES,
    MAX_TARGET_PROCESS_RSS_BYTES,
    MAX_TARGET_RESEARCH_RAW_BYTES,
    MAX_TARGET_WAL_BYTES,
    MIN_TARGET_FREE_BYTES,
    load_bounded_json,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PERFORMANCE_BUDGET = ROOT / "config" / "bayes_basket_target" / "performance_budget_v1.json"
SCHEMA = "bayes_basket_target_performance_budget_v1"


class PerformanceBudgetError(ValueError):
    """Raised when the target-only performance budget is invalid."""


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise PerformanceBudgetError(f"{name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PerformanceBudgetError(f"{name}_invalid") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise PerformanceBudgetError(f"{name}_invalid")
    return parsed


def _positive_int(value: Any, name: str) -> int:
    parsed = _positive_number(value, name)
    if parsed != int(parsed):
        raise PerformanceBudgetError(f"{name}_invalid")
    return int(parsed)


def load_performance_budget(path: Path | str = DEFAULT_PERFORMANCE_BUDGET) -> dict[str, Any]:
    """Load and validate a target-owned no-live performance budget."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise PerformanceBudgetError("performance_budget_symlink_forbidden")
    resolved = candidate.resolve()
    try:
        resolved.relative_to((ROOT / "config" / "bayes_basket_target").resolve())
    except ValueError as exc:
        raise PerformanceBudgetError("performance_budget_outside_target_config") from exc

    raw = load_bounded_json(resolved)
    if not isinstance(raw, Mapping):
        raise PerformanceBudgetError("performance_budget_not_object")
    if raw.get("schema") != SCHEMA:
        raise PerformanceBudgetError("performance_budget_schema_invalid")
    if raw.get("state") != "DECLARED_FOR_NO_LIVE_AUDIT":
        raise PerformanceBudgetError("performance_budget_state_invalid")
    if raw.get("lane") != "bayes_basket_target":
        raise PerformanceBudgetError("performance_budget_lane_invalid")
    if raw.get("execution_mode") != "production_no_live":
        raise PerformanceBudgetError("performance_budget_execution_mode_invalid")
    scope = raw.get("approval_scope")
    if scope != "bounded_read_only_audit_only":
        raise PerformanceBudgetError("performance_budget_scope_invalid")
    for key in ("live_submit_authorized", "places_orders", "writes_production_db", "calls_order_gateway"):
        if raw.get(key) is not False:
            raise PerformanceBudgetError(f"performance_budget_{key}_boundary_invalid")

    limits = raw.get("limits")
    if not isinstance(limits, Mapping):
        raise PerformanceBudgetError("performance_budget_limits_missing")
    numeric_limits = {
        "runtime_audit_p95_ms": _positive_number(limits.get("runtime_audit_p95_ms"), "runtime_audit_p95_ms"),
        "runtime_audit_p99_ms": _positive_number(limits.get("runtime_audit_p99_ms"), "runtime_audit_p99_ms"),
        "resource_audit_p95_ms": _positive_number(limits.get("resource_audit_p95_ms"), "resource_audit_p95_ms"),
        "resource_audit_p99_ms": _positive_number(limits.get("resource_audit_p99_ms"), "resource_audit_p99_ms"),
        "max_ws_snapshot_age_sec": _positive_number(limits.get("max_ws_snapshot_age_sec"), "max_ws_snapshot_age_sec"),
        "max_writer_lock_wait_ms": _positive_number(limits.get("max_writer_lock_wait_ms"), "max_writer_lock_wait_ms"),
        "max_process_rss_bytes": _positive_int(limits.get("max_process_rss_bytes"), "max_process_rss_bytes"),
        "max_database_bytes": _positive_int(limits.get("max_database_bytes"), "max_database_bytes"),
        "max_wal_bytes": _positive_int(limits.get("max_wal_bytes"), "max_wal_bytes"),
        "max_raw_bytes": _positive_int(limits.get("max_raw_bytes"), "max_raw_bytes"),
        "min_free_bytes": _positive_int(limits.get("min_free_bytes"), "min_free_bytes"),
    }
    if numeric_limits["runtime_audit_p99_ms"] < numeric_limits["runtime_audit_p95_ms"]:
        raise PerformanceBudgetError("performance_budget_runtime_percentiles_invalid")
    if numeric_limits["resource_audit_p99_ms"] < numeric_limits["resource_audit_p95_ms"]:
        raise PerformanceBudgetError("performance_budget_resource_percentiles_invalid")
    hard_limits = {
        "max_process_rss_bytes": MAX_TARGET_PROCESS_RSS_BYTES,
        "max_database_bytes": MAX_TARGET_DB_BYTES,
        "max_wal_bytes": MAX_TARGET_WAL_BYTES,
        "max_raw_bytes": MAX_TARGET_RESEARCH_RAW_BYTES,
        "min_free_bytes": MIN_TARGET_FREE_BYTES,
    }
    for key, hard_limit in hard_limits.items():
        if key == "min_free_bytes":
            if numeric_limits[key] < hard_limit:
                raise PerformanceBudgetError(f"performance_budget_{key}_below_hard_floor")
        elif numeric_limits[key] > hard_limit:
            raise PerformanceBudgetError(f"performance_budget_{key}_above_hard_limit")

    return {
        "schema": SCHEMA,
        "state": str(raw["state"]),
        "lane": str(raw["lane"]),
        "execution_mode": str(raw["execution_mode"]),
        "approval_scope": str(raw["approval_scope"]),
        "source": str(resolved),
        "effective_date": str(raw.get("effective_date") or ""),
        "limits": numeric_limits,
        "live_submit_authorized": False,
        "places_orders": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
    }


__all__ = ["DEFAULT_PERFORMANCE_BUDGET", "PerformanceBudgetError", "SCHEMA", "load_performance_budget"]
