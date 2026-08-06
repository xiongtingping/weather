from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.deterministic_republisher_oos import (
    _truth_index,
    build_deterministic_republisher_oos,
)
from tests.test_awc_observation_contract import _contract as awc_contract


EVENT_DATE = "2026-08-01"
AS_OF = "2026-08-04T13:00:00Z"


def _database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth ("
        "truth_id TEXT,station_id TEXT,local_date TEXT,official_tmax_c REAL,"
        "revision_no INTEGER,canonical_committed_at_utc TEXT,content_sha256 TEXT,"
        "source_lane TEXT,qc_status TEXT,authority_scope TEXT)"
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy-only",
            "KATL",
            EVENT_DATE,
            99.0,
            0,
            "2026-08-02T05:00:00Z",
            "9" * 64,
            "AWC",
            "ACCEPTED",
            "FORMAL_NATIVE_ELIGIBLE",
        ),
    )
    conn.commit()
    return conn


def _insert_v12_truth(conn: sqlite3.Connection) -> dict[str, object]:
    packet = awc_contract()
    ingest_canonical_contract(
        conn, packet, commit=True, ingested_at_utc="2026-08-02T05:00:00Z"
    )
    return packet["day_bounds_facts"][0]


def _capture() -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": "bayes_basket_target_historical_aggregator_rebuild_evidence_v1",
        "provider_id": "test_open_meteo",
        "model_id": "TEST_MODEL",
        "reconstructed_at_utc": "2026-08-03T00:00:00Z",
        "station_timezones": {"KATL": "America/New_York"},
        "source": {
            "source_class": "aggregator_republisher",
            "open_meteo": True,
        },
        "checks": {
            "native_arrival_index_eligible": False,
            "prospective_strict_asof_eligible": False,
        },
        "curves": {
            "eligible_model_rows": [
                {
                    "station_id": "KATL",
                    "event_date": EVENT_DATE,
                    "source_class": "aggregator_republisher",
                    "aggregator_or_republisher": True,
                    "open_meteo": True,
                    "complete": True,
                    "forecast_daily_max_c": 30.0,
                    "raw_message_sha256": "7" * 64,
                    "raw_object_ref": "retained:test",
                }
            ]
        },
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def test_legacy_truth_projection_never_counts_without_v12_day_bounds(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path / "ssot.db")
    try:
        result = _truth_index(
            conn,
            scopes=[("KATL", EVENT_DATE)],
            as_of=datetime.fromisoformat(AS_OF.replace("Z", "+00:00")),
            timezones={"KATL": "America/New_York"},
        )
    finally:
        conn.close()
    assert result == {}


def test_canonical_v12_day_bounds_is_exact_lineage_source(tmp_path: Path) -> None:
    conn = _database(tmp_path / "ssot.db")
    try:
        fact = _insert_v12_truth(conn)
        result = _truth_index(
            conn,
            scopes=[("KATL", EVENT_DATE)],
            as_of=datetime.fromisoformat(AS_OF.replace("Z", "+00:00")),
            timezones={"KATL": "America/New_York"},
        )
    finally:
        conn.close()
    assert result[("KATL", EVENT_DATE)]["official_tmax_c"] == 31.0
    assert result[("KATL", EVENT_DATE)]["content_sha256"] == fact["content_sha256"]
    assert result[("KATL", EVENT_DATE)]["observation_revision_vector_sha256"] == fact[
        "observation_revision_vector_sha256"
    ]


def test_zero_canonical_truth_station_gets_explicit_shortfall(tmp_path: Path) -> None:
    conn = _database(tmp_path / "ssot.db")
    try:
        report = build_deterministic_republisher_oos(
            [_capture()],
            conn,
            as_of_utc=AS_OF,
        )
    finally:
        conn.close()
    assert report["candidate_count"] == 0
    assert report["truth_count"] == 0
    assert report["truth_source"] == "canonical_ssot_fact_v12_awc_observation_contract_v1"
    assert report["legacy_truth_projection_read"] is False
    assert report["shortfalls"] == [
        {
            "station_id": "KATL",
            "model_id": "TEST_MODEL",
            "provider_id": "test_open_meteo",
            "source_class": "aggregator_republisher",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "settlement_timezone": "America/New_York",
            "lead_bucket": "d0",
            "run_cycle": None,
            "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
            "state": "BLOCKED",
            "failure_code": "LINEAGE_MISMATCH",
            "available_closed_historical_days": 0,
            "required_train_days": 20,
            "required_holdout_days": 8,
            "missing_days": 28,
            "timezone_mismatch_scope_count": 0,
            "data_missing_scope_count": 1,
            "model_lineage_gap_scope_count": 1,
        }
    ]
