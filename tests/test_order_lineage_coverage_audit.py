import json
import sqlite3
from pathlib import Path

from scripts import order_lineage_coverage_audit as audit
from scripts import ssot_chain_replay_audit
from signal_to_order_ledger_writer import SIGNAL_TO_ORDER_TIMING_COLUMNS


def _create_orders_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE auto_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE,
            correlation_id TEXT,
            decision_id TEXT,
            signal_id TEXT,
            order_intent_id TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            created_at TEXT
        )
        """
    )


def test_order_lineage_coverage_passes_when_required_lineage_present(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_1', 'corr_1', 'dec_1', 'sig_1', 'oint_1',
               'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:00Z')
        """
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(db, limit=5)

    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["missing_by_column"] == {
        "order_id": 0,
        "correlation_id": 0,
        "decision_id": 0,
        "signal_id": 0,
        "order_intent_id": 0,
    }


def test_order_lineage_coverage_blocks_missing_decision_and_signal(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_legacy', 'corr_legacy', 'oint_legacy',
               'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:00Z')
        """
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(db, limit=5)

    assert report["ok"] is False
    assert report["missing_by_column"]["decision_id"] == 1
    assert report["missing_by_column"]["signal_id"] == 1
    assert "auto_orders_decision_id_missing" in report["blockers"]
    assert "auto_orders_signal_id_missing_for_hard_source_signal" not in report["blockers"]
    assert report["signal_applicability"]["not_applicable_missing_count"] == 1


def test_order_lineage_coverage_ignores_legacy_scope(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_legacy', 'corr_legacy', 'oint_legacy',
               'slug', '2026-07-03', '32°C', 'YES', 'entry', '2000-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(db, limit=5, ignore_legacy_orders=True, order_since_hours=1)

    assert report["ok"] is True
    assert report["total_order_count"] == 1
    assert report["scoped_order_count"] == 0
    assert report["legacy_ignored_count"] == 1
    assert report["missing_by_column"]["decision_id"] == 0
    assert "order_lineage_scoped_orders_empty" in report["warnings"]


def test_order_lineage_coverage_recent_scope_still_blocks(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_recent', 'corr_recent', 'oint_recent',
               'slug', '2026-07-03', '32°C', 'YES', 'entry', '2999-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(db, limit=5, ignore_legacy_orders=True, order_since_hours=1)

    assert report["ok"] is False
    assert report["scoped_order_count"] == 1
    assert report["legacy_ignored_count"] == 0
    assert report["missing_by_column"]["decision_id"] == 1
    assert "auto_orders_decision_id_missing" in report["blockers"]


def test_order_lineage_coverage_ignores_orders_before_absolute_date(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_july6', 'corr_legacy', 'oint_legacy',
               'slug', '2026-07-06', '32°C', 'YES', 'entry', '2026-07-07T03:59:59+00:00')
        """
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(
        db,
        limit=5,
        ignore_legacy_orders=True,
        ignore_orders_before="2026-07-07",
    )

    assert report["ok"] is True
    assert report["legacy_ignored_count"] == 1
    assert report["scoped_order_count"] == 0
    assert report["order_scope"]["cutoff_created_at"] == "2026-07-07T04:00:00+00:00"


def test_order_lineage_coverage_keeps_orders_on_absolute_date(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_july7', 'corr_recent', 'oint_recent',
               'slug', '2026-07-07', '32°C', 'YES', 'entry', '2026-07-07T04:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(
        db,
        limit=5,
        ignore_legacy_orders=True,
        ignore_orders_before="2026-07-07",
    )

    assert report["ok"] is False
    assert report["legacy_ignored_count"] == 0
    assert report["scoped_order_count"] == 1
    assert "auto_orders_decision_id_missing" in report["blockers"]


