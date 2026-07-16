import importlib.util
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phase0_migration_manifest.py"
spec = importlib.util.spec_from_file_location("phase0_migration_manifest", SCRIPT_PATH)
manifest_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manifest_mod)


def _create_legacy_archive_db(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE event_fact_history (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, fact_as_of TEXT, fact_json TEXT, created_at TEXT)"
        )
        conn.execute("CREATE TABLE event_fact_latest (slug TEXT, event_date TEXT, fact_id INTEGER, updated_at TEXT)")
        conn.execute("CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, will_order INTEGER, details_json TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE auto_trade_jobs (id INTEGER PRIMARY KEY, run_date TEXT, source TEXT, state TEXT, payload_json TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE auto_orders (id INTEGER PRIMARY KEY, order_id TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, created_at TEXT)")


def _table_names(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def test_migration_manifest_is_no_apply_and_requires_preflight(tmp_path):
    db = tmp_path / "archive.db"
    _create_legacy_archive_db(db)
    tables_before = _table_names(db)

    manifest = manifest_mod.build_manifest(
        db,
        generated_at="2026-07-04T00:00:00Z",
    )

    assert manifest["schema"] == "phase0_migration_manifest_v1"
    assert manifest["read_only"] is True
    assert manifest["dry_run"] is True
    assert manifest["execution_mode"] == "no_apply_preflight_manifest"
    assert manifest["apply_supported"] is True
    assert manifest["apply_contract_schema"] == "phase0_schema_apply_contract_v1"
    assert manifest["manifest_valid_for_apply"] is False
    assert manifest["review_persistence_writer_contract"]["writer_schema"] == "daily_review_persistence_writer_v1"
    assert "daily_weather_reviews" in manifest["review_persistence_writer_contract"]["planned_tables"]
    assert "daily_calibration_records" in manifest["review_persistence_writer_contract"]["planned_tables"]
    assert manifest["guards"]["executes_sql"] is False
    assert manifest["guards"]["writes_archive_db"] is False
    assert manifest["guards"]["creates_backup"] is False
    assert manifest["backup_required"] is True
    assert manifest["backup_created"] is False
    assert Path(manifest["recommended_backup_path"]).name == "archive.db.phase0-20260704T000000Z.bak"
    assert Path(manifest["recommended_backup_path"]).exists() is False
    assert "script_apply_support" not in manifest["apply_blockers"]
    assert "fresh_archive_db_backup" in manifest["apply_blockers"]
    assert "post_apply_phase0_schema_probe_schema_only_strict" in manifest["apply_blockers"]
    assert "post_forward_write_phase0_schema_probe_full_strict" not in manifest["apply_blockers"]
    assert "daily_review_persistence_writer_contract" not in manifest["apply_blockers"]
    assert manifest["ddl_summary"]["ddl_count"] > 0
    assert manifest["ddl_summary"]["add_column_count"] > 0
    assert manifest["ddl_summary"]["create_table_count"] > 0
    assert manifest["ddl_summary"]["create_unique_index_count"] > 0
    assert "lineage_forward_unique_indexes" in manifest["ddl_summary"]["apply_order_groups"]
    assert any(item["group"] == "lineage_forward_unique_indexes" for item in manifest["apply_sequence"])
    assert manifest["rollback_plan"]["requires_pre_apply_backup"] is True
    assert "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only" in manifest["post_apply_verification_commands"]
    assert any("test_daily_review_persistence_writer.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_daily_review_persistence_apply.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_daily_review_persistence_batch_plan.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_daily_review_coverage_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_daily_review_post_apply_acceptance_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_hierarchical_probability_unit_contract_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_fact_writer_persisted_apply_contract_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_canonical_writer_persisted_coverage_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_prior_layer_contract_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_forecast_weak_likelihood_contract_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_metar_strong_likelihood_contract_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert any("test_posterior_ssot_fact_coverage_audit.py" in command for command in manifest["post_apply_verification_commands"])
    assert "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 --max-items-output 20" in manifest["post_review_plan_verification_commands"]
    assert "python3 -m pytest tests/test_daily_review_persistence_apply.py" in manifest["post_review_plan_verification_commands"]
    assert "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20" in manifest["post_review_persistence_verification_commands"]
    assert "python3 scripts/daily_review_post_apply_acceptance_audit.py --since-days 45 --max-items-output 20" in manifest["post_review_persistence_verification_commands"]
    assert "python3 scripts/phase0_schema_probe.py --immutable --strict" in manifest["post_forward_write_verification_commands"]
    assert (
        "python3 scripts/hierarchical_probability_unit_contract_audit.py --summary"
        in manifest["post_forward_write_verification_commands"]
    )
    assert (
        "python3 scripts/fact_writer_persisted_apply_contract_audit.py --summary"
        in manifest["post_forward_write_verification_commands"]
    )
    assert (
        "python3 scripts/canonical_writer_persisted_coverage_audit.py --since-hours 168 --latest-only --limit 50 --max-items-output 20 --summary"
        in manifest["post_forward_write_verification_commands"]
    )
    assert (
        "python3 scripts/prior_layer_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward"
        in manifest["post_forward_write_verification_commands"]
    )
    assert (
        "python3 scripts/forecast_weak_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward"
        in manifest["post_forward_write_verification_commands"]
    )
    assert (
        "python3 scripts/metar_strong_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward"
        in manifest["post_forward_write_verification_commands"]
    )
    assert (
        "python3 scripts/posterior_ssot_fact_coverage_audit.py --since-hours 168 --latest-only --limit 50 --max-items-output 20 --project-forward"
        in manifest["post_forward_write_verification_commands"]
    )
    assert (
        "python3 scripts/hierarchical_probability_release_gate.py --since-hours 168 --limit 50 --max-items-output 20"
        in manifest["post_forward_write_verification_commands"]
    )
    assert manifest["post_apply_verification_stages"][0]["stage"] == "schema_surface"
    assert manifest["post_apply_verification_stages"][1]["stage"] == "review_persistence_writer_contract"
    assert any(
        "test_daily_review_persistence_apply.py" in command
        for command in manifest["post_apply_verification_stages"][1]["commands"]
    )
    assert manifest["post_apply_verification_stages"][2]["stage"] == "daily_review_batch_plan_before_persistence"
    assert manifest["post_apply_verification_stages"][3]["stage"] == "daily_review_coverage_after_persistence"
    assert manifest["post_apply_verification_stages"][4]["stage"] == "future_fact_json_contract"
    assert any(
        "hierarchical_probability_unit_contract_audit.py" in command
        for command in manifest["post_apply_verification_stages"][4]["commands"]
    )
    assert any(
        "fact_writer_persisted_apply_contract_audit.py" in command
        for command in manifest["post_apply_verification_stages"][4]["commands"]
    )
    assert any(
        "canonical_writer_persisted_coverage_audit.py" in command
        for command in manifest["post_apply_verification_stages"][4]["commands"]
    )
    assert any(
        "prior_layer_contract_audit.py" in command
        for command in manifest["post_apply_verification_stages"][4]["commands"]
    )
    assert any(
        "forecast_weak_likelihood_contract_audit.py" in command
        for command in manifest["post_apply_verification_stages"][4]["commands"]
    )
    assert any(
        "posterior_ssot_fact_coverage_audit.py" in command
        for command in manifest["post_apply_verification_stages"][4]["commands"]
    )
    assert any(
        "hierarchical_probability_release_gate.py" in command
        for command in manifest["post_apply_verification_stages"][4]["commands"]
    )
    assert _table_names(db) == tables_before


def test_migration_manifest_main_prints_json(tmp_path, capsys):
    db = tmp_path / "archive.db"
    _create_legacy_archive_db(db)

    code = manifest_mod.main(["--db", str(db)])
    out = capsys.readouterr().out

    assert code == 0
    assert "phase0_migration_manifest_v1" in out
    assert '"execution_mode": "no_apply_preflight_manifest"' in out
    assert '"creates_backup": false' in out
    assert '"apply_supported": true' in out
