#!/usr/bin/env python3
"""Build the read-only master packet for daily review controlled apply.

This packet composes the already-controlled surfaces for:

1. Phase0 forward schema apply.
2. Daily review lineage repair.
3. Daily review persistence apply.
4. Post-apply acceptance verification.

It never writes archive.db, never materializes the manifest, never creates a
backup, never applies DDL, never persists reviews, never changes parameters,
never restarts launchd, and never places orders.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_lineage_repair_operator_runbook as lineage_runbook  # noqa: E402
from scripts import daily_review_persistence_apply  # noqa: E402
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402
from scripts import phase0_apply_confirmation_packet as phase0_packet  # noqa: E402
from scripts import phase0_schema_apply  # noqa: E402


SCHEMA_VERSION = "daily_review_controlled_apply_confirmation_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
DEFAULT_MISSING_FACT_DECISION = "exclude_sample"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _counter(values: Sequence[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        text = str(value or "").strip() or "missing"
        out[text] = out.get(text, 0) + 1
    return dict(sorted(out.items()))


def _step_by_name(rows: Sequence[Any], step: str) -> Mapping[str, Any]:
    for row in rows:
        row_map = _as_mapping(row)
        if str(row_map.get("step") or "") == step:
            return row_map
    return {}


def _scope_args(
    *,
    db: Path,
    since_days: int,
    since_date: str,
    slug: str,
    event_date: str,
    market_scope: str,
    limit: int,
    max_items_output: int,
) -> list[str]:
    args = [
        "--db",
        _q(db),
        "--since-days",
        str(int(since_days)),
        "--max-items-output",
        str(int(max_items_output)),
    ]
    if since_date:
        args.extend(["--since-date", _q(since_date)])
    if slug:
        args.extend(["--slug", _q(slug)])
    if event_date:
        args.extend(["--event-date", _q(event_date)])
    if market_scope:
        args.extend(["--market-scope", _q(market_scope)])
    if int(limit) != 100:
        args.extend(["--limit", str(int(limit))])
    return args


def _cmd(script: str, args: Sequence[str]) -> str:
    return " ".join(["python3", script, *args])


def _guard() -> dict[str, bool]:
    return {
        "executes_sql": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_daily_weather_reviews": False,
        "writes_daily_calibration_records": False,
        "writes_bias_correction_candidates": False,
        "writes_parameter_change_events": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _backup_dir_state(path: Path | str) -> dict[str, Any]:
    raw = Path(path).expanduser()
    resolved = raw.resolve(strict=False)
    is_symlink = raw.is_symlink()
    symlink_target = ""
    if is_symlink:
        try:
            symlink_target = os.readlink(raw)
        except OSError:
            symlink_target = ""
    return {
        "raw_path": str(raw),
        "resolved_path": str(resolved),
        "exists": raw.exists(),
        "is_symlink": is_symlink,
        "symlink_target": symlink_target,
        "symlink_target_exists": resolved.exists() if is_symlink else None,
        "parent_exists": resolved.parent.exists(),
    }


def _backup_command_blocked_reason(state: Mapping[str, Any]) -> str:
    if state.get("is_symlink") and not state.get("symlink_target_exists"):
        return "backup_dir_symlink_target_missing"
    return ""


def _decision_counts(manifest: Mapping[str, Any]) -> dict[str, int]:
    return _counter([_as_mapping(row).get("decision") for row in _as_sequence(manifest.get("decisions"))])


def _persistence_summary(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": contract.get("schema"),
        "read_only": bool(contract.get("read_only")),
        "dry_run": bool(contract.get("dry_run")),
        "ready_to_apply_now": bool(contract.get("ready_to_apply_now")),
        "batch_plan_ready_after_confirmation": bool(contract.get("batch_plan_ready_after_confirmation")),
        "batch_plan_source": contract.get("batch_plan_source"),
        "settled_event_count": contract.get("settled_event_count"),
        "effective_settled_event_count": contract.get("effective_settled_event_count"),
        "excluded_settled_event_count": contract.get("excluded_settled_event_count"),
        "planned_review_count": contract.get("planned_review_count"),
        "planned_calibration_record_count": contract.get("planned_calibration_record_count"),
        "planned_candidate_count": contract.get("planned_candidate_count"),
        "planned_parameter_event_count": contract.get("planned_parameter_event_count"),
        "apply_blockers": list(contract.get("apply_blockers") or []),
        "batch_apply_blockers": list(contract.get("batch_apply_blockers") or []),
        "lineage_decision_apply_readiness": dict(contract.get("lineage_decision_apply_readiness") or {}),
        "controlled_apply_command": contract.get("controlled_apply_command"),
        "boundary": dict(contract.get("boundary") or {}),
        "guards": dict(contract.get("guards") or {}),
    }


def _control_sequence(
    *,
    db: Path,
    backup_path: str,
    manifest_path: str,
    scope_args: Sequence[str],
    phase0_summary: Mapping[str, Any],
    runbook: Mapping[str, Any],
    persistence_contract: Mapping[str, Any],
    missing_fact_decision: str,
    limit: int,
    max_items_output: int,
    backup_command_blocked_reason: str = "",
) -> list[dict[str, Any]]:
    phase0_preflight = _as_sequence(phase0_summary.get("required_preflight_steps"))
    phase0_verification = _as_sequence(phase0_summary.get("required_verification_steps"))
    phase0_execution = _as_sequence(phase0_summary.get("execution_steps"))
    runbook_steps = _as_sequence(runbook.get("checklist"))

    materialize_manifest = _step_by_name(runbook_steps, "materialize_reviewed_manifest")
    recheck_lineage = _step_by_name(runbook_steps, "recheck_lineage_scope")
    risk_audit = _step_by_name(runbook_steps, "review_risk_audit")
    dry_run_lineage = _cmd(
        "scripts/daily_review_lineage_repair_apply.py",
        [*scope_args, "--lineage-decision-manifest", _q(manifest_path)],
    )
    apply_lineage = _cmd(
        "scripts/daily_review_lineage_repair_apply.py",
        [
            *scope_args,
            "--lineage-decision-manifest",
            _q(manifest_path),
            "--apply",
            "--confirm",
            batch_plan.LINEAGE_REPAIR_CONFIRMATION_PHRASE,
            "--backup",
            _q(backup_path),
            "--writer-quiescent",
        ],
    )

    batch_plan_recheck = _cmd(
        "scripts/daily_review_persistence_batch_plan.py",
        [*scope_args, "--lineage-decision-manifest", _q(manifest_path)],
    )
    persistence_dry_run = _cmd(
        "scripts/daily_review_persistence_apply.py",
        [*scope_args, "--lineage-decision-manifest", _q(manifest_path)],
    )
    persistence_apply = _cmd(
        "scripts/daily_review_persistence_apply.py",
        [
            *scope_args,
            "--lineage-decision-manifest",
            _q(manifest_path),
            "--apply",
            "--confirm",
            daily_review_persistence_apply.CONFIRMATION_PHRASE,
            "--backup",
            _q(backup_path),
            "--writer-quiescent",
        ],
    )
    manifest_summary = _cmd(
        "scripts/daily_review_lineage_repair_operator_runbook.py",
        [
            *scope_args,
            "--backup-dir",
            _q(Path(backup_path).parent),
            "--missing-fact-decision",
            missing_fact_decision,
        ],
    )

    create_backup = _step_by_name(phase0_preflight, "create_fresh_backup")
    apply_schema = _step_by_name(phase0_execution, "apply_schema_only")
    schema_verify = _step_by_name(phase0_verification, "schema_surface_strict")
    ssot_verify = _step_by_name(phase0_verification, "ssot_alignment")
    create_backup_command = str(create_backup.get("command") or "").strip()
    if backup_command_blocked_reason:
        create_backup_command = ""
    elif not create_backup_command and backup_path:
        create_backup_command = (
            f"mkdir -p {_q(Path(backup_path).parent)} && "
            f"sqlite3 {_q(db)} \".backup {_q(backup_path)}\""
        )
    joint_plan_acceptance = (
        "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary "
        f"--limit {int(limit)} --max-items-output {int(max_items_output)}"
    )
    lineage_required = bool(
        _as_mapping(runbook.get("lineage_runbook_summary")).get("ready_for_controlled_apply_after_runbook")
    ) or bool(runbook.get("ready_for_controlled_apply_after_runbook"))

    return [
        {
            "order": 1,
            "phase": "review",
            "step": "review_master_packet",
            "command": "python3 scripts/daily_review_controlled_apply_confirmation_packet.py --summary",
            "writes": False,
            "required": True,
            "pass_condition": "operator sees schema, lineage, persistence, and post-apply gates in one packet",
        },
        {
            "order": 2,
            "phase": "review",
            "step": "review_lineage_runbook",
            "command": manifest_summary,
            "writes": False,
            "required": True,
            "pass_condition": "manifest decisions are reviewed before the manifest file is materialized",
        },
        {
            "order": 3,
            "phase": "prepare",
            "step": "materialize_reviewed_manifest",
            "command": materialize_manifest.get("command", ""),
            "writes": True,
            "writes_archive_db": False,
            "writes_manifest_file": True,
            "required": bool(materialize_manifest.get("required")),
            "blocked_by_runbook": bool(materialize_manifest.get("blocked_by_runbook")),
            "output": manifest_path,
            "reason": materialize_manifest.get("reason"),
        },
        {
            "order": 4,
            "phase": "prepare",
            "step": "assert_writer_quiescence",
            "command": (
                "python3 scripts/controlled_apply_maintenance_window_packet.py "
                f"--db {_q(db)} --summary --max-items-output 20"
            ),
            "writes": False,
            "required": True,
            "pass_condition": "writer_quiescent == true and blocker_count == 0",
        },
        {
            "order": 5,
            "phase": "prepare",
            "step": "create_fresh_backup",
            "command": create_backup_command,
            "writes": True,
            "writes_archive_db": False,
            "creates_backup": True,
            "required": True,
            "output": backup_path,
            "backup_command_blocked_reason": backup_command_blocked_reason,
        },
        {
            "order": 6,
            "phase": "apply",
            "step": "apply_phase0_schema",
            "command": apply_schema.get("command", ""),
            "writes": bool(apply_schema.get("writes")),
            "writes_archive_db": bool(apply_schema.get("writes")),
            "confirmation_phrase": phase0_schema_apply.CONFIRMATION_PHRASE,
            "required": True,
        },
        {
            "order": 7,
            "phase": "verify",
            "step": "verify_phase0_schema",
            "command": schema_verify.get("command", "python3 scripts/phase0_schema_probe.py --immutable --strict --schema-only"),
            "writes": False,
            "required": True,
        },
        {
            "order": 8,
            "phase": "preflight",
            "step": "dry_run_lineage_repair_and_risk",
            "commands": [
                dry_run_lineage,
                risk_audit.get("command", ""),
            ],
            "writes": False,
            "required": lineage_required,
            "pass_condition": risk_audit.get("pass_condition", ""),
        },
        {
            "order": 9,
            "phase": "apply",
            "step": "apply_lineage_repair",
            "command": apply_lineage,
            "writes": lineage_required,
            "writes_archive_db": lineage_required,
            "confirmation_phrase": batch_plan.LINEAGE_REPAIR_CONFIRMATION_PHRASE,
            "required": lineage_required,
            "blocked_by_runbook": not bool(runbook.get("ready_for_controlled_apply_after_runbook")),
            "blockers": list(_as_mapping(runbook.get("risk_summary")).get("blockers") or []),
        },
        {
            "order": 10,
            "phase": "verify",
            "step": "recheck_lineage_scope",
            "commands": [recheck_lineage.get("command", ""), batch_plan_recheck],
            "writes": False,
            "required": True,
        },
        {
            "order": 11,
            "phase": "preflight",
            "step": "dry_run_daily_review_persistence",
            "command": persistence_dry_run,
            "writes": False,
            "required": True,
            "current_contract_ready_to_apply_now": bool(persistence_contract.get("ready_to_apply_now")),
            "current_contract_blockers": list(persistence_contract.get("apply_blockers") or []),
        },
        {
            "order": 12,
            "phase": "apply",
            "step": "apply_daily_review_persistence",
            "command": persistence_apply,
            "writes": True,
            "writes_archive_db": True,
            "confirmation_phrase": daily_review_persistence_apply.CONFIRMATION_PHRASE,
            "required": True,
            "blocked_until": "phase0_schema_apply_and_lineage_repair_have_passed_post_apply_rechecks",
        },
        {
            "order": 13,
            "phase": "verify",
            "step": "post_apply_acceptance",
            "commands": [
                "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20",
                "python3 scripts/daily_review_post_apply_acceptance_audit.py --since-days 45 --max-items-output 20",
                ssot_verify.get("command", "python3 scripts/check_ssot_alignment.py --immutable"),
                joint_plan_acceptance,
            ],
            "writes": False,
            "required": True,
        },
    ]


def build_confirmation_packet(
    db_path: Path | str = batch_plan.DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 100,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    missing_fact_decision: str = DEFAULT_MISSING_FACT_DECISION,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_state = _backup_dir_state(backup_dir)
    backup_root = Path(str(backup_state["resolved_path"]))
    backup_blocked_reason = _backup_command_blocked_reason(backup_state)
    scope_args = _scope_args(
        db=db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max_items_output,
    )

    effective_missing_fact_decision = (
        str(missing_fact_decision or "").strip()
        if str(missing_fact_decision or "").strip() in lineage_runbook.confirmation_packet.MISSING_FACT_DECISIONS
        else DEFAULT_MISSING_FACT_DECISION
    )
    runbook = lineage_runbook.build_operator_runbook(
        db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max_items_output,
        backup_dir=backup_root,
        generated_at=generated,
        missing_fact_decision=effective_missing_fact_decision,
    )
    proposed_manifest = dict(_as_mapping(runbook.get("proposed_manifest_json")))
    prebuilt_persistence_plan = _as_mapping(
        runbook.get("proposed_recheck_batch_plan_for_persistence_contract")
    )
    phase0_full_packet = phase0_packet.build_confirmation_packet(
        db,
        backup_dir=backup_root,
        generated_at=generated,
        lineage_decision_manifest=proposed_manifest,
        prebuilt_review_batch_plan=prebuilt_persistence_plan or None,
    )
    phase0_summary = phase0_packet.compact_confirmation_packet(phase0_full_packet)
    backup_path = str(phase0_full_packet.get("backup_path") or runbook.get("backup_path") or "")
    manifest_path = str(runbook.get("manifest_output_path") or "")
    persistence_contract = daily_review_persistence_apply.build_daily_review_persistence_apply_contract(
        db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max_items_output,
        lineage_decision_manifest=proposed_manifest,
        backup_path=Path(backup_path) if backup_path else None,
        allow_apply=False,
        confirmation_phrase="",
        writer_quiescent=False,
        prebuilt_batch_plan=prebuilt_persistence_plan or None,
    )
    decision_counts = _decision_counts(proposed_manifest)
    control_sequence = _control_sequence(
        db=db,
        backup_path=backup_path,
        manifest_path=manifest_path,
        scope_args=scope_args,
        phase0_summary=phase0_summary,
        runbook=runbook,
        persistence_contract=persistence_contract,
        missing_fact_decision=effective_missing_fact_decision,
        limit=limit,
        max_items_output=max_items_output,
        backup_command_blocked_reason=backup_blocked_reason,
    )
    risk_summary = dict(_as_mapping(runbook.get("risk_summary")))
    apply_readiness = dict(_as_mapping(runbook.get("apply_readiness_summary")))
    operator_gates = dict(_as_mapping(phase0_summary.get("operator_apply_gate_summary")))
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(
            phase0_full_packet.get("ok")
            and runbook.get("ok")
            and persistence_contract.get("ok", True)
            and not backup_blocked_reason
        ),
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "daily_review_master_controlled_apply_confirmation_no_apply",
        "db": str(db),
        "scope": {
            "since_days": int(since_days),
            "since_date": since_date,
            "slug": slug,
            "event_date": event_date,
            "market_scope": market_scope,
            "limit": int(limit),
            "max_items_output": int(max_items_output),
        },
        "backup_dir": str(backup_root),
        "backup_dir_state": backup_state,
        "backup_path": backup_path,
        "backup_exists": Path(backup_path).exists() if backup_path else False,
        "backup_command_blocked_reason": backup_blocked_reason,
        "manifest_output_path": manifest_path,
        "manifest_output_exists": Path(manifest_path).exists() if manifest_path else False,
        "missing_fact_decision_policy": effective_missing_fact_decision,
        "proposed_manifest_decision_counts": decision_counts,
        "proposed_manifest_json": proposed_manifest,
        "phase0_summary": phase0_summary,
        "lineage_runbook_summary": {
            "schema": runbook.get("schema"),
            "ok": bool(runbook.get("ok")),
            "read_only": bool(runbook.get("read_only")),
            "dry_run": bool(runbook.get("dry_run")),
            "candidate_count": runbook.get("candidate_count"),
            "missing_fact_decision_policy": runbook.get("missing_fact_decision_policy"),
            "risk_summary": risk_summary,
            "contract_summary": dict(_as_mapping(runbook.get("contract_summary"))),
            "apply_readiness_summary": apply_readiness,
            "ready_for_controlled_apply_after_runbook": bool(
                runbook.get("ready_for_controlled_apply_after_runbook")
            ),
            "guards": dict(_as_mapping(runbook.get("guards"))),
        },
        "daily_review_persistence_apply_contract_summary": _persistence_summary(persistence_contract),
        "operator_apply_gate_summary": operator_gates,
        "control_sequence": control_sequence,
        "confirmation_phrases": {
            "phase0_schema_apply": phase0_schema_apply.CONFIRMATION_PHRASE,
            "lineage_repair_apply": str(
                _as_mapping(_step_by_name(_as_sequence(runbook.get("checklist")), "apply_lineage_repair")).get(
                    "confirmation_phrase"
                )
                or batch_plan.LINEAGE_REPAIR_CONFIRMATION_PHRASE
            ),
            "daily_review_persistence_apply": daily_review_persistence_apply.CONFIRMATION_PHRASE,
            "daily_review_parameter_events": daily_review_persistence_apply.PARAMETER_EVENT_CONFIRMATION_PHRASE,
        },
        "boundary": {
            "phase0_schema_apply_included_as_controlled_step": True,
            "lineage_repair_included_as_controlled_step": True,
            "daily_review_persistence_included_as_controlled_step": True,
            "post_apply_acceptance_included": True,
            "posterior_ssot_repair_included": False,
            "historical_backfill_included": False,
            "parameter_events_skipped_unless_separately_confirmed": True,
            "launchd_restart_included": False,
            "orders_or_trade_decisions_included": False,
        },
        "guards": _guard(),
        "blocked_operations": [
            "automatic_schema_apply",
            "automatic_manifest_file_write",
            "automatic_backup_creation",
            "automatic_lineage_repair_apply",
            "automatic_daily_review_persistence_apply",
            "daily_review_parameter_event_apply_without_separate_confirmation",
            "historical_backfill",
            "launchd_restart",
            "order_or_trade_decision_write",
            "parameter_change",
        ],
        "next_boundary": "operator_review_then_controlled_phase0_schema_apply",
    }


def compact_confirmation_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    phase0_summary = _as_mapping(packet.get("phase0_summary"))
    lineage_summary = _as_mapping(packet.get("lineage_runbook_summary"))
    persistence_summary = _as_mapping(packet.get("daily_review_persistence_apply_contract_summary"))
    control_sequence = [
        {
            "order": item.get("order"),
            "phase": item.get("phase"),
            "step": item.get("step"),
            "writes": bool(item.get("writes")),
            "writes_archive_db": bool(item.get("writes_archive_db")),
            "required": bool(item.get("required")),
            "confirmation_phrase": item.get("confirmation_phrase"),
            "blocked_by_runbook": bool(item.get("blocked_by_runbook")),
            "backup_command_blocked_reason": item.get("backup_command_blocked_reason"),
            "command": item.get("command") or item.get("commands"),
        }
        for item in _as_sequence(packet.get("control_sequence"))
    ]
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "daily_review_master_controlled_apply_summary_no_apply",
        "db": packet.get("db"),
        "backup_path": packet.get("backup_path"),
        "backup_exists": bool(packet.get("backup_exists")),
        "backup_dir_state": dict(_as_mapping(packet.get("backup_dir_state"))),
        "backup_command_blocked_reason": packet.get("backup_command_blocked_reason"),
        "manifest_output_path": packet.get("manifest_output_path"),
        "manifest_output_exists": bool(packet.get("manifest_output_exists")),
        "missing_fact_decision_policy": packet.get("missing_fact_decision_policy"),
        "proposed_manifest_decision_counts": dict(_as_mapping(packet.get("proposed_manifest_decision_counts"))),
        "phase0": {
            "needs_apply": bool(phase0_summary.get("needs_apply")),
            "ddl_summary": dict(_as_mapping(phase0_summary.get("ddl_summary"))),
            "operator_apply_gate_summary": dict(_as_mapping(phase0_summary.get("operator_apply_gate_summary"))),
        },
        "lineage": {
            "ok": bool(lineage_summary.get("ok")),
            "candidate_count": lineage_summary.get("candidate_count"),
            "risk_summary": dict(_as_mapping(lineage_summary.get("risk_summary"))),
            "apply_readiness_summary": dict(_as_mapping(lineage_summary.get("apply_readiness_summary"))),
            "ready_for_controlled_apply_after_runbook": bool(
                lineage_summary.get("ready_for_controlled_apply_after_runbook")
            ),
        },
        "persistence": persistence_summary,
        "confirmation_phrases": dict(_as_mapping(packet.get("confirmation_phrases"))),
        "control_sequence": control_sequence,
        "boundary": dict(_as_mapping(packet.get("boundary"))),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "next_boundary": packet.get("next_boundary"),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("PM_ARCHIVE_DB_PATH") or batch_plan.DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=100)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--missing-fact-decision",
        choices=lineage_runbook.confirmation_packet.MISSING_FACT_DECISIONS,
        default=DEFAULT_MISSING_FACT_DECISION,
        help="Decision to prefill for settled events with no replayable fact snapshot row.",
    )
    parser.add_argument("--summary", action="store_true", help="Print compact operator summary instead of full packet.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    packet = build_confirmation_packet(
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
    )
    payload = compact_confirmation_packet(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
