#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import phase0_migration_manifest
import phase0_schema_apply
import phase0_schema_probe
import daily_review_persistence_batch_plan


SCHEMA_VERSION = "phase0_apply_confirmation_packet_v1"
OPERATOR_APPLY_GATE_SUMMARY_SCHEMA = "phase0_operator_apply_gate_summary_v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_APPLY_PREFLIGHT_BLOCKERS = {
    "explicit_user_confirmation",
    "fresh_archive_db_backup",
    "writer_quiescence",
    "post_apply_phase0_schema_probe_schema_only_strict",
    "post_apply_ssot_alignment",
    "apply_flag",
    "confirmation_phrase",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_stamp(value: str) -> str:
    return (
        str(value or "")
        .replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("+00:00", "Z")
    )


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _backup_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.phase0-{_safe_stamp(generated_at)}.bak"


def _check_by_name(rows: list[Mapping[str, Any]], name: str) -> Mapping[str, Any]:
    for row in rows:
        if str(row.get("name") or "") == name:
            return row
    return {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _gate_by_name(readiness: Mapping[str, Any], gate: str) -> Mapping[str, Any]:
    for item in _as_list(readiness.get("gates")):
        item_map = _as_mapping(item)
        if str(item_map.get("gate") or "") == gate:
            return item_map
    return {}


def _build_review_batch_plan_summary(
    db: Path,
    *,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
    prebuilt_review_batch_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if prebuilt_review_batch_plan:
            plan = dict(prebuilt_review_batch_plan)
            batch_plan_source = "prebuilt_batch_plan"
        else:
            plan = daily_review_persistence_batch_plan.build_batch_plan(
                db,
                since_days=45,
                max_items_output=20,
                include_ui_snapshot_audit=False,
                include_replay_audit=False,
                lineage_decision_manifest=lineage_decision_manifest,
            )
            batch_plan_source = "built_batch_plan"
    except Exception as exc:  # pragma: no cover - defensive packet guard
        return {
            "schema": "phase0_apply_review_batch_plan_summary_v1",
            "available": False,
            "read_only": True,
            "batch_plan_source": "unavailable",
            "error": f"{exc.__class__.__name__}: {exc}",
            "operator_review_required": True,
            "guards": {
                "writes_archive_db": False,
                "writes_daily_weather_reviews": False,
                "writes_daily_calibration_records": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "places_orders": False,
                "restarts_launchd": False,
            },
        }
    lineage_state = dict(plan.get("lineage_readiness_state") or {})
    repair_candidates = list(plan.get("lineage_repair_candidates") or [])
    lineage_decision_apply_readiness = dict(plan.get("lineage_decision_apply_readiness") or {})
    return {
        "schema": "phase0_apply_review_batch_plan_summary_v1",
        "available": True,
        "read_only": True,
        "batch_plan_source": batch_plan_source,
        "batch_plan_schema": plan.get("schema"),
        "batch_plan_ok": bool(plan.get("ok")),
        "settled_event_count": plan.get("settled_event_count"),
        "effective_settled_event_count": plan.get("effective_settled_event_count"),
        "excluded_settled_event_count": plan.get("excluded_settled_event_count"),
        "planned_review_count": plan.get("planned_review_count"),
        "raw_planned_review_count": plan.get("raw_planned_review_count"),
        "planned_calibration_record_count": plan.get("planned_calibration_record_count"),
        "raw_planned_calibration_record_count": plan.get("raw_planned_calibration_record_count"),
        "planned_candidate_count": plan.get("planned_candidate_count"),
        "planned_parameter_event_count": plan.get("planned_parameter_event_count"),
        "aggregate_row_counts": dict(plan.get("aggregate_row_counts") or {}),
        "raw_aggregate_row_counts": dict(plan.get("raw_aggregate_row_counts") or {}),
        "can_apply_after_schema_and_confirmation": bool(plan.get("can_apply_after_schema_and_confirmation")),
        "apply_blockers": list(plan.get("apply_blockers") or []),
        "blockers": list(plan.get("blockers") or []),
        "next_boundary": plan.get("next_boundary"),
        "daily_review_persistence_schema_ok": bool(
            (plan.get("daily_review_persistence_schema_status") or {}).get("ok")
        ),
        "lineage_readiness_state": lineage_state,
        "lineage_repair_candidate_count": plan.get("lineage_repair_candidate_count"),
        "effective_lineage_repair_candidate_count": plan.get("effective_lineage_repair_candidate_count"),
        "lineage_repair_type_counts": dict(plan.get("lineage_repair_type_counts") or {}),
        "lineage_repair_candidates": repair_candidates,
        "lineage_repair_candidates_truncated": bool(plan.get("lineage_repair_candidates_truncated")),
        "lineage_repair_operator_packet": dict(plan.get("lineage_repair_operator_packet") or {}),
        "lineage_decision_manifest_audit": dict(plan.get("lineage_decision_manifest_audit") or {}),
        "lineage_repair_apply_contract": dict(plan.get("lineage_repair_apply_contract") or {}),
        "lineage_decision_apply_readiness": lineage_decision_apply_readiness,
        "operator_review_required": bool(repair_candidates or plan.get("apply_blockers")),
        "required_operator_decision": (
            "confirm_repair_or_exclude_each_lineage_candidate_before_daily_review_persistence_apply"
            if repair_candidates
            else "review_batch_plan_before_daily_review_persistence_apply"
        ),
        "guards": dict(plan.get("guards") or {}),
    }


def _build_operator_apply_gate_summary(
    *,
    needs_apply: bool,
    manifest: Mapping[str, Any],
    apply_contract: Mapping[str, Any],
    review_batch_plan_summary: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_blockers = [
        str(value)
        for value in _as_list(manifest.get("apply_blockers"))
        if str(value or "").strip()
    ]
    apply_contract_blockers = [
        str(value)
        for value in _as_list(apply_contract.get("apply_blockers"))
        if str(value or "").strip()
    ]
    schema_blockers_now = sorted(set(manifest_blockers + apply_contract_blockers))
    unexpected_schema_blockers = [
        value for value in schema_blockers_now if value not in SCHEMA_APPLY_PREFLIGHT_BLOCKERS
    ]
    schema_applicable = bool(needs_apply)
    schema_ready_after_preflight = bool(schema_applicable and not unexpected_schema_blockers)

    readiness = _as_mapping(review_batch_plan_summary.get("lineage_decision_apply_readiness"))
    readiness_available = bool(readiness.get("schema"))
    decision_counts = dict(_as_mapping(readiness.get("operator_decision_counts")))
    lineage_repair = dict(_as_mapping(readiness.get("lineage_repair")))
    daily_review_persistence = dict(_as_mapping(readiness.get("daily_review_persistence")))
    lineage_decision_gate = _gate_by_name(readiness, "lineage_decision_manifest")
    lineage_repair_contract_gate = _gate_by_name(readiness, "lineage_repair_apply_contract")
    repair_candidate_count = int(review_batch_plan_summary.get("lineage_repair_candidate_count") or 0)
    effective_repair_candidate_count = int(
        review_batch_plan_summary.get("effective_lineage_repair_candidate_count")
        if review_batch_plan_summary.get("effective_lineage_repair_candidate_count") is not None
        else repair_candidate_count
    )
    repair_item_count = int(lineage_repair.get("repair_item_count") or 0)
    repair_item_blockers = [
        str(value)
        for value in _as_list(lineage_repair.get("repair_item_blockers"))
        if str(value or "").strip()
    ]
    repair_apply_blockers = [
        str(value)
        for value in _as_list(lineage_repair.get("repair_apply_blockers"))
        if str(value or "").strip()
    ]
    manual_rebuild_count = int(decision_counts.get("manual_rebuild_fact_snapshot") or 0)
    wait_for_new_fact_count = int(decision_counts.get("wait_for_new_fact") or 0)
    lineage_repair_blockers: list[str] = []
    repair_applicable = bool(effective_repair_candidate_count or repair_item_count)
    if repair_applicable and not readiness_available:
        lineage_repair_blockers.append("lineage_decision_apply_readiness_missing")
    if repair_applicable and lineage_decision_gate and lineage_decision_gate.get("blocked"):
        lineage_repair_blockers.append(str(lineage_decision_gate.get("reason") or "lineage_decision_manifest_blocked"))
    if repair_applicable and not repair_item_count:
        lineage_repair_blockers.append("no_confirm_repair_items_selected")
    lineage_repair_blockers.extend(repair_item_blockers)
    if repair_applicable and manual_rebuild_count:
        lineage_repair_blockers.append("manual_rebuild_fact_snapshot_pending")
    if repair_applicable and wait_for_new_fact_count:
        lineage_repair_blockers.append("wait_for_new_fact_pending")
    lineage_repair_ready_after_preflight = bool(
        repair_applicable
        and repair_item_count
        and lineage_repair.get("repair_preflight_ready")
        and not repair_item_blockers
    )

    review_available = bool(review_batch_plan_summary.get("available"))
    daily_remaining_blockers = [
        str(value)
        for value in _as_list(daily_review_persistence.get("remaining_hard_blockers"))
        if str(value or "").strip()
    ]
    daily_apply_blockers = [
        str(value)
        for value in _as_list(daily_review_persistence.get("apply_blockers"))
        if str(value or "").strip()
    ]
    daily_gate_blockers = daily_remaining_blockers or daily_apply_blockers
    if not review_available:
        daily_gate_blockers = ["review_batch_plan_summary_unavailable"]
    elif not readiness_available:
        daily_gate_blockers = ["lineage_decision_apply_readiness_missing"]
    daily_ready_after_preflight = bool(
        review_available
        and readiness_available
        and daily_review_persistence.get("ready_after_explicit_confirmation")
    )

    gates = [
        {
            "gate": "phase0_schema_apply",
            "applicable": schema_applicable,
            "ready_now": False,
            "ready_after_operator_preflight": schema_ready_after_preflight,
            "blocked": bool(schema_applicable and not schema_ready_after_preflight),
            "blockers_now": schema_blockers_now,
            "unexpected_blockers": unexpected_schema_blockers,
            "reason": (
                "schema_apply_ready_after_confirmation_backup_and_quiescence"
                if schema_ready_after_preflight
                else "schema_apply_not_required"
                if not schema_applicable
                else "schema_apply_has_unexpected_blockers"
            ),
            "source": "phase0_schema_apply_contract",
        },
        {
            "gate": "lineage_repair_apply",
            "applicable": repair_applicable,
            "ready_now": False,
            "ready_after_operator_preflight": lineage_repair_ready_after_preflight,
            "blocked": bool(repair_applicable and not lineage_repair_ready_after_preflight),
            "repair_candidate_count": repair_candidate_count,
            "effective_repair_candidate_count": effective_repair_candidate_count,
            "repair_item_count": repair_item_count,
            "blockers_now": sorted(set(lineage_repair_blockers)),
            "apply_blockers_after_preflight": repair_apply_blockers,
            "reason": (
                "lineage_repair_subset_ready_after_confirmation_backup_and_quiescence"
                if lineage_repair_ready_after_preflight
                else "no_lineage_repair_candidates"
                if not repair_applicable
                else "lineage_repair_waiting_for_operator_decision_or_manual_resolution"
            ),
            "source": "lineage_decision_apply_readiness.lineage_repair",
        },
        {
            "gate": "daily_review_persistence_apply",
            "applicable": True,
            "ready_now": False,
            "ready_after_operator_preflight": daily_ready_after_preflight,
            "blocked": bool(not daily_ready_after_preflight),
            "planned_review_count": review_batch_plan_summary.get("planned_review_count"),
            "effective_settled_event_count": review_batch_plan_summary.get("effective_settled_event_count"),
            "blockers_now": sorted(set(daily_gate_blockers)),
            "reason": (
                "daily_review_persistence_ready_after_confirmation"
                if daily_ready_after_preflight
                else "review_batch_plan_summary_unavailable"
                if not review_available
                else "daily_review_persistence_has_remaining_blockers"
            ),
            "source": "lineage_decision_apply_readiness.daily_review_persistence",
        },
    ]
    actionable_blocked_gates = [
        item.get("gate")
        for item in gates
        if item.get("applicable") and item.get("blocked")
    ]
    return {
        "schema": OPERATOR_APPLY_GATE_SUMMARY_SCHEMA,
        "read_only": True,
        "execution_mode": "phase0_operator_apply_gate_summary_no_apply",
        "gate_order": [
            "phase0_schema_apply",
            "lineage_repair_apply",
            "daily_review_persistence_apply",
        ],
        "ready_now": False,
        "ready_after_operator_preflight": not actionable_blocked_gates,
        "first_blocked_gate": actionable_blocked_gates[0] if actionable_blocked_gates else "",
        "actionable_blocked_gates": actionable_blocked_gates,
        "gates": gates,
        "next_operator_action": (
            "run_phase0_schema_apply_in_confirmed_maintenance_window"
            if schema_ready_after_preflight
            else "resolve_phase0_schema_apply_blockers"
            if schema_applicable
            else readiness.get("next_action")
            or "review_daily_review_batch_plan_before_persistence"
        ),
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "creates_backup": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "historical_backfill": False,
            "restarts_launchd": False,
            "places_orders": False,
            "changes_parameters": False,
        },
    }


def build_confirmation_packet(
    db_path: Path,
    *,
    immutable: bool = False,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
    prebuilt_review_batch_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup = _backup_path(db, backup_root, generated_at)
    manifest = phase0_migration_manifest.build_manifest(db, immutable=immutable, generated_at=generated_at)
    apply_contract = phase0_schema_apply.build_apply_contract(
        db,
        immutable=False,
        backup_path=backup,
        allow_apply=False,
        confirmation_phrase="",
        writer_quiescent=False,
    )
    ddl_summary = dict(manifest.get("ddl_summary") or {})
    review_batch_plan_summary = _build_review_batch_plan_summary(
        db,
        lineage_decision_manifest=lineage_decision_manifest,
        prebuilt_review_batch_plan=prebuilt_review_batch_plan,
    )
    needs_apply = int(ddl_summary.get("ddl_count") or 0) > 0
    backup_cmd = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup)}\""
    apply_cmd = (
        f"python3 scripts/phase0_schema_apply.py --db {_q(db)} --apply "
        f"--confirm {phase0_schema_apply.CONFIRMATION_PHRASE} "
        f"--backup {_q(backup)} --writer-quiescent"
    )
    rollback_cmds = [
        "stop_or_quiesce_writers_before_rollback",
        f"cp -p {_q(backup)} {_q(db)}",
        "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only",
        "python3 scripts/check_ssot_alignment.py --immutable",
    ]
    preflight = [
        {
            "step": "review_manifest",
            "command": f"python3 scripts/phase0_migration_manifest.py --db {_q(db)} --immutable",
            "writes": False,
            "required": True,
        },
        {
            "step": "verify_ssot_alignment",
            "command": "python3 scripts/check_ssot_alignment.py --immutable",
            "writes": False,
            "required": True,
        },
        {
            "step": "review_daily_review_batch_plan",
            "command": "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 --max-items-output 20",
            "writes": False,
            "required": bool(needs_apply),
            "pass_condition": "operator understands settled scope, row previews, schema blocker, and lineage readiness before schema apply",
        },
        {
            "step": "dry_run_daily_review_persistence_apply_entrypoint",
            "command": "python3 scripts/daily_review_persistence_apply.py --since-days 45 --max-items-output 20",
            "writes": False,
            "required": bool(needs_apply),
            "pass_condition": "controlled persistence apply entrypoint returns a no-apply contract unless explicit apply preconditions are supplied",
        },
        {
            "step": "review_lineage_repair_candidates",
            "command": "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 --max-items-output 20",
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": "each proposed legacy lineage repair is explicitly confirmed or excluded before any daily review persistence apply",
        },
        {
            "step": "generate_lineage_decision_manifest_template",
            "command": (
                "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 "
                "--max-items-output 20 --emit-lineage-decision-manifest-template"
            ),
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": (
                "operator receives a no-apply manifest template; decisions remain undecided until explicitly edited"
            ),
        },
        {
            "step": "review_lineage_repair_final_confirmation_packet",
            "command": "python3 scripts/daily_review_lineage_repair_confirmation_packet.py --since-days 45 --max-items-output 20",
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": "operator can review manifest proposal, backup/apply commands, repair contract, and verification plan before any lineage repair apply",
        },
        {
            "step": "review_lineage_repair_operator_runbook",
            "command": "python3 scripts/daily_review_lineage_repair_operator_runbook.py --since-days 45 --max-items-output 20",
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": "operator sees compact manifest, backup, dry-run, risk, apply, verify, and rollback checklist before any materialization",
        },
        {
            "step": "recheck_lineage_decision_manifest_scope",
            "command": (
                "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 "
                "--max-items-output 20 --lineage-decision-manifest <manifest.json>"
            ),
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": (
                "batch plan shows expected effective/excluded scope and no unintended repair or persistence writes"
            ),
        },
        {
            "step": "review_lineage_repair_apply_contract",
            "command": (
                "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 "
                "--max-items-output 20 --lineage-decision-manifest <manifest.json> "
                "--emit-lineage-repair-apply-contract"
            ),
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": (
                "confirm_repair decisions produce a targeted no-apply merge-fill contract before controlled repair apply"
            ),
        },
        {
            "step": "review_lineage_repair_risk_audit",
            "command": (
                "python3 scripts/daily_review_lineage_repair_risk_audit.py --since-days 45 "
                "--max-items-output 20 --lineage-decision-manifest <manifest.json>"
            ),
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": (
                "risk audit shows merge-fill-only diffs, no target conflicts, and expected lineage blocker removal"
            ),
        },
        {
            "step": "dry_run_lineage_repair_apply_entrypoint",
            "command": "python3 scripts/daily_review_lineage_repair_apply.py --lineage-decision-manifest <manifest.json>",
            "writes": False,
            "required": bool(review_batch_plan_summary.get("lineage_repair_candidate_count")),
            "pass_condition": "controlled repair apply entrypoint returns no-apply contract unless explicit --apply preconditions are supplied",
        },
        {
            "step": "verify_writer_quiescence",
            "command": f"lsof {_q(db)}",
            "writes": False,
            "required": bool(needs_apply),
            "pass_condition": "no active writer process; read-only viewers must be stopped or explicitly accepted for maintenance window",
        },
        {
            "step": "create_fresh_backup",
            "command": backup_cmd,
            "writes": True,
            "writes_archive_db": False,
            "required": bool(needs_apply),
            "output": str(backup),
        },
    ]
    execution = [
        {
            "step": "apply_schema_only",
            "command": apply_cmd,
            "writes": bool(needs_apply),
            "writes_archive_db": bool(needs_apply),
            "historical_backfill": False,
            "restarts_launchd": False,
            "places_orders": False,
            "requires_user_confirmation": True,
            "confirmation_phrase": phase0_schema_apply.CONFIRMATION_PHRASE,
        }
    ]
    verification = [
        {
            "step": "schema_surface_strict",
            "command": "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only",
            "required": True,
        },
        {
            "step": "ssot_alignment",
            "command": "python3 scripts/check_ssot_alignment.py --immutable",
            "required": True,
        },
        {
            "step": "phase0_test_slice",
            "command": "python3 -m pytest tests/test_phase0_schema_probe.py tests/test_phase0_forward_schema_plan.py tests/test_phase0_migration_manifest.py tests/test_phase0_schema_apply.py",
            "required": True,
        },
        {
            "step": "review_persistence_writer_contract",
            "command": "python3 -m pytest tests/test_daily_review_persistence_writer.py tests/test_daily_review_persistence_apply.py tests/test_daily_weather_review_report.py tests/test_daily_review_persistence_batch_plan.py tests/test_daily_review_coverage_audit.py tests/test_daily_review_post_apply_acceptance_audit.py tests/test_posterior_ssot_fact_coverage_audit.py",
            "required": True,
            "reason": "planned daily review tables must match writer contract before production persistence is enabled",
        },
        {
            "step": "daily_review_report_persistence_plan_probe",
            "command": "python3 scripts/daily_weather_review_report.py --skip-ui-snapshot-audit --skip-replay-audit --limit 1",
            "required": False,
            "reason": "read-only report probe should show compact persistence plan; may require at least one fact row",
        },
        {
            "step": "daily_review_batch_plan_before_persistence",
            "command": "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 --max-items-output 20",
            "required": False,
            "reason": "operator review packet showing settled events and compact rows that would be persisted; no DB writes",
        },
        {
            "step": "daily_review_coverage_audit_after_persistence",
            "command": "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20",
            "required": False,
            "reason": "run after daily review records are persisted; before persistence it should expose missing planned table or missing review coverage",
        },
        {
            "step": "daily_review_post_apply_acceptance_after_persistence",
            "command": "python3 scripts/daily_review_post_apply_acceptance_audit.py --since-days 45 --max-items-output 20",
            "required": False,
            "reason": "single read-only closeout after review persistence; verifies coverage, review replay, posterior lineage, and UI snapshot lineage share the same fact snapshot",
        },
        {
            "step": "prior_layer_contract_after_forward_writes",
            "command": "python3 scripts/prior_layer_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
            "required": False,
            "reason": "read-only future writer projection; confirms prior_distribution/prior_meta/prior_na_reason coverage before weak/strong likelihood and posterior consumption",
        },
        {
            "step": "forecast_weak_likelihood_contract_after_forward_writes",
            "command": "python3 scripts/forecast_weak_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
            "required": False,
            "reason": "read-only future writer projection; confirms weak forecast source grouping, precision caps, and no hard/current/market source leakage",
        },
        {
            "step": "metar_strong_likelihood_contract_after_forward_writes",
            "command": "python3 scripts/metar_strong_likelihood_contract_audit.py --since-hours 168 --latest-only --active-only --limit 50 --max-items-output 20 --project-forward",
            "required": False,
            "reason": "read-only future writer projection; confirms METAR strong observation state, trajectory dedupe, hard mask, and no forecast/current source leakage",
        },
        {
            "step": "posterior_ssot_fact_coverage_after_forward_writes",
            "command": "python3 scripts/posterior_ssot_fact_coverage_audit.py --since-hours 168 --latest-only --limit 50 --max-items-output 20 --project-forward",
            "required": False,
            "reason": "read-only future writer projection; confirms forward posterior_ssot_fact/phase/beta coverage without historical backfill",
        },
        {
            "step": "full_strict_after_forward_writes",
            "command": "python3 scripts/phase0_schema_probe.py --immutable --strict",
            "required": False,
            "reason": "after schema apply, full strict also depends on future forward writes and planned table population",
        },
    ]
    manifest_checks = list(manifest.get("preflight_checks") or [])
    apply_checks = list(apply_contract.get("preflight_checks") or [])
    operator_apply_gate_summary = _build_operator_apply_gate_summary(
        needs_apply=needs_apply,
        manifest=manifest,
        apply_contract=apply_contract,
        review_batch_plan_summary=review_batch_plan_summary,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(manifest.get("ok", True) and apply_contract.get("ok", True)),
        "generated_at": generated_at,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "confirmation_packet_no_apply",
        "db": str(db),
        "needs_apply": bool(needs_apply),
        "ddl_summary": ddl_summary,
        "backup_path": str(backup),
        "backup_exists": backup.exists(),
        "apply_confirmation_phrase": phase0_schema_apply.CONFIRMATION_PHRASE,
        "manifest_schema": manifest.get("schema"),
        "apply_contract_schema": apply_contract.get("schema"),
        "review_persistence_writer_contract": dict(manifest.get("review_persistence_writer_contract") or {}),
        "review_batch_plan_summary": review_batch_plan_summary,
        "operator_apply_gate_summary": operator_apply_gate_summary,
        "lineage_repair_review_packet": {
            "schema": "phase0_lineage_repair_review_packet_v1",
            "read_only": True,
            "candidate_count": review_batch_plan_summary.get("lineage_repair_candidate_count"),
            "candidate_type_counts": dict(review_batch_plan_summary.get("lineage_repair_type_counts") or {}),
            "candidates": list(review_batch_plan_summary.get("lineage_repair_candidates") or []),
            "candidates_truncated": bool(review_batch_plan_summary.get("lineage_repair_candidates_truncated")),
            "operator_decision_packet": dict(
                review_batch_plan_summary.get("lineage_repair_operator_packet") or {}
            ),
            "decision_manifest_audit": dict(
                review_batch_plan_summary.get("lineage_decision_manifest_audit") or {}
            ),
            "repair_apply_contract": dict(
                review_batch_plan_summary.get("lineage_repair_apply_contract") or {}
            ),
            "apply_readiness": dict(
                review_batch_plan_summary.get("lineage_decision_apply_readiness") or {}
            ),
            "can_auto_apply": False,
            "required_operator_decision": review_batch_plan_summary.get("required_operator_decision"),
            "guards": {
                "writes_archive_db": False,
                "writes_event_fact_history": False,
                "writes_daily_weather_reviews": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "places_orders": False,
                "restarts_launchd": False,
            },
        },
        "manifest_apply_blockers": list(manifest.get("apply_blockers") or []),
        "apply_contract_blockers_without_confirmation": list(apply_contract.get("apply_blockers") or []),
        "preflight_check_summary": {
            "manifest_explicit_confirmation": dict(_check_by_name(manifest_checks, "explicit_user_confirmation")),
            "manifest_backup": dict(_check_by_name(manifest_checks, "fresh_archive_db_backup")),
            "manifest_writer_quiescence": dict(_check_by_name(manifest_checks, "writer_quiescence")),
            "apply_contract_confirmation": dict(_check_by_name(apply_checks, "confirmation_phrase")),
            "apply_contract_backup": dict(_check_by_name(apply_checks, "fresh_archive_db_backup")),
            "apply_contract_writer_quiescence": dict(_check_by_name(apply_checks, "writer_quiescence")),
        },
        "operator_packet": {
            "preflight": preflight,
            "execution": execution,
            "verification": verification,
            "rollback": [
                {
                    "step": "restore_backup_if_post_apply_verification_fails",
                    "commands": rollback_cmds,
                    "requires_manual_confirmation": True,
                }
            ],
        },
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "creates_backup": False,
            "historical_backfill": False,
            "restarts_launchd": False,
            "places_orders": False,
            "changes_parameters": False,
        },
        "blocked_operations": [
            "historical_backfill",
            "fact_json_backfill",
            "launchd_restart",
            "order_or_trade_decision_write",
            "parameter_change",
        ],
        "next_actions": [
            "review_packet_with_user_before_schema_apply",
            *(
                ["review_lineage_repair_candidates_before_daily_review_apply"]
                if review_batch_plan_summary.get("lineage_repair_candidate_count")
                else []
            ),
            "create_fresh_backup_in_confirmed_maintenance_window",
            "run_phase0_schema_apply_only_after_writer_quiescence",
        ]
        if needs_apply
        else ["phase0_schema_surface_already_ready"],
    }


