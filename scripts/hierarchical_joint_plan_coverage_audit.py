#!/usr/bin/env python3
"""Read-only coverage audit for the hierarchical joint likelihood development plan.

This audit composes the core probability release gate, AWC signal-to-order
timing replay, the daily review controlled apply packet, and daily review
post-apply acceptance into one plan-level surface. It never writes the database,
fetches external sources, restarts launchd, changes parameters, or places
orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_post_apply_acceptance_audit as review_acceptance_audit
from scripts import daily_review_controlled_apply_confirmation_packet as controlled_apply_packet
from scripts import daily_review_post_apply_resolution_confirmation_packet as resolution_confirmation_packet
from scripts import daily_review_production_apply_preflight_packet as production_preflight_packet
from scripts import current_latest_posterior_apply_runbook as posterior_apply_runbook
from scripts import fact_write_migration_plan
from scripts import hierarchical_core_integration_contract_audit
from scripts import hierarchical_probability_release_gate as probability_gate
from scripts import parameter_governance_surface_audit
from scripts import posterior_ssot_rematerialization_plan as rematerialization_plan
from scripts import phase0_schema_apply
from scripts import phase0_schema_probe
from scripts import production_identity_coverage_audit
from scripts import production_identity_closeout_confirmation_packet
from scripts import production_identity_closeout_operator_runbook
from scripts import production_identity_duplicate_fact_snapshot_apply_contract
from scripts import production_identity_merge_fill_apply_contract
from scripts import awc_hard_source_terminal_coverage_audit
from scripts import awc_legacy_terminal_closeout_decision_packet
from scripts import awc_legacy_terminal_closeout_apply_contract
from scripts import awc_legacy_terminal_closeout_operator_runbook
from scripts import hierarchical_joint_closeout_operator_runbook
from scripts import controlled_apply_maintenance_window_packet
from scripts import awc_signal_timing_contract_audit
from scripts import order_lineage_coverage_audit
from scripts import signal_to_order_safety_contract_audit
from scripts import ssot_chain_replay_audit as replay_audit


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "hierarchical_joint_plan_coverage_audit_v1"

AWC_SLO_REQUIRED_KEYS = (
    "awc_valid_time",
    "awc_hard_source_signal_at",
    "preparation_started_at",
    "pre_signal_prepared_at",
    "awc_pre_signal_deadline_at",
    "fact_written_at",
    "posterior_written_at",
    "decision_written_at",
    "terminal_at",
    "terminal_state",
    "terminal_reason",
    "orderbook_checked_at",
    "risk_checked_at",
    "idempotency_checked_at",
    "submit_requested_at",
    "exchange_ack_at",
    "order_submitted_at",
    "signal_to_order_slo_ms",
    "signal_to_order_slo_met",
    "signal_to_order_slo_violation_reason",
)

PHASE0_EXPECTED_APPLY_BLOCKERS = {
    "apply_flag",
    "confirmation_phrase",
    "fresh_archive_db_backup",
    "writer_quiescence",
}

PHASE0_REQUIRED_DAILY_REVIEW_TABLES = (
    "daily_weather_reviews",
    "daily_calibration_records",
    "bias_correction_candidates",
    "parameter_change_events",
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "writes_bias_correction_candidates": False,
        "writes_parameter_change_events": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _report_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "schema": str(report.get("schema") or ""),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "blockers": _as_list(report.get("blockers"))[:20],
        "warnings": _as_list(report.get("warnings"))[:20],
        "next_boundary": str(report.get("next_boundary") or ""),
    }
    for key in (
        "checks",
        "scope",
        "coverage",
        "counts",
        "snapshot",
        "source_counts",
        "production_coverage",
        "terminal_state_counts",
        "daily_weather_reviews_row_count",
        "duplicate",
        "merge_fill",
        "maintenance_window",
    ):
        value = report.get(key)
        if isinstance(value, Mapping):
            out[key] = dict(value)
    for key in (
        "signal_count",
        "accepted_signal_count",
        "submitted_signal_count",
        "settled_event_count",
        "reviewed_event_count",
        "missing_review_count",
    ):
        if key in report:
            out[key] = report.get(key)
    for key in (
        "schema_surface_ready",
        "phase0_ready",
        "daily_review_tables_materialized",
        "unexpected_apply_blockers",
        "missing_post_apply_verification_fragments",
    ):
        if key in report:
            out[key] = report.get(key)
    return out


def _gate_blockers_from_report(
    *,
    gate: str,
    report_name: str,
    report: Mapping[str, Any],
    require_ok: bool = True,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if require_ok and report.get("ok") is not True:
        blockers.append(
            {
                "blocker": f"{gate}_{report_name}_not_ok",
                "report": report_name,
                "schema": str(report.get("schema") or ""),
                "report_blockers": _as_list(report.get("blockers"))[:20],
            }
        )
    if report.get("read_only") is not True:
        blockers.append(
            {
                "blocker": f"{gate}_{report_name}_not_read_only",
                "report": report_name,
                "schema": str(report.get("schema") or ""),
            }
        )
    guards = report.get("guards")
    if isinstance(guards, Mapping):
        unsafe = {
            str(key): value
            for key, value in guards.items()
            if (
                str(key).startswith("writes_")
                or str(key)
                in {
                    "places_orders",
                    "restarts_launchd",
                    "historical_backfill",
                    "changes_parameters",
                    "fetches_external_weather_sources",
                }
            )
            and bool(value)
        }
        if unsafe:
            blockers.append(
                {
                    "blocker": f"{gate}_{report_name}_unsafe_guard_enabled",
                    "report": report_name,
                    "unsafe_guards": unsafe,
                }
            )
    return blockers


def _make_gate(
    *,
    gate: str,
    description: str,
    reports: Mapping[str, Mapping[str, Any]],
    continue_to: str,
    require_ok: bool = True,
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    for report_name, report in reports.items():
        blockers.extend(
            _gate_blockers_from_report(
                gate=gate,
                report_name=report_name,
                report=report,
                require_ok=require_ok,
            )
        )
    return {
        "gate": gate,
        "ok": not blockers,
        "description": description,
        "reports": {name: _report_summary(report) for name, report in reports.items()},
        "blockers": blockers,
        "next_boundary": continue_to if not blockers else f"fix_{gate}_before_continue",
    }


def _make_production_identity_gate(
    *,
    production_identity_report: Mapping[str, Any],
    duplicate_contract_report: Mapping[str, Any],
    merge_fill_contract_report: Mapping[str, Any],
    closeout_packet_report: Mapping[str, Any],
    closeout_operator_runbook_report: Mapping[str, Any],
    continue_to: str,
) -> dict[str, Any]:
    """Gate production identity through one formal closeout path.

    The standalone duplicate/merge-fill contracts are still reported, but before
    the reviewed manifest is materialized and duplicate repair is applied their
    `ok=false` state is an operator-pending diagnostic. The hard release blockers
    are the actual production identity coverage and the structural closeout
    packet that defines the only allowed production apply sequence.
    """

    gate = "production_identity_coverage_gate"
    reports = {
        "production_identity_coverage": production_identity_report,
        "production_identity_duplicate_apply_contract": duplicate_contract_report,
        "production_identity_merge_fill_apply_contract": merge_fill_contract_report,
        "production_identity_closeout_confirmation_packet": closeout_packet_report,
        "production_identity_closeout_operator_runbook": closeout_operator_runbook_report,
    }
    blockers: list[dict[str, Any]] = []
    blockers.extend(
        _gate_blockers_from_report(
            gate=gate,
            report_name="production_identity_coverage",
            report=production_identity_report,
            require_ok=True,
        )
    )
    blockers.extend(
        _gate_blockers_from_report(
            gate=gate,
            report_name="production_identity_closeout_confirmation_packet",
            report=closeout_packet_report,
            require_ok=True,
        )
    )
    blockers.extend(
        _gate_blockers_from_report(
            gate=gate,
            report_name="production_identity_closeout_operator_runbook",
            report=closeout_operator_runbook_report,
            require_ok=True,
        )
    )
    for report_name, report in (
        ("production_identity_duplicate_apply_contract", duplicate_contract_report),
        ("production_identity_merge_fill_apply_contract", merge_fill_contract_report),
    ):
        blockers.extend(
            _gate_blockers_from_report(
                gate=gate,
                report_name=report_name,
                report=report,
                require_ok=False,
            )
        )

    return {
        "gate": gate,
        "ok": not blockers,
        "description": (
            "Production identity coverage is the hard fact blocker; duplicate "
            "fact_snapshot and identity merge-fill contracts are diagnostics until "
            "the closeout confirmation packet materializes the reviewed manifest "
            "and enforces duplicate-before-merge-fill apply order."
        ),
        "reports": {name: _report_summary(report) for name, report in reports.items()},
        "blockers": blockers,
        "next_boundary": continue_to if not blockers else "fix_production_identity_coverage_before_continue",
    }


def _exception_report(schema: str, blocker: str, exc: BaseException) -> dict[str, Any]:
    return {
        "schema": schema,
        "ok": False,
        "read_only": True,
        "blockers": [
            {
                "blocker": blocker,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        ],
        "warnings": [],
        "guards": _guards(),
        "next_boundary": f"fix_{blocker}_before_plan_gate",
    }


def _sqlite_read_diagnostics(db_path: Path) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()

    def file_state(path: Path) -> dict[str, Any]:
        return {
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else None,
        }

    diagnostics: dict[str, Any] = {
        "schema": "sqlite_read_diagnostics_v1",
        "read_only": True,
        "db": file_state(db),
        "wal": file_state(Path(f"{db}-wal")),
        "shm": file_state(Path(f"{db}-shm")),
        "quick_check": {
            "status": "skipped_db_missing" if not db.exists() else "not_run",
            "result": "",
            "error": "",
        },
    }
    if not db.exists():
        return diagnostics
    try:
        uri = f"file:{db}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
        diagnostics["quick_check"] = {
            "status": "ok",
            "result": str(row[0] if row else ""),
            "error": "",
        }
    except Exception as exc:
        diagnostics["quick_check"] = {
            "status": "error",
            "result": "",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return diagnostics


def _awc_signal_to_order_contract_report() -> dict[str, Any]:
    try:
        contract = replay_audit.ssot_chain_replay_input_contract()
    except Exception as exc:
        return _exception_report(
            "awc_signal_to_order_contract_exception_v1",
            "awc_signal_to_order_contract_exception",
            exc,
        )
    slo = _as_mapping(contract.get("awc_signal_to_order_slo"))
    required_keys = {str(item) for item in _as_list(slo.get("required_timing_keys"))}
    blockers: list[str] = []
    missing_keys = [key for key in AWC_SLO_REQUIRED_KEYS if key not in required_keys]
    if missing_keys:
        blockers.append("awc_slo_required_timing_keys_missing")
    if str(slo.get("start_field") or "") != "awc_hard_source_signal_at":
        blockers.append("awc_slo_start_field_not_hard_source_signal")
    if str(slo.get("end_field") or "") != "order_submitted_at":
        blockers.append("awc_slo_end_field_not_order_submitted")
    try:
        slo_ms = int(slo.get("slo_ms") or 0)
    except Exception:
        slo_ms = 0
    if slo_ms != 3000:
        blockers.append("awc_slo_not_3000_ms")
    missing_policy = _as_mapping(contract.get("missing_policy"))
    for key in (
        "signal_to_order_timing_table_missing",
        "accepted_signal_missing_signal_to_order_ledger",
        "submitted_missing_orderbook_check",
        "submitted_missing_risk_check",
        "submitted_missing_idempotency_check",
        "submitted_precheck_after_submit_request",
    ):
        if key not in missing_policy:
            blockers.append(f"awc_slo_missing_policy_{key}")
    guards = dict(_as_mapping(contract.get("guards"))) or _guards()
    return {
        "schema": "awc_signal_to_order_contract_audit_v1",
        "ok": not blockers,
        "read_only": True,
        "contract_schema": str(contract.get("schema") or ""),
        "start_field": str(slo.get("start_field") or ""),
        "end_field": str(slo.get("end_field") or ""),
        "slo_ms": slo_ms,
        "required_timing_keys": sorted(required_keys),
        "missing_required_timing_keys": missing_keys,
        "blockers": blockers,
        "warnings": [],
        "guards": guards,
        "next_boundary": "run_ssot_chain_replay_gate" if not blockers else "fix_awc_signal_to_order_contract",
    }


def _awc_signal_to_order_replay_report(
    *,
    db_path: Path,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
    require_awc_signal_sample: bool,
) -> dict[str, Any]:
    try:
        report = replay_audit.audit(
            db_path=db_path,
            since_hours=float(since_hours),
            limit=int(replay_limit),
            max_signals_output=int(max_items_output),
            include_details=False,
            fail_legacy_artifacts=False,
        )
    except Exception as exc:
        return _exception_report(
            "awc_signal_to_order_replay_exception_v1",
            "awc_signal_to_order_replay_exception",
            exc,
        )
    report = dict(report)
    blockers = [str(item) for item in report.get("blockers") or []]
    warnings = [str(item) for item in report.get("warnings") or []]
    if not require_awc_signal_sample and "no_awc_hard_source_signals_found" in blockers:
        blockers = [item for item in blockers if item != "no_awc_hard_source_signals_found"]
        warnings.append("plan_gate_no_recent_awc_hard_source_signal_sample")
    report["schema"] = str(report.get("schema") or "ssot_chain_replay_audit_v1")
    report["blockers"] = sorted(set(blockers))
    report["warnings"] = sorted(set(warnings))
    report["ok"] = not report["blockers"]
    report["read_only"] = True
    report["release_gate_sample_required"] = bool(require_awc_signal_sample)
    return report


def _signal_to_order_safety_contract_report(*, root: Path) -> dict[str, Any]:
    try:
        report = signal_to_order_safety_contract_audit.build_audit(root=Path(root))
    except Exception as exc:
        return _exception_report(
            "signal_to_order_safety_contract_exception_v1",
            "signal_to_order_safety_contract_exception",
            exc,
        )
    report = dict(report)
    report["schema"] = str(report.get("schema") or "signal_to_order_safety_contract_audit_v1")
    report["read_only"] = True
    return report


def _order_lineage_coverage_report(
    *,
    db_path: Path,
    replay_limit: int,
    max_items_output: int,
    ignore_legacy_orders: bool,
    order_since_hours: float,
    ignore_orders_before: str,
) -> dict[str, Any]:
    try:
        report = order_lineage_coverage_audit.build_audit(
            db_path,
            limit=int(max_items_output),
            max_replay_samples=0,
            since_hours=24.0 * 14.0,
            ignore_legacy_orders=bool(ignore_legacy_orders),
            order_since_hours=float(order_since_hours),
            ignore_orders_before=str(ignore_orders_before or ""),
        )
    except Exception as exc:
        return _exception_report(
            "order_lineage_coverage_exception_v1",
            "order_lineage_coverage_exception",
            exc,
        )
    report = dict(report)
    report["schema"] = str(report.get("schema") or "order_lineage_coverage_audit_v1")
    report["read_only"] = True
    report["release_gate_replay_limit"] = int(replay_limit)
    report["release_gate_ignore_legacy_orders"] = bool(ignore_legacy_orders)
    report["release_gate_order_since_hours"] = float(order_since_hours)
    report["release_gate_ignore_orders_before"] = str(ignore_orders_before or "")
    return report


def _core_integration_contract_report() -> dict[str, Any]:
    try:
        report = hierarchical_core_integration_contract_audit.build_audit()
    except Exception as exc:
        return _exception_report(
            "hierarchical_core_integration_contract_exception_v1",
            "hierarchical_core_integration_contract_exception",
            exc,
        )
    report = dict(report)
    report["schema"] = str(report.get("schema") or "hierarchical_core_integration_contract_audit_v1")
    report["read_only"] = True
    return report


def _awc_signal_timing_contract_report(*, root: Path) -> dict[str, Any]:
    try:
        report = awc_signal_timing_contract_audit.build_audit(root=Path(root))
    except Exception as exc:
        return _exception_report(
            "awc_signal_timing_contract_exception_v1",
            "awc_signal_timing_contract_exception",
            exc,
        )
    report = dict(report)
    report["schema"] = str(report.get("schema") or "awc_signal_timing_contract_audit_v1")
    report["read_only"] = True
    return report


def _awc_hard_source_terminal_coverage_report(
    *,
    db_path: Path,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
    require_awc_signal_sample: bool,
    replay_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        report = awc_hard_source_terminal_coverage_audit.build_audit(
            db_path=Path(db_path),
            since_hours=max(float(since_hours), float(24 * 90)),
            limit=max(int(replay_limit), 1000),
            max_items_output=int(max_items_output),
            require_signal_sample=bool(require_awc_signal_sample),
            fail_legacy_artifacts=False,
            replay_report=replay_report,
        )
    except Exception as exc:
        return _exception_report(
            "awc_hard_source_terminal_coverage_exception_v1",
            "awc_hard_source_terminal_coverage_exception",
            exc,
        )
    report = dict(report)
    report["schema"] = str(report.get("schema") or "awc_hard_source_terminal_coverage_audit_v1")
    report["read_only"] = True
    return report


def _awc_legacy_terminal_closeout_packet_report(
    *,
    db_path: Path,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
) -> dict[str, Any]:
    try:
        report = awc_legacy_terminal_closeout_decision_packet.build_packet(
            db_path=Path(db_path),
            since_hours=max(float(since_hours), float(24 * 90)),
            limit=max(int(replay_limit), 500),
            max_items_output=int(max_items_output),
        )
        summary = awc_legacy_terminal_closeout_decision_packet.compact_packet(report)
    except Exception as exc:
        return _exception_report(
            "awc_legacy_terminal_closeout_decision_packet_exception_v1",
            "awc_legacy_terminal_closeout_decision_packet_exception",
            exc,
        )
    summary = dict(summary)
    summary["schema"] = str(summary.get("schema") or "awc_legacy_terminal_closeout_decision_packet_v1_summary")
    summary["read_only"] = True
    summary["_replay_report"] = report.get("_replay_report")
    summary["_closeout_packet"] = report
    return summary


def _awc_legacy_terminal_closeout_apply_contract_report(
    *,
    db_path: Path,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
    closeout_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        contract = awc_legacy_terminal_closeout_apply_contract.build_contract(
            db_path=Path(db_path),
            since_hours=max(float(since_hours), float(24 * 90)),
            limit=max(int(replay_limit), 500),
            max_items_output=int(max_items_output),
            closeout_packet=closeout_packet,
        )
        summary = awc_legacy_terminal_closeout_apply_contract.compact_contract(
            contract,
            max_items_output=int(max_items_output),
        )
    except Exception as exc:
        return _exception_report(
            "awc_legacy_terminal_closeout_apply_contract_exception_v1",
            "awc_legacy_terminal_closeout_apply_contract_exception",
            exc,
        )
    summary = dict(summary)
    summary["schema"] = str(summary.get("schema") or "awc_legacy_terminal_closeout_apply_contract_v1_summary")
    summary["read_only"] = True
    summary["_apply_contract"] = contract
    return summary


def _awc_legacy_terminal_closeout_operator_runbook_report(
    *,
    db_path: Path,
    since_hours: float,
    replay_limit: int,
    max_items_output: int,
    closeout_packet: Mapping[str, Any] | None = None,
    apply_contract_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        maintenance = controlled_apply_maintenance_window_packet.build_maintenance_window_packet(
            Path(db_path),
            max_items_output=int(max_items_output),
        )
        runbook = awc_legacy_terminal_closeout_operator_runbook.build_operator_runbook_from_components(
            db_path=Path(db_path),
            since_hours=max(float(since_hours), float(24 * 90)),
            limit=max(int(replay_limit), 500),
            max_items_output=int(max_items_output),
            closeout_packet=_as_mapping(closeout_packet),
            apply_contract_report=_as_mapping(apply_contract_report),
            maintenance_report=maintenance,
        )
        summary = awc_legacy_terminal_closeout_operator_runbook.compact_operator_runbook(runbook)
    except Exception as exc:
        return _exception_report(
            "awc_legacy_terminal_closeout_operator_runbook_exception_v1",
            "awc_legacy_terminal_closeout_operator_runbook_exception",
            exc,
        )
    summary = dict(summary)
    summary["schema"] = str(summary.get("schema") or "awc_legacy_terminal_closeout_operator_runbook_v1_summary")
    summary["read_only"] = True
    summary["_operator_runbook"] = runbook
    return summary


def _phase0_schema_apply_contract_report(*, db_path: Path) -> dict[str, Any]:
    try:
        contract = phase0_schema_apply.build_apply_contract(
            Path(db_path),
            immutable=False,
            backup_path=None,
            allow_apply=False,
            confirmation_phrase="",
            writer_quiescent=False,
        )
        probe = phase0_schema_probe.run_probe(Path(db_path), immutable=True)
    except Exception as exc:
        report = _exception_report(
            "phase0_schema_apply_contract_exception_v1",
            "phase0_schema_apply_contract_exception",
            exc,
        )
        report["db_diagnostics"] = _sqlite_read_diagnostics(Path(db_path))
        return report

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(contract.get("guards")) or _guards()
    ddl = [
        dict(_as_mapping(item))
        for item in _as_list(contract.get("ddl"))
        if isinstance(item, Mapping)
    ]
    ddl_tables = {str(item.get("table") or "") for item in ddl}
    planned_tables = _as_mapping(probe.get("planned_tables"))
    planned_table_status: dict[str, dict[str, Any]] = {}
    for table in PHASE0_REQUIRED_DAILY_REVIEW_TABLES:
        table_probe = _as_mapping(planned_tables.get(table))
        exists = bool(table_probe.get("exists"))
        ddl_has_table = table in ddl_tables
        missing_columns = [
            str(column)
            for column in _as_list(table_probe.get("missing_required_columns"))
            if str(column or "").strip()
        ]
        planned_table_status[table] = {
            "exists": exists,
            "ddl_planned": ddl_has_table,
            "missing_required_columns": missing_columns,
        }
        if not exists and not ddl_has_table:
            blockers.append(f"{table}_missing_without_create_table_ddl")
        if exists and missing_columns and not ddl_has_table:
            blockers.append(f"{table}_missing_columns_without_forward_ddl")
        if not exists:
            warnings.append(f"{table}_not_materialized_in_archive_db")

    if contract.get("read_only") is not True:
        blockers.append("phase0_schema_apply_contract_not_read_only")
    if contract.get("dry_run") is not True:
        blockers.append("phase0_schema_apply_contract_not_dry_run")
    if contract.get("applied"):
        blockers.append("phase0_schema_apply_contract_already_applied")
    if contract.get("apply_supported") is not True:
        blockers.append("phase0_schema_apply_contract_apply_not_supported")
    unexpected_apply_blockers = [
        str(item)
        for item in _as_list(contract.get("apply_blockers"))
        if str(item or "").strip() and str(item) not in PHASE0_EXPECTED_APPLY_BLOCKERS
    ]
    if unexpected_apply_blockers:
        blockers.append("phase0_schema_apply_contract_unexpected_apply_blockers")

    verification_commands = [str(item) for item in _as_list(contract.get("post_apply_verification_commands"))]
    required_command_fragments = (
        "phase0_schema_probe.py --immutable --strict --schema-only",
        "check_ssot_alignment.py --immutable",
        "test_daily_review_persistence_writer.py",
        "test_daily_review_persistence_apply.py",
        "test_daily_review_persistence_batch_plan.py",
        "test_daily_review_coverage_audit.py",
        "test_daily_review_post_apply_acceptance_audit.py",
    )
    missing_command_fragments = [
        fragment
        for fragment in required_command_fragments
        if not any(fragment in command for command in verification_commands)
    ]
    if missing_command_fragments:
        blockers.append("phase0_schema_apply_post_verify_commands_incomplete")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("phase0_schema_apply_contract_unsafe_guard_enabled")

    ddl_summary = _as_mapping(contract.get("ddl_summary"))
    return {
        "schema": "phase0_schema_apply_contract_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "phase0_schema_apply_contract_read_only_gate",
        "contract_schema": str(contract.get("schema") or ""),
        "probe_schema": str(probe.get("schema") or ""),
        "schema_surface_ready": bool(probe.get("schema_surface_ready")),
        "phase0_ready": bool(probe.get("phase0_ready")),
        "daily_review_tables_materialized": all(
            bool(item.get("exists")) for item in planned_table_status.values()
        ),
        "planned_table_status": planned_table_status,
        "ddl_summary": dict(ddl_summary),
        "expected_apply_blockers": sorted(PHASE0_EXPECTED_APPLY_BLOCKERS),
        "apply_blockers_without_confirmation": [
            str(item) for item in _as_list(contract.get("apply_blockers"))
        ],
        "unexpected_apply_blockers": unexpected_apply_blockers,
        "missing_post_apply_verification_fragments": missing_command_fragments,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "daily_review_post_apply_gate_after_controlled_schema_and_persistence_apply"
            if not blockers
            else "fix_phase0_schema_apply_contract_before_review_materialization"
        ),
    }


def _production_identity_coverage_report(
    *,
    db_path: Path,
    max_items_output: int,
) -> dict[str, Any]:
    try:
        report = production_identity_coverage_audit.audit(
            Path(db_path),
            immutable=False,
            sample_limit=int(max_items_output),
        )
        summary = production_identity_coverage_audit.compact_report(
            report,
            max_tables=int(max_items_output),
        )
    except Exception as exc:
        return _exception_report(
            "production_identity_coverage_exception_v1",
            "production_identity_coverage_exception",
            exc,
        )

    blockers: list[str] = []
    warnings = [str(item) for item in _as_list(summary.get("warnings")) if str(item or "").strip()]
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("production_identity_coverage_not_read_only")
    if summary.get("ok") is not True:
        blockers.append("production_identity_coverage_not_ok")
    if summary.get("schema_identity_ready") is not True:
        blockers.append("production_identity_schema_not_ready")
    if summary.get("production_row_identity_ready") is not True:
        blockers.append("production_identity_rows_not_ready")
    for blocker in _as_list(summary.get("blockers")):
        if isinstance(blocker, Mapping):
            text = str(blocker.get("blocker") or "").strip()
            if text:
                blockers.append(text)
        elif str(blocker or "").strip():
            blockers.append(str(blocker))
    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("production_identity_coverage_unsafe_guard_enabled")

    return {
        "schema": "production_identity_coverage_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "production_identity_coverage_read_only_gate",
        "source_schema": str(summary.get("source_schema") or ""),
        "schema_identity_ready": bool(summary.get("schema_identity_ready")),
        "production_row_identity_ready": bool(summary.get("production_row_identity_ready")),
        "table_count": int(summary.get("table_count") or 0),
        "ready_table_count": int(summary.get("ready_table_count") or 0),
        "gap_table_count": int(summary.get("gap_table_count") or 0),
        "gap_tables": list(summary.get("gap_tables") or []),
        "tables": dict(_as_mapping(summary.get("tables"))),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "production_identity_coverage_ready"
            if not blockers
            else "fix_production_identity_coverage_before_closeout"
        ),
    }


def _production_identity_duplicate_apply_contract_report(
    *,
    db_path: Path,
    max_items_output: int,
) -> dict[str, Any]:
    try:
        contract = production_identity_duplicate_fact_snapshot_apply_contract.build_contract(
            db_path,
            immutable=True,
            max_items_output=int(max_items_output),
        )
        summary = production_identity_duplicate_fact_snapshot_apply_contract.compact_contract(
            contract,
            max_items_output=int(max_items_output),
        )
    except Exception as exc:
        return _exception_report(
            "production_identity_duplicate_apply_contract_exception_v1",
            "production_identity_duplicate_apply_contract_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("production_identity_duplicate_apply_contract_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("production_identity_duplicate_apply_contract_not_dry_run")
    if summary.get("ok") is not True:
        blockers.append("production_identity_duplicate_apply_contract_not_ready")
    for blocker in _as_list(summary.get("blockers")):
        if str(blocker or "").strip():
            blockers.append(str(blocker))
    operator_contract = _as_mapping(summary.get("operator_contract"))
    if int(summary.get("duplicate_row_count") or 0) > 0:
        required_fragments = (
            "production_identity_duplicate_fact_snapshot_packet.py",
            "production_identity_merge_fill_plan.py",
            "production_identity_coverage_audit.py",
        )
        post_apply_commands = " ".join(
            str(item) for item in _as_list(operator_contract.get("post_apply_verification_commands"))
        )
        for fragment in required_fragments:
            if fragment not in post_apply_commands:
                blockers.append("production_identity_duplicate_apply_contract_post_apply_verification_incomplete")
                break
        if operator_contract.get("confirmation_phrase") != production_identity_duplicate_fact_snapshot_apply_contract.CONFIRMATION_PHRASE:
            blockers.append("production_identity_duplicate_apply_contract_confirmation_phrase_missing")
        if operator_contract.get("requires_backup") is not True:
            blockers.append("production_identity_duplicate_apply_contract_backup_not_required")
        if operator_contract.get("requires_writer_quiescence") is not True:
            blockers.append("production_identity_duplicate_apply_contract_writer_quiescence_not_required")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("production_identity_duplicate_apply_contract_unsafe_guard_enabled")
    if int(summary.get("duplicate_row_count") or 0) > 0 and summary.get("ok") is not True:
        warnings.append("review_duplicate_decision_manifest_before_identity_merge_fill")

    return {
        "schema": "production_identity_duplicate_apply_contract_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "production_identity_duplicate_apply_contract_read_only_gate",
        "source_schema": str(summary.get("source_schema") or ""),
        "duplicate_group_count": int(summary.get("duplicate_group_count") or 0),
        "duplicate_row_count": int(summary.get("duplicate_row_count") or 0),
        "operation_count": int(summary.get("operation_count") or 0),
        "decision_manifest_audit": dict(_as_mapping(summary.get("decision_manifest_audit"))),
        "operator_contract": dict(operator_contract),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "production_identity_duplicate_apply_contract_ready"
            if not blockers
            else "review_duplicate_decision_manifest_before_identity_merge_fill"
        ),
    }


def _production_identity_merge_fill_apply_contract_report(
    *,
    db_path: Path,
    max_items_output: int,
    coverage_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        contract = production_identity_merge_fill_apply_contract.build_contract(
            db_path,
            limit=1000000,
            max_items_output=int(max_items_output),
            coverage_report=coverage_report,
        )
        summary = production_identity_merge_fill_apply_contract.compact_contract(
            contract,
            max_items_output=int(max_items_output),
        )
    except Exception as exc:
        return _exception_report(
            "production_identity_merge_fill_apply_contract_exception_v1",
            "production_identity_merge_fill_apply_contract_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("production_identity_merge_fill_apply_contract_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("production_identity_merge_fill_apply_contract_not_dry_run")
    if summary.get("ok") is not True:
        blockers.append("production_identity_merge_fill_apply_contract_not_ready")
    for blocker in _as_list(summary.get("blockers")):
        if str(blocker or "").strip():
            blockers.append(str(blocker))
    operator_contract = _as_mapping(summary.get("operator_contract"))
    if int(summary.get("operation_count") or 0) > 0:
        if operator_contract.get("confirmation_phrase") != production_identity_merge_fill_apply_contract.CONFIRMATION_PHRASE:
            blockers.append("production_identity_merge_fill_apply_contract_confirmation_phrase_missing")
        if operator_contract.get("requires_backup") is not True:
            blockers.append("production_identity_merge_fill_apply_contract_backup_not_required")
        if operator_contract.get("requires_writer_quiescence") is not True:
            blockers.append("production_identity_merge_fill_apply_contract_writer_quiescence_not_required")
    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("production_identity_merge_fill_apply_contract_unsafe_guard_enabled")
    if summary.get("ok") is not True:
        warnings.append("run_duplicate_repair_then_review_identity_merge_fill_contract")

    return {
        "schema": "production_identity_merge_fill_apply_contract_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "production_identity_merge_fill_apply_contract_read_only_gate",
        "source_schema": str(summary.get("source_schema") or ""),
        "operation_count": int(summary.get("operation_count") or 0),
        "candidate_count": int(summary.get("candidate_count") or 0),
        "safe_candidate_count": int(summary.get("safe_candidate_count") or 0),
        "blocked_candidate_count": int(summary.get("blocked_candidate_count") or 0),
        "duplicate_fact_snapshot_precondition": dict(
            _as_mapping(summary.get("duplicate_fact_snapshot_precondition"))
        ),
        "contract_summary": dict(summary),
        "operator_contract": dict(operator_contract),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "production_identity_merge_fill_apply_contract_ready"
            if not blockers
            else "resolve_identity_merge_fill_contract_blockers_before_apply"
        ),
    }


def _production_identity_closeout_confirmation_packet_report(
    *,
    db_path: Path,
    max_items_output: int,
    merge_fill_contract_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        packet = production_identity_closeout_confirmation_packet.build_confirmation_packet(
            db_path,
            max_items_output=int(max_items_output),
            limit=1000000,
            precomputed_merge_fill_contract=merge_fill_contract_summary,
        )
        summary = production_identity_closeout_confirmation_packet.compact_confirmation_packet(packet)
    except Exception as exc:
        return _exception_report(
            "production_identity_closeout_confirmation_packet_exception_v1",
            "production_identity_closeout_confirmation_packet_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("production_identity_closeout_packet_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("production_identity_closeout_packet_not_dry_run")

    boundary = _as_mapping(summary.get("boundary"))
    expected_boundary = {
        "packet_executes_apply": False,
        "packet_creates_backup": False,
        "packet_writes_manifest": False,
        "packet_stops_or_restarts_services": False,
        "duplicate_apply_must_precede_identity_merge_fill": True,
        "identity_audit_index_apply_must_precede_identity_coverage_verification": True,
        "single_shared_backup_for_all_identity_apply_stages": True,
        "temp_drill_must_precede_production_apply": True,
        "production_apply_requires_operator_confirmation": True,
    }
    for key, expected in expected_boundary.items():
        if bool(boundary.get(key)) is not expected:
            blockers.append(f"production_identity_closeout_boundary_{key}_mismatch")

    steps = {
        str(_as_mapping(row).get("step") or ""): _as_mapping(row)
        for row in _as_list(summary.get("control_sequence"))
    }
    sequence = [_as_mapping(row) for row in _as_list(summary.get("control_sequence"))]
    required_steps = (
        "pause_writer_services",
        "materialize_reviewed_duplicate_manifest",
        "audit_reviewed_duplicate_manifest",
        "review_duplicate_apply_contract",
        "review_identity_audit_index_contract",
        "run_identity_audit_index_temp_drill",
        "run_identity_closeout_temp_drill",
        "create_shared_fresh_backup",
        "verify_writer_quiescence",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "verify_duplicate_repair",
        "review_identity_merge_fill_contract_after_duplicate_repair",
        "apply_identity_merge_fill",
        "verify_identity_coverage",
        "verify_full_joint_gate",
    )
    missing_steps = [step for step in required_steps if step not in steps]
    if missing_steps:
        blockers.append("production_identity_closeout_packet_missing_required_steps")

    def first_index(step_name: str) -> int | None:
        for idx, row in enumerate(sequence):
            if str(row.get("step") or "") == step_name:
                return idx
        return None

    def require_before(left: str, right: str, blocker: str) -> None:
        left_idx = first_index(left)
        right_idx = first_index(right)
        if left_idx is None or right_idx is None or left_idx >= right_idx:
            blockers.append(blocker)

    if not missing_steps:
        ordered_pairs = (
            (
                "pause_writer_services",
                "materialize_reviewed_duplicate_manifest",
                "production_identity_closeout_pause_not_before_manifest",
            ),
            (
                "materialize_reviewed_duplicate_manifest",
                "audit_reviewed_duplicate_manifest",
                "production_identity_closeout_manifest_not_before_audit",
            ),
            (
                "audit_reviewed_duplicate_manifest",
                "review_duplicate_apply_contract",
                "production_identity_closeout_manifest_audit_not_before_duplicate_contract",
            ),
            (
                "review_duplicate_apply_contract",
                "review_identity_audit_index_contract",
                "production_identity_closeout_duplicate_contract_not_before_index_contract",
            ),
            (
                "review_identity_audit_index_contract",
                "run_identity_audit_index_temp_drill",
                "production_identity_closeout_index_contract_not_before_index_temp_drill",
            ),
            (
                "run_identity_audit_index_temp_drill",
                "run_identity_closeout_temp_drill",
                "production_identity_closeout_index_temp_drill_not_before_merge_temp_drill",
            ),
            (
                "run_identity_closeout_temp_drill",
                "create_shared_fresh_backup",
                "production_identity_closeout_temp_drill_not_before_backup",
            ),
            (
                "create_shared_fresh_backup",
                "verify_writer_quiescence",
                "production_identity_closeout_backup_not_before_quiescence_verify",
            ),
            (
                "verify_writer_quiescence",
                "apply_identity_audit_indexes",
                "production_identity_closeout_quiescence_not_before_index_apply",
            ),
            (
                "apply_identity_audit_indexes",
                "apply_duplicate_fact_snapshot_repair",
                "production_identity_closeout_index_apply_not_before_duplicate_apply",
            ),
            (
                "apply_duplicate_fact_snapshot_repair",
                "verify_duplicate_repair",
                "production_identity_closeout_duplicate_apply_not_before_verify",
            ),
            (
                "verify_duplicate_repair",
                "review_identity_merge_fill_contract_after_duplicate_repair",
                "production_identity_closeout_duplicate_verify_not_before_merge_contract",
            ),
            (
                "review_identity_merge_fill_contract_after_duplicate_repair",
                "apply_identity_merge_fill",
                "production_identity_closeout_merge_contract_not_before_merge_apply",
            ),
            (
                "apply_identity_merge_fill",
                "verify_identity_coverage",
                "production_identity_closeout_merge_apply_not_before_coverage",
            ),
            (
                "verify_identity_coverage",
                "verify_full_joint_gate",
                "production_identity_closeout_coverage_not_before_joint_gate",
            ),
        )
        for left, right, blocker in ordered_pairs:
            require_before(left, right, blocker)

    shared_backup_path = str(summary.get("shared_backup_path") or "")
    duplicate_manifest_path = str(summary.get("duplicate_manifest_path") or "")
    if not shared_backup_path:
        blockers.append("production_identity_closeout_shared_backup_path_missing")
    if not duplicate_manifest_path:
        blockers.append("production_identity_closeout_duplicate_manifest_path_missing")

    command_checks = {
        "materialize_reviewed_duplicate_manifest": (
            "--emit-reviewed-manifest-json",
            duplicate_manifest_path,
        ),
        "audit_reviewed_duplicate_manifest": (
            "production_identity_duplicate_fact_snapshot_packet.py",
            "--decision-manifest",
            duplicate_manifest_path,
        ),
        "review_duplicate_apply_contract": (
            "production_identity_duplicate_fact_snapshot_apply_contract.py",
            "--decision-manifest",
            duplicate_manifest_path,
        ),
        "review_identity_audit_index_contract": (
            "production_identity_index_apply_contract.py",
            "--summary",
        ),
        "run_identity_audit_index_temp_drill": (
            "production_identity_index_temp_drill.py",
            "--summary",
        ),
        "run_identity_closeout_temp_drill": (
            "production_identity_merge_fill_temp_drill.py",
            "--summary",
            "--copy-mode",
            "focused",
        ),
        "create_shared_fresh_backup": ("sqlite3", ".backup", shared_backup_path),
        "verify_writer_quiescence": ("controlled_apply_maintenance_window_packet.py", "--summary"),
        "apply_identity_audit_indexes": (
            "production_identity_index_apply.py",
            "--apply",
            production_identity_closeout_confirmation_packet.index_apply.CONFIRMATION_PHRASE,
            "--backup",
            shared_backup_path,
            "--writer-quiescent",
        ),
        "apply_duplicate_fact_snapshot_repair": (
            "production_identity_duplicate_fact_snapshot_apply.py",
            "--apply",
            production_identity_closeout_confirmation_packet.duplicate_apply.CONFIRMATION_PHRASE,
            "--backup",
            shared_backup_path,
            "--writer-quiescent",
            "--decision-manifest",
            duplicate_manifest_path,
        ),
        "review_identity_merge_fill_contract_after_duplicate_repair": (
            "production_identity_merge_fill_apply_contract.py",
            "--duplicate-decision-manifest",
            duplicate_manifest_path,
        ),
        "apply_identity_merge_fill": (
            "production_identity_merge_fill_apply.py",
            "--apply",
            production_identity_closeout_confirmation_packet.merge_fill_apply.CONFIRMATION_PHRASE,
            "--backup",
            shared_backup_path,
            "--writer-quiescent",
            "--duplicate-decision-manifest",
            duplicate_manifest_path,
        ),
        "verify_identity_coverage": ("production_identity_coverage_audit.py", "--summary"),
        "verify_full_joint_gate": ("hierarchical_joint_plan_coverage_audit.py", "--summary"),
    }
    missing_command_fragments: list[str] = []
    for step, fragments in command_checks.items():
        command = str(_as_mapping(steps.get(step)).get("command") or "")
        for fragment in fragments:
            if fragment and fragment not in command:
                missing_command_fragments.append(f"{step}:{fragment}")
    if missing_command_fragments:
        blockers.append("production_identity_closeout_packet_commands_incomplete")

    confirmations = _as_mapping(summary.get("confirmation_phrases"))
    if confirmations.get("identity_audit_indexes") != (
        production_identity_closeout_confirmation_packet.index_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("production_identity_closeout_identity_audit_index_confirmation_phrase_missing")
    if confirmations.get("duplicate_fact_snapshot_repair") != (
        production_identity_closeout_confirmation_packet.duplicate_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("production_identity_closeout_duplicate_confirmation_phrase_missing")
    if confirmations.get("identity_merge_fill") != (
        production_identity_closeout_confirmation_packet.merge_fill_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("production_identity_closeout_merge_fill_confirmation_phrase_missing")

    index_apply_step = _as_mapping(steps.get("apply_identity_audit_indexes"))
    duplicate_apply_step = _as_mapping(steps.get("apply_duplicate_fact_snapshot_repair"))
    merge_apply_step = _as_mapping(steps.get("apply_identity_merge_fill"))
    if index_apply_step.get("confirmation_phrase") != (
        production_identity_closeout_confirmation_packet.index_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("production_identity_closeout_identity_audit_index_apply_step_confirmation_missing")
    if duplicate_apply_step.get("confirmation_phrase") != (
        production_identity_closeout_confirmation_packet.duplicate_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("production_identity_closeout_duplicate_apply_step_confirmation_missing")
    if merge_apply_step.get("confirmation_phrase") != (
        production_identity_closeout_confirmation_packet.merge_fill_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("production_identity_closeout_merge_apply_step_confirmation_missing")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("production_identity_closeout_packet_unsafe_guard_enabled")

    for blocker in _as_list(summary.get("hard_blockers")):
        if str(blocker or "").strip():
            warnings.append(f"production_identity_closeout_operator_pending:{blocker}")

    duplicate = _as_mapping(summary.get("duplicate"))
    identity_audit_indexes = _as_mapping(summary.get("identity_audit_indexes"))
    merge_fill = _as_mapping(summary.get("merge_fill"))
    maintenance = _as_mapping(summary.get("maintenance_window"))
    return {
        "schema": "production_identity_closeout_confirmation_packet_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "production_identity_closeout_confirmation_packet_read_only_gate",
        "_closeout_packet": packet,
        "packet_schema": str(summary.get("source_schema") or ""),
        "shared_backup_path": shared_backup_path,
        "duplicate_manifest_path": duplicate_manifest_path,
        "duplicate": dict(duplicate),
        "identity_audit_indexes": dict(identity_audit_indexes),
        "merge_fill": dict(merge_fill),
        "maintenance_window": dict(maintenance),
        "missing_required_steps": missing_steps,
        "missing_command_fragments": missing_command_fragments,
        "hard_blockers": list(summary.get("hard_blockers") or []),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "operator_can_use_production_identity_closeout_packet_after_confirmation"
            if not blockers
            else "fix_production_identity_closeout_packet_before_operator_confirmation"
        ),
    }


def _production_identity_closeout_operator_runbook_report(
    *,
    db_path: Path,
    max_items_output: int,
    closeout_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if closeout_packet:
            runbook = production_identity_closeout_operator_runbook.build_operator_runbook_from_packet(
                closeout_packet
            )
        else:
            runbook = production_identity_closeout_operator_runbook.build_operator_runbook(
                db_path,
                max_items_output=int(max_items_output),
                limit=1000000,
            )
        summary = production_identity_closeout_operator_runbook.compact_operator_runbook(runbook)
    except Exception as exc:
        return _exception_report(
            "production_identity_closeout_operator_runbook_exception_v1",
            "production_identity_closeout_operator_runbook_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("production_identity_closeout_operator_runbook_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("production_identity_closeout_operator_runbook_not_dry_run")

    sequence = [_as_mapping(row) for row in _as_list(summary.get("operator_sequence"))]
    steps = {str(row.get("step") or ""): row for row in sequence}
    sequence_steps = [str(row.get("step") or "") for row in sequence]
    required_steps = (
        "pause_writer_services",
        "materialize_reviewed_duplicate_manifest",
        "audit_reviewed_duplicate_manifest",
        "review_duplicate_apply_contract",
        "review_identity_audit_index_contract",
        "run_identity_audit_index_temp_drill",
        "run_identity_closeout_temp_drill",
        "create_shared_fresh_backup",
        "verify_writer_quiescence",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "verify_duplicate_repair",
        "review_identity_merge_fill_contract_after_duplicate_repair",
        "apply_identity_merge_fill",
        "verify_identity_coverage",
        "verify_full_joint_gate",
        "resume_writer_services",
    )
    missing_steps = [step for step in required_steps if step not in steps]
    if missing_steps:
        blockers.append("production_identity_closeout_operator_runbook_missing_required_steps")

    def first_index(step_name: str) -> int | None:
        for idx, row in enumerate(sequence):
            if str(row.get("step") or "") == step_name:
                return idx
        return None

    def require_before(left: str, right: str, blocker: str) -> None:
        left_idx = first_index(left)
        right_idx = first_index(right)
        if left_idx is None or right_idx is None or left_idx >= right_idx:
            blockers.append(blocker)

    if not missing_steps:
        ordered_pairs = (
            (
                "pause_writer_services",
                "materialize_reviewed_duplicate_manifest",
                "production_identity_closeout_runbook_pause_not_before_manifest",
            ),
            (
                "materialize_reviewed_duplicate_manifest",
                "audit_reviewed_duplicate_manifest",
                "production_identity_closeout_runbook_manifest_not_before_audit",
            ),
            (
                "audit_reviewed_duplicate_manifest",
                "review_duplicate_apply_contract",
                "production_identity_closeout_runbook_manifest_audit_not_before_duplicate_contract",
            ),
            (
                "review_duplicate_apply_contract",
                "review_identity_audit_index_contract",
                "production_identity_closeout_runbook_duplicate_contract_not_before_index_contract",
            ),
            (
                "review_identity_audit_index_contract",
                "run_identity_audit_index_temp_drill",
                "production_identity_closeout_runbook_index_contract_not_before_index_temp_drill",
            ),
            (
                "run_identity_audit_index_temp_drill",
                "run_identity_closeout_temp_drill",
                "production_identity_closeout_runbook_index_temp_drill_not_before_merge_temp_drill",
            ),
            (
                "run_identity_closeout_temp_drill",
                "create_shared_fresh_backup",
                "production_identity_closeout_runbook_temp_drill_not_before_backup",
            ),
            (
                "create_shared_fresh_backup",
                "verify_writer_quiescence",
                "production_identity_closeout_runbook_backup_not_before_quiescence_verify",
            ),
            (
                "verify_writer_quiescence",
                "apply_identity_audit_indexes",
                "production_identity_closeout_runbook_quiescence_not_before_index_apply",
            ),
            (
                "apply_identity_audit_indexes",
                "apply_duplicate_fact_snapshot_repair",
                "production_identity_closeout_runbook_index_apply_not_before_duplicate_apply",
            ),
            (
                "apply_duplicate_fact_snapshot_repair",
                "verify_duplicate_repair",
                "production_identity_closeout_runbook_duplicate_apply_not_before_verify",
            ),
            (
                "verify_duplicate_repair",
                "review_identity_merge_fill_contract_after_duplicate_repair",
                "production_identity_closeout_runbook_duplicate_verify_not_before_merge_contract",
            ),
            (
                "review_identity_merge_fill_contract_after_duplicate_repair",
                "apply_identity_merge_fill",
                "production_identity_closeout_runbook_merge_contract_not_before_merge_apply",
            ),
            (
                "apply_identity_merge_fill",
                "verify_identity_coverage",
                "production_identity_closeout_runbook_merge_apply_not_before_coverage",
            ),
            (
                "verify_identity_coverage",
                "verify_full_joint_gate",
                "production_identity_closeout_runbook_coverage_not_before_joint_gate",
            ),
            (
                "verify_full_joint_gate",
                "resume_writer_services",
                "production_identity_closeout_runbook_resume_not_after_joint_gate",
            ),
        )
        for left, right, blocker in ordered_pairs:
            require_before(left, right, blocker)

    artifacts = _as_mapping(summary.get("artifacts"))
    shared_backup_path = str(artifacts.get("shared_backup_path") or "")
    duplicate_manifest_path = str(artifacts.get("duplicate_manifest_path") or "")
    if not shared_backup_path:
        blockers.append("production_identity_closeout_runbook_shared_backup_path_missing")
    if not duplicate_manifest_path:
        blockers.append("production_identity_closeout_runbook_duplicate_manifest_path_missing")

    expected_apply_steps = {
        "apply_identity_audit_indexes": (
            production_identity_closeout_confirmation_packet.index_apply.CONFIRMATION_PHRASE,
            "production_identity_index_apply.py",
        ),
        "apply_duplicate_fact_snapshot_repair": (
            production_identity_closeout_confirmation_packet.duplicate_apply.CONFIRMATION_PHRASE,
            "production_identity_duplicate_fact_snapshot_apply.py",
        ),
        "apply_identity_merge_fill": (
            production_identity_closeout_confirmation_packet.merge_fill_apply.CONFIRMATION_PHRASE,
            "production_identity_merge_fill_apply.py",
        ),
    }
    for step_name, (confirmation_phrase, command_fragment) in expected_apply_steps.items():
        row = _as_mapping(steps.get(step_name))
        command = str(row.get("command") or "")
        blocked_until = [str(item) for item in _as_list(row.get("blocked_until")) if str(item or "").strip()]
        if row.get("writes_archive_db") is not True:
            blockers.append(f"production_identity_closeout_runbook_{step_name}_not_archive_db_apply")
        if row.get("requires_operator_confirmation_phrase") is not True:
            blockers.append(f"production_identity_closeout_runbook_{step_name}_confirmation_not_required")
        if row.get("confirmation_phrase") != confirmation_phrase:
            blockers.append(f"production_identity_closeout_runbook_{step_name}_confirmation_phrase_missing")
        for fragment in (
            command_fragment,
            "--apply",
            confirmation_phrase,
            "--backup",
            shared_backup_path,
            "--writer-quiescent",
        ):
            if fragment and fragment not in command:
                blockers.append(f"production_identity_closeout_runbook_{step_name}_command_missing_fragment")
        for required_blocker in (
            "fresh_backup_exists",
            "writer_quiescence_passes_immediately_before_apply",
            "operator_enters_confirmation_phrase",
        ):
            if required_blocker not in blocked_until:
                blockers.append(f"production_identity_closeout_runbook_{step_name}_blocked_until_missing")
        if summary.get("ready_for_controlled_apply_now") is not True and row.get("execution_allowed_now") is True:
            blockers.append(f"production_identity_closeout_runbook_{step_name}_enabled_before_ready")

    command_checks = {
        "materialize_reviewed_duplicate_manifest": (
            "--emit-reviewed-manifest-json",
            duplicate_manifest_path,
        ),
        "audit_reviewed_duplicate_manifest": (
            "production_identity_duplicate_fact_snapshot_packet.py",
            "--decision-manifest",
            duplicate_manifest_path,
        ),
        "review_duplicate_apply_contract": (
            "production_identity_duplicate_fact_snapshot_apply_contract.py",
            "--decision-manifest",
            duplicate_manifest_path,
        ),
        "review_identity_audit_index_contract": (
            "production_identity_index_apply_contract.py",
            "--summary",
        ),
        "run_identity_audit_index_temp_drill": (
            "production_identity_index_temp_drill.py",
            "--summary",
        ),
        "run_identity_closeout_temp_drill": (
            "production_identity_merge_fill_temp_drill.py",
            "--summary",
            "--copy-mode",
            "focused",
        ),
        "create_shared_fresh_backup": ("sqlite3", ".backup", shared_backup_path),
        "verify_writer_quiescence": ("controlled_apply_maintenance_window_packet.py", "--summary"),
        "verify_duplicate_repair": (
            "production_identity_duplicate_fact_snapshot_packet.py",
            "--decision-manifest",
            duplicate_manifest_path,
        ),
        "review_identity_merge_fill_contract_after_duplicate_repair": (
            "production_identity_merge_fill_apply_contract.py",
            "--duplicate-decision-manifest",
            duplicate_manifest_path,
        ),
        "verify_identity_coverage": ("production_identity_coverage_audit.py", "--summary"),
        "verify_full_joint_gate": ("hierarchical_joint_plan_coverage_audit.py", "--summary"),
    }
    missing_command_fragments: list[str] = []
    for step_name, fragments in command_checks.items():
        command = str(_as_mapping(steps.get(step_name)).get("command") or "")
        for fragment in fragments:
            if fragment and fragment not in command:
                missing_command_fragments.append(f"{step_name}:{fragment}")
    if missing_command_fragments:
        blockers.append("production_identity_closeout_operator_runbook_commands_incomplete")

    impact = _as_mapping(summary.get("impact_summary"))
    archive_apply_steps = [str(item) for item in _as_list(impact.get("archive_db_apply_steps"))]
    if archive_apply_steps != [
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "apply_identity_merge_fill",
    ]:
        blockers.append("production_identity_closeout_operator_runbook_apply_steps_mismatch")
    if impact.get("fetches_external_weather_sources") is not False:
        blockers.append("production_identity_closeout_operator_runbook_fetch_enabled")
    if impact.get("places_orders") is not False:
        blockers.append("production_identity_closeout_operator_runbook_orders_enabled")
    if impact.get("changes_parameters") is not False:
        blockers.append("production_identity_closeout_operator_runbook_parameter_change_enabled")
    if impact.get("historical_backfill") is not False:
        blockers.append("production_identity_closeout_operator_runbook_backfill_enabled")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("production_identity_closeout_operator_runbook_unsafe_guard_enabled")

    for blocker in _as_list(summary.get("start_blockers")):
        warnings.append(f"production_identity_closeout_runbook_start_blocker:{blocker}")

    return {
        "schema": "production_identity_closeout_operator_runbook_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "production_identity_closeout_operator_runbook_read_only_gate",
        "runbook_schema": str(summary.get("source_schema") or ""),
        "ready_for_controlled_apply_now": bool(summary.get("ready_for_controlled_apply_now")),
        "start_blockers": list(summary.get("start_blockers") or []),
        "maintenance_window": dict(_as_mapping(summary.get("maintenance_window"))),
        "artifacts": dict(artifacts),
        "identity_scope": dict(_as_mapping(summary.get("identity_scope"))),
        "impact_summary": dict(impact),
        "missing_required_steps": missing_steps,
        "missing_command_fragments": missing_command_fragments,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "_operator_runbook": runbook,
        "next_boundary": (
            "operator_can_use_production_identity_closeout_runbook_after_confirmation"
            if not blockers
            else "fix_production_identity_closeout_operator_runbook_before_apply"
        ),
    }


def _hierarchical_joint_closeout_operator_runbook_report(
    *,
    db_path: Path,
    awc_terminal_operator_runbook_report: Mapping[str, Any],
    production_identity_closeout_operator_runbook_report: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        runbook = hierarchical_joint_closeout_operator_runbook.build_master_runbook_from_components(
            db_path=db_path,
            awc=_as_mapping(awc_terminal_operator_runbook_report.get("_operator_runbook")),
            identity=_as_mapping(production_identity_closeout_operator_runbook_report.get("_operator_runbook")),
        )
        summary = hierarchical_joint_closeout_operator_runbook.compact_master_runbook(runbook)
    except Exception as exc:
        return _exception_report(
            "hierarchical_joint_closeout_operator_runbook_exception_v1",
            "hierarchical_joint_closeout_operator_runbook_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("hierarchical_joint_closeout_operator_runbook_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("hierarchical_joint_closeout_operator_runbook_not_dry_run")

    sequence = [_as_mapping(row) for row in _as_list(summary.get("operator_sequence"))]
    steps = {str(row.get("step") or ""): row for row in sequence}
    required_steps = (
        "emit_review_report",
        "emit_decision_manifest_template",
        "operator_reviews_decision_manifest",
        "audit_reviewed_decision_manifest",
        "materialize_reviewed_duplicate_manifest",
        "audit_reviewed_duplicate_manifest",
        "review_duplicate_apply_contract",
        "review_identity_audit_index_contract",
        "run_identity_audit_index_temp_drill",
        "run_identity_closeout_temp_drill",
        "pause_writer_services",
        "verify_writer_quiescence",
        "create_fresh_backup",
        "apply_awc_legacy_terminal_closeout",
        "verify_awc_terminal_coverage",
        "create_shared_fresh_backup",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "verify_duplicate_repair",
        "review_identity_merge_fill_contract_after_duplicate_repair",
        "apply_identity_merge_fill",
        "verify_identity_coverage",
        "verify_full_joint_gate",
        "resume_writer_services",
    )
    missing_steps = [step for step in required_steps if step not in steps]
    if missing_steps:
        blockers.append("hierarchical_joint_closeout_operator_runbook_missing_required_steps")

    def first_index(step_name: str) -> int | None:
        for idx, row in enumerate(sequence):
            if str(row.get("step") or "") == step_name:
                return idx
        return None

    def require_before(left: str, right: str, blocker: str) -> None:
        left_idx = first_index(left)
        right_idx = first_index(right)
        if left_idx is None or right_idx is None or left_idx >= right_idx:
            blockers.append(blocker)

    if not missing_steps:
        ordered_pairs = (
            ("audit_reviewed_decision_manifest", "pause_writer_services", "joint_closeout_awc_review_not_before_pause"),
            (
                "review_duplicate_apply_contract",
                "review_identity_audit_index_contract",
                "joint_closeout_duplicate_contract_not_before_index_contract",
            ),
            (
                "review_identity_audit_index_contract",
                "run_identity_audit_index_temp_drill",
                "joint_closeout_index_contract_not_before_index_temp_drill",
            ),
            (
                "run_identity_audit_index_temp_drill",
                "run_identity_closeout_temp_drill",
                "joint_closeout_index_temp_drill_not_before_merge_temp_drill",
            ),
            ("run_identity_closeout_temp_drill", "pause_writer_services", "joint_closeout_temp_drill_not_before_pause"),
            ("verify_writer_quiescence", "create_fresh_backup", "joint_closeout_quiescence_not_before_awc_backup"),
            ("create_fresh_backup", "apply_awc_legacy_terminal_closeout", "joint_closeout_awc_backup_not_before_awc_apply"),
            ("apply_awc_legacy_terminal_closeout", "verify_awc_terminal_coverage", "joint_closeout_awc_apply_not_before_awc_verify"),
            ("verify_awc_terminal_coverage", "create_shared_fresh_backup", "joint_closeout_awc_verify_not_before_identity_backup"),
            (
                "create_shared_fresh_backup",
                "apply_identity_audit_indexes",
                "joint_closeout_identity_backup_not_before_index_apply",
            ),
            (
                "apply_identity_audit_indexes",
                "apply_duplicate_fact_snapshot_repair",
                "joint_closeout_index_apply_not_before_duplicate_apply",
            ),
            ("apply_duplicate_fact_snapshot_repair", "verify_duplicate_repair", "joint_closeout_duplicate_apply_not_before_verify"),
            ("verify_duplicate_repair", "review_identity_merge_fill_contract_after_duplicate_repair", "joint_closeout_duplicate_verify_not_before_merge_contract"),
            ("review_identity_merge_fill_contract_after_duplicate_repair", "apply_identity_merge_fill", "joint_closeout_merge_contract_not_before_merge_apply"),
            ("apply_identity_merge_fill", "verify_identity_coverage", "joint_closeout_merge_apply_not_before_identity_verify"),
            ("verify_identity_coverage", "verify_full_joint_gate", "joint_closeout_identity_verify_not_before_joint_gate"),
            ("verify_full_joint_gate", "resume_writer_services", "joint_closeout_joint_gate_not_before_resume"),
        )
        for left, right, blocker in ordered_pairs:
            require_before(left, right, blocker)

    impact = _as_mapping(summary.get("impact_summary"))
    archive_apply_steps = [str(item) for item in _as_list(impact.get("archive_db_apply_steps"))]
    if archive_apply_steps != [
        "apply_awc_legacy_terminal_closeout",
        "apply_identity_audit_indexes",
        "apply_duplicate_fact_snapshot_repair",
        "apply_identity_merge_fill",
    ]:
        blockers.append("hierarchical_joint_closeout_operator_runbook_apply_steps_mismatch")

    expected_phrases = {
        "apply_awc_legacy_terminal_closeout": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
        "apply_identity_audit_indexes": (
            production_identity_closeout_confirmation_packet.index_apply.CONFIRMATION_PHRASE
        ),
        "apply_duplicate_fact_snapshot_repair": (
            production_identity_closeout_confirmation_packet.duplicate_apply.CONFIRMATION_PHRASE
        ),
        "apply_identity_merge_fill": (
            production_identity_closeout_confirmation_packet.merge_fill_apply.CONFIRMATION_PHRASE
        ),
    }
    for step_name, confirmation_phrase in expected_phrases.items():
        row = _as_mapping(steps.get(step_name))
        if row.get("writes_archive_db") is not True:
            blockers.append(f"hierarchical_joint_closeout_operator_runbook_{step_name}_not_archive_db_apply")
        if row.get("requires_operator_confirmation_phrase") is not True:
            blockers.append(f"hierarchical_joint_closeout_operator_runbook_{step_name}_confirmation_not_required")
        if row.get("confirmation_phrase") != confirmation_phrase:
            blockers.append(f"hierarchical_joint_closeout_operator_runbook_{step_name}_confirmation_phrase_missing")
        if summary.get("ready_for_controlled_apply_now") is not True and row.get("execution_allowed_now") is True:
            blockers.append(f"hierarchical_joint_closeout_operator_runbook_{step_name}_enabled_before_ready")

    confirmations = _as_mapping(summary.get("confirmation_phrases"))
    if confirmations.get("awc_terminal_closeout") != "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT":
        blockers.append("hierarchical_joint_closeout_operator_runbook_awc_confirmation_phrase_missing")
    if confirmations.get("production_identity_audit_indexes") != (
        production_identity_closeout_confirmation_packet.index_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("hierarchical_joint_closeout_operator_runbook_identity_audit_index_confirmation_phrase_missing")
    if confirmations.get("production_duplicate_repair") != (
        production_identity_closeout_confirmation_packet.duplicate_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("hierarchical_joint_closeout_operator_runbook_duplicate_confirmation_phrase_missing")
    if confirmations.get("production_identity_merge_fill") != (
        production_identity_closeout_confirmation_packet.merge_fill_apply.CONFIRMATION_PHRASE
    ):
        blockers.append("hierarchical_joint_closeout_operator_runbook_merge_fill_confirmation_phrase_missing")

    for key, blocker in (
        ("fetches_external_weather_sources", "hierarchical_joint_closeout_operator_runbook_fetch_enabled"),
        ("places_orders", "hierarchical_joint_closeout_operator_runbook_orders_enabled"),
        ("changes_parameters", "hierarchical_joint_closeout_operator_runbook_parameter_change_enabled"),
        ("historical_backfill", "hierarchical_joint_closeout_operator_runbook_backfill_enabled"),
    ):
        if impact.get(key) is not False:
            blockers.append(blocker)

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("hierarchical_joint_closeout_operator_runbook_unsafe_guard_enabled")

    for blocker in _as_list(summary.get("start_blockers")):
        warnings.append(f"hierarchical_joint_closeout_runbook_start_blocker:{blocker}")

    return {
        "schema": "hierarchical_joint_closeout_operator_runbook_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "hierarchical_joint_closeout_operator_runbook_read_only_gate",
        "runbook_schema": str(summary.get("source_schema") or ""),
        "ready_for_controlled_apply_now": bool(summary.get("ready_for_controlled_apply_now")),
        "start_blockers": list(summary.get("start_blockers") or []),
        "impact_summary": dict(impact),
        "confirmation_phrases": dict(confirmations),
        "missing_required_steps": missing_steps,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "operator_can_use_hierarchical_joint_closeout_runbook_after_confirmation"
            if not blockers
            else "fix_hierarchical_joint_closeout_operator_runbook_before_apply"
        ),
    }


def _posterior_rematerialization_report(
    *,
    db_path: Path,
    since_hours: float,
    limit: int,
    max_items_output: int,
) -> dict[str, Any]:
    try:
        plan = rematerialization_plan.build_plan(
            db_path=db_path,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
            latest_only=False,
        )
        summary = rematerialization_plan.compact_plan(plan)
    except Exception as exc:
        return _exception_report(
            "posterior_rematerialization_exception_v1",
            "posterior_rematerialization_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    remat = _as_mapping(summary.get("historical_rematerialization_plan"))
    forward = _as_mapping(summary.get("forward_write_acceptance"))
    current_latest_issue_count = int(remat.get("current_latest_issue_count") or 0)
    current_latest_projected_issue_count = int(remat.get("current_latest_projected_issue_count") or 0)
    historical_only_issue_count = int(remat.get("historical_only_issue_count") or 0)
    if summary.get("read_only") is not True:
        blockers.append("posterior_rematerialization_plan_not_read_only")
    if bool(remat.get("production_latest_chain_blocked_by_candidates")) or current_latest_issue_count > 0:
        blockers.append("posterior_rematerialization_current_latest_issue_candidates")
    if current_latest_projected_issue_count > 0:
        blockers.append("posterior_rematerialization_current_latest_not_forward_fixable")
    if historical_only_issue_count > 0:
        warnings.append("posterior_rematerialization_history_only_candidates_present")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("posterior_rematerialization_plan_unsafe_guard_enabled")

    return {
        "schema": "posterior_rematerialization_governance_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "posterior_rematerialization_governance_read_only_gate",
        "source_schema": str(summary.get("source_schema") or ""),
        "scope": dict(_as_mapping(summary.get("scope"))),
        "forward_write_acceptance": dict(forward),
        "historical_rematerialization_plan": dict(remat),
        "candidate_output_count": int(summary.get("candidate_output_count") or 0),
        "candidates_truncated": bool(summary.get("candidates_truncated")),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "history_only_rematerialization_candidates_can_wait_for_operator_review"
            if not blockers and historical_only_issue_count > 0
            else "posterior_rematerialization_governance_ok"
            if not blockers
            else "fix_current_latest_posterior_rematerialization_gap_before_downstream_closeout"
        ),
    }


def _daily_review_controlled_apply_packet_report(
    *,
    db_path: Path,
    since_days: int,
    since_date: str,
    slug: str,
    event_date: str,
    market_scope: str,
    limit: int,
    max_items_output: int,
) -> dict[str, Any]:
    try:
        packet = controlled_apply_packet.build_confirmation_packet(
            db_path,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=int(limit),
            max_items_output=int(max_items_output),
            missing_fact_decision=controlled_apply_packet.DEFAULT_MISSING_FACT_DECISION,
        )
        summary = controlled_apply_packet.compact_confirmation_packet(packet)
    except Exception as exc:
        return _exception_report(
            "daily_review_controlled_apply_packet_exception_v1",
            "daily_review_controlled_apply_packet_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("controlled_apply_packet_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("controlled_apply_packet_not_dry_run")
    if str(summary.get("missing_fact_decision_policy") or "") != controlled_apply_packet.DEFAULT_MISSING_FACT_DECISION:
        blockers.append("controlled_apply_packet_missing_fact_policy_not_exclude_sample")

    steps = {
        str(_as_mapping(row).get("step") or ""): _as_mapping(row)
        for row in _as_list(summary.get("control_sequence"))
    }
    required_steps = (
        "materialize_reviewed_manifest",
        "create_fresh_backup",
        "apply_phase0_schema",
        "dry_run_lineage_repair_and_risk",
        "apply_lineage_repair",
        "dry_run_daily_review_persistence",
        "apply_daily_review_persistence",
        "post_apply_acceptance",
    )
    missing_steps = [step for step in required_steps if step not in steps]
    if missing_steps:
        blockers.append("controlled_apply_packet_missing_required_steps")

    backup_path = str(summary.get("backup_path") or "")
    apply_command_checks = {
        "apply_phase0_schema": "APPLY_PHASE0_FORWARD_SCHEMA",
        "apply_lineage_repair": "APPLY_DAILY_REVIEW_LINEAGE_REPAIR",
        "apply_daily_review_persistence": "APPLY_DAILY_REVIEW_PERSISTENCE",
    }
    missing_command_fragments: list[str] = []
    for step, phrase in apply_command_checks.items():
        command = str(_as_mapping(steps.get(step)).get("command") or "")
        for fragment in ("--apply", "--confirm", phrase, "--backup", backup_path, "--writer-quiescent"):
            if fragment and fragment not in command:
                missing_command_fragments.append(f"{step}:{fragment}")
    materialize_command = str(_as_mapping(steps.get("materialize_reviewed_manifest")).get("command") or "")
    if "--emit-proposed-manifest-json" not in materialize_command:
        missing_command_fragments.append("materialize_reviewed_manifest:--emit-proposed-manifest-json")
    backup_command = str(_as_mapping(steps.get("create_fresh_backup")).get("command") or "")
    for fragment in ("sqlite3", ".backup", backup_path):
        if fragment and fragment not in backup_command:
            missing_command_fragments.append(f"create_fresh_backup:{fragment}")
    if "--lineage-decision-manifest" not in str(_as_mapping(steps.get("apply_daily_review_persistence")).get("command") or ""):
        missing_command_fragments.append("apply_daily_review_persistence:--lineage-decision-manifest")
    post_apply_raw = _as_mapping(steps.get("post_apply_acceptance")).get("command")
    post_apply_commands = (
        [str(item) for item in _as_list(post_apply_raw)]
        if isinstance(post_apply_raw, Sequence) and not isinstance(post_apply_raw, (str, bytes, bytearray))
        else [str(post_apply_raw or "")]
    )
    joint_acceptance_commands = [
        command for command in post_apply_commands if "hierarchical_joint_plan_coverage_audit.py --summary" in command
    ]
    if not joint_acceptance_commands:
        missing_command_fragments.append("post_apply_acceptance:hierarchical_joint_plan_coverage_audit.py --summary")
    else:
        expected_limit = f"--limit {int(limit)}"
        expected_items = f"--max-items-output {int(max_items_output)}"
        if not any(expected_limit in command for command in joint_acceptance_commands):
            missing_command_fragments.append(f"post_apply_acceptance:{expected_limit}")
        if not any(expected_items in command for command in joint_acceptance_commands):
            missing_command_fragments.append(f"post_apply_acceptance:{expected_items}")
    if missing_command_fragments:
        blockers.append("controlled_apply_packet_commands_incomplete")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("controlled_apply_packet_unsafe_guard_enabled")

    phase0 = _as_mapping(summary.get("phase0"))
    lineage = _as_mapping(summary.get("lineage"))
    persistence = _as_mapping(summary.get("persistence"))
    return {
        "schema": "daily_review_controlled_apply_packet_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "daily_review_controlled_apply_packet_read_only_gate",
        "_controlled_apply_packet": packet,
        "packet_schema": str(summary.get("source_schema") or ""),
        "backup_path": backup_path,
        "manifest_output_path": str(summary.get("manifest_output_path") or ""),
        "missing_fact_decision_policy": str(summary.get("missing_fact_decision_policy") or ""),
        "proposed_manifest_decision_counts": dict(_as_mapping(summary.get("proposed_manifest_decision_counts"))),
        "phase0": {
            "needs_apply": bool(phase0.get("needs_apply")),
            "ddl_summary": dict(_as_mapping(phase0.get("ddl_summary"))),
        },
        "lineage": {
            "candidate_count": lineage.get("candidate_count"),
            "ready_for_controlled_apply_after_runbook": bool(
                lineage.get("ready_for_controlled_apply_after_runbook")
            ),
            "risk_summary": dict(_as_mapping(lineage.get("risk_summary"))),
        },
        "persistence": {
            "effective_settled_event_count": persistence.get("effective_settled_event_count"),
            "excluded_settled_event_count": persistence.get("excluded_settled_event_count"),
            "ready_to_apply_now": bool(persistence.get("ready_to_apply_now")),
            "batch_apply_blockers": list(persistence.get("batch_apply_blockers") or []),
        },
        "missing_required_steps": missing_steps,
        "missing_command_fragments": missing_command_fragments,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "operator_review_then_controlled_phase0_schema_apply"
            if not blockers
            else "fix_daily_review_controlled_apply_packet_before_operator_confirmation"
        ),
    }


def _daily_review_acceptance_report(
    *,
    db_path: Path,
    since_days: int,
    since_date: str,
    slug: str,
    event_date: str,
    market_scope: str,
    limit: int,
    max_items_output: int,
    since_hours: float,
    replay_limit: int,
    include_ui_snapshot_audit: bool,
    ui_snapshot_mode: str,
    ui_snapshot_run_date: str,
    ui_snapshot_limit: int,
    ui_snapshot_city_filter: str,
    require_ui_visible: bool,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return review_acceptance_audit.build_report(
            db_path,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=int(limit),
            max_items_output=int(max_items_output),
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            include_ui_snapshot_audit=bool(include_ui_snapshot_audit),
            ui_snapshot_mode=str(ui_snapshot_mode or "summary"),
            ui_snapshot_run_date=str(ui_snapshot_run_date or ""),
            ui_snapshot_limit=int(ui_snapshot_limit),
            ui_snapshot_city_filter=str(ui_snapshot_city_filter or ""),
            require_ui_visible=bool(require_ui_visible),
            lineage_decision_manifest=lineage_decision_manifest,
        )
    except Exception as exc:
        return _exception_report(
            "daily_review_post_apply_acceptance_exception_v1",
            "daily_review_post_apply_acceptance_exception",
            exc,
        )


def _daily_review_post_apply_resolution_packet_report(
    *,
    db_path: Path,
    since_days: int,
    since_date: str,
    slug: str,
    event_date: str,
    market_scope: str,
    limit: int,
    max_items_output: int,
    master_confirmation_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        packet = resolution_confirmation_packet.build_confirmation_packet(
            db_path,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=int(limit),
            max_items_output=int(max_items_output),
            missing_fact_decision=controlled_apply_packet.DEFAULT_MISSING_FACT_DECISION,
            master_confirmation_packet=master_confirmation_packet,
        )
        summary = resolution_confirmation_packet.compact_confirmation_packet(packet)
        lineage_decision_manifest = dict(_as_mapping(packet.get("lineage_decision_manifest")))
    except Exception as exc:
        return _exception_report(
            "daily_review_post_apply_resolution_packet_exception_v1",
            "daily_review_post_apply_resolution_packet_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("post_apply_resolution_packet_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("post_apply_resolution_packet_not_dry_run")

    boundary = _as_mapping(summary.get("boundary"))
    expected_boundary = {
        "posterior_ssot_repair_included_as_controlled_step": True,
        "missing_fact_snapshot_exclusion_included": True,
        "daily_review_master_controlled_apply_included": True,
        "legacy_awc_signal_anchor_auto_rebuild_included": False,
        "legacy_awc_signal_anchor_decision_manifest_included": True,
        "historical_backfill_included": False,
        "external_fetch_included": False,
        "orders_or_trade_decisions_included": False,
        "parameter_change_included": False,
    }
    for key, expected in expected_boundary.items():
        if bool(boundary.get(key)) is not expected:
            blockers.append(f"post_apply_resolution_packet_boundary_{key}_mismatch")

    steps = {
        str(_as_mapping(row).get("step") or ""): _as_mapping(row)
        for row in _as_list(summary.get("control_sequence"))
    }
    required_steps = (
        "review_exact_posterior_rematerialization_probe",
        "apply_posterior_ssot_repairs_per_fact",
        "verify_posterior_ssot_repairs",
        "resolve_legacy_awc_signal_anchors",
        "confirm_missing_fact_snapshot_exclusion_scope",
        "continue_with_daily_review_controlled_apply_sequence",
    )
    missing_steps = [step for step in required_steps if step not in steps]
    if missing_steps:
        blockers.append("post_apply_resolution_packet_missing_required_steps")
    apply_step = _as_mapping(steps.get("apply_posterior_ssot_repairs_per_fact"))
    if apply_step and apply_step.get("confirmation_phrase") != "APPLY_POSTERIOR_SSOT_FACT_REPAIR":
        blockers.append("post_apply_resolution_packet_posterior_confirm_phrase_missing")

    posterior = _as_mapping(summary.get("posterior_ssot_repair"))
    legacy = _as_mapping(summary.get("legacy_awc_signal_anchor"))
    if int(posterior.get("candidate_count") or 0) > 0 and not posterior.get("ready_for_operator_controlled_apply"):
        blockers.append("post_apply_resolution_packet_posterior_repair_not_ready")
    if int(legacy.get("candidate_count") or 0) > 0 and legacy.get("decision_required") is not True:
        blockers.append("post_apply_resolution_packet_legacy_anchor_decision_not_exposed")
    if legacy.get("ready_for_automatic_apply") is True:
        blockers.append("post_apply_resolution_packet_legacy_anchor_auto_apply_enabled")
    if int(legacy.get("candidate_count") or 0) > 0:
        if legacy.get("decision_manifest_schema") != "legacy_awc_signal_anchor_decision_manifest_v1":
            blockers.append("post_apply_resolution_packet_legacy_anchor_manifest_schema_missing")
        manifest_audit = _as_mapping(legacy.get("decision_manifest_audit"))
        if not manifest_audit:
            blockers.append("post_apply_resolution_packet_legacy_anchor_manifest_audit_missing")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("post_apply_resolution_packet_unsafe_guard_enabled")
    for blocker in _as_list(summary.get("hard_blockers")):
        warnings.append(f"post_apply_resolution_operator_pending:{blocker}")

    return {
        "schema": "daily_review_post_apply_resolution_packet_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "daily_review_post_apply_resolution_packet_read_only_gate",
        "_post_apply_resolution_packet": packet,
        "packet_schema": str(summary.get("source_schema") or ""),
        "resolution_summary": dict(_as_mapping(summary.get("resolution_summary"))),
        "posterior_ssot_repair": dict(posterior),
        "legacy_awc_signal_anchor": dict(legacy),
        "daily_review_master": dict(_as_mapping(summary.get("daily_review_master"))),
        "_lineage_decision_manifest": lineage_decision_manifest,
        "missing_required_steps": missing_steps,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "operator_resolves_post_apply_resolution_sequence"
            if not blockers
            else "fix_daily_review_post_apply_resolution_packet_before_operator_confirmation"
        ),
    }


def _daily_review_production_apply_preflight_packet_report(
    *,
    db_path: Path,
    since_days: int,
    since_date: str,
    slug: str,
    event_date: str,
    market_scope: str,
    limit: int,
    max_items_output: int,
    resolution_confirmation_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        packet = production_preflight_packet.build_preflight_packet(
            db_path,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=int(limit),
            max_items_output=int(max_items_output),
            missing_fact_decision=controlled_apply_packet.DEFAULT_MISSING_FACT_DECISION,
            resolution_confirmation_packet=resolution_confirmation_packet,
        )
        summary = production_preflight_packet.compact_preflight_packet(packet)
    except Exception as exc:
        return _exception_report(
            "daily_review_production_apply_preflight_packet_exception_v1",
            "daily_review_production_apply_preflight_packet_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("production_apply_preflight_packet_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("production_apply_preflight_packet_not_dry_run")

    boundary = _as_mapping(summary.get("boundary"))
    expected_boundary = {
        "no_production_write": True,
        "no_backup_creation": True,
        "no_manifest_materialization": True,
        "no_external_fetch": True,
        "no_order_or_trade_decision": True,
        "legacy_awc_manifest_is_pre_sequence_gate": True,
        "posterior_repairs_precede_daily_review_master_apply": True,
        "daily_review_persistence_runs_after_schema_and_lineage_repair": True,
        "writer_quiescent_is_pre_sequence_gate": True,
    }
    for key, expected in expected_boundary.items():
        if bool(boundary.get(key)) is not expected:
            blockers.append(f"production_apply_preflight_boundary_{key}_mismatch")

    stages = {
        str(_as_mapping(row).get("key") or ""): _as_mapping(row)
        for row in _as_list(summary.get("stages"))
    }
    required_stage_keys = (
        "maintenance_window_writer_quiescence",
        "legacy_awc_signal_anchor_decision_manifest",
        "current_latest_posterior_materialization",
        "daily_review_posterior_ssot_fact_repair",
        "phase0_schema_apply",
        "lineage_repair_apply",
        "daily_review_persistence_apply",
        "post_apply_acceptance",
    )
    missing_stages = [key for key in required_stage_keys if key not in stages]
    if missing_stages:
        blockers.append("production_apply_preflight_missing_required_stages")

    current_latest = _as_mapping(summary.get("current_latest_posterior_materialization"))
    current_latest_count = int(current_latest.get("candidate_count") or 0)
    current_latest_fact_ids = _as_list(current_latest.get("candidate_fact_ids"))
    if current_latest_count > 0 and len(current_latest_fact_ids) != current_latest_count:
        blockers.append("production_apply_preflight_current_latest_fact_ids_incomplete")
    if current_latest_count > 0 and not str(current_latest.get("apply_confirmation_command") or ""):
        blockers.append("production_apply_preflight_current_latest_confirmation_command_missing")

    maintenance = _as_mapping(summary.get("maintenance_window"))
    stop_authorization = _as_mapping(maintenance.get("stop_authorization"))
    if "maintenance_window_writer_quiescence" in stages and "writer_quiescent" not in maintenance:
        blockers.append("production_apply_preflight_maintenance_window_summary_missing")
    if stop_authorization and "can_pause_required_services_now" not in stop_authorization:
        blockers.append("production_apply_preflight_stop_authorization_incomplete")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("production_apply_preflight_packet_unsafe_guard_enabled")

    for blocker in _as_list(summary.get("start_blockers")):
        warnings.append(f"production_apply_start_blocker:{blocker}")
    for blocker in _as_list(summary.get("sequence_dependency_blockers")):
        warnings.append(f"production_apply_sequence_dependency:{blocker}")

    return {
        "schema": "daily_review_production_apply_preflight_packet_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "daily_review_production_apply_preflight_packet_read_only_gate",
        "_production_preflight_packet": packet,
        "packet_schema": str(summary.get("source_schema") or ""),
        "ready_to_start_controlled_sequence": bool(summary.get("ready_to_start_controlled_sequence")),
        "start_blockers": list(summary.get("start_blockers") or []),
        "sequence_dependency_blockers": list(summary.get("sequence_dependency_blockers") or []),
        "maintenance_window": dict(maintenance),
        "current_latest_posterior_materialization": dict(current_latest),
        "missing_required_stages": missing_stages,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "operator_uses_production_preflight_to_start_controlled_sequence_when_ready"
            if not blockers
            else "fix_daily_review_production_preflight_packet_before_operator_confirmation"
        ),
    }


def _current_latest_posterior_apply_runbook_report(
    *,
    db_path: Path,
    since_hours: float,
    limit: int,
    max_items_output: int,
    production_preflight_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        packet = posterior_apply_runbook.build_runbook(
            db_path,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
            production_preflight_packet=production_preflight_packet,
        )
        summary = posterior_apply_runbook.compact_runbook(packet)
    except Exception as exc:
        return _exception_report(
            "current_latest_posterior_apply_runbook_exception_v1",
            "current_latest_posterior_apply_runbook_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(summary.get("guards")) or _guards()
    if summary.get("read_only") is not True:
        blockers.append("current_latest_posterior_runbook_not_read_only")
    if summary.get("dry_run") is not True:
        blockers.append("current_latest_posterior_runbook_not_dry_run")

    boundary = _as_mapping(summary.get("boundary"))
    expected_boundary = {
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
    }
    for key, expected in expected_boundary.items():
        if bool(boundary.get(key)) is not expected:
            blockers.append(f"current_latest_posterior_runbook_boundary_{key}_mismatch")

    scope = _as_mapping(summary.get("scope"))
    candidate_fact_ids = [int(value) for value in _as_list(scope.get("candidate_fact_ids")) if int(value or 0) > 0]
    apply_required = bool(summary.get("apply_required"))
    if apply_required != bool(candidate_fact_ids):
        blockers.append("current_latest_posterior_runbook_apply_required_scope_mismatch")

    preflights = [_as_mapping(row) for row in _as_list(summary.get("single_fact_preflights"))]
    preflight_fact_ids = [
        int(row.get("fact_id") or 0)
        for row in preflights
        if int(row.get("fact_id") or 0) > 0
    ]
    if candidate_fact_ids and preflight_fact_ids != candidate_fact_ids:
        blockers.append("current_latest_posterior_runbook_preflight_fact_ids_mismatch")
    for row in preflights:
        fact_id = int(row.get("fact_id") or 0)
        if row.get("ok") is not True:
            blockers.append("current_latest_posterior_runbook_single_fact_preflight_not_ok")
        if apply_required and not row.get("apply_command_available"):
            blockers.append("current_latest_posterior_runbook_apply_command_missing")
        if apply_required and not row.get("uses_shared_backup"):
            blockers.append("current_latest_posterior_runbook_shared_backup_not_used")
        if not str(row.get("projected_target_bucket") or "") and apply_required:
            blockers.append("current_latest_posterior_runbook_projected_target_missing")
        if fact_id <= 0:
            blockers.append("current_latest_posterior_runbook_invalid_fact_id")

    sequence = [_as_mapping(row) for row in _as_list(summary.get("operator_sequence"))]
    sequence_steps = [str(row.get("step") or "") for row in sequence]
    required_steps = (
        *(("run_current_latest_temp_drill",) if apply_required else ()),
        "verify_writer_quiescence",
        "create_one_shared_archive_backup",
        "post_apply_latest_coverage",
        "post_apply_rematerialization_governance",
        "post_apply_exact_candidate_acceptance",
        "post_apply_joint_plan_gate",
    )
    missing_steps = [step for step in required_steps if step not in sequence_steps]
    if missing_steps:
        blockers.append("current_latest_posterior_runbook_missing_required_steps")
    if apply_required and sequence_steps.count("apply_one_current_latest_fact") != len(candidate_fact_ids):
        blockers.append("current_latest_posterior_runbook_apply_step_count_mismatch")

    def step_indices(step_name: str) -> list[int]:
        return [
            idx
            for idx, row in enumerate(sequence)
            if str(row.get("step") or "") == step_name
        ]

    def first_index(step_name: str) -> int | None:
        indices = step_indices(step_name)
        return indices[0] if indices else None

    def last_index(step_name: str) -> int | None:
        indices = step_indices(step_name)
        return indices[-1] if indices else None

    if apply_required and not missing_steps:
        apply_indices = step_indices("apply_one_current_latest_fact")
        first_apply_idx = min(apply_indices) if apply_indices else None
        last_apply_idx = max(apply_indices) if apply_indices else None
        ordered_pre_apply_steps = (
            ("run_current_latest_temp_drill", "current_latest_posterior_runbook_temp_drill_not_before_apply"),
            ("verify_writer_quiescence", "current_latest_posterior_runbook_verify_quiescence_not_before_apply"),
            ("create_one_shared_archive_backup", "current_latest_posterior_runbook_shared_backup_not_before_apply"),
        )
        for step_name, blocker_name in ordered_pre_apply_steps:
            idx = first_index(step_name)
            if idx is None or first_apply_idx is None or idx >= first_apply_idx:
                blockers.append(blocker_name)
        verify_idx = first_index("verify_writer_quiescence")
        backup_idx = first_index("create_one_shared_archive_backup")
        if verify_idx is None or backup_idx is None or verify_idx >= backup_idx:
            blockers.append("current_latest_posterior_runbook_backup_not_after_quiescence_check")

        for fact_id in candidate_fact_ids:
            fact_preflight_indices = [
                idx
                for idx, row in enumerate(sequence)
                if str(row.get("step") or "") == "preflight_one_current_latest_fact"
                and int(row.get("fact_id") or 0) == int(fact_id)
            ]
            fact_apply_indices = [
                idx
                for idx, row in enumerate(sequence)
                if str(row.get("step") or "") == "apply_one_current_latest_fact"
                and int(row.get("fact_id") or 0) == int(fact_id)
            ]
            if not fact_preflight_indices or not fact_apply_indices:
                blockers.append("current_latest_posterior_runbook_preflight_apply_pair_missing")
                continue
            if max(fact_preflight_indices) >= min(fact_apply_indices):
                blockers.append("current_latest_posterior_runbook_preflight_not_before_apply")

        post_order = (
            ("post_apply_latest_coverage", "current_latest_posterior_runbook_coverage_not_after_all_apply"),
            (
                "post_apply_rematerialization_governance",
                "current_latest_posterior_runbook_rematerialization_not_after_coverage",
            ),
            (
                "post_apply_exact_candidate_acceptance",
                "current_latest_posterior_runbook_exact_acceptance_not_after_rematerialization",
            ),
            ("post_apply_joint_plan_gate", "current_latest_posterior_runbook_joint_gate_not_after_exact_acceptance"),
        )
        previous_idx = last_apply_idx
        for step_name, blocker_name in post_order:
            idx = first_index(step_name)
            if idx is None or previous_idx is None or idx <= previous_idx:
                blockers.append(blocker_name)
            previous_idx = idx

        joint_idx = first_index("post_apply_joint_plan_gate")
        for idx in step_indices("resume_writer_service"):
            if joint_idx is None or idx <= joint_idx:
                blockers.append("current_latest_posterior_runbook_resume_not_after_joint_gate")
                break

    shared_backup = _as_mapping(summary.get("shared_backup"))
    shared_backup_path = str(shared_backup.get("path") or "")
    if apply_required and not shared_backup_path:
        blockers.append("current_latest_posterior_runbook_shared_backup_path_missing")
    for row in sequence:
        step = str(row.get("step") or "")
        command = str(row.get("command") or "")
        if step == "run_current_latest_temp_drill":
            if apply_required and "scripts/current_latest_posterior_apply_temp_drill.py" not in command:
                blockers.append("current_latest_posterior_runbook_temp_drill_command_missing")
            if apply_required and "--fact-ids" not in command:
                blockers.append("current_latest_posterior_runbook_temp_drill_candidate_ids_arg_missing")
            for fact_id in candidate_fact_ids:
                if apply_required and str(fact_id) not in command:
                    blockers.append("current_latest_posterior_runbook_temp_drill_fact_id_missing")
                    break
            drill_candidate_fact_ids = [
                int(value)
                for value in _as_list(row.get("candidate_fact_ids"))
                if int(value or 0) > 0
            ]
            if apply_required and drill_candidate_fact_ids != candidate_fact_ids:
                blockers.append("current_latest_posterior_runbook_temp_drill_candidate_ids_mismatch")
            if apply_required and int(row.get("candidate_count") or 0) != len(candidate_fact_ids):
                blockers.append("current_latest_posterior_runbook_temp_drill_candidate_count_mismatch")
            if bool(row.get("writes_archive_db")):
                blockers.append("current_latest_posterior_runbook_temp_drill_writes_archive_db")
        if step == "post_apply_exact_candidate_acceptance":
            if apply_required and "scripts/current_latest_posterior_post_apply_acceptance.py" not in command:
                blockers.append("current_latest_posterior_runbook_exact_acceptance_command_missing")
            if apply_required and "--candidate-fact-ids" not in command:
                blockers.append("current_latest_posterior_runbook_exact_acceptance_candidate_ids_missing")
            if apply_required and "--expected-projections-json" not in command:
                blockers.append("current_latest_posterior_runbook_exact_acceptance_expected_projection_missing")
            for fact_id in candidate_fact_ids:
                if apply_required and str(fact_id) not in command:
                    blockers.append("current_latest_posterior_runbook_exact_acceptance_fact_id_missing")
                    break
            if bool(row.get("writes_archive_db")):
                blockers.append("current_latest_posterior_runbook_exact_acceptance_writes_archive_db")
        if step == "apply_one_current_latest_fact":
            enabled_now = bool(row.get("enabled_now"))
            blocked_until = [str(item) for item in _as_list(row.get("blocked_until")) if str(item or "").strip()]
            preconditions = [str(item) for item in _as_list(row.get("preconditions")) if str(item or "").strip()]
            if not bool(summary.get("ready_for_controlled_apply_now")) and enabled_now:
                blockers.append("current_latest_posterior_runbook_apply_enabled_before_ready")
            if not enabled_now and apply_required and not blocked_until:
                blockers.append("current_latest_posterior_runbook_apply_blocked_until_missing")
            if command and not preconditions:
                blockers.append("current_latest_posterior_runbook_apply_preconditions_missing")
            if command and not any("run_current_latest_temp_drill" in item for item in preconditions):
                blockers.append("current_latest_posterior_runbook_apply_missing_temp_drill_precondition")
            if command and not any("verify_writer_quiescence" in item for item in preconditions):
                blockers.append("current_latest_posterior_runbook_apply_missing_writer_quiescence_precondition")
            for fragment in (
                "scripts/apply_posterior_ssot_fact_repair.py",
                "--backup",
                "APPLY_POSTERIOR_SSOT_FACT_REPAIR",
                "--writer-quiescent",
            ):
                if fragment not in command:
                    blockers.append(f"current_latest_posterior_runbook_apply_command_missing_{fragment}")
            if shared_backup_path and shared_backup_path not in command:
                blockers.append("current_latest_posterior_runbook_apply_command_not_using_shared_backup")

    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "executes_sql_writes",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
                "fetches_external_weather_sources",
                "creates_backup",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("current_latest_posterior_runbook_unsafe_guard_enabled")

    if apply_required:
        for blocker in _as_list(summary.get("start_blockers")):
            warnings.append(f"current_latest_posterior_runbook_start_blocker:{blocker}")
        for blocker in _as_list(summary.get("risk_blockers")):
            warnings.append(f"current_latest_posterior_runbook_risk_blocker:{blocker}")
    if not bool(summary.get("ready_for_controlled_apply_now")) and apply_required:
        warnings.append("current_latest_posterior_runbook_not_ready_for_apply_now")

    return {
        "schema": "current_latest_posterior_apply_runbook_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "current_latest_posterior_apply_runbook_read_only_gate",
        "runbook_schema": str(summary.get("source_schema") or ""),
        "ready_for_controlled_apply_now": bool(summary.get("ready_for_controlled_apply_now")),
        "apply_required": apply_required,
        "candidate_fact_ids": candidate_fact_ids,
        "start_blockers": list(summary.get("start_blockers") or []),
        "risk_blockers": list(summary.get("risk_blockers") or []),
        "maintenance_window": dict(_as_mapping(summary.get("maintenance_window"))),
        "shared_backup": dict(shared_backup),
        "single_fact_preflights": [dict(row) for row in preflights],
        "missing_required_steps": missing_steps,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "operator_can_use_current_latest_posterior_runbook_when_writer_quiescent"
            if not blockers
            else "fix_current_latest_posterior_runbook_before_operator_apply"
        ),
    }


def _parameter_governance_surface_report(*, root: Path) -> dict[str, Any]:
    try:
        report = parameter_governance_surface_audit.build_audit(Path(root), max_env_rows=200)
    except Exception as exc:
        return _exception_report(
            "parameter_governance_surface_exception_v1",
            "parameter_governance_surface_exception",
            exc,
        )

    blockers: list[str] = []
    warnings = [str(item) for item in _as_list(report.get("warnings")) if str(item or "").strip()]
    guards = _as_mapping(report.get("guards")) or _guards()
    if report.get("read_only") is not True:
        blockers.append("parameter_governance_surface_not_read_only")
    if report.get("ok") is not True:
        blockers.append("parameter_governance_surface_not_ok")
    for blocker in _as_list(report.get("blockers")):
        text = str(blocker or "").strip()
        if text:
            blockers.append(text)
    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "changes_parameters",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "fetches_external_weather_sources",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("parameter_governance_surface_unsafe_guard_enabled")

    parameter_surfaces = _as_list(report.get("parameter_write_surfaces"))
    launchd_surfaces = _as_list(report.get("launchd_restart_surfaces"))
    config_surface = _as_mapping(report.get("config_surface"))
    return {
        "schema": "parameter_governance_surface_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "parameter_governance_surface_read_only_gate",
        "source_schema": str(report.get("schema") or ""),
        "parameter_surface_count": len(parameter_surfaces),
        "launchd_restart_surface_count": len(launchd_surfaces),
        "env_key_count": int(config_surface.get("env_key_count") or 0),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "parameter_governance_surface_ready_for_release_acceptance"
            if not blockers
            else "fix_parameter_governance_surface_before_release"
        ),
    }


def _fact_write_boundary_report(*, root: Path) -> dict[str, Any]:
    try:
        report = fact_write_migration_plan.build_fact_write_migration_plan(
            Path(root),
            db_path=None,
            sample_limit=3,
        )
    except Exception as exc:
        return _exception_report(
            "fact_write_boundary_exception_v1",
            "fact_write_boundary_exception",
            exc,
        )

    blockers: list[str] = []
    warnings: list[str] = []
    guards = _as_mapping(report.get("guards")) or _guards()
    if report.get("read_only") is not True:
        blockers.append("fact_write_boundary_not_read_only")
    if report.get("ok") is not True:
        blockers.append("fact_write_boundary_not_ok")
    pending_count = int(report.get("pending_migration_file_count") or 0)
    if pending_count:
        blockers.append("fact_write_boundary_pending_migration_files")
    lanes = [_as_mapping(row) for row in _as_list(report.get("lanes"))]
    unknown_lane = next((row for row in lanes if str(row.get("lane") or "") == "unknown_fact_writer_surface"), {})
    unknown_count = int(_as_mapping(unknown_lane).get("file_count") or 0)
    if unknown_count:
        blockers.append("fact_write_boundary_unknown_writer_surface_present")
    for lane in lanes:
        lane_name = str(lane.get("lane") or "")
        status = str(lane.get("status") or "")
        if lane_name and status in {"not_detected", "unknown", "unclassified"}:
            blockers.append(f"fact_write_boundary_lane_unclassified:{lane_name}")
    unsafe_guards = {
        str(key): value
        for key, value in guards.items()
        if (
            str(key).startswith("writes_")
            or str(key)
            in {
                "executes_sql",
                "places_orders",
                "restarts_launchd",
                "historical_backfill",
                "changes_parameters",
            }
        )
        and bool(value)
    }
    if unsafe_guards:
        blockers.append("fact_write_boundary_unsafe_guard_enabled")
    next_actions = [str(item) for item in _as_list(report.get("next_actions")) if str(item or "").strip()]
    if next_actions:
        warnings.extend(f"fact_write_boundary_next_action:{item}" for item in next_actions[:10])

    return {
        "schema": "fact_write_boundary_gate_v1",
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "fact_write_boundary_read_only_gate",
        "source_schema": str(report.get("schema") or ""),
        "target_owner": str(report.get("target_owner") or ""),
        "pending_migration_file_count": pending_count,
        "unknown_fact_writer_surface_count": unknown_count,
        "lane_count": len(lanes),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": guards,
        "next_boundary": (
            "fact_write_boundary_frozen_for_release_acceptance"
            if not blockers
            else "fix_fact_write_boundary_before_release"
        ),
    }


def _downstream_single_ssot_gate(
    *,
    core_gate: Mapping[str, Any],
    core_integration_gate: Mapping[str, Any],
    posterior_rematerialization_gate: Mapping[str, Any],
    awc_contract_gate: Mapping[str, Any],
    awc_replay_gate: Mapping[str, Any],
    awc_signal_timing_gate: Mapping[str, Any],
    awc_terminal_coverage_gate: Mapping[str, Any],
    order_lineage_coverage_gate: Mapping[str, Any],
    signal_to_order_safety_gate: Mapping[str, Any],
    phase0_schema_gate: Mapping[str, Any],
    production_identity_gate: Mapping[str, Any],
    hierarchical_joint_closeout_gate: Mapping[str, Any],
    controlled_apply_packet_gate: Mapping[str, Any],
    post_apply_resolution_packet_gate: Mapping[str, Any],
    production_apply_preflight_gate: Mapping[str, Any],
    current_latest_posterior_runbook_gate: Mapping[str, Any],
    daily_review_gate: Mapping[str, Any],
    parameter_governance_gate: Mapping[str, Any],
    fact_write_boundary_gate: Mapping[str, Any],
) -> dict[str, Any]:
    dependencies = {
        "core_probability_release_gate": bool(core_gate.get("ok")),
        "hierarchical_core_integration_contract_gate": bool(core_integration_gate.get("ok")),
        "posterior_rematerialization_governance_gate": bool(posterior_rematerialization_gate.get("ok")),
        "awc_signal_to_order_contract_gate": bool(awc_contract_gate.get("ok")),
        "awc_signal_to_order_replay_gate": bool(awc_replay_gate.get("ok")),
        "awc_signal_timing_contract_gate": bool(awc_signal_timing_gate.get("ok")),
        "awc_hard_source_terminal_coverage_gate": bool(awc_terminal_coverage_gate.get("ok")),
        "order_lineage_coverage_gate": bool(order_lineage_coverage_gate.get("ok")),
        "signal_to_order_safety_contract_gate": bool(signal_to_order_safety_gate.get("ok")),
        "phase0_schema_apply_contract_gate": bool(phase0_schema_gate.get("ok")),
        "production_identity_coverage_gate": bool(production_identity_gate.get("ok")),
        "hierarchical_joint_closeout_operator_runbook_gate": bool(
            hierarchical_joint_closeout_gate.get("ok")
        ),
        "daily_review_controlled_apply_packet_gate": bool(controlled_apply_packet_gate.get("ok")),
        "daily_review_post_apply_resolution_packet_gate": bool(post_apply_resolution_packet_gate.get("ok")),
        "daily_review_production_apply_preflight_packet_gate": bool(production_apply_preflight_gate.get("ok")),
        "current_latest_posterior_apply_runbook_gate": bool(current_latest_posterior_runbook_gate.get("ok")),
        "daily_review_post_apply_gate": bool(daily_review_gate.get("ok")),
        "parameter_governance_surface_gate": bool(parameter_governance_gate.get("ok")),
        "fact_write_boundary_gate": bool(fact_write_boundary_gate.get("ok")),
    }
    blockers = [
        {
            "blocker": "downstream_single_ssot_dependency_not_ok",
            "dependency": name,
        }
        for name, ok in dependencies.items()
        if not ok
    ]
    return {
        "gate": "downstream_single_ssot_gate",
        "ok": not blockers,
        "description": "UI, trading, AWC timing, and daily review consume the same posterior_ssot_fact without a second target, posterior, or blocker source.",
        "dependencies": dependencies,
        "reports": {},
        "blockers": blockers,
        "next_boundary": "plan_coverage_complete" if not blockers else "fix_downstream_single_ssot_dependencies",
    }


def _actionable_diagnostics(
    *,
    awc_terminal_coverage_report: Mapping[str, Any],
    awc_terminal_closeout_report: Mapping[str, Any],
    awc_terminal_apply_contract_report: Mapping[str, Any],
    awc_terminal_operator_runbook_report: Mapping[str, Any],
    posterior_rematerialization_report: Mapping[str, Any],
    production_identity_closeout_report: Mapping[str, Any],
    production_identity_closeout_runbook_report: Mapping[str, Any],
    hierarchical_joint_closeout_runbook_report: Mapping[str, Any],
    production_apply_preflight_report: Mapping[str, Any],
    current_latest_posterior_runbook_report: Mapping[str, Any],
    daily_review_report: Mapping[str, Any],
    parameter_governance_report: Mapping[str, Any],
    fact_write_boundary_report: Mapping[str, Any],
    max_items_output: int,
) -> dict[str, Any]:
    max_items = max(0, int(max_items_output))
    awc_terminal = awc_terminal_coverage_report
    remat = _as_mapping(posterior_rematerialization_report.get("historical_rematerialization_plan"))
    forward = _as_mapping(posterior_rematerialization_report.get("forward_write_acceptance"))
    identity_duplicate = _as_mapping(production_identity_closeout_report.get("duplicate"))
    identity_audit_indexes = _as_mapping(production_identity_closeout_report.get("identity_audit_indexes"))
    identity_merge_fill = _as_mapping(production_identity_closeout_report.get("merge_fill"))
    identity_maintenance = _as_mapping(production_identity_closeout_report.get("maintenance_window"))
    identity_runbook_artifacts = _as_mapping(production_identity_closeout_runbook_report.get("artifacts"))
    identity_runbook_scope = _as_mapping(production_identity_closeout_runbook_report.get("identity_scope"))
    joint_closeout_impact = _as_mapping(hierarchical_joint_closeout_runbook_report.get("impact_summary"))
    current_latest = _as_mapping(production_apply_preflight_report.get("current_latest_posterior_materialization"))
    maintenance = _as_mapping(production_apply_preflight_report.get("maintenance_window"))
    shared_backup = _as_mapping(current_latest_posterior_runbook_report.get("shared_backup"))
    preflights = [
        {
            "fact_id": _as_mapping(row).get("fact_id"),
            "ok": bool(_as_mapping(row).get("ok")),
            "risk_level": _as_mapping(row).get("risk_level"),
            "projected_target_bucket": _as_mapping(row).get("projected_target_bucket"),
            "projected_target_probability": _as_mapping(row).get("projected_target_probability"),
            "uses_shared_backup": bool(_as_mapping(row).get("uses_shared_backup")),
            "apply_command_available": bool(_as_mapping(row).get("apply_command_available")),
            "blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(_as_mapping(row).get("blockers"))[:max_items]
            ],
        }
        for row in _as_list(current_latest_posterior_runbook_report.get("single_fact_preflights"))[
            :max_items
        ]
    ]
    coverage = _as_mapping(daily_review_report.get("coverage"))
    return {
        "awc_hard_source_terminal_coverage": {
            "ok": bool(awc_terminal.get("ok")),
            "accepted_signal_count": int(awc_terminal.get("accepted_signal_count") or 0),
            "submitted_signal_count": int(awc_terminal.get("submitted_signal_count") or 0),
            "terminal_state_counts": dict(_as_mapping(awc_terminal.get("terminal_state_counts"))),
            "non_final_terminal_signal_ids": list(
                awc_terminal.get("non_final_terminal_signal_ids") or []
            )[:max_items],
            "missing_terminal_signal_ids": list(
                awc_terminal.get("missing_terminal_signal_ids") or []
            )[:max_items],
            "missing_ledger_signal_ids": list(
                awc_terminal.get("missing_ledger_signal_ids") or []
            )[:max_items],
            "blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(awc_terminal.get("blockers"))[:max_items]
            ],
            "closeout": {
                "ok": bool(awc_terminal_closeout_report.get("ok")),
                "candidate_count": int(awc_terminal_closeout_report.get("candidate_count") or 0),
                "recommended_materialize_count": int(
                    awc_terminal_closeout_report.get("recommended_materialize_count") or 0
                ),
                "operator_review_required_count": int(
                    awc_terminal_closeout_report.get("operator_review_required_count") or 0
                ),
                "manifest_provided": bool(awc_terminal_closeout_report.get("manifest_provided")),
                "operation_count": int(awc_terminal_closeout_report.get("operation_count") or 0),
                "hard_blockers": [
                    _compact_summary_blocker(item, max_items_output=max_items)
                    for item in _as_list(awc_terminal_closeout_report.get("hard_blockers"))[:max_items]
                ],
                "next_boundary": awc_terminal_closeout_report.get("next_boundary"),
            },
            "apply_contract": {
                "ok": bool(awc_terminal_apply_contract_report.get("ok")),
                "operation_count": int(awc_terminal_apply_contract_report.get("operation_count") or 0),
                "blockers": [
                    _compact_summary_blocker(item, max_items_output=max_items)
                    for item in _as_list(awc_terminal_apply_contract_report.get("blockers"))[:max_items]
                ],
                "confirmation_phrase": _as_mapping(
                    awc_terminal_apply_contract_report.get("operator_contract")
                ).get("confirmation_phrase"),
                "requires_backup": bool(
                    _as_mapping(awc_terminal_apply_contract_report.get("operator_contract")).get("requires_backup")
                ),
                "requires_writer_quiescence": bool(
                    _as_mapping(awc_terminal_apply_contract_report.get("operator_contract")).get("requires_writer_quiescence")
                ),
                "next_boundary": awc_terminal_apply_contract_report.get("next_boundary"),
            },
            "operator_runbook": {
                "ok": bool(awc_terminal_operator_runbook_report.get("ok")),
                "ready_for_controlled_apply_now": bool(
                    awc_terminal_operator_runbook_report.get("ready_for_controlled_apply_now")
                ),
                "start_blockers": [
                    _compact_summary_blocker(item, max_items_output=max_items)
                    for item in _as_list(awc_terminal_operator_runbook_report.get("start_blockers"))[:max_items]
                ],
                "decision_manifest_path": _as_mapping(
                    awc_terminal_operator_runbook_report.get("artifacts")
                ).get("decision_manifest_path"),
                "review_report_path": _as_mapping(
                    awc_terminal_operator_runbook_report.get("artifacts")
                ).get("review_report_path"),
                "shared_backup_path": _as_mapping(
                    awc_terminal_operator_runbook_report.get("artifacts")
                ).get("shared_backup_path"),
                "review_report_steps": list(
                    _as_mapping(awc_terminal_operator_runbook_report.get("impact_summary")).get(
                        "review_report_steps"
                    )
                    or []
                )[:max_items],
                "archive_db_apply_steps": list(
                    _as_mapping(awc_terminal_operator_runbook_report.get("impact_summary")).get(
                        "archive_db_apply_steps"
                    )
                    or []
                )[:max_items],
                "manifest_write_steps": list(
                    _as_mapping(awc_terminal_operator_runbook_report.get("impact_summary")).get(
                        "manifest_write_steps"
                    )
                    or []
                )[:max_items],
                "confirmation_phrase": _as_mapping(
                    awc_terminal_operator_runbook_report.get("confirmation_phrases")
                ).get("apply_awc_legacy_terminal_closeout"),
                "next_boundary": awc_terminal_operator_runbook_report.get("next_boundary"),
            },
        },
        "posterior_rematerialization": {
            "candidate_count": int(remat.get("candidate_count") or 0),
            "current_latest_issue_count": int(remat.get("current_latest_issue_count") or 0),
            "historical_only_issue_count": int(remat.get("historical_only_issue_count") or 0),
            "current_latest_projected_ok_count": int(remat.get("current_latest_projected_ok_count") or 0),
            "current_latest_projected_issue_count": int(remat.get("current_latest_projected_issue_count") or 0),
            "forward_projected_ok_count": int(forward.get("projected_ok_count") or 0),
            "forward_projected_issue_count": int(forward.get("projected_issue_count") or 0),
            "recommended_apply_mode": remat.get("recommended_apply_mode"),
        },
        "production_identity_closeout": {
            "packet_ok": bool(production_identity_closeout_report.get("ok")),
            "operator_runbook_ok": bool(production_identity_closeout_runbook_report.get("ok")),
            "operator_runbook_ready_for_controlled_apply_now": bool(
                production_identity_closeout_runbook_report.get("ready_for_controlled_apply_now")
            ),
            "duplicate_row_count": int(identity_duplicate.get("row_count") or 0),
            "duplicate_reviewed_apply_contract_ok": bool(identity_duplicate.get("reviewed_apply_contract_ok")),
            "duplicate_reviewed_apply_operation_count": int(
                identity_duplicate.get("reviewed_apply_operation_count") or 0
            ),
            "identity_audit_index_contract_ok": bool(identity_audit_indexes.get("contract_ok")),
            "identity_audit_index_already_satisfied": bool(identity_audit_indexes.get("already_satisfied")),
            "identity_audit_index_needs_apply": bool(identity_audit_indexes.get("needs_apply")),
            "identity_audit_index_ddl_count": int(identity_audit_indexes.get("ddl_count") or 0),
            "identity_audit_index_contract_blocker_count": int(
                identity_audit_indexes.get("contract_blocker_count") or 0
            ),
            "identity_audit_index_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(identity_audit_indexes.get("blockers"))[:max_items]
            ],
            "identity_audit_index_apply_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(identity_audit_indexes.get("apply_blockers"))[:max_items]
            ],
            "merge_fill_contract_stage": identity_merge_fill.get("contract_stage"),
            "merge_fill_contract_ok": bool(identity_merge_fill.get("contract_ok")),
            "merge_fill_expected_ready_after_duplicate_repair": bool(
                identity_merge_fill.get("expected_to_be_ready_after_duplicate_repair_apply")
            ),
            "shared_backup_path": production_identity_closeout_report.get("shared_backup_path"),
            "duplicate_manifest_path": production_identity_closeout_report.get("duplicate_manifest_path"),
            "runbook_shared_backup_path": identity_runbook_artifacts.get("shared_backup_path"),
            "runbook_duplicate_manifest_path": identity_runbook_artifacts.get("duplicate_manifest_path"),
            "runbook_merge_fill_operation_count": int(identity_runbook_scope.get("merge_fill_operation_count") or 0),
            "maintenance_writer_quiescent": bool(identity_maintenance.get("writer_quiescent")),
            "hard_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(production_identity_closeout_report.get("hard_blockers"))[:max_items]
            ],
            "blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(production_identity_closeout_report.get("blockers"))[:max_items]
            ],
            "runbook_start_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(production_identity_closeout_runbook_report.get("start_blockers"))[:max_items]
            ],
            "runbook_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(production_identity_closeout_runbook_report.get("blockers"))[:max_items]
            ],
        },
        "hierarchical_joint_closeout": {
            "ok": bool(hierarchical_joint_closeout_runbook_report.get("ok")),
            "ready_for_controlled_apply_now": bool(
                hierarchical_joint_closeout_runbook_report.get("ready_for_controlled_apply_now")
            ),
            "start_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(hierarchical_joint_closeout_runbook_report.get("start_blockers"))[:max_items]
            ],
            "archive_db_apply_steps": list(
                joint_closeout_impact.get("archive_db_apply_steps") or []
            )[:max_items],
            "manifest_write_steps": list(
                joint_closeout_impact.get("manifest_write_steps") or []
            )[:max_items],
            "backup_steps": list(joint_closeout_impact.get("backup_steps") or [])[:max_items],
            "service_control_steps": list(
                joint_closeout_impact.get("service_control_steps") or []
            )[:max_items],
            "confirmation_phrases": dict(
                _as_mapping(hierarchical_joint_closeout_runbook_report.get("confirmation_phrases"))
            ),
            "blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(hierarchical_joint_closeout_runbook_report.get("blockers"))[:max_items]
            ],
            "next_boundary": hierarchical_joint_closeout_runbook_report.get("next_boundary"),
        },
        "current_latest_posterior_apply": {
            "apply_required": bool(current_latest_posterior_runbook_report.get("apply_required")),
            "ready_for_controlled_apply_now": bool(
                current_latest_posterior_runbook_report.get("ready_for_controlled_apply_now")
            ),
            "candidate_fact_ids": list(current_latest_posterior_runbook_report.get("candidate_fact_ids") or []),
            "production_preflight_candidate_fact_ids": list(current_latest.get("candidate_fact_ids") or []),
            "start_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(current_latest_posterior_runbook_report.get("start_blockers"))[:max_items]
            ],
            "risk_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(current_latest_posterior_runbook_report.get("risk_blockers"))[:max_items]
            ],
            "shared_backup_path": shared_backup.get("path"),
            "single_fact_preflights": preflights,
        },
        "production_apply": {
            "ready_to_start_controlled_sequence": bool(
                production_apply_preflight_report.get("ready_to_start_controlled_sequence")
            ),
            "start_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(production_apply_preflight_report.get("start_blockers"))[:max_items]
            ],
            "sequence_dependency_blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(production_apply_preflight_report.get("sequence_dependency_blockers"))[:max_items]
            ],
            "maintenance_writer_quiescent": bool(maintenance.get("writer_quiescent")),
            "maintenance_required_stop_labels": list(maintenance.get("required_stop_labels") or []),
            "maintenance_operator_commands": dict(_as_mapping(maintenance.get("operator_commands"))),
            "maintenance_stop_authorization": dict(_as_mapping(maintenance.get("stop_authorization"))),
        },
        "daily_review_post_apply": {
            "settled_event_total_count": int(coverage.get("settled_event_total_count") or 0),
            "reviewed_event_count": int(coverage.get("reviewed_event_count") or 0),
            "missing_review_count": int(coverage.get("missing_review_count") or 0),
            "coverage_scope_truncated_count": int(coverage.get("coverage_scope_truncated_count") or 0),
            "item_blocker_counts": dict(_as_mapping(coverage.get("item_blocker_counts"))),
            "item_blocker_samples": [
                _compact_summary_blocker(row, max_items_output=max_items)
                for row in _as_list(daily_review_report.get("item_blocker_samples"))[:max_items]
            ],
        },
        "parameter_governance": {
            "parameter_surface_count": int(parameter_governance_report.get("parameter_surface_count") or 0),
            "launchd_restart_surface_count": int(parameter_governance_report.get("launchd_restart_surface_count") or 0),
            "env_key_count": int(parameter_governance_report.get("env_key_count") or 0),
            "blockers": [
                _compact_summary_blocker(item, max_items_output=max_items)
                for item in _as_list(parameter_governance_report.get("blockers"))[:max_items]
            ],
        },
        "fact_write_boundary": {
            "target_owner": fact_write_boundary_report.get("target_owner"),
            "pending_migration_file_count": int(fact_write_boundary_report.get("pending_migration_file_count") or 0),
            "unknown_fact_writer_surface_count": int(
                fact_write_boundary_report.get("unknown_fact_writer_surface_count") or 0
            ),
            "lane_count": int(fact_write_boundary_report.get("lane_count") or 0),
            "blockers": list(fact_write_boundary_report.get("blockers") or [])[:10],
        },
    }


def build_hierarchical_joint_plan_coverage_audit(
    *,
    root: Path = ROOT,
    db_path: Path = DEFAULT_DB,
    since_hours: float = 168.0,
    latest_only: bool = True,
    active_only: bool = False,
    project_forward: bool = True,
    limit: int = 50,
    max_items_output: int = 5,
    min_checked_facts: int = 1,
    ui_mode: str = "summary",
    ui_run_date: str = "",
    ui_limit: int = 120,
    ui_city_filter: str = "",
    run_ui_replay: bool = True,
    replay_limit: int = 50,
    require_awc_signal_sample: bool = False,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    daily_review_scope_limit: int = 1000000,
    include_daily_review_ui_snapshot_audit: bool = True,
    require_daily_review_ui_visible: bool = False,
    ignore_legacy_order_lineage: bool = True,
    order_lineage_since_hours: float = 0.0,
    ignore_orders_before: str = "2026-07-07",
    trace_stages: bool = False,
) -> dict[str, Any]:
    db = Path(db_path)
    stage_timings: list[dict[str, Any]] = []

    def stage(name: str, fn):
        if trace_stages:
            print(f"[hierarchical_joint_plan_coverage_audit] START {name}", file=sys.stderr, flush=True)
        started = time.monotonic()
        try:
            return fn()
        finally:
            elapsed = time.monotonic() - started
            stage_timings.append({"stage": name, "elapsed_sec": round(elapsed, 3)})
            if trace_stages:
                print(
                    f"[hierarchical_joint_plan_coverage_audit] DONE {name} {elapsed:.3f}s",
                    file=sys.stderr,
                    flush=True,
                )

    core_report = stage(
        "core_probability_release",
        lambda: probability_gate.build_hierarchical_probability_release_gate(
            root=Path(root),
            db_path=db,
            since_hours=float(since_hours),
            latest_only=bool(latest_only),
            active_only=bool(active_only),
            project_forward=bool(project_forward),
            limit=int(limit),
            max_items_output=int(max_items_output),
            min_checked_facts=max(1, int(min_checked_facts or 1)),
            ui_mode=str(ui_mode or "summary"),
            ui_run_date=str(ui_run_date or ""),
            ui_limit=int(ui_limit),
            ui_city_filter=str(ui_city_filter or ""),
            run_ui_replay=bool(run_ui_replay),
            replay_limit=int(replay_limit),
            require_awc_signal_sample=False,
        ),
    )
    core_integration_report = stage("core_integration_contract", _core_integration_contract_report)
    awc_contract_report = stage("awc_signal_to_order_contract", _awc_signal_to_order_contract_report)
    posterior_rematerialization_report = stage(
        "posterior_rematerialization",
        lambda: _posterior_rematerialization_report(
            db_path=db,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
        ),
    )
    awc_replay_report = stage(
        "awc_signal_to_order_replay",
        lambda: _awc_signal_to_order_replay_report(
            db_path=db,
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
            require_awc_signal_sample=bool(require_awc_signal_sample),
        ),
    )
    awc_signal_timing_report = stage(
        "awc_signal_timing_contract",
        lambda: _awc_signal_timing_contract_report(root=Path(root)),
    )
    awc_terminal_closeout_report = stage(
        "awc_legacy_terminal_closeout_packet",
        lambda: _awc_legacy_terminal_closeout_packet_report(
            db_path=db,
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
        ),
    )
    awc_terminal_coverage_report = stage(
        "awc_hard_source_terminal_coverage",
        lambda: _awc_hard_source_terminal_coverage_report(
            db_path=db,
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
            require_awc_signal_sample=bool(require_awc_signal_sample),
            replay_report=_as_mapping(awc_terminal_closeout_report.get("_replay_report")) or None,
        ),
    )
    order_lineage_coverage_report = stage(
        "order_lineage_coverage",
        lambda: _order_lineage_coverage_report(
            db_path=db,
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
            ignore_legacy_orders=bool(ignore_legacy_order_lineage),
            order_since_hours=float(order_lineage_since_hours),
            ignore_orders_before=str(ignore_orders_before or ""),
        ),
    )
    awc_terminal_apply_contract_report = stage(
        "awc_legacy_terminal_apply_contract",
        lambda: _awc_legacy_terminal_closeout_apply_contract_report(
            db_path=db,
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
            closeout_packet=_as_mapping(awc_terminal_closeout_report.get("_closeout_packet")) or None,
        ),
    )
    awc_terminal_operator_runbook_report = stage(
        "awc_legacy_terminal_operator_runbook",
        lambda: _awc_legacy_terminal_closeout_operator_runbook_report(
            db_path=db,
            since_hours=float(since_hours),
            replay_limit=int(replay_limit),
            max_items_output=int(max_items_output),
            closeout_packet=_as_mapping(awc_terminal_closeout_report.get("_closeout_packet")) or None,
            apply_contract_report=_as_mapping(awc_terminal_apply_contract_report.get("_apply_contract")) or None,
        ),
    )
    signal_to_order_safety_report = stage(
        "signal_to_order_safety_contract",
        lambda: _signal_to_order_safety_contract_report(root=Path(root)),
    )
    phase0_schema_report = stage("phase0_schema_apply_contract", lambda: _phase0_schema_apply_contract_report(db_path=db))
    production_identity_report = stage(
        "production_identity_coverage",
        lambda: _production_identity_coverage_report(
            db_path=db,
            max_items_output=int(max_items_output),
        ),
    )
    production_identity_duplicate_contract_report = stage(
        "production_identity_duplicate_contract",
        lambda: _production_identity_duplicate_apply_contract_report(
            db_path=db,
            max_items_output=int(max_items_output),
        ),
    )
    production_identity_merge_fill_contract_report = stage(
        "production_identity_merge_fill_contract",
        lambda: _production_identity_merge_fill_apply_contract_report(
            db_path=db,
            max_items_output=int(max_items_output),
            coverage_report=production_identity_report,
        ),
    )
    production_identity_closeout_packet_report = stage(
        "production_identity_closeout_packet",
        lambda: _production_identity_closeout_confirmation_packet_report(
            db_path=db,
            max_items_output=int(max_items_output),
            merge_fill_contract_summary=_as_mapping(
                production_identity_merge_fill_contract_report.get("contract_summary")
            ),
        ),
    )
    production_identity_closeout_operator_runbook_report = stage(
        "production_identity_closeout_operator_runbook",
        lambda: _production_identity_closeout_operator_runbook_report(
            db_path=db,
            max_items_output=int(max_items_output),
            closeout_packet=_as_mapping(production_identity_closeout_packet_report.get("_closeout_packet")) or None,
        ),
    )
    hierarchical_joint_closeout_runbook_report = stage(
        "hierarchical_joint_closeout_runbook",
        lambda: _hierarchical_joint_closeout_operator_runbook_report(
            db_path=db,
            awc_terminal_operator_runbook_report=awc_terminal_operator_runbook_report,
            production_identity_closeout_operator_runbook_report=production_identity_closeout_operator_runbook_report,
        ),
    )
    controlled_apply_packet_report = stage(
        "daily_review_controlled_apply_packet",
        lambda: _daily_review_controlled_apply_packet_report(
            db_path=db,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=int(limit),
            max_items_output=int(max_items_output),
        ),
    )
    post_apply_resolution_packet_report = stage(
        "daily_review_post_apply_resolution_packet",
        lambda: _daily_review_post_apply_resolution_packet_report(
            db_path=db,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=int(limit),
            max_items_output=int(max_items_output),
            master_confirmation_packet=_as_mapping(
                controlled_apply_packet_report.get("_controlled_apply_packet")
            )
            or None,
        ),
    )
    production_apply_preflight_packet_report = stage(
        "daily_review_production_apply_preflight_packet",
        lambda: _daily_review_production_apply_preflight_packet_report(
            db_path=db,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=int(limit),
            max_items_output=int(max_items_output),
            resolution_confirmation_packet=_as_mapping(
                post_apply_resolution_packet_report.get("_post_apply_resolution_packet")
            )
            or None,
        ),
    )
    current_latest_posterior_runbook_report = stage(
        "current_latest_posterior_runbook",
        lambda: _current_latest_posterior_apply_runbook_report(
            db_path=db,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
            production_preflight_packet=_as_mapping(
                production_apply_preflight_packet_report.get("_production_preflight_packet")
            )
            or None,
        ),
    )
    daily_review_report = stage(
        "daily_review_post_apply_acceptance",
        lambda: _daily_review_acceptance_report(
            db_path=db,
            since_days=int(since_days),
            since_date=str(since_date or ""),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            market_scope=str(market_scope or "highest_temperature"),
            limit=max(1, int(daily_review_scope_limit)),
            max_items_output=int(max_items_output),
            since_hours=max(float(since_hours), float(24 * 90)),
            replay_limit=max(int(replay_limit), 500),
            include_ui_snapshot_audit=bool(include_daily_review_ui_snapshot_audit),
            ui_snapshot_mode=str(ui_mode or "summary"),
            ui_snapshot_run_date=str(ui_run_date or ""),
            ui_snapshot_limit=int(ui_limit),
            ui_snapshot_city_filter=str(ui_city_filter or ""),
            require_ui_visible=bool(require_daily_review_ui_visible),
            lineage_decision_manifest=_as_mapping(
                post_apply_resolution_packet_report.get("_lineage_decision_manifest")
            )
            or None,
        ),
    )
    parameter_governance_report = stage(
        "parameter_governance_surface",
        lambda: _parameter_governance_surface_report(root=Path(root)),
    )
    fact_write_boundary_report = stage(
        "fact_write_boundary",
        lambda: _fact_write_boundary_report(root=Path(root)),
    )

    core_gate = _make_gate(
        gate="core_probability_release_gate",
        description="Prior, weak/strong likelihood, posterior, and posterior SSOT pass the existing probability release gate.",
        reports={"hierarchical_probability_release": core_report},
        continue_to="hierarchical_core_integration_contract_gate",
    )
    core_integration_gate = _make_gate(
        gate="hierarchical_core_integration_contract_gate",
        description="Posterior SSOT fact flows through fact writer, UI snapshot lineage, auto-trade candidate, and daily review/calibration without a second target口径.",
        reports={"hierarchical_core_integration_contract": core_integration_report},
        continue_to="posterior_rematerialization_governance_gate",
    )
    awc_contract_gate = _make_gate(
        gate="awc_signal_to_order_contract_gate",
        description="AWC +5/+6 hard-source signal has a 3s timing contract from hard-source acceptance to order submission.",
        reports={"awc_signal_to_order_contract": awc_contract_report},
        continue_to="awc_signal_to_order_replay_gate",
    )
    posterior_rematerialization_gate = _make_gate(
        gate="posterior_rematerialization_governance_gate",
        description="Historical posterior SSOT rematerialization candidates are classified so current latest gaps block and superseded source-only history remains operator-governed.",
        reports={"posterior_rematerialization_governance": posterior_rematerialization_report},
        continue_to="awc_signal_to_order_contract_gate",
    )
    awc_replay_gate = _make_gate(
        gate="awc_signal_to_order_replay_gate",
        description="Accepted AWC hard-source signals replay through SSOT, posterior, decision, and execution timing ledger.",
        reports={"ssot_chain_replay": awc_replay_report},
        continue_to="awc_signal_timing_contract_gate",
    )
    awc_signal_timing_gate = _make_gate(
        gate="awc_signal_timing_contract_gate",
        description="AWC hourly signal timing contract requires validTime, signal detection/fetch, SSOT, decision, job, orderbook, submit, and exchange-ack timestamps.",
        reports={"awc_signal_timing_contract": awc_signal_timing_report},
        continue_to="awc_hard_source_terminal_coverage_gate",
    )
    awc_terminal_coverage_gate = _make_gate(
        gate="awc_hard_source_terminal_coverage_gate",
        description="Every accepted production AWC hard-source signal has one final terminal state and a signal-to-order ledger record.",
        reports={"awc_hard_source_terminal_coverage": awc_terminal_coverage_report},
        continue_to="order_lineage_coverage_gate",
    )
    order_lineage_coverage_gate = _make_gate(
        gate="order_lineage_coverage_gate",
        description="Every auto order row carries structured lineage back to one order id, correlation id, order intent id, decision id, and signal id.",
        reports={"order_lineage_coverage": order_lineage_coverage_report},
        continue_to="signal_to_order_safety_contract_gate",
    )
    signal_to_order_safety_gate = _make_gate(
        gate="signal_to_order_safety_contract_gate",
        description="3s execution safety contract blocks stale decision, stale posterior, stale orderbook, and duplicate order paths without bypassing idempotency, orderbook, or risk checks.",
        reports={"signal_to_order_safety_contract": signal_to_order_safety_report},
        continue_to="phase0_schema_apply_contract_gate",
    )
    phase0_schema_gate = _make_gate(
        gate="phase0_schema_apply_contract_gate",
        description="Forward schema and daily-review planned tables have a no-apply contract, explicit preflight blockers, and post-apply verification commands.",
        reports={"phase0_schema_apply_contract": phase0_schema_report},
        continue_to="production_identity_coverage_gate",
    )
    production_identity_gate = _make_production_identity_gate(
        production_identity_report=production_identity_report,
        duplicate_contract_report=production_identity_duplicate_contract_report,
        merge_fill_contract_report=production_identity_merge_fill_contract_report,
        closeout_packet_report=production_identity_closeout_packet_report,
        closeout_operator_runbook_report=production_identity_closeout_operator_runbook_report,
        continue_to="hierarchical_joint_closeout_operator_runbook_gate",
    )
    hierarchical_joint_closeout_gate = _make_gate(
        gate="hierarchical_joint_closeout_operator_runbook_gate",
        description="AWC terminal closeout and production identity closeout are composed into one maintenance-window operator runbook with a fixed AWC -> duplicate repair -> identity merge-fill archive apply order.",
        reports={"hierarchical_joint_closeout_operator_runbook": hierarchical_joint_closeout_runbook_report},
        continue_to="daily_review_controlled_apply_packet_gate",
    )
    controlled_apply_packet_gate = _make_gate(
        gate="daily_review_controlled_apply_packet_gate",
        description="Phase0 schema, lineage repair, daily review persistence, and post-apply verification are composed into one read-only controlled operator packet.",
        reports={"daily_review_controlled_apply_packet": controlled_apply_packet_report},
        continue_to="daily_review_post_apply_resolution_packet_gate",
    )
    post_apply_resolution_gate = _make_gate(
        gate="daily_review_post_apply_resolution_packet_gate",
        description="Remaining post-apply blockers are composed into one read-only resolution packet covering posterior SSOT repair, missing fact exclusion, legacy AWC anchors, and daily review continuation.",
        reports={"daily_review_post_apply_resolution_packet": post_apply_resolution_packet_report},
        continue_to="daily_review_production_apply_preflight_packet_gate",
    )
    production_apply_preflight_gate = _make_gate(
        gate="daily_review_production_apply_preflight_packet_gate",
        description="Production controlled-apply start conditions are exposed in one read-only preflight packet covering writer quiescence, current-latest posterior materialization, and ordered apply stages.",
        reports={"daily_review_production_apply_preflight_packet": production_apply_preflight_packet_report},
        continue_to="current_latest_posterior_apply_runbook_gate",
    )
    current_latest_posterior_runbook_gate = _make_gate(
        gate="current_latest_posterior_apply_runbook_gate",
        description="Current-latest posterior SSOT materialization debt has a no-write runbook with exact fact ids, one shared backup, single-fact preflight/apply commands, and post-apply verification.",
        reports={"current_latest_posterior_apply_runbook": current_latest_posterior_runbook_report},
        continue_to="daily_review_post_apply_gate",
    )
    daily_review_gate = _make_gate(
        gate="daily_review_post_apply_gate",
        description="Settled weather markets have persisted daily reviews that replay to the same fact snapshot and posterior run.",
        reports={"daily_review_post_apply_acceptance": daily_review_report},
        continue_to="parameter_governance_surface_gate",
    )
    parameter_governance_gate = _make_gate(
        gate="parameter_governance_surface_gate",
        description="Parameter/config/restart surfaces are inventoried, default no-write, and not allowed to change production parameters without approved governance.",
        reports={"parameter_governance_surface": parameter_governance_report},
        continue_to="fact_write_boundary_gate",
    )
    fact_write_boundary_gate = _make_gate(
        gate="fact_write_boundary_gate",
        description="Fact-layer write surfaces are classified, unknown writer surfaces are absent, and historical/backfill writes remain outside this release path.",
        reports={"fact_write_boundary": fact_write_boundary_report},
        continue_to="downstream_single_ssot_gate",
    )
    downstream_gate = _downstream_single_ssot_gate(
        core_gate=core_gate,
        core_integration_gate=core_integration_gate,
        posterior_rematerialization_gate=posterior_rematerialization_gate,
        awc_contract_gate=awc_contract_gate,
        awc_replay_gate=awc_replay_gate,
        awc_signal_timing_gate=awc_signal_timing_gate,
        awc_terminal_coverage_gate=awc_terminal_coverage_gate,
        order_lineage_coverage_gate=order_lineage_coverage_gate,
        signal_to_order_safety_gate=signal_to_order_safety_gate,
        phase0_schema_gate=phase0_schema_gate,
        production_identity_gate=production_identity_gate,
        hierarchical_joint_closeout_gate=hierarchical_joint_closeout_gate,
        controlled_apply_packet_gate=controlled_apply_packet_gate,
        post_apply_resolution_packet_gate=post_apply_resolution_gate,
        production_apply_preflight_gate=production_apply_preflight_gate,
        current_latest_posterior_runbook_gate=current_latest_posterior_runbook_gate,
        daily_review_gate=daily_review_gate,
        parameter_governance_gate=parameter_governance_gate,
        fact_write_boundary_gate=fact_write_boundary_gate,
    )
    gates = [
        core_gate,
        core_integration_gate,
        posterior_rematerialization_gate,
        awc_contract_gate,
        awc_replay_gate,
        awc_signal_timing_gate,
        awc_terminal_coverage_gate,
        order_lineage_coverage_gate,
        signal_to_order_safety_gate,
        phase0_schema_gate,
        production_identity_gate,
        hierarchical_joint_closeout_gate,
        controlled_apply_packet_gate,
        post_apply_resolution_gate,
        production_apply_preflight_gate,
        current_latest_posterior_runbook_gate,
        daily_review_gate,
        parameter_governance_gate,
        fact_write_boundary_gate,
        downstream_gate,
    ]
    blockers = [
        dict(blocker)
        for gate in gates
        for blocker in gate.get("blockers") or []
        if isinstance(blocker, Mapping)
    ]
    warnings = sorted(
        {
            str(warning)
            for report in (
                core_report,
                core_integration_report,
                awc_contract_report,
                posterior_rematerialization_report,
                awc_replay_report,
                awc_signal_timing_report,
                awc_terminal_coverage_report,
                order_lineage_coverage_report,
                awc_terminal_closeout_report,
                awc_terminal_apply_contract_report,
                awc_terminal_operator_runbook_report,
                signal_to_order_safety_report,
                phase0_schema_report,
                production_identity_report,
                production_identity_duplicate_contract_report,
                production_identity_merge_fill_contract_report,
                production_identity_closeout_packet_report,
                production_identity_closeout_operator_runbook_report,
                hierarchical_joint_closeout_runbook_report,
                controlled_apply_packet_report,
                post_apply_resolution_packet_report,
                production_apply_preflight_packet_report,
                current_latest_posterior_runbook_report,
                daily_review_report,
                parameter_governance_report,
                fact_write_boundary_report,
            )
            for warning in _as_list(report.get("warnings"))
        }
    )
    actionable_diagnostics = _actionable_diagnostics(
        awc_terminal_coverage_report=awc_terminal_coverage_report,
        awc_terminal_closeout_report=awc_terminal_closeout_report,
        awc_terminal_apply_contract_report=awc_terminal_apply_contract_report,
        awc_terminal_operator_runbook_report=awc_terminal_operator_runbook_report,
        posterior_rematerialization_report=posterior_rematerialization_report,
        production_identity_closeout_report=production_identity_closeout_packet_report,
        production_identity_closeout_runbook_report=production_identity_closeout_operator_runbook_report,
        hierarchical_joint_closeout_runbook_report=hierarchical_joint_closeout_runbook_report,
        production_apply_preflight_report=production_apply_preflight_packet_report,
        current_latest_posterior_runbook_report=current_latest_posterior_runbook_report,
        daily_review_report=daily_review_report,
        parameter_governance_report=parameter_governance_report,
        fact_write_boundary_report=fact_write_boundary_report,
        max_items_output=int(max_items_output),
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "hierarchical_joint_plan_coverage_audit_read_only",
        "root": str(Path(root).expanduser().resolve()),
        "db": str(db),
        "guards": _guards(),
        "scope": {
            "since_hours": float(since_hours),
            "latest_only": bool(latest_only),
            "active_only": bool(active_only),
            "project_forward": bool(project_forward),
            "limit": int(limit),
            "max_items_output": int(max_items_output),
            "min_checked_facts": max(1, int(min_checked_facts or 1)),
            "ui_mode": str(ui_mode or "summary"),
            "ui_run_date": str(ui_run_date or ""),
            "ui_limit": int(ui_limit),
            "ui_city_filter": str(ui_city_filter or ""),
            "run_ui_replay": bool(run_ui_replay),
            "replay_limit": int(replay_limit),
            "require_awc_signal_sample": bool(require_awc_signal_sample),
            "since_days": int(since_days),
            "since_date": str(since_date or ""),
            "slug": str(slug or ""),
            "event_date": str(event_date or ""),
            "market_scope": str(market_scope or "highest_temperature"),
            "daily_review_scope_limit": max(1, int(daily_review_scope_limit)),
            "include_daily_review_ui_snapshot_audit": bool(include_daily_review_ui_snapshot_audit),
            "require_daily_review_ui_visible": bool(require_daily_review_ui_visible),
            "trace_stages": bool(trace_stages),
        },
        "stage_timings": stage_timings,
        "gate_order": [str(gate.get("gate") or "") for gate in gates],
        "checks": {str(gate.get("gate") or ""): bool(gate.get("ok")) for gate in gates},
        "gates": gates,
        "blockers": blockers,
        "warnings": warnings,
        "actionable_diagnostics": actionable_diagnostics,
        "next_actions": (
            [
                "fix_failed_joint_plan_gate_before_declaring_full_system_closed",
                "rerun_scripts/hierarchical_joint_plan_coverage_audit.py",
            ]
            if blockers
            else [
                "joint_plan_coverage_ok_keep_in_release_closeout",
                "continue_with_controlled_schema_or_persistence_work_only_after_confirmation",
            ]
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only plan-level audit for hierarchical Bayes, AWC timing, and daily review SSOT coverage."
    )
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--all-history", action="store_true")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--persisted-only", action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-items-output", type=int, default=5)
    parser.add_argument("--min-checked-facts", type=int, default=1)
    parser.add_argument("--ui-mode", choices=("full", "summary"), default="summary")
    parser.add_argument("--ui-run-date", default="")
    parser.add_argument("--ui-limit", type=int, default=120)
    parser.add_argument("--ui-city-filter", default="")
    parser.add_argument("--skip-ui-replay", action="store_true")
    parser.add_argument("--replay-limit", type=int, default=50)
    parser.add_argument("--require-awc-signal-sample", action="store_true")
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--daily-review-scope-limit", type=int, default=1000000)
    parser.add_argument("--skip-daily-review-ui-snapshot-audit", action="store_true")
    parser.add_argument("--require-daily-review-ui-visible", action="store_true")
    parser.add_argument("--order-lineage-since-hours", type=float, default=0.0)
    parser.add_argument("--ignore-orders-before", default="2026-07-07")
    parser.add_argument("--include-legacy-order-lineage", action="store_true")
    parser.add_argument("--trace-stages", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def _compact_summary_blocker(value: Any, *, max_items_output: int) -> Any:
    if isinstance(value, Mapping):
        keep_keys = (
            "blocker",
            "report",
            "schema",
            "gate",
            "dependency",
            "block_reason",
            "reason",
            "field",
            "table",
            "column",
            "count",
            "candidate_count",
            "operation_count",
        )
        out: dict[str, Any] = {
            str(key): value.get(key)
            for key in keep_keys
            if key in value and not isinstance(value.get(key), (Mapping, list, tuple))
        }
        if "report_blockers" in value:
            out["report_blockers"] = [
                _compact_summary_blocker(item, max_items_output=max_items_output)
                for item in _as_list(value.get("report_blockers"))[: max(0, int(max_items_output))]
            ]
            out["report_blockers_truncated"] = len(_as_list(value.get("report_blockers"))) > max(
                0, int(max_items_output)
            )
        return out or {
            "schema": str(value.get("schema") or ""),
            "keys": sorted(str(key) for key in value.keys())[: max(0, int(max_items_output))],
            "truncated": True,
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _compact_summary_blocker(item, max_items_output=max_items_output)
            for item in list(value)[: max(0, int(max_items_output))]
        ]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_hierarchical_joint_plan_coverage_audit(
        root=Path(args.root),
        db_path=Path(args.db),
        since_hours=float(args.since_hours),
        latest_only=not bool(args.all_history),
        active_only=bool(args.active_only),
        project_forward=not bool(args.persisted_only),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        min_checked_facts=max(1, int(args.min_checked_facts or 1)),
        ui_mode=str(args.ui_mode or "summary"),
        ui_run_date=str(args.ui_run_date or ""),
        ui_limit=int(args.ui_limit),
        ui_city_filter=str(args.ui_city_filter or ""),
        run_ui_replay=not bool(args.skip_ui_replay),
        replay_limit=int(args.replay_limit),
        require_awc_signal_sample=bool(args.require_awc_signal_sample),
        since_days=int(args.since_days),
        since_date=str(args.since_date or ""),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        market_scope=str(args.market_scope or "highest_temperature"),
        daily_review_scope_limit=max(1, int(args.daily_review_scope_limit)),
        include_daily_review_ui_snapshot_audit=not bool(args.skip_daily_review_ui_snapshot_audit),
        require_daily_review_ui_visible=bool(args.require_daily_review_ui_visible),
        order_lineage_since_hours=float(args.order_lineage_since_hours),
        ignore_legacy_order_lineage=not bool(args.include_legacy_order_lineage),
        ignore_orders_before=str(args.ignore_orders_before or ""),
        trace_stages=bool(args.trace_stages),
    )
    if args.summary:
        max_items_output = max(0, int(args.max_items_output))
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "execution_mode": report["execution_mode"],
            "scope": report["scope"],
            "stage_timings": report.get("stage_timings", []),
            "gate_order": report["gate_order"],
            "checks": report["checks"],
            "blockers": [
                _compact_summary_blocker(item, max_items_output=max_items_output)
                for item in _as_list(report.get("blockers"))[:max_items_output]
            ],
            "blockers_truncated": len(_as_list(report.get("blockers"))) > max_items_output,
            "warnings": _as_list(report.get("warnings"))[:max_items_output],
            "warnings_truncated": len(_as_list(report.get("warnings"))) > max_items_output,
            "actionable_diagnostics": report["actionable_diagnostics"],
            "next_actions": report["next_actions"],
            "guards": report["guards"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
