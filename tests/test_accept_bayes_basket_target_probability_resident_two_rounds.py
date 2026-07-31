from __future__ import annotations

import copy
import sqlite3

import pytest

import scripts.accept_bayes_basket_target_probability_resident_two_rounds as acceptance
from scripts.accept_bayes_basket_target_probability_resident_two_rounds import (
    CANONICAL_STATION_IDS,
    MAX_CORE_ROWS,
    _latest_core_chain_rows,
    _probability_authority,
    _station_counts,
    _validate_core_chain_rows,
    compare_migration_rounds,
    compare_rounds,
    migration_round_from_strict,
    normalize_station_scope,
)


SCOPE = ("EGLC", "RKPK")


def _replay(station: str) -> dict:
    return {
        "station_id": station,
        "event_date": "2026-07-29",
        "settlement_timezone": "Asia/Seoul",
        "rows": [{"row_sha256": "1" * 64}],
        "continuation": {
            "last_rowid": 1,
            "chain_sha256": "2" * 64,
        },
    }


def _round() -> dict:
    return {
        "state": "PASS",
        "round_sha256": "a" * 64,
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(SCOPE),
        },
        "registry_started_station_ids": list(SCOPE),
        "started_station_ids": list(SCOPE),
        "candidate_accumulation_station_ids": [],
        "not_started_station_ids": [],
        "services": {
            "com.weather.reported_trajectory_no": 1,
            "com.weather.market_ws": 2,
            "com.weather.clob_order_service": 3,
        },
        "outside_scope_station_counts": {"auto_trade_decisions": 10},
        "scoped_station_counts": {"auto_trade_decisions": 20},
        "station_results": {
            station: {
                "state": "PASS",
                "started": True,
                "durable_ssot_replay": _replay(station),
            }
            for station in SCOPE
        },
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def test_default_scope_is_exact_frozen_45() -> None:
    assert normalize_station_scope() == CANONICAL_STATION_IDS
    assert len(CANONICAL_STATION_IDS) == 45
    with pytest.raises(RuntimeError, match="duplicate_or_empty"):
        normalize_station_scope(("RKPK", "RKPK"))
    with pytest.raises(RuntimeError, match="not_canonical"):
        normalize_station_scope(("XXXX",))


def test_formal_p0_eligibility_requires_exact_two_model_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        acceptance,
        "load_production_artifacts",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        acceptance,
        "candidate_run_initializations",
        lambda **_kwargs: ["2026-07-29T00:00:00Z"],
    )
    monkeypatch.setattr(
        acceptance,
        "compatible_run_candidates",
        lambda **_kwargs: [{
            "run_initialization_utc": "2026-07-29T00:00:00Z",
            "activation_binding": {
                "selected_models": ["ECMWF", "GFS"],
            },
        }],
    )
    result = acceptance._formal_p0_scope_eligibility(
        sqlite3.connect(":memory:"),
        station_id="RKPK",
        registry_rows=[{
            "station_id": "RKPK",
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
        }],
        as_of_utc="2026-07-29T12:00:00Z",
    )
    assert result["eligible"] is True
    assert result["compatible_scopes"][0]["selected_models"] == [
        "ECMWF",
        "GFS",
    ]


def test_single_model_binding_stays_candidate_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        acceptance,
        "load_production_artifacts",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        acceptance,
        "candidate_run_initializations",
        lambda **_kwargs: ["2026-07-29T00:00:00Z"],
    )
    monkeypatch.setattr(
        acceptance,
        "compatible_run_candidates",
        lambda **_kwargs: [{
            "run_initialization_utc": "2026-07-29T00:00:00Z",
            "activation_binding": {"selected_models": ["ECMWF"]},
        }],
    )
    result = acceptance._formal_p0_scope_eligibility(
        sqlite3.connect(":memory:"),
        station_id="RKPK",
        registry_rows=[{
            "station_id": "RKPK",
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
        }],
        as_of_utc="2026-07-29T12:00:00Z",
    )
    assert result["eligible"] is False
    assert result["compatible_scopes"] == []


def test_station_counts_separate_scope_from_outside_scope() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE auto_trade_decisions(station_id TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO auto_trade_decisions VALUES(?)",
        [("EGLC",), ("RKPK",), ("ZUUU",)],
    )
    scoped, outside = _station_counts(conn, station_ids=SCOPE)
    assert scoped["auto_trade_decisions"] == 2
    assert outside["auto_trade_decisions"] == 1


