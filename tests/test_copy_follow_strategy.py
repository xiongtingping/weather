from __future__ import annotations

from datetime import date

from copy_follow_strategy import (
    CopyFollowCandidate,
    allocate_source_scaled_copy,
    allocate_settlement_floor,
    assess_source_building,
    compare_forecast_peak_to_bucket,
    event_slug_from_activity,
    extract_four_bucket_candidates,
    fetch_trades,
    rank_recent_city_candidates,
    parse_activity_bucket,
)


def _activity(event: str, bucket: str, usdc: float) -> dict:
    return {
        "type": "TRADE",
        "side": "BUY",
        "outcome": "Yes",
        "eventSlug": event,
        "title": f"Will the highest temperature in Test City be {bucket} on July 21?",
        "transactionHash": f"tx-{bucket}-{usdc}",
        "asset": f"asset-{bucket}",
        "timestamp": 1784550000,
        "size": usdc * 10,
        "price": 0.1,
        "usdcSize": usdc,
    }


def test_copy_candidate_keeps_exact_four_and_uses_source_ratio() -> None:
    rows = [
        _activity("highest-temperature-in-test-city-on-july-21-2026", "30°C", 12),
        _activity("highest-temperature-in-test-city-on-july-21-2026", "31°C", 9),
        _activity("highest-temperature-in-test-city-on-july-21-2026", "32°C", 6),
        _activity("highest-temperature-in-test-city-on-july-21-2026", "33°C", 3),
    ]
    candidate = extract_four_bucket_candidates(
        rows,
        source_user="0xabc",
        city_cap_usdc=30,
        today_et=date(2026, 7, 20),
        lookahead_days=2,
    )[0]
    assert candidate.buckets == ("30°C", "31°C", "32°C", "33°C")
    assert candidate.main_bucket == "30°C"
    assert sum(candidate.allocation_usdc_by_bucket.values()) == 30.0
    assert candidate.allocation_usdc_by_bucket["30°C"] > candidate.allocation_usdc_by_bucket["33°C"]
    assert candidate.source_shares_by_bucket == {
        "30°C": 120.0,
        "31°C": 90.0,
        "32°C": 60.0,
        "33°C": 30.0,
    }


def test_source_scaled_copy_uses_capital_scale_and_share_mix_without_floor() -> None:
    result = allocate_source_scaled_copy(
        buckets=("30°C", "31°C", "32°C", "33°C"),
        source_notional_by_bucket={"30°C": 12, "31°C": 9, "32°C": 6, "33°C": 3},
        source_shares_by_bucket={"30°C": 10, "31°C": 11, "32°C": 12, "33°C": 13},
        best_ask_by_bucket={bucket: 0.25 for bucket in ("30°C", "31°C", "32°C", "33°C")},
        budget_usdc=30,
    )

    assert result["status"] == "ready"
    assert result["mode"] == "source_capital_scale_and_share_ratio"
    assert result["quantities"] == {"30°C": 10.0, "31°C": 11.0, "32°C": 12.0, "33°C": 13.0}
    assert result["total_cost_usdc"] == 11.5
    assert result["settlement_floor_removed"] is True


def test_source_scaled_copy_reports_effective_scale_when_prices_exceed_budget() -> None:
    result = allocate_source_scaled_copy(
        buckets=("30°C", "31°C", "32°C", "33°C"),
        source_notional_by_bucket={bucket: 7.5 for bucket in ("30°C", "31°C", "32°C", "33°C")},
        source_shares_by_bucket={bucket: 10.0 for bucket in ("30°C", "31°C", "32°C", "33°C")},
        best_ask_by_bucket={bucket: 0.9 for bucket in ("30°C", "31°C", "32°C", "33°C")},
        budget_usdc=30,
    )
    assert result["status"] == "ready"
    assert result["capital_scale_factor"] == 1.0
    assert result["price_scale_factor"] == 1.0
    assert result["copy_scale_factor"] == 1.0
    assert result["total_cost_usdc"] == 36.0
    assert result["budget_exceeded"] is True


def test_copy_candidate_never_crops_a_five_bucket_selection() -> None:
    rows = [
        _activity("highest-temperature-in-test-city-on-july-21-2026", f"{degree}°C", 1)
        for degree in range(30, 35)
    ]
    assert extract_four_bucket_candidates(
        rows,
        source_user="0xabc",
        today_et=date(2026, 7, 20),
        lookahead_days=2,
    ) == []


