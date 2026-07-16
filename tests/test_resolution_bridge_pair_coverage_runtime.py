from __future__ import annotations

import hashlib
import json
import sqlite3

from resolution_bridge_pair_coverage_runtime import build_resolution_bridge_pair_coverage_audit
from resolution_observed_x_s_pair_shadow import PAIR_TABLE, SCHEMA_VERSION as PAIR_SCHEMA, initialize_isolated_resolution_observed_x_s_pair_schema
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _append_pair(conn: sqlite3.Connection, *, event_date: str, physical_x: float, settlement_s: float) -> None:
    contract_sha = _sha(f"contract:{event_date}")
    row = {
        "schema": PAIR_SCHEMA,
        "ok": True,
        "state": "available_for_bridge_calibration_only",
        "target_contract_sha256": contract_sha,
        "target_identity": f"target:{event_date}",
        "event_date": event_date,
        "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
        "authority_contract_sha256": _sha("authority-v4"),
        "target_resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
        "final_physical_high_c": physical_x,
        "settlement_scalar_c": settlement_s,
        "physical_source": {"source_observation_id": f"x:{event_date}", "raw_sha256": _sha(f"x:{event_date}"), "raw_ref": f"raw://x/{event_date}"},
        "settlement_source": {"source_observation_id": f"s:{event_date}", "raw_sha256": _sha(f"s:{event_date}"), "raw_ref": f"raw://s/{event_date}"},
    }
    pair_sha = _sha(json.dumps(row, sort_keys=True))
    conn.execute(
        f"INSERT INTO {PAIR_TABLE}(observed_x_s_pair_sha256,target_contract_sha256,target_identity,event_date,physical_source_observation_id,settlement_source_observation_id,pair_json,observed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (pair_sha, contract_sha, row["target_identity"], event_date, row["physical_source"]["source_observation_id"], row["settlement_source"]["source_observation_id"], json.dumps(row), f"{event_date}T20:00:00Z", f"{event_date}T20:00:00Z"),
    )


def test_empty_pair_registry_is_accumulating_not_calibrated(tmp_path) -> None:
    path = tmp_path / "pairs.db"
    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_isolated_resolution_observed_x_s_pair_schema(conn)
    result = build_resolution_bridge_pair_coverage_audit(db_path=path, minimum_distinct_event_days=2, minimum_distinct_x_values=2)
    assert result["state"] == "accumulating"
    assert result["pair_count"] == 0
    assert "bridge_pair_event_day_coverage_below_minimum" in result["block_reasons"]
    assert "bridge_pair_settlement_s_support_below_minimum" in result["block_reasons"]
    assert result["boundary"]["closes_bridge"] is False


def test_pair_coverage_requires_settlement_support_as_well_as_x_support(tmp_path) -> None:
    path = tmp_path / "pairs.db"
    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_isolated_resolution_observed_x_s_pair_schema(conn)
        _append_pair(conn, event_date="2026-07-01", physical_x=31.0, settlement_s=32.0)
        _append_pair(conn, event_date="2026-07-02", physical_x=33.0, settlement_s=32.0)
    blocked = build_resolution_bridge_pair_coverage_audit(db_path=path, minimum_distinct_event_days=2, minimum_distinct_x_values=2, minimum_distinct_s_values=2)
    assert blocked["pair_count"] == 2
    assert blocked["distinct_physical_x_value_count"] == 2
    assert blocked["distinct_settlement_s_value_count"] == 1
    assert blocked["block_reasons"] == ["bridge_pair_settlement_s_support_below_minimum"]

    with sqlite3.connect(path) as conn:
        _append_pair(conn, event_date="2026-07-03", physical_x=34.0, settlement_s=34.0)
    ready = build_resolution_bridge_pair_coverage_audit(db_path=path, minimum_distinct_event_days=2, minimum_distinct_x_values=2, minimum_distinct_s_values=2)
    assert ready["state"] == "ready_for_calibration_review"
    assert ready["ok"] is True
