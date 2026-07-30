from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.model_curve_schedule import aligned_target_sample_offsets
from bayes_basket_target.settlement_time import (
    assert_probability_timezone_matches_market,
    historical_registry_scope_settlement_timezone,
    normalize_event_clock_utc,
    operational_day_at,
    registry_scope_settlement_timezone,
    registry_settlement_timezone,
    resolution_local_date_at,
    settlement_day_window,
    time_contract_snapshot,
)
from scripts.audit_bayes_basket_target_time_contract import (
    build_time_contract_audit,
)


def test_historical_timezone_uses_only_registry_proven_station_invariant() -> None:
    rows = [
        {
            "station_id": "RKPK",
            "event_date": event_date,
            "settlement_timezone": "Asia/Seoul",
        }
        for event_date in ("2026-07-16", "2026-07-17")
    ]
    assert historical_registry_scope_settlement_timezone(
        rows,
        station_ids=["RKPK"],
        event_dates=["2026-06-28", "2026-07-16"],
    ) == "Asia/Seoul"


def test_historical_timezone_rejects_non_invariant_station_registry() -> None:
    rows = [
        {
            "station_id": "RKPK",
            "event_date": "2026-07-16",
            "settlement_timezone": "Asia/Seoul",
        },
        {
            "station_id": "RKPK",
            "event_date": "2026-07-17",
            "settlement_timezone": "America/New_York",
        },
    ]
    with pytest.raises(ContractError, match="station_timezone_not_invariant"):
        historical_registry_scope_settlement_timezone(
            rows,
            station_ids=["RKPK"],
            event_dates=["2026-06-28"],
        )


def test_rkpk_20260727_uses_registry_kst_settlement_window() -> None:
    assert settlement_day_window("2026-07-27", "Asia/Seoul") == {
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "target_day_start_utc": "2026-07-26T15:00:00Z",
        "target_day_end_utc": "2026-07-27T15:00:00Z",
    }


def test_et_is_a_different_boundary_not_a_market_override() -> None:
    window = settlement_day_window("2026-07-27", "America/New_York")
    assert window["target_day_start_utc"] == "2026-07-27T04:00:00Z"
    assert window["target_day_end_utc"] == "2026-07-28T04:00:00Z"
    assert window != settlement_day_window("2026-07-27", "Asia/Seoul")


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "market_registry_settlement_scope_missing"),
        (
            [
                {
                    "station_id": "RKPK",
                    "event_date": "2026-07-27",
                    "settlement_timezone": "Asia/Seoul",
                },
                {
                    "station_id": "RKPK",
                    "event_date": "2026-07-27",
                    "settlement_timezone": "America/New_York",
                },
            ],
            "market_registry_settlement_timezone_not_unique",
        ),
    ],
)
def test_registry_timezone_scope_is_required_and_unique(
    rows: list[dict[str, str]],
    reason: str,
) -> None:
    with pytest.raises(ContractError, match=reason):
        registry_settlement_timezone(
            rows,
            station_id="RKPK",
            event_date="2026-07-27",
        )


def test_probability_timezone_must_exactly_match_registry_market_timezone() -> None:
    assert_probability_timezone_matches_market(
        probability_timezone="Asia/Seoul",
        market_timezone="Asia/Seoul",
    )
    with pytest.raises(
        ContractError,
        match="probability_market_settlement_timezone_mismatch",
    ):
        assert_probability_timezone_matches_market(
            probability_timezone="America/New_York",
            market_timezone="Asia/Seoul",
        )
    with pytest.raises(ContractError, match="settlement_timezone_missing"):
        assert_probability_timezone_matches_market(
            probability_timezone="",
            market_timezone="Asia/Seoul",
        )


def test_rkpk_common_model_grid_is_derived_from_settlement_window() -> None:
    start = datetime(2026, 7, 26, 15, tzinfo=timezone.utc)
    run = datetime(2026, 7, 25, 18, tzinfo=timezone.utc)
    assert aligned_target_sample_offsets(
        target_day_start_utc=start,
        run_initialization=run,
    ) == (0, 6, 12, 18, 21)


