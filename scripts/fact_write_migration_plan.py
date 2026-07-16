#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import phase0_schema_probe
import phase0_ssot_inventory


SCHEMA_VERSION = "fact_write_migration_plan_v1"
ROOT = Path(__file__).resolve().parents[1]
TARGET_OWNER = "weather_fact_snapshot_writer.py"

FACT_TABLES: tuple[str, ...] = (
    "event_fact_history",
    "event_fact_latest",
    "event_fact_summary_cache",
)

LANE_ORDER: tuple[str, ...] = (
    "target_owner_boundary",
    "ui_current_helper",
    "hard_source_realtime",
    "source_acquisition_archive_apply",
    "posterior_materialization_worker",
    "legacy_scanner_strategy",
    "materializer_rebuild_backfill",
    "diagnostic_repair",
    "controlled_posterior_repair",
    "controlled_lineage_repair",
    "controlled_identity_repair",
    "read_only_contract_surface",
    "isolated_shadow_ssot_writer",
    "production_transaction_b_cutover_candidate",
    "retired_probability_writer_surface",
    "maintenance_cleanup",
    "unknown_fact_writer_surface",
)

LANE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "target_owner_boundary": {
        "migration_order": 0,
        "owner": "weather_fact_snapshot_writer",
        "intent": "canonical writer contract, no-apply plan boundary, and explicit apply boundary",
        "target_action": "keep_as_target_owner_and_apply_only_through_explicit_allow_apply_contract",
        "requires_explicit_apply_window": False,
    },
    "ui_current_helper": {
        "migration_order": 1,
        "owner": "ui/server.py current fact helper",
        "intent": "central legacy helper used by UI and imported by realtime scripts",
        "target_action": "replace direct helper internals with weather_fact_snapshot_writer plan/apply contract",
        "requires_explicit_apply_window": True,
    },
    "hard_source_realtime": {
        "migration_order": 2,
        "owner": "AWC/hard-source realtime path",
        "intent": "AWC hard-source signal to fact snapshot path before posterior/decision",
        "target_action": "route hard-source accepted payloads through weather_fact_snapshot_writer with source_event_id/source_observation_id and posterior_run_id lineage",
        "requires_explicit_apply_window": True,
    },
    "source_acquisition_archive_apply": {
        "migration_order": 3,
        "owner": "source acquisition archive append path",
        "intent": "AMSC/AWC source-only hard observations appended to fact history without repointing posterior latest",
        "target_action": "allow only canonical-writer source history append; source acquisition must not repoint posterior SSOT latest",
        "requires_explicit_apply_window": True,
    },
    "posterior_materialization_worker": {
        "migration_order": 3,
        "owner": "current-event posterior materialization worker",
        "intent": "consume current latest posterior plus newer source-only AMSC/AWC history and materialize a new posterior SSOT fact",
        "target_action": "allow only current-event task-queue materialization through canonical writer with no external fetch, no orders, and no historical backfill",
        "requires_explicit_apply_window": False,
    },
    "legacy_scanner_strategy": {
        "migration_order": 4,
        "owner": "legacy scanner/strategy runtime",
        "intent": "legacy strategy or scanner updates touching fact JSON or latest-derived fields",
        "target_action": "retire direct fact writes or route through canonical writer with explicit write_reason",
        "requires_explicit_apply_window": True,
    },
    "materializer_rebuild_backfill": {
        "migration_order": 5,
        "owner": "materializer/rebuild/copy scripts",
        "intent": "bulk materialization, rebuild, copy, or acceptance scripts",
        "target_action": "convert to batch writer plan with historical_backfill guard and manual approval gate",
        "requires_explicit_apply_window": True,
    },
    "diagnostic_repair": {
        "migration_order": 6,
        "owner": "diagnostic or repair tooling",
        "intent": "latency drill or repair scripts that patch fact history/latest for analysis",
        "target_action": "make default no-apply/audit-only; any repair apply must call canonical writer with repair reason",
        "requires_explicit_apply_window": True,
    },
    "controlled_posterior_repair": {
        "migration_order": 7,
        "owner": "posterior SSOT controlled repair entrypoint",
        "intent": "single latest posterior SSOT rematerialization through no-apply projection and canonical writer",
        "target_action": "apply only with explicit confirmation, fresh backup, writer quiescence, and post-apply release gate",
        "requires_explicit_apply_window": True,
    },
    "controlled_lineage_repair": {
        "migration_order": 8,
        "owner": "daily review lineage repair entrypoint",
        "intent": "targeted legacy fact lineage repair with explicit confirmation and canonical writer apply",
        "target_action": "keep preflight in repair module but execute fact mutation only through weather_fact_snapshot_writer",
        "requires_explicit_apply_window": True,
    },
    "controlled_identity_repair": {
        "migration_order": 9,
        "owner": "production identity repair entrypoint",
        "intent": "targeted production identity closeout repair with explicit confirmation, backup, and writer quiescence",
        "target_action": "allow only maintenance-window identity merge-fill/duplicate repair; never as live posterior fact writer",
        "requires_explicit_apply_window": True,
    },
    "read_only_contract_surface": {
        "migration_order": 10,
        "owner": "read-only contract or schema probe",
        "intent": "helper/schema references used for plans, probes, and operator packets without applying fact writes",
        "target_action": "keep read-only and exclude from production writer surfaces",
        "requires_explicit_apply_window": False,
    },
    "isolated_shadow_ssot_writer": {
        "migration_order": 10,
        "owner": "v2 isolated shadow-only contract writer",
        "intent": "temporary sentinel-gated v2 Transaction B proof; never a production archive writer",
        "target_action": "retain only with explicit isolated manifest and hard production-archive refusal",
        "requires_explicit_apply_window": False,
    },
    "production_transaction_b_cutover_candidate": {
        "migration_order": 10,
        "owner": "production probability SSOT transaction owners",
        "intent": "closed P0-initial and canonical-incremental writer partition for the sole production probability mainline",
        "target_action": "require one frozen activation; P0 and Transaction-B roles must remain mutually exclusive and legacy posterior writers retired",
        "requires_explicit_apply_window": True,
    },
    "retired_probability_writer_surface": {
        "migration_order": 10,
        "owner": "retired pre-reported-X probability epoch",
        "intent": "legacy probability writer retained only for isolated historical drills and tests",
        "target_action": "exclude from the active successor manifest and all reported-X entrypoints; never grant production write authority",
        "requires_explicit_apply_window": False,
    },
    "maintenance_cleanup": {
        "migration_order": 11,
        "owner": "maintenance cleanup",
        "intent": "retention or cleanup jobs that delete or compact fact records",
        "target_action": "move under explicit retention policy with reviewable dry-run and no default deletes",
        "requires_explicit_apply_window": True,
    },
    "unknown_fact_writer_surface": {
        "migration_order": 99,
        "owner": "unknown",
        "intent": "unclassified fact writer surface",
        "target_action": "manually classify before changing production behavior",
        "requires_explicit_apply_window": True,
    },
}

