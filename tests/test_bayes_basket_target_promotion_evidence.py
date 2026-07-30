from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.promotion_evidence import (
    validate_cash_phase_forward_scorecard,
    validate_station_policy_forward_scorecard,
)


def _cash_scorecard() -> dict[str, object]:
    return {
        "schema": "bayes_basket_target_cash_phase_forward_scorecard_v1",
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "policy_version": "cash-v1",
        "scope": {
            "window_start_utc": "2026-07-24T00:00:00Z",
            "window_end_utc": "2026-07-25T00:00:00Z",
            "station_count": 1,
            "event_count": 1,
            "decision_count": 3,
        },
        "phase_observations": {
            phase: {"observed": True, "count": 1}
            for phase in ("HEAVY", "STANDARD", "TAIL")
        },
        "coverage": {
            key: {"complete": True, "sample_count": 1}
            for key in ("price", "candidate_fill", "pnl_attribution")
        },
        "fault_scorecard": {"case_count": 1, "all_boundary_cases_pass": True},
        "pnl_attribution": {"settled_event_count": 1, "attributed_event_count": 1},
        "checks": {
            "target_only": True,
            "strict_asof": True,
            "price_coverage_complete": True,
            "candidate_fill_scorecard_complete": True,
            "pnl_attribution_complete": True,
            "fault_scorecard_complete": True,
            "phase_monotonic": True,
            "cash_phase_does_not_change_weather_plan": True,
        },
    }


def _station_scorecard() -> dict[str, object]:
    families: dict[str, object] = {}
    for family in ("target", "reserve", "cadence"):
        families[family] = {
            slice_name: {"complete": True, "sample_count": 1}
            for slice_name in (
                "same_station_time_oos",
                "leave_one_station_out",
                "cross_season",
                "windows_3_7_15_30",
                "per_station_baseline_and_interval",
                "forward_shadow_and_pnl",
            )
        }
    return {
        "schema": "bayes_basket_target_station_policy_forward_scorecard_v1",
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "candidate_bundle_sha256": "a" * 64,
        "scope": {
            "station_ids": ["S1", "S2"],
            "station_count": 2,
            "season_count": 4,
            "window_count": 4,
            "window_start_utc": "2026-01-01T00:00:00Z",
            "window_end_utc": "2026-07-25T00:00:00Z",
        },
        "families": families,
        "checks": {
            "target_only": True,
            "strict_asof_et": True,
            "four_seasons": True,
            "four_windows": True,
            "candidate_scope_matches": True,
            "all_station_family_scores": True,
            "random_baseline_intervals": True,
            "forward_shadow_and_pnl_attribution": True,
            "target_final_audit_fallback_absent": True,
        },
    }


def test_cash_scorecard_requires_all_phases_and_self_owned_evidence() -> None:
    result = validate_cash_phase_forward_scorecard(_cash_scorecard())
    assert result["valid"] is True
    broken = _cash_scorecard()
    broken["phase_observations"] = dict(broken["phase_observations"])  # type: ignore[index]
    broken["phase_observations"]["HEAVY"] = {"observed": False, "count": 0}  # type: ignore[index]
    with pytest.raises(ContractError, match="cash_phase_heavy_observed_must_be_true"):
        validate_cash_phase_forward_scorecard(broken)


def test_cash_scorecard_rejects_fake_boolean_pass_without_samples() -> None:
    broken = _cash_scorecard()
    broken["pnl_attribution"] = {"settled_event_count": 1, "attributed_event_count": 0}
    with pytest.raises(ContractError, match="cash_phase_pnl_attribution_incomplete"):
        validate_cash_phase_forward_scorecard(broken)


def test_station_scorecard_requires_exact_candidate_scope_and_all_three_families() -> None:
    result = validate_station_policy_forward_scorecard(
        _station_scorecard(),
        expected_station_ids=("S1", "S2"),
        expected_bundle_sha256="a" * 64,
    )
    assert result["valid"] is True
    broken = _station_scorecard()
    broken["scope"] = {**broken["scope"], "station_ids": ["S1"]}  # type: ignore[index]
    with pytest.raises(ContractError, match="station_policy_scorecard_station_scope_mismatch"):
        validate_station_policy_forward_scorecard(
            broken,
            expected_station_ids=("S1", "S2"),
            expected_bundle_sha256="a" * 64,
        )


def test_station_scorecard_cannot_cross_bundle_lineage() -> None:
    with pytest.raises(ContractError, match="station_policy_scorecard_bundle_mismatch"):
        validate_station_policy_forward_scorecard(
            _station_scorecard(),
            expected_station_ids=("S1", "S2"),
            expected_bundle_sha256="b" * 64,
        )