def test_probability_authority_uses_highest_materialized_stage() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history("
        "station_id TEXT,event_date TEXT,stage TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_latest("
        "station_id TEXT,event_date TEXT,checkpoint_id TEXT,stage TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_history VALUES(?,?,?)",
        [
            ("RKPK", "2026-07-30", "P0"),
            ("RKPK", "2026-07-30", "P0-M"),
            ("RKPK", "2026-07-30", "P1"),
        ],
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_latest VALUES(?,?,?,?)",
        ("RKPK", "2026-07-30", "p1", "P1"),
    )
    passed = _probability_authority(
        conn,
        station_id="RKPK",
        event_dates=("2026-07-30",),
    )
    assert passed["state"] == "PASS"
    conn.execute(
        "UPDATE basket_probability_bridge_latest SET stage='P0'"
    )
    blocked = _probability_authority(
        conn,
        station_id="RKPK",
        event_dates=("2026-07-30",),
    )
    assert blocked["state"] == "BLOCKED_FAIL_CLOSED"
    assert (
        "probability_latest_authority_downgraded:2026-07-30"
        in blocked["blockers"]
    )


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
    assert (
        _validate_core_chain_rows(rows)["content_sha256"]
        == result["content_sha256"]
    )


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
    assert len(_validate_core_chain_rows(rows)["violations"]) == 2


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


def test_two_round_acceptance_allows_all_scoped_stations_to_advance() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["round_sha256"] = "b" * 64
    second["scoped_station_counts"]["auto_trade_decisions"] = 22
    result = compare_rounds(first, second)
    assert result["state"] == "PASS"
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_two_round_acceptance_rejects_service_or_outside_scope_drift() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["services"]["com.weather.market_ws"] = 99
    second["outside_scope_station_counts"]["auto_trade_decisions"] = 11
    result = compare_rounds(first, second)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "service_pid_drift" in result["blockers"]
    assert "outside_scope_station_write_detected" in result["blockers"]


def test_two_round_acceptance_rejects_deletion_and_live_boundary() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["scoped_station_counts"]["auto_trade_decisions"] = 19
    second["live_submit_authorized"] = True
    result = compare_rounds(first, second)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert (
        "scoped_append_only_count_decreased:auto_trade_decisions"
        in result["blockers"]
    )
    assert "no_live_boundary_invalid" in result["blockers"]


def test_two_round_acceptance_rejects_per_station_replay_drift() -> None:
    first = _round()
    second = copy.deepcopy(first)
    second["station_results"]["RKPK"]["durable_ssot_replay"]["rows"][0][
        "row_sha256"
    ] = "9" * 64
    result = compare_rounds(first, second)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert (
        "RKPK:durable_ssot_replay_not_append_only_prefix"
        in result["blockers"]
    )


def test_future_station_can_become_started_without_waiting_for_settlement() -> None:
    first = _round()
    first["registry_started_station_ids"] = ["EGLC"]
    first["started_station_ids"] = ["EGLC"]
    first["not_started_station_ids"] = ["RKPK"]
    first["station_results"]["RKPK"] = {
        "state": "NOT_STARTED_NO_MATERIALIZATION_REQUIRED",
        "started": False,
        "durable_ssot_replay": None,
    }
    second = _round()
    second["round_sha256"] = "b" * 64
    result = compare_rounds(first, second)
    assert result["state"] == "PASS"


def test_migration_round_separates_materialization_from_safety_blockers() -> None:
    strict = _round()
    strict["state"] = "BLOCKED_FAIL_CLOSED"
    strict["blockers"] = [
        "RKPK:candidate_artifact_missing",
        "EGLC:formal_two_model_scope_missing_p0",
    ]
    migration = migration_round_from_strict(strict)
    assert migration["state"] == "PASS"
    assert migration["blockers"] == []
    assert migration["materialization_pending_blockers"] == [
        "EGLC:formal_two_model_scope_missing_p0",
        "RKPK:candidate_artifact_missing",
    ]

    strict["blockers"].append("probability_evidence_schema_incomplete")
    blocked = migration_round_from_strict(strict)
    assert blocked["state"] == "BLOCKED_FAIL_CLOSED"
    assert blocked["blockers"] == [
        "probability_evidence_schema_incomplete"
    ]


def test_migration_two_rounds_allow_pending_materialization_but_not_drift() -> None:
    first = migration_round_from_strict({
        **_round(),
        "state": "BLOCKED_FAIL_CLOSED",
        "blockers": ["RKPK:directional_core_decision_missing"],
        "probability_evidence_runtime": {
            "missing_tables": [],
            "missing_bridge_tables": [],
            "candidate_activation_count_all": 0,
        },
        "forbidden_execution_counts": {"order_events": 0},
        "resources": {
            "database_bytes": 1024,
            "wal_bytes": 0,
            "free_bytes": 4 * 1024 * 1024 * 1024,
        },
    })
    second = copy.deepcopy(first)
    second["round_sha256"] = "b" * 64
    passed = compare_migration_rounds(first, second)
    assert passed["state"] == "PASS"
    assert passed["started_station_ids"] == list(SCOPE)
    assert passed["materialization_pending_blockers"] == [
        "RKPK:directional_core_decision_missing"
    ]

    second["services"]["com.weather.market_ws"] = 99
    blocked = compare_migration_rounds(first, second)
    assert blocked["state"] == "BLOCKED_FAIL_CLOSED"
    assert "service_pid_drift" in blocked["blockers"]
