from __future__ import annotations

import sqlite3

from scripts.audit_regional_model_candidate_closure import (
    _load_republisher_temporal_evidence,
    _merge_republisher_temporal_evidence,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE calibration_republisher_historical_oos_contract (
          historical_oos_contract_id TEXT,
          ensemble_system_id TEXT,
          station_id TEXT,
          target_local_date TEXT,
          historical_oos_eligible INTEGER,
          station_day_closed INTEGER,
          awc_truth_source_lane TEXT,
          receipt_class TEXT,
          temporal_scope TEXT,
          future_event_count INTEGER,
          archive_lead_bucket TEXT,
          run_cycle TEXT
        );
        CREATE TABLE calibration_republisher_ensemble_historical_member_run (
          ensemble_system_id TEXT,
          station_id TEXT,
          target_local_date TEXT,
          historical_oos_eligible INTEGER,
          historical_oos_contract_id TEXT,
          archive_lead_bucket TEXT,
          run_cycle TEXT,
          transport_provider_id TEXT
        );
        CREATE TABLE calibration_republisher_ensemble_member_run (
          ensemble_system_id TEXT,
          station_id TEXT,
          target_local_date TEXT,
          transport_provider_id TEXT
        );
        """
    )
    return conn


def test_historical_republisher_contract_is_counted_separately_from_forward() -> None:
    conn = _connection()
    contract = (
        "contract-1",
        "ECCC_GEM_GLOBAL_ENS",
        "CYYZ",
        "2026-06-01",
        1,
        1,
        "AWC",
        "HISTORICAL_ASOF_REPUBLISHER_RECEIPT",
        "CLOSED_HISTORICAL_AWC_TRUTH",
        0,
        "d0",
        "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
    )
    conn.execute(
        "INSERT INTO calibration_republisher_historical_oos_contract VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        contract,
    )
    conn.executemany(
        "INSERT INTO calibration_republisher_ensemble_historical_member_run VALUES(?,?,?,?,?,?,?,?)",
        [
            (
                "ECCC_GEM_GLOBAL_ENS", "CYYZ", "2026-06-01", 1,
                "contract-1", "d0", "AGGREGATOR_UNKNOWN_NATIVE_CYCLE", "OPEN_METEO",
            ),
            # This is a retained historical row, but no immutable strict-as-of
            # contract binds it; it must never become an OOS count.
            (
                "ECCC_GEM_GLOBAL_ENS", "CYYZ", "2026-06-02", 0,
                None, "d0", "AGGREGATOR_UNKNOWN_NATIVE_CYCLE", "OPEN_METEO",
            ),
        ],
    )
    # The target date is deliberately historical.  It remains a forward
    # packet/observational candidate because it lacks the historic contract.
    conn.execute(
        "INSERT INTO calibration_republisher_ensemble_member_run VALUES(?,?,?,?)",
        ("ECCC_GEM_GLOBAL_ENS", "CYYZ", "2026-06-03", "OPEN_METEO"),
    )

    evidence, digest = _load_republisher_temporal_evidence(conn)
    row = evidence[("ECCC_GEM_GLOBAL_ENS", "CYYZ")]

    assert len(digest) == 64
    assert row["historical_republisher_contract_count"] == 1
    assert row["historical_strict_oos_contract_count"] == 1
    assert row["historical_republisher_member_run_count"] == 2
    assert row["historical_strict_oos_member_run_count"] == 1
    assert row["historical_station_day_count"] == 2
    assert row["historical_strict_oos_station_day_count"] == 1
    assert row["forward_observational_member_run_count"] == 1
    assert row["forward_observational_station_day_count"] == 1
    assert row["forward_evidence_role"] == "observational_candidate_not_oos"


def test_invalid_historical_contract_and_forward_packets_do_not_claim_strict_oos() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO calibration_republisher_historical_oos_contract VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "bad-contract", "DWD_ICON_EU_EPS", "EDDM", "2026-06-01", 0,
            1, "AWC", "HISTORICAL_ASOF_REPUBLISHER_RECEIPT",
            "CLOSED_HISTORICAL_AWC_TRUTH", 0, "d0",
            "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        ),
    )
    conn.execute(
        "INSERT INTO calibration_republisher_ensemble_historical_member_run VALUES(?,?,?,?,?,?,?,?)",
        (
            "DWD_ICON_EU_EPS", "EDDM", "2026-06-01", 0, "bad-contract",
            "d0", "AGGREGATOR_UNKNOWN_NATIVE_CYCLE", "OPEN_METEO",
        ),
    )
    conn.execute(
        "INSERT INTO calibration_republisher_ensemble_member_run VALUES(?,?,?,?)",
        ("DWD_ICON_EU_EPS", "EDDM", "2026-06-02", "OPEN_METEO"),
    )

    evidence, _ = _load_republisher_temporal_evidence(conn)
    row = evidence[("DWD_ICON_EU_EPS", "EDDM")]
    assert row["historical_republisher_contract_count"] == 1
    assert row["historical_strict_oos_contract_count"] == 0
    assert row["historical_strict_oos_member_run_count"] == 0
    assert row["forward_observational_member_run_count"] == 1

    merged = _merge_republisher_temporal_evidence([row])
    assert merged["historical_strict_oos_contract_count"] == 0
    assert merged["forward_evidence_role"] == "observational_candidate_not_oos"