COMPLETED_LANE_STATUSES: frozenset[str] = frozenset(
    {
        "target_owner_apply_contract_present",
        "target_owner_ready",
        "ui_current_helper_single_writer_path_present",
        "compat_adapter_alignment_apply_wrapper_parity_split_writer_apply_path_function_gate_sample_suite_and_default_switch_present",
        "hard_source_realtime_writer_apply_default_present",
        "source_acquisition_archive_apply_writer_boundary_present",
        "posterior_materialization_worker_boundary_present",
        "legacy_scanner_q_calibrated_writer_apply_path_present",
        "materializer_ctp_writer_apply_path_with_historical_guard_present",
        "materializer_copy_acceptance_writer_apply_path_with_temp_db_guard_present",
        "materializer_archive_rebuild_candidate_copy_guard_present",
        "materializer_rebuild_writer_apply_or_candidate_copy_paths_present",
        "diagnostic_repair_temporary_drill_writer_apply_boundary_present",
        "controlled_posterior_repair_writer_apply_boundary_present",
        "controlled_lineage_repair_writer_apply_boundary_present",
        "controlled_identity_repair_apply_boundary_present",
        "read_only_contract_surface_no_apply_boundary_present",
        "isolated_shadow_ssot_writer_manifest_boundary_present",
        "production_transaction_b_cutover_candidate_default_disabled",
        "production_probability_ssot_writer_partition_active",
        "retired_probability_writer_unreachable_from_active_epoch",
        "maintenance_cleanup_retention_policy_wrapper_present",
    }
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _classify_lane(path: str) -> str:
    rel = str(path or "").strip()
    if rel == TARGET_OWNER:
        return "target_owner_boundary"
    if rel == "ui/server.py":
        return "ui_current_helper"
    if rel in {
        "scripts/hard_source_direct_poller.py",
        "scripts/promote_final_bucket_lock_facts.py",
    }:
        return "hard_source_realtime"
    if rel == "scripts/apply_amsc_zuuu_fact_snapshot_plan.py":
        return "source_acquisition_archive_apply"
    if rel == "scripts/materialize_amsc_posterior_task.py":
        return "posterior_materialization_worker"
    if rel == "scan_weather_market.py":
        return "legacy_scanner_strategy"
    if rel in {
        "scripts/archive_db_rebuild_build.py",
        "scripts/hard_source_direct_copy_acceptance.py",
        "scripts/materialize_ctp_latest_facts.py",
    }:
        return "materializer_rebuild_backfill"
    if rel == "scripts/posterior_target_direct_latency_drill.py":
        return "diagnostic_repair"
    if rel == "scripts/apply_posterior_ssot_fact_repair.py":
        return "controlled_posterior_repair"
    if rel == "daily_review_lineage_repair_writer.py":
        return "controlled_lineage_repair"
    if rel in {
        "scripts/production_identity_duplicate_fact_snapshot_apply.py",
        "scripts/production_identity_merge_fill_apply.py",
        "scripts/repair_latest_to_attested_amsc_posterior.py",
    }:
        return "controlled_identity_repair"
    if rel in {
        "posterior_ssot_projection_runtime.py",
        "scripts/canonical_writer_persisted_coverage_audit.py",
        "scripts/daily_review_persistence_batch_plan.py",
        "scripts/fetch_zuuu_metar_taf.py",
        "scripts/forecast_weak_likelihood_contract_audit.py",
        "scripts/fact_writer_persisted_apply_contract_audit.py",
        "scripts/hierarchical_core_integration_contract_audit.py",
        "scripts/live_offline_evidence_isolation_audit.py",
        "scripts/metar_strong_likelihood_contract_audit.py",
        "scripts/posterior_ssot_apply_confirmation_packet.py",
        "scripts/posterior_ssot_fact_coverage_audit.py",
        "scripts/posterior_ssot_rematerialization_plan.py",
        "scripts/phase0_schema_probe.py",
        "scripts/production_chain_uniqueness_audit.py",
        "scripts/production_identity_duplicate_fact_snapshot_apply_contract.py",
        "scripts/production_identity_merge_fill_apply_contract.py",
        "scripts/prior_layer_contract_audit.py",
        "production_probability_runtime_manifest.py",
        "scripts/retired_model_taf_conflict_audit.py",
        "scripts/source_acquisition_contract_audit.py",
    }:
        return "read_only_contract_surface"
    if rel == "sequential_bayes_public_ssot_shadow_writer.py":
        return "isolated_shadow_ssot_writer"
    if rel in {
        "production_probability_v6_amsc_archive_ingress.py",
        "production_probability_v6_initial_writer.py",
        "production_probability_v6_public_ssot_writer.py",
        "production_probability_v6_weak_revision_writer.py",
    }:
        return "production_transaction_b_cutover_candidate"
    if rel in {
        "production_amsc_archive_probability_ingress.py",
        "production_initial_posterior_writer.py",
        "production_reported_x_amsc_archive_probability_ingress.py",
        "production_reported_x_initial_writer.py",
        "production_reported_x_public_ssot_writer.py",
        "production_reported_x_weak_revision_writer.py",
        "production_sequential_bayes_public_ssot_writer.py",
        "production_weak_forecast_revision_writer.py",
    }:
        return "retired_probability_writer_surface"
    if rel == "cleanup_data.sh":
        return "maintenance_cleanup"
    return "unknown_fact_writer_surface"


def _pattern_by_id(inventory: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for row in inventory.get("pattern_inventory") or []:
        if isinstance(row, Mapping):
            out[str(row.get("id") or "")] = row
    return out


def _table_inventory(inventory: Mapping[str, Any]) -> Mapping[str, Any]:
    value = inventory.get("table_write_inventory")
    return value if isinstance(value, Mapping) else {}


def _files_from_inventory(inventory: Mapping[str, Any]) -> list[str]:
    files: set[str] = set()
    tables = _table_inventory(inventory)
    for table in FACT_TABLES:
        item = tables.get(table)
        if not isinstance(item, Mapping):
            continue
        for path in list(item.get("writer_files") or []) + list(item.get("non_live_writer_files") or []):
            text = str(path or "").strip()
            if text:
                files.add(text)
    fact_helper = _pattern_by_id(inventory).get("fact_writer_helper") or {}
    for path in (fact_helper.get("files") or {}).keys():
        text = str(path or "").strip()
        if text:
            files.add(text)
    return sorted(files)


def _file_samples(patterns: Mapping[str, Mapping[str, Any]], path: str, *, limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for pattern_id in (
        "write_event_fact_history",
        "write_event_fact_latest",
        "write_event_fact_summary_cache",
        "fact_writer_helper",
    ):
        pattern = patterns.get(pattern_id) or {}
        for sample in pattern.get("samples") or []:
            if isinstance(sample, Mapping) and str(sample.get("file") or "") == path:
                samples.append(
                    {
                        "pattern_id": pattern_id,
                        "line": sample.get("line"),
                        "text": sample.get("text"),
                    }
                )
                if len(samples) >= limit:
                    return samples
    return samples


def _file_table_matches(
    inventory: Mapping[str, Any],
    path: str,
    *,
    file_key: str = "writer_files",
) -> dict[str, int]:
    out: dict[str, int] = {}
    tables = _table_inventory(inventory)
    for table in FACT_TABLES:
        item = tables.get(table)
        if not isinstance(item, Mapping):
            continue
        files = item.get(file_key) or []
        if path in files:
            # table_write_inventory exposes files but not per-file counts; pattern inventory has counts.
            pattern_id = str(item.get("pattern_id") or "")
            pattern = _pattern_by_id(inventory).get(pattern_id) or {}
            per_file = pattern.get("files") if isinstance(pattern.get("files"), Mapping) else {}
            out[table] = int(per_file.get(path) or 0)
    return out


def _file_helper_matches(patterns: Mapping[str, Mapping[str, Any]], path: str) -> int:
    helper = patterns.get("fact_writer_helper") or {}
    files = helper.get("files") if isinstance(helper.get("files"), Mapping) else {}
    return int(files.get(path) or 0)


def _file_contains(root: Path, path: str, token: str) -> bool:
    try:
        text = (Path(root) / path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return str(token or "") in text


def _production_probability_activation_state(root: Path) -> str:
    path = Path(root) / "config/production_probability_runtime_activation.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return "missing_or_invalid"
    if not isinstance(payload, Mapping):
        return "missing_or_invalid"
    state = str(payload.get("state") or "")
    if payload.get("places_orders") is not False or payload.get("authorizes_clob") is not False:
        return "invalid_execution_authority"
    if state == "production_enabled" and all(
        payload.get(key) is True
        for key in (
            "production_transaction_b_authorized",
            "unique_probability_mainline",
            "legacy_probability_writer_disabled",
        )
    ):
        return "production_enabled"
    if state == "disabled":
        return "disabled"
    return "invalid_state"


def _production_probability_activation(root: Path) -> Mapping[str, Any]:
    path = Path(root) / "config/production_probability_runtime_activation.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _retired_weak_writer_unreachable_from_active_epoch(root: Path) -> bool:
    """Prove the v2 weak writer is outside the reported-X activation closure."""

    try:
        manifest = (root / "production_probability_successor_runtime_manifest.py").read_text(
            encoding="utf-8"
        )
        active_event = (root / "production_reported_x_weak_revision_event_runtime.py").read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    return bool(
        "SUCCESSOR_EXCLUDED_LEGACY_FILES" in manifest
        and '"production_weak_forecast_revision_writer.py"' in manifest
        and "if name not in SUCCESSOR_EXCLUDED_LEGACY_FILES" in manifest
        and "production_weak_forecast_revision_event_runtime" not in active_event
        and "production_weak_forecast_revision_writer" not in active_event
        and "taf_revision_candidate_selection_runtime" in active_event
    )


def _risk_flags(*, path: str, lane: str, table_matches: Mapping[str, int], helper_match_count: int) -> dict[str, bool]:
    is_target = lane == "target_owner_boundary"
    read_only_contract = lane == "read_only_contract_surface"
    return {
        "direct_sql_write_surface": bool(table_matches) and not is_target,
        "uses_fact_helper_surface": bool(helper_match_count) and not is_target and not read_only_contract,
        "can_repoint_latest": bool(table_matches.get("event_fact_latest")) or (lane in {"ui_current_helper", "hard_source_realtime"}),
        "can_append_history": bool(table_matches.get("event_fact_history")) or (bool(helper_match_count) and not read_only_contract),
        "can_update_summary_cache": bool(table_matches.get("event_fact_summary_cache")),
        "historical_backfill_risk": lane in {"materializer_rebuild_backfill", "maintenance_cleanup"},
        "hard_source_realtime_risk": lane == "hard_source_realtime",
        "target_owner": is_target,
    }


def _build_file_entry(
    inventory: Mapping[str, Any],
    path: str,
    *,
    root: Path,
    sample_limit: int,
) -> dict[str, Any]:
    patterns = _pattern_by_id(inventory)
    lane = _classify_lane(path)
    lane_def = LANE_DEFINITIONS[lane]
    table_matches = _file_table_matches(inventory, path)
    non_live_table_matches = _file_table_matches(inventory, path, file_key="non_live_writer_files")
    helper_match_count = _file_helper_matches(patterns, path)
    risk_flags = _risk_flags(path=path, lane=lane, table_matches=table_matches, helper_match_count=helper_match_count)
    compat_adapter_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "build_ui_event_fact_write_compat_plan")
    )
    alignment_audit_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "build_ui_event_fact_write_compat_alignment_audit")
    )
    apply_parity_audit_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "build_ui_event_fact_write_apply_parity_audit")
    )
    apply_contract_present = bool(
        lane == "target_owner_boundary"
        and _file_contains(Path(root), path, "def apply_fact_snapshot_write_plan")
    )
    post_apply_verification_present = bool(
        lane == "target_owner_boundary"
        and _file_contains(Path(root), path, "def build_post_apply_persisted_posterior_ssot_verification")
        and _file_contains(Path(root), path, "post_apply_persisted_posterior_ssot_verification_failed")
    )
    apply_result_summary_helper_present = bool(
        lane == "target_owner_boundary"
        and _file_contains(Path(root), path, "def compact_fact_snapshot_writer_apply_result_for_lineage")
        and _file_contains(Path(root), path, "post_apply_persisted_verification_ok")
    )
    writer_apply_result_summary_exposed = bool(
        lane != "target_owner_boundary"
        and not table_matches
        and _file_contains(Path(root), path, "compact_fact_snapshot_writer_apply_result_for_lineage")
        and _file_contains(Path(root), path, "writer_apply_result_summary")
    )
    apply_wrapper_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "def apply_ui_event_fact_write_compat_plan")
    )
    materialized_payload_split_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "_finalize_event_fact_history_materialized_payload_for_storage")
        and _file_contains(Path(root), path, "_insert_materialized_event_fact_history_row")
    )
    helper_writer_apply_path_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "build_materialized_event_fact_history_writer_apply_plan")
        and _file_contains(Path(root), path, "use_writer_apply")
        and _file_contains(Path(root), path, "write_latest=False")
    )
    function_parity_gate_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "build_event_fact_history_insert_function_parity_audit")
        and _file_contains(Path(root), path, "uses_in_memory_db")
    )
    function_parity_sample_suite_present = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, "build_event_fact_history_insert_function_parity_sample_suite")
        and _file_contains(Path(root), path, "sample_case_ids")
    )
    helper_writer_apply_default_enabled = bool(
        lane == "ui_current_helper"
        and _file_contains(Path(root), path, 'PM_UI_FACT_HELPER_WRITER_APPLY_ENABLED", True')
        and _file_contains(Path(root), path, "kill_switch_env")
    )
    helper_single_writer_path_present = bool(
        lane == "ui_current_helper"
        and helper_writer_apply_path_present
        and _file_contains(Path(root), path, "legacy_path_removed")
        and _file_contains(Path(root), path, "latest_repoint_owned_by_weather_fact_snapshot_writer")
        and _file_contains(Path(root), path, "compact_weather_replaced_event_fact_history_row")
    )
    hard_source_writer_apply_path_present = bool(
        lane == "hard_source_realtime"
        and (
            _file_contains(Path(root), path, "build_hard_source_fact_snapshot_writer_apply_plan")
            or _file_contains(Path(root), path, "build_final_bucket_lock_fact_snapshot_writer_apply_plan")
        )
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "write_latest=True")
    )
    hard_source_writer_apply_default_enabled = bool(
        hard_source_writer_apply_path_present
        and (
            (
                _file_contains(Path(root), path, "PM_HARD_SOURCE_FACT_WRITER_APPLY_ENABLED")
                and _file_contains(Path(root), path, "PM_HARD_SOURCE_FACT_WRITER_APPLY_ENABLED=0")
            )
            or (
                _file_contains(Path(root), path, "CONFIRM_PHRASE")
                and _file_contains(Path(root), path, "_sqlite_backup")
            )
        )
    )
    source_acquisition_archive_apply_writer_boundary_present = bool(
        lane == "source_acquisition_archive_apply"
        and path == "scripts/apply_amsc_zuuu_fact_snapshot_plan.py"
        and not table_matches
        and _file_contains(Path(root), path, "def apply_amsc_zuuu_fact_snapshot_plan")
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "CONFIRM_PHRASE")
        and _file_contains(Path(root), path, "allow_apply")
        and _file_contains(Path(root), path, "write_latest=False")
        and _file_contains(Path(root), path, "source_acquisition_latest_repoint_policy")
        and _file_contains(Path(root), path, "source_acquisition_may_append_history_but_must_not_repoint_posterior_ssot_latest")
        and _file_contains(Path(root), path, '"writes_event_fact_latest": False')
    )
    posterior_materialization_worker_boundary_present = bool(
        lane == "posterior_materialization_worker"
        and path == "scripts/materialize_amsc_posterior_task.py"
        and not table_matches
        and _file_contains(Path(root), path, "def materialize_amsc_posterior_task")
        and _file_contains(Path(root), path, "build_fact_projection_no_apply")
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "enable_apply_contract=True")
        and _file_contains(Path(root), path, "write_latest=True")
        and _file_contains(Path(root), path, "source_history_observations_merged")
        and _file_contains(Path(root), path, "fetches_external_weather_sources")
        and _file_contains(Path(root), path, '"places_orders": False')
        and _file_contains(Path(root), path, '"historical_backfill": False')
        and _file_contains(Path(root), path, '"writes_auto_trade_decisions": False')
    )
    scanner_writer_apply_path_present = bool(
        lane == "legacy_scanner_strategy"
        and _file_contains(Path(root), path, "build_q_calibrated_writeback_fact_snapshot_writer_apply_plan")
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "write_latest=True")
        and _file_contains(Path(root), path, "Q_CALIBRATED_WRITEBACK_COMPOSER_VERSION")
    )
    scanner_historical_guard_present = bool(
        scanner_writer_apply_path_present
        and _file_contains(Path(root), path, "--q-calibrated-writeback-allow-historical")
        and _file_contains(Path(root), path, "historical_writeback_requires_explicit_allow")
        and _file_contains(Path(root), path, 'today_date_str("America/New_York")')
    )
    materializer_ctp_writer_apply_path_present = bool(
        lane == "materializer_rebuild_backfill"
        and path == "scripts/materialize_ctp_latest_facts.py"
        and _file_contains(Path(root), path, "build_ctp_materialized_fact_snapshot_writer_apply_plan")
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "build_fact_summary_cache_repair_plan")
        and _file_contains(Path(root), path, "apply_fact_summary_cache_repair_plan")
        and _file_contains(Path(root), path, "write_latest=True")
    )
    materializer_historical_guard_present = bool(
        materializer_ctp_writer_apply_path_present
        and _file_contains(Path(root), path, "--allow-historical")
        and _file_contains(Path(root), path, "historical_apply_requires_explicit_allow")
        and _file_contains(Path(root), path, 'ZoneInfo("America/New_York")')
    )
    materializer_copy_acceptance_writer_apply_path_present = bool(
        lane == "materializer_rebuild_backfill"
        and path == "scripts/hard_source_direct_copy_acceptance.py"
        and _file_contains(Path(root), path, "build_copy_acceptance_fact_snapshot_writer_apply_plan")
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "write_latest=True")
        and _file_contains(Path(root), path, "COPY_ACCEPTANCE_COMPOSER_VERSION")
    )
    materializer_temp_db_guard_present = bool(
        materializer_copy_acceptance_writer_apply_path_present
        and _file_contains(Path(root), path, "temporary_db_copy")
        and _file_contains(Path(root), path, "writes_temporary_archive_db_copy")
        and _file_contains(Path(root), path, "historical_backfill")
    )
    materializer_archive_rebuild_candidate_copy_guard_present = bool(
        lane == "materializer_rebuild_backfill"
        and path == "scripts/archive_db_rebuild_build.py"
        and _file_contains(Path(root), path, "build_archive_rebuild_candidate_copy_boundary")
        and _file_contains(Path(root), path, "candidate_db_table_copy_not_fact_snapshot_mutation")
        and _file_contains(Path(root), path, "writes_candidate_archive_db")
        and _file_contains(Path(root), path, "replaces_source_db")
        and _file_contains(Path(root), path, "not_applicable_for_candidate_copy")
    )
    diagnostic_writer_apply_path_present = bool(
        lane == "diagnostic_repair"
        and path == "scripts/posterior_target_direct_latency_drill.py"
        and _file_contains(Path(root), path, "build_posterior_target_drill_fact_snapshot_writer_apply_plan")
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "write_latest=True")
        and _file_contains(Path(root), path, "DRILL_FACT_COMPOSER_VERSION")
    )
    diagnostic_temp_db_boundary_present = bool(
        diagnostic_writer_apply_path_present
        and _file_contains(Path(root), path, "build_posterior_target_direct_latency_drill_boundary")
        and _file_contains(Path(root), path, "temporary_db_copy_fake_live_drill_not_production_writer")
        and _file_contains(Path(root), path, "writes_temporary_archive_db_copy")
        and _file_contains(Path(root), path, "writes_temporary_auto_trade_decisions")
        and _file_contains(Path(root), path, "uses_fake_order_runner")
    )
    controlled_posterior_repair_writer_apply_path_present = bool(
        lane == "controlled_posterior_repair"
        and path == "scripts/apply_posterior_ssot_fact_repair.py"
        and not table_matches
        and _file_contains(Path(root), path, "def apply_posterior_ssot_fact_repair")
        and _file_contains(Path(root), path, "build_fact_projection_no_apply")
        and _file_contains(Path(root), path, "apply_fact_snapshot_write_plan")
        and _file_contains(Path(root), path, "enable_apply_contract=True")
        and _file_contains(Path(root), path, "write_latest=True")
    )
    controlled_posterior_repair_preflight_present = bool(
        controlled_posterior_repair_writer_apply_path_present
        and _file_contains(Path(root), path, "CONFIRMATION_PHRASE")
        and _file_contains(Path(root), path, "allow_apply")
        and _file_contains(Path(root), path, "writer_quiescent")
        and _file_contains(Path(root), path, "writer_quiescent_false")
        and _file_contains(Path(root), path, "_backup_sqlite")
        and _file_contains(Path(root), path, "backup_failed")
    )
    controlled_lineage_repair_writer_apply_path_present = bool(
        lane == "controlled_lineage_repair"
        and path == "daily_review_lineage_repair_writer.py"
        and _file_contains(Path(root), path, "apply_event_fact_history_merge_fill_repair_contract")
        and _file_contains(Path(root), path, "weather_fact_snapshot_writer.WRITE_OWNER")
        and not table_matches
    )
    controlled_lineage_repair_preflight_present = bool(
        controlled_lineage_repair_writer_apply_path_present
        and _file_contains(Path(root), path, "CONFIRMATION_PHRASE")
        and _file_contains(Path(root), path, "backup_path_required_before_lineage_repair_apply")
        and _file_contains(Path(root), path, "requires_explicit_writer_quiescent_assertion")
    )
    controlled_identity_repair_apply_boundary_present = bool(
        lane == "controlled_identity_repair"
        and path
        in {
            "scripts/production_identity_duplicate_fact_snapshot_apply.py",
            "scripts/production_identity_merge_fill_apply.py",
        }
        and table_matches
        and _file_contains(Path(root), path, "CONFIRMATION_PHRASE")
        and _file_contains(Path(root), path, "writer_quiescent")
        and _file_contains(Path(root), path, "_verify_existing_backup")
        and _file_contains(Path(root), path, "controlled_apply_maintenance_window_packet")
        and _file_contains(Path(root), path, '"places_orders": False')
        and _file_contains(Path(root), path, '"historical_backfill": False')
        and _file_contains(Path(root), path, '"fetches_external_weather_sources": False')
        and _file_contains(Path(root), path, '"restarts_launchd": False')
        and _file_contains(Path(root), path, '"creates_backup": False')
    )
    attested_latest_repair_boundary_present = bool(
        lane == "controlled_identity_repair"
        and path == "scripts/repair_latest_to_attested_amsc_posterior.py"
        and table_matches
        and _file_contains(Path(root), path, "writer_quiescent_assertion_required")
        and _file_contains(Path(root), path, "backup_path_required_before_attested_latest_repair")
        and _file_contains(Path(root), path, "_verify_fresh_backup")
        and _file_contains(Path(root), path, "latest_pointer_compare_and_swap_conflict")
    )
    read_only_contract_surface_present = bool(
        lane == "read_only_contract_surface"
        and (
            (
                not table_matches
                and (
                    _file_contains(Path(root), path, '"writes_facts_sqlite": False')
                    or _file_contains(Path(root), path, '"writes_event_fact_history": False')
                    or _file_contains(Path(root), path, '"writes_archive_db": False')
                    or _file_contains(Path(root), path, "mode=ro")
                )
                and (
                    _file_contains(Path(root), path, "read_only")
                    or _file_contains(Path(root), path, "no_apply")
                    or _file_contains(Path(root), path, "contract")
                    or _file_contains(Path(root), path, "audit")
                )
            )
            or
            (
                path
                in {
                    "scripts/production_identity_duplicate_fact_snapshot_apply_contract.py",
                    "scripts/production_identity_merge_fill_apply_contract.py",
                }
                and _file_contains(Path(root), path, "CONFIRMATION_PHRASE")
                and _file_contains(Path(root), path, '"writes_archive_db": False')
                and _file_contains(Path(root), path, '"writes_event_fact_history": False')
                and _file_contains(Path(root), path, '"executes_sql_writes": False')
                and _file_contains(Path(root), path, '"creates_backup": False')
                and _file_contains(Path(root), path, "read_only")
                and _file_contains(Path(root), path, "dry_run")
            )
            or
            (
                path == "scripts/daily_review_persistence_batch_plan.py"
                and _file_contains(Path(root), path, "LINEAGE_REPAIR_APPLY_CONTRACT_SCHEMA")
                and _file_contains(Path(root), path, '"writes_event_fact_history": False')
                and _file_contains(Path(root), path, "lineage_repair_apply_contract_no_apply")
            )
            or (
                path == "scripts/phase0_schema_probe.py"
                and _file_contains(Path(root), path, "REQUIRED_TABLE_COLUMNS")
                and _file_contains(Path(root), path, "TABLE_ID_CONSTRAINTS")
                and _file_contains(Path(root), path, "write_owner")
            )
            or (
                path == "scripts/production_chain_uniqueness_audit.py"
                and _file_contains(Path(root), path, "production_chain_uniqueness_read_only_audit")
                and _file_contains(Path(root), path, "writes_archive_db")
                and _file_contains(Path(root), path, "posterior_ssot_fact")
            )
            or (
                path == "production_probability_runtime_manifest.py"
                and _file_contains(Path(root), path, "RUNTIME_FILES")
                and _file_contains(Path(root), path, "build_current_runtime_manifest")
                and not table_matches
            )
        )
    )
    isolated_shadow_ssot_writer_boundary_present = bool(
        lane == "isolated_shadow_ssot_writer"
        and path == "sequential_bayes_public_ssot_shadow_writer.py"
        and _file_contains(Path(root), path, "build_isolated_public_ssot_shadow_boundary")
        and _file_contains(Path(root), path, "isolated_shadow_only_temp_db_never_production_archive")
        and _file_contains(Path(root), path, "SHADOW_MANIFEST_TABLE")
        and _file_contains(Path(root), path, "_assert_isolated_shadow_db")
        and _file_contains(Path(root), path, '"writes_production_archive": False')
        and _file_contains(Path(root), path, '"places_orders": False')
    )
    incremental_production_candidate = bool(
        lane == "production_transaction_b_cutover_candidate"
        and (
            (
                path == "production_sequential_bayes_public_ssot_writer.py"
                and _file_contains(Path(root), path, "validate_production_probability_activation")
                and _file_contains(Path(root), path, "production_transaction_b_refuses_shadow_database")
                and _file_contains(Path(root), path, "production_transaction_b_requires_no_ambient_transaction")
            )
            or (
                path in {
                    "production_probability_v6_public_ssot_writer.py",
                    "production_probability_v6_weak_revision_writer.py",
                }
                and _file_contains(Path(root), path, "validate_v6_activation")
                and _file_contains(Path(root), path, "BEGIN IMMEDIATE")
            )
            or (
                path in {
                    "production_reported_x_public_ssot_writer.py",
                    "production_reported_x_weak_revision_writer.py",
                }
                and _file_contains(Path(root), path, "validate_successor_activation")
                and _file_contains(Path(root), path, "_assert_boundary")
                and _file_contains(Path(root), path, "BEGIN IMMEDIATE")
            )
        )
    )
    initial_production_candidate = bool(
        lane == "production_transaction_b_cutover_candidate"
        and (
            (
                path == "production_initial_posterior_writer.py"
                and _file_contains(Path(root), path, "validate_production_probability_activation")
                and _file_contains(Path(root), path, "production_initial_checkpoint_refuses_shadow_database")
                and _file_contains(Path(root), path, "production_initial_checkpoint_requires_no_ambient_transaction")
            )
            or (
                path == "production_probability_v6_initial_writer.py"
                and _file_contains(Path(root), path, "validate_v6_activation")
                and _file_contains(Path(root), path, "commit_v6_initial_posterior")
                and _file_contains(Path(root), path, "BEGIN IMMEDIATE")
            )
            or (
                path == "production_reported_x_initial_writer.py"
                and _file_contains(Path(root), path, "_assert_boundary")
                and _file_contains(Path(root), path, "commit_reported_x_initial_posterior")
                and _file_contains(Path(root), path, "BEGIN IMMEDIATE")
            )
        )
    )
    production_probability_ingress_boundary = bool(
        lane == "production_transaction_b_cutover_candidate"
        and (
            (
                path == "production_amsc_archive_probability_ingress.py"
                and not table_matches
                and _file_contains(Path(root), path, "apply_p2_canonical_amsc_forward_append")
                and _file_contains(Path(root), path, "drain_production_canonical_events")
                and _file_contains(Path(root), path, "canonical_retained_probability_blocked")
                and _file_contains(Path(root), path, '"places_orders": False')
            )
            or (
                path == "production_probability_v6_amsc_archive_ingress.py"
                and not table_matches
                and _file_contains(Path(root), path, "validate_v6_activation")
                and _file_contains(Path(root), path, "drain_v6_canonical_events")
                and _file_contains(Path(root), path, "canonical_retained_v6_probability_blocked")
                and _file_contains(Path(root), path, '"places_orders": False')
            )
            or (
                path == "production_reported_x_amsc_archive_probability_ingress.py"
                and not table_matches
                and _file_contains(Path(root), path, "validate_successor_activation")
                and _file_contains(Path(root), path, "drain_reported_x_canonical_events")
                and _file_contains(Path(root), path, "commit_reported_x_initial_posterior")
                and _file_contains(Path(root), path, '"places_orders": False')
            )
        )
    )
    production_writer_boundary_present = bool(
        incremental_production_candidate
        or initial_production_candidate
        or production_probability_ingress_boundary
    )
    production_activation_state = _production_probability_activation_state(Path(root))
    production_transaction_b_cutover_candidate_present = bool(
        production_writer_boundary_present and production_activation_state == "disabled"
    )
    production_probability_ssot_writer_active = bool(
        production_writer_boundary_present
        and production_activation_state == "production_enabled"
    )
    active_activation = _production_probability_activation(Path(root))
    retired_probability_writer_unreachable = bool(
        lane == "retired_probability_writer_surface"
        and production_activation_state == "production_enabled"
        and str(active_activation.get("schema") or "") == "production_probability_v6_activation_v1"
        and active_activation.get("legacy_probability_writer_disabled") is True
        and active_activation.get("v5_fallback_forbidden") is True
    )
    maintenance_retention_policy_wrapper_present = bool(
        lane == "maintenance_cleanup"
        and path == "cleanup_data.sh"
        and _file_contains(Path(root), path, "scripts/archive_db_governance.py")
        and _file_contains(Path(root), path, "Default mode is dry-run")
        and _file_contains(Path(root), path, "PM_CLEANUP_APPLY_CONFIRM")
        and _file_contains(Path(root), path, "RETENTION_APPLY_CONFIRMED")
        and _file_contains(Path(root), path, "It does not run direct DELETE, VACUUM, or ANALYZE SQL itself.")
    )
    status = "target_owner_ready" if lane == "target_owner_boundary" else "pending_migration"
    if maintenance_retention_policy_wrapper_present:
        status = "maintenance_cleanup_retention_policy_wrapper_present"
    if diagnostic_writer_apply_path_present and diagnostic_temp_db_boundary_present:
        status = "diagnostic_repair_temporary_drill_writer_apply_boundary_present"
    elif diagnostic_writer_apply_path_present:
        status = "diagnostic_repair_writer_apply_path_present_pending_temp_db_boundary"
    if controlled_posterior_repair_writer_apply_path_present and controlled_posterior_repair_preflight_present:
        status = "controlled_posterior_repair_writer_apply_boundary_present"
    elif controlled_posterior_repair_writer_apply_path_present:
        status = "controlled_posterior_repair_writer_apply_path_present_pending_preflight"
    if controlled_lineage_repair_writer_apply_path_present and controlled_lineage_repair_preflight_present:
        status = "controlled_lineage_repair_writer_apply_boundary_present"
    elif controlled_lineage_repair_writer_apply_path_present:
        status = "controlled_lineage_repair_writer_apply_path_present_pending_preflight"
    if controlled_identity_repair_apply_boundary_present or attested_latest_repair_boundary_present:
        status = "controlled_identity_repair_apply_boundary_present"
    if read_only_contract_surface_present:
        status = "read_only_contract_surface_no_apply_boundary_present"
    if isolated_shadow_ssot_writer_boundary_present:
        status = "isolated_shadow_ssot_writer_manifest_boundary_present"
    if production_transaction_b_cutover_candidate_present:
        status = "production_transaction_b_cutover_candidate_default_disabled"
    if production_probability_ssot_writer_active:
        status = "production_probability_ssot_writer_partition_active"
    if retired_probability_writer_unreachable:
        status = "retired_probability_writer_unreachable_from_active_epoch"
    if apply_contract_present:
        status = "target_owner_apply_contract_present"
    if materializer_archive_rebuild_candidate_copy_guard_present:
        status = "materializer_archive_rebuild_candidate_copy_guard_present"
    if materializer_copy_acceptance_writer_apply_path_present and materializer_temp_db_guard_present:
        status = "materializer_copy_acceptance_writer_apply_path_with_temp_db_guard_present"
    elif materializer_copy_acceptance_writer_apply_path_present:
        status = "materializer_copy_acceptance_writer_apply_path_present_pending_temp_db_guard"
    if materializer_ctp_writer_apply_path_present and materializer_historical_guard_present:
        status = "materializer_ctp_writer_apply_path_with_historical_guard_present"
    elif materializer_ctp_writer_apply_path_present:
        status = "materializer_ctp_writer_apply_path_present_pending_historical_guard"
    if scanner_writer_apply_path_present and scanner_historical_guard_present:
        status = "legacy_scanner_q_calibrated_writer_apply_path_present"
    elif scanner_writer_apply_path_present:
        status = "legacy_scanner_writer_apply_path_present_pending_historical_guard"
    if hard_source_writer_apply_default_enabled:
        status = "hard_source_realtime_writer_apply_default_present"
    elif hard_source_writer_apply_path_present:
        status = "hard_source_realtime_writer_apply_path_present_pending_default_switch"
    if source_acquisition_archive_apply_writer_boundary_present:
        status = "source_acquisition_archive_apply_writer_boundary_present"
    if posterior_materialization_worker_boundary_present:
        status = "posterior_materialization_worker_boundary_present"
    if (
        helper_single_writer_path_present
        and compat_adapter_present
        and alignment_audit_present
        and apply_wrapper_present
        and apply_parity_audit_present
        and materialized_payload_split_present
        and function_parity_gate_present
        and function_parity_sample_suite_present
    ):
        status = "ui_current_helper_single_writer_path_present"
    elif (
        compat_adapter_present
        and alignment_audit_present
        and apply_wrapper_present
        and apply_parity_audit_present
        and materialized_payload_split_present
        and helper_writer_apply_path_present
        and function_parity_gate_present
        and function_parity_sample_suite_present
        and helper_writer_apply_default_enabled
    ):
        status = "compat_adapter_alignment_apply_wrapper_parity_split_writer_apply_path_function_gate_sample_suite_and_default_switch_present"
    elif (
        compat_adapter_present
        and alignment_audit_present
        and apply_wrapper_present
        and apply_parity_audit_present
        and materialized_payload_split_present
        and helper_writer_apply_path_present
        and function_parity_gate_present
        and function_parity_sample_suite_present
    ):
        status = "compat_adapter_alignment_apply_wrapper_parity_split_writer_apply_path_function_gate_and_sample_suite_present_pending_default_switch"
    elif (
        compat_adapter_present
        and alignment_audit_present
        and apply_wrapper_present
        and apply_parity_audit_present
        and materialized_payload_split_present
        and helper_writer_apply_path_present
        and function_parity_gate_present
    ):
        status = "compat_adapter_alignment_apply_wrapper_parity_split_writer_apply_path_and_function_gate_present_pending_default_switch"
    elif (
        compat_adapter_present
        and alignment_audit_present
        and apply_wrapper_present
        and apply_parity_audit_present
        and materialized_payload_split_present
        and helper_writer_apply_path_present
    ):
        status = "compat_adapter_alignment_apply_wrapper_parity_split_and_writer_apply_path_present_pending_default_switch"
    elif (
        compat_adapter_present
        and alignment_audit_present
        and apply_wrapper_present
        and apply_parity_audit_present
        and materialized_payload_split_present
    ):
        status = "compat_adapter_alignment_apply_wrapper_parity_and_materialized_split_present_pending_helper_switch"
    elif compat_adapter_present and alignment_audit_present and apply_wrapper_present and apply_parity_audit_present:
        status = "compat_adapter_alignment_apply_wrapper_and_parity_audit_present_pending_helper_switch"
    elif compat_adapter_present and alignment_audit_present and apply_wrapper_present:
        status = "compat_adapter_alignment_audit_and_apply_wrapper_present_pending_helper_switch"
    if compat_adapter_present and alignment_audit_present:
        status = status if apply_wrapper_present else "compat_adapter_and_alignment_audit_present_pending_apply_migration"
    if compat_adapter_present:
        status = status if alignment_audit_present else "compat_adapter_present_pending_apply_migration"
    return {
        "file": path,
        "lane": lane,
        "migration_order": lane_def["migration_order"],
        "target_owner": TARGET_OWNER,
        "current_role": lane_def["intent"],
        "target_action": lane_def["target_action"],
        "requires_explicit_apply_window": bool(lane_def["requires_explicit_apply_window"]),
        "table_matches": dict(table_matches),
        "non_live_table_matches": dict(non_live_table_matches),
        "helper_match_count": helper_match_count,
        "compat_adapter_present": compat_adapter_present,
        "alignment_audit_present": alignment_audit_present,
        "apply_parity_audit_present": apply_parity_audit_present,
        "apply_contract_present": apply_contract_present,
        "post_apply_verification_present": post_apply_verification_present,
        "apply_result_summary_helper_present": apply_result_summary_helper_present,
        "writer_apply_result_summary_exposed": writer_apply_result_summary_exposed,
        "apply_wrapper_present": apply_wrapper_present,
        "materialized_payload_split_present": materialized_payload_split_present,
        "helper_writer_apply_path_present": helper_writer_apply_path_present,
        "function_parity_gate_present": function_parity_gate_present,
        "function_parity_sample_suite_present": function_parity_sample_suite_present,
        "helper_writer_apply_default_enabled": helper_writer_apply_default_enabled,
        "helper_single_writer_path_present": helper_single_writer_path_present,
        "hard_source_writer_apply_path_present": hard_source_writer_apply_path_present,
        "hard_source_writer_apply_default_enabled": hard_source_writer_apply_default_enabled,
        "source_acquisition_archive_apply_writer_boundary_present": source_acquisition_archive_apply_writer_boundary_present,
        "posterior_materialization_worker_boundary_present": posterior_materialization_worker_boundary_present,
        "scanner_writer_apply_path_present": scanner_writer_apply_path_present,
        "scanner_historical_guard_present": scanner_historical_guard_present,
        "materializer_ctp_writer_apply_path_present": materializer_ctp_writer_apply_path_present,
        "materializer_historical_guard_present": materializer_historical_guard_present,
        "materializer_copy_acceptance_writer_apply_path_present": materializer_copy_acceptance_writer_apply_path_present,
        "materializer_temp_db_guard_present": materializer_temp_db_guard_present,
        "materializer_archive_rebuild_candidate_copy_guard_present": materializer_archive_rebuild_candidate_copy_guard_present,
        "diagnostic_writer_apply_path_present": diagnostic_writer_apply_path_present,
        "diagnostic_temp_db_boundary_present": diagnostic_temp_db_boundary_present,
        "controlled_posterior_repair_writer_apply_path_present": controlled_posterior_repair_writer_apply_path_present,
        "controlled_posterior_repair_preflight_present": controlled_posterior_repair_preflight_present,
        "controlled_lineage_repair_writer_apply_path_present": controlled_lineage_repair_writer_apply_path_present,
        "controlled_lineage_repair_preflight_present": controlled_lineage_repair_preflight_present,
        "controlled_identity_repair_apply_boundary_present": controlled_identity_repair_apply_boundary_present,
        "attested_latest_repair_boundary_present": attested_latest_repair_boundary_present,
        "read_only_contract_surface_present": read_only_contract_surface_present,
        "isolated_shadow_ssot_writer_boundary_present": isolated_shadow_ssot_writer_boundary_present,
        "production_transaction_b_cutover_candidate_present": production_transaction_b_cutover_candidate_present,
        "production_probability_ssot_writer_active": production_probability_ssot_writer_active,
        "retired_probability_writer_unreachable": retired_probability_writer_unreachable,
        "production_probability_activation_state": production_activation_state,
        "maintenance_retention_policy_wrapper_present": maintenance_retention_policy_wrapper_present,
        "risk_flags": risk_flags,
        "samples": _file_samples(patterns, path, limit=sample_limit),
        "status": status,
    }


