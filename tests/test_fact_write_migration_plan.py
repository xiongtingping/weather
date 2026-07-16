import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fact_write_migration_plan.py"
spec = importlib.util.spec_from_file_location("fact_write_migration_plan", SCRIPT_PATH)
plan_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plan_mod)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_fact_write_migration_plan_groups_known_fact_write_lanes(tmp_path):
    _write(
        tmp_path / "weather_fact_snapshot_writer.py",
        """
def build_fact_snapshot_write_plan(): pass
def build_hierarchical_posterior_fact_snapshot_write_plan(): pass
def apply_fact_snapshot_write_plan(conn, plan): pass
def build_post_apply_persisted_posterior_ssot_verification(conn): pass
post_apply_persisted_posterior_ssot_verification_failed = True
def compact_fact_snapshot_writer_apply_result_for_lineage(result):
    return {"post_apply_persisted_verification_ok": True}
        """,
    )
    _write(
        tmp_path / "ui" / "server.py",
        """
def ensure_fact_tables(conn): pass
def _insert_event_fact_history(conn): pass
def build_ui_event_fact_write_compat_plan(conn): pass
def build_ui_event_fact_write_compat_alignment_audit(conn): pass
def apply_ui_event_fact_write_compat_plan(conn, plan): pass
def build_ui_event_fact_write_apply_parity_audit(conn): pass
def _finalize_event_fact_history_materialized_payload_for_storage(item): pass
def _insert_materialized_event_fact_history_row(conn): pass
def build_materialized_event_fact_history_writer_apply_plan(conn): pass
def _insert_event_fact_history(conn, use_writer_apply=False):
    return build_fact_snapshot_write_plan(write_latest=False)
def _event_fact_history_writer_apply_enabled(value=None):
    return True
def build_event_fact_history_insert_function_parity_audit():
    return {"uses_in_memory_db": True, "function_paths": {"legacy_path_removed": True}}
def build_event_fact_history_insert_function_parity_sample_suite():
    return {"sample_case_ids": ["baseline_distribution", "awc_hard_source_observation"]}
latest_repoint_owned_by_weather_fact_snapshot_writer = True
compact_weather_replaced_event_fact_history_row()
        """,
    )
    _write(
        tmp_path / "scripts" / "hard_source_direct_poller.py",
        """
def build_hard_source_fact_snapshot_writer_apply_plan(conn): pass
def _hard_source_fact_writer_apply_enabled(value=None):
    return _truthy(value, True)
kill_switch_env = "PM_HARD_SOURCE_FACT_WRITER_APPLY_ENABLED=0"
def _insert_fact_snapshot(conn):
    result = apply_fact_snapshot_write_plan(write_latest=True)
    writer_apply_result_summary = compact_fact_snapshot_writer_apply_result_for_lineage(result)
    return {"writer_apply_result_summary": writer_apply_result_summary}
        """,
    )
    _write(
        tmp_path / "scan_weather_market.py",
        """
import weather_fact_snapshot_writer
Q_CALIBRATED_WRITEBACK_COMPOSER_VERSION = "q_calibrated_writeback_writer_v1"
def build_q_calibrated_writeback_fact_snapshot_writer_apply_plan(conn): pass
def writeback_q_calibrated_for_run_date(run_date, allow_historical=False):
    if not allow_historical and run_date != today_date_str("America/New_York"):
        return {"error": "historical_writeback_requires_explicit_allow"}
    result = weather_fact_snapshot_writer.apply_fact_snapshot_write_plan(write_latest=True)
    writer_apply_result_summary = weather_fact_snapshot_writer.compact_fact_snapshot_writer_apply_result_for_lineage(result)
    return {"writer_apply_result_summary": writer_apply_result_summary}
def main():
    flag = "--q-calibrated-writeback-allow-historical"
        """,
    )
    _write(
        tmp_path / "scripts" / "materialize_ctp_latest_facts.py",
        """
conn.execute("INSERT OR REPLACE INTO event_fact_summary_cache(fact_id) VALUES (?)")
conn.execute("UPDATE event_fact_latest SET fact_id = ? WHERE slug = ?")
        """,
    )
    _write(
        tmp_path / "cleanup_data.sh",
        """
sqlite3 data/archive.db "DELETE FROM event_fact_history WHERE fact_as_of < datetime('now','-30 days')"
        """,
    )

    report = plan_mod.build_fact_write_migration_plan(tmp_path, db_path=None, sample_limit=10)
    lanes = {lane["lane"]: lane for lane in report["lanes"]}
    entries = {entry["file"]: entry for entry in report["file_entries"]}

    assert report["schema"] == "fact_write_migration_plan_v1"
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["executes_sql"] is False
    assert lanes["target_owner_boundary"]["files"] == ["weather_fact_snapshot_writer.py"]
    assert lanes["target_owner_boundary"]["apply_contract_count"] == 1
    assert lanes["target_owner_boundary"]["post_apply_verification_count"] == 1
    assert lanes["target_owner_boundary"]["apply_result_summary_helper_count"] == 1
    assert lanes["target_owner_boundary"]["status"] == "target_owner_apply_contract_present"
    assert entries["weather_fact_snapshot_writer.py"]["post_apply_verification_present"] is True
    assert entries["weather_fact_snapshot_writer.py"]["apply_result_summary_helper_present"] is True
    assert lanes["ui_current_helper"]["files"] == ["ui/server.py"]
    assert lanes["ui_current_helper"]["compat_adapter_count"] == 1
    assert lanes["ui_current_helper"]["alignment_audit_count"] == 1
    assert lanes["ui_current_helper"]["apply_wrapper_count"] == 1
    assert lanes["ui_current_helper"]["apply_parity_audit_count"] == 1
    assert lanes["ui_current_helper"]["materialized_payload_split_count"] == 1
    assert lanes["ui_current_helper"]["helper_writer_apply_path_count"] == 1
    assert lanes["ui_current_helper"]["function_parity_gate_count"] == 1
    assert lanes["ui_current_helper"]["function_parity_sample_suite_count"] == 1
    assert lanes["ui_current_helper"]["helper_writer_apply_default_enabled_count"] == 0
    assert lanes["ui_current_helper"]["helper_single_writer_path_count"] == 1
    assert lanes["ui_current_helper"]["status"] == "ui_current_helper_single_writer_path_present"
    assert lanes["hard_source_realtime"]["files"] == ["scripts/hard_source_direct_poller.py"]
    assert lanes["hard_source_realtime"]["hard_source_writer_apply_path_count"] == 1
    assert lanes["hard_source_realtime"]["hard_source_writer_apply_default_enabled_count"] == 1
    assert lanes["hard_source_realtime"]["writer_apply_result_summary_exposed_count"] == 1
    assert lanes["hard_source_realtime"]["status"] == "hard_source_realtime_writer_apply_default_present"
    assert lanes["materializer_rebuild_backfill"]["files"] == ["scripts/materialize_ctp_latest_facts.py"]
    assert lanes["maintenance_cleanup"]["files"] == ["cleanup_data.sh"]
    assert entries["ui/server.py"]["risk_flags"]["can_repoint_latest"] is True
    assert entries["ui/server.py"]["compat_adapter_present"] is True
    assert entries["ui/server.py"]["alignment_audit_present"] is True
    assert entries["ui/server.py"]["apply_wrapper_present"] is True
    assert entries["ui/server.py"]["apply_parity_audit_present"] is True
    assert entries["ui/server.py"]["materialized_payload_split_present"] is True
    assert entries["ui/server.py"]["helper_writer_apply_path_present"] is True
    assert entries["ui/server.py"]["function_parity_gate_present"] is True
    assert entries["ui/server.py"]["function_parity_sample_suite_present"] is True
    assert entries["ui/server.py"]["helper_writer_apply_default_enabled"] is False
    assert entries["ui/server.py"]["helper_single_writer_path_present"] is True
    assert entries["scripts/hard_source_direct_poller.py"]["hard_source_writer_apply_path_present"] is True
    assert entries["scripts/hard_source_direct_poller.py"]["hard_source_writer_apply_default_enabled"] is True
    assert lanes["legacy_scanner_strategy"]["files"] == ["scan_weather_market.py"]
    assert lanes["legacy_scanner_strategy"]["scanner_writer_apply_path_count"] == 1
    assert lanes["legacy_scanner_strategy"]["scanner_historical_guard_count"] == 1
    assert lanes["legacy_scanner_strategy"]["writer_apply_result_summary_exposed_count"] == 1
    assert lanes["legacy_scanner_strategy"]["status"] == "legacy_scanner_q_calibrated_writer_apply_path_present"
    assert entries["scan_weather_market.py"]["scanner_writer_apply_path_present"] is True
    assert entries["scan_weather_market.py"]["scanner_historical_guard_present"] is True
    assert entries["scripts/materialize_ctp_latest_facts.py"]["risk_flags"]["can_update_summary_cache"] is True
    assert entries["cleanup_data.sh"]["risk_flags"]["historical_backfill_risk"] is True
    assert report["pending_migration_file_count"] == 2
    assert "migrate_ui_server_fact_helpers_to_weather_fact_snapshot_writer" not in report["next_actions"]
    assert "migrate_hard_source_realtime_fact_writes_to_writer" not in report["next_actions"]
    assert "retire_or_route_legacy_scanner_fact_updates" not in report["next_actions"]
    assert "convert_remaining_materializer_rebuild_scripts_to_batch_writer_plan" in report["next_actions"]


