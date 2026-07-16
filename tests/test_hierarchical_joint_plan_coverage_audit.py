from pathlib import Path
import sqlite3

from scripts import hierarchical_joint_plan_coverage_audit as audit


def _core_report(*, ok=True):
    return {
        "schema": "hierarchical_probability_release_gate_v1",
        "ok": ok,
        "read_only": True,
        "checks": {
            "prior_gate": ok,
            "likelihood_gate": ok,
            "posterior_gate": ok,
            "posterior_ssot_gate": ok,
            "ui_snapshot_lineage_gate": ok,
            "ssot_chain_replay_gate": ok,
        },
        "blockers": [] if ok else [{"blocker": "posterior_ssot_gate_failed"}],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _core_integration_report(*, ok=True):
    return {
        "schema": "hierarchical_core_integration_contract_audit_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "hierarchical_core_integration_contract_audit_read_only",
        "checks": {
            "source_to_fact_writer_plan": ok,
            "production_posterior_output_contract": ok,
            "ui_snapshot_reads_posterior_ssot": ok,
            "auto_trade_candidate_reads_posterior_ssot": ok,
            "fact_snapshot_to_daily_review_to_calibration": ok,
        },
        "blockers": [] if ok else ["auto_trade_candidate_details_target_matches_ssot_failed"],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "fetches_external_weather_sources": False,
            "fetches_orderbook": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _replay_contract(*, missing_key: str = ""):
    required = set(audit.AWC_SLO_REQUIRED_KEYS)
    if missing_key:
        required.discard(missing_key)
    return {
        "schema": "ssot_chain_replay_input_contract_v1",
        "read_only": True,
        "awc_signal_to_order_slo": {
            "start_field": "awc_hard_source_signal_at",
            "end_field": "order_submitted_at",
            "slo_ms": 3000,
            "terminal_states": ["submitted", "blocked"],
            "required_timing_keys": sorted(required),
        },
        "missing_policy": {
            "signal_to_order_timing_table_missing": "coverage_blocker",
            "accepted_signal_missing_signal_to_order_ledger": "coverage_blocker",
            "submitted_missing_orderbook_check": "blocker",
            "submitted_missing_risk_check": "blocker",
            "submitted_missing_idempotency_check": "blocker",
            "submitted_precheck_after_submit_request": "blocker",
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _replay_report(*, ok=True, signal_count=1, blockers=None):
    return {
        "schema": "ssot_chain_replay_audit_v1",
        "ok": ok,
        "read_only": True,
        "signal_count": signal_count,
        "source_counts": {"signal_to_order_timing": signal_count} if signal_count else {},
        "production_coverage": {
            "schema": "signal_to_order_production_coverage_v1",
            "ok": ok,
            "accepted_signal_count": signal_count,
            "submitted_signal_count": signal_count if ok else 0,
        },
        "blockers": list(blockers or ([] if ok else ["accepted_signal_base_timing_incomplete"])),
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _daily_review_report(*, ok=True):
    return {
        "schema": "daily_review_post_apply_acceptance_audit_v1",
        "ok": ok,
        "read_only": True,
        "daily_weather_reviews_row_count": 1 if ok else 0,
        "coverage": {
            "settled_event_count": 1,
            "reviewed_event_count": 1 if ok else 0,
            "missing_review_count": 0 if ok else 1,
            "coverage_ratio": 1.0 if ok else 0.0,
        },
        "gates": [
            {
                "gate": "daily_review_coverage_gate",
                "ok": ok,
                "blockers": [] if ok else ["daily_weather_reviews_missing_planned_table"],
            }
        ],
        "blockers": [] if ok else ["daily_weather_reviews_missing_planned_table"],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _phase0_schema_report(*, ok=True, materialized=False):
    return {
        "schema": "phase0_schema_apply_contract_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "phase0_schema_apply_contract_read_only_gate",
        "contract_schema": "phase0_schema_apply_contract_v1",
        "probe_schema": "phase0_schema_probe_v1",
        "schema_surface_ready": materialized,
        "phase0_ready": materialized,
        "daily_review_tables_materialized": materialized,
        "planned_table_status": {
            table: {
                "exists": materialized,
                "ddl_planned": not materialized,
                "missing_required_columns": [] if materialized else ["review_id"],
            }
            for table in audit.PHASE0_REQUIRED_DAILY_REVIEW_TABLES
        },
        "ddl_summary": {
            "ddl_count": 4 if not materialized else 0,
            "affected_tables": list(audit.PHASE0_REQUIRED_DAILY_REVIEW_TABLES),
        },
        "expected_apply_blockers": sorted(audit.PHASE0_EXPECTED_APPLY_BLOCKERS),
        "apply_blockers_without_confirmation": sorted(audit.PHASE0_EXPECTED_APPLY_BLOCKERS),
        "unexpected_apply_blockers": [],
        "missing_post_apply_verification_fragments": [],
        "blockers": [] if ok else ["daily_weather_reviews_missing_without_create_table_ddl"],
        "warnings": [] if materialized else ["daily_weather_reviews_not_materialized_in_archive_db"],
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _signal_to_order_safety_report(*, ok=True):
    return {
        "schema": "signal_to_order_safety_contract_audit_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "signal_to_order_safety_contract_audit_read_only",
        "checks": {
            "stale_decision_blocks": ok,
            "stale_posterior_blocks": ok,
            "stale_orderbook_blocks": ok,
            "duplicate_order_blocks": ok,
            "stale_terminal_does_not_submit": ok,
            "idempotency_contract_present": ok,
        },
        "blockers": [] if ok else ["stale_posterior_does_not_classify_as_stale"],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_auto_orders": False,
            "writes_signal_to_order_timing": False,
            "places_orders": False,
            "fetches_orderbook": False,
            "fetches_external_weather_sources": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _order_lineage_coverage_report(*, ok=True):
    return {
        "schema": "order_lineage_coverage_audit_v1",
        "ok": ok,
        "read_only": True,
        "total_order_count": 1,
        "missing_by_column": {
            "order_id": 0,
            "correlation_id": 0,
            "decision_id": 0 if ok else 1,
            "signal_id": 0 if ok else 1,
            "order_intent_id": 0,
        },
        "blockers": [] if ok else [
            "auto_orders_decision_id_missing",
            "auto_orders_signal_id_missing",
        ],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "executes_sql_writes": False,
            "creates_backup": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _awc_signal_timing_report(*, ok=True):
    return {
        "schema": "awc_signal_timing_contract_audit_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "awc_signal_timing_contract_audit_read_only",
        "checks": {
            "contract_has_awc_valid_time": ok,
            "contract_has_signal_detection_time": ok,
            "contract_has_ssot_times": ok,
            "contract_has_decision_job_times": ok,
            "contract_has_order_times": ok,
            "missing_policy_covers_aggregate_blockers": ok,
            "full_chain_fixture_passes": ok,
            "missing_awc_valid_time_blocks": ok,
            "missing_ssot_materialization_time_blocks": ok,
            "missing_decision_time_blocks": ok,
            "missing_job_time_blocks": ok,
            "missing_order_execution_time_blocks": ok,
        },
        "blockers": [] if ok else ["missing_awc_valid_time_not_blocked"],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_auto_orders": False,
            "writes_signal_to_order_timing": False,
            "fetches_external_weather_sources": False,
            "fetches_orderbook": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _awc_terminal_coverage_report(*, ok=True):
    return {
        "schema": "awc_hard_source_terminal_coverage_audit_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "awc_hard_source_terminal_coverage_read_only",
        "accepted_signal_count": 1,
        "accepted_signal_ids": ["sig_awc"],
        "submitted_signal_count": 1 if ok else 0,
        "terminal_state_counts": {"submitted": 1} if ok else {"job_enqueued": 1},
        "non_final_terminal_signal_ids": [] if ok else ["sig_awc"],
        "missing_terminal_signal_ids": [],
        "missing_terminal_reason_signal_ids": [],
        "missing_ledger_signal_ids": [] if ok else ["sig_awc"],
        "coverage_blockers": [] if ok else [
            "accepted_signal_terminal_coverage_incomplete",
            "accepted_signal_missing_signal_to_order_ledger",
        ],
        "replay_blockers": [] if ok else ["job_without_auto_trade_decision"],
        "blockers": [] if ok else [
            "accepted_signal_missing_signal_to_order_ledger",
            "accepted_signal_terminal_coverage_incomplete",
            "job_without_auto_trade_decision",
        ],
        "warnings": [] if ok else ["legacy_accepted_signal_terminal_non_final"],
        "guards": {
            "writes_archive_db": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_signal_to_order_timing": False,
            "fetches_external_weather_sources": False,
            "fetches_orderbook": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _awc_terminal_closeout_report(*, ok=False):
    return {
        "schema": "awc_legacy_terminal_closeout_decision_packet_v1_summary",
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "awc_legacy_terminal_closeout_decision_packet_summary_read_only",
        "candidate_count": 2,
        "recommended_materialize_count": 1,
        "operator_review_required_count": 1,
        "manifest_provided": ok,
        "operation_count": 2 if ok else 0,
        "hard_blockers": [] if ok else [
            "awc_legacy_terminal_closeout_manifest_missing",
            "awc_legacy_terminal_closeout_decision_pending",
        ],
        "guards": {
            "writes_archive_db": False,
            "writes_awc_hard_source_signals": False,
            "writes_signal_to_order_timing": False,
            "places_orders": False,
            "fetches_external_weather_sources": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "awc_legacy_terminal_closeout_apply_contract_ready"
            if ok
            else "review_awc_legacy_terminal_closeout_manifest_before_apply_contract"
        ),
    }


def _awc_terminal_apply_contract_report(*, ok=False):
    return {
        "schema": "awc_legacy_terminal_closeout_apply_contract_v1_summary",
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "awc_legacy_terminal_closeout_apply_contract_no_apply",
        "operation_count": 2 if ok else 0,
        "blockers": [] if ok else [
            "awc_legacy_terminal_closeout_packet_not_ready",
            "decision_manifest_required",
        ],
        "operator_contract": {
            "schema": "awc_legacy_terminal_closeout_apply_operator_contract_v1",
            "confirmation_phrase": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "allow_apply_now": False,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_awc_hard_source_signals": False,
            "writes_signal_to_order_timing": False,
            "writes_manifest_file": False,
            "executes_sql_writes": False,
            "creates_backup": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "run_awc_legacy_terminal_closeout_apply_after_backup_writer_quiescence_and_confirmation"
            if ok
            else "resolve_awc_legacy_terminal_closeout_apply_contract_blockers"
        ),
    }


def _awc_terminal_operator_runbook_report(*, ok=False):
    return {
        "schema": "awc_legacy_terminal_closeout_operator_runbook_v1_summary",
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "awc_legacy_terminal_closeout_operator_runbook_summary_no_apply",
        "ready_for_controlled_apply_now": ok,
        "start_blockers": [] if ok else [
            "awc_legacy_terminal_closeout_decision_pending",
            "writer_quiescence_not_satisfied",
        ],
        "artifacts": {
            "review_report_path": "/tmp/archive.db.awc-legacy-terminal-closeout.review-report.json",
            "decision_manifest_path": "/tmp/archive.db.awc-legacy-terminal-closeout.decision-manifest.json",
            "shared_backup_path": "/tmp/archive.db.awc-legacy-terminal-closeout.bak",
        },
        "awc_terminal_scope": {
            "candidate_count": 2,
            "contract_operation_count": 2 if ok else 0,
        },
        "impact_summary": {
            "review_report_steps": ["emit_review_report"],
            "archive_db_apply_steps": ["apply_awc_legacy_terminal_closeout"],
            "manifest_write_steps": ["emit_decision_manifest_template", "operator_reviews_decision_manifest"],
        },
        "confirmation_phrases": {
            "apply_awc_legacy_terminal_closeout": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
        },
        "guards": {
            "writes_archive_db": False,
            "writes_awc_hard_source_signals": False,
            "writes_signal_to_order_timing": False,
            "writes_manifest_file": False,
            "executes_sql_writes": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": (
            "run_awc_legacy_terminal_closeout_operator_sequence"
            if ok
            else "review_awc_terminal_manifest_and_satisfy_writer_quiescence_before_apply"
        ),
    }


def _production_identity_report(*, ok=True):
    return {
        "schema": "production_identity_coverage_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "production_identity_coverage_read_only_gate",
        "source_schema": "production_identity_coverage_audit_v1",
        "schema_identity_ready": ok,
        "production_row_identity_ready": ok,
        "table_count": 12,
        "ready_table_count": 12 if ok else 8,
        "gap_table_count": 0 if ok else 4,
        "gap_tables": [] if ok else ["event_fact_history", "auto_trade_decisions"],
        "tables": {},
        "blockers": [] if ok else ["production_identity_rows_not_ready"],
        "warnings": [],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _production_identity_duplicate_contract_report(*, ok=True, duplicate_row_count=0):
    return {
        "schema": "production_identity_duplicate_apply_contract_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "production_identity_duplicate_apply_contract_read_only_gate",
        "source_schema": "production_identity_duplicate_fact_snapshot_apply_contract_v1_summary",
        "duplicate_group_count": 0 if duplicate_row_count == 0 else 2,
        "duplicate_row_count": duplicate_row_count,
        "operation_count": 0 if not ok else duplicate_row_count,
        "decision_manifest_audit": {
            "manifest_provided": ok,
            "expected_decision_count": duplicate_row_count,
            "ready_decision_count": duplicate_row_count if ok else 0,
            "blocking_for_current_apply": not ok,
        },
        "operator_contract": {
            "confirmation_phrase": "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR",
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "post_apply_verification_commands": [
                "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py --summary",
                "python3 scripts/production_identity_merge_fill_plan.py --summary",
                "python3 scripts/production_identity_coverage_audit.py --summary",
            ],
        },
        "blockers": [] if ok else ["duplicate_decision_manifest_required"],
        "warnings": [] if ok else ["review_duplicate_decision_manifest_before_identity_merge_fill"],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _production_identity_merge_fill_contract_report(*, ok=True, operation_count=0):
    return {
        "schema": "production_identity_merge_fill_apply_contract_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "production_identity_merge_fill_apply_contract_read_only_gate",
        "source_schema": "production_identity_merge_fill_apply_contract_v1_summary",
        "operation_count": operation_count,
        "candidate_count": operation_count,
        "safe_candidate_count": operation_count if ok else 0,
        "blocked_candidate_count": 0 if ok else operation_count,
        "duplicate_fact_snapshot_precondition": {"ok": ok, "duplicate_row_count": 0},
        "operator_contract": {
            "confirmation_phrase": "APPLY_PRODUCTION_IDENTITY_MERGE_FILL",
            "requires_backup": True,
            "requires_writer_quiescence": True,
        },
        "blockers": [] if ok else ["merge_fill_plan_not_ready"],
        "warnings": [] if ok else ["run_duplicate_repair_then_review_identity_merge_fill_contract"],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_auto_orders": False,
            "writes_source_observations": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _production_identity_closeout_packet_report(*, ok=True):
    return {
        "schema": "production_identity_closeout_confirmation_packet_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "production_identity_closeout_confirmation_packet_read_only_gate",
        "packet_schema": "production_identity_closeout_confirmation_packet_v1",
        "shared_backup_path": "/tmp/archive.db.production-identity-closeout.bak",
        "duplicate_manifest_path": "/tmp/archive.db.production-identity-closeout.duplicate-manifest.json",
        "duplicate": {
            "group_count": 0,
            "row_count": 0,
            "reviewed_apply_contract_ok": True,
            "reviewed_apply_operation_count": 0,
            "reviewed_apply_blockers": [],
        },
        "identity_audit_indexes": {
            "contract_ok": True,
            "already_satisfied": False,
            "needs_apply": True,
            "ddl_count": 1,
            "contract_blocker_count": 0,
            "blockers": [],
            "apply_blockers": [],
        },
        "merge_fill": {
            "contract_ok": True,
            "contract_stage": "pre_duplicate_repair_diagnostic",
            "expected_to_be_ready_after_duplicate_repair_apply": True,
            "operation_count": 0,
            "candidate_count": 0,
            "safe_candidate_count": 0,
            "blocked_candidate_count": 0,
            "blockers": [],
        },
        "maintenance_window": {
            "ok": False,
            "writer_quiescent": False,
            "required_stop_labels": ["system/com.weather.monitor.daemon"],
        },
        "missing_required_steps": [],
        "missing_command_fragments": [],
        "hard_blockers": ["writer_quiescence_not_satisfied"],
        "blockers": [] if ok else ["production_identity_closeout_packet_commands_incomplete"],
        "warnings": ["production_identity_closeout_operator_pending:writer_quiescence_not_satisfied"],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_auto_orders": False,
            "writes_source_observations": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _production_identity_closeout_operator_runbook_report(*, ok=True):
    return {
        "schema": "production_identity_closeout_operator_runbook_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "production_identity_closeout_operator_runbook_read_only_gate",
        "runbook_schema": "production_identity_closeout_operator_runbook_v1",
        "ready_for_controlled_apply_now": False,
        "start_blockers": ["writer_quiescence_not_satisfied"],
        "maintenance_window": {
            "writer_quiescent": False,
            "required_stop_labels": ["system/com.weather.monitor.daemon"],
        },
        "artifacts": {
            "shared_backup_path": "/tmp/archive.db.production-identity-closeout.bak",
            "duplicate_manifest_path": "/tmp/archive.db.production-identity-closeout.duplicate-manifest.json",
            "latest_anchor_policy": "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor",
        },
        "identity_scope": {
            "duplicate_row_count": 0,
            "duplicate_apply_operation_count": 0,
            "identity_audit_index_ddl_count": 1,
            "identity_audit_index_needs_apply": True,
            "merge_fill_operation_count": 0,
            "merge_fill_expected_ready_after_duplicate_repair_apply": True,
        },
        "impact_summary": {
            "archive_db_apply_steps": [
                "apply_identity_audit_indexes",
                "apply_duplicate_fact_snapshot_repair",
                "apply_identity_merge_fill",
            ],
            "manifest_write_steps": ["materialize_reviewed_duplicate_manifest"],
            "backup_steps": ["create_shared_fresh_backup"],
            "service_control_steps": ["pause_writer_services", "resume_writer_services"],
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "changes_parameters": False,
            "historical_backfill": False,
        },
        "missing_required_steps": [],
        "missing_command_fragments": [],
        "blockers": [] if ok else ["production_identity_closeout_operator_runbook_commands_incomplete"],
        "warnings": ["production_identity_closeout_runbook_start_blocker:writer_quiescence_not_satisfied"],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_auto_orders": False,
            "writes_source_observations": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _hierarchical_joint_closeout_operator_runbook_report(*, ok=True):
    return {
        "schema": "hierarchical_joint_closeout_operator_runbook_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "hierarchical_joint_closeout_operator_runbook_read_only_gate",
        "runbook_schema": "hierarchical_joint_closeout_operator_runbook_v1",
        "ready_for_controlled_apply_now": False,
        "start_blockers": [
            "awc_legacy_terminal_closeout_decision_pending",
            "writer_quiescence_not_satisfied",
        ],
        "impact_summary": {
            "archive_db_apply_steps": [
                "apply_awc_legacy_terminal_closeout",
                "apply_identity_audit_indexes",
                "apply_duplicate_fact_snapshot_repair",
                "apply_identity_merge_fill",
            ],
            "manifest_write_steps": [
                "emit_decision_manifest_template",
                "operator_reviews_decision_manifest",
                "materialize_reviewed_duplicate_manifest",
            ],
            "backup_steps": ["create_fresh_backup", "create_shared_fresh_backup"],
            "service_control_steps": ["pause_writer_services", "resume_writer_services"],
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "changes_parameters": False,
            "historical_backfill": False,
        },
        "confirmation_phrases": {
            "awc_terminal_closeout": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
            "production_identity_audit_indexes": "APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES",
            "production_duplicate_repair": "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR",
            "production_identity_merge_fill": "APPLY_PRODUCTION_IDENTITY_MERGE_FILL",
        },
        "missing_required_steps": [],
        "blockers": [] if ok else ["hierarchical_joint_closeout_operator_runbook_apply_steps_mismatch"],
        "warnings": [
            "hierarchical_joint_closeout_runbook_start_blocker:awc_legacy_terminal_closeout_decision_pending",
            "hierarchical_joint_closeout_runbook_start_blocker:writer_quiescence_not_satisfied",
        ],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_awc_hard_source_signals": False,
            "writes_signal_to_order_timing": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_auto_orders": False,
            "writes_source_observations": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "operator_can_use_hierarchical_joint_closeout_runbook_after_confirmation"
            if ok
            else "fix_hierarchical_joint_closeout_operator_runbook_before_apply"
        ),
    }


def _posterior_rematerialization_report(*, ok=True, current_latest_issue_count=0, historical_only_issue_count=0):
    return {
        "schema": "posterior_rematerialization_governance_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "posterior_rematerialization_governance_read_only_gate",
        "source_schema": "posterior_ssot_rematerialization_plan_v1",
        "forward_write_acceptance": {
            "checked_fact_count": current_latest_issue_count + historical_only_issue_count,
            "current_issue_fact_count": current_latest_issue_count + historical_only_issue_count,
            "projected_ok_count": current_latest_issue_count + historical_only_issue_count,
            "projected_issue_count": 0,
            "can_forward_writer_fix_all_candidates": True,
            "does_not_execute_apply": True,
        },
        "historical_rematerialization_plan": {
            "candidate_count": current_latest_issue_count + historical_only_issue_count,
            "current_latest_issue_count": current_latest_issue_count,
            "historical_only_issue_count": historical_only_issue_count,
            "impact_counts": (
                {"current_latest_fact_has_posterior_ssot_gap": current_latest_issue_count}
                if current_latest_issue_count
                else {"historical_fact_superseded_by_latest_fact": historical_only_issue_count}
                if historical_only_issue_count
                else {}
            ),
            "production_latest_chain_blocked_by_candidates": current_latest_issue_count > 0,
            "requires_operator_confirmation": bool(current_latest_issue_count or historical_only_issue_count),
            "no_apply_plan_only": True,
        },
        "blockers": [] if ok else ["posterior_rematerialization_current_latest_issue_candidates"],
        "warnings": ["posterior_rematerialization_history_only_candidates_present"] if historical_only_issue_count else [],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_daily_weather_reviews": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _controlled_apply_packet_report(*, ok=True):
    return {
        "schema": "daily_review_controlled_apply_packet_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "daily_review_controlled_apply_packet_read_only_gate",
        "packet_schema": "daily_review_controlled_apply_confirmation_packet_v1",
        "backup_path": "/tmp/archive.db.phase0.bak",
        "manifest_output_path": "/tmp/archive.db.lineage-repair.manifest.json",
        "missing_fact_decision_policy": "exclude_sample",
        "proposed_manifest_decision_counts": {"confirm_repair": 1, "exclude_sample": 1},
        "missing_required_steps": [],
        "missing_command_fragments": [],
        "blockers": [] if ok else ["controlled_apply_packet_commands_incomplete"],
        "warnings": [],
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "writes_manifest_file": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _controlled_apply_compact_summary(
    *,
    backup_command: str,
    joint_acceptance_command: str,
) -> dict:
    backup_path = "/tmp/archive.db.phase0.bak"
    return {
        "schema": "daily_review_controlled_apply_confirmation_packet_v1_summary",
        "source_schema": "daily_review_controlled_apply_confirmation_packet_v1",
        "read_only": True,
        "dry_run": True,
        "backup_path": backup_path,
        "manifest_output_path": "/tmp/archive.db.lineage-repair.manifest.json",
        "missing_fact_decision_policy": "exclude_sample",
        "proposed_manifest_decision_counts": {"exclude_sample": 1},
        "phase0": {"needs_apply": False, "ddl_summary": {}},
        "lineage": {
            "candidate_count": 1,
            "ready_for_controlled_apply_after_runbook": True,
            "risk_summary": {},
        },
        "persistence": {
            "effective_settled_event_count": 1,
            "excluded_settled_event_count": 1,
            "ready_to_apply_now": False,
            "batch_apply_blockers": ["explicit_user_confirmation_required"],
        },
        "control_sequence": [
            {
                "step": "materialize_reviewed_manifest",
                "command": (
                    "python3 scripts/daily_review_lineage_repair_operator_runbook.py "
                    "--emit-proposed-manifest-json"
                ),
            },
            {"step": "create_fresh_backup", "command": backup_command},
            {
                "step": "apply_phase0_schema",
                "command": (
                    "python3 scripts/phase0_schema_apply.py --apply "
                    f"--confirm APPLY_PHASE0_FORWARD_SCHEMA --backup {backup_path} --writer-quiescent"
                ),
            },
            {"step": "dry_run_lineage_repair_and_risk", "command": []},
            {
                "step": "apply_lineage_repair",
                "command": (
                    "python3 scripts/daily_review_lineage_repair_apply.py --apply "
                    f"--confirm APPLY_DAILY_REVIEW_LINEAGE_REPAIR --backup {backup_path} --writer-quiescent"
                ),
            },
            {"step": "dry_run_daily_review_persistence", "command": "python3 scripts/daily_review_persistence_apply.py"},
            {
                "step": "apply_daily_review_persistence",
                "command": (
                    "python3 scripts/daily_review_persistence_apply.py --lineage-decision-manifest "
                    "/tmp/archive.db.lineage-repair.manifest.json --apply "
                    f"--confirm APPLY_DAILY_REVIEW_PERSISTENCE --backup {backup_path} --writer-quiescent"
                ),
            },
            {
                "step": "post_apply_acceptance",
                "command": [
                    "python3 scripts/daily_review_post_apply_acceptance_audit.py --since-days 45",
                    joint_acceptance_command,
                ],
            },
        ],
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _post_apply_resolution_packet_report(*, ok=True):
    return {
        "schema": "daily_review_post_apply_resolution_packet_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "daily_review_post_apply_resolution_packet_read_only_gate",
        "packet_schema": "daily_review_post_apply_resolution_confirmation_packet_v1",
        "resolution_summary": {
            "unresolved_counts": {
                "missing_fact_snapshot_excluded": 1,
                "posterior_ssot_fact_candidates": 2,
                "legacy_awc_signal_anchor_candidates": 1,
            }
        },
        "posterior_ssot_repair": {
            "candidate_count": 2,
            "apply_command_count": 2,
            "ready_for_operator_controlled_apply": True,
        },
        "legacy_awc_signal_anchor": {
            "candidate_count": 1,
            "decision_required": True,
            "operator_route_complete": False,
            "acceptance_exception_complete": False,
            "decision_manifest_schema": "legacy_awc_signal_anchor_decision_manifest_v1",
            "decision_manifest_audit": {
                "manifest_provided": False,
                "decision_counts": {"undecided": 1},
                "missing_decision_count": 1,
                "invalid_decision_count": 0,
                "review_evidence_missing_count": 0,
                "dedicated_writer_required_count": 0,
                "reviewed_exception_count": 0,
                "hard_blockers": [
                    "legacy_awc_signal_anchor_decision_manifest_missing",
                    "legacy_awc_signal_anchor_decision_missing",
                    "legacy_awc_signal_anchor_decision_pending",
                ],
            },
            "ready_for_automatic_apply": False,
        },
        "daily_review_master": {
            "ok": True,
            "proposed_manifest_decision_counts": {"confirm_repair": 2, "exclude_sample": 1},
        },
        "missing_required_steps": [],
        "blockers": [] if ok else ["post_apply_resolution_packet_missing_required_steps"],
        "warnings": ["post_apply_resolution_operator_pending:legacy_awc_signal_anchor_operator_decision_required"],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "writes_signal_to_order_timing": False,
            "writes_manifest_file": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _production_preflight_packet_report(*, ok=True):
    return {
        "schema": "daily_review_production_apply_preflight_packet_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "daily_review_production_apply_preflight_packet_read_only_gate",
        "packet_schema": "daily_review_production_apply_preflight_packet_v1",
        "ready_to_start_controlled_sequence": False,
        "start_blockers": ["writer_quiescence_not_satisfied"] if ok else ["writer_quiescence_not_satisfied"],
        "sequence_dependency_blockers": ["requires_prior_apply_stages_to_complete"],
        "maintenance_window": {
            "writer_quiescent": False,
            "required_stop_labels": ["system/com.weather.monitor.daemon"],
            "operator_commands": {
                "verify_writer_quiescence": "python3 scripts/controlled_apply_maintenance_window_packet.py --summary",
                "suggested_pause_commands": ["sudo launchctl disable system/com.weather.monitor.daemon && sudo launchctl stop system/com.weather.monitor.daemon"],
                "suggested_resume_commands": [
                    "sudo launchctl enable system/com.weather.monitor.daemon && sudo launchctl kickstart -k system/com.weather.monitor.daemon  # resume after apply"
                ],
                "manual_wait_or_stop": ["wait_for_backup_process_to_finish(pid=4505)"],
            },
            "stop_authorization": {
                "can_pause_required_services_now": False,
                "blockers": ["system_monitor_daemon_stop_requires_interactive_sudo"],
            },
        },
        "current_latest_posterior_materialization": {
            "candidate_count": 1,
            "projected_ok_count": 1,
            "projected_issue_count": 0,
            "candidate_fact_ids": [106743],
            "apply_confirmation_command": (
                "python3 scripts/posterior_ssot_apply_confirmation_packet.py "
                "--fact-ids 106743 --summary"
            ),
        },
        "missing_required_stages": [],
        "blockers": [] if ok else ["production_apply_preflight_missing_required_stages"],
        "warnings": [
            "production_apply_start_blocker:writer_quiescence_not_satisfied",
            "production_apply_sequence_dependency:requires_prior_apply_stages_to_complete",
        ],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "writes_signal_to_order_timing": False,
            "writes_manifest_file": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _current_latest_posterior_runbook_report(*, ok=True):
    return {
        "schema": "current_latest_posterior_apply_runbook_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "current_latest_posterior_apply_runbook_read_only_gate",
        "runbook_schema": "current_latest_posterior_apply_runbook_v1",
        "ready_for_controlled_apply_now": False,
        "apply_required": True,
        "candidate_fact_ids": [106743],
        "start_blockers": ["writer_quiescence_not_satisfied"],
        "risk_blockers": [],
        "maintenance_window": {"writer_quiescent": False},
        "shared_backup": {
            "path": "/tmp/archive.db.current-latest-posterior-runbook.bak",
            "command": "sqlite3 /tmp/archive.db \".backup /tmp/archive.db.current-latest-posterior-runbook.bak\"",
        },
        "single_fact_preflights": [
            {
                "fact_id": 106743,
                "ok": True,
                "risk_level": "controlled_low_after_operator_confirmation",
                "blockers": [],
                "projected_target_bucket": "34°C",
                "projected_target_probability": 0.99,
                "uses_shared_backup": True,
                "apply_command_available": True,
            }
        ],
        "missing_required_steps": [],
        "blockers": [] if ok else ["current_latest_posterior_runbook_apply_command_missing"],
        "warnings": ["current_latest_posterior_runbook_start_blocker:writer_quiescence_not_satisfied"],
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "writes_signal_to_order_timing": False,
            "writes_manifest_file": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _parameter_governance_report(*, ok=True):
    return {
        "schema": "parameter_governance_surface_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "parameter_governance_surface_read_only_gate",
        "source_schema": "parameter_governance_surface_audit_v1",
        "parameter_surface_count": 4,
        "launchd_restart_surface_count": 4,
        "env_key_count": 76,
        "blockers": [] if ok else ["parameter_governance_surface_not_ok"],
        "warnings": [],
        "guards": {
            "writes_archive_db": False,
            "writes_config_files": False,
            "writes_parameter_change_events": False,
            "changes_parameters": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
        },
    }


def _fact_write_boundary_report(*, ok=True):
    return {
        "schema": "fact_write_boundary_gate_v1",
        "ok": ok,
        "read_only": True,
        "execution_mode": "fact_write_boundary_read_only_gate",
        "source_schema": "fact_write_migration_plan_v1",
        "target_owner": "weather_fact_snapshot_writer.py",
        "pending_migration_file_count": 0 if ok else 1,
        "unknown_fact_writer_surface_count": 0,
        "lane_count": 12,
        "blockers": [] if ok else ["fact_write_boundary_pending_migration_files"],
        "warnings": [],
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def _stub_clean(monkeypatch):
    monkeypatch.setattr(
        audit.probability_gate,
        "build_hierarchical_probability_release_gate",
        lambda **kwargs: _core_report(),
    )
    monkeypatch.setattr(
        audit,
        "_core_integration_contract_report",
        lambda: _core_integration_report(),
    )
    monkeypatch.setattr(
        audit.replay_audit,
        "ssot_chain_replay_input_contract",
        lambda: _replay_contract(),
    )
    monkeypatch.setattr(
        audit.replay_audit,
        "audit",
        lambda **kwargs: _replay_report(),
    )
    monkeypatch.setattr(
        audit,
        "_phase0_schema_apply_contract_report",
        lambda **kwargs: _phase0_schema_report(),
    )
    monkeypatch.setattr(
        audit,
        "_awc_signal_timing_contract_report",
        lambda **kwargs: _awc_signal_timing_report(),
    )
    monkeypatch.setattr(
        audit,
        "_awc_hard_source_terminal_coverage_report",
        lambda **kwargs: _awc_terminal_coverage_report(),
    )
    monkeypatch.setattr(
        audit,
        "_order_lineage_coverage_report",
        lambda **kwargs: _order_lineage_coverage_report(),
    )
    monkeypatch.setattr(
        audit,
        "_awc_legacy_terminal_closeout_packet_report",
        lambda **kwargs: _awc_terminal_closeout_report(),
    )
    monkeypatch.setattr(
        audit,
        "_awc_legacy_terminal_closeout_apply_contract_report",
        lambda **kwargs: _awc_terminal_apply_contract_report(),
    )
    monkeypatch.setattr(
        audit,
        "_awc_legacy_terminal_closeout_operator_runbook_report",
        lambda **kwargs: _awc_terminal_operator_runbook_report(),
    )
    monkeypatch.setattr(
        audit,
        "_signal_to_order_safety_contract_report",
        lambda **kwargs: _signal_to_order_safety_report(),
    )
    monkeypatch.setattr(
        audit,
        "_production_identity_coverage_report",
        lambda **kwargs: _production_identity_report(),
    )
    monkeypatch.setattr(
        audit,
        "_production_identity_duplicate_apply_contract_report",
        lambda **kwargs: _production_identity_duplicate_contract_report(),
    )
    monkeypatch.setattr(
        audit,
        "_production_identity_merge_fill_apply_contract_report",
        lambda **kwargs: _production_identity_merge_fill_contract_report(),
    )
    monkeypatch.setattr(
        audit,
        "_production_identity_closeout_confirmation_packet_report",
        lambda **kwargs: _production_identity_closeout_packet_report(),
    )
    monkeypatch.setattr(
        audit,
        "_production_identity_closeout_operator_runbook_report",
        lambda **kwargs: _production_identity_closeout_operator_runbook_report(),
    )
    monkeypatch.setattr(
        audit,
        "_hierarchical_joint_closeout_operator_runbook_report",
        lambda **kwargs: _hierarchical_joint_closeout_operator_runbook_report(),
    )
    monkeypatch.setattr(
        audit,
        "_posterior_rematerialization_report",
        lambda **kwargs: _posterior_rematerialization_report(),
    )
    monkeypatch.setattr(
        audit,
        "_daily_review_controlled_apply_packet_report",
        lambda **kwargs: _controlled_apply_packet_report(),
    )
    monkeypatch.setattr(
        audit,
        "_daily_review_post_apply_resolution_packet_report",
        lambda **kwargs: _post_apply_resolution_packet_report(),
    )
    monkeypatch.setattr(
        audit,
        "_daily_review_production_apply_preflight_packet_report",
        lambda **kwargs: _production_preflight_packet_report(),
    )
    monkeypatch.setattr(
        audit,
        "_current_latest_posterior_apply_runbook_report",
        lambda **kwargs: _current_latest_posterior_runbook_report(),
    )
    monkeypatch.setattr(
        audit,
        "_parameter_governance_surface_report",
        lambda **kwargs: _parameter_governance_report(),
    )
    monkeypatch.setattr(
        audit,
        "_fact_write_boundary_report",
        lambda **kwargs: _fact_write_boundary_report(),
    )
    monkeypatch.setattr(
        audit.review_acceptance_audit,
        "build_report",
        lambda *args, **kwargs: _daily_review_report(),
    )


def test_joint_plan_coverage_audit_passes_when_core_awc_and_review_pass(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is True
    assert report["gate_order"] == [
        "core_probability_release_gate",
        "hierarchical_core_integration_contract_gate",
        "posterior_rematerialization_governance_gate",
        "awc_signal_to_order_contract_gate",
        "awc_signal_to_order_replay_gate",
        "awc_signal_timing_contract_gate",
        "awc_hard_source_terminal_coverage_gate",
        "order_lineage_coverage_gate",
        "signal_to_order_safety_contract_gate",
        "phase0_schema_apply_contract_gate",
        "production_identity_coverage_gate",
        "hierarchical_joint_closeout_operator_runbook_gate",
        "daily_review_controlled_apply_packet_gate",
        "daily_review_post_apply_resolution_packet_gate",
        "daily_review_production_apply_preflight_packet_gate",
        "current_latest_posterior_apply_runbook_gate",
        "daily_review_post_apply_gate",
        "parameter_governance_surface_gate",
        "fact_write_boundary_gate",
        "downstream_single_ssot_gate",
    ]
    assert all(report["checks"].values())
    assert report["guards"]["writes_archive_db"] is False


def test_joint_plan_daily_review_uses_independent_scope_limit(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    captured = {}

    def fake_daily_review_report(*args, **kwargs):
        captured.update(kwargs)
        return _daily_review_report()

    monkeypatch.setattr(audit.review_acceptance_audit, "build_report", fake_daily_review_report)

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
        limit=20,
        daily_review_scope_limit=1234,
    )

    assert report["scope"]["limit"] == 20
    assert report["scope"]["daily_review_scope_limit"] == 1234
    assert captured["limit"] == 1234
    assert report["checks"]["daily_review_post_apply_gate"] is True
    assert report["guards"]["places_orders"] is False
    diagnostics = report["actionable_diagnostics"]
    assert diagnostics["awc_hard_source_terminal_coverage"]["accepted_signal_count"] == 1
    assert diagnostics["awc_hard_source_terminal_coverage"]["terminal_state_counts"] == {
        "submitted": 1
    }
    assert diagnostics["awc_hard_source_terminal_coverage"]["closeout"]["candidate_count"] == 2
    assert (
        diagnostics["awc_hard_source_terminal_coverage"]["closeout"]["next_boundary"]
        == "review_awc_legacy_terminal_closeout_manifest_before_apply_contract"
    )
    assert diagnostics["awc_hard_source_terminal_coverage"]["apply_contract"]["confirmation_phrase"] == (
        "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT"
    )
    assert diagnostics["awc_hard_source_terminal_coverage"]["apply_contract"]["requires_backup"] is True
    assert diagnostics["awc_hard_source_terminal_coverage"]["apply_contract"]["requires_writer_quiescence"] is True
    assert diagnostics["awc_hard_source_terminal_coverage"]["operator_runbook"]["confirmation_phrase"] == (
        "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT"
    )
    assert diagnostics["awc_hard_source_terminal_coverage"]["operator_runbook"]["archive_db_apply_steps"] == [
        "apply_awc_legacy_terminal_closeout"
    ]
    assert diagnostics["awc_hard_source_terminal_coverage"]["operator_runbook"]["review_report_steps"] == [
        "emit_review_report"
    ]
    assert diagnostics["current_latest_posterior_apply"]["candidate_fact_ids"] == [106743]
    assert diagnostics["current_latest_posterior_apply"]["shared_backup_path"] == (
        "/tmp/archive.db.current-latest-posterior-runbook.bak"
    )
    assert diagnostics["current_latest_posterior_apply"]["single_fact_preflights"][0]["fact_id"] == 106743
    assert diagnostics["production_identity_closeout"]["packet_ok"] is True
    assert diagnostics["production_identity_closeout"]["operator_runbook_ok"] is True
    assert diagnostics["production_identity_closeout"]["operator_runbook_ready_for_controlled_apply_now"] is False
    assert diagnostics["production_identity_closeout"]["identity_audit_index_contract_ok"] is True
    assert diagnostics["production_identity_closeout"]["identity_audit_index_already_satisfied"] is False
    assert diagnostics["production_identity_closeout"]["identity_audit_index_needs_apply"] is True
    assert diagnostics["production_identity_closeout"]["identity_audit_index_ddl_count"] == 1
    assert diagnostics["production_identity_closeout"]["identity_audit_index_contract_blocker_count"] == 0
    assert diagnostics["hierarchical_joint_closeout"]["ok"] is True
    assert diagnostics["hierarchical_joint_closeout"]["ready_for_controlled_apply_now"] is False
    assert diagnostics["hierarchical_joint_closeout"]["archive_db_apply_steps"] == [
        "apply_awc_legacy_terminal_closeout",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "apply_identity_merge_fill",
    ]
    assert diagnostics["hierarchical_joint_closeout"]["confirmation_phrases"] == {
        "awc_terminal_closeout": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
        "production_identity_audit_indexes": "APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES",
        "production_duplicate_repair": "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR",
        "production_identity_merge_fill": "APPLY_PRODUCTION_IDENTITY_MERGE_FILL",
    }
    assert diagnostics["production_identity_closeout"]["hard_blockers"] == ["writer_quiescence_not_satisfied"]
    assert diagnostics["production_identity_closeout"]["runbook_start_blockers"] == [
        "writer_quiescence_not_satisfied"
    ]
    assert diagnostics["production_apply"]["start_blockers"] == ["writer_quiescence_not_satisfied"]
    assert diagnostics["production_apply"]["maintenance_required_stop_labels"] == [
        "system/com.weather.monitor.daemon"
    ]
    assert diagnostics["production_apply"]["maintenance_operator_commands"]["suggested_pause_commands"] == [
        "sudo launchctl disable system/com.weather.monitor.daemon && sudo launchctl stop system/com.weather.monitor.daemon"
    ]
    assert diagnostics["production_apply"]["maintenance_operator_commands"]["manual_wait_or_stop"] == [
        "wait_for_backup_process_to_finish(pid=4505)"
    ]
    assert diagnostics["production_apply"]["maintenance_stop_authorization"]["can_pause_required_services_now"] is False
    assert diagnostics["daily_review_post_apply"]["missing_review_count"] == 0
    assert diagnostics["parameter_governance"]["parameter_surface_count"] == 4
    assert diagnostics["fact_write_boundary"]["pending_migration_file_count"] == 0


def test_joint_plan_summary_blocker_keeps_downstream_dependency():
    blocker = audit._compact_summary_blocker(
        {
            "blocker": "downstream_single_ssot_dependency_not_ok",
            "dependency": "awc_hard_source_terminal_coverage_gate",
        },
        max_items_output=5,
    )

    assert blocker == {
        "blocker": "downstream_single_ssot_dependency_not_ok",
        "dependency": "awc_hard_source_terminal_coverage_gate",
    }


def test_joint_plan_coverage_blocks_core_integration_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_core_integration_contract_report",
        lambda: _core_integration_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["hierarchical_core_integration_contract_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "hierarchical_core_integration_contract_gate_hierarchical_core_integration_contract_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_order_lineage_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_order_lineage_coverage_report",
        lambda **kwargs: _order_lineage_coverage_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["order_lineage_coverage_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "order_lineage_coverage_gate_order_lineage_coverage_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_signal_to_order_safety_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_signal_to_order_safety_contract_report",
        lambda **kwargs: _signal_to_order_safety_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["signal_to_order_safety_contract_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "signal_to_order_safety_contract_gate_signal_to_order_safety_contract_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_awc_signal_timing_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_awc_signal_timing_contract_report",
        lambda **kwargs: _awc_signal_timing_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["awc_signal_timing_contract_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "awc_signal_timing_contract_gate_awc_signal_timing_contract_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_awc_terminal_coverage_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_awc_hard_source_terminal_coverage_report",
        lambda **kwargs: _awc_terminal_coverage_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["awc_hard_source_terminal_coverage_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert (
        "awc_hard_source_terminal_coverage_gate_awc_hard_source_terminal_coverage_not_ok"
        in blocker_ids
    )
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids
    diagnostics = report["actionable_diagnostics"]["awc_hard_source_terminal_coverage"]
    assert diagnostics["non_final_terminal_signal_ids"] == ["sig_awc"]
    assert diagnostics["missing_ledger_signal_ids"] == ["sig_awc"]


def test_joint_plan_coverage_blocks_production_identity_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_production_identity_coverage_report",
        lambda **kwargs: _production_identity_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["production_identity_coverage_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "production_identity_coverage_gate_production_identity_coverage_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_downgrades_duplicate_identity_contract_gap_when_closeout_ready(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_production_identity_duplicate_apply_contract_report",
        lambda **kwargs: _production_identity_duplicate_contract_report(ok=False, duplicate_row_count=56),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is True
    assert report["checks"]["production_identity_coverage_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is True
    production_gate = [
        gate for gate in report["gates"] if gate["gate"] == "production_identity_coverage_gate"
    ][0]
    assert "production_identity_duplicate_apply_contract" in production_gate["reports"]
    assert production_gate["reports"]["production_identity_duplicate_apply_contract"]["ok"] is False
    assert "review_duplicate_decision_manifest_before_identity_merge_fill" in report["warnings"]


def test_joint_plan_coverage_downgrades_merge_fill_identity_contract_gap_when_closeout_ready(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_production_identity_merge_fill_apply_contract_report",
        lambda **kwargs: _production_identity_merge_fill_contract_report(ok=False, operation_count=12),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is True
    assert report["checks"]["production_identity_coverage_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is True
    production_gate = [
        gate for gate in report["gates"] if gate["gate"] == "production_identity_coverage_gate"
    ][0]
    assert "production_identity_merge_fill_apply_contract" in production_gate["reports"]
    assert production_gate["reports"]["production_identity_merge_fill_apply_contract"]["ok"] is False
    assert "run_duplicate_repair_then_review_identity_merge_fill_contract" in report["warnings"]


def test_joint_plan_coverage_blocks_identity_contract_structural_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_production_identity_duplicate_apply_contract_report",
        lambda **kwargs: {
            **_production_identity_duplicate_contract_report(ok=False, duplicate_row_count=56),
            "read_only": False,
        },
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["production_identity_coverage_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "production_identity_coverage_gate_production_identity_duplicate_apply_contract_not_read_only" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_identity_closeout_packet_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_production_identity_closeout_confirmation_packet_report",
        lambda **kwargs: _production_identity_closeout_packet_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["production_identity_coverage_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    production_gate = [
        gate for gate in report["gates"] if gate["gate"] == "production_identity_coverage_gate"
    ][0]
    assert "production_identity_closeout_confirmation_packet" in production_gate["reports"]
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "production_identity_coverage_gate_production_identity_closeout_confirmation_packet_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_identity_closeout_operator_runbook_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_production_identity_closeout_operator_runbook_report",
        lambda **kwargs: _production_identity_closeout_operator_runbook_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["production_identity_coverage_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    production_gate = [
        gate for gate in report["gates"] if gate["gate"] == "production_identity_coverage_gate"
    ][0]
    assert "production_identity_closeout_operator_runbook" in production_gate["reports"]
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "production_identity_coverage_gate_production_identity_closeout_operator_runbook_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_joint_closeout_operator_runbook_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_hierarchical_joint_closeout_operator_runbook_report",
        lambda **kwargs: _hierarchical_joint_closeout_operator_runbook_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["hierarchical_joint_closeout_operator_runbook_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    closeout_gate = [
        gate for gate in report["gates"] if gate["gate"] == "hierarchical_joint_closeout_operator_runbook_gate"
    ][0]
    assert "hierarchical_joint_closeout_operator_runbook" in closeout_gate["reports"]
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert (
        "hierarchical_joint_closeout_operator_runbook_gate_hierarchical_joint_closeout_operator_runbook_not_ok"
        in blocker_ids
    )
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_audit_blocks_missing_daily_review(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit.review_acceptance_audit,
        "build_report",
        lambda *args, **kwargs: _daily_review_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["daily_review_post_apply_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    assert report["checks"]["posterior_rematerialization_governance_gate"] is True
    assert report["checks"]["phase0_schema_apply_contract_gate"] is True
    assert report["checks"]["daily_review_controlled_apply_packet_gate"] is True
    assert report["checks"]["daily_review_post_apply_resolution_packet_gate"] is True
    assert report["checks"]["daily_review_production_apply_preflight_packet_gate"] is True
    assert report["checks"]["current_latest_posterior_apply_runbook_gate"] is True
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "daily_review_post_apply_gate_daily_review_post_apply_acceptance_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_phase0_schema_contract_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_phase0_schema_apply_contract_report",
        lambda **kwargs: _phase0_schema_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["phase0_schema_apply_contract_gate"] is False
    assert report["checks"]["posterior_rematerialization_governance_gate"] is True
    assert report["checks"]["daily_review_controlled_apply_packet_gate"] is True
    assert report["checks"]["daily_review_post_apply_resolution_packet_gate"] is True
    assert report["checks"]["daily_review_production_apply_preflight_packet_gate"] is True
    assert report["checks"]["current_latest_posterior_apply_runbook_gate"] is True
    assert report["checks"]["daily_review_post_apply_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "phase0_schema_apply_contract_gate_phase0_schema_apply_contract_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_parameter_governance_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_parameter_governance_surface_report",
        lambda **kwargs: _parameter_governance_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["parameter_governance_surface_gate"] is False
    assert report["checks"]["fact_write_boundary_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "parameter_governance_surface_gate_parameter_governance_surface_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_fact_write_boundary_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_fact_write_boundary_report",
        lambda **kwargs: _fact_write_boundary_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["parameter_governance_surface_gate"] is True
    assert report["checks"]["fact_write_boundary_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "fact_write_boundary_gate_fact_write_boundary_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_controlled_apply_packet_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_daily_review_controlled_apply_packet_report",
        lambda **kwargs: _controlled_apply_packet_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["daily_review_controlled_apply_packet_gate"] is False
    assert report["checks"]["posterior_rematerialization_governance_gate"] is True
    assert report["checks"]["daily_review_post_apply_resolution_packet_gate"] is True
    assert report["checks"]["daily_review_production_apply_preflight_packet_gate"] is True
    assert report["checks"]["current_latest_posterior_apply_runbook_gate"] is True
    assert report["checks"]["daily_review_post_apply_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "daily_review_controlled_apply_packet_gate_daily_review_controlled_apply_packet_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_controlled_apply_packet_gate_rejects_missing_backup_command_and_stale_joint_limit(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(audit.controlled_apply_packet, "build_confirmation_packet", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        audit.controlled_apply_packet,
        "compact_confirmation_packet",
        lambda _packet: _controlled_apply_compact_summary(
            backup_command="",
            joint_acceptance_command=(
                "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary "
                "--limit 20 --max-items-output 5"
            ),
        ),
    )

    report = audit._daily_review_controlled_apply_packet_report(
        db_path=tmp_path / "archive.db",
        since_days=45,
        since_date="",
        slug="",
        event_date="",
        market_scope="highest_temperature",
        limit=100,
        max_items_output=8,
    )

    assert report["ok"] is False
    assert "controlled_apply_packet_commands_incomplete" in report["blockers"]
    fragments = set(report["missing_command_fragments"])
    assert "create_fresh_backup:sqlite3" in fragments
    assert "create_fresh_backup:.backup" in fragments
    assert "create_fresh_backup:/tmp/archive.db.phase0.bak" in fragments
    assert "post_apply_acceptance:--limit 100" in fragments
    assert "post_apply_acceptance:--max-items-output 8" in fragments


def test_joint_plan_coverage_blocks_post_apply_resolution_packet_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_daily_review_post_apply_resolution_packet_report",
        lambda **kwargs: _post_apply_resolution_packet_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["daily_review_post_apply_resolution_packet_gate"] is False
    assert report["checks"]["daily_review_production_apply_preflight_packet_gate"] is True
    assert report["checks"]["current_latest_posterior_apply_runbook_gate"] is True
    assert report["checks"]["posterior_rematerialization_governance_gate"] is True
    assert report["checks"]["daily_review_post_apply_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "daily_review_post_apply_resolution_packet_gate_daily_review_post_apply_resolution_packet_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_production_preflight_contract_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_daily_review_production_apply_preflight_packet_report",
        lambda **kwargs: _production_preflight_packet_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["daily_review_production_apply_preflight_packet_gate"] is False
    assert report["checks"]["current_latest_posterior_apply_runbook_gate"] is True
    assert report["checks"]["daily_review_post_apply_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "daily_review_production_apply_preflight_packet_gate_daily_review_production_apply_preflight_packet_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_blocks_current_latest_posterior_runbook_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_current_latest_posterior_apply_runbook_report",
        lambda **kwargs: _current_latest_posterior_runbook_report(ok=False),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["current_latest_posterior_apply_runbook_gate"] is False
    assert report["checks"]["daily_review_post_apply_gate"] is True
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "current_latest_posterior_apply_runbook_gate_current_latest_posterior_apply_runbook_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_reuses_production_preflight_for_current_latest_runbook(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    prebuilt_preflight = {"schema": "daily_review_production_apply_preflight_packet_v1", "marker": "reuse-me"}
    captured = {}

    monkeypatch.setattr(
        audit,
        "_daily_review_production_apply_preflight_packet_report",
        lambda **kwargs: {
            **_production_preflight_packet_report(),
            "_production_preflight_packet": prebuilt_preflight,
        },
    )

    def _fake_current_latest_report(**kwargs):
        captured["production_preflight_packet"] = kwargs.get("production_preflight_packet")
        return _current_latest_posterior_runbook_report()

    monkeypatch.setattr(
        audit,
        "_current_latest_posterior_apply_runbook_report",
        _fake_current_latest_report,
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["checks"]["current_latest_posterior_apply_runbook_gate"] is True
    assert captured["production_preflight_packet"] is prebuilt_preflight


def test_current_latest_runbook_gate_blocks_apply_enabled_before_ready(monkeypatch, tmp_path: Path):
    backup_path = "/tmp/archive.db.current-latest-posterior-runbook.bak"
    command = (
        "python3 scripts/apply_posterior_ssot_fact_repair.py --fact-id 106743 "
        f"--backup {backup_path} --confirm APPLY_POSTERIOR_SSOT_FACT_REPAIR --writer-quiescent"
    )
    compact = {
        "schema": "current_latest_posterior_apply_runbook_v1_summary",
        "source_schema": "current_latest_posterior_apply_runbook_v1",
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "ready_for_controlled_apply_now": False,
        "start_blockers": ["writer_quiescence_not_satisfied"],
        "risk_blockers": [],
        "scope": {"apply_required": True, "candidate_fact_ids": [106743]},
        "apply_required": True,
        "maintenance_window": {"writer_quiescent": False},
        "shared_backup": {
            "path": backup_path,
            "command": f"sqlite3 /tmp/archive.db \".backup {backup_path}\"",
        },
        "single_fact_preflights": [
            {
                "fact_id": 106743,
                "ok": True,
                "risk_level": "controlled_low_after_operator_confirmation",
                "blockers": [],
                "projected_target_bucket": "34°C",
                "uses_shared_backup": True,
                "apply_command_available": True,
            }
        ],
        "operator_sequence": [
            {
                "step": "run_current_latest_temp_drill",
                "command": "python3 scripts/current_latest_posterior_apply_temp_drill.py --db /tmp/archive.db --fact-ids 106743",
                "writes_archive_db": False,
                "writes_temp_archive_db": True,
                "candidate_fact_ids": [106743],
                "candidate_count": 1,
            },
            {
                "step": "verify_writer_quiescence",
                "command": "python3 scripts/controlled_apply_maintenance_window_packet.py",
            },
            {
                "step": "create_one_shared_archive_backup",
                "command": f"sqlite3 /tmp/archive.db \".backup {backup_path}\"",
            },
            {
                "step": "preflight_one_current_latest_fact",
                "fact_id": 106743,
                "command": "python3 scripts/posterior_ssot_apply_preflight_risk_packet.py --fact-id 106743",
            },
            {
                "step": "apply_one_current_latest_fact",
                "fact_id": 106743,
                "command": command,
                "enabled_now": True,
                "blocked_until": [],
                "preconditions": [
                    "run_current_latest_temp_drill returns ok == true for the same candidate_fact_ids",
                    "verify_writer_quiescence returns writer_quiescent == true and start_blockers == []"
                ],
            },
            {
                "step": "post_apply_latest_coverage",
                "command": "python3 scripts/posterior_ssot_fact_coverage_audit.py",
            },
            {
                "step": "post_apply_rematerialization_governance",
                "command": "python3 scripts/posterior_ssot_rematerialization_plan.py",
            },
            {
                "step": "post_apply_exact_candidate_acceptance",
                "command": (
                    "python3 scripts/current_latest_posterior_post_apply_acceptance.py "
                    "--db /tmp/archive.db --candidate-fact-ids 106743 "
                    "--expected-projections-json '[{\"fact_id\":106743,\"target_bucket\":\"34°C\",\"target_probability\":0.99}]' "
                    "--max-items-output 8"
                ),
                "writes_archive_db": False,
                "required": True,
            },
            {
                "step": "post_apply_joint_plan_gate",
                "command": "python3 scripts/hierarchical_joint_plan_coverage_audit.py",
            },
        ],
        "boundary": {
            "runbook_executes_stop_or_start": False,
            "runbook_executes_temp_drill": False,
            "runbook_executes_backup": False,
            "runbook_executes_apply": False,
            "runbook_executes_sql_writes": False,
            "temp_drill_writes_production_db": False,
            "requires_temp_drill_before_apply": True,
            "uses_single_fact_apply_entrypoint_only": True,
            "requires_writer_quiescence_before_apply": True,
            "requires_one_shared_backup_before_apply": True,
            "requires_exact_candidate_acceptance_after_apply": True,
            "historical_backfill_allowed": False,
            "orders_or_decisions_allowed": False,
        },
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_daily_weather_reviews": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }
    monkeypatch.setattr(audit.posterior_apply_runbook, "build_runbook", lambda *args, **kwargs: {})
    monkeypatch.setattr(audit.posterior_apply_runbook, "compact_runbook", lambda _packet: compact)

    report = audit._current_latest_posterior_apply_runbook_report(
        db_path=tmp_path / "archive.db",
        since_hours=168.0,
        limit=100,
        max_items_output=8,
    )

    assert report["ok"] is False
    assert "current_latest_posterior_runbook_apply_enabled_before_ready" in report["blockers"]


def test_current_latest_runbook_gate_suppresses_start_blocker_warning_when_no_apply_required(monkeypatch, tmp_path: Path):
    compact = {
        "schema": "current_latest_posterior_apply_runbook_v1_summary",
        "source_schema": "current_latest_posterior_apply_runbook_v1",
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "ready_for_controlled_apply_now": False,
        "start_blockers": [
            "system_monitor_daemon_stop_requires_interactive_sudo",
            "writer_quiescence_not_satisfied",
        ],
        "risk_blockers": [],
        "scope": {"apply_required": False, "candidate_fact_ids": []},
        "apply_required": False,
        "maintenance_window": {"writer_quiescent": False},
        "shared_backup": {
            "path": "/tmp/archive.db.current-latest-posterior-runbook.bak",
            "command": "sqlite3 /tmp/archive.db \".backup /tmp/archive.db.current-latest-posterior-runbook.bak\"",
        },
        "single_fact_preflights": [],
        "operator_sequence": [
            {
                "step": "verify_writer_quiescence",
                "command": "python3 scripts/controlled_apply_maintenance_window_packet.py",
            },
            {
                "step": "create_one_shared_archive_backup",
                "command": "sqlite3 /tmp/archive.db \".backup /tmp/archive.db.current-latest-posterior-runbook.bak\"",
            },
            {
                "step": "post_apply_latest_coverage",
                "command": "python3 scripts/posterior_ssot_fact_coverage_audit.py",
            },
            {
                "step": "post_apply_rematerialization_governance",
                "command": "python3 scripts/posterior_ssot_rematerialization_plan.py",
            },
            {
                "step": "post_apply_exact_candidate_acceptance",
                "command": "python3 scripts/current_latest_posterior_post_apply_acceptance.py",
            },
            {
                "step": "post_apply_joint_plan_gate",
                "command": "python3 scripts/hierarchical_joint_plan_coverage_audit.py",
            },
        ],
        "boundary": {
            "runbook_executes_stop_or_start": False,
            "runbook_executes_temp_drill": False,
            "runbook_executes_backup": False,
            "runbook_executes_apply": False,
            "runbook_executes_sql_writes": False,
            "temp_drill_writes_production_db": False,
            "requires_temp_drill_before_apply": True,
            "uses_single_fact_apply_entrypoint_only": True,
            "requires_writer_quiescence_before_apply": True,
            "requires_one_shared_backup_before_apply": True,
            "requires_exact_candidate_acceptance_after_apply": True,
            "historical_backfill_allowed": False,
            "orders_or_decisions_allowed": False,
        },
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_daily_weather_reviews": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }
    monkeypatch.setattr(audit.posterior_apply_runbook, "build_runbook", lambda *args, **kwargs: {})
    monkeypatch.setattr(audit.posterior_apply_runbook, "compact_runbook", lambda _packet: compact)

    report = audit._current_latest_posterior_apply_runbook_report(
        db_path=tmp_path / "archive.db",
        since_hours=168.0,
        limit=100,
        max_items_output=8,
    )

    assert report["ok"] is True
    assert report["apply_required"] is False
    assert "current_latest_posterior_runbook_start_blocker:writer_quiescence_not_satisfied" not in report["warnings"]
    assert (
        "current_latest_posterior_runbook_start_blocker:system_monitor_daemon_stop_requires_interactive_sudo"
        not in report["warnings"]
    )


def test_phase0_schema_apply_contract_exception_includes_sqlite_diagnostics(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "archive.db"
    db_path.write_bytes(b"not sqlite")
    monkeypatch.setattr(
        audit.phase0_schema_apply,
        "build_apply_contract",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("database disk image is malformed")),
    )

    report = audit._phase0_schema_apply_contract_report(db_path=db_path)

    assert report["ok"] is False
    assert report["schema"] == "phase0_schema_apply_contract_exception_v1"
    diagnostics = report["db_diagnostics"]
    assert diagnostics["schema"] == "sqlite_read_diagnostics_v1"
    assert diagnostics["read_only"] is True
    assert diagnostics["db"]["exists"] is True
    assert diagnostics["quick_check"]["status"] == "error"


def test_current_latest_runbook_gate_blocks_wrong_post_apply_sequence(monkeypatch, tmp_path: Path):
    backup_path = "/tmp/archive.db.current-latest-posterior-runbook.bak"
    command = (
        "python3 scripts/apply_posterior_ssot_fact_repair.py --fact-id 106743 "
        f"--backup {backup_path} --confirm APPLY_POSTERIOR_SSOT_FACT_REPAIR --writer-quiescent"
    )
    compact = {
        "schema": "current_latest_posterior_apply_runbook_v1_summary",
        "source_schema": "current_latest_posterior_apply_runbook_v1",
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "ready_for_controlled_apply_now": False,
        "start_blockers": ["writer_quiescence_not_satisfied"],
        "risk_blockers": [],
        "scope": {"apply_required": True, "candidate_fact_ids": [106743]},
        "apply_required": True,
        "maintenance_window": {"writer_quiescent": False},
        "shared_backup": {
            "path": backup_path,
            "command": f"sqlite3 /tmp/archive.db \".backup {backup_path}\"",
        },
        "single_fact_preflights": [
            {
                "fact_id": 106743,
                "ok": True,
                "risk_level": "controlled_low_after_operator_confirmation",
                "blockers": [],
                "projected_target_bucket": "34°C",
                "uses_shared_backup": True,
                "apply_command_available": True,
            }
        ],
        "operator_sequence": [
            {
                "step": "run_current_latest_temp_drill",
                "command": "python3 scripts/current_latest_posterior_apply_temp_drill.py --db /tmp/archive.db --fact-ids 106743",
                "writes_archive_db": False,
                "writes_temp_archive_db": True,
                "candidate_fact_ids": [106743],
                "candidate_count": 1,
            },
            {
                "step": "verify_writer_quiescence",
                "command": "python3 scripts/controlled_apply_maintenance_window_packet.py",
            },
            {
                "step": "create_one_shared_archive_backup",
                "command": f"sqlite3 /tmp/archive.db \".backup {backup_path}\"",
            },
            {
                "step": "preflight_one_current_latest_fact",
                "fact_id": 106743,
                "command": "python3 scripts/posterior_ssot_apply_preflight_risk_packet.py --fact-id 106743",
            },
            {
                "step": "post_apply_exact_candidate_acceptance",
                "command": (
                    "python3 scripts/current_latest_posterior_post_apply_acceptance.py "
                    "--db /tmp/archive.db --candidate-fact-ids 106743 "
                    "--expected-projections-json '[{\"fact_id\":106743,\"target_bucket\":\"34°C\",\"target_probability\":0.99}]'"
                ),
                "writes_archive_db": False,
                "required": True,
            },
            {
                "step": "apply_one_current_latest_fact",
                "fact_id": 106743,
                "command": command,
                "enabled_now": False,
                "blocked_until": ["writer_quiescence_not_satisfied"],
                "preconditions": [
                    "run_current_latest_temp_drill returns ok == true for the same candidate_fact_ids",
                    "verify_writer_quiescence returns writer_quiescent == true and start_blockers == []",
                ],
            },
            {
                "step": "post_apply_latest_coverage",
                "command": "python3 scripts/posterior_ssot_fact_coverage_audit.py",
            },
            {
                "step": "post_apply_rematerialization_governance",
                "command": "python3 scripts/posterior_ssot_rematerialization_plan.py",
            },
            {
                "step": "post_apply_joint_plan_gate",
                "command": "python3 scripts/hierarchical_joint_plan_coverage_audit.py",
            },
        ],
        "boundary": {
            "runbook_executes_stop_or_start": False,
            "runbook_executes_temp_drill": False,
            "runbook_executes_backup": False,
            "runbook_executes_apply": False,
            "runbook_executes_sql_writes": False,
            "temp_drill_writes_production_db": False,
            "requires_temp_drill_before_apply": True,
            "uses_single_fact_apply_entrypoint_only": True,
            "requires_writer_quiescence_before_apply": True,
            "requires_one_shared_backup_before_apply": True,
            "requires_exact_candidate_acceptance_after_apply": True,
            "historical_backfill_allowed": False,
            "orders_or_decisions_allowed": False,
        },
        "guards": {
            "executes_sql_writes": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_daily_weather_reviews": False,
            "creates_backup": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }
    monkeypatch.setattr(audit.posterior_apply_runbook, "build_runbook", lambda *args, **kwargs: {})
    monkeypatch.setattr(audit.posterior_apply_runbook, "compact_runbook", lambda _packet: compact)

    report = audit._current_latest_posterior_apply_runbook_report(
        db_path=tmp_path / "archive.db",
        since_hours=168.0,
        limit=100,
        max_items_output=8,
    )

    assert report["ok"] is False
    assert "current_latest_posterior_runbook_exact_acceptance_not_after_rematerialization" in report["blockers"]


def test_post_apply_resolution_packet_gate_requires_legacy_awc_decision_manifest(monkeypatch, tmp_path: Path):
    summary = {
        "schema": "daily_review_post_apply_resolution_confirmation_packet_v1_summary",
        "source_schema": "daily_review_post_apply_resolution_confirmation_packet_v1",
        "read_only": True,
        "dry_run": True,
        "boundary": {
            "posterior_ssot_repair_included_as_controlled_step": True,
            "missing_fact_snapshot_exclusion_included": True,
            "daily_review_master_controlled_apply_included": True,
            "legacy_awc_signal_anchor_auto_rebuild_included": False,
            "legacy_awc_signal_anchor_decision_manifest_included": True,
            "historical_backfill_included": False,
            "external_fetch_included": False,
            "orders_or_trade_decisions_included": False,
            "parameter_change_included": False,
        },
        "control_sequence": [
            {"step": "review_exact_posterior_rematerialization_probe"},
            {"step": "apply_posterior_ssot_repairs_per_fact", "confirmation_phrase": "APPLY_POSTERIOR_SSOT_FACT_REPAIR"},
            {"step": "verify_posterior_ssot_repairs"},
            {"step": "resolve_legacy_awc_signal_anchors"},
            {"step": "confirm_missing_fact_snapshot_exclusion_scope"},
            {"step": "continue_with_daily_review_controlled_apply_sequence"},
        ],
        "posterior_ssot_repair": {
            "candidate_count": 1,
            "ready_for_operator_controlled_apply": True,
        },
        "legacy_awc_signal_anchor": {
            "candidate_count": 1,
            "decision_required": True,
            "ready_for_automatic_apply": False,
        },
        "daily_review_master": {"ok": True},
        "guards": {"writes_archive_db": False, "executes_sql_writes": False},
        "hard_blockers": ["legacy_awc_signal_anchor_operator_decision_required"],
    }

    monkeypatch.setattr(
        audit.resolution_confirmation_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: {"schema": "daily_review_post_apply_resolution_confirmation_packet_v1"},
    )
    monkeypatch.setattr(
        audit.resolution_confirmation_packet,
        "compact_confirmation_packet",
        lambda _packet: summary,
    )

    report = audit._daily_review_post_apply_resolution_packet_report(
        db_path=tmp_path / "archive.db",
        since_days=45,
        since_date="",
        slug="",
        event_date="",
        market_scope="highest_temperature",
        limit=100,
        max_items_output=5,
    )

    assert report["ok"] is False
    assert "post_apply_resolution_packet_legacy_anchor_manifest_schema_missing" in report["blockers"]
    assert "post_apply_resolution_packet_legacy_anchor_manifest_audit_missing" in report["blockers"]


def test_joint_plan_coverage_audit_blocks_awc_contract_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit.replay_audit,
        "ssot_chain_replay_input_contract",
        lambda: _replay_contract(missing_key="order_submitted_at"),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["awc_signal_to_order_contract_gate"] is False
    contract_gate = next(
        gate for gate in report["gates"] if gate["gate"] == "awc_signal_to_order_contract_gate"
    )
    report_blockers = contract_gate["reports"]["awc_signal_to_order_contract"]["blockers"]
    assert "awc_slo_required_timing_keys_missing" in report_blockers


def test_joint_plan_coverage_blocks_current_latest_rematerialization_gap(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_posterior_rematerialization_report",
        lambda **kwargs: _posterior_rematerialization_report(
            ok=False,
            current_latest_issue_count=1,
            historical_only_issue_count=0,
        ),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is False
    assert report["checks"]["posterior_rematerialization_governance_gate"] is False
    assert report["checks"]["downstream_single_ssot_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "posterior_rematerialization_governance_gate_posterior_rematerialization_governance_not_ok" in blocker_ids
    assert "downstream_single_ssot_dependency_not_ok" in blocker_ids


def test_joint_plan_coverage_allows_history_only_rematerialization_candidates(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit,
        "_posterior_rematerialization_report",
        lambda **kwargs: _posterior_rematerialization_report(
            ok=True,
            current_latest_issue_count=0,
            historical_only_issue_count=3,
        ),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is True
    assert report["checks"]["posterior_rematerialization_governance_gate"] is True
    assert "posterior_rematerialization_history_only_candidates_present" in report["warnings"]


def test_joint_plan_coverage_downgrades_missing_awc_sample_by_default(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit.replay_audit,
        "audit",
        lambda **kwargs: _replay_report(
            ok=False,
            signal_count=0,
            blockers=["no_awc_hard_source_signals_found"],
        ),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
    )

    assert report["ok"] is True
    assert report["checks"]["awc_signal_to_order_replay_gate"] is True
    assert "plan_gate_no_recent_awc_hard_source_signal_sample" in report["warnings"]


def test_joint_plan_coverage_can_require_awc_sample(monkeypatch, tmp_path: Path):
    _stub_clean(monkeypatch)
    monkeypatch.setattr(
        audit.replay_audit,
        "audit",
        lambda **kwargs: _replay_report(
            ok=False,
            signal_count=0,
            blockers=["no_awc_hard_source_signals_found"],
        ),
    )

    report = audit.build_hierarchical_joint_plan_coverage_audit(
        root=tmp_path,
        db_path=tmp_path / "archive.db",
        require_awc_signal_sample=True,
    )

    assert report["ok"] is False
    assert report["checks"]["awc_signal_to_order_replay_gate"] is False
    blocker_ids = {item["blocker"] for item in report["blockers"]}
    assert "awc_signal_to_order_replay_gate_ssot_chain_replay_not_ok" in blocker_ids
