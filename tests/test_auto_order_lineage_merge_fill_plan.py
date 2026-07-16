import json
import sqlite3
from pathlib import Path

from scripts import auto_order_lineage_merge_fill_plan as plan


def _create_schema(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE auto_orders (
            id INTEGER PRIMARY KEY,
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
        );
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY,
            decision_id TEXT,
            signal_id TEXT,
            order_intent_id TEXT,
            correlation_id TEXT,
            details_json TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            created_at TEXT
        );
        CREATE INDEX idx_auto_trade_decisions_order_id_action
        ON auto_trade_decisions(json_extract(details_json, '$.order_id'), action, created_at);
        CREATE TABLE auto_trade_jobs (
            id INTEGER PRIMARY KEY,
            decision_id TEXT,
            signal_id TEXT,
            order_intent_id TEXT,
            correlation_id TEXT,
            source TEXT,
            state TEXT,
            created_at TEXT
        );
        CREATE UNIQUE INDEX idx_phase0_auto_trade_jobs_order_intent_id
        ON auto_trade_jobs(order_intent_id)
        WHERE order_intent_id IS NOT NULL AND order_intent_id <> '';
        CREATE TABLE signal_to_order_timing (
            signal_id TEXT PRIMARY KEY,
            decision_id TEXT,
            order_intent_id TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_order(conn: sqlite3.Connection, *, order_id: str = "ord_1", intent: str = "intent_1") -> None:
    conn.execute(
        """
        INSERT INTO auto_orders(
            id, order_id, correlation_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        )
        VALUES(1, ?, 'corr_order', '', '', ?, 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:40Z')
        """,
        (order_id, intent),
    )


def test_auto_order_lineage_plan_fills_from_exact_decision_order_id(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(
            id, decision_id, signal_id, order_intent_id, correlation_id,
            details_json, slug, event_date, outcome, side, action, created_at
        )
        VALUES(10, 'dec_1', 'sig_1', 'intent_1', 'corr_decision', ?, 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:39Z')
        """,
        (json.dumps({"order_id": "ord_1"}),),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, resolve_candidates=True, enable_decision_order_id_lookup=True)

    assert report["ok"] is True
    assert report["safe_candidate_count"] == 1
    op = report["candidate_operations"][0]
    assert op["set_fields"] == {"decision_id": "dec_1", "signal_id": "sig_1"}
    assert op["source"] == "auto_trade_decisions.details_json.order_id"
    assert op["blockers"] == []
    assert report["guards"]["writes_archive_db"] is False


def test_auto_order_lineage_plan_uses_decision_payload_signal_when_columns_empty(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    details = {
        "order_id": "ord_1",
        "signal_to_order_timing": {
            "signal_id": "sig_payload",
            "order_intent_id": "intent_1",
        },
    }
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(
            id, decision_id, signal_id, order_intent_id, correlation_id,
            details_json, slug, event_date, outcome, side, action, created_at
        )
        VALUES(10, '', '', '', '', ?, 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:39Z')
        """,
        (json.dumps(details),),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, resolve_candidates=True, enable_decision_order_id_lookup=True)

    op = report["candidate_operations"][0]
    assert op["set_fields"]["decision_id"].startswith("dec_")
    assert op["set_fields"]["signal_id"] == "sig_payload"
    assert report["ok"] is True


def test_auto_order_lineage_plan_allows_posterior_order_without_signal(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(
            id, decision_id, signal_id, order_intent_id, correlation_id,
            details_json, slug, event_date, outcome, side, action, created_at
        )
        VALUES(10, 'dec_1', '', 'intent_1', 'corr_decision', ?, 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:39Z')
        """,
        (json.dumps({"order_id": "ord_1", "strategy": "target_realization_binary_settlement"}),),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, resolve_candidates=True, enable_decision_order_id_lookup=True)

    assert report["ok"] is True
    assert report["safe_candidate_count"] == 1
    op = report["candidate_operations"][0]
    assert op["set_fields"] == {"decision_id": "dec_1"}
    assert op["blockers"] == []


def test_auto_order_lineage_plan_requires_signal_for_hard_source_order(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(
            id, decision_id, signal_id, order_intent_id, correlation_id,
            details_json, slug, event_date, outcome, side, action, created_at
        )
        VALUES(10, 'dec_1', '', 'intent_1', 'corr_decision', ?, 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:39Z')
        """,
        (
            json.dumps(
                {
                    "order_id": "ord_1",
                    "hard_source_direct": True,
                    "signal_to_order_timing": {"awc_hard_source_signal_at": "2026-07-03T09:05:38Z"},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, resolve_candidates=True, enable_decision_order_id_lookup=True)

    assert report["ok"] is False
    op = report["candidate_operations"][0]
    assert op["set_fields"] == {"decision_id": "dec_1"}
    assert "strong_lineage_source_missing_required_fields:signal_id" in op["blockers"]


def test_auto_order_lineage_plan_blocks_ambiguous_decision_order_id(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    for row_id in (10, 11):
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
                id, decision_id, signal_id, order_intent_id, correlation_id,
                details_json, slug, event_date, outcome, side, action, created_at
            )
            VALUES(?, ?, ?, 'intent_1', 'corr', ?, 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:39Z')
            """,
            (row_id, f"dec_{row_id}", f"sig_{row_id}", json.dumps({"order_id": "ord_1"})),
        )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, resolve_candidates=True, enable_decision_order_id_lookup=True)

    assert report["ok"] is False
    assert report["blocked_candidate_count"] == 1
    assert "auto_trade_decisions.details_json.order_id:decision_order_id_anchor_ambiguous" in report["candidate_operations"][0]["blockers"]


def test_auto_order_lineage_plan_does_not_guess_from_market_similarity(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(
            id, decision_id, signal_id, order_intent_id, correlation_id,
            details_json, slug, event_date, outcome, side, action, created_at
        )
        VALUES(10, 'dec_looks_close', 'sig_looks_close', '', 'corr',
            '{}', 'slug', '2026-07-03', '32°C', 'YES', 'entry', '2026-07-03T09:05:39Z')
        """
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, resolve_candidates=True)

    assert report["ok"] is False
    op = report["candidate_operations"][0]
    assert op["set_fields"] == {}
    assert "no_strong_lineage_source" in op["blockers"]


def test_auto_order_lineage_plan_can_fill_from_unique_job_intent(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, decision_id, signal_id, order_intent_id, correlation_id, source, state, created_at
        )
        VALUES(20, 'dec_job', 'sig_job', 'intent_1', 'corr_job', 'hard_source_direct', 'done', '2026-07-03T09:05:39Z')
        """
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, resolve_candidates=True)

    assert report["ok"] is True
    op = report["candidate_operations"][0]
    assert op["set_fields"] == {"decision_id": "dec_job", "signal_id": "sig_job"}
    assert op["source"] == "auto_trade_jobs.order_intent_id"


def test_auto_order_lineage_plan_default_does_not_resolve_candidates(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.commit()
    conn.close()

    report = plan.build_plan(db)

    assert report["candidate_resolution_enabled"] is False
    assert report["candidate_count"] == 0
    assert report["safe_candidate_count"] == 0
    assert "candidate_resolution_disabled" in report["blockers"]
    assert report["guards"]["executes_sql_writes"] is False


def test_auto_order_lineage_plan_ignores_legacy_scope(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute("UPDATE auto_orders SET created_at='2000-01-01T00:00:00+00:00' WHERE id=1")
    conn.commit()
    conn.close()

    report = plan.build_plan(
        db,
        resolve_candidates=True,
        ignore_legacy_orders=True,
        order_since_hours=1,
    )

    assert report["ok"] is True
    assert report["needs_apply"] is False
    assert report["missing_order_count"] == 0
    assert report["total_order_count"] == 1
    assert report["scoped_order_count"] == 0
    assert report["legacy_ignored_count"] == 1


def test_auto_order_lineage_plan_ignores_orders_before_absolute_date(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute("UPDATE auto_orders SET created_at='2026-07-07T03:59:59+00:00' WHERE id=1")
    conn.commit()
    conn.close()

    report = plan.build_plan(
        db,
        resolve_candidates=True,
        ignore_legacy_orders=True,
        ignore_orders_before="2026-07-07",
    )

    assert report["ok"] is True
    assert report["needs_apply"] is False
    assert report["missing_order_count"] == 0
    assert report["scoped_order_count"] == 0
    assert report["legacy_ignored_count"] == 1
    assert report["order_scope"]["cutoff_created_at"] == "2026-07-07T04:00:00+00:00"


def test_auto_order_lineage_plan_keeps_orders_on_absolute_date(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_schema(db)
    conn = sqlite3.connect(db)
    _insert_order(conn)
    conn.execute("UPDATE auto_orders SET created_at='2026-07-07T04:00:00+00:00' WHERE id=1")
    conn.commit()
    conn.close()

    report = plan.build_plan(
        db,
        resolve_candidates=True,
        ignore_legacy_orders=True,
        ignore_orders_before="2026-07-07",
    )

    assert report["ok"] is False
    assert report["missing_order_count"] == 1
    assert report["scoped_order_count"] == 1
    assert report["legacy_ignored_count"] == 0