def test_fact_write_migration_plan_marks_materializer_ctp_writer_path_complete(tmp_path):
    materializer_path = tmp_path / "scripts" / "materialize_ctp_latest_facts.py"
    _write(
        materializer_path,
        """
def build_ctp_materialized_fact_snapshot_writer_apply_plan():
    return build_fact_snapshot_write_plan(write_latest=True)
def run():
    flag = "--allow-historical"
    reason = "historical_apply_requires_explicit_allow"
    tz = ZoneInfo("America/New_York")
    build_fact_summary_cache_repair_plan()
    apply_fact_summary_cache_repair_plan()
    return apply_fact_snapshot_write_plan()
        """,
    )
    inventory = {
        "pattern_inventory": [
            {
                "id": "write_event_fact_summary_cache",
                "files": {"scripts/materialize_ctp_latest_facts.py": 1},
                "samples": [
                    {
                        "file": "scripts/materialize_ctp_latest_facts.py",
                        "line": 7,
                        "text": "apply_fact_summary_cache_repair_plan()",
                    }
                ],
            }
        ],
        "table_write_inventory": {
            "event_fact_summary_cache": {
                "writer_files": ["scripts/materialize_ctp_latest_facts.py"],
                "pattern_id": "write_event_fact_summary_cache",
            }
        },
    }

    entry = plan_mod._build_file_entry(
        inventory,
        "scripts/materialize_ctp_latest_facts.py",
        root=tmp_path,
        sample_limit=5,
    )
    lane = {
        row["lane"]: row
        for row in plan_mod._group_lanes([entry])
    }["materializer_rebuild_backfill"]

    assert entry["materializer_ctp_writer_apply_path_present"] is True
    assert entry["materializer_historical_guard_present"] is True
    assert entry["status"] == "materializer_ctp_writer_apply_path_with_historical_guard_present"
    assert lane["materializer_writer_apply_path_count"] == 1
    assert lane["materializer_historical_guard_count"] == 1
    assert lane["materializer_guard_count"] == 1
    assert lane["status"] == "materializer_rebuild_writer_apply_or_candidate_copy_paths_present"


