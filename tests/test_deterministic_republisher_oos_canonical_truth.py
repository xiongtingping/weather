from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.deterministic_republisher_oos import (
    _truth_index,
    build_deterministic_republisher_oos,
)
from scripts.apply_canonical_contract_schema import migrate as apply_contract_schema


EVENT_DATE = "2026-07-01"
AS_OF = "2026-08-04T05:16:39Z"


def _database(path: Path) -> sqlite3.Connection:
    path.touch()
    assert apply_contract_schema(path, apply=True)["state"] == "APPLIED"
    conn = sqlite3.connect(path)
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
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
            "TEST",
            EVENT_DATE,
            99.0,
            0,
            "2026-07-02T01:00:00Z",
            "9" * 64,
            "AWC",
            "ACCEPTED",
            "FORMAL_NATIVE_ELIGIBLE",
        ),
    )
    conn.commit()
    return conn


def _insert_canonical_truth(conn: sqlite3.Connection) -> dict[str, object]:
    identity: dict[str, object] = {
        "truth_id": "canonical-truth-test-20260701",
        "station_id": "TEST",
        "event_date": EVENT_DATE,
        "truth_revision_no": 0,
        "truth_value": 31.0,
        "truth_content_sha256": "3" * 64,
        "canonical_committed_at_utc": "2026-07-02T01:00:00Z",
        "station_day_end_utc": "2026-07-02T00:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "Etc/UTC",
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    lineage = {
        "truth_content_sha256": fact["truth_content_sha256"],
        "source_role": fact["source_role"],
        "authority_scope": fact["authority_scope"],
    }
    conn.execute(
        "INSERT INTO canonical_ssot_fact "
        "(fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "truth:" + str(fact["content_sha256"]),
            "truth",
            "TEST|2026-07-01|0",
            fact["content_sha256"],
            json.dumps(fact, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, sort_keys=True, separators=(",", ":")),
            "2026-08-04T00:00:00Z",
        ),
    )
    conn.commit()
    return fact


def _capture() -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": "bayes_basket_target_historical_aggregator_rebuild_evidence_v1",
        "provider_id": "test_open_meteo",
        "model_id": "TEST_MODEL",
        "reconstructed_at_utc": "2026-08-03T00:00:00Z",
        "station_timezones": {"TEST": "Etc/UTC"},
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
                    "station_id": "TEST",
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


def test_legacy_truth_projection_never_counts_without_canonical_v6_fact(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path / "ssot.db")
    try:
        result = _truth_index(
            conn,
            scopes=[("TEST", EVENT_DATE)],
            as_of=datetime.fromisoformat(AS_OF.replace("Z", "+00:00")),
            timezones={"TEST": "Etc/UTC"},
        )
    finally:
        conn.close()
    assert result == {}


def test_canonical_v6_truth_fact_is_exact_lineage_source(tmp_path: Path) -> None:
    conn = _database(tmp_path / "ssot.db")
    try:
        fact = _insert_canonical_truth(conn)
        result = _truth_index(
            conn,
            scopes=[("TEST", EVENT_DATE)],
            as_of=datetime.fromisoformat(AS_OF.replace("Z", "+00:00")),
            timezones={"TEST": "Etc/UTC"},
        )
    finally:
        conn.close()
    assert result[("TEST", EVENT_DATE)]["official_tmax_c"] == 31.0
    assert result[("TEST", EVENT_DATE)]["content_sha256"] == fact["content_sha256"]
    assert result[("TEST", EVENT_DATE)]["truth_content_sha256"] == "3" * 64


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
    assert report["truth_source"] == "canonical_ssot_fact_v6"
    assert report["legacy_truth_projection_read"] is False
    assert report["shortfalls"] == [
        {
            "station_id": "TEST",
            "available_closed_historical_days": 0,
            "required_train_days": 20,
            "required_holdout_days": 8,
            "missing_days": 28,
        }
    ]
