"""Strict validators for target-lane promotion evidence.

These validators are deliberately narrower than the production compiler.  They
only answer whether a separately produced, content-addressed scorecard has the
minimum shape required by M8-12/M8-13.  They never promote an artifact, write
SQLite, call a gateway, or turn a source-wallet replay into target evidence.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .contracts import ContractError


CASH_SCORECARD_SCHEMA = "bayes_basket_target_cash_phase_forward_scorecard_v1"
STATION_SCORECARD_SCHEMA = "bayes_basket_target_station_policy_forward_scorecard_v1"


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result <= 0:
        raise ContractError(f"{name}_must_be_positive")
    return result


def _nonnegative_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result < 0:
        raise ContractError(f"{name}_must_be_nonnegative")
    return result


def _zero(name: str, value: Any) -> None:
    if isinstance(value, bool) or int(value) != 0:
        raise ContractError(f"{name}_must_be_zero")


def _true(name: str, value: Any) -> None:
    if value is not True:
        raise ContractError(f"{name}_must_be_true")


def _target_no_live_boundary(raw: Mapping[str, Any]) -> None:
    _true("production_no_live", raw.get("production_no_live"))
    if raw.get("live_order_authorized") is not False:
        raise ContractError("live_order_authorized_must_be_false")
    if raw.get("writes_production_db") is not False:
        raise ContractError("writes_production_db_must_be_false")
    if raw.get("calls_order_gateway") is not False:
        raise ContractError("calls_order_gateway_must_be_false")
    _zero("orders_submitted", raw.get("orders_submitted"))


def _required_checks(raw: Mapping[str, Any], names: Sequence[str]) -> dict[str, bool]:
    checks = _mapping("scorecard_checks", raw.get("checks"))
    result: dict[str, bool] = {}
    for name in names:
        value = checks.get(name)
        if not isinstance(value, bool):
            raise ContractError(f"scorecard_check_{name}_must_be_boolean")
        result[name] = value
    return result


CASH_REQUIRED_CHECKS = (
    "target_only",
    "strict_asof",
    "price_coverage_complete",
    "candidate_fill_scorecard_complete",
    "pnl_attribution_complete",
    "fault_scorecard_complete",
    "phase_monotonic",
    "cash_phase_does_not_change_weather_plan",
)


def validate_cash_phase_forward_scorecard(
    raw: Mapping[str, Any],
    *,
    expected_policy_version: str | None = None,
) -> dict[str, Any]:
    """Validate a complete self-owned cash-phase forward scorecard.

    The checks are intentionally structural as well as boolean.  A packet
    cannot pass by setting flags to ``true`` while omitting observations,
    phase coverage, attribution, or fault cases.
    """

    raw = _mapping("cash_phase_scorecard", raw)
    if raw.get("schema") != CASH_SCORECARD_SCHEMA:
        raise ContractError("cash_phase_scorecard_schema_invalid")
    _target_no_live_boundary(raw)
    policy_version = _text("policy_version", raw.get("policy_version"))
    if expected_policy_version and policy_version != expected_policy_version:
        raise ContractError("cash_phase_scorecard_policy_version_mismatch")
    checks = _required_checks(raw, CASH_REQUIRED_CHECKS)

    scope = _mapping("cash_phase_scope", raw.get("scope"))
    for key in ("window_start_utc", "window_end_utc"):
        _text(f"cash_phase_scope_{key}", scope.get(key))
    for key in ("station_count", "event_count", "decision_count"):
        _positive_int(f"cash_phase_scope_{key}", scope.get(key))

    phase_observations = _mapping("cash_phase_observations", raw.get("phase_observations"))
    for phase in ("HEAVY", "STANDARD", "TAIL"):
        row = _mapping(f"cash_phase_{phase.lower()}_observation", phase_observations.get(phase))
        _true(f"cash_phase_{phase.lower()}_observed", row.get("observed"))
        _positive_int(f"cash_phase_{phase.lower()}_count", row.get("count"))

    coverage = _mapping("cash_phase_coverage", raw.get("coverage"))
    coverage_keys = ("price", "candidate_fill", "pnl_attribution")
    for key in coverage_keys:
        row = _mapping(f"cash_phase_{key}_coverage", coverage.get(key))
        _true(f"cash_phase_{key}_coverage_complete", row.get("complete"))
        _positive_int(f"cash_phase_{key}_sample_count", row.get("sample_count"))
    faults = _mapping("cash_phase_fault_scorecard", raw.get("fault_scorecard"))
    _positive_int("cash_phase_fault_case_count", faults.get("case_count"))
    _true("cash_phase_fault_cases_pass", faults.get("all_boundary_cases_pass"))
    pnl = _mapping("cash_phase_pnl_attribution", raw.get("pnl_attribution"))
    settled = _positive_int("cash_phase_settled_event_count", pnl.get("settled_event_count"))
    attributed = _nonnegative_int("cash_phase_attributed_event_count", pnl.get("attributed_event_count"))
    if attributed != settled:
        raise ContractError("cash_phase_pnl_attribution_incomplete")

    passed = all(checks.values())
    return {
        "schema": CASH_SCORECARD_SCHEMA,
        "valid": passed,
        "policy_version": policy_version,
        "checks": checks,
        "scope": dict(scope),
        "phase_observations": {key: dict(value) for key, value in phase_observations.items()},
        "coverage": {key: dict(value) for key, value in coverage.items()},
        "fault_scorecard": dict(faults),
        "pnl_attribution": dict(pnl),
    }


STATION_REQUIRED_CHECKS = (
    "target_only",
    "strict_asof_et",
    "four_seasons",
    "four_windows",
    "candidate_scope_matches",
    "all_station_family_scores",
    "random_baseline_intervals",
    "forward_shadow_and_pnl_attribution",
    "target_final_audit_fallback_absent",
)
STATION_FAMILIES = ("target", "reserve", "cadence")
STATION_FAMILY_SLICES = (
    "same_station_time_oos",
    "leave_one_station_out",
    "cross_season",
    "windows_3_7_15_30",
    "per_station_baseline_and_interval",
    "forward_shadow_and_pnl",
)


def validate_station_policy_forward_scorecard(
    raw: Mapping[str, Any],
    *,
    expected_station_ids: Sequence[str],
    expected_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate all three family-specific station-policy OOS scorecards."""

    raw = _mapping("station_policy_scorecard", raw)
    if raw.get("schema") != STATION_SCORECARD_SCHEMA:
        raise ContractError("station_policy_scorecard_schema_invalid")
    _target_no_live_boundary(raw)
    bundle_sha = _text("candidate_bundle_sha256", raw.get("candidate_bundle_sha256"))
    if expected_bundle_sha256 and bundle_sha != expected_bundle_sha256:
        raise ContractError("station_policy_scorecard_bundle_mismatch")
    checks = _required_checks(raw, STATION_REQUIRED_CHECKS)
    expected = tuple(sorted({_text("expected_station_id", value) for value in expected_station_ids}))
    scope = _mapping("station_policy_scorecard_scope", raw.get("scope"))
    actual_ids = tuple(sorted({_text("scorecard_station_id", value) for value in (scope.get("station_ids") or [])}))
    if actual_ids != expected:
        raise ContractError("station_policy_scorecard_station_scope_mismatch")
    if _positive_int("station_policy_scorecard_station_count", scope.get("station_count")) != len(expected):
        raise ContractError("station_policy_scorecard_station_count_mismatch")
    if _positive_int("station_policy_scorecard_season_count", scope.get("season_count")) < 4:
        raise ContractError("station_policy_scorecard_four_seasons_required")
    if _positive_int("station_policy_scorecard_window_count", scope.get("window_count")) != 4:
        raise ContractError("station_policy_scorecard_four_windows_required")
    for key in ("window_start_utc", "window_end_utc"):
        _text(f"station_policy_scorecard_{key}", scope.get(key))

    families_raw = _mapping("station_policy_scorecard_families", raw.get("families"))
    families: dict[str, dict[str, Any]] = {}
    for family in STATION_FAMILIES:
        family_raw = _mapping(f"station_policy_family_{family}", families_raw.get(family))
        slices: dict[str, Any] = {}
        for slice_name in STATION_FAMILY_SLICES:
            row = _mapping(f"station_policy_{family}_{slice_name}", family_raw.get(slice_name))
            _true(f"station_policy_{family}_{slice_name}_complete", row.get("complete"))
            _positive_int(f"station_policy_{family}_{slice_name}_sample_count", row.get("sample_count"))
            slices[slice_name] = dict(row)
        families[family] = slices

    passed = all(checks.values())
    return {
        "schema": STATION_SCORECARD_SCHEMA,
        "valid": passed,
        "candidate_bundle_sha256": bundle_sha,
        "checks": checks,
        "scope": dict(scope),
        "families": families,
    }


__all__ = [
    "CASH_SCORECARD_SCHEMA",
    "STATION_SCORECARD_SCHEMA",
    "validate_cash_phase_forward_scorecard",
    "validate_station_policy_forward_scorecard",
]