def test_fact_write_migration_plan_classifies_production_identity_repair_surfaces(tmp_path):
    apply_path = tmp_path / "scripts" / "production_identity_duplicate_fact_snapshot_apply.py"
    contract_path = tmp_path / "scripts" / "production_identity_duplicate_fact_snapshot_apply_contract.py"
    _write(
        apply_path,
        """
from scripts import controlled_apply_maintenance_window_packet
CONFIRMATION_PHRASE = "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR"
def _verify_existing_backup(path): pass
def run(writer_quiescent=False):
    guards = {
        "places_orders": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "restarts_launchd": False,
        "creates_backup": False,
    }
    conn.execute("UPDATE event_fact_history SET fact_snapshot_id = ?")
    conn.execute("UPDATE event_fact_latest SET fact_snapshot_id = ?")
    return guards
        """,
    )
    _write(
        contract_path,
        """
CONFIRMATION_PHRASE = "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR"
def build_contract():
    return {
        "read_only": True,
        "dry_run": True,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "sql_preview": "UPDATE event_fact_history SET ...",
    }
        """,
    )
    inventory = {
        "pattern_inventory": [
            {
                "id": "write_event_fact_history",
                "files": {
                    "scripts/production_identity_duplicate_fact_snapshot_apply.py": 1,
                    "scripts/production_identity_duplicate_fact_snapshot_apply_contract.py": 1,
                },
                "samples": [],
            }
        ],
        "table_write_inventory": {
            "event_fact_history": {
                "writer_files": [
                    "scripts/production_identity_duplicate_fact_snapshot_apply.py",
                    "scripts/production_identity_duplicate_fact_snapshot_apply_contract.py",
                ],
                "pattern_id": "write_event_fact_history",
            }
        },
    }

    report = plan_mod.build_fact_write_migration_plan(
        tmp_path,
        db_path=None,
        prebuilt_inventory=inventory,
    )
    entries = {entry["file"]: entry for entry in report["file_entries"]}
    lanes = {lane["lane"]: lane for lane in report["lanes"]}

    assert entries["scripts/production_identity_duplicate_fact_snapshot_apply.py"]["lane"] == "controlled_identity_repair"
    assert entries["scripts/production_identity_duplicate_fact_snapshot_apply.py"]["status"] == "controlled_identity_repair_apply_boundary_present"
    assert entries["scripts/production_identity_duplicate_fact_snapshot_apply_contract.py"]["lane"] == "read_only_contract_surface"
    assert entries["scripts/production_identity_duplicate_fact_snapshot_apply_contract.py"]["status"] == "read_only_contract_surface_no_apply_boundary_present"
    assert lanes["controlled_identity_repair"]["status"] == "controlled_identity_repair_apply_boundary_present"
    assert report["pending_migration_file_count"] == 0
    assert "classify_unknown_fact_write_surfaces_before_migration" not in report["next_actions"]