def test_half_hour_timezone_uses_provider_aligned_fractional_offsets() -> None:
    start = datetime(2026, 6, 27, 18, 30, tzinfo=timezone.utc)
    run = datetime(2026, 6, 26, 18, tzinfo=timezone.utc)
    assert aligned_target_sample_offsets(
        target_day_start_utc=start,
        run_initialization=run,
    ) == (2.5, 8.5, 14.5, 20.5, 23.5)


def test_registry_scope_rejects_mixed_settlement_timezones() -> None:
    rows = [
        {
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
        },
        {
            "station_id": "KATL",
            "event_date": "2026-07-27",
            "settlement_timezone": "America/New_York",
        },
    ]
    with pytest.raises(
        ContractError,
        match="market_registry_mixed_settlement_timezones_forbidden",
    ):
        registry_scope_settlement_timezone(
            rows,
            station_ids=["RKPK", "KATL"],
            event_dates=["2026-07-27"],
        )


def test_three_clocks_remain_independent_across_kst_and_et_day_boundaries() -> None:
    snapshot = time_contract_snapshot(
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        event_at_utc="2026-07-26T15:30:00Z",
    )
    assert snapshot["target_day_start_utc"] == "2026-07-26T15:00:00Z"
    assert snapshot["target_day_end_utc"] == "2026-07-27T15:00:00Z"
    assert snapshot["event_clock_utc"] == "2026-07-26T15:30:00Z"
    assert snapshot["resolution_local_date_at_event"] == "2026-07-27"
    assert snapshot["operational_day"] == "2026-07-26"


def test_dst_windows_are_23_and_25_hours_without_overriding_event_clock() -> None:
    spring = settlement_day_window("2026-03-08", "America/New_York")
    assert spring["target_day_start_utc"] == "2026-03-08T05:00:00Z"
    assert spring["target_day_end_utc"] == "2026-03-09T04:00:00Z"
    fall = settlement_day_window("2026-11-01", "America/New_York")
    assert fall["target_day_start_utc"] == "2026-11-01T04:00:00Z"
    assert fall["target_day_end_utc"] == "2026-11-02T05:00:00Z"
    assert normalize_event_clock_utc("2026-11-01T01:30:00-04:00") == (
        "2026-11-01T05:30:00Z"
    )
    assert normalize_event_clock_utc("2026-11-01T01:30:00-05:00") == (
        "2026-11-01T06:30:00Z"
    )


def test_half_hour_settlement_timezone_and_et_operational_day_are_exact() -> None:
    window = settlement_day_window("2026-07-27", "Asia/Kolkata")
    assert window["target_day_start_utc"] == "2026-07-26T18:30:00Z"
    assert window["target_day_end_utc"] == "2026-07-27T18:30:00Z"
    assert resolution_local_date_at(
        "2026-07-26T18:29:59Z", "Asia/Kolkata"
    ) == "2026-07-26"
    assert resolution_local_date_at(
        "2026-07-26T18:30:00Z", "Asia/Kolkata"
    ) == "2026-07-27"
    assert operational_day_at("2026-07-27T03:59:59Z") == "2026-07-26"
    assert operational_day_at("2026-07-27T04:00:00Z") == "2026-07-27"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "event_clock_missing"),
        ("2026-07-27T00:00:00", "event_clock_timezone_required"),
        ("not-a-clock", "event_clock_invalid_iso8601"),
    ],
)
def test_event_clock_fails_closed_without_explicit_timezone(
    value: str,
    reason: str,
) -> None:
    with pytest.raises(ContractError, match=reason):
        normalize_event_clock_utc(value)


def test_m2_09_time_contract_receipt_is_bounded_and_no_live() -> None:
    receipt = build_time_contract_audit()
    assert receipt["status"] == "PASS"
    assert all(receipt["checks"].values())
    assert receipt["boundary"]["market_timezone_authority"] == (
        "market_registry.settlement_timezone"
    )
    assert receipt["boundary"]["et_may_override_market_timezone"] is False
    assert receipt["boundary"]["writes_production_db"] is False
    assert receipt["boundary"]["calls_order_gateway"] is False
    assert len(json.dumps(receipt).encode("utf-8")) < 1024 * 1024