def test_order_lineage_coverage_allows_posterior_order_without_signal(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT,
            signal_id TEXT,
            details_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_posterior', 'corr_1', 'dec_1', '', 'oint_1',
               'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(decision_id, signal_id, details_json)
        VALUES('dec_1', '', ?)
        """,
        (json.dumps({"order_id": "ord_posterior", "strategy": "target_realization_binary_settlement"}),),
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(db, limit=5)

    assert report["ok"] is True
    assert report["missing_by_column"]["signal_id"] == 1
    assert report["signal_applicability"]["not_applicable_missing_count"] == 1
    assert report["signal_applicability"]["required_missing_count"] == 0


def test_order_lineage_coverage_blocks_hard_source_order_without_signal(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_orders_schema(conn)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT,
            signal_id TEXT,
            details_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES('ord_hard', 'corr_1', 'dec_1', '', 'oint_1',
               'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(decision_id, signal_id, details_json)
        VALUES('dec_1', '', ?)
        """,
        (
            json.dumps(
                {
                    "order_id": "ord_hard",
                    "hard_source_direct": True,
                    "signal_to_order_timing": {"awc_hard_source_signal_at": "2026-07-03T09:05:38Z"},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(db, limit=5)

    assert report["ok"] is False
    assert "auto_orders_signal_id_missing_for_hard_source_signal" in report["blockers"]
    assert report["signal_applicability"]["required_missing_count"] == 1


def test_order_lineage_coverage_can_replay_sample_order(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    timing_columns = []
    for column in SIGNAL_TO_ORDER_TIMING_COLUMNS:
        suffix = " PRIMARY KEY" if column == "signal_id" else ""
        timing_columns.append(f"{column} TEXT{suffix}")
    conn.executescript(
        f"""
        CREATE TABLE signal_to_order_timing ({', '.join(timing_columns)});
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            will_order INTEGER,
            details_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_trade_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            source TEXT,
            state TEXT,
            payload_json TEXT,
            created_at TEXT,
            started_at TEXT,
            ended_at TEXT
        );
        """
    )
    _create_orders_schema(conn)
    timing = {
        "schema": "signal_to_order_timing_v1",
        "hard_source_runtime_contract": {
            "schema": "hard_source_runtime_contract_v1",
            "contract_version": ssot_chain_replay_audit._current_hard_source_runtime_contract_version(),
        },
        "hard_source_runtime_contract_version": ssot_chain_replay_audit._current_hard_source_runtime_contract_version(),
        "signal_id": "sig_complete",
        "source_event_id": "src_awc_complete",
        "source_observation_id": "srcobs_awc_complete",
        "fact_snapshot_id": "fact_complete",
        "posterior_run_id": "post_complete",
        "awc_valid_time": "2026-07-03T09:00:00Z",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "preparation_started_at": "2026-07-03T09:00:00Z",
        "pre_signal_prepare_state": "prepared_before_deadline",
        "pre_signal_prepare_reason": "market_prefetch_refreshed",
        "pre_signal_prepare_complete": True,
        "pre_signal_prepare_missing_components": [],
        "pre_signal_prepare_required_components": [
            "market_prefetch",
            "market_mapping",
            "orderbook",
            "open_orders",
            "positions",
            "capital_caps",
        ],
        "pre_signal_prepare_components": {
            "market_prefetch": {"ok": True, "state": "prepared"},
            "market_mapping": {"ok": True, "state": "prepared"},
            "orderbook": {"ok": True, "state": "prepared"},
            "open_orders": {"ok": True, "state": "prepared"},
            "positions": {"ok": True, "state": "prepared"},
            "capital_caps": {"ok": True, "state": "prepared"},
        },
        "pre_signal_prepared_at": "2026-07-03T09:04:50Z",
        "awc_pre_signal_deadline_at": "2026-07-03T09:05:00Z",
        "pre_signal_prepared_before_deadline": True,
        "fact_written_at": "2026-07-03T09:05:38.100Z",
        "posterior_written_at": "2026-07-03T09:05:38.200Z",
        "decision_written_at": "2026-07-03T09:05:38.250Z",
        "job_enqueued_at": "2026-07-03T09:05:38.260Z",
        "worker_started_at": "2026-07-03T09:05:38.280Z",
        "order_submitted_at": "2026-07-03T09:05:40Z",
        "terminal_state": "submitted",
        "terminal_reason": "order_submitted",
        "orderbook_checked_at": "2026-07-03T09:05:38.300Z",
        "orderbook_check_source": "unit_test_orderbook",
        "orderbook_check_result": "pass",
        "risk_checked_at": "2026-07-03T09:05:38.900Z",
        "risk_check_source": "unit_test_risk",
        "risk_check_result": "pass",
        "idempotency_checked_at": "2026-07-03T09:05:39.100Z",
        "idempotency_key": "clobsvc:test-complete",
        "submit_requested_at": "2026-07-03T09:05:39.100Z",
        "exchange_ack_at": "2026-07-03T09:05:39.700Z",
        "order_ack_at": "2026-07-03T09:05:39.700Z",
        "order_ack_status": "submitted",
        "terminal_at": "2026-07-03T09:05:40Z",
        "signal_to_order_slo_met": True,
    }
    timing_json = json.dumps(timing)
    columns = [column for column in SIGNAL_TO_ORDER_TIMING_COLUMNS if column in timing]
    conn.execute(
        f"INSERT INTO signal_to_order_timing ({', '.join(columns)}, timing_json) VALUES ({', '.join('?' for _ in columns)}, ?)",
        tuple(str(timing[column]) for column in columns) + (timing_json,),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-07-03', 'slug', '2026-07-03', '2026-07-03T09:05:39Z', ?, '2026-07-03T09:05:39Z')
        """,
        (json.dumps({"signal_to_order_timing": timing, "fact_snapshot_id": "fact_complete", "posterior_run_id": "post_complete"}),),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(slug, event_date, outcome, side, action, will_order, details_json, created_at)
        VALUES('slug', '2026-07-03', '32°C', 'YES', 'entry', 1, ?, '2026-07-03T09:05:39Z')
        """,
        (json.dumps({"signal_to_order_timing": timing, "hard_source_direct": True}),),
    )
    decision_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO auto_orders(
            order_id, correlation_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES(?, 'corr_1', ?, 'sig_complete', 'oint_1',
               'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:40Z')
        """,
        ("ord_1", str(decision_id)),
    )
    conn.commit()
    conn.close()

    report = audit.build_audit(db, limit=5, max_replay_samples=1, since_hours=24 * 14)

    assert report["ok"] is True
    assert report["replay_sample_count"] == 1
    assert report["replay_samples"][0]["order_id"] == "ord_1"
    assert report["replay_samples"][0]["ok"] is True