def test_copy_candidate_keeps_non_contiguous_four_bucket_selection() -> None:
    rows = [
        _activity("highest-temperature-in-test-city-on-july-21-2026", bucket, usdc)
        for bucket, usdc in (("28°C", 12), ("29°C", 9), ("31°C", 6), ("32°C", 3))
    ]
    candidate = extract_four_bucket_candidates(
        rows,
        source_user="0xabc",
        today_et=date(2026, 7, 20),
        lookahead_days=2,
    )[0]
    assert candidate.buckets == ("28°C", "29°C", "31°C", "32°C")


def test_recent_city_rank_uses_trades_for_activity_and_positions_for_roi() -> None:
    event_a = "highest-temperature-in-alpha-city-on-july-21-2026"
    event_b = "highest-temperature-in-beta-city-on-july-21-2026"

    def positions(event: str, pnl: float) -> list[dict]:
        return [
            {
                "eventSlug": event,
                "asset": f"{event}-{bucket}",
                "title": f"Will the highest temperature in Test City be {bucket} on July 21?",
                "outcome": "Yes",
                "size": 100.0,
                "initialValue": 10.0,
                "currentValue": 10.0 + pnl,
                "cashPnl": pnl,
                "realizedPnl": 0.0,
            }
            for bucket in ("30°C", "31°C", "32°C", "33°C")
        ]

    trades = [
        {"eventSlug": event_a, "timestamp": 1784550000, "type": "TRADE"},
        {"eventSlug": event_b, "timestamp": 1784550000, "type": "TRADE"},
    ]
    result = rank_recent_city_candidates(
        positions(event_a, 8.0) + positions(event_b, 2.0),
        trades,
        source_user="0xabc",
        max_cities=2,
        today_et=date(2026, 7, 20),
        lookahead_days=2,
        now_timestamp=1784553600,
    )
    assert result["status"] == "ready"
    assert result["selected_cities"] == ["alpha-city", "beta-city"]
    assert result["selected_ranking"][0]["roi_percent"] == 80.0
    assert result["selected_ranking"][1]["roi_percent"] == 20.0


def test_trades_pagination_stops_when_required_events_are_covered() -> None:
    event_a = "highest-temperature-in-alpha-city-on-july-21-2026"
    event_b = "highest-temperature-in-beta-city-on-july-21-2026"
    pages = {
        0: [{"eventSlug": event_a, "timestamp": 1784550000, "side": "BUY", "outcome": "Yes", "size": 1, "price": 0.1}],
        1: [{"eventSlug": event_b, "timestamp": 1784550001, "side": "BUY", "outcome": "Yes", "size": 1, "price": 0.1}],
        2: [{"eventSlug": "highest-temperature-in-unrelated-city-on-july-21-2026", "timestamp": 1784550002}],
    }
    calls: list[str] = []

    class Response:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(self.value).encode("utf-8")

    def opener(request, *, timeout):
        calls.append(request.full_url)
        offset = int(request.full_url.split("offset=", 1)[1].split("&", 1)[0])
        return Response(pages[offset])

    rows = fetch_trades(
        user="0xabc",
        page_size=1,
        max_pages=10,
        since_timestamp=1784540000,
        required_event_slugs=(event_a, event_b),
        opener=opener,
    )
    assert len(rows) == 2
    assert len(calls) == 2


def test_settlement_floor_overrides_source_ratio_but_stays_under_cap() -> None:
    asks = {bucket: [(0.1, 1000.0)] for bucket in ("30°C", "31°C", "32°C", "33°C")}
    result = allocate_settlement_floor(
        buckets=asks,
        source_allocations={"30°C": 12, "31°C": 9, "32°C": 6, "33°C": 3},
        asks_by_bucket=asks,
        budget_usdc=100,
        primary_bucket="30°C",
        secondary_bucket="31°C",
        safety_floor_usdc=0.01,
    )
    assert result["status"] == "ready"
    assert result["total_cost_usdc"] <= 30
    assert result["worst_case_profit_usdc"] >= 0.00999
    quantities = result["quantities"]
    assert quantities["30°C"] > quantities["31°C"] > quantities["32°C"]
    assert quantities["31°C"] > quantities["33°C"]