def _group_lanes(file_entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    lanes: list[dict[str, Any]] = []
    by_lane: dict[str, list[Mapping[str, Any]]] = {}
    for entry in file_entries:
        by_lane.setdefault(str(entry.get("lane") or "unknown_fact_writer_surface"), []).append(entry)
    for lane in LANE_ORDER:
        lane_def = LANE_DEFINITIONS[lane]
        entries = sorted(by_lane.get(lane) or [], key=lambda item: str(item.get("file") or ""))
        compat_adapter_count = sum(1 for entry in entries if bool(entry.get("compat_adapter_present")))
        alignment_audit_count = sum(1 for entry in entries if bool(entry.get("alignment_audit_present")))
        apply_parity_audit_count = sum(1 for entry in entries if bool(entry.get("apply_parity_audit_present")))
        apply_contract_count = sum(1 for entry in entries if bool(entry.get("apply_contract_present")))
        post_apply_verification_count = sum(
            1 for entry in entries if bool(entry.get("post_apply_verification_present"))
        )
        apply_result_summary_helper_count = sum(
            1 for entry in entries if bool(entry.get("apply_result_summary_helper_present"))
        )
        writer_apply_result_summary_exposed_count = sum(
            1 for entry in entries if bool(entry.get("writer_apply_result_summary_exposed"))
        )
        apply_wrapper_count = sum(1 for entry in entries if bool(entry.get("apply_wrapper_present")))
        materialized_payload_split_count = sum(
            1 for entry in entries if bool(entry.get("materialized_payload_split_present"))
        )
        helper_writer_apply_path_count = sum(
            1 for entry in entries if bool(entry.get("helper_writer_apply_path_present"))
        )
        function_parity_gate_count = sum(
            1 for entry in entries if bool(entry.get("function_parity_gate_present"))
        )
        function_parity_sample_suite_count = sum(
            1 for entry in entries if bool(entry.get("function_parity_sample_suite_present"))
        )
        helper_writer_apply_default_enabled_count = sum(
            1 for entry in entries if bool(entry.get("helper_writer_apply_default_enabled"))
        )
        helper_single_writer_path_count = sum(
            1 for entry in entries if bool(entry.get("helper_single_writer_path_present"))
        )
        hard_source_writer_apply_path_count = sum(
            1 for entry in entries if bool(entry.get("hard_source_writer_apply_path_present"))
        )
        hard_source_writer_apply_default_enabled_count = sum(
            1 for entry in entries if bool(entry.get("hard_source_writer_apply_default_enabled"))
        )
        source_acquisition_archive_apply_writer_boundary_count = sum(
            1 for entry in entries if bool(entry.get("source_acquisition_archive_apply_writer_boundary_present"))
        )
        posterior_materialization_worker_boundary_count = sum(
            1 for entry in entries if bool(entry.get("posterior_materialization_worker_boundary_present"))
        )
        scanner_writer_apply_path_count = sum(
            1 for entry in entries if bool(entry.get("scanner_writer_apply_path_present"))
        )
        scanner_historical_guard_count = sum(
            1 for entry in entries if bool(entry.get("scanner_historical_guard_present"))
        )
        materializer_writer_apply_path_count = sum(
            1
            for entry in entries
            if bool(entry.get("materializer_ctp_writer_apply_path_present"))
            or bool(entry.get("materializer_copy_acceptance_writer_apply_path_present"))
        )
        materializer_historical_guard_count = sum(
            1 for entry in entries if bool(entry.get("materializer_historical_guard_present"))
        )
        materializer_temp_db_guard_count = sum(
            1 for entry in entries if bool(entry.get("materializer_temp_db_guard_present"))
        )
        materializer_archive_rebuild_candidate_copy_guard_count = sum(
            1 for entry in entries if bool(entry.get("materializer_archive_rebuild_candidate_copy_guard_present"))
        )
        diagnostic_writer_apply_path_count = sum(
            1 for entry in entries if bool(entry.get("diagnostic_writer_apply_path_present"))
        )
        diagnostic_temp_db_boundary_count = sum(
            1 for entry in entries if bool(entry.get("diagnostic_temp_db_boundary_present"))
        )
        controlled_posterior_repair_writer_apply_path_count = sum(
            1 for entry in entries if bool(entry.get("controlled_posterior_repair_writer_apply_path_present"))
        )
        controlled_posterior_repair_preflight_count = sum(
            1 for entry in entries if bool(entry.get("controlled_posterior_repair_preflight_present"))
        )
        controlled_lineage_repair_writer_apply_path_count = sum(
            1 for entry in entries if bool(entry.get("controlled_lineage_repair_writer_apply_path_present"))
        )
        controlled_lineage_repair_preflight_count = sum(
            1 for entry in entries if bool(entry.get("controlled_lineage_repair_preflight_present"))
        )
        controlled_identity_repair_apply_boundary_count = sum(
            1
            for entry in entries
            if bool(entry.get("controlled_identity_repair_apply_boundary_present"))
            or bool(entry.get("attested_latest_repair_boundary_present"))
        )
        read_only_contract_surface_count = sum(
            1 for entry in entries if bool(entry.get("read_only_contract_surface_present"))
        )
        isolated_shadow_ssot_writer_boundary_count = sum(
            1 for entry in entries if bool(entry.get("isolated_shadow_ssot_writer_boundary_present"))
        )
        production_cutover_candidate_count = sum(
            1 for entry in entries if bool(entry.get("production_transaction_b_cutover_candidate_present"))
        )
        production_ssot_writer_active_count = sum(
            1 for entry in entries if bool(entry.get("production_probability_ssot_writer_active"))
        )
        retired_probability_writer_unreachable_count = sum(
            1 for entry in entries if bool(entry.get("retired_probability_writer_unreachable"))
        )
        maintenance_retention_policy_wrapper_count = sum(
            1 for entry in entries if bool(entry.get("maintenance_retention_policy_wrapper_present"))
        )
        materializer_guard_count = (
            materializer_historical_guard_count
            + materializer_temp_db_guard_count
            + materializer_archive_rebuild_candidate_copy_guard_count
        )
        materializer_coverage_count = materializer_writer_apply_path_count + materializer_archive_rebuild_candidate_copy_guard_count
        status = "not_present"
        if entries:
            if lane == "target_owner_boundary":
                status = "target_owner_apply_contract_present" if apply_contract_count else "target_owner_ready"
            elif (
                lane == "legacy_scanner_strategy"
                and scanner_writer_apply_path_count == len(entries)
                and scanner_historical_guard_count == len(entries)
            ):
                status = "legacy_scanner_q_calibrated_writer_apply_path_present"
            elif lane == "legacy_scanner_strategy" and scanner_writer_apply_path_count == len(entries):
                status = "legacy_scanner_writer_apply_path_present_pending_historical_guard"
            elif lane == "hard_source_realtime" and hard_source_writer_apply_default_enabled_count == len(entries):
                status = "hard_source_realtime_writer_apply_default_present"
            elif lane == "hard_source_realtime" and hard_source_writer_apply_path_count == len(entries):
                status = "hard_source_realtime_writer_apply_paths_present"
            elif lane == "hard_source_realtime" and hard_source_writer_apply_default_enabled_count:
                status = "hard_source_realtime_writer_apply_default_partially_present"
            elif lane == "hard_source_realtime" and hard_source_writer_apply_path_count:
                status = "hard_source_realtime_writer_apply_path_present_pending_default_switch"
            elif (
                lane == "source_acquisition_archive_apply"
                and source_acquisition_archive_apply_writer_boundary_count == len(entries)
            ):
                status = "source_acquisition_archive_apply_writer_boundary_present"
            elif (
                lane == "posterior_materialization_worker"
                and posterior_materialization_worker_boundary_count == len(entries)
            ):
                status = "posterior_materialization_worker_boundary_present"
            elif (
                lane == "materializer_rebuild_backfill"
                and materializer_coverage_count == len(entries)
                and materializer_guard_count == len(entries)
            ):
                status = "materializer_rebuild_writer_apply_or_candidate_copy_paths_present"
            elif lane == "materializer_rebuild_backfill" and materializer_guard_count:
                status = "materializer_rebuild_writer_apply_path_partially_present"
            elif lane == "materializer_rebuild_backfill" and materializer_coverage_count:
                status = "materializer_rebuild_writer_apply_path_present_pending_historical_guard"
            elif (
                lane == "diagnostic_repair"
                and diagnostic_writer_apply_path_count == len(entries)
                and diagnostic_temp_db_boundary_count == len(entries)
            ):
                status = "diagnostic_repair_temporary_drill_writer_apply_boundary_present"
            elif lane == "diagnostic_repair" and diagnostic_writer_apply_path_count:
                status = "diagnostic_repair_writer_apply_path_present_pending_temp_db_boundary"
            elif (
                lane == "controlled_posterior_repair"
                and controlled_posterior_repair_writer_apply_path_count == len(entries)
                and controlled_posterior_repair_preflight_count == len(entries)
            ):
                status = "controlled_posterior_repair_writer_apply_boundary_present"
            elif lane == "controlled_posterior_repair" and controlled_posterior_repair_writer_apply_path_count:
                status = "controlled_posterior_repair_writer_apply_path_present_pending_preflight"
            elif (
                lane == "controlled_lineage_repair"
                and controlled_lineage_repair_writer_apply_path_count == len(entries)
                and controlled_lineage_repair_preflight_count == len(entries)
            ):
                status = "controlled_lineage_repair_writer_apply_boundary_present"
            elif lane == "controlled_lineage_repair" and controlled_lineage_repair_writer_apply_path_count:
                status = "controlled_lineage_repair_writer_apply_path_present_pending_preflight"
            elif (
                lane == "controlled_identity_repair"
                and controlled_identity_repair_apply_boundary_count == len(entries)
            ):
                status = "controlled_identity_repair_apply_boundary_present"
            elif lane == "controlled_identity_repair" and controlled_identity_repair_apply_boundary_count:
                status = "controlled_identity_repair_apply_boundary_partially_present"
            elif (
                lane == "read_only_contract_surface"
                and read_only_contract_surface_count == len(entries)
            ):
                status = "read_only_contract_surface_no_apply_boundary_present"
            elif (
                lane == "isolated_shadow_ssot_writer"
                and isolated_shadow_ssot_writer_boundary_count == len(entries)
            ):
                status = "isolated_shadow_ssot_writer_manifest_boundary_present"
            elif (
                lane == "production_transaction_b_cutover_candidate"
                and production_ssot_writer_active_count == len(entries)
            ):
                status = "production_probability_ssot_writer_partition_active"
            elif (
                lane == "production_transaction_b_cutover_candidate"
                and production_cutover_candidate_count == len(entries)
            ):
                status = "production_transaction_b_cutover_candidate_default_disabled"
            elif (
                lane == "retired_probability_writer_surface"
                and retired_probability_writer_unreachable_count == len(entries)
            ):
                status = "retired_probability_writer_unreachable_from_active_epoch"
            elif (
                lane == "maintenance_cleanup"
                and maintenance_retention_policy_wrapper_count == len(entries)
            ):
                status = "maintenance_cleanup_retention_policy_wrapper_present"
            elif (
                lane == "ui_current_helper"
                and helper_single_writer_path_count == len(entries)
                and compat_adapter_count
                and alignment_audit_count
                and apply_wrapper_count
                and apply_parity_audit_count
                and materialized_payload_split_count
                and helper_writer_apply_path_count
                and function_parity_gate_count
                and function_parity_sample_suite_count
            ):
                status = "ui_current_helper_single_writer_path_present"
            elif (
                compat_adapter_count
                and alignment_audit_count
                and apply_wrapper_count
                and apply_parity_audit_count
                and materialized_payload_split_count
                and helper_writer_apply_path_count
                and function_parity_gate_count
                and function_parity_sample_suite_count
                and helper_writer_apply_default_enabled_count
            ):
                status = "compat_adapter_alignment_apply_wrapper_parity_split_writer_apply_path_function_gate_sample_suite_and_default_switch_present"
            elif (
                compat_adapter_count
                and alignment_audit_count
                and apply_wrapper_count
                and apply_parity_audit_count
                and materialized_payload_split_count
                and helper_writer_apply_path_count
                and function_parity_gate_count
                and function_parity_sample_suite_count
            ):
                status = "compat_adapter_alignment_apply_wrapper_parity_split_writer_apply_path_function_gate_and_sample_suite_present_pending_default_switch"
            elif (
                compat_adapter_count
                and alignment_audit_count
                and apply_wrapper_count
                and apply_parity_audit_count
                and materialized_payload_split_count
                and helper_writer_apply_path_count
                and function_parity_gate_count
            ):
                status = "compat_adapter_alignment_apply_wrapper_parity_split_writer_apply_path_and_function_gate_present_pending_default_switch"
            elif (
                compat_adapter_count
                and alignment_audit_count
                and apply_wrapper_count
                and apply_parity_audit_count
                and materialized_payload_split_count
                and helper_writer_apply_path_count
            ):
                status = "compat_adapter_alignment_apply_wrapper_parity_split_and_writer_apply_path_present_pending_default_switch"
            elif (
                compat_adapter_count
                and alignment_audit_count
                and apply_wrapper_count
                and apply_parity_audit_count
                and materialized_payload_split_count
            ):
                status = "compat_adapter_alignment_apply_wrapper_parity_and_materialized_split_present_pending_helper_switch"
            elif compat_adapter_count and alignment_audit_count and apply_wrapper_count and apply_parity_audit_count:
                status = "compat_adapter_alignment_apply_wrapper_and_parity_audit_present_pending_helper_switch"
            elif compat_adapter_count and alignment_audit_count and apply_wrapper_count:
                status = "compat_adapter_alignment_audit_and_apply_wrapper_present_pending_helper_switch"
            elif compat_adapter_count and alignment_audit_count:
                status = "compat_adapter_and_alignment_audit_present_pending_apply_migration"
            elif compat_adapter_count:
                status = "compat_adapter_present_pending_apply_migration"
            else:
                status = "pending_migration"
        lanes.append(
            {
                "lane": lane,
                "migration_order": lane_def["migration_order"],
                "owner": lane_def["owner"],
                "intent": lane_def["intent"],
                "target_action": lane_def["target_action"],
                "requires_explicit_apply_window": bool(lane_def["requires_explicit_apply_window"]),
                "file_count": len(entries),
                "files": [str(entry.get("file") or "") for entry in entries],
                "compat_adapter_count": compat_adapter_count,
                "alignment_audit_count": alignment_audit_count,
                "apply_parity_audit_count": apply_parity_audit_count,
                "apply_contract_count": apply_contract_count,
                "post_apply_verification_count": post_apply_verification_count,
                "apply_result_summary_helper_count": apply_result_summary_helper_count,
                "writer_apply_result_summary_exposed_count": writer_apply_result_summary_exposed_count,
                "apply_wrapper_count": apply_wrapper_count,
                "materialized_payload_split_count": materialized_payload_split_count,
                "helper_writer_apply_path_count": helper_writer_apply_path_count,
                "function_parity_gate_count": function_parity_gate_count,
                "function_parity_sample_suite_count": function_parity_sample_suite_count,
                "helper_writer_apply_default_enabled_count": helper_writer_apply_default_enabled_count,
                "helper_single_writer_path_count": helper_single_writer_path_count,
                "hard_source_writer_apply_path_count": hard_source_writer_apply_path_count,
                "hard_source_writer_apply_default_enabled_count": hard_source_writer_apply_default_enabled_count,
                "source_acquisition_archive_apply_writer_boundary_count": source_acquisition_archive_apply_writer_boundary_count,
                "posterior_materialization_worker_boundary_count": posterior_materialization_worker_boundary_count,
                "scanner_writer_apply_path_count": scanner_writer_apply_path_count,
                "scanner_historical_guard_count": scanner_historical_guard_count,
                "materializer_writer_apply_path_count": materializer_writer_apply_path_count,
                "materializer_historical_guard_count": materializer_historical_guard_count,
                "materializer_temp_db_guard_count": materializer_temp_db_guard_count,
                "materializer_archive_rebuild_candidate_copy_guard_count": materializer_archive_rebuild_candidate_copy_guard_count,
                "materializer_guard_count": materializer_guard_count,
                "materializer_coverage_count": materializer_coverage_count,
                "diagnostic_writer_apply_path_count": diagnostic_writer_apply_path_count,
                "diagnostic_temp_db_boundary_count": diagnostic_temp_db_boundary_count,
                "controlled_posterior_repair_writer_apply_path_count": controlled_posterior_repair_writer_apply_path_count,
                "controlled_posterior_repair_preflight_count": controlled_posterior_repair_preflight_count,
                "controlled_lineage_repair_writer_apply_path_count": controlled_lineage_repair_writer_apply_path_count,
                "controlled_lineage_repair_preflight_count": controlled_lineage_repair_preflight_count,
                "controlled_identity_repair_apply_boundary_count": controlled_identity_repair_apply_boundary_count,
                "read_only_contract_surface_count": read_only_contract_surface_count,
                "isolated_shadow_ssot_writer_boundary_count": isolated_shadow_ssot_writer_boundary_count,
                "production_cutover_candidate_count": production_cutover_candidate_count,
                "production_ssot_writer_active_count": production_ssot_writer_active_count,
                "maintenance_retention_policy_wrapper_count": maintenance_retention_policy_wrapper_count,
                "status": status,
            }
        )
    return lanes


def _migration_steps(lanes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for lane in lanes:
        if int(lane.get("file_count") or 0) <= 0:
            continue
        lane_id = str(lane.get("lane") or "")
        if lane_id == "target_owner_boundary":
            status = "completed_apply_contract_present" if int(lane.get("apply_contract_count") or 0) else "completed_no_apply_boundary_present"
        elif str(lane.get("status") or "") in COMPLETED_LANE_STATUSES:
            status = "completed_writer_path_present"
        else:
            status = "pending"
        steps.append(
            {
                "order": lane.get("migration_order"),
                "lane": lane_id,
                "status": status,
                "files": lane.get("files") or [],
                "target_action": lane.get("target_action") or "",
                "must_preserve": [
                    "append_event_fact_history_before_latest_repoint",
                    "merge_fill_never_overwrite_stronger_fact",
                    "source_event_id_fact_snapshot_id_posterior_run_id_lineage",
                    "no_history_backfill_without_explicit_confirmation",
                    "no_order_or_decision_side_effect",
                ],
            }
        )
    return sorted(steps, key=lambda item: int(item.get("order") or 99))


def _next_actions(lanes: Sequence[Mapping[str, Any]]) -> list[str]:
    by_lane = {str(lane.get("lane") or ""): lane for lane in lanes}
    present = {lane: int(row.get("file_count") or 0) for lane, row in by_lane.items()}
    actions: list[str] = []
    ui_status = str((by_lane.get("ui_current_helper") or {}).get("status") or "")
    if (
        present.get("ui_current_helper")
        and "default_switch_present" not in ui_status
        and ui_status != "ui_current_helper_single_writer_path_present"
    ):
        actions.append("migrate_ui_server_fact_helpers_to_weather_fact_snapshot_writer")
    hard_status = str((by_lane.get("hard_source_realtime") or {}).get("status") or "")
    if present.get("hard_source_realtime") and hard_status != "hard_source_realtime_writer_apply_default_present":
        actions.append("migrate_hard_source_realtime_fact_writes_to_writer")
    source_acquisition_status = str((by_lane.get("source_acquisition_archive_apply") or {}).get("status") or "")
    if (
        present.get("source_acquisition_archive_apply")
        and source_acquisition_status != "source_acquisition_archive_apply_writer_boundary_present"
    ):
        actions.append("route_source_acquisition_archive_apply_through_writer_source_only_history")
    posterior_materialization_status = str((by_lane.get("posterior_materialization_worker") or {}).get("status") or "")
    if (
        present.get("posterior_materialization_worker")
        and posterior_materialization_status != "posterior_materialization_worker_boundary_present"
    ):
        actions.append("route_posterior_materialization_worker_through_canonical_writer_current_event_boundary")
    scanner_status = str((by_lane.get("legacy_scanner_strategy") or {}).get("status") or "")
    if present.get("legacy_scanner_strategy") and scanner_status != "legacy_scanner_q_calibrated_writer_apply_path_present":
        actions.append("retire_or_route_legacy_scanner_fact_updates")
    materializer_status = str((by_lane.get("materializer_rebuild_backfill") or {}).get("status") or "")
    if (
        present.get("materializer_rebuild_backfill")
        and materializer_status != "materializer_rebuild_writer_apply_or_candidate_copy_paths_present"
    ):
        actions.append("convert_remaining_materializer_rebuild_scripts_to_batch_writer_plan")
    diagnostic_status = str((by_lane.get("diagnostic_repair") or {}).get("status") or "")
    if (
        present.get("diagnostic_repair")
        and diagnostic_status != "diagnostic_repair_temporary_drill_writer_apply_boundary_present"
    ):
        actions.append("make_diagnostic_fact_repairs_no_apply_by_default")
    posterior_repair_status = str((by_lane.get("controlled_posterior_repair") or {}).get("status") or "")
    if (
        present.get("controlled_posterior_repair")
        and posterior_repair_status != "controlled_posterior_repair_writer_apply_boundary_present"
    ):
        actions.append("route_posterior_ssot_repair_apply_through_controlled_writer_boundary")
    lineage_status = str((by_lane.get("controlled_lineage_repair") or {}).get("status") or "")
    if (
        present.get("controlled_lineage_repair")
        and lineage_status != "controlled_lineage_repair_writer_apply_boundary_present"
    ):
        actions.append("route_lineage_repair_apply_through_weather_fact_snapshot_writer")
    identity_status = str((by_lane.get("controlled_identity_repair") or {}).get("status") or "")
    if (
        present.get("controlled_identity_repair")
        and identity_status != "controlled_identity_repair_apply_boundary_present"
    ):
        actions.append("classify_identity_repair_as_controlled_maintenance_window_apply")
    read_only_status = str((by_lane.get("read_only_contract_surface") or {}).get("status") or "")
    if (
        present.get("read_only_contract_surface")
        and read_only_status != "read_only_contract_surface_no_apply_boundary_present"
    ):
        actions.append("mark_contract_and_schema_probe_surfaces_read_only_no_apply")
    maintenance_status = str((by_lane.get("maintenance_cleanup") or {}).get("status") or "")
    if (
        present.get("maintenance_cleanup")
        and maintenance_status != "maintenance_cleanup_retention_policy_wrapper_present"
    ):
        actions.append("move_cleanup_under_explicit_retention_policy")
    if present.get("unknown_fact_writer_surface"):
        actions.append("classify_unknown_fact_write_surfaces_before_migration")
    if actions:
        actions.append("rerun_phase0_ssot_inventory_until_fact_snapshot_surface_has_single_owner")
    return actions


def build_fact_write_migration_plan(
    root: Path,
    *,
    db_path: Path | None = None,
    immutable: bool = False,
    sample_limit: int = 5,
    prebuilt_inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    inventory = (
        dict(prebuilt_inventory)
        if isinstance(prebuilt_inventory, Mapping)
        else phase0_ssot_inventory.build_inventory(
            Path(root),
            db_path=db_path,
            immutable=immutable,
            max_samples_per_pattern=max(1, int(sample_limit or 1)),
        )
    )
    files = set(_files_from_inventory(inventory))
    for known_policy_file in ("cleanup_data.sh",):
        if (Path(root) / known_policy_file).exists():
            files.add(known_policy_file)
    files = sorted(files)
    file_entries = [
        _build_file_entry(inventory, path, root=Path(root), sample_limit=max(0, int(sample_limit or 0)))
        for path in files
    ]
    lanes = _group_lanes(file_entries)
    non_target_pending = [
        entry for entry in file_entries
        if str(entry.get("lane") or "") != "target_owner_boundary"
        and str(entry.get("status") or "") not in COMPLETED_LANE_STATUSES
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "execution_mode": "read_only_fact_write_migration_plan",
        "root": str(Path(root).expanduser().resolve()),
        "guards": _guards(),
        "target_owner": TARGET_OWNER,
        "inventory_schema": inventory.get("schema"),
        "inventory_scan": inventory.get("scan"),
        "current_fact_writer_files": [entry["file"] for entry in file_entries],
        "pending_migration_file_count": len(non_target_pending),
        "lanes": lanes,
        "file_entries": sorted(file_entries, key=lambda item: (int(item.get("migration_order") or 99), str(item.get("file") or ""))),
        "migration_steps": _migration_steps(lanes),
        "next_actions": _next_actions(lanes),
        "blocked_operations": [
            "execute_sql",
            "create_backup",
            "historical_backfill",
            "repoint_event_fact_latest",
            "append_event_fact_history",
            "place_order",
            "restart_launchd",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only migration plan for fact write surfaces.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or phase0_schema_probe.DEFAULT_DB))
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_fact_write_migration_plan(
        Path(args.root),
        db_path=None if args.no_db else Path(args.db),
        immutable=bool(args.immutable),
        sample_limit=max(0, int(args.sample_limit or 0)),
    )
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "guards": report["guards"],
            "target_owner": report["target_owner"],
            "pending_migration_file_count": report["pending_migration_file_count"],
            "lanes": report["lanes"],
            "next_actions": report["next_actions"],
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
