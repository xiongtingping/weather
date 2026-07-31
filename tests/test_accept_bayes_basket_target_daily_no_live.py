from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scripts.accept_bayes_basket_target_daily_no_live as acceptance


H = "a" * 64
SERVICES = {
    "com.weather.reported_trajectory_no": 1001,
    "com.weather.market_ws": 1002,
    "com.weather.clob_order_service": 1003,
}
AS_OF = "2026-07-30T12:00:00Z"


def _create_db(path: Path, *, stale_ws: bool = False, missing_lineage: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE basket_strategy_activation_state(
                singleton INTEGER PRIMARY KEY, generation INTEGER, state TEXT,
                strategy_id TEXT, activation_sha256 TEXT, activation_json TEXT,
                updated_at TEXT
            );
            CREATE TABLE basket_probability_envelope(
                envelope_id TEXT PRIMARY KEY, stage TEXT, checkpoint_id TEXT,
                station_id TEXT, event_date TEXT, settlement_timezone TEXT,
                decision_at_utc TEXT, fresh_until_utc TEXT,
                bucket_ordinals_json TEXT, probabilities_json TEXT,
                posterior_version TEXT, posterior_sha256 TEXT, bridge_sha256 TEXT,
                lineage_json TEXT, content_sha256 TEXT, state TEXT, created_at TEXT
            );
            CREATE TABLE basket_official_ws_snapshot(
                snapshot_id TEXT PRIMARY KEY, market_id TEXT, station_id TEXT,
                event_date TEXT, settlement_timezone TEXT, sequence INTEGER,
                observed_at TEXT, quote_version TEXT, quote_observed_at TEXT,
                quote_expires_at TEXT, quote_freshness_policy_sha256 TEXT,
                quote_source TEXT, inventory_version TEXT, order_state_version TEXT,
                remaining_budget_usdc REAL, buckets_json TEXT, cash_phase_input_json TEXT,
                content_sha256 TEXT, state TEXT, created_at TEXT
            );
            CREATE TABLE basket_target_execution_cycles(
                cycle_id TEXT PRIMARY KEY, plan_id TEXT, station_id TEXT,
                event_date TEXT, settlement_timezone TEXT, strategy_lane TEXT,
                plan_sha256 TEXT, requested_reserve_usdc REAL, state TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE auto_trade_decisions(
                decision_id TEXT PRIMARY KEY, cycle_id TEXT UNIQUE, plan_id TEXT UNIQUE,
                station_id TEXT, event_date TEXT, settlement_timezone TEXT,
                strategy_lane TEXT, decided_state TEXT, will_order INTEGER,
                target_execution_hard_deadline_at TEXT, reason_codes_json TEXT,
                details_json TEXT, decision_sha256 TEXT, created_at TEXT
            );
            CREATE TABLE basket_target_execution_version_vectors(
                order_intent_id TEXT, cycle_id TEXT, decision_id TEXT,
                vector_policy TEXT, vector_sha256 TEXT, created_at TEXT
            );
            CREATE TABLE basket_target_order_intents(
                order_intent_id TEXT PRIMARY KEY, station_id TEXT,
                live_submit_authorized INTEGER
            );
            CREATE TABLE basket_target_order_events(event_id TEXT PRIMARY KEY, station_id TEXT);
            CREATE TABLE basket_target_execution_receipts(receipt_id TEXT PRIMARY KEY, station_id TEXT);
            CREATE TABLE basket_target_order_reconciliations(
                order_intent_id TEXT PRIMARY KEY, state TEXT
            );
            CREATE TABLE basket_policy_training_fact(
                fact_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT, stage TEXT,
                decision_id TEXT, decision_at_utc TEXT, content_sha256 TEXT,
                authority_scope TEXT
            );
            CREATE TABLE basket_policy_training_label(
                label_id TEXT PRIMARY KEY, fact_id TEXT, truth_id TEXT,
                truth_sha256 TEXT, content_sha256 TEXT, labeled_at_utc TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO basket_strategy_activation_state VALUES (1,1,'enabled',?,?,?,?)",
            (
                "target",
                H,
                json.dumps({
                    "execution_mode": "production_no_live",
                    "live_decision_write_authorized": False,
                    "live_submit_authorized": False,
                    "places_orders": False,
                    "calls_order_gateway": False,
                }),
                AS_OF,
            ),
        )
        for stage, suffix in (("P0", "p0"), ("P0-M", "p0m"), ("P1", "p1")):
            lineage = {} if missing_lineage else {
                "source": "NWP" if stage == "P0" else "TAF_METAR",
                "parent_envelope_id": suffix + "-parent",
                "same_day_historical_prior": stage == "P0",
            }
            conn.execute(
                "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    suffix, stage, "cp-" + suffix, "RKPK", "2026-07-30", "Asia/Seoul",
                    "2026-07-30T11:00:00Z", "2026-07-30T13:00:00Z", "[]", "{}",
                    "v1", H, H, json.dumps(lineage), H, "COMMITTED", AS_OF,
                ),
            )
        expiry = "2026-07-30T11:30:00Z" if stale_ws else "2026-07-30T13:00:00Z"
        conn.execute(
            "INSERT INTO basket_official_ws_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ws-1", "market-1", "RKPK", "2026-07-30", "Asia/Seoul", 1,
                "2026-07-30T11:50:00Z", "q1", "2026-07-30T11:50:00Z", expiry, H,
                "OFFICIAL_CLOB_WS", "inv-1", "os-1", 10.0, "{}", "{}", H,
                "COMMITTED", AS_OF,
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_execution_cycles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("cycle-1", "plan-1", "RKPK", "2026-07-30", "Asia/Seoul", "basket", H, 0.0, "prepared", AS_OF, AS_OF),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("decision-1", "cycle-1", "plan-1", "RKPK", "2026-07-30", "Asia/Seoul", "basket", "prepared", 0, None, "[]", "{}", H, AS_OF),
        )
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors VALUES (?,?,?,?,?,?)",
            (None, "cycle-1", "decision-1", "NO_EXECUTION", H, AS_OF),
        )
        conn.execute(
            "INSERT INTO basket_policy_training_fact VALUES (?,?,?,?,?,?,?,?)",
            ("fact-1", "RKPK", "2026-07-30", "P1", "decision-1", AS_OF, H, "PRODUCTION_NO_LIVE_FORWARD"),
        )


