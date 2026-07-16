#!/usr/bin/env python3
"""Build a read-only operator runbook for daily review lineage repair.

The runbook is a compact execution checklist derived from the final
confirmation packet. It never creates the manifest, creates the backup, writes
archive.db, applies repair, persists daily reviews, restarts services, or places
orders.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_lineage_repair_confirmation_packet as confirmation_packet  # noqa: E402
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402


SCHEMA_VERSION = "daily_review_lineage_repair_operator_runbook_v1"


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _step_by_name(packet: Mapping[str, Any], section: str, step: str) -> Mapping[str, Any]:
    operator_packet = _as_mapping(packet.get("operator_packet"))
    for row in _as_sequence(operator_packet.get(section)):
        row_map = _as_mapping(row)
        if str(row_map.get("step") or "") == step:
            return row_map
    return {}


def _candidate_rows(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    risk_items_by_id = {
        str(item.get("repair_correlation_id") or ""): _as_mapping(item)
        for item in _as_sequence(_as_mapping(packet.get("lineage_repair_risk_audit")).get("items"))
    }
    rows: list[dict[str, Any]] = []
    for row in _as_sequence(_as_mapping(packet.get("proposed_confirm_repair_manifest")).get("decisions")):
        decision = _as_mapping(row)
        repair_id = str(decision.get("repair_correlation_id") or "")
        risk_item = risk_items_by_id.get(repair_id, {})
        rows.append(
            {
                "repair_correlation_id": repair_id,
                "decision": decision.get("decision"),
                "slug": decision.get("slug"),
                "event_date": decision.get("event_date"),
                "event_run_id": decision.get("event_run_id"),
                "target_table": decision.get("target_table"),
                "target_row_id": decision.get("target_row_id"),
                "candidate_fact_snapshot_id": decision.get("candidate_fact_snapshot_id"),
                "risk_level": "blocked" if risk_item.get("blockers") else "low",
                "would_change": bool(risk_item.get("would_change")),
                "expected_diff": list(_as_sequence(risk_item.get("expected_diff"))),
                "post_apply_prediction": dict(_as_mapping(risk_item.get("post_apply_prediction"))),
                "blockers": list(_as_sequence(risk_item.get("blockers"))),
            }
        )
    return rows


def _guard() -> dict[str, bool]:
    return {
        "executes_sql": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_daily_weather_reviews": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _counter(values: Sequence[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        text = str(value or "").strip() or "missing"
        out[text] = out.get(text, 0) + 1
    return dict(sorted(out.items()))


def build_operator_runbook_from_packet(
    packet: Mapping[str, Any],
    *,
    manifest_command: str | None = None,
) -> dict[str, Any]:
    manifest_path = str(packet.get("manifest_output_path") or "")
    backup_path = str(packet.get("backup_path") or "")
    materialize_manifest_command = manifest_command or (
        f"python3 scripts/daily_review_lineage_repair_operator_runbook.py --emit-proposed-manifest-json > {_q(manifest_path)}"
        if manifest_path
        else ""
    )
    risk_audit = _as_mapping(packet.get("lineage_repair_risk_audit"))
    contract = _as_mapping(packet.get("lineage_repair_apply_contract"))
    proposed_recheck_summary = _as_mapping(packet.get("proposed_recheck_summary"))
    apply_readiness = _as_mapping(proposed_recheck_summary.get("lineage_decision_apply_readiness"))
    review_batch_step = _step_by_name(packet, "preflight", "review_current_batch_plan")
    backup_step = _step_by_name(packet, "preflight", "create_fresh_backup")
    dry_run_step = _step_by_name(packet, "preflight", "dry_run_lineage_repair_apply_entrypoint")
    risk_step = _step_by_name(packet, "preflight", "review_lineage_repair_risk_audit")
    apply_step = _step_by_name(packet, "preflight", "apply_lineage_repair")
    recheck_step = _step_by_name(packet, "verification", "recheck_lineage_scope")
    coverage_step = _step_by_name(packet, "verification", "daily_review_coverage_audit")
    ssot_step = _step_by_name(packet, "verification", "ssot_alignment")
    rollback_step = _step_by_name(packet, "rollback", "restore_backup_if_verification_fails")
    candidate_rows = _candidate_rows(packet)
    risk_blockers = list(_as_sequence(risk_audit.get("blockers")))
    blocked_candidates = [row for row in candidate_rows if row.get("blockers")]
    ready_after_runbook = bool(candidate_rows) and bool(risk_audit.get("ok")) and not risk_blockers
    checklist = [
        {
            "step": "review_current_batch_plan",
            "phase": "review",
            "command": review_batch_step.get("command", ""),
            "required": True,
            "writes": False,
        },
        {
            "step": "review_candidate_table",
            "phase": "review",
            "command": "inspect operator_runbook.candidate_review_table",
            "required": bool(candidate_rows),
            "writes": False,
        },
        {
            "step": "materialize_reviewed_manifest",
            "phase": "prepare",
            "command": materialize_manifest_command,
            "required": bool(candidate_rows) and ready_after_runbook,
            "writes": True,
            "writes_archive_db": False,
            "writes_manifest_file": True,
            "requires_operator_review": True,
            "blocked_by_runbook": bool(candidate_rows) and not ready_after_runbook,
            "blockers": risk_blockers,
            "output": manifest_path,
            "reason": "the runbook only prints manifest JSON; operator decides whether to materialize the reviewed manifest",
        },
        {
            "step": "create_fresh_backup",
            "phase": "prepare",
            "command": backup_step.get("command", ""),
            "required": bool(candidate_rows),
            "writes": True,
            "writes_archive_db": False,
            "creates_backup": True,
            "output": backup_path,
        },
        {
            "step": "dry_run_apply_entrypoint",
            "phase": "preflight",
            "command": dry_run_step.get("command", ""),
            "required": bool(candidate_rows),
            "writes": False,
        },
        {
            "step": "review_risk_audit",
            "phase": "preflight",
            "command": risk_step.get("command", ""),
            "required": bool(candidate_rows),
            "writes": False,
            "pass_condition": risk_step.get("pass_condition", ""),
        },
        {
            "step": "apply_lineage_repair",
            "phase": "apply",
            "command": apply_step.get("command", ""),
            "required": ready_after_runbook,
            "writes": bool(candidate_rows),
            "writes_archive_db": bool(candidate_rows),
            "requires_user_confirmation": True,
            "confirmation_phrase": apply_step.get("confirmation_phrase"),
            "blocked_until_operator_runs_this_step": ready_after_runbook,
            "blocked_by_runbook": bool(candidate_rows) and not ready_after_runbook,
            "blockers": risk_blockers,
        },
        {
            "step": "recheck_lineage_scope",
            "phase": "verify",
            "command": recheck_step.get("command", ""),
            "required": bool(candidate_rows),
            "writes": False,
        },
        {
            "step": "coverage_audit",
            "phase": "verify",
            "command": coverage_step.get("command", ""),
            "required": False,
            "writes": False,
            "reason": coverage_step.get("reason", ""),
        },
        {
            "step": "ssot_alignment",
            "phase": "verify",
            "command": ssot_step.get("command", ""),
            "required": True,
            "writes": False,
        },
        {
            "step": "rollback_if_verification_fails",
            "phase": "rollback",
            "commands": list(_as_sequence(rollback_step.get("commands"))),
            "required": False,
            "requires_manual_confirmation": True,
        },
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(packet.get("ok") and risk_audit.get("ok")),
        "generated_at": packet.get("generated_at"),
        "read_only": True,
        "dry_run": True,
        "execution_mode": "lineage_repair_operator_runbook_no_apply",
        "db": packet.get("db"),
        "candidate_count": packet.get("candidate_count"),
        "missing_fact_decision_policy": packet.get("missing_fact_decision_policy"),
        "manifest_output_path": manifest_path,
        "manifest_output_exists": Path(manifest_path).exists() if manifest_path else False,
        "backup_path": backup_path,
        "backup_exists": bool(packet.get("backup_exists")),
        "risk_summary": {
            "risk_level": risk_audit.get("risk_level"),
            "blockers": risk_blockers,
            "blocker_counts": _counter(risk_blockers),
            "blocked_candidate_count": len(blocked_candidates),
            "repair_item_count": risk_audit.get("repair_item_count"),
            "would_change_count": risk_audit.get("would_change_count"),
            "lineage_blocker_expected_removed_count": risk_audit.get("lineage_blocker_expected_removed_count"),
            "lineage_blocker_expected_remaining_count": _as_mapping(
                risk_audit.get("post_apply_prediction_summary")
            ).get("lineage_blocker_expected_remaining_count"),
        },
        "blocked_candidate_table": blocked_candidates,
        "contract_summary": {
            "repair_item_count": contract.get("repair_item_count"),
            "apply_blockers": list(_as_sequence(contract.get("apply_blockers"))),
            "confirmation_phrase_required": contract.get("confirmation_phrase_required"),
            "targeted_legacy_fact_lineage_repair_only": bool(
                _as_mapping(contract.get("boundary")).get("targeted_legacy_fact_lineage_repair_only")
            ),
        },
        "apply_readiness_summary": {
            "schema": apply_readiness.get("schema"),
            "execution_mode": apply_readiness.get("execution_mode"),
            "operator_decision_counts": dict(_as_mapping(apply_readiness.get("operator_decision_counts"))),
            "daily_review_persistence": dict(
                _as_mapping(apply_readiness.get("daily_review_persistence"))
            ),
            "lineage_repair": dict(_as_mapping(apply_readiness.get("lineage_repair"))),
            "gates": list(_as_sequence(apply_readiness.get("gates"))),
            "next_action": apply_readiness.get("next_action"),
            "next_boundary": apply_readiness.get("next_boundary"),
            "read_only": bool(apply_readiness.get("read_only")) if apply_readiness else True,
        },
        "candidate_review_table": candidate_rows,
        "proposed_manifest_json": packet.get("proposed_confirm_repair_manifest"),
        "proposed_recheck_batch_plan_for_persistence_contract": packet.get(
            "proposed_recheck_batch_plan_for_persistence_contract"
        ),
        "checklist": checklist,
        "ready_for_controlled_apply_after_runbook": ready_after_runbook,
        "blocked_operations": list(_as_sequence(packet.get("blocked_operations"))),
        "guards": _guard(),
        "next_boundary": (
            "operator_review_manifest_then_materialize_backup_and_run_controlled_apply"
            if ready_after_runbook
            else "resolve_runbook_blockers_before_manifest_materialization_or_apply"
            if candidate_rows
            else "no_lineage_repair_candidates"
        ),
    }


def build_operator_runbook(
    db_path: Path | str = batch_plan.DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 100,
    backup_dir: Path | str = confirmation_packet.DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    missing_fact_decision: str = "manual_rebuild_fact_snapshot",
) -> dict[str, Any]:
    base_cmd_parts = [
        "python3 scripts/daily_review_lineage_repair_operator_runbook.py",
        "--db",
        _q(Path(db_path).expanduser().resolve()),
        "--since-days",
        str(int(since_days)),
        "--max-items-output",
        str(int(max_items_output)),
        "--backup-dir",
        _q(Path(backup_dir).expanduser().resolve()),
    ]
    if since_date:
        base_cmd_parts.extend(["--since-date", _q(since_date)])
    if slug:
        base_cmd_parts.extend(["--slug", _q(slug)])
    if event_date:
        base_cmd_parts.extend(["--event-date", _q(event_date)])
    if market_scope:
        base_cmd_parts.extend(["--market-scope", _q(market_scope)])
    if int(limit) != 100:
        base_cmd_parts.extend(["--limit", str(int(limit))])
    if str(missing_fact_decision or "").strip() != "manual_rebuild_fact_snapshot":
        base_cmd_parts.extend(["--missing-fact-decision", _q(str(missing_fact_decision or ""))])
    packet = confirmation_packet.build_confirmation_packet(
        db_path,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max_items_output,
        backup_dir=backup_dir,
        generated_at=generated_at,
        missing_fact_decision=missing_fact_decision,
    )
    manifest_path = str(packet.get("manifest_output_path") or "")
    manifest_command = " ".join(base_cmd_parts + ["--emit-proposed-manifest-json"])
    if manifest_path:
        manifest_command = f"{manifest_command} > {_q(manifest_path)}"
    return build_operator_runbook_from_packet(packet, manifest_command=manifest_command)


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
    parser.add_argument("--backup-dir", type=Path, default=confirmation_packet.DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--missing-fact-decision",
        choices=confirmation_packet.MISSING_FACT_DECISIONS,
        default="manual_rebuild_fact_snapshot",
        help="Decision to prefill for candidates with no replayable fact snapshot row.",
    )
    parser.add_argument(
        "--emit-proposed-manifest-json",
        action="store_true",
        help="Print only the proposed confirm-repair manifest JSON; does not write the file.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    runbook = build_operator_runbook(
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
    if args.emit_proposed_manifest_json:
        print(json.dumps(runbook.get("proposed_manifest_json") or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    print(json.dumps(runbook, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if runbook.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
