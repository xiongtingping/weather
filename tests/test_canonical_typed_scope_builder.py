from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.canonical_contract import CanonicalTypedScopeEnvelope
from bayes_basket_target.canonical_typed_scope_builder import (
    CanonicalTypedScopeBuildError,
    build_canonical_typed_scope_envelope,
)


AS_OF = "2026-08-03T21:08:00Z"


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "typed.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE canonical_ssot_fact(
              fact_id TEXT PRIMARY KEY,fact_type TEXT,scope_key TEXT,
              content_sha256 TEXT,fact_json TEXT,lineage_json TEXT,
              ingested_at_utc TEXT);
            CREATE TABLE basket_probability_envelope(
              envelope_id TEXT PRIMARY KEY,stage TEXT,station_id TEXT,
              event_date TEXT,decision_at_utc TEXT,posterior_sha256 TEXT,
              lineage_json TEXT,content_sha256 TEXT,state TEXT,created_at TEXT);
            CREATE TABLE basket_official_ws_snapshot(
              snapshot_id TEXT PRIMARY KEY,market_id TEXT,station_id TEXT,
              event_date TEXT,sequence INTEGER,observed_at TEXT,
              buckets_json TEXT,content_sha256 TEXT,quote_source TEXT,
              state TEXT,created_at TEXT);
            """
        )
        observation = {
            "observation_id": "rtobs-1",
            "station_id": "KATL",
            "event_date": "2026-07-20",
            "settlement_timezone": "America/New_York",
            "revision_no": 1,
            "sequence": 1,
            "observed_at_utc": "2026-07-20T12:00:00Z",
            "canonical_committed_at_utc": "2026-07-21T05:00:00Z",
            "qc_state": "ACCEPTED",
            "halt_state": "CLEAR",
            "source_lane": "AWC",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        }
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                "truth_observation_revision:" + "1" * 64,
                "truth_observation_revision",
                "KATL|rtobs-1|1",
                "1" * 64,
                json.dumps(observation),
                "{}",
                "2026-07-21T05:00:00Z",
            ),
        )
        day_bounds = {
            "station_id": "KATL", "event_date": "2026-07-20",
            "settlement_timezone": "America/New_York", "bounds_version": 1,
            "canonical_committed_at_utc": "2026-07-21T05:00:00Z",
            "closed_day": True, "source_lane": "AWC",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        }
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                "truth_observation_day_bounds:" + "a" * 64,
                "truth_observation_day_bounds",
                "KATL|2026-07-20|America/New_York|1",
                "a" * 64,
                json.dumps(day_bounds), "{}", "2026-07-21T05:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "p0m-1",
                "P0-M",
                "KATL",
                "2026-08-01",
                "2026-07-31T12:00:00Z",
                "2" * 64,
                json.dumps(
                    {
                        "p0_root_content_sha256": "3" * 64,
                        "forecast_revision_id": "taf-revision-1",
                    }
                ),
                "4" * 64,
                "COMMITTED",
                "2026-07-31T12:00:01Z",
            ),
        )
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "p1-awc",
                "P1",
                "KATL",
                "2026-08-01",
                "2026-08-01T12:00:00Z",
                "5" * 64,
                json.dumps(
                    {
                        "source_lane": "AWC",
                        "canonical_observation_event_id": "rtobs-1",
                    }
                ),
                "6" * 64,
                "COMMITTED",
                "2026-08-01T12:00:01Z",
            ),
        )
        # A later non-AWC row is outside the formal P1 source lane and must not
        # displace the latest eligible AWC row.
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "p1-legacy",
                "P1",
                "KATL",
                "2026-08-01",
                "2026-08-01T13:00:00Z",
                "7" * 64,
                json.dumps(
                    {
                        "source_lane": "AMSC",
                        "canonical_observation_event_id": "legacy-1",
                    }
                ),
                "8" * 64,
                "COMMITTED",
                "2026-08-01T13:00:01Z",
            ),
        )
        conn.execute(
            "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws-1",
                "market-1",
                "KATL",
                "2026-08-01",
                7,
                "2026-08-01T12:30:00Z",
                json.dumps(
                    [
                        {"token_id": "yes-1", "quote_source": "OFFICIAL_CLOB_WS"},
                        {"token_id": "yes-2", "quote_source": "OFFICIAL_CLOB_WS"},
                    ]
                ),
                "9" * 64,
                "OFFICIAL_CLOB_WS",
                "COMMITTED",
                "2026-08-01T12:30:01Z",
            ),
        )
    return path


@pytest.mark.parametrize(
    "scope,expected_records",
    (
        ("TRUTH_OBSERVATION", 1),
        ("TRUTH_DAY", 1),
        ("TAF_P0M", 1),
        ("TRAJECTORY_P1", 1),
        ("MARKET_WS", 2),
    ),
)
def test_real_source_scopes_build_canonical_candidate_envelopes(
    tmp_path: Path, scope: str, expected_records: int
) -> None:
    path = _db(tmp_path)
    before = path.stat()
    envelope, receipt = build_canonical_typed_scope_envelope(
        path, scope_type=scope, as_of_utc=AS_OF
    )
    assert envelope is not None
    validated = CanonicalTypedScopeEnvelope.from_mapping(envelope)
    assert validated.scope_type == scope
    assert len(validated.records) == expected_records
    expected_state = "DATA_READY" if scope == "TRUTH_OBSERVATION" else "CANDIDATE_ONLY"
    assert all(record.state == expected_state for record in validated.records)
    assert all(
        record.blockers
        == (() if scope == "TRUTH_OBSERVATION" else ("ACTIVATION_ROOT_MISSING",))
        for record in validated.records
    )
    assert receipt["state"] == "CANDIDATE_ONLY"
    assert receipt["writes_database"] is False
    after = path.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_p1_uses_exact_awc_lineage_and_not_later_legacy_row(tmp_path: Path) -> None:
    envelope, _receipt = build_canonical_typed_scope_envelope(
        _db(tmp_path), scope_type="TRAJECTORY_P1", as_of_utc=AS_OF
    )
    assert envelope is not None
    key = envelope["records"][0]["natural_key"]
    assert key["observation_id"] == "rtobs-1"
    assert key["probability_root_sha256"] == "5" * 64


@pytest.mark.parametrize(
    "scope,fields",
    (
        ("P0_ACTIVATION", ["activation_content_sha256"]),
        (
            "P0_SNAPSHOT",
            [
                "p0_snapshot_content_sha256",
                "probability_envelope_content_sha256",
                "market_bucket_bridge_content_sha256",
            ],
        ),
        ("STRATEGY_POLICY", ["strategy_policy_activation_content_sha256"]),
        (
            "STRATEGY_UTILITY",
            ["candidate_inventory_sha256", "baseline_inventory_sha256"],
        ),
        ("RUNTIME", ["runtime_release_id"]),
    ),
)
def test_missing_producer_scopes_return_receipt_without_fake_envelope(
    tmp_path: Path, scope: str, fields: list[str]
) -> None:
    envelope, receipt = build_canonical_typed_scope_envelope(
        _db(tmp_path), scope_type=scope, as_of_utc=AS_OF
    )
    assert envelope is None
    assert receipt["state"] == "BLOCKED"
    assert receipt["blockers"] == ["DATA_MISSING"]
    assert receipt["missing_producer_fields"] == fields


def test_missing_taf_revision_fails_closed(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE basket_probability_envelope SET lineage_json=? "
            "WHERE envelope_id='p0m-1'",
            (json.dumps({"p0_root_content_sha256": "3" * 64}),),
        )
    with pytest.raises(
        CanonicalTypedScopeBuildError, match="p0m_taf_revision_id_missing"
    ):
        build_canonical_typed_scope_envelope(
            path, scope_type="TAF_P0M", as_of_utc=AS_OF
        )