def _audit(path: Path, **kwargs):
    return acceptance.audit(
        path,
        station_id="RKPK",
        as_of_utc=AS_OF,
        service_probe=lambda: dict(SERVICES),
        **kwargs,
    )


def test_daily_acceptance_passes_and_reports_forward_oos_as_nonblocking(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    before = db.read_bytes()
    result = _audit(db)

    assert result["state"] == "PASS"
    assert result["ready_for_production_no_live"] is True
    assert result["checks"]["probability"]["stage_counts"] == {"P0": 1, "P0-M": 1, "P1": 1}
    assert result["checks"]["training"]["labels_state"] == "PENDING_SETTLEMENT"
    assert result["checks"]["forward_oos"]["blocks_production_no_live"] is False
    assert result["checks"]["forward_oos"]["blocks_parameter_or_weight_promotion"] is True
    assert db.read_bytes() == before


def test_service_missing_or_drift_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    result = acceptance.audit(
        db,
        station_id="RKPK",
        as_of_utc=AS_OF,
        service_probe=lambda: {"com.weather.market_ws": 2, "unexpected": 3},
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "user_service_set_drift" in result["blockers"]
    assert "user_service_missing:com.weather.reported_trajectory_no" in result["blockers"]


def test_stale_official_ws_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db, stale_ws=True)
    result = _audit(db)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "official_ws_evidence_stale" in result["blockers"]


def test_missing_probability_lineage_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db, missing_lineage=True)
    result = _audit(db)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any("probability_lineage_empty" in item for item in result["blockers"])


def test_forbidden_execution_boundary_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE auto_trade_decisions SET will_order=1")
    result = _audit(db)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "execution_will_order_nonzero" in result["blockers"]


def test_no_live_candidate_execution_vector_is_valid_without_order_submission(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE basket_target_execution_version_vectors "
            "SET vector_policy='EXECUTION'"
        )

    result = _audit(db)

    assert result["state"] == "PASS"
    assert result["checks"]["atomic_ledger"]["vector_policies"] == {
        "EXECUTION": 1
    }
    assert result["checks"]["execution_boundary"]["counts"][
        "live_submit_authorized_nonzero"
    ] == 0


def test_global_intent_table_without_station_column_is_still_audited(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE basket_target_order_intents")
        conn.execute(
            "CREATE TABLE basket_target_order_intents("
            "order_intent_id TEXT PRIMARY KEY,"
            "live_submit_authorized INTEGER)"
        )
        conn.execute(
            "INSERT INTO basket_target_order_intents VALUES('intent-1',1)"
        )

    result = _audit(db)

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "execution_live_submit_authorized_nonzero" in result["blockers"]


def test_missing_execution_authority_table_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE basket_target_execution_receipts")

    result = _audit(db)

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert (
        "execution_table_missing:basket_target_execution_receipts"
        in result["blockers"]
    )


def test_forward_oos_does_not_block_no_live_when_reported_as_incomplete(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE forward_oos_scorecard(state TEXT)")
        conn.execute("INSERT INTO forward_oos_scorecard VALUES ('INCOMPLETE')")
    result = _audit(db)
    assert result["state"] == "PASS"
    oos = result["checks"]["forward_oos"]
    assert oos["state"] == "REPORTED_ONLY"
    assert oos["promotion_scope"] == "FORMAL_PROMOTION_ONLY"
    assert oos["blocks_production_no_live"] is False


def test_row_cap_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    monkeypatch.setattr(acceptance, "MAX_LATEST_ROWS", 2)
    result = _audit(db)
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "probability_row_cap_exceeded" in result["blockers"]


def test_resource_probe_failure_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    result = _audit(
        db,
        resource_probe=lambda *_args, **_kwargs: {
            "ok": False,
            "state": "over_budget",
            "violations": ["target_database_wal_bytes_exceeded"],
        },
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "resource:target_database_wal_bytes_exceeded" in result["blockers"]


def test_command_writes_bounded_json_only_to_requested_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "target.db"
    _create_db(db)
    output = tmp_path / "daily.json"
    monkeypatch.setattr(acceptance, "_service_pids", lambda: dict(SERVICES))
    assert acceptance.main([
        "--db", str(db), "--station-id", "RKPK", "--as-of-utc", AS_OF,
        "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == acceptance.SCHEMA
    assert payload["read_only"] is True