def test_fact_write_migration_plan_marks_copy_acceptance_temp_writer_path_complete(tmp_path):
    _write(
        tmp_path / "scripts" / "hard_source_direct_copy_acceptance.py",
        """
COPY_ACCEPTANCE_COMPOSER_VERSION = "hard_source_direct_copy_acceptance_fixture_v1"
def build_copy_acceptance_fact_snapshot_writer_apply_plan():
    temporary_db_copy = True
    writes_temporary_archive_db_copy = True
    historical_backfill = False
    return build_fact_snapshot_write_plan(write_latest=True)
def _apply_copy_acceptance_fact_payload():
    return apply_fact_snapshot_write_plan()
        """,
    )
    inventory = {
        "pattern_inventory": [
            {
                "id": "write_event_fact_latest",
                "files": {"scripts/hard_source_direct_copy_acceptance.py": 1},
                "samples": [
                    {
                        "file": "scripts/hard_source_direct_copy_acceptance.py",
                        "line": 5,
                        "text": "writes_temporary_archive_db_copy = True",
                    }
                ],
            }
        ],
        "table_write_inventory": {
            "event_fact_latest": {
                "writer_files": ["scripts/hard_source_direct_copy_acceptance.py"],
                "pattern_id": "write_event_fact_latest",
            }
        },
    }

    entry = plan_mod._build_file_entry(
        inventory,
        "scripts/hard_source_direct_copy_acceptance.py",
        root=tmp_path,
        sample_limit=5,
    )
    lane = {
        row["lane"]: row
        for row in plan_mod._group_lanes([entry])
    }["materializer_rebuild_backfill"]

    assert entry["materializer_copy_acceptance_writer_apply_path_present"] is True
    assert entry["materializer_temp_db_guard_present"] is True
    assert entry["status"] == "materializer_copy_acceptance_writer_apply_path_with_temp_db_guard_present"
    assert lane["materializer_writer_apply_path_count"] == 1
    assert lane["materializer_temp_db_guard_count"] == 1
    assert lane["materializer_guard_count"] == 1
    assert lane["status"] == "materializer_rebuild_writer_apply_or_candidate_copy_paths_present"


def test_fact_write_migration_plan_classifies_lineage_repair_and_read_only_contract_surfaces(tmp_path):
    _write(
        tmp_path / "daily_review_lineage_repair_writer.py",
        """
import weather_fact_snapshot_writer
CONFIRMATION_PHRASE = "APPLY_DAILY_REVIEW_LINEAGE_REPAIR"
def _preflight_checks():
    return [
        {"reason": "backup_path_required_before_lineage_repair_apply"},
        {"reason": "requires_explicit_writer_quiescent_assertion"},
    ]
def apply_lineage_repair_contract(conn, contract):
    return weather_fact_snapshot_writer.apply_event_fact_history_merge_fill_repair_contract(conn, contract)
weather_fact_snapshot_writer.WRITE_OWNER
        """,
    )
    _write(
        tmp_path / "scripts" / "daily_review_persistence_batch_plan.py",
        """
LINEAGE_REPAIR_APPLY_CONTRACT_SCHEMA = "daily_review_lineage_repair_apply_contract_v1"
def build_lineage_repair_apply_contract():
    return {
        "execution_mode": "lineage_repair_apply_contract_no_apply",
        "guards": {"writes_event_fact_history": False},
    }
weather_fact_snapshot_writer = "contract reference only"
        """,
    )
    _write(
        tmp_path / "scripts" / "phase0_schema_probe.py",
        """
REQUIRED_TABLE_COLUMNS = {"event_fact_history": ("id",)}
TABLE_ID_CONSTRAINTS = {"event_fact_history": {"write_owner": "weather_fact_snapshot_writer"}}
        """,
    )
    _write(
        tmp_path / "scripts" / "fact_writer_persisted_apply_contract_audit.py",
        """
import weather_fact_snapshot_writer
def _guards():
    return {"writes_archive_db": False, "writes_facts_sqlite": False}
def build_fact_writer_persisted_apply_contract_audit():
    return {
        "read_only": True,
        "execution_mode": "temporary_sqlite_fixture_no_archive_db_write",
        "coverage_mode": "writer_apply_result_summary_temp_persisted_fixture",
        "guards": _guards(),
    }
weather_fact_snapshot_writer.apply_fact_snapshot_write_plan
writer_apply_result_summary_contract_v1 = "contract"
        """,
    )
    _write(
        tmp_path / "scripts" / "canonical_writer_persisted_coverage_audit.py",
        """
def _guards():
    return {"writes_archive_db": False, "writes_facts_sqlite": False}
def build_canonical_writer_persisted_coverage_audit():
    return {
        "read_only": True,
        "coverage_mode": "canonical_writer_persisted_fact_json_strict_marker",
        "guards": _guards(),
    }
audit_fact_payload = "read_only"
weather_fact_snapshot_writer = "read_only contract reference only"
        """,
    )
    _write(
        tmp_path / "scripts" / "source_acquisition_contract_audit.py",
        """
def _guards():
    return {"writes_archive_db": False, "writes_facts_sqlite": False}
weather_fact_snapshot_writer = "read_only contract reference only"
source_acquisition_contract_audit_read_only = True
        """,
    )
    _write(
        tmp_path / "scripts" / "hierarchical_core_integration_contract_audit.py",
        """
def _guards():
    return {"writes_archive_db": False, "writes_facts_sqlite": False, "writes_auto_trade_decisions": False}
def build_audit():
    return {"read_only": True, "execution_mode": "hierarchical_core_integration_contract_audit_read_only", "guards": _guards()}
weather_fact_snapshot_writer = "read_only no_apply contract reference only"
build_fact_snapshot_write_plan = "no_apply fixture only"
        """,
    )

    report = plan_mod.build_fact_write_migration_plan(tmp_path, db_path=None, sample_limit=10)
    lanes = {lane["lane"]: lane for lane in report["lanes"]}
    entries = {entry["file"]: entry for entry in report["file_entries"]}

    assert lanes["controlled_lineage_repair"]["files"] == ["daily_review_lineage_repair_writer.py"]
    assert (
        lanes["controlled_lineage_repair"]["status"]
        == "controlled_lineage_repair_writer_apply_boundary_present"
    )
    assert entries["daily_review_lineage_repair_writer.py"]["risk_flags"]["direct_sql_write_surface"] is False
    assert entries["daily_review_lineage_repair_writer.py"]["controlled_lineage_repair_preflight_present"] is True
    assert sorted(lanes["read_only_contract_surface"]["files"]) == [
        "scripts/canonical_writer_persisted_coverage_audit.py",
        "scripts/daily_review_persistence_batch_plan.py",
        "scripts/fact_writer_persisted_apply_contract_audit.py",
        "scripts/hierarchical_core_integration_contract_audit.py",
        "scripts/phase0_schema_probe.py",
        "scripts/source_acquisition_contract_audit.py",
    ]
    assert lanes["read_only_contract_surface"]["status"] == "read_only_contract_surface_no_apply_boundary_present"
    assert entries["scripts/daily_review_persistence_batch_plan.py"]["risk_flags"]["uses_fact_helper_surface"] is False
    assert entries["scripts/canonical_writer_persisted_coverage_audit.py"]["risk_flags"]["uses_fact_helper_surface"] is False
    assert entries["scripts/fact_writer_persisted_apply_contract_audit.py"]["risk_flags"]["uses_fact_helper_surface"] is False
    assert entries["scripts/hierarchical_core_integration_contract_audit.py"]["risk_flags"]["uses_fact_helper_surface"] is False
    assert entries["scripts/phase0_schema_probe.py"]["read_only_contract_surface_present"] is True
    assert entries["scripts/source_acquisition_contract_audit.py"]["risk_flags"]["uses_fact_helper_surface"] is False
    assert lanes["unknown_fact_writer_surface"]["file_count"] == 0
    assert report["pending_migration_file_count"] == 0
    assert report["next_actions"] == []


