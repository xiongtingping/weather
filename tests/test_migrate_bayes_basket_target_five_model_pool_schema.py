from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.ssot import (
    DDL,
    _execute_sql_script_transactional,
    inspect_calibration_model_pool_schema,
    production_ssot_schema_status,
    upgrade_calibration_model_pool_schema_preserving_rows,
)


def _legacy_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    legacy = DDL.replace(
        "('ECMWF','GFS','ICON','GEM','CMA')",
        "('ECMWF','GFS','ICON')",
    )
    _execute_sql_script_transactional(conn, legacy)
    conn.execute(
        "INSERT INTO calibration_station_registry VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "RKPK", "RKPK", "Busan", "Asia/Seoul", "AWC", "local-day",
            "registry-v1", "official_truth", "a" * 64,
        ),
    )
    conn.execute(
        """
        INSERT INTO calibration_station_daily_high_truth(
          truth_id,station_id,local_date,settlement_timezone,official_tmax_c,
          source_lane,
          settlement_definition,revision_no,published_at_utc,received_at_utc,
          canonical_committed_at_utc,timestamps_complete,qc_status,source_role,
          source_payload_sha256,observation_ids_json,authority_scope,content_sha256
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "truth-1", "RKPK", "2026-07-01", "Asia/Seoul", 31.0,
            "AWC", "local-day", 0,
            "2026-07-02T00:00:00Z", "2026-07-02T00:01:00Z",
            "2026-07-02T00:02:00Z", 1, "ACCEPTED", "official_truth",
            "b" * 64, "[]", "FORMAL_NATIVE_ELIGIBLE", "c" * 64,
        ),
    )
    # Columns named rather than positional.  Both inserts here were written
    # as bare VALUES tuples, so each one broke the moment the table gained a
    # column -- ``settlement_timezone``, required since the station-local
    # season work -- and the failure ("28 columns but 27 values") says
    # nothing about which column is missing.
    conn.execute(
        """
        INSERT INTO calibration_historical_asof_model_run(
          model_run_id,model_id,station_id,target_local_date,
          settlement_timezone,run_init_utc,published_at_utc,received_at_utc,
          timestamps_complete,lead_bucket,maturity_key,run_cycle,raw_tmax_c,
          settlement_definition,provider_id,provider_role,source_class,
          aggregator_or_republisher,open_meteo,source_url,raw_object_ref,
          asof_status,capture_modes_json,historical_replay_eligible,
          prospective_strict_asof_eligible,authority_scope,
          source_object_sha256,content_sha256
        ) VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "run-1", "ECMWF", "RKPK", "2026-07-01", "Asia/Seoul",
            "2026-06-30T00:00:00Z", "2026-06-30T06:00:00Z",
            "2026-06-30T06:01:00Z", 1, "d1", "d1", "00Z", 30.0,
            "local-day", "ecmwf", "native", "official_native", 0, 0,
            "https://example.invalid", "raw/ref", "STRICT_ASOF", "[]", 1, 1,
            "FORMAL_NATIVE_ELIGIBLE", "d" * 64, "e" * 64,
        ),
    )
    conn.execute(
        """
        INSERT INTO calibration_model_station_residual VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "residual-1", "truth-1", "run-1", "RKPK", "ECMWF", "summer",
            "d1", "d1", "00Z", 1.0, "2026-07-02T00:02:00Z", "ACCEPTED",
            1, 1, "FORMAL_NATIVE_ELIGIBLE", "f" * 64, "1" * 64,
        ),
    )
    conn.execute(
        """
        INSERT INTO calibration_model_artifact VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "artifact-1", "RKPK", "ECMWF", "summer", "d1", "00Z",
            "local-day", "2026-07-02T00:00:00Z", 1, 1.0, 1.0, 1.0,
            "{}", "2026-07-02T00:00:00Z", "2026-08-01T00:00:00Z",
            "2" * 64,
        ),
    )
    conn.commit()
    return conn


def test_row_preserving_five_model_migration_rolls_back_and_repeats() -> None:
    conn = _legacy_connection()
    before = {
        table: conn.execute(f"SELECT * FROM {table}").fetchall()
        for table in (
            "calibration_historical_asof_model_run",
            "calibration_model_station_residual",
            "calibration_model_artifact",
        )
    }
    assert inspect_calibration_model_pool_schema(conn)["ok"] is False
    assert production_ssot_schema_status(conn)["ok"] is False

    conn.execute("BEGIN IMMEDIATE")
    first = upgrade_calibration_model_pool_schema_preserving_rows(conn)
    assert first["state"] == "migrated"
    assert inspect_calibration_model_pool_schema(conn)["ok"] is True
    conn.rollback()
    assert inspect_calibration_model_pool_schema(conn)["ok"] is False

    conn.execute("BEGIN IMMEDIATE")
    second = upgrade_calibration_model_pool_schema_preserving_rows(conn)
    conn.commit()
    assert second["rows_preserved"] == {
        "calibration_historical_asof_model_run": 1,
        "calibration_model_station_residual": 1,
        "calibration_model_artifact": 1,
    }
    assert inspect_calibration_model_pool_schema(conn)["ok"] is True
    assert {
        table: conn.execute(f"SELECT * FROM {table}").fetchall()
        for table in before
    } == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert upgrade_calibration_model_pool_schema_preserving_rows(conn)[
        "state"
    ] == "already_current"

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE calibration_model_artifact SET sample_n=2 "
            "WHERE calibration_artifact_id='artifact-1'"
        )
