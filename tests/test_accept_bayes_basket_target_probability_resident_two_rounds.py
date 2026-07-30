from __future__ import annotations

import copy
import sqlite3

from scripts.accept_bayes_basket_target_probability_resident_two_rounds import (
    MAX_CORE_ROWS,
    _latest_core_chain_rows,
    _validate_core_chain_rows,
    compare_rounds,
)


def _round() -> dict:
    return {
        "state": "PASS",
        "round_sha256": "a" * 64,
        "services": {
            "com.weather.reported_trajectory_no": 1,
            "com.weather.market_ws": 2,
            "com.weather.clob_order_service": 3,
        },
        "non_target_station_counts": {"auto_trade_decisions": 10},
        "station_counts": {"auto_trade_decisions": 20},
        "durable_ssot_replay": {
            "station_id": "RKPK",
            "event_date": "2026-07-29",
            "settlement_timezone": "Asia/Seoul",
            "rows": [{"row_sha256": "1" * 64}],
            "continuation": {
                "last_rowid": 1,
                "chain_sha256": "2" * 64,
            },
        },
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def test_core_direction_round_trip_binds_previous_state() -> None:
    rows = [
        (
            1,
            200,
            '{"core_buckets":["38","39"],"current_core_buckets":[],'
            '"direction":"UP","current_direction":null,'
            '"core_transition":{"direction_transition":"INITIALIZE"}}',
        ),
        (
            2,
            220,
            '{"core_buckets":["37","38"],'
            '"current_core_buckets":["38","39"],'
            '"direction":"DOWN","current_direction":"UP",'
            '"core_transition":{"direction_transition":"MIGRATE"}}',
        ),
    ]
    result = _validate_core_chain_rows(rows)
    assert result["validated_count"] == 2
    assert result["violations"] == []
    replay = _validate_core_chain_rows(rows)
    assert replay["content_sha256"] == result["content_sha256"]


def test_core_direction_predecessor_drift_fails_closed() -> None:
    rows = [
        (
            1,
            200,
            '{"core_buckets":["38"],"current_core_buckets":[],'
            '"direction":"UP","current_direction":null,'
            '"core_transition":{"direction_transition":"INITIALIZE"}}',
        ),
        (
            2,
            220,
            '{"core_buckets":["39"],"current_core_buckets":["37"],'
            '"direction":"DOWN","current_direction":"NEUTRAL",'
            '"core_transition":{"direction_transition":"MIGRATE"}}',
        ),
    ]
    result = _validate_core_chain_rows(rows)
    assert len(result["violations"]) == 2


def test_core_chain_query_reads_latest_bounded_window_in_causal_order() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "create table auto_trade_decisions("
        "station_id text, strategy_lane text, details_json text)"
    )
    payload = (
        '{"core_buckets":["38"],"current_core_buckets":["38"],'
        '"direction":"NEUTRAL","current_direction":"NEUTRAL",'
        '"core_transition":{"direction_transition":"KEEP"}}'
    )
    conn.executemany(
        "insert into auto_trade_decisions values('RKPK','basket',?)",
        [(payload,)] * (MAX_CORE_ROWS + 7),
    )
    conn.execute(
        "insert into auto_trade_decisions values('OTHER','basket',?)",
        (payload,),
    )

    rows, truncated = _latest_core_chain_rows(conn, station_id="RKPK")

    assert truncated is True
    assert len(rows) == MAX_CORE_ROWS
    assert [row[0] for row in rows] == list(
        range(8, MAX_CORE_ROWS + 8)
    )


def test_two_round_acceptance_allows_target_advance_only() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["round_sha256"] = "b" * 64
    second["station_counts"]["auto_trade_decisions"] = 21
    result = compare_rounds(first, second)
    assert result["state"] == "PASS"
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_two_round_acceptance_rejects_service_or_non_target_drift() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["services"]["com.weather.market_ws"] = 99
    second["non_target_station_counts"]["auto_trade_decisions"] = 11
    result = compare_rounds(first, second)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "service_pid_drift" in result["blockers"]
    assert "non_target_station_write_detected" in result["blockers"]


def test_two_round_acceptance_rejects_deletion_and_live_boundary() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["station_counts"]["auto_trade_decisions"] = 19
    second["live_submit_authorized"] = True
    result = compare_rounds(first, second)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert (
        "target_append_only_count_decreased:auto_trade_decisions"
        in result["blockers"]
    )
    assert "no_live_boundary_invalid" in result["blockers"]


def test_two_round_acceptance_rejects_same_count_content_root_drift() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["round_sha256"] = "b" * 64
    second["durable_ssot_replay"]["rows"][0]["row_sha256"] = "9" * 64
    second["durable_ssot_replay"]["continuation"]["chain_sha256"] = "8" * 64
    result = compare_rounds(first, second)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "durable_ssot_replay_not_append_only_prefix" in result["blockers"]
