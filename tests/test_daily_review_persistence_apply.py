import json
import shutil
import sqlite3
from pathlib import Path

from scripts import daily_review_persistence_apply as apply_mod


SLUG = "highest-temperature-in-chengdu-on-july-4-2026"


def _create_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "archive.db"
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
            CREATE TABLE daily_weather_reviews (
                review_id TEXT PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_snapshot_id TEXT,
                first_bad_link TEXT,
                review_json TEXT,
                created_at TEXT
            );
            CREATE TABLE daily_calibration_records (
                daily_calibration_record_id TEXT PRIMARY KEY,
                review_id TEXT,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_snapshot_id TEXT,
                state TEXT,
                calibration_metrics_json TEXT,
                error_attribution_json TEXT,
                strata_json TEXT,
                record_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE bias_correction_candidates (
                calibration_candidate_id TEXT PRIMARY KEY,
                review_id TEXT,
                candidate_type TEXT,
                state TEXT,
                proposal_json TEXT,
                rollback_plan_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE parameter_change_events (
                id INTEGER PRIMARY KEY,
                calibration_candidate_id TEXT,
                state TEXT,
                changed_by TEXT,
                change_json TEXT,
                created_at TEXT
            );
            """
        )
        fact_payload = {
            "slug": SLUG,
            "event_date": "2026-07-04",
            "run_date": "2026-07-04",
            "source_observations": [{"source_event_id": "srcevt_apply_1"}],
            "lineage_state": {"fact_snapshot_id": "fact_apply_1"},
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "fact_snapshot_id": "fact_apply_1",
                "posterior_run_id": "post_apply_1",
                "target_bucket": "30",
                "target_probability": 0.8,
                "bucket_probabilities": [
                    {"outcome": "30", "probability": 0.8},
                    {"outcome": "31", "probability": 0.2},
                ],
            },
            "distribution": [
                {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
                {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
            ],
            "posterior_target_bucket_selection": {
                "target_bucket": "30",
                "target_probability": 0.8,
            },
        }
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', 'chengdu', 'ZUUU', 31.0,
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
            VALUES(1, 'ord_apply_1', ?, '2026-07-04', '30', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-04T09:05:40Z', 'submitted')
            """,
            (SLUG,),
        )
    return db_path


def _count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_daily_review_persistence_apply_contract_defaults_to_no_apply(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)

    contract = apply_mod.build_daily_review_persistence_apply_contract(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
    )

    assert contract["schema"] == "daily_review_persistence_apply_contract_v1"
    assert contract["ok"] is True
    assert contract["read_only"] is True
    assert contract["dry_run"] is True
    assert contract["ready_to_apply_now"] is False
    assert contract["batch_plan_ready_after_confirmation"] is True
    assert contract["planned_review_count"] == 1
    assert contract["planned_calibration_record_count"] == 1
    assert "apply_flag" in contract["apply_blockers"]
    assert "confirmation_phrase" in contract["apply_blockers"]
    assert "fresh_archive_db_backup" in contract["apply_blockers"]
    assert "writer_quiescence" in contract["apply_blockers"]
    assert contract["boundary"]["parameter_events_skipped_unless_separately_confirmed"] is True
    assert contract["guards"]["writes_archive_db"] is False
    assert _count(db_path, "daily_weather_reviews") == 0


def test_daily_review_persistence_apply_contract_can_reuse_prebuilt_batch_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = _create_db(tmp_path)

    def fail_build_batch_plan(*_args, **_kwargs):
        raise AssertionError("prebuilt batch plan must avoid rebuilding")

    monkeypatch.setattr(apply_mod.batch_plan, "build_batch_plan", fail_build_batch_plan)

    contract = apply_mod.build_daily_review_persistence_apply_contract(
        db_path,
        max_items_output=5,
        prebuilt_batch_plan={
            "schema": "daily_review_persistence_batch_plan_v1",
            "ok": True,
            "can_apply_after_schema_and_confirmation": True,
            "settled_event_count": 1,
            "effective_settled_event_count": 1,
            "excluded_settled_event_count": 0,
            "planned_review_count": 1,
            "planned_calibration_record_count": 1,
            "planned_candidate_count": 0,
            "planned_parameter_event_count": 0,
            "aggregate_row_counts": {"daily_weather_reviews": 1},
            "apply_blockers": ["explicit_user_confirmation_required"],
            "items": [
                {
                    "slug": SLUG,
                    "event_date": "2026-07-04",
                    "event_run_id": 1,
                    "settlement_actual": 30.0,
                    "review_id": "review_prebuilt",
                    "fact_snapshot_id": "fact_prebuilt",
                    "first_bad_link": "none",
                }
            ],
            "lineage_decision_apply_readiness": {"schema": "unit_readiness"},
        },
    )

    assert contract["batch_plan_source"] == "prebuilt_batch_plan"
    assert contract["planned_review_count"] == 1
    assert contract["item_keys"][0]["review_id"] == "review_prebuilt"
    assert contract["guards"]["writes_archive_db"] is False


def test_daily_review_persistence_apply_writes_temp_scope_and_skips_parameter_events_by_default(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    backup = tmp_path / "archive.db.persistence.bak"
    shutil.copy2(db_path, backup)

    result = apply_mod.apply_daily_review_persistence_batch(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        backup_path=backup,
        allow_apply=True,
        confirmation_phrase=apply_mod.CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["aggregate_row_counts"]["daily_weather_reviews"] == 1
    assert result["aggregate_row_counts"]["daily_calibration_records"] == 1
    assert result["aggregate_row_counts"]["bias_correction_candidates"] == 1
    assert result["aggregate_row_counts"]["parameter_change_events"] == 0
    assert result["guards"]["writes_parameter_change_events"] is False
    assert _count(db_path, "daily_weather_reviews") == 1
    assert _count(db_path, "daily_calibration_records") == 1
    assert _count(db_path, "bias_correction_candidates") == 1
    assert _count(db_path, "parameter_change_events") == 0


def test_daily_review_persistence_apply_parameter_events_need_separate_confirmation(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path)
    backup = tmp_path / "archive.db.persistence.bak"
    shutil.copy2(db_path, backup)

    blocked = apply_mod.apply_daily_review_persistence_batch(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        backup_path=backup,
        allow_apply=True,
        confirmation_phrase=apply_mod.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        allow_parameter_event_apply=True,
    )

    assert blocked["ok"] is False
    assert blocked["applied"] is False
    assert "parameter_event_apply" in blocked["contract"]["apply_blockers"]
    assert _count(db_path, "daily_weather_reviews") == 0

    applied = apply_mod.apply_daily_review_persistence_batch(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        backup_path=backup,
        allow_apply=True,
        confirmation_phrase=apply_mod.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        allow_parameter_event_apply=True,
        parameter_event_confirmation_phrase=apply_mod.PARAMETER_EVENT_CONFIRMATION_PHRASE,
    )

    assert applied["ok"] is True
    assert applied["aggregate_row_counts"]["parameter_change_events"] == 1
    assert applied["guards"]["writes_parameter_change_events"] is True
    assert _count(db_path, "parameter_change_events") == 1
