from __future__ import annotations

from scripts.polymarket_settled_weather_event_discovery import (
    build_discovery_report,
    normalize_resolved_weather_event,
)


def _market(market_id: str, label: str, yes: float) -> dict:
    return {
        "id": market_id,
        "groupItemTitle": label,
        "question": f"Will the highest temperature in Chengdu be {label} on July 10?",
        "closed": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'[{yes}, {1.0 - yes}]',
        "umaResolutionStatus": "resolved",
        "resolutionSource": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "closedTime": "2026-07-10T23:30:00Z",
    }


def _event() -> dict:
    return {
        "id": "event-1",
        "title": "Highest temperature in Chengdu on July 10?",
        "slug": "highest-temperature-in-chengdu-on-july-10-2026",
        "closed": True,
        "resolutionSource": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "markets": [
            _market("m1", "35°C or below", 0.0),
            _market("m2", "36°C", 1.0),
            _market("m3", "37°C or higher", 0.0),
        ],
    }


def test_normalize_resolved_event_counts_one_event_day_and_preserves_bucket_semantics():
    normalized, issues = normalize_resolved_weather_event(_event())

    assert issues == []
    assert normalized is not None
    assert normalized["event_date_local"] == "2026-07-10"
    assert normalized["station_id"] == "ZUUU"
    assert normalized["winning_bucket_label"] == "36°C"
    assert normalized["settlement_actual"]["lower"] == 35.5
    assert normalized["settlement_actual"]["upper"] == 36.5
    assert normalized["market_count"] == 3


def test_discovery_report_filters_nonclosed_and_deduplicates_event_id():
    duplicate = _event()
    open_event = {**_event(), "id": "open", "closed": False}
    report = build_discovery_report(
        [{"events": [_event(), duplicate, open_event]}],
        query="highest temperature Chengdu",
        city="Chengdu",
        station_id="ZUUU",
        planned_page_count=2,
        fetched=True,
    )

    assert report["raw_event_count"] == 3
    assert report["resolved_event_day_count"] == 1
    assert report["excluded_counts_by_reason"] == {"event_not_closed": 1}
    assert report["planned_request_count"] == 2
    assert report["impact"]["downloads_historical_forecasts"] is False


def test_event_with_two_winning_buckets_is_rejected():
    event = _event()
    event["markets"][0]["outcomePrices"] = "[1, 0]"
    normalized, issues = normalize_resolved_weather_event(event)

    assert normalized is None
    assert issues == ["winning_yes_market_not_unique"]


def test_replacement_events_share_one_event_day_and_latest_resolution_wins():
    old = _event()
    old["id"] = "old"
    for market in old["markets"]:
        market["closedTime"] = "2026-07-10T01:00:00Z"
    replacement = _event()
    replacement["id"] = "replacement"
    for market in replacement["markets"]:
        market["closedTime"] = "2026-07-10T02:00:00Z"
    report = build_discovery_report(
        [{"events": [old, replacement]}],
        query="highest temperature Chengdu",
        city="Chengdu",
        station_id="ZUUU",
        planned_page_count=1,
        fetched=True,
    )

    assert report["resolved_event_day_count"] == 1
    assert report["events"][0]["event_id"] == "replacement"
    assert report["excluded_counts_by_reason"]["duplicate_event_day_superseded"] == 1
    assert report["duplicate_event_day_trace"][0]["selection_rule"] == "latest_resolved_at_then_market_count_then_event_id"
