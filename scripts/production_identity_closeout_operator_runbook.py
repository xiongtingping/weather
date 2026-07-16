#!/usr/bin/env python3
"""Read-only operator runbook for production identity closeout.

This turns the production identity closeout confirmation packet into a compact
operator checklist. It never pauses services, writes manifest files, creates
backups, writes archive.db, fetches weather sources, restarts launchd, or places
orders.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import production_identity_closeout_confirmation_packet as closeout_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "production_identity_closeout_operator_runbook_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


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
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "writes_source_observations": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _load_json_file(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _step_category(row: Mapping[str, Any]) -> str:
    step = str(row.get("step") or "")
    if step == "pause_writer_services":
        return "service_pause"
    if bool(row.get("writes_archive_db")):
        return "archive_db_apply"
    if bool(row.get("writes_manifest_file")):
        return "manifest_materialization"
    if bool(row.get("creates_backup")):
        return "backup"
    if str(row.get("phase") or "") == "verify":
        return "verification"
    return "read_only_preflight"


def _operator_step(row: Mapping[str, Any], *, start_blockers: Sequence[str]) -> dict[str, Any]:
    category = _step_category(row)
    writes_archive_db = bool(row.get("writes_archive_db"))
    blocked_until: list[str] = []
    if row.get("blocked_until"):
        blocked_until.append(str(row.get("blocked_until")))
    if writes_archive_db:
        blocked_until.extend(str(item) for item in start_blockers)
        blocked_until.append("fresh_backup_exists")
        blocked_until.append("writer_quiescence_passes_immediately_before_apply")
        blocked_until.append("operator_enters_confirmation_phrase")
    if category == "backup":
        blocked_until.append("reviewed_manifest_and_temp_drill_pass")
    if category == "manifest_materialization":
        blocked_until.append("operator_reviewed_duplicate_decisions")
    if category == "verification" and row.get("step") != "verify_writer_quiescence":
        blocked_until.append("prior_apply_stage_completed")

    return {
        "order": int(row.get("order") or 0),
        "phase": row.get("phase"),
        "step": row.get("step"),
        "category": category,
        "command": row.get("command") or "",
        "required": bool(row.get("required")),
        "read_only_command": not bool(row.get("writes")),
        "writes_archive_db": writes_archive_db,
        "writes_manifest_file": bool(row.get("writes_manifest_file")),
        "creates_backup": bool(row.get("creates_backup")),
        "restarts_launchd": category == "service_pause",
        "requires_operator_confirmation_phrase": bool(row.get("confirmation_phrase")),
        "confirmation_phrase": row.get("confirmation_phrase") or "",
        "pass_condition": row.get("pass_condition") or "",
        "blocked_until": blocked_until,
        "execution_allowed_now": bool(
            category in {"service_pause", "read_only_preflight"}
            and not writes_archive_db
            and (category != "service_pause" or bool(row.get("required")))
        ),
    }


def _resume_step(packet: Mapping[str, Any]) -> dict[str, Any]:
    maintenance = _as_mapping(packet.get("maintenance_window"))
    operator_commands = _as_mapping(maintenance.get("operator_commands"))
    resume_commands = [str(item) for item in _as_sequence(operator_commands.get("suggested_resume_commands")) if str(item)]
    control_sequence = list(_as_sequence(packet.get("control_sequence")))
    last_order = max([int(_as_mapping(row).get("order") or 0) for row in control_sequence] or [0])
    return {
        "order": last_order + 1,
        "phase": "restore",
        "step": "resume_writer_services",
        "category": "service_resume",
        "command": " && ".join(resume_commands) or "no_writer_services_to_resume",
        "required": bool(resume_commands),
        "read_only_command": False,
        "writes_archive_db": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "restarts_launchd": bool(resume_commands),
        "requires_operator_confirmation_phrase": False,
        "confirmation_phrase": "",
        "pass_condition": "services restored after successful apply or operator aborts maintenance window",
        "blocked_until": ["post_apply_verification_complete_or_operator_aborts_after_pause"],
        "execution_allowed_now": False,
    }


def _start_blockers(packet: Mapping[str, Any]) -> list[str]:
    blockers = [str(item) for item in _as_sequence(packet.get("hard_blockers")) if str(item)]
    maintenance = _as_mapping(packet.get("maintenance_window"))
    stop_auth = _as_mapping(maintenance.get("stop_authorization"))
    for blocker in _as_sequence(stop_auth.get("blockers")):
        text = str(blocker)
        if text and text not in blockers:
            blockers.append(text)
    return blockers


def build_operator_runbook(
    db_path: Path | str = DEFAULT_DB,
    *,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    max_items_output: int = 8,
    limit: int = 1000000,
    duplicate_decision_manifest: Mapping[str, Any] | None = None,
    latest_anchor_policy: str = closeout_packet.DEFAULT_LATEST_ANCHOR_POLICY,
) -> dict[str, Any]:
    packet = closeout_packet.build_confirmation_packet(
        db_path,
        backup_dir=backup_dir,
        generated_at=generated_at,
        max_items_output=int(max_items_output),
        limit=int(limit),
        duplicate_decision_manifest=duplicate_decision_manifest,
        latest_anchor_policy=str(latest_anchor_policy or closeout_packet.DEFAULT_LATEST_ANCHOR_POLICY),
    )
    return build_operator_runbook_from_packet(packet)


def build_operator_runbook_from_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    blockers = _start_blockers(packet)
    maintenance = _as_mapping(packet.get("maintenance_window"))
    operator_commands = _as_mapping(maintenance.get("operator_commands"))
    steps = [
        _operator_step(_as_mapping(row), start_blockers=blockers)
        for row in _as_sequence(packet.get("control_sequence"))
    ]
    steps.append(_resume_step(packet))
    apply_steps = [row for row in steps if row.get("writes_archive_db")]
    identity_audit_indexes = _as_mapping(packet.get("identity_audit_indexes"))

    ready = bool(packet.get("ok") and not blockers and maintenance.get("writer_quiescent") is True)
    return {
        "schema": SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_closeout_operator_runbook_no_apply",
        "db": packet.get("db"),
        "generated_at": packet.get("generated_at"),
        "ready_for_controlled_apply_now": ready,
        "start_blockers": blockers,
        "next_boundary": (
            "run_operator_sequence_with_explicit_confirmations"
            if ready
            else "resolve_start_blockers_then_rerun_operator_runbook"
        ),
        "maintenance_window": {
            "writer_quiescent": bool(maintenance.get("writer_quiescent")),
            "required_stop_labels": list(maintenance.get("required_stop_labels") or []),
            "operator_commands": dict(operator_commands),
            "stop_authorization": dict(_as_mapping(maintenance.get("stop_authorization"))),
            "next_boundary": maintenance.get("next_boundary"),
        },
        "artifacts": {
            "duplicate_manifest_path": packet.get("duplicate_manifest_path"),
            "duplicate_manifest_exists": bool(packet.get("duplicate_manifest_exists")),
            "shared_backup_path": packet.get("shared_backup_path"),
            "shared_backup_exists": bool(packet.get("shared_backup_exists")),
            "latest_anchor_policy": packet.get("latest_anchor_policy"),
        },
        "identity_scope": {
            "identity_audit_index_contract_ok": identity_audit_indexes.get("contract_ok"),
            "identity_audit_index_already_satisfied": identity_audit_indexes.get("already_satisfied"),
            "identity_audit_index_needs_apply": identity_audit_indexes.get("needs_apply"),
            "identity_audit_index_ddl_count": identity_audit_indexes.get("ddl_count"),
            "identity_audit_index_contract_blocker_count": identity_audit_indexes.get("contract_blocker_count"),
            "identity_audit_index_apply_blockers": list(identity_audit_indexes.get("apply_blockers") or []),
            "duplicate_row_count": _as_mapping(packet.get("duplicate")).get("row_count"),
            "duplicate_apply_operation_count": _as_mapping(packet.get("duplicate")).get(
                "reviewed_apply_operation_count"
            ),
            "merge_fill_operation_count": _as_mapping(packet.get("merge_fill")).get("operation_count"),
            "merge_fill_expected_ready_after_duplicate_repair_apply": _as_mapping(packet.get("merge_fill")).get(
                "expected_to_be_ready_after_duplicate_repair_apply"
            ),
        },
        "operator_sequence": steps,
        "impact_summary": {
            "archive_db_apply_steps": [row.get("step") for row in apply_steps],
            "manifest_write_steps": [row.get("step") for row in steps if row.get("writes_manifest_file")],
            "backup_steps": [row.get("step") for row in steps if row.get("creates_backup")],
            "service_control_steps": [
                row.get("step")
                for row in steps
                if row.get("category") in {"service_pause", "service_resume"} and row.get("required")
            ],
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "changes_parameters": False,
            "historical_backfill": False,
        },
        "confirmation_phrases": dict(_as_mapping(packet.get("confirmation_phrases"))),
        "guards": _guards(),
        "source_closeout_summary": closeout_packet.compact_confirmation_packet(packet),
    }


def compact_operator_runbook(runbook: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": runbook.get("schema"),
        "read_only": bool(runbook.get("read_only")),
        "dry_run": bool(runbook.get("dry_run")),
        "execution_mode": "production_identity_closeout_operator_runbook_summary_no_apply",
        "db": runbook.get("db"),
        "ready_for_controlled_apply_now": bool(runbook.get("ready_for_controlled_apply_now")),
        "start_blockers": list(runbook.get("start_blockers") or []),
        "next_boundary": runbook.get("next_boundary"),
        "maintenance_window": dict(_as_mapping(runbook.get("maintenance_window"))),
        "artifacts": dict(_as_mapping(runbook.get("artifacts"))),
        "identity_scope": dict(_as_mapping(runbook.get("identity_scope"))),
        "operator_sequence": [
            {
                "order": row.get("order"),
                "phase": row.get("phase"),
                "step": row.get("step"),
                "category": row.get("category"),
                "command": row.get("command"),
                "required": bool(row.get("required")),
                "read_only_command": bool(row.get("read_only_command")),
                "writes_archive_db": bool(row.get("writes_archive_db")),
                "writes_manifest_file": bool(row.get("writes_manifest_file")),
                "creates_backup": bool(row.get("creates_backup")),
                "restarts_launchd": bool(row.get("restarts_launchd")),
                "requires_operator_confirmation_phrase": bool(row.get("requires_operator_confirmation_phrase")),
                "confirmation_phrase": row.get("confirmation_phrase"),
                "blocked_until": list(row.get("blocked_until") or []),
                "execution_allowed_now": bool(row.get("execution_allowed_now")),
            }
            for row in _as_sequence(runbook.get("operator_sequence"))
        ],
        "impact_summary": dict(_as_mapping(runbook.get("impact_summary"))),
        "confirmation_phrases": dict(_as_mapping(runbook.get("confirmation_phrases"))),
        "guards": dict(_as_mapping(runbook.get("guards"))),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--generated-at", default="")
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--duplicate-decision-manifest", type=Path, default=None)
    parser.add_argument("--latest-anchor-policy", default=closeout_packet.DEFAULT_LATEST_ANCHOR_POLICY)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runbook = build_operator_runbook(
        args.db,
        backup_dir=args.backup_dir,
        generated_at=str(args.generated_at or "") or None,
        max_items_output=int(args.max_items_output),
        limit=int(args.limit),
        duplicate_decision_manifest=_load_json_file(args.duplicate_decision_manifest),
        latest_anchor_policy=str(args.latest_anchor_policy or closeout_packet.DEFAULT_LATEST_ANCHOR_POLICY),
    )
    payload: Mapping[str, Any] = compact_operator_runbook(runbook) if args.summary else runbook
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if runbook.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