def test_fact_write_migration_plan_classifies_posterior_and_amsc_boundaries(tmp_path):
    _write(
        tmp_path / "posterior_ssot_projection_runtime.py",
        """
def build_fact_projection_no_apply():
    conn = sqlite3.connect("file:data/archive.db?mode=ro", uri=True)
    return {"read_only": True, "execution_mode": "posterior_ssot_projection_no_apply"}
build_fact_snapshot_write_plan
        """,
    )
    _write(
        tmp_path / "scripts" / "fetch_zuuu_metar_taf.py",
        """
def build_amsc_fact_snapshot_write_plan():
    return {
        "caller": {"lane": "amsc_zuuu_metar_taf_read_only"},
        "next_boundary": "weather_fact_snapshot_writer_dry_run_or_no_apply",
        "guards": {"writes_archive_db": False, "writes_facts_sqlite": False},
        "read_only": True,
    }
build_fact_snapshot_write_plan
        """,
    )
    _write(
        tmp_path / "scripts" / "posterior_ssot_apply_confirmation_packet.py",
        """
CONFIRMATION_PHRASE = "APPLY_POSTERIOR_SSOT_FACT_REPAIR"
def _guards():
    return {"writes_archive_db": False, "writes_facts_sqlite": False}
def build_confirmation_packet():
    return {
        "read_only": True,
        "execution_mode": "posterior_ssot_repair_confirmation_no_apply",
        "operator_preflight": [],
    }
build_fact_projection_no_apply
        """,
    )
    _write(
        tmp_path / "scripts" / "apply_amsc_zuuu_fact_snapshot_plan.py",
        """
CONFIRM_PHRASE = "APPLY_AMSC_ZUUU_FACT_SNAPSHOT"
def apply_amsc_zuuu_fact_snapshot_plan(plan, allow_apply=False, confirm=""):
    if not allow_apply or confirm != CONFIRM_PHRASE:
        return {"guards": {"writes_event_fact_latest": False}}
    rebuilt = build_fact_snapshot_write_plan(write_latest=False)
    rebuilt["source_acquisition_latest_repoint_policy"] = {
        "boundary": "source_acquisition_may_append_history_but_must_not_repoint_posterior_ssot_latest"
    }
    return apply_fact_snapshot_write_plan()
        """,
    )
    _write(
        tmp_path / "scripts" / "apply_posterior_ssot_fact_repair.py",
        """
CONFIRMATION_PHRASE = "APPLY_POSTERIOR_SSOT_FACT_REPAIR"
def _backup_sqlite(): pass
def apply_posterior_ssot_fact_repair(allow_apply=False, confirm="", writer_quiescent=False):
    if not allow_apply:
        return {"block_reason": "allow_apply_false"}
    if confirm != CONFIRMATION_PHRASE:
        return {"block_reason": "confirmation_phrase_missing_or_invalid"}
    if not writer_quiescent:
        return {"block_reason": "writer_quiescent_false"}
    projection = build_fact_projection_no_apply(enable_apply_contract=True, write_latest=True)
    if not _backup_sqlite():
        return {"block_reason": "backup_failed"}
    return apply_fact_snapshot_write_plan()
        """,
    )
    _write(
        tmp_path / "scripts" / "materialize_amsc_posterior_task.py",
        """
def _guards(writes=False):
    return {
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "historical_backfill": False,
        "writes_auto_trade_decisions": False,
    }
def materialize_amsc_posterior_task(payload):
    projection = build_fact_projection_no_apply(enable_apply_contract=True, write_latest=True)
    if not projection["context"].get("source_history_observations_merged"):
        return {"skipped": True}
    return apply_fact_snapshot_write_plan()
        """,
    )
    inventory = {
        "schema": "fixture_inventory",
        "pattern_inventory": [
            {
                "id": "fact_writer_helper",
                "files": {
                    "posterior_ssot_projection_runtime.py": 1,
                    "scripts/apply_amsc_zuuu_fact_snapshot_plan.py": 1,
                    "scripts/apply_posterior_ssot_fact_repair.py": 1,
                    "scripts/materialize_amsc_posterior_task.py": 1,
                    "scripts/fetch_zuuu_metar_taf.py": 1,
                    "scripts/posterior_ssot_apply_confirmation_packet.py": 1,
                },
                "samples": [],
            }
        ],
        "table_write_inventory": {},
    }

    report = plan_mod.build_fact_write_migration_plan(
        tmp_path,
        db_path=None,
        sample_limit=10,
        prebuilt_inventory=inventory,
    )
    lanes = {lane["lane"]: lane for lane in report["lanes"]}
    entries = {entry["file"]: entry for entry in report["file_entries"]}

    assert sorted(lanes["read_only_contract_surface"]["files"]) == [
        "posterior_ssot_projection_runtime.py",
        "scripts/fetch_zuuu_metar_taf.py",
        "scripts/posterior_ssot_apply_confirmation_packet.py",
    ]
    assert lanes["read_only_contract_surface"]["status"] == "read_only_contract_surface_no_apply_boundary_present"
    assert lanes["source_acquisition_archive_apply"]["files"] == [
        "scripts/apply_amsc_zuuu_fact_snapshot_plan.py"
    ]
    assert (
        lanes["source_acquisition_archive_apply"]["status"]
        == "source_acquisition_archive_apply_writer_boundary_present"
    )
    assert lanes["controlled_posterior_repair"]["files"] == [
        "scripts/apply_posterior_ssot_fact_repair.py"
    ]
    assert (
        lanes["controlled_posterior_repair"]["status"]
        == "controlled_posterior_repair_writer_apply_boundary_present"
    )
    assert lanes["posterior_materialization_worker"]["files"] == [
        "scripts/materialize_amsc_posterior_task.py"
    ]
    assert (
        lanes["posterior_materialization_worker"]["status"]
        == "posterior_materialization_worker_boundary_present"
    )
    assert entries["scripts/apply_amsc_zuuu_fact_snapshot_plan.py"][
        "source_acquisition_archive_apply_writer_boundary_present"
    ] is True
    assert entries["scripts/apply_posterior_ssot_fact_repair.py"][
        "controlled_posterior_repair_preflight_present"
    ] is True
    assert entries["scripts/materialize_amsc_posterior_task.py"][
        "posterior_materialization_worker_boundary_present"
    ] is True
    assert lanes["unknown_fact_writer_surface"]["file_count"] == 0
    assert report["pending_migration_file_count"] == 0
    assert report["next_actions"] == []


