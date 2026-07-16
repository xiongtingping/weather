#!/usr/bin/env python3
"""Build a no-apply final confirmation packet for daily review lineage repair.

This packet is the last human-review artifact before running the controlled
lineage repair apply entrypoint. It does not write a manifest file, create a
backup, update archive.db, persist daily reviews, restart services, or place
orders.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daily_review_lineage_repair_writer import CONFIRMATION_PHRASE, preview_lineage_repair_contract  # noqa: E402
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402


SCHEMA_VERSION = "daily_review_lineage_repair_confirmation_packet_v1"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
MISSING_FACT_DECISIONS = (
    "manual_rebuild_fact_snapshot",
    "exclude_sample",
    "wait_for_new_fact",
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_stamp(value: str) -> str:
    return str(value or "").replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z")


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


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _backup_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.lineage-repair-{_safe_stamp(generated_at)}.bak"


def _manifest_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.lineage-repair-{_safe_stamp(generated_at)}.manifest.json"


def _proposed_confirm_decisions(
    candidates: Sequence[Mapping[str, Any]],
    *,
    missing_fact_decision: str = "manual_rebuild_fact_snapshot",
) -> dict[str, dict[str, str]]:
    missing_decision = (
        str(missing_fact_decision or "").strip()
        if str(missing_fact_decision or "").strip() in MISSING_FACT_DECISIONS
        else "manual_rebuild_fact_snapshot"
    )
    out: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        repair_id = str(candidate.get("repair_correlation_id") or "").strip()
        if not repair_id:
            continue
        if str(candidate.get("repair_type") or "") == "legacy_fact_snapshot_lineage_repair":
            out[repair_id] = {
                "decision": "confirm_repair",
                "reason": "proposed_for_operator_review_confirm_legacy_fact_snapshot_id_merge_fill",
            }
        elif missing_decision == "exclude_sample":
            out[repair_id] = {
                "decision": "exclude_sample",
                "reason": "proposed_for_operator_review_exclude_missing_fact_snapshot_from_current_apply_scope",
            }
        elif missing_decision == "wait_for_new_fact":
            out[repair_id] = {
                "decision": "wait_for_new_fact",
                "reason": "proposed_for_operator_review_wait_until_replayable_fact_snapshot_exists",
            }
        else:
            out[repair_id] = {
                "decision": "manual_rebuild_fact_snapshot",
                "reason": "confirm_repair_not_available_until_concrete_fact_snapshot_row_exists",
            }
    return out


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
    missing_fact_decision: str = "manual_rebuild_fact_snapshot",
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup = _backup_path(db, backup_root, generated)
    manifest_file = _manifest_path(db, backup_root, generated)
    review_plan = batch_plan.build_batch_plan(
        db,
        since_days=int(since_days),
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=int(limit),
        max_items_output=max(int(max_items_output), int(limit)),
        include_ui_snapshot_audit=False,
        include_replay_audit=False,
    )
    candidates = list(review_plan.get("lineage_repair_candidates") or [])
    undecided_manifest = batch_plan.build_lineage_repair_decision_manifest_template(review_plan)
    proposed_manifest = batch_plan.build_lineage_repair_decision_manifest(
        candidates,
        decisions=_proposed_confirm_decisions(
            candidates,
            missing_fact_decision=missing_fact_decision,
        ),
        decided_by="operator_required",
        decided_at=generated,
    )
    proposed_recheck_plan = batch_plan.build_batch_plan(
        db,
        since_days=int(since_days),
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=int(limit),
        max_items_output=max(int(max_items_output), int(limit)),
        include_ui_snapshot_audit=False,
        include_replay_audit=False,
        lineage_decision_manifest=proposed_manifest,
    )
    repair_contract = batch_plan.build_lineage_repair_apply_contract(
        proposed_recheck_plan,
        lineage_decision_manifest=proposed_manifest,
        backup_path=backup,
        allow_apply=False,
        confirmation_phrase="",
        writer_quiescent=False,
    )
    with _connect_readonly(db) as conn:
        risk_audit = preview_lineage_repair_contract(conn, repair_contract)
    backup_cmd = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup)}\""
    dry_run_apply_cmd = (
        f"python3 scripts/daily_review_lineage_repair_apply.py --db {_q(db)} "
        f"--since-days {int(since_days)} --max-items-output {int(max_items_output)} "
        f"--lineage-decision-manifest {_q(manifest_file)}"
    )
    risk_audit_cmd = (
        f"python3 scripts/daily_review_lineage_repair_risk_audit.py --db {_q(db)} "
        f"--since-days {int(since_days)} --max-items-output {int(max_items_output)} "
        f"--lineage-decision-manifest {_q(manifest_file)}"
    )
    operator_runbook_cmd = (
        f"python3 scripts/daily_review_lineage_repair_operator_runbook.py --db {_q(db)} "
        f"--since-days {int(since_days)} --max-items-output {int(max_items_output)}"
    )
    apply_cmd = (
        f"{dry_run_apply_cmd} --apply --confirm {CONFIRMATION_PHRASE} "
        f"--backup {_q(backup)} --writer-quiescent"
    )
    recheck_cmd = (
        f"python3 scripts/daily_review_persistence_batch_plan.py --db {_q(db)} "
        f"--since-days {int(since_days)} --max-items-output {int(max_items_output)} "
        f"--lineage-decision-manifest {_q(manifest_file)}"
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "generated_at": generated,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "lineage_repair_final_confirmation_packet_no_apply",
        "db": str(db),
        "manifest_output_path": str(manifest_file),
        "backup_path": str(backup),
        "backup_exists": backup.exists(),
        "candidate_count": len(candidates),
        "candidate_source_truncated": bool(review_plan.get("lineage_repair_candidates_truncated")),
        "missing_fact_decision_policy": (
            str(missing_fact_decision or "").strip()
            if str(missing_fact_decision or "").strip() in MISSING_FACT_DECISIONS
            else "manual_rebuild_fact_snapshot"
        ),
        "lineage_decision_manifest_template": undecided_manifest,
        "proposed_confirm_repair_manifest": proposed_manifest,
        "proposed_recheck_summary": {
            "schema": "daily_review_lineage_repair_confirmation_recheck_summary_v1",
            "settled_event_count": proposed_recheck_plan.get("settled_event_count"),
            "effective_settled_event_count": proposed_recheck_plan.get("effective_settled_event_count"),
            "excluded_settled_event_count": proposed_recheck_plan.get("excluded_settled_event_count"),
            "lineage_repair_candidate_count": proposed_recheck_plan.get("lineage_repair_candidate_count"),
            "effective_lineage_repair_candidate_count": proposed_recheck_plan.get("effective_lineage_repair_candidate_count"),
            "apply_blockers": list(proposed_recheck_plan.get("apply_blockers") or []),
            "lineage_decision_manifest_audit": dict(proposed_recheck_plan.get("lineage_decision_manifest_audit") or {}),
            "lineage_decision_apply_readiness": dict(
                proposed_recheck_plan.get("lineage_decision_apply_readiness") or {}
            ),
        },
        "proposed_recheck_batch_plan_for_persistence_contract": proposed_recheck_plan,
        "lineage_repair_apply_contract": repair_contract,
        "lineage_repair_risk_audit": risk_audit,
        "operator_packet": {
            "schema": "daily_review_lineage_repair_operator_packet_v1",
            "preflight": [
                {
                    "step": "review_current_batch_plan",
                    "command": (
                        f"python3 scripts/daily_review_persistence_batch_plan.py --db {_q(db)} "
                        f"--since-days {int(since_days)} --max-items-output {int(max_items_output)}"
                    ),
                    "writes": False,
                    "required": True,
                },
                {
                    "step": "review_operator_runbook",
                    "command": operator_runbook_cmd,
                    "writes": False,
                    "required": bool(candidates),
                    "pass_condition": "operator sees the compact manifest, backup, dry-run, risk, apply, verify, and rollback checklist before materializing anything",
                },
                {
                    "step": "review_or_edit_manifest",
                    "command": "review proposed_confirm_repair_manifest before placing it at manifest_output_path",
                    "writes": False,
                    "required": bool(candidates),
                    "pass_condition": "every candidate decision is explicitly reviewed by the operator",
                },
                {
                    "step": "create_fresh_backup",
                    "command": backup_cmd,
                    "writes": True,
                    "writes_archive_db": False,
                    "required": bool(candidates),
                    "output": str(backup),
                },
                {
                    "step": "dry_run_lineage_repair_apply_entrypoint",
                    "command": dry_run_apply_cmd,
                    "writes": False,
                    "required": bool(candidates),
                },
                {
                    "step": "review_lineage_repair_risk_audit",
                    "command": risk_audit_cmd,
                    "writes": False,
                    "required": bool(candidates),
                    "pass_condition": "each target row diff is merge-fill only, no conflicts exist, and lineage blockers are expected to clear",
                },
                {
                    "step": "apply_lineage_repair",
                    "command": apply_cmd,
                    "writes": bool(candidates),
                    "writes_archive_db": bool(candidates),
                    "requires_user_confirmation": True,
                    "confirmation_phrase": CONFIRMATION_PHRASE,
                    "historical_backfill": False,
                    "places_orders": False,
                    "restarts_launchd": False,
                    "changes_parameters": False,
                },
            ],
            "verification": [
                {
                    "step": "recheck_lineage_scope",
                    "command": recheck_cmd,
                    "required": True,
                },
                {
                    "step": "daily_review_coverage_audit",
                    "command": "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20",
                    "required": False,
                    "reason": "coverage still depends on planned review tables after lineage repair",
                },
                {
                    "step": "ssot_alignment",
                    "command": "python3 scripts/check_ssot_alignment.py --immutable",
                    "required": True,
                },
            ],
            "rollback": [
                {
                    "step": "restore_backup_if_verification_fails",
                    "commands": [
                        "stop_or_quiesce_writers_before_rollback",
                        f"cp -p {_q(backup)} {_q(db)}",
                        recheck_cmd,
                        "python3 scripts/check_ssot_alignment.py --immutable",
                    ],
                    "requires_manual_confirmation": True,
                }
            ],
        },
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_manifest_file": False,
            "creates_backup": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "blocked_operations": [
            "automatic_manifest_file_write",
            "automatic_backup_creation",
            "automatic_lineage_repair_apply",
            "daily_review_persistence",
            "historical_backfill",
            "launchd_restart",
            "order_or_trade_decision_write",
            "parameter_change",
        ],
        "next_actions": (
            [
                "review_proposed_confirm_repair_manifest",
                "review_operator_runbook",
                "create_manifest_file_only_after_operator_review",
                "create_fresh_backup_in_confirmed_maintenance_window",
                "run_dry_run_lineage_repair_apply_entrypoint",
                "run_apply_lineage_repair_only_after_confirmation",
            ]
            if candidates
            else ["no_lineage_repair_candidates"]
        ),
    }


def compact_confirmation_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    proposed_manifest = _as_mapping(packet.get("proposed_confirm_repair_manifest"))
    decisions = [
        dict(_as_mapping(row))
        for row in _as_sequence(proposed_manifest.get("decisions"))
        if isinstance(row, Mapping)
    ]
    recheck = _as_mapping(packet.get("proposed_recheck_summary"))
    readiness = _as_mapping(recheck.get("lineage_decision_apply_readiness"))
    lineage_repair = _as_mapping(readiness.get("lineage_repair"))
    daily_review_persistence = _as_mapping(readiness.get("daily_review_persistence"))
    contract = _as_mapping(packet.get("lineage_repair_apply_contract"))
    risk_audit = _as_mapping(packet.get("lineage_repair_risk_audit"))
    risk_prediction = _as_mapping(risk_audit.get("post_apply_prediction_summary"))
    operator_packet = _as_mapping(packet.get("operator_packet"))
    preflight = [
        dict(_as_mapping(row))
        for row in _as_sequence(operator_packet.get("preflight"))
        if isinstance(row, Mapping)
    ]
    verification = [
        dict(_as_mapping(row))
        for row in _as_sequence(operator_packet.get("verification"))
        if isinstance(row, Mapping)
    ]

    def _steps(rows: Sequence[Mapping[str, Any]], *, required_only: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            if required_only and not bool(row.get("required")):
                continue
            out.append(
                {
                    "step": row.get("step"),
                    "required": bool(row.get("required")),
                    "writes": bool(row.get("writes")),
                    "writes_archive_db": bool(row.get("writes_archive_db")),
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
        "execution_mode": "lineage_repair_final_confirmation_packet_summary_no_apply",
        "db": packet.get("db"),
        "manifest_output_path": packet.get("manifest_output_path"),
        "backup_path": packet.get("backup_path"),
        "backup_exists": bool(packet.get("backup_exists")),
        "candidate_count": packet.get("candidate_count"),
        "candidate_source_truncated": bool(packet.get("candidate_source_truncated")),
        "missing_fact_decision_policy": packet.get("missing_fact_decision_policy"),
        "proposed_decision_counts": _counter([row.get("decision") for row in decisions]),
        "proposed_recheck_summary": {
            "settled_event_count": recheck.get("settled_event_count"),
            "effective_settled_event_count": recheck.get("effective_settled_event_count"),
            "excluded_settled_event_count": recheck.get("excluded_settled_event_count"),
            "lineage_repair_candidate_count": recheck.get("lineage_repair_candidate_count"),
            "effective_lineage_repair_candidate_count": recheck.get(
                "effective_lineage_repair_candidate_count"
            ),
            "apply_blockers": list(recheck.get("apply_blockers") or []),
            "operator_decision_counts": dict(_as_mapping(readiness.get("operator_decision_counts"))),
            "lineage_repair": {
                "repair_required": bool(lineage_repair.get("repair_required")),
                "repair_item_count": lineage_repair.get("repair_item_count"),
                "repair_preflight_ready": bool(lineage_repair.get("repair_preflight_ready")),
                "repair_apply_blockers": list(lineage_repair.get("repair_apply_blockers") or []),
            },
            "daily_review_persistence": {
                "schema_ready": bool(daily_review_persistence.get("schema_ready")),
                "ready_after_explicit_confirmation": bool(
                    daily_review_persistence.get("ready_after_explicit_confirmation")
                ),
                "remaining_hard_blockers": list(
                    daily_review_persistence.get("remaining_hard_blockers") or []
                ),
            },
            "next_action": readiness.get("next_action"),
            "next_boundary": readiness.get("next_boundary"),
        },
        "lineage_repair_apply_contract_summary": {
            "schema": contract.get("schema"),
            "apply_supported": bool(contract.get("apply_supported")),
            "repair_item_count": contract.get("repair_item_count"),
            "apply_blockers": list(contract.get("apply_blockers") or []),
            "confirmation_phrase_required": contract.get("confirmation_phrase_required"),
            "targeted_legacy_fact_lineage_repair_only": bool(
                _as_mapping(contract.get("boundary")).get("targeted_legacy_fact_lineage_repair_only")
            ),
        },
        "lineage_repair_risk_summary": {
            "schema": risk_audit.get("schema"),
            "ok": bool(risk_audit.get("ok")),
            "risk_level": risk_audit.get("risk_level"),
            "blockers": list(risk_audit.get("blockers") or []),
            "repair_item_count": risk_audit.get("repair_item_count"),
            "would_change_count": risk_audit.get("would_change_count"),
            "lineage_blocker_expected_removed_count": risk_audit.get(
                "lineage_blocker_expected_removed_count"
            ),
            "lineage_blocker_expected_remaining_count": risk_prediction.get(
                "lineage_blocker_expected_remaining_count"
            ),
        },
        "preflight_steps": _steps(preflight),
        "required_preflight_steps": _steps(preflight, required_only=True),
        "controlled_apply_step": next(
            (
                item
                for item in _steps(preflight)
                if str(item.get("step") or "") == "apply_lineage_repair"
            ),
            {},
        ),
        "required_verification_steps": _steps(verification, required_only=True),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "blocked_operations": list(packet.get("blocked_operations") or []),
        "next_actions": list(packet.get("next_actions") or []),
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
        choices=MISSING_FACT_DECISIONS,
        default="manual_rebuild_fact_snapshot",
        help="Decision to prefill for candidates with no replayable fact snapshot row.",
    )
    parser.add_argument("--summary", action="store_true", help="Print a compact operator summary instead of the full packet.")
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
    if args.summary:
        packet = compact_confirmation_packet(packet)
    print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
