from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from scripts.audit_historical_probability_vertical_chain import (
    audit_historical_probability_vertical_chain,
)


def _sha(char: str) -> str:
    return char * 64


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE basket_probability_envelope (
          stage TEXT, checkpoint_id TEXT, station_id TEXT, event_date TEXT,
          settlement_timezone TEXT, decision_at_utc TEXT, content_sha256 TEXT,
          lineage_json TEXT, state TEXT
        );
        CREATE TABLE basket_target_observation_facts (
          observation_id TEXT, station_id TEXT, event_date TEXT, source_lane TEXT,
          report_type TEXT, raw_sha256 TEXT, canonical_committed_at TEXT,
          content_sha256 TEXT
        );
        CREATE TABLE calibration_station_daily_high_truth (
          station_id TEXT, local_date TEXT, source_lane TEXT, authority_scope TEXT,
          qc_status TEXT, timestamps_complete INTEGER, source_payload_sha256 TEXT
        );
        CREATE TABLE calibration_forward_taf_capture (
          revision_id TEXT, station_id TEXT, event_date TEXT, source_object_sha256 TEXT,
          issued_at_utc TEXT, provider_available_at_utc TEXT, source_class TEXT,
          authority_scope TEXT
        );
        CREATE TABLE calibration_native_ensemble_activation (
          content_sha256 TEXT, artifact_kind TEXT, artifact_id TEXT, station_id TEXT,
          authority_scope TEXT, valid_from_utc TEXT, valid_until_utc TEXT
        );
        CREATE TABLE calibration_cross_native_ensemble_artifact (
          content_sha256 TEXT, cross_ensemble_artifact_id TEXT, station_id TEXT,
          ensemble_system_count INTEGER, independent_family_count INTEGER,
          probability_pathway TEXT, valid_from_utc TEXT, valid_until_utc TEXT
        );
        """
    )


def _insert_chain(path: Path, *, legacy_p0: bool = False, awc: bool = True) -> None:
    with sqlite3.connect(path) as conn:
        _schema(conn)
        p0_sha, p0m_sha, p1_sha = _sha("a"), _sha("b"), _sha("c")
        cross_sha, activation_sha, taf_sha, observation_sha = (
            _sha("d"), _sha("e"), _sha("f"), _sha("1")
        )
        p0_lineage = (
            {"legacy": "yes"}
            if legacy_p0
            else {
                "p0_probability_pathway": "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE",
                "native_authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                "native_transport_provider": "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
                "aggregator_fallback_p0_eligible": "false",
                "ensemble_system_ids_json": json.dumps(["ECMWF", "GEFS"]),
                "independence_family_ids_json": json.dumps(["ECMWF", "NOAA_GEFS"]),
                "native_cross_activation_sha256": activation_sha,
                "native_cross_ensemble_artifact_sha256": cross_sha,
            }
        )
        p0m_lineage = {
            "p0_root_checkpoint_id": "p0",
            "p0_root_content_sha256": p0_sha,
            "predecessor_checkpoint_id": "p0",
            "forecast_revision_id": "taf-1",
            "forecast_source_object_sha256": taf_sha,
            "taf_revision_artifact_sha256": _sha("2"),
            "taf_revision_proof_sha256": _sha("3"),
            "taf_physical_features_sha256": _sha("4"),
            "taf_physical_confirmation_state": "CONFIRMS",
            "taf_hard_exclusion": "FORBIDDEN",
        }
        p1_lineage = {
            "p0_root_checkpoint_id": "p0",
            "predecessor_checkpoint_id": "p0m",
            "source_lane": "AWC" if awc else "AMSC",
            "report_type": "METAR",
            "canonical_observation_event_id": "obs-1",
            "canonical_observation_sha256": observation_sha,
            "canonical_observation_committed_at": "2026-07-30T02:00:00Z",
        }
        conn.executemany(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("P0", "p0", "RKPK", "2026-07-30", "Asia/Seoul", "2026-07-30T00:00:00Z", p0_sha, json.dumps(p0_lineage), "COMMITTED"),
                ("P0-M", "p0m", "RKPK", "2026-07-30", "Asia/Seoul", "2026-07-30T01:00:00Z", p0m_sha, json.dumps(p0m_lineage), "COMMITTED"),
                ("P1", "p1", "RKPK", "2026-07-30", "Asia/Seoul", "2026-07-30T02:00:01Z", p1_sha, json.dumps(p1_lineage), "COMMITTED"),
            ],
        )
        conn.execute(
            "INSERT INTO basket_target_observation_facts VALUES (?,?,?,?,?,?,?,?)",
            ("obs-1", "RKPK", "2026-07-30", "AWC" if awc else "AMSC", "METAR", observation_sha, "2026-07-30T02:00:00Z", _sha("5")),
        )
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES (?,?,?,?,?,?,?)",
            ("RKPK", "2026-07-30", "AWC", "FORMAL_NATIVE_ELIGIBLE", "ACCEPTED", 1, _sha("6")),
        )
        conn.execute(
            "INSERT INTO calibration_forward_taf_capture VALUES (?,?,?,?,?,?,?,?)",
            ("taf-1", "RKPK", "2026-07-30", taf_sha, "2026-07-29T18:00:00Z", "2026-07-29T18:05:00Z", "official_forward_native_receipt", "PRODUCTION_NO_LIVE_ONLY"),
        )
        if not legacy_p0:
            conn.execute(
                "INSERT INTO calibration_cross_native_ensemble_artifact VALUES (?,?,?,?,?,?,?,?)",
                (cross_sha, "cross-1", "RKPK", 2, 2, "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE", "2026-07-29T00:00:00Z", "2026-08-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO calibration_native_ensemble_activation VALUES (?,?,?,?,?,?,?)",
                (activation_sha, "CROSS_NATIVE_ENSEMBLE", "cross-1", "RKPK", "PRODUCTION_NO_LIVE_ONLY", "2026-07-29T00:00:00Z", "2026-08-01T00:00:00Z"),
            )


def test_formal_native_awc_chain_passes(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _insert_chain(db)
    result = audit_historical_probability_vertical_chain(db)
    assert result["status"] == "PASS"
    assert result["counts"]["formal_no_live_chain_scopes"] == 1
    assert result["chains"][0]["status"] == "FORMAL_NO_LIVE_CHAIN_PASS"
    assert result["writes_target_ssot"] is False
    assert result["orders_submitted"] == 0


def test_legacy_p0_remains_candidate_not_formal(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _insert_chain(db, legacy_p0=True)
    result = audit_historical_probability_vertical_chain(db)
    assert result["status"] == "PARTIAL_FAIL_CLOSED"
    assert result["counts"]["formal_no_live_chain_scopes"] == 0
    assert result["chains"][0]["status"] == "CANDIDATE_CHAIN_ONLY_LEGACY_P0"
    assert any(item["code"] == "p0_not_current_native_pathway" for item in result["chains"][0]["blockers"])


def test_amsc_cannot_complete_awc_p1_chain(tmp_path: Path) -> None:
    db = tmp_path / "amsc.db"
    _insert_chain(db, awc=False)
    result = audit_historical_probability_vertical_chain(db)
    assert result["status"] == "PARTIAL_FAIL_CLOSED"
    assert result["chains"][0]["status"] == "BLOCKED_FAIL_CLOSED"
    assert any(item["code"] == "p1_awc_observation_binding_invalid" for item in result["chains"][0]["blockers"])
