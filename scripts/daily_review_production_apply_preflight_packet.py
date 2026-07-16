#!/usr/bin/env python3
"""Read-only production preflight for the daily-review controlled apply chain.

The packet combines post-apply resolution, posterior SSOT repair readiness,
legacy AWC anchor decision state, and the daily-review master controlled-apply
packet into one machine-readable start gate. It never writes archive.db,
creates backups, materializes manifests, restarts launchd, fetches weather
sources, or places orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_controlled_apply_confirmation_packet as master_packet  # noqa: E402
from scripts import daily_review_post_apply_resolution_confirmation_packet as resolution_packet  # noqa: E402
from scripts import legacy_awc_signal_anchor_decision_packet as legacy_awc_packet  # noqa: E402
from scripts import controlled_apply_maintenance_window_packet as maintenance_packet  # noqa: E402
from scripts import posterior_ssot_rematerialization_plan as rematerialization_plan  # noqa: E402
from scripts import posterior_ssot_apply_confirmation_packet as posterior_apply_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "daily_review_production_apply_preflight_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_daily_weather_reviews": False,
        "writes_daily_calibration_records": False,
        "writes_bias_correction_candidates": False,
        "writes_parameter_change_events": False,
        "writes_signal_to_order_timing": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _stage(
    *,
    key: str,
    order: int,
    title: str,
    ready: bool,
    required: bool = True,
    blockers: Sequence[str] = (),
    next_action: str = "",
    writes_archive_db_when_executed: bool = False,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cleaned_blockers = [str(value) for value in blockers if str(value or "").strip()]
    return {
        "order": int(order),
        "key": key,
        "title": title,
        "required": bool(required),
        "ready": bool(ready),
        "blocked": bool(required and (not ready or cleaned_blockers)),
        "blockers": cleaned_blockers,
        "next_action": str(next_action or ""),
        "writes_archive_db_when_executed": bool(writes_archive_db_when_executed),
        "detail": dict(detail or {}),
    }


def _gate_by_name(master_summary: Mapping[str, Any], gate_name: str) -> Mapping[str, Any]:
    phase0 = _as_mapping(master_summary.get("phase0"))
    operator_summary = _as_mapping(phase0.get("operator_apply_gate_summary"))
    for row in _as_sequence(operator_summary.get("gates")):
        row_map = _as_mapping(row)
        if str(row_map.get("gate") or "") == gate_name:
            return row_map
    return {}


def _resolution_control_step(full_packet: Mapping[str, Any], step_name: str) -> Mapping[str, Any]:
    for row in _as_sequence(full_packet.get("control_sequence")):
        row_map = _as_mapping(row)
        if str(row_map.get("step") or "") == step_name:
            return row_map
    return {}


def _current_latest_posterior_report(
    db_path: Path,
    *,
    since_hours: float = 168.0,
    limit: int = 100,
    max_items_output: int = 100,
) -> dict[str, Any]:
    report = rematerialization_plan.build_plan(
        db_path,
        since_hours=float(since_hours),
        limit=int(limit),
        latest_only=True,
        max_items_output=max(int(limit), int(max_items_output)),
    )
    remat = _as_mapping(report.get("historical_rematerialization_plan"))
    candidates = [
        dict(_as_mapping(item))
        for item in _as_sequence(report.get("candidates"))
        if bool(_as_mapping(item).get("is_latest_fact"))
    ]
    fact_ids = []
    for item in candidates:
        try:
            fact_id = int(item.get("fact_id") or 0)
        except Exception:
            fact_id = 0
        if fact_id > 0 and fact_id not in fact_ids:
            fact_ids.append(fact_id)
    fact_ids_csv = ",".join(str(value) for value in fact_ids)
    return {
        "schema": "current_latest_posterior_materialization_preflight_v1",
        "read_only": True,
        "source_schema": report.get("schema"),
        "candidate_count": int(remat.get("current_latest_issue_count") or 0),
        "projected_ok_count": int(remat.get("current_latest_projected_ok_count") or 0),
        "projected_issue_count": int(remat.get("current_latest_projected_issue_count") or 0),
        "candidate_fact_ids": fact_ids,
        "candidate_count_matches_fact_ids": int(remat.get("current_latest_issue_count") or 0) == len(fact_ids),
        "issue_family_counts": dict(_as_mapping(remat.get("issue_family_counts"))),
        "current_issue_counts": dict(_as_mapping(remat.get("current_issue_counts"))),
        "apply_confirmation_command": (
            "python3 scripts/posterior_ssot_apply_confirmation_packet.py "
            f"--fact-ids {fact_ids_csv} --max-items-output {max(10, len(fact_ids))} --summary"
            if fact_ids
            else ""
        ),
        "single_fact_preflight_command_template": (
            "python3 scripts/posterior_ssot_apply_preflight_risk_packet.py --fact-id <fact_id> --max-items-output 5"
            if fact_ids
            else ""
        ),
        "confirmation_phrase": posterior_apply_packet.CONFIRMATION_PHRASE,
        "guards": dict(_as_mapping(report.get("guards"))),
    }


def _production_preflight_stages(
    full_packet: Mapping[str, Any],
    *,
    maintenance_window: Mapping[str, Any],
    current_latest_posterior: Mapping[str, Any],
) -> list[dict[str, Any]]:
    posterior = _as_mapping(full_packet.get("posterior_ssot_repair_control"))
    legacy = _as_mapping(full_packet.get("legacy_awc_signal_anchor_control"))
    legacy_audit = _as_mapping(legacy.get("decision_manifest_audit"))
    master_summary = _as_mapping(full_packet.get("daily_review_master_control_summary"))

    legacy_candidate_count = int(legacy.get("candidate_count") or 0)
    legacy_ready = bool(
        legacy_candidate_count == 0
        or legacy.get("acceptance_exception_complete")
        or (legacy.get("operator_route_complete") and not legacy.get("decision_required"))
    )
    legacy_blockers = list(legacy_audit.get("hard_blockers") or [])
    if legacy_candidate_count and legacy.get("decision_required") and not legacy_blockers:
        legacy_blockers.append("legacy_awc_signal_anchor_operator_decision_required")

    posterior_candidate_count = int(posterior.get("candidate_count") or 0)
    posterior_ready = bool(
        posterior_candidate_count == 0 or posterior.get("ready_for_operator_controlled_apply")
    )
    posterior_blockers: list[str] = []
    if posterior_candidate_count and not posterior_ready:
        posterior_blockers.append("posterior_ssot_repair_projection_not_ready")
    current_latest_count = int(current_latest_posterior.get("candidate_count") or 0)
    current_latest_projected_issues = int(current_latest_posterior.get("projected_issue_count") or 0)
    current_latest_fact_ids = list(current_latest_posterior.get("candidate_fact_ids") or [])
    current_latest_ready = bool(
        current_latest_count == 0
        or (
            current_latest_projected_issues == 0
            and bool(current_latest_posterior.get("candidate_count_matches_fact_ids"))
        )
    )
    current_latest_blockers: list[str] = []
    if current_latest_count and current_latest_projected_issues:
        current_latest_blockers.append("current_latest_posterior_materialization_not_forward_fixable")
    if current_latest_count and not current_latest_posterior.get("candidate_count_matches_fact_ids"):
        current_latest_blockers.append("current_latest_posterior_candidate_fact_ids_incomplete")
    maintenance_blockers: list[str] = []
    if maintenance_window.get("writer_quiescent") is not True:
        maintenance_blockers.append("writer_quiescence_not_satisfied")
    maintenance_stop = _as_mapping(maintenance_window.get("stop_authorization"))
    maintenance_blockers.extend(
        str(item)
        for item in maintenance_stop.get("blockers") or []
        if str(item or "").strip()
    )

    phase0_gate = _gate_by_name(master_summary, "phase0_schema_apply")
    lineage_gate = _gate_by_name(master_summary, "lineage_repair_apply")
    persistence_gate = _gate_by_name(master_summary, "daily_review_persistence_apply")
    phase0_ready_after_preflight = bool(phase0_gate.get("ready_after_operator_preflight"))
    lineage_ready_after_preflight = bool(lineage_gate.get("ready_after_operator_preflight"))
    persistence_ready_now = bool(persistence_gate.get("ready_now"))
    persistence_ready_after_preflight = bool(persistence_gate.get("ready_after_operator_preflight"))
    phase0_blockers = list(phase0_gate.get("unexpected_blockers") or [])
    if not phase0_ready_after_preflight and not phase0_blockers:
        phase0_blockers.append("phase0_schema_apply_waits_for_operator_preflight")
    lineage_blockers = list(lineage_gate.get("blockers_now") or [])
    if not lineage_ready_after_preflight and not lineage_blockers:
        lineage_blockers.append("lineage_repair_apply_waits_for_operator_preflight")
    persistence_blockers = list(persistence_gate.get("blockers_now") or [])
    if not persistence_ready_now and not persistence_ready_after_preflight and not persistence_blockers:
        persistence_blockers.append("daily_review_persistence_waits_for_prior_apply_stages")

    return [
        _stage(
            key="maintenance_window_writer_quiescence",
            order=0,
            title="Pause archive.db writers and verify writer quiescence",
            ready=not maintenance_blockers,
            blockers=maintenance_blockers,
            next_action=(
                "run_interactive_sudo_launchctl_stop_system_monitor_then_rerun_maintenance_packet"
                if maintenance_blockers
                else "writer_quiescent_verified"
            ),
            detail={
                "writer_quiescent": bool(maintenance_window.get("writer_quiescent")),
                "required_stop_labels": list(maintenance_window.get("required_stop_labels") or []),
                "stop_authorization": dict(maintenance_stop),
                "counts": dict(_as_mapping(maintenance_window.get("counts"))),
            },
        ),
        _stage(
            key="legacy_awc_signal_anchor_decision_manifest",
            order=1,
            title="Resolve legacy AWC signal-anchor decisions",
            ready=legacy_ready,
            blockers=legacy_blockers,
            next_action=(
                "provide_reviewed_no_signal_manifest_or_dedicated_signal_anchor_writer"
                if not legacy_ready
                else "legacy_awc_anchor_decision_route_resolved"
            ),
            detail={
                "candidate_count": legacy_candidate_count,
                "decision_manifest_schema": legacy.get("decision_manifest_schema"),
                "decision_counts": dict(_as_mapping(legacy_audit.get("decision_counts"))),
                "acceptance_exception_complete": bool(legacy.get("acceptance_exception_complete")),
                "operator_route_complete": bool(legacy.get("operator_route_complete")),
            },
        ),
        _stage(
            key="current_latest_posterior_materialization",
            order=2,
            title="Materialize current latest posterior SSOT facts",
            ready=current_latest_ready,
            blockers=current_latest_blockers,
            next_action=(
                "run_exact_fact_id_confirmation_then_single_fact_preflight_and_apply"
                if current_latest_count
                else "no_current_latest_posterior_materialization_required"
            ),
            writes_archive_db_when_executed=bool(current_latest_count),
            detail={
                "candidate_count": current_latest_count,
                "candidate_fact_ids": current_latest_fact_ids,
                "projected_ok_count": int(current_latest_posterior.get("projected_ok_count") or 0),
                "projected_issue_count": current_latest_projected_issues,
                "apply_confirmation_command": current_latest_posterior.get("apply_confirmation_command"),
                "single_fact_preflight_command_template": current_latest_posterior.get(
                    "single_fact_preflight_command_template"
                ),
                "confirmation_phrase": current_latest_posterior.get("confirmation_phrase"),
            },
        ),
        _stage(
            key="daily_review_posterior_ssot_fact_repair",
            order=3,
            title="Apply daily-review scoped posterior SSOT fact repairs",
            ready=posterior_ready,
            blockers=posterior_blockers,
            next_action=(
                "run_per_fact_controlled_posterior_ssot_repairs"
                if posterior_candidate_count
                else "no_posterior_ssot_repairs_required"
            ),
            writes_archive_db_when_executed=bool(posterior_candidate_count),
            detail={
                "candidate_count": posterior_candidate_count,
                "apply_command_count": int(posterior.get("apply_command_count") or 0),
                "projection": dict(_as_mapping(posterior.get("probe"))),
                "control_step": dict(
                    _resolution_control_step(full_packet, "apply_posterior_ssot_repairs_per_fact")
                ),
            },
        ),
        _stage(
            key="phase0_schema_apply",
            order=4,
            title="Apply Phase0 planned schema",
            ready=phase0_ready_after_preflight,
            blockers=phase0_blockers,
            next_action=str(
                phase0_gate.get("next_operator_action")
                or "run_phase0_schema_apply_with_confirmation_backup_and_writer_quiescence"
            ),
            writes_archive_db_when_executed=True,
            detail=dict(phase0_gate),
        ),
        _stage(
            key="lineage_repair_apply",
            order=5,
            title="Apply daily review lineage repairs",
            ready=lineage_ready_after_preflight,
            blockers=lineage_blockers,
            next_action="run_lineage_repair_apply_then_rerun_batch_plan",
            writes_archive_db_when_executed=True,
            detail=dict(lineage_gate),
        ),
        _stage(
            key="daily_review_persistence_apply",
            order=6,
            title="Apply daily review persistence",
            ready=bool(persistence_ready_now or persistence_ready_after_preflight),
            blockers=persistence_blockers,
            next_action=(
                "rerun_batch_plan_after_schema_and_lineage_apply_then_apply_persistence"
                if persistence_blockers
                else "run_daily_review_persistence_apply"
            ),
            writes_archive_db_when_executed=True,
            detail=dict(persistence_gate),
        ),
        _stage(
            key="post_apply_acceptance",
            order=7,
            title="Run post-apply acceptance and joint gate",
            ready=False,
            blockers=["requires_prior_apply_stages_to_complete"],
            next_action="run_daily_review_post_apply_acceptance_and_joint_gate_after_apply",
            writes_archive_db_when_executed=False,
            detail={
                "control_step": dict(_resolution_control_step(full_packet, "continue_with_daily_review_controlled_apply_sequence"))
            },
        ),
    ]


def build_preflight_packet(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    missing_fact_decision: str = master_packet.DEFAULT_MISSING_FACT_DECISION,
    legacy_awc_anchor_decision_manifest: Mapping[str, Any] | None = None,
    maintenance_window_packet: Mapping[str, Any] | None = None,
    current_latest_posterior_packet: Mapping[str, Any] | None = None,
    resolution_confirmation_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    packet = dict(
        resolution_confirmation_packet
        or resolution_packet.build_confirmation_packet(
            db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=max_items_output,
            backup_dir=backup_dir,
            missing_fact_decision=missing_fact_decision,
            legacy_awc_anchor_decision_manifest=legacy_awc_anchor_decision_manifest,
        )
    )
    maintenance_window = dict(
        maintenance_window_packet
        or maintenance_packet.build_maintenance_window_packet(
            db,
            max_items_output=max_items_output,
        )
    )
    current_latest_posterior = dict(
        current_latest_posterior_packet
        or _current_latest_posterior_report(
            db,
            limit=limit,
            max_items_output=max_items_output,
        )
    )
    stages = _production_preflight_stages(
        packet,
        maintenance_window=maintenance_window,
        current_latest_posterior=current_latest_posterior,
    )
    pre_sequence_blockers = [
        blocker
        for stage in stages[:4]
        for blocker in stage.get("blockers", [])
        if str(blocker or "").strip()
    ]
    pre_sequence_stage_ready = all(bool(stage.get("ready")) for stage in stages[:4])
    unsafe_guards = {
        key: value
        for key, value in _guards().items()
        if bool(value)
    }
    ready_to_start = bool(
        not pre_sequence_blockers
        and pre_sequence_stage_ready
        and not unsafe_guards
        and maintenance_window.get("writer_quiescent") is True
        and _as_mapping(packet.get("daily_review_master_control_summary")).get("ok")
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(ready_to_start),
        "read_only": True,
        "dry_run": True,
        "execution_mode": "daily_review_production_apply_preflight_no_apply",
        "db": str(db),
        "scope": {
            "since_days": int(since_days),
            "since_date": since_date,
            "slug": slug,
            "event_date": event_date,
            "market_scope": market_scope,
            "limit": int(limit),
            "max_items_output": int(max_items_output),
            "missing_fact_decision": missing_fact_decision,
        },
        "ready_to_start_controlled_sequence": ready_to_start,
        "start_blockers": sorted(set(pre_sequence_blockers)),
        "sequence_dependency_blockers": sorted(
            set(
                blocker
                for stage in stages[4:]
                for blocker in stage.get("blockers", [])
                if str(blocker or "").strip()
            )
        ),
        "stages": stages,
        "maintenance_window": maintenance_window,
        "current_latest_posterior_materialization": current_latest_posterior,
        "resolution_packet_summary": resolution_packet.compact_confirmation_packet(packet),
        "control_sequence": list(packet.get("control_sequence") or []),
        "guards": _guards(),
        "boundary": {
            "no_production_write": True,
            "no_backup_creation": True,
            "no_manifest_materialization": True,
            "no_external_fetch": True,
            "no_order_or_trade_decision": True,
            "legacy_awc_manifest_is_pre_sequence_gate": True,
            "posterior_repairs_precede_daily_review_master_apply": True,
            "daily_review_persistence_runs_after_schema_and_lineage_repair": True,
            "writer_quiescent_is_pre_sequence_gate": True,
        },
        "next_boundary": (
            "resolve_start_blockers_before_controlled_apply"
            if not ready_to_start
            else "operator_can_start_controlled_sequence_with_explicit_confirmations_backups_and_writer_quiescence"
        ),
    }


def compact_preflight_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    stages = []
    for stage in _as_sequence(packet.get("stages")):
        stage_map = _as_mapping(stage)
        stages.append(
            {
                "order": stage_map.get("order"),
                "key": stage_map.get("key"),
                "ready": bool(stage_map.get("ready")),
                "blocked": bool(stage_map.get("blocked")),
                "blockers": list(stage_map.get("blockers") or []),
                "next_action": stage_map.get("next_action"),
                "writes_archive_db_when_executed": bool(stage_map.get("writes_archive_db_when_executed")),
            }
        )
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "daily_review_production_apply_preflight_summary_no_apply",
        "db": packet.get("db"),
        "scope": dict(_as_mapping(packet.get("scope"))),
        "ready_to_start_controlled_sequence": bool(packet.get("ready_to_start_controlled_sequence")),
        "start_blockers": list(packet.get("start_blockers") or []),
        "sequence_dependency_blockers": list(packet.get("sequence_dependency_blockers") or []),
        "stages": stages,
        "maintenance_window": {
            "ok": bool(_as_mapping(packet.get("maintenance_window")).get("ok")),
            "writer_quiescent": bool(_as_mapping(packet.get("maintenance_window")).get("writer_quiescent")),
            "required_stop_labels": list(_as_mapping(packet.get("maintenance_window")).get("required_stop_labels") or []),
            "operator_commands": dict(
                _as_mapping(_as_mapping(packet.get("maintenance_window")).get("operator_commands"))
            ),
            "stop_authorization": dict(
                _as_mapping(_as_mapping(packet.get("maintenance_window")).get("stop_authorization"))
            ),
        },
        "current_latest_posterior_materialization": {
            "candidate_count": int(
                _as_mapping(packet.get("current_latest_posterior_materialization")).get("candidate_count") or 0
            ),
            "projected_ok_count": int(
                _as_mapping(packet.get("current_latest_posterior_materialization")).get("projected_ok_count") or 0
            ),
            "projected_issue_count": int(
                _as_mapping(packet.get("current_latest_posterior_materialization")).get("projected_issue_count") or 0
            ),
            "candidate_fact_ids": list(
                _as_mapping(packet.get("current_latest_posterior_materialization")).get("candidate_fact_ids") or []
            ),
            "apply_confirmation_command": _as_mapping(
                packet.get("current_latest_posterior_materialization")
            ).get("apply_confirmation_command"),
        },
        "guards": dict(_as_mapping(packet.get("guards"))),
        "boundary": dict(_as_mapping(packet.get("boundary"))),
        "next_boundary": packet.get("next_boundary"),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--missing-fact-decision",
        choices=master_packet.lineage_runbook.confirmation_packet.MISSING_FACT_DECISIONS,
        default=master_packet.DEFAULT_MISSING_FACT_DECISION,
    )
    parser.add_argument("--legacy-awc-anchor-decision-manifest", type=Path, default=None)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    legacy_manifest = legacy_awc_packet.load_decision_manifest_file(
        args.legacy_awc_anchor_decision_manifest
    )
    packet = build_preflight_packet(
        args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
        missing_fact_decision=args.missing_fact_decision,
        legacy_awc_anchor_decision_manifest=legacy_manifest,
    )
    payload = compact_preflight_packet(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
