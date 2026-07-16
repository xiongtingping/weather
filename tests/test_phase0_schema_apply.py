import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phase0_schema_apply.py"
spec = importlib.util.spec_from_file_location("phase0_schema_apply", SCRIPT_PATH)
apply_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(apply_mod)


def _create_legacy_archive_db(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE event_fact_history (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, fact_as_of TEXT, fact_json TEXT, created_at TEXT)"
        )
        conn.execute("CREATE TABLE event_fact_latest (slug TEXT, event_date TEXT, fact_id INTEGER, updated_at TEXT)")
        conn.execute(
            "CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, will_order INTEGER, details_json TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE auto_trade_jobs (id INTEGER PRIMARY KEY, run_date TEXT, source TEXT, state TEXT, payload_json TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE auto_orders (id INTEGER PRIMARY KEY, order_id TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO event_fact_history(id, run_date, slug, event_date, fact_as_of, fact_json, created_at) "
            "VALUES (1, '2026-07-03', 's', '2026-07-03', '2026-07-03T00:00:00Z', ?, '2026-07-03T00:00:00Z')",
            (json.dumps({}),),
        )
        conn.execute(
            "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES ('s', '2026-07-03', 1, 'x')"
        )


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _indexes(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {str(row[0]) for row in rows}


def test_schema_apply_contract_defaults_to_no_apply(tmp_path):
    db = tmp_path / "archive.db"
    _create_legacy_archive_db(db)
    tables_before = _tables(db)

    contract = apply_mod.build_apply_contract(db)

    assert contract["schema"] == "phase0_schema_apply_contract_v1"
    assert contract["read_only"] is True
    assert contract["dry_run"] is True
    assert contract["apply_supported"] is True
    assert contract["applied"] is False
    assert contract["guards"]["executes_sql"] is False
    assert contract["guards"]["writes_archive_db"] is False
    assert "apply_flag" in contract["apply_blockers"]
    assert "confirmation_phrase" in contract["apply_blockers"]
    assert "fresh_archive_db_backup" in contract["apply_blockers"]
    assert "writer_quiescence" in contract["apply_blockers"]
    assert "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only" in contract["post_apply_verification_commands"]
    assert contract["ddl_summary"]["create_unique_index_count"] > 0
    assert any("test_daily_review_persistence_writer.py" in command for command in contract["post_apply_verification_commands"])
    assert any("test_daily_review_persistence_apply.py" in command for command in contract["post_apply_verification_commands"])
    assert any("test_daily_review_persistence_batch_plan.py" in command for command in contract["post_apply_verification_commands"])
    assert any("test_daily_review_coverage_audit.py" in command for command in contract["post_apply_verification_commands"])
    assert any("test_daily_review_post_apply_acceptance_audit.py" in command for command in contract["post_apply_verification_commands"])
    assert _tables(db) == tables_before


def test_schema_apply_contract_applies_only_with_all_preconditions_on_temp_db(tmp_path):
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.phase0-test.bak"
    _create_legacy_archive_db(db)
    shutil.copy2(db, backup)

    result = apply_mod.apply_schema_contract(
        db,
        allow_apply=True,
        confirmation_phrase=apply_mod.CONFIRMATION_PHRASE,
        backup_path=backup,
        writer_quiescent=True,
    )

    assert result["applied"] is True
    assert result["execution_mode"] == "controlled_apply_completed"
    assert result["guards"]["executes_sql"] is True
    assert result["guards"]["writes_archive_db"] is True
    assert result["guards"]["historical_backfill"] is False
    assert result["guards"]["restarts_launchd"] is False
    assert result["guards"]["places_orders"] is False
    assert result["apply_result"]["schema_surface_ready"] is True
    assert result["apply_result"]["phase0_ready"] is False
    assert result["apply_result"]["fact_json_missing_count"] > 0
    assert "correlation_id" in _columns(db, "event_fact_history")
    assert "lineage_state_json" in _columns(db, "event_fact_latest")
    assert "source_observations" in _tables(db)
    assert "signal_to_order_timing" in _tables(db)
    assert "idx_phase0_event_fact_history_fact_snapshot_id" in _indexes(db)


def test_schema_apply_cli_blocks_without_confirmation(tmp_path, capsys):
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.phase0-test.bak"
    _create_legacy_archive_db(db)
    shutil.copy2(db, backup)

    code = apply_mod.main(["--db", str(db), "--apply", "--backup", str(backup), "--writer-quiescent"])
    out = capsys.readouterr().out

    assert code == 4
    assert '"execution_mode": "controlled_apply_blocked"' in out
    assert '"confirmation_phrase"' in out
    assert "correlation_id" not in _columns(db, "event_fact_history")