def test_fact_write_migration_plan_marks_archive_rebuild_candidate_copy_boundary_complete(tmp_path):
    _write(
        tmp_path / "scripts" / "archive_db_rebuild_build.py",
        """
def build_archive_rebuild_candidate_copy_boundary():
    return {
        "execution_mode": "candidate_db_table_copy_not_fact_snapshot_mutation",
        "guards": {
            "writes_candidate_archive_db": True,
            "replaces_source_db": False,
        },
        "fact_writer_integration": {"status": "not_applicable_for_candidate_copy"},
    }
        """,
    )
    inventory = {
        "pattern_inventory": [
            {
                "id": "write_event_fact_history",
                "files": {"scripts/archive_db_rebuild_build.py": 1},
                "samples": [
                    {
                        "file": "scripts/archive_db_rebuild_build.py",
                        "line": 4,
                        "text": "candidate_db_table_copy_not_fact_snapshot_mutation",
                    }
                ],
            }
        ],
        "table_write_inventory": {
            "event_fact_history": {
                "writer_files": [],
                "non_live_writer_files": ["scripts/archive_db_rebuild_build.py"],
                "pattern_id": "write_event_fact_history",
            }
        },
    }

    assert plan_mod._files_from_inventory(inventory) == ["scripts/archive_db_rebuild_build.py"]

    entry = plan_mod._build_file_entry(
        inventory,
        "scripts/archive_db_rebuild_build.py",
        root=tmp_path,
        sample_limit=5,
    )
    lane = {
        row["lane"]: row
        for row in plan_mod._group_lanes([entry])
    }["materializer_rebuild_backfill"]

    assert entry["materializer_archive_rebuild_candidate_copy_guard_present"] is True
    assert entry["table_matches"] == {}
    assert entry["non_live_table_matches"] == {"event_fact_history": 1}
    assert entry["status"] == "materializer_archive_rebuild_candidate_copy_guard_present"
    assert lane["materializer_archive_rebuild_candidate_copy_guard_count"] == 1
    assert lane["materializer_coverage_count"] == 1
    assert lane["materializer_guard_count"] == 1
    assert lane["status"] == "materializer_rebuild_writer_apply_or_candidate_copy_paths_present"