def test_settlement_floor_blocks_when_best_ask_sum_reaches_one() -> None:
    asks = {bucket: [(0.25, 1000.0)] for bucket in ("30°C", "31°C", "32°C", "33°C")}
    result = allocate_settlement_floor(
        buckets=asks,
        source_allocations={bucket: 7.5 for bucket in asks},
        asks_by_bucket=asks,
        budget_usdc=30,
        primary_bucket="30°C",
        secondary_bucket="31°C",
    )
    assert result["status"] == "blocked"
    assert "copy_follow_floor_solver_not_converged" in result["reason_codes"]


def test_settlement_floor_searches_inside_ask_depth_boundary() -> None:
    asks = {
        "39°C": [(0.33, 100.0)],
        "40°C": [(0.46, 100.0)],
        "41°C": [(0.16, 100.0)],
        "42°C": [(0.036, 100.0)],
    }
    result = allocate_settlement_floor(
        buckets=asks,
        source_allocations={"39°C": 0.07, "40°C": 14.69, "41°C": 7.28, "42°C": 7.96},
        asks_by_bucket=asks,
        budget_usdc=100,
        primary_bucket="40°C",
        secondary_bucket="42°C",
        safety_floor_usdc=0.01,
        minimum_shares=1.0,
        minimum_notional_usdc=0.25,
        strict_share_gap=1.0,
    )
    assert result["status"] == "ready"
    assert result["best_ask_sum"] == 0.986
    assert result["worst_case_profit_usdc"] >= 0.00999


def test_forecast_comparison_reports_market_unit_relation_and_missing() -> None:
    inside = compare_forecast_peak_to_bucket(28.5, "82-83°F")
    assert inside["market_unit"] == "F"
    assert inside["forecast_peak_market_value"] == 83.3
    assert inside["relation"] == "above_main_bucket"
    assert inside["inside_main_bucket"] is False

    below = compare_forecast_peak_to_bucket(20, "22°C")
    assert below["relation"] == "below_main_bucket"
    assert below["delta_to_bucket_center"] == -2.0

    missing = compare_forecast_peak_to_bucket(None, "22°C")
    assert missing["relation"] == "missing"
    assert missing["inside_main_bucket"] is None


def test_activity_bucket_normalizes_polymarket_between_prefix() -> None:
    title = "Will the highest temperature in Chicago be between 82-83°F on July 21?"
    assert parse_activity_bucket(title) == "82-83°F"
    assert event_slug_from_activity({"eventSlug": "", "slug": "highest-temperature-in-chicago-on-july-21-2026-82-83f"}) == "highest-temperature-in-chicago-on-july-21-2026"


def test_source_building_requires_fresh_four_buckets_and_large_core() -> None:
    candidate = CopyFollowCandidate(
        source_user="0xabc",
        event_slug="event",
        city_slug="city",
        event_date="2026-07-21",
        buckets=("30°C", "31°C", "32°C", "33°C"),
        source_notional_by_bucket={"30°C": 12.0, "31°C": 9.0, "32°C": 6.0, "33°C": 3.0},
        allocation_usdc_by_bucket={"30°C": 12.0, "31°C": 9.0, "32°C": 6.0, "33°C": 3.0},
        total_source_notional_usdc=30.0,
        total_allocation_usdc=30.0,
        main_bucket="30°C",
        last_activity_timestamp=1000,
    )
    active = assess_source_building(candidate, now_timestamp=1100)
    assert active["active"] is True

    stale = assess_source_building(candidate, now_timestamp=2000)
    assert stale["active"] is False
    assert "copy_follow_source_not_actively_building" in stale["reason_codes"]

    weak = assess_source_building(candidate, now_timestamp=1100, minimum_main_notional_usdc=13)
    assert weak["active"] is False
    assert "copy_follow_main_bucket_notional_not_large_enough" in weak["reason_codes"]

    not_four = candidate.__class__(**{**candidate.__dict__, "buckets": ("30°C", "31°C", "32°C")})
    not_four_result = assess_source_building(not_four, now_timestamp=1100)
    assert not_four_result["active"] is False
    assert "copy_follow_four_bucket_selection_not_formed" in not_four_result["reason_codes"]
