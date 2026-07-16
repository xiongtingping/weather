#!/usr/bin/env python3
"""Read-only confirmation packet for post-apply resolution work.

This packet stitches the remaining daily-review post-apply blockers into one
operator-facing sequence:

1. Exact posterior SSOT rematerialization candidates.
2. Missing fact-snapshot exclusion state.
3. Legacy AWC signal-anchor decisions.
4. The existing Phase0/lineage/daily-review controlled apply packet.

It does not write archive.db, create backups, repair facts, materialize
manifests, rebuild signal anchors, restart launchd, or place orders.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_controlled_apply_confirmation_packet as master_packet  # noqa: E402
from scripts import legacy_awc_signal_anchor_decision_packet as legacy_awc_anchor_packet  # noqa: E402
from scripts import daily_review_post_apply_resolution_plan as resolution_plan  # noqa: E402
from scripts import posterior_ssot_apply_confirmation_packet as posterior_apply_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "daily_review_post_apply_resolution_confirmation_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


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


def _fact_id_list(values: Sequence[Any]) -> list[int]:
    out: list[int] = []
    for raw in values:
        try:
            fact_id = int(raw or 0)
        except Exception:
            continue
        if fact_id > 0 and fact_id not in out:
            out.append(fact_id)
    return out


def _posterior_apply_command(
    *,
    db: Path,
    backup_dir: Path,
    backup_path: Path | None = None,
    fact_id: int,
    slug: str = "",
    event_date: str = "",
) -> str:
    args = [
        "--db",
        _q(db),
        "--fact-id",
        str(int(fact_id)),
    ]
    if slug:
        args.extend(["--slug", _q(slug)])
    if event_date:
        args.extend(["--event-date", _q(event_date)])
    if backup_path:
        args.extend(["--backup", _q(backup_path)])
    else:
        args.extend(["--backup-dir", _q(backup_dir)])
    args.extend(
        [
            "--apply",
            "--confirm",
            posterior_apply_packet.CONFIRMATION_PHRASE,
            "--writer-quiescent",
            "--json",
        ]
    )
    return _cmd("scripts/apply_posterior_ssot_fact_repair.py", args)


def _posterior_repair_control(
    *,
    db: Path,
    backup_dir: Path,
    backup_path: Path | None = None,
    posterior_resolution: Mapping[str, Any],
    max_items_output: int,
) -> dict[str, Any]:
    probe = _as_mapping(posterior_resolution.get("rematerialization_probe"))
    fact_ids = _fact_id_list(probe.get("fact_ids") or posterior_resolution.get("candidate_fact_ids") or [])
    item_by_id: dict[int, Mapping[str, Any]] = {}
    for item in _as_sequence(probe.get("items")):
        item_map = _as_mapping(item)
        try:
            fact_id = int(item_map.get("fact_id") or 0)
        except Exception:
            fact_id = 0
        if fact_id > 0:
            item_by_id[fact_id] = item_map
    commands: list[dict[str, Any]] = []
    for fact_id in fact_ids:
        item = item_by_id.get(fact_id, {})
        commands.append(
            {
                "fact_id": fact_id,
                "slug": str(item.get("slug") or ""),
                "event_date": str(item.get("event_date") or ""),
                "command": _posterior_apply_command(
                    db=db,
                    backup_dir=backup_dir,
                    backup_path=backup_path,
                    fact_id=fact_id,
                    slug=str(item.get("slug") or ""),
                    event_date=str(item.get("event_date") or ""),
                ),
                "confirmation_phrase": posterior_apply_packet.CONFIRMATION_PHRASE,
                "writes_archive_db": True,
            }
        )
    visible_limit = max(0, int(max_items_output))
    return {
        "candidate_count": int(posterior_resolution.get("candidate_count") or 0),
        "fact_ids": fact_ids,
        "probe": {
            "available": bool(probe.get("available")),
            "checked_fact_count": int(probe.get("checked_fact_count") or 0),
            "candidate_count": int(probe.get("candidate_count") or 0),
            "projected_ok_count": int(probe.get("projected_ok_count") or 0),
            "projected_issue_count": int(probe.get("projected_issue_count") or 0),
            "can_forward_writer_fix_all_candidates": bool(probe.get("can_forward_writer_fix_all_candidates")),
            "action_counts": dict(_as_mapping(probe.get("action_counts"))),
            "issue_family_counts": dict(_as_mapping(probe.get("issue_family_counts"))),
        },
        "apply_mode": "per_fact_controlled_apply_existing_posterior_ssot_fact_repair_entrypoint",
        "apply_command_count": len(commands),
        "apply_commands": commands[:visible_limit] if visible_limit else [],
        "apply_commands_truncated": len(commands) > visible_limit,
        "all_commands_materialized_in_packet": visible_limit >= len(commands),
        "requires_explicit_confirmation": bool(commands),
        "requires_fresh_backup": bool(commands),
        "backup_path": str(backup_path) if backup_path else "",
        "requires_writer_quiescence": bool(commands),
        "ready_for_operator_controlled_apply": bool(
            commands
            and probe.get("available")
            and probe.get("can_forward_writer_fix_all_candidates")
            and int(probe.get("projected_issue_count") or 0) == 0
        ),
    }


def _legacy_awc_anchor_control(
    replay_resolution: Mapping[str, Any],
    *,
    max_items_output: int,
    decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    items = [dict(_as_mapping(item)) for item in _as_sequence(replay_resolution.get("items"))]
    visible_limit = max(0, int(max_items_output))
    decision_packet = legacy_awc_anchor_packet.build_decision_packet_from_replay_resolution(
        replay_resolution,
        decision_manifest=decision_manifest,
        max_items_output=visible_limit,
    )
    decision_audit = _as_mapping(decision_packet.get("decision_manifest_audit"))
    decisions = []
    for item in items:
        decisions.append(
            {
                "fact_id": int(item.get("fact_id") or 0),
                "slug": item.get("slug"),
                "event_date": item.get("event_date"),
                "source": _as_mapping(item.get("override")).get("source"),
                "constraint_mode": _as_mapping(item.get("override")).get("constraint_mode"),
                "no_apply_probe_command": item.get("no_apply_probe_command"),
                "allowed_operator_decisions": list(legacy_awc_anchor_packet.ALLOWED_DECISIONS),
                "apply_command": "",
                "writes_archive_db": False,
                "reason": "legacy_awc_lower_bound_signal_anchor_requires_human_decision_no_auto_rebuild",
            }
        )
    return {
        "candidate_count": int(replay_resolution.get("candidate_count") or len(items)),
        "source_counts": dict(_as_mapping(replay_resolution.get("source_counts"))),
        "constraint_mode_counts": dict(_as_mapping(replay_resolution.get("constraint_mode_counts"))),
        "decision_required": bool(decision_audit.get("decision_required")),
        "operator_route_complete": bool(decision_audit.get("operator_route_complete")),
        "acceptance_exception_complete": bool(decision_audit.get("acceptance_exception_complete")),
        "decision_manifest_schema": legacy_awc_anchor_packet.MANIFEST_SCHEMA_VERSION,
        "decision_manifest_template": decision_packet.get("decision_manifest_template"),
        "decision_manifest_audit": decision_audit,
        "decision_packet_summary": legacy_awc_anchor_packet.compact_decision_packet(decision_packet),
        "ready_for_automatic_apply": False,
        "manual_decision_items": decisions[:visible_limit] if visible_limit else [],
        "manual_decision_items_truncated": len(decisions) > visible_limit,
        "boundary": "no_signal_ledger_backfill_without_explicit_operator_evidence_and_dedicated_writer",
    }


def _control_sequence(
    *,
    db: Path,
    backup_dir: Path,
    posterior_repair_backup: Path | None = None,
    scope_args: Sequence[str],
    resolution: Mapping[str, Any],
    posterior_control: Mapping[str, Any],
    legacy_control: Mapping[str, Any],
    master_summary: Mapping[str, Any],
    missing_fact_decision: str,
    max_items_output: int,
) -> list[dict[str, Any]]:
    fact_ids = _fact_id_list(posterior_control.get("fact_ids") or [])
    fact_ids_csv = ",".join(str(value) for value in fact_ids)
    remat_args = ["--db", _q(db), "--max-items-output", str(int(max_items_output))]
    if fact_ids_csv:
        remat_args.extend(["--fact-ids", _q(fact_ids_csv)])
    else:
        remat_args.extend(["--since-hours", "0", "--limit", "1"])
    legacy_decision_commands = [
        _cmd("scripts/legacy_awc_signal_anchor_decision_packet.py", [*scope_args, "--summary"]),
        _cmd(
            "scripts/legacy_awc_signal_anchor_decision_packet.py",
            [*scope_args, "--emit-decision-manifest-template"],
        ),
    ]
    legacy_decision_commands.extend(
        str(item.get("no_apply_probe_command") or "")
        for item in _as_sequence(legacy_control.get("manual_decision_items"))
        if str(item.get("no_apply_probe_command") or "")
    )
    return [
        {
            "order": 1,
            "phase": "review",
            "step": "review_post_apply_resolution_packet",
            "command": _cmd("scripts/daily_review_post_apply_resolution_confirmation_packet.py", [*scope_args, "--summary"]),
            "writes": False,
            "required": True,
            "pass_condition": "operator sees posterior, missing fact, legacy AWC anchor, and daily review gates together",
        },
        {
            "order": 2,
            "phase": "review",
            "step": "review_exact_posterior_rematerialization_probe",
            "command": _cmd("scripts/posterior_ssot_rematerialization_plan.py", remat_args),
            "writes": False,
            "required": bool(fact_ids),
            "pass_condition": "projected_issue_count remains 0 and candidate fact_ids match packet",
        },
        {
            "order": 3,
            "phase": "apply",
            "step": "apply_posterior_ssot_repairs_per_fact",
            "commands": [dict(_as_mapping(item)).get("command") for item in _as_sequence(posterior_control.get("apply_commands"))],
            "command_count": int(posterior_control.get("apply_command_count") or 0),
            "commands_truncated": bool(posterior_control.get("apply_commands_truncated")),
            "writes": bool(posterior_control.get("apply_command_count")),
            "writes_archive_db": bool(posterior_control.get("apply_command_count")),
            "confirmation_phrase": posterior_apply_packet.CONFIRMATION_PHRASE,
            "required": bool(posterior_control.get("apply_command_count")),
            "blocked_until": "explicit_confirmation_fresh_backup_writer_quiescence_and_projection_recheck",
            "backup_dir": str(backup_dir),
            "backup_path": str(posterior_repair_backup) if posterior_repair_backup else "",
        },
        {
            "order": 4,
            "phase": "verify",
            "step": "verify_posterior_ssot_repairs",
            "commands": [
                _cmd("scripts/posterior_ssot_fact_coverage_audit.py", ["--db", _q(db), "--limit", "120", "--max-items-output", str(int(max_items_output))]),
                _cmd("scripts/daily_review_post_apply_resolution_plan.py", scope_args),
            ],
            "writes": False,
            "required": bool(fact_ids),
            "pass_condition": "posterior_ssot_fact_candidates drops to 0 before final daily review acceptance",
        },
        {
            "order": 5,
            "phase": "manual_decision",
            "step": "resolve_legacy_awc_signal_anchors",
            "commands": legacy_decision_commands,
            "candidate_count": int(legacy_control.get("candidate_count") or 0),
            "writes": False,
            "required": bool(legacy_control.get("decision_required")),
            "blocked_until": "operator_selects_rebuild_signal_anchor_or_reviewed_legacy_no_signal_for_each_candidate",
            "allowed_operator_decisions": list(legacy_awc_anchor_packet.ALLOWED_DECISIONS),
            "decision_manifest_schema": legacy_awc_anchor_packet.MANIFEST_SCHEMA_VERSION,
        },
        {
            "order": 6,
            "phase": "review",
            "step": "confirm_missing_fact_snapshot_exclusion_scope",
            "command": _cmd(
                "scripts/daily_review_controlled_apply_confirmation_packet.py",
                [*scope_args, "--missing-fact-decision", missing_fact_decision, "--summary"],
            ),
            "writes": False,
            "required": bool(_as_mapping(resolution.get("unresolved_counts")).get("missing_fact_snapshot_excluded")),
            "pass_condition": "excluded sample count is intentional and effective review scope matches planned rows",
        },
        {
            "order": 7,
            "phase": "delegate_controlled_apply",
            "step": "continue_with_daily_review_controlled_apply_sequence",
            "command": _cmd(
                "scripts/daily_review_controlled_apply_confirmation_packet.py",
                [*scope_args, "--missing-fact-decision", missing_fact_decision, "--summary"],
            ),
            "writes": False,
            "required": True,
            "nested_control_sequence_schema": master_summary.get("schema"),
            "nested_control_sequence_count": len(_as_sequence(master_summary.get("control_sequence"))),
            "blocked_until": "posterior_repairs_verified_and_legacy_awc_anchor_decisions_recorded_or_accepted",
        },
    ]


def build_confirmation_packet(
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
    posterior_repair_backup: Path | str | None = None,
    generated_at: str | None = None,
    missing_fact_decision: str = master_packet.DEFAULT_MISSING_FACT_DECISION,
    legacy_awc_anchor_decision_manifest: Mapping[str, Any] | None = None,
    master_confirmation_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    posterior_backup_path = Path(posterior_repair_backup).expanduser().resolve() if posterior_repair_backup else None
    operation_max_items = max(1, int(limit), int(max_items_output))
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
    master = dict(
        master_confirmation_packet
        or master_packet.build_confirmation_packet(
            db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=operation_max_items,
            backup_dir=backup_root,
            generated_at=generated,
            missing_fact_decision=missing_fact_decision,
        )
    )
    resolution = resolution_plan.build_resolution_plan(
        db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=operation_max_items,
        missing_fact_decision=missing_fact_decision,
        master_confirmation_packet=master,
    )
    master_summary = master_packet.compact_confirmation_packet(master)
    posterior_control = _posterior_repair_control(
        db=db,
        backup_dir=backup_root,
        backup_path=posterior_backup_path,
        posterior_resolution=_as_mapping(_as_mapping(resolution).get("posterior_ssot_resolution")),
        max_items_output=operation_max_items,
    )
    legacy_control = _legacy_awc_anchor_control(
        _as_mapping(_as_mapping(resolution).get("replay_anchor_resolution")),
        max_items_output=operation_max_items,
        decision_manifest=legacy_awc_anchor_decision_manifest,
    )
    control_sequence = _control_sequence(
        db=db,
        backup_dir=backup_root,
        posterior_repair_backup=posterior_backup_path,
        scope_args=scope_args,
        resolution=resolution,
        posterior_control=posterior_control,
        legacy_control=legacy_control,
        master_summary=master_summary,
        missing_fact_decision=missing_fact_decision,
        max_items_output=max_items_output,
    )
    unresolved = dict(_as_mapping(resolution.get("unresolved_counts")))
    lineage_resolution = _as_mapping(resolution.get("lineage_resolution"))
    hard_blockers: list[str] = []
    if posterior_control.get("candidate_count") and not posterior_control.get("ready_for_operator_controlled_apply"):
        hard_blockers.append("posterior_ssot_repair_projection_not_ready")
    if legacy_control.get("decision_required"):
        hard_blockers.append("legacy_awc_signal_anchor_operator_decision_required")
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(
            resolution.get("operator_resolution_surface_ready")
            and master.get("ok")
            and not hard_blockers
        ),
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "daily_review_post_apply_resolution_confirmation_no_apply",
        "db": str(db),
        "scope": {
            "since_days": int(since_days),
            "since_date": since_date,
            "slug": slug,
            "event_date": event_date,
            "market_scope": market_scope,
            "limit": int(limit),
            "max_items_output": int(max_items_output),
            "operation_max_items_output": int(operation_max_items),
            "missing_fact_decision": missing_fact_decision,
        },
        "backup_dir": str(backup_root),
        "posterior_repair_backup": str(posterior_backup_path) if posterior_backup_path else "",
        "resolution_summary": {
            "schema": resolution.get("schema"),
            "resolution_complete": bool(resolution.get("resolution_complete")),
            "operator_resolution_surface_ready": bool(
                resolution.get("operator_resolution_surface_ready")
            ),
            "acceptance_summary": dict(_as_mapping(resolution.get("acceptance_summary"))),
            "daily_review_missing_resolution": dict(
                _as_mapping(resolution.get("daily_review_missing_resolution"))
            ),
            "batch_plan_summary": dict(_as_mapping(resolution.get("batch_plan_summary"))),
            "unresolved_counts": unresolved,
            "source_counts": dict(_as_mapping(resolution.get("source_counts"))),
        },
        "missing_fact_snapshot_control": {
            "excluded_sample_count": int(lineage_resolution.get("excluded_sample_count") or 0),
            "confirm_repair_count": int(lineage_resolution.get("confirm_repair_count") or 0),
            "proposed_manifest_decision_counts": dict(
                _as_mapping(lineage_resolution.get("proposed_manifest_decision_counts"))
            ),
            "recommended_action": lineage_resolution.get("recommended_action"),
            "excluded_samples": [
                {
                    "slug": item.get("slug"),
                    "event_date": item.get("event_date"),
                    "event_run_id": item.get("event_run_id"),
                    "decision": item.get("decision"),
                    "repair_type": item.get("repair_type"),
                    "reason": item.get("reason"),
                    "state": item.get("state"),
                    "guards": dict(_as_mapping(item.get("guards"))),
                }
                for item in _as_sequence(lineage_resolution.get("excluded_samples"))
            ],
        },
        "posterior_ssot_repair_control": posterior_control,
        "legacy_awc_signal_anchor_control": legacy_control,
        "daily_review_master_control_summary": master_summary,
        "lineage_decision_manifest": dict(_as_mapping(master.get("proposed_manifest_json"))),
        "control_sequence": control_sequence,
        "confirmation_phrases": {
            "posterior_ssot_fact_repair": posterior_apply_packet.CONFIRMATION_PHRASE,
            **dict(_as_mapping(master_summary.get("confirmation_phrases"))),
        },
        "boundary": {
            "posterior_ssot_repair_included_as_controlled_step": True,
            "legacy_awc_signal_anchor_auto_rebuild_included": False,
            "legacy_awc_signal_anchor_decision_manifest_included": True,
            "missing_fact_snapshot_exclusion_included": True,
            "daily_review_master_controlled_apply_included": True,
            "historical_backfill_included": False,
            "external_fetch_included": False,
            "orders_or_trade_decisions_included": False,
            "parameter_change_included": False,
        },
        "hard_blockers": hard_blockers,
        "blocked_operations": [
            "automatic_posterior_ssot_repair_apply",
            "automatic_legacy_awc_signal_anchor_rebuild",
            "automatic_schema_apply",
            "automatic_lineage_repair_apply",
            "automatic_daily_review_persistence_apply",
            "historical_backfill",
            "external_weather_fetch",
            "launchd_restart",
            "order_or_trade_decision_write",
            "parameter_change",
        ],
        "guards": _guards(),
        "next_boundary": (
            "resolve_legacy_awc_signal_anchor_decisions_then_run_controlled_sequence"
            if hard_blockers
            else "operator_review_then_run_controlled_post_apply_resolution_sequence"
        ),
    }


def compact_confirmation_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    posterior = _as_mapping(packet.get("posterior_ssot_repair_control"))
    legacy = _as_mapping(packet.get("legacy_awc_signal_anchor_control"))
    master = _as_mapping(packet.get("daily_review_master_control_summary"))
    missing_fact = _as_mapping(packet.get("missing_fact_snapshot_control"))

    def compact_command(value: Any, *, sample_limit: int = 3) -> Any:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            rows = list(value)
            return rows[: max(0, int(sample_limit))]
        return value

    control_sequence = [
        {
            "order": item.get("order"),
            "phase": item.get("phase"),
            "step": item.get("step"),
            "writes": bool(item.get("writes")),
            "writes_archive_db": bool(item.get("writes_archive_db")),
            "required": bool(item.get("required")),
            "confirmation_phrase": item.get("confirmation_phrase"),
            "command": compact_command(item.get("command") or item.get("commands")),
            "command_count": item.get("command_count"),
            "commands_truncated": (
                len(item.get("command") or item.get("commands") or []) > 3
                if isinstance(item.get("command") or item.get("commands"), Sequence)
                and not isinstance(item.get("command") or item.get("commands"), (str, bytes, bytearray))
                else False
            ),
            "blocked_until": item.get("blocked_until"),
        }
        for item in _as_sequence(packet.get("control_sequence"))
    ]
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "daily_review_post_apply_resolution_confirmation_summary_no_apply",
        "db": packet.get("db"),
        "scope": dict(_as_mapping(packet.get("scope"))),
        "backup_dir": packet.get("backup_dir"),
        "resolution_summary": dict(_as_mapping(packet.get("resolution_summary"))),
        "missing_fact_snapshot": {
            "excluded_sample_count": int(missing_fact.get("excluded_sample_count") or 0),
            "confirm_repair_count": int(missing_fact.get("confirm_repair_count") or 0),
            "proposed_manifest_decision_counts": dict(
                _as_mapping(missing_fact.get("proposed_manifest_decision_counts"))
            ),
            "recommended_action": missing_fact.get("recommended_action"),
            "excluded_samples": [
                {
                    "slug": item.get("slug"),
                    "event_date": item.get("event_date"),
                    "event_run_id": item.get("event_run_id"),
                    "decision": item.get("decision"),
                    "repair_type": item.get("repair_type"),
                    "reason": item.get("reason"),
                    "state": item.get("state"),
                }
                for item in _as_sequence(missing_fact.get("excluded_samples"))
            ],
        },
        "posterior_ssot_repair": {
            "candidate_count": posterior.get("candidate_count"),
            "apply_command_count": posterior.get("apply_command_count"),
            "ready_for_operator_controlled_apply": bool(posterior.get("ready_for_operator_controlled_apply")),
            "probe": dict(_as_mapping(posterior.get("probe"))),
            "apply_mode": posterior.get("apply_mode"),
        },
        "legacy_awc_signal_anchor": {
            "candidate_count": legacy.get("candidate_count"),
            "decision_required": bool(legacy.get("decision_required")),
            "operator_route_complete": bool(legacy.get("operator_route_complete")),
            "acceptance_exception_complete": bool(legacy.get("acceptance_exception_complete")),
            "decision_manifest_schema": legacy.get("decision_manifest_schema"),
            "decision_manifest_audit": {
                "manifest_provided": bool(_as_mapping(legacy.get("decision_manifest_audit")).get("manifest_provided")),
                "decision_counts": dict(_as_mapping(_as_mapping(legacy.get("decision_manifest_audit")).get("decision_counts"))),
                "missing_decision_count": int(_as_mapping(legacy.get("decision_manifest_audit")).get("missing_decision_count") or 0),
                "invalid_decision_count": int(_as_mapping(legacy.get("decision_manifest_audit")).get("invalid_decision_count") or 0),
                "review_evidence_missing_count": int(_as_mapping(legacy.get("decision_manifest_audit")).get("review_evidence_missing_count") or 0),
                "dedicated_writer_required_count": int(_as_mapping(legacy.get("decision_manifest_audit")).get("dedicated_writer_required_count") or 0),
                "reviewed_exception_count": int(_as_mapping(legacy.get("decision_manifest_audit")).get("reviewed_exception_count") or 0),
                "hard_blockers": list(_as_mapping(legacy.get("decision_manifest_audit")).get("hard_blockers") or []),
            },
            "ready_for_automatic_apply": bool(legacy.get("ready_for_automatic_apply")),
            "source_counts": dict(_as_mapping(legacy.get("source_counts"))),
            "boundary": legacy.get("boundary"),
        },
        "daily_review_master": {
            "ok": bool(master.get("ok")),
            "proposed_manifest_decision_counts": dict(_as_mapping(master.get("proposed_manifest_decision_counts"))),
            "boundary": dict(_as_mapping(master.get("boundary"))),
        },
        "control_sequence": control_sequence,
        "confirmation_phrases": dict(_as_mapping(packet.get("confirmation_phrases"))),
        "boundary": dict(_as_mapping(packet.get("boundary"))),
        "hard_blockers": list(packet.get("hard_blockers") or []),
        "guards": dict(_as_mapping(packet.get("guards"))),
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
    parser.add_argument("--posterior-repair-backup", type=Path, default=None)
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
    legacy_awc_anchor_manifest = legacy_awc_anchor_packet.load_decision_manifest_file(
        args.legacy_awc_anchor_decision_manifest
    )
    packet = build_confirmation_packet(
        db_path=args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
        posterior_repair_backup=args.posterior_repair_backup,
        missing_fact_decision=args.missing_fact_decision,
        legacy_awc_anchor_decision_manifest=legacy_awc_anchor_manifest,
    )
    payload = compact_confirmation_packet(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("ok") or packet.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