def test_fact_write_migration_plan_classifies_manifest_gated_v2_public_shadow_writer_as_non_live(tmp_path):
    _write(
        tmp_path / "sequential_bayes_public_ssot_shadow_writer.py",
        """
SHADOW_MANIFEST_TABLE = "v2_shadow_runtime_manifest"
def _assert_isolated_shadow_db(conn): pass
def build_isolated_public_ssot_shadow_boundary():
    return {
        "execution_mode": "isolated_shadow_only_temp_db_never_production_archive",
        "writes_production_archive": False,
        "writes_production_auto_trade_decisions": False,
        "places_orders": False,
    }
def run(conn):
    _assert_isolated_shadow_db(conn)
    conn.execute("INSERT INTO event_fact_history(slug) VALUES (?)")
    conn.execute("INSERT INTO event_fact_latest(slug) VALUES (?)")
        """,
    )

    report = plan_mod.build_fact_write_migration_plan(tmp_path, db_path=None, sample_limit=5)
    entries = {entry["file"]: entry for entry in report["file_entries"]}
    lane = {row["lane"]: row for row in report["lanes"]}["isolated_shadow_ssot_writer"]

    assert entries["sequential_bayes_public_ssot_shadow_writer.py"]["lane"] == "isolated_shadow_ssot_writer"
    assert entries["sequential_bayes_public_ssot_shadow_writer.py"]["table_matches"] == {}
    assert entries["sequential_bayes_public_ssot_shadow_writer.py"]["non_live_table_matches"] == {
        "event_fact_history": 1,
        "event_fact_latest": 1,
    }
    assert entries["sequential_bayes_public_ssot_shadow_writer.py"]["status"] == "isolated_shadow_ssot_writer_manifest_boundary_present"
    assert lane["status"] == "isolated_shadow_ssot_writer_manifest_boundary_present"
    assert report["pending_migration_file_count"] == 0


def test_fact_write_migration_plan_marks_temporary_drill_writer_boundary_complete(tmp_path):
    _write(
        tmp_path / "scripts" / "posterior_target_direct_latency_drill.py",
        """
DRILL_FACT_COMPOSER_VERSION = "posterior_target_direct_latency_drill_fact_writer_v1"
def build_posterior_target_direct_latency_drill_boundary():
    return {
        "execution_mode": "temporary_db_copy_fake_live_drill_not_production_writer",
        "guards": {
            "writes_temporary_archive_db_copy": True,
            "writes_temporary_auto_trade_decisions": True,
            "uses_fake_order_runner": True,
        },
    }
def build_posterior_target_drill_fact_snapshot_writer_apply_plan():
    return build_fact_snapshot_write_plan(write_latest=True)
def _apply_posterior_target_drill_fact_payload():
    return apply_fact_snapshot_write_plan()
        """,
    )
    inventory = {
        "pattern_inventory": [
            {
                "id": "fact_writer_helper",
                "files": {"scripts/posterior_target_direct_latency_drill.py": 1},
                "samples": [
                    {
                        "file": "scripts/posterior_target_direct_latency_drill.py",
                        "line": 13,
                        "text": "return apply_fact_snapshot_write_plan()",
                    }
                ],
            }
        ],
        "table_write_inventory": {},
    }

    entry = plan_mod._build_file_entry(
        inventory,
        "scripts/posterior_target_direct_latency_drill.py",
        root=tmp_path,
        sample_limit=5,
    )
    lane = {
        row["lane"]: row
        for row in plan_mod._group_lanes([entry])
    }["diagnostic_repair"]

    assert entry["diagnostic_writer_apply_path_present"] is True
    assert entry["diagnostic_temp_db_boundary_present"] is True
    assert entry["status"] == "diagnostic_repair_temporary_drill_writer_apply_boundary_present"
    assert lane["diagnostic_writer_apply_path_count"] == 1
    assert lane["diagnostic_temp_db_boundary_count"] == 1
    assert lane["status"] == "diagnostic_repair_temporary_drill_writer_apply_boundary_present"
    assert "make_diagnostic_fact_repairs_no_apply_by_default" not in plan_mod._next_actions([lane])