def compact_confirmation_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    review_summary = _as_mapping(packet.get("review_batch_plan_summary"))
    lineage_packet = _as_mapping(packet.get("lineage_repair_review_packet"))
    operator_packet = _as_mapping(packet.get("operator_packet"))
    preflight = [
        dict(_as_mapping(item))
        for item in _as_list(operator_packet.get("preflight"))
        if isinstance(item, Mapping)
    ]
    execution = [
        dict(_as_mapping(item))
        for item in _as_list(operator_packet.get("execution"))
        if isinstance(item, Mapping)
    ]
    verification = [
        dict(_as_mapping(item))
        for item in _as_list(operator_packet.get("verification"))
        if isinstance(item, Mapping)
    ]

    def _steps(rows: list[Mapping[str, Any]], *, required_only: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            if required_only and not bool(row.get("required")):
                continue
            out.append(
                {
                    "step": row.get("step"),
                    "required": bool(row.get("required")),
                    "writes": bool(row.get("writes")),
                    "command": row.get("command"),
                }
            )
        return out

    return {
        "schema": f"{SCHEMA_VERSION}_summary",
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "confirmation_packet_summary_no_apply",
        "db": packet.get("db"),
        "needs_apply": bool(packet.get("needs_apply")),
        "ddl_summary": dict(_as_mapping(packet.get("ddl_summary"))),
        "backup_path": packet.get("backup_path"),
        "backup_exists": bool(packet.get("backup_exists")),
        "apply_confirmation_phrase": packet.get("apply_confirmation_phrase"),
        "manifest_apply_blockers": list(packet.get("manifest_apply_blockers") or []),
        "apply_contract_blockers_without_confirmation": list(
            packet.get("apply_contract_blockers_without_confirmation") or []
        ),
        "operator_apply_gate_summary": dict(_as_mapping(packet.get("operator_apply_gate_summary"))),
        "review_batch_plan_summary": {
            "available": bool(review_summary.get("available")),
            "batch_plan_source": review_summary.get("batch_plan_source"),
            "settled_event_count": review_summary.get("settled_event_count"),
            "effective_settled_event_count": review_summary.get("effective_settled_event_count"),
            "excluded_settled_event_count": review_summary.get("excluded_settled_event_count"),
            "planned_review_count": review_summary.get("planned_review_count"),
            "planned_calibration_record_count": review_summary.get("planned_calibration_record_count"),
            "planned_candidate_count": review_summary.get("planned_candidate_count"),
            "planned_parameter_event_count": review_summary.get("planned_parameter_event_count"),
            "can_apply_after_schema_and_confirmation": bool(
                review_summary.get("can_apply_after_schema_and_confirmation")
            ),
            "apply_blockers": list(review_summary.get("apply_blockers") or []),
            "lineage_repair_candidate_count": review_summary.get("lineage_repair_candidate_count"),
            "effective_lineage_repair_candidate_count": review_summary.get(
                "effective_lineage_repair_candidate_count"
            ),
            "lineage_repair_type_counts": dict(review_summary.get("lineage_repair_type_counts") or {}),
            "operator_decision_counts": dict(
                _as_mapping(review_summary.get("lineage_decision_apply_readiness")).get(
                    "operator_decision_counts"
                )
                or {}
            ),
            "next_boundary": review_summary.get("next_boundary"),
        },
        "lineage_repair_review_summary": {
            "candidate_count": lineage_packet.get("candidate_count"),
            "candidate_type_counts": dict(lineage_packet.get("candidate_type_counts") or {}),
            "candidates_truncated": bool(lineage_packet.get("candidates_truncated")),
            "can_auto_apply": bool(lineage_packet.get("can_auto_apply")),
            "required_operator_decision": lineage_packet.get("required_operator_decision"),
        },
        "required_preflight_steps": _steps(preflight, required_only=True),
        "execution_steps": _steps(execution),
        "required_verification_steps": _steps(verification, required_only=True),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "blocked_operations": list(packet.get("blocked_operations") or []),
        "next_actions": list(packet.get("next_actions") or []),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a no-apply Phase 0 schema apply confirmation packet.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or phase0_schema_probe.DEFAULT_DB))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--immutable", action="store_true", help="Open SQLite in immutable read-only mode while building the packet.")
    parser.add_argument(
        "--lineage-decision-manifest",
        default="",
        help="Read a no-apply lineage decision manifest JSON and include the same batch-plan scope audit.",
    )
    parser.add_argument("--summary", action="store_true", help="Print a compact operator summary instead of the full packet.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lineage_decision_manifest = daily_review_persistence_batch_plan.load_lineage_decision_manifest_file(
        args.lineage_decision_manifest
    )
    packet = build_confirmation_packet(
        Path(args.db),
        immutable=bool(args.immutable),
        backup_dir=Path(args.backup_dir),
        lineage_decision_manifest=lineage_decision_manifest,
    )
    if args.summary:
        packet = compact_confirmation_packet(packet)
    print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
