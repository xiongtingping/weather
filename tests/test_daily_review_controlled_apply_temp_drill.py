import json
import sqlite3
from pathlib import Path

from scripts import daily_review_controlled_apply_temp_drill as drill


SLUG = "highest-temperature-in-chengdu-on-july-4-2026"


def _create_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "archive.db"
    fact_payload = {
        "slug": SLUG,
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "source_observations": [{"source_event_id": "srcevt_drill_1"}],
        "distribution": [
            {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "30",
            "target_probability": 0.8,
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                city TEXT,
                station_id TEXT,
                settlement_actual REAL,
                rounding_rule TEXT,
                market_closed INTEGER,
                event_end_utc TEXT,
                created_at TEXT
            );
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_as_of TEXT,
                fact_json TEXT,
                created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                reasons_json TEXT,
                details_json TEXT,
                created_at TEXT
            );
            CREATE TABLE auto_orders (
                id INTEGER PRIMARY KEY,
                order_id TEXT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                price REAL,
                size REAL,
                strategy TEXT,
                created_at TEXT,
                order_status TEXT,
                order_checked_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', 'chengdu', 'ZUUU', 30.0,
                   'whole_degrees', 1, '2026-07-04T16:00:00Z', '2026-07-05T00:10:00Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO event_fact_history
                (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', '2026-07-04T09:05:38Z', ?, '2026-07-04T09:05:39Z')
            """,
            (SLUG, json.dumps(fact_payload)),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
                (id, run_id, run_date, slug, event_date, outcome, side, action,
                 decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at)
            VALUES(1, 7, '2026-07-04', ?, '2026-07-04', '30', 'YES', 'entry',
                   'entry', 1, '[]', '[]', '{}', '2026-07-04T09:05:39Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO auto_orders
                (id, order_id, slug, event_date, outcome, side, action, price,
                 size, strategy, created_at, order_status)
            VALUES(1, 'ord_drill_1', ?, '2026-07-04', '30', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-04T09:05:40Z', 'submitted')
            """,
            (SLUG,),
        )
    return db_path


def _table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    return row is not None


def test_compact_acceptance_preserves_post_apply_blocker_samples() -> None:
    compact = drill._compact_acceptance(
        {
            "schema": "daily_review_post_apply_acceptance_audit_v1",
            "ok": False,
            "read_only": True,
            "coverage": {
                "settled_event_count": 1,
                "reviewed_event_count": 1,
                "missing_review_count": 0,
                "coverage_ratio": 1.0,
                "item_blocker_counts": {
                    "posterior_lineage_failed": 1,
                    "review_replay_fact_missing": 1,
                },
            },
            "blockers": ["posterior_lineage_failed", "review_replay_fact_missing"],
            "item_blocker_samples": [
                {
                    "slug": SLUG,
                    "event_date": "2026-07-04",
                    "review_id": "rev_drill_1",
                    "first_bad_link": "posterior",
                    "blockers": ["posterior_lineage_failed", "review_replay_fact_missing"],
                    "review_fact_snapshot_id": "fact_old",
                    "review_posterior_run_id": "post_old",
                    "latest_fact": {
                        "fact_id": 7,
                        "fact_snapshot_id": "fact_new",
                        "posterior_ssot_fact_present": False,
                    },
                    "posterior_lineage": {
                        "ok": False,
                        "posterior_target_source": "legacy_distribution",
                    },
                    "review_replay": {
                        "ok": False,
                        "source_counts": {"event_fact_history": 0},
                        "blockers": ["fact_missing"],
                    },
                }
            ],
        }
    )

    assert compact["ok"] is False
    assert compact["item_blocker_counts"] == {
        "posterior_lineage_failed": 1,
        "review_replay_fact_missing": 1,
    }
    assert compact["blocker_samples"][0]["slug"] == SLUG
    assert compact["blocker_samples"][0]["latest_fact"]["fact_id"] == 7
    assert compact["blocker_samples"][0]["review_replay"]["source_counts"]["event_fact_history"] == 0


def test_operation_max_items_output_does_not_narrow_apply_scope() -> None:
    assert drill._operation_max_items_output(limit=100, max_items_output=20) == 100
    assert drill._operation_max_items_output(limit=5, max_items_output=20) == 20


def test_copy_sqlite_backup_creates_readable_backup(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES('ok')")
        conn.commit()

    mode = drill._copy_sqlite_backup(source, backup)

    assert mode in {"apfs_clonefile", "sqlite_backup"}
    assert backup.exists()
    assert backup.stat().st_size > 0
    with sqlite3.connect(backup) as conn:
        row = conn.execute("SELECT value FROM sample WHERE id = 1").fetchone()
    assert row == ("ok",)


def test_controlled_apply_temp_drill_runs_formal_chain_without_production_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = _create_db(tmp_path)

    def fake_acceptance(*args, **kwargs):
        return {
            "schema": "daily_review_post_apply_acceptance_audit_v1",
            "ok": True,
            "read_only": True,
            "daily_weather_reviews_row_count": 1,
            "coverage": {
                "settled_event_count": 1,
                "reviewed_event_count": 1,
                "missing_review_count": 0,
                "coverage_ratio": 1.0,
            },
            "blockers": [],
            "warnings": ["ui_snapshot_lineage_audit_skipped"],
        }

    monkeypatch.setattr(drill.daily_review_post_apply_acceptance_audit, "build_report", fake_acceptance)

    result = drill.run_drill(
        db_path=source_db,
        since_date="2026-07-01",
        max_items_output=10,
    )

    assert result["ok"] is True
    assert result["read_only_production"] is True
    assert result["guards"]["writes_production_archive_db"] is False
    assert result["guards"]["writes_temp_archive_db"] is True
    assert result["apply_chain_ok"] is True
    assert result["post_apply_acceptance_ok"] is True
    assert result["manifest_decision_counts"]["confirm_repair"] == 1
    assert result["phase0"]["applied"] is True
    assert result["lineage_repair"]["applied_item_count"] == 1
    assert result["daily_review_persistence"]["aggregate_row_counts"]["daily_weather_reviews"] == 1
    assert result["daily_review_persistence"]["aggregate_row_counts"]["parameter_change_events"] == 0
    assert result["production_counts_before"] == result["production_counts_after"]
    assert _table_exists(source_db, "daily_weather_reviews") is False
    assert result["temp_db_retained"] is False