def test_fact_write_migration_plan_marks_cleanup_retention_wrapper_complete(tmp_path):
    _write(
        tmp_path / "cleanup_data.sh",
        """
#!/bin/bash
CONFIRM_PHRASE="RETENTION_APPLY_CONFIRMED"
echo "Default mode is dry-run"
echo "It does not run direct DELETE, VACUUM, or ANALYZE SQL itself."
if [ "${PM_CLEANUP_APPLY_CONFIRM:-}" = "$CONFIRM_PHRASE" ]; then
  "$PY" "$ROOT/scripts/archive_db_governance.py" --apply
else
  "$PY" "$ROOT/scripts/archive_db_governance.py"
fi
        """,
    )

    report = plan_mod.build_fact_write_migration_plan(tmp_path, db_path=None, sample_limit=5)
    lanes = {lane["lane"]: lane for lane in report["lanes"]}
    entries = {entry["file"]: entry for entry in report["file_entries"]}

    assert "cleanup_data.sh" in report["current_fact_writer_files"]
    assert entries["cleanup_data.sh"]["maintenance_retention_policy_wrapper_present"] is True
    assert lanes["maintenance_cleanup"]["maintenance_retention_policy_wrapper_count"] == 1
    assert lanes["maintenance_cleanup"]["status"] == "maintenance_cleanup_retention_policy_wrapper_present"
    assert report["pending_migration_file_count"] == 0
    assert "move_cleanup_under_explicit_retention_policy" not in report["next_actions"]


def test_fact_write_migration_plan_marks_unknown_writer_for_manual_classification(tmp_path):
    _write(
        tmp_path / "scripts" / "custom_fact_writer.py",
        'conn.execute("INSERT INTO event_fact_history(slug) VALUES (?)")\n',
    )

    report = plan_mod.build_fact_write_migration_plan(tmp_path, db_path=None, sample_limit=5)
    unknown = [entry for entry in report["file_entries"] if entry["lane"] == "unknown_fact_writer_surface"]

    assert unknown
    assert unknown[0]["file"] == "scripts/custom_fact_writer.py"
    assert unknown[0]["requires_explicit_apply_window"] is True
    assert "classify_unknown_fact_write_surfaces_before_migration" in report["next_actions"]


def test_generic_apply_plan_is_not_misclassified_as_fact_writer(tmp_path):
    _write(
        tmp_path / "scripts" / "position_ledger_maintenance.py",
        """
def apply_plan(conn, rows):
    conn.execute("UPDATE edge_trades SET exit_skip_reason = ?", ("maintenance",))
        """,
    )

    report = plan_mod.build_fact_write_migration_plan(tmp_path, db_path=None, sample_limit=20)

    assert "scripts/position_ledger_maintenance.py" not in report["current_fact_writer_files"]
    assert report["pending_migration_file_count"] == 0


def test_active_probability_activation_closes_p0_and_incremental_writer_partition(tmp_path):
    _write(
        tmp_path / "production_probability_v6_initial_writer.py",
        """
validate_v6_activation()
def commit_v6_initial_posterior(): pass
BEGIN = "BEGIN IMMEDIATE"
conn.execute("INSERT INTO event_fact_history(slug) VALUES (?)")
conn.execute("INSERT INTO event_fact_latest(slug) VALUES (?)")
        """,
    )
    _write(
        tmp_path / "production_probability_v6_public_ssot_writer.py",
        """
validate_v6_activation()
def commit_v6_public_physical_transaction(): pass
BEGIN = "BEGIN IMMEDIATE"
conn.execute("INSERT INTO event_fact_history(slug) VALUES (?)")
conn.execute("INSERT INTO event_fact_latest(slug) VALUES (?)")
        """,
    )
    _write(
        tmp_path / "config" / "production_probability_runtime_activation.json",
        json.dumps(
                {
                    "schema": "production_probability_v6_activation_v1",
                    "state": "production_enabled",
                "production_transaction_b_authorized": True,
                "unique_probability_mainline": True,
                    "legacy_probability_writer_disabled": True,
                    "v5_fallback_forbidden": True,
                "places_orders": False,
                "authorizes_clob": False,
            }
        ),
    )

    report = plan_mod.build_fact_write_migration_plan(tmp_path, db_path=None, sample_limit=10)
    lane = {
        row["lane"]: row for row in report["lanes"]
    }["production_transaction_b_cutover_candidate"]
    entries = {
        row["file"]: row for row in report["file_entries"]
    }

    assert report["pending_migration_file_count"] == 0
    assert lane["status"] == "production_probability_ssot_writer_partition_active"
    assert lane["production_ssot_writer_active_count"] == 2
    assert entries["production_probability_v6_initial_writer.py"]["status"] == (
        "production_probability_ssot_writer_partition_active"
    )
    assert entries["production_probability_v6_public_ssot_writer.py"]["status"] == (
        "production_probability_ssot_writer_partition_active"
    )


def test_fact_write_migration_plan_summary_cli_is_read_only(tmp_path, capsys):
    _write(
        tmp_path / "ui" / "server.py",
        'conn.execute("INSERT INTO event_fact_latest(slug, event_date, fact_id) VALUES (?, ?, ?)")\n',
    )

    code = plan_mod.main(["--root", str(tmp_path), "--no-db", "--summary"])
    out = capsys.readouterr().out

    assert code == 0
    assert "fact_write_migration_plan_v1" in out
    assert '"writes_archive_db": false' in out
    assert "migrate_ui_server_fact_helpers_to_weather_fact_snapshot_writer" in out
