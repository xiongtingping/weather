import importlib.util
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phase0_forward_schema_plan.py"
spec = importlib.util.spec_from_file_location("phase0_forward_schema_plan", SCRIPT_PATH)
plan_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plan_mod)


def test_forward_schema_plan_is_dry_run_and_contains_missing_columns(tmp_path):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE event_fact_history (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, fact_as_of TEXT, fact_json TEXT, created_at TEXT)"
        )
        conn.execute("CREATE TABLE event_fact_latest (slug TEXT, event_date TEXT, fact_id INTEGER, updated_at TEXT)")
        conn.execute("CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, will_order INTEGER, details_json TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE auto_trade_jobs (id INTEGER PRIMARY KEY, run_date TEXT, source TEXT, state TEXT, payload_json TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE auto_orders (id INTEGER PRIMARY KEY, order_id TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, created_at TEXT)")

    plan = plan_mod.build_plan(db)
    sql = "\n".join(item["sql"] for item in plan["ddl"])

    assert plan["read_only"] is True
    assert plan["dry_run"] is True
    assert plan["execution_mode"] == "no_apply_dry_run"
    assert plan["apply_supported"] is False
    assert plan["guards"]["executes_sql"] is False
    assert "fresh_archive_db_backup_required" in plan["pre_apply_requirements"]
    assert "ALTER TABLE \"event_fact_history\" ADD COLUMN \"correlation_id\" TEXT" in sql
    assert "CREATE TABLE IF NOT EXISTS \"source_observations\"" in sql
    assert "CREATE TABLE IF NOT EXISTS \"task_queue\"" in sql
    assert "DEFAULT 'pending'" in sql
    assert "DEFAULT (datetime('now'))" in sql
    assert "CREATE INDEX IF NOT EXISTS \"idx_task_queue_status_priority\"" in sql
    assert "CREATE TABLE IF NOT EXISTS \"signal_to_order_timing\"" in sql
    assert "CREATE TABLE IF NOT EXISTS \"daily_calibration_records\"" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS \"idx_phase0_event_fact_history_fact_snapshot_id\"" in sql
    assert "WHERE \"fact_snapshot_id\" IS NOT NULL AND \"fact_snapshot_id\" <> ''" in sql
    add_column = next(item for item in plan["ddl"] if item.get("action") == "add_column")
    create_table = next(item for item in plan["ddl"] if item.get("action") == "create_table")
    create_unique_index = next(item for item in plan["ddl"] if item.get("action") == "create_unique_index")
    assert add_column["requires_backup"] is True
    assert add_column["rollback_hint"].startswith("restore_pre_migration_backup")
    assert create_table["requires_backup"] is False
    assert create_table["rollback_hint"].startswith("DROP TABLE IF EXISTS")
    assert create_unique_index["requires_backup"] is True
    assert create_unique_index["apply_order_group"] == "lineage_forward_unique_indexes"
    assert create_unique_index["partial_unique_where"]


def test_forward_schema_plan_main_prints_json(tmp_path, capsys):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY)")

    code = plan_mod.main(["--db", str(db)])
    out = capsys.readouterr().out

    assert code == 0
    assert "phase0_forward_schema_plan_v1" in out
    assert '"executes_sql": false' in out
    assert '"apply_supported": false' in out


def test_forward_schema_plan_create_tables_have_single_primary_key(tmp_path):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY)")

    plan = plan_mod.build_plan(db)
    create_table_sql = {
        item["table"]: item["sql"]
        for item in plan["ddl"]
        if item.get("action") == "create_table"
    }

    assert create_table_sql["awc_hard_source_signals"].count("PRIMARY KEY") == 1
    assert '"signal_id" TEXT PRIMARY KEY' in create_table_sql["awc_hard_source_signals"]
    assert '"source_event_id" TEXT PRIMARY KEY' not in create_table_sql["awc_hard_source_signals"]
    assert create_table_sql["bias_correction_candidates"].count("PRIMARY KEY") == 1
    assert '"calibration_candidate_id" TEXT PRIMARY KEY' in create_table_sql["bias_correction_candidates"]
    assert '"review_id" TEXT PRIMARY KEY' not in create_table_sql["bias_correction_candidates"]
    assert create_table_sql["daily_calibration_records"].count("PRIMARY KEY") == 1
    assert '"daily_calibration_record_id" TEXT PRIMARY KEY' in create_table_sql["daily_calibration_records"]
    assert '"review_id" TEXT PRIMARY KEY' not in create_table_sql["daily_calibration_records"]
    assert create_table_sql["parameter_change_events"].count("PRIMARY KEY") == 1
    assert '"id" INTEGER PRIMARY KEY' in create_table_sql["parameter_change_events"]
    assert '"calibration_candidate_id" TEXT PRIMARY KEY' not in create_table_sql["parameter_change_events"]
    assert create_table_sql["task_queue"].count("PRIMARY KEY") == 1
    assert '"id" INTEGER PRIMARY KEY' in create_table_sql["task_queue"]
