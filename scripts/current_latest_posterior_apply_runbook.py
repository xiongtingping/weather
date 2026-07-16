#!/usr/bin/env python3
"""Read-only runbook for current-latest posterior SSOT materialization.

This packet turns the current-latest posterior materialization debt into an
ordered operator sequence. It does not pause services, create backups, write
archive.db, fetch weather data, restart launchd, or place orders.
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

from scripts import daily_review_production_apply_preflight_packet as production_preflight  # noqa: E402
from scripts import posterior_ssot_apply_confirmation_packet as confirmation_packet  # noqa: E402
from scripts import posterior_ssot_apply_preflight_risk_packet as risk_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "current_latest_posterior_apply_runbook_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


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


def _parse_fact_ids(value: str | Sequence[Any] | None = None) -> list[int]:
    values: Sequence[Any]
    if value is None:
        values = []
    elif isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = [value]
    out: list[int] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            fact_id = int(text)
        except Exception:
            continue
        if fact_id > 0 and fact_id not in out:
            out.append(fact_id)
    return out


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_daily_weather_reviews": False,
        "writes_auto_trade_decisions": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _backup_path(db: Path, backup_dir: Path, generated_at: str) -> Path:
    return backup_dir / f"{db.name}.current-latest-posterior-runbook-{_safe_stamp(generated_at)}.bak"


def _candidate_by_fact_id(confirmation: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    out: dict[int, Mapping[str, Any]] = {}
    for row in _as_sequence(confirmation.get("candidates")):
        row_map = _as_mapping(row)
        fact_id = int(row_map.get("fact_id") or 0)
        if fact_id > 0:
            out[fact_id] = row_map
    return out


def _shared_backup_apply_command(
    *,
    db: Path,
    fact_id: int,
    candidate: Mapping[str, Any],
    backup: Path,
) -> str:
    slug = str(candidate.get("slug") or "")
    event_date = str(candidate.get("event_date") or "")
    if not fact_id or not slug or not event_date:
        return ""
    return (
        f"python3 scripts/apply_posterior_ssot_fact_repair.py --db {_q(db)} "
        f"--fact-id {int(fact_id)} --slug {_q(slug)} --event-date {_q(event_date)} "
        f"--backup {_q(backup)} --apply --confirm {confirmation_packet.CONFIRMATION_PHRASE} "
        "--writer-quiescent --json"
    )


def build_runbook(
    db_path: Path | str = DEFAULT_DB,
    *,
    fact_ids: str | Sequence[int] | None = None,
    since_hours: float = 168.0,
    limit: int = 100,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    production_preflight_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup = _backup_path(db, backup_root, generated)

    production_packet = dict(
        production_preflight_packet
        or production_preflight.build_preflight_packet(
            db,
            limit=int(limit),
            max_items_output=int(max_items_output),
        )
    )
    current_latest = _as_mapping(production_packet.get("current_latest_posterior_materialization"))
    explicit_fact_ids = _parse_fact_ids(fact_ids)
    candidate_fact_ids = explicit_fact_ids or _parse_fact_ids(current_latest.get("candidate_fact_ids"))

    confirmation = confirmation_packet.build_confirmation_packet(
        db,
        fact_ids=candidate_fact_ids,
        since_hours=float(since_hours),
        limit=int(limit),
        latest_only=True,
        max_items_output=max(int(max_items_output), len(candidate_fact_ids), 1),
        backup_dir=backup_root,
        generated_at=generated,
    )
    confirmation_summary = confirmation_packet.compact_confirmation_packet(confirmation)
    candidates_by_id = _candidate_by_fact_id(confirmation)
    if not candidate_fact_ids and candidates_by_id:
        candidate_fact_ids = list(candidates_by_id)

    single_fact_preflights: list[dict[str, Any]] = []
    for fact_id in candidate_fact_ids:
        risk = risk_packet.build_preflight_risk_packet(
            db,
            fact_ids=[int(fact_id)],
            since_hours=float(since_hours),
            limit=int(limit),
            latest_only=True,
            max_items_output=int(max_items_output),
            backup_dir=backup_root,
            generated_at=generated,
        )
        candidate = candidates_by_id.get(int(fact_id), {})
        shared_apply_command = (
            _shared_backup_apply_command(
                db=db,
                fact_id=int(fact_id),
                candidate=candidate,
                backup=backup,
            )
            if risk.get("ok") is True
            else ""
        )
        single_fact_preflights.append(
            {
                "fact_id": int(fact_id),
                "ok": bool(risk.get("ok")),
                "risk_level": risk.get("risk_level"),
                "blockers": list(risk.get("blockers") or []),
                "candidate_count": int(risk.get("candidate_count") or 0),
                "expected_write_summary": dict(_as_mapping(risk.get("expected_write_summary"))),
                "projected_target_bucket": candidate.get("projected_target_bucket"),
                "projected_target_probability": candidate.get("projected_target_probability"),
                "slug": candidate.get("slug"),
                "event_date": candidate.get("event_date"),
                "preflight_command": (
                    f"python3 scripts/posterior_ssot_apply_preflight_risk_packet.py --db {_q(db)} "
                    f"--fact-id {int(fact_id)} --limit {int(limit)} "
                    f"--max-items-output {int(max_items_output)}"
                ),
                "apply_command": shared_apply_command,
                "uses_shared_backup": bool(shared_apply_command),
            }
        )

    maintenance = _as_mapping(production_packet.get("maintenance_window"))
    start_blockers = list(production_packet.get("start_blockers") or [])
    risk_blockers = [
        f"fact_{row['fact_id']}:{blocker}"
        for row in single_fact_preflights
        for blocker in row.get("blockers", [])
    ]
    if confirmation.get("ok") is not True:
        risk_blockers.append("confirmation_packet_not_ok")
    if len(candidates_by_id) != len(candidate_fact_ids):
        risk_blockers.append("confirmation_candidate_count_mismatch")

    ready_for_controlled_apply_now = bool(
        not start_blockers
        and not risk_blockers
        and maintenance.get("writer_quiescent") is True
        and candidate_fact_ids
        and all(row.get("ok") for row in single_fact_preflights)
    )
    apply_required = bool(candidate_fact_ids)

    backup_command = f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup)}\""
    coverage_command = (
        "python3 scripts/posterior_ssot_fact_coverage_audit.py --latest-only --project-forward "
        f"--limit {int(limit)} --max-items-output {int(max_items_output)}"
    )
    remat_command = (
        "python3 scripts/posterior_ssot_rematerialization_plan.py --summary "
        f"--limit {int(limit)} --max-items-output {int(max_items_output)}"
    )
    candidate_fact_ids_arg = ",".join(str(int(fact_id)) for fact_id in candidate_fact_ids)
    temp_drill_command = (
        f"python3 scripts/current_latest_posterior_apply_temp_drill.py --db {_q(db)} "
        f"--fact-ids {_q(candidate_fact_ids_arg)} "
        f"--limit {int(limit)} --max-items-output {int(max_items_output)}"
    )
    expected_projections_json = json.dumps(
        [
            {
                "fact_id": int(row.get("fact_id") or 0),
                "target_bucket": row.get("projected_target_bucket"),
                "target_probability": row.get("projected_target_probability"),
            }
            for row in single_fact_preflights
            if int(row.get("fact_id") or 0) > 0
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    exact_acceptance_command = (
        f"python3 scripts/current_latest_posterior_post_apply_acceptance.py --db {_q(db)} "
        f"--candidate-fact-ids {_q(candidate_fact_ids_arg)} "
        f"--expected-projections-json {_q(expected_projections_json)} "
        f"--max-items-output {int(max_items_output)}"
    )
    joint_gate_command = (
        "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary "
        f"--limit {int(limit)} --max-items-output {int(max_items_output)}"
    )

    pause_commands = list(_as_mapping(maintenance.get("operator_commands")).get("suggested_pause_commands") or [])
    resume_commands = list(_as_mapping(maintenance.get("operator_commands")).get("suggested_resume_commands") or [])
    verify_quiescence = str(
        _as_mapping(maintenance.get("operator_commands")).get("verify_writer_quiescence")
        or f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} --summary --max-items-output {int(max_items_output)}"
    )
    sequence: list[dict[str, Any]] = []
    order = 1
    if apply_required:
        sequence.append(
            {
                "order": order,
                "step": "run_current_latest_temp_drill",
                "command": temp_drill_command,
                "writes_archive_db": False,
                "writes_temp_archive_db": True,
                "required": True,
                "candidate_fact_ids": list(candidate_fact_ids),
                "candidate_count": len(candidate_fact_ids),
                "pass_condition": (
                    "ok == true and production_counts_unchanged == true "
                    "and source_runbook.candidate_fact_ids == expected candidate_fact_ids "
                    "and post_apply_rematerialization.current_latest_issue_count == 0"
                ),
            }
        )
        order += 1
    for command in pause_commands:
        sequence.append(
            {
                "order": order,
                "step": "pause_writer_service",
                "command": command,
                "writes_archive_db": False,
                "required": True,
            }
        )
        order += 1
    sequence.extend(
        [
            {
                "order": order,
                "step": "verify_writer_quiescence",
                "command": verify_quiescence,
                "writes_archive_db": False,
                "required": True,
                "pass_condition": "writer_quiescent == true and start_blockers == []",
            },
            {
                "order": order + 1,
                "step": "create_one_shared_archive_backup",
                "command": backup_command,
                "writes_archive_db": False,
                "creates_backup": True,
                "required": bool(candidate_fact_ids),
            },
        ]
    )
    order += 2
    for row in single_fact_preflights:
        apply_enabled_now = bool(ready_for_controlled_apply_now and row.get("ok") and row.get("apply_command"))
        apply_blocked_until = (
            []
            if apply_enabled_now
            else sorted(
                {
                    *[str(item) for item in start_blockers if str(item or "").strip()],
                    *[str(item) for item in risk_blockers if str(item or "").strip()],
                    "verify_writer_quiescence_passes",
                    "shared_backup_created",
                }
            )
        )
        sequence.extend(
            [
                {
                    "order": order,
                    "step": "preflight_one_current_latest_fact",
                    "fact_id": row["fact_id"],
                    "command": row["preflight_command"],
                    "writes_archive_db": False,
                    "required": True,
                },
                {
                    "order": order + 1,
                    "step": "apply_one_current_latest_fact",
                    "fact_id": row["fact_id"],
                    "command": row["apply_command"],
                    "writes_archive_db": True,
                    "required": bool(row["apply_command"]),
                    "confirmation_phrase": confirmation_packet.CONFIRMATION_PHRASE,
                    "writer_quiescent_flag_required": True,
                    "shared_backup_path": str(backup),
                    "enabled_now": apply_enabled_now,
                    "blocked_until": apply_blocked_until,
                    "preconditions": [
                        "run_current_latest_temp_drill returns ok == true for the same candidate_fact_ids",
                        "verify_writer_quiescence returns writer_quiescent == true and start_blockers == []",
                        "apply_posterior_ssot_fact_repair rechecks controlled_apply_maintenance_window_packet before writing",
                        f"create_one_shared_archive_backup has created {backup}",
                        "apply_posterior_ssot_fact_repair verifies the shared backup contains the same candidate fact and latest pointer before writing",
                        "preflight_one_current_latest_fact is ok for this fact_id",
                        f"operator supplies {confirmation_packet.CONFIRMATION_PHRASE}",
                    ],
                },
            ]
        )
        order += 2
    sequence.extend(
        [
            {
                "order": order,
                "step": "post_apply_latest_coverage",
                "command": coverage_command,
                "writes_archive_db": False,
                "required": True,
            },
            {
                "order": order + 1,
                "step": "post_apply_rematerialization_governance",
                "command": remat_command,
                "writes_archive_db": False,
                "required": True,
            },
            {
                "order": order + 2,
                "step": "post_apply_exact_candidate_acceptance",
                "command": exact_acceptance_command,
                "writes_archive_db": False,
                "required": bool(candidate_fact_ids),
                "pass_condition": (
                    "ok == true and accepted_count == candidate_count "
                    "and every original candidate latest_replaced_candidate == true"
                ),
            },
            {
                "order": order + 3,
                "step": "post_apply_joint_plan_gate",
                "command": joint_gate_command,
                "writes_archive_db": False,
                "required": True,
            },
        ]
    )
    order += 4
    for command in resume_commands:
        sequence.append(
            {
                "order": order,
                "step": "resume_writer_service",
                "command": command,
                "writes_archive_db": False,
                "required": True,
            }
        )
        order += 1

    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(not risk_blockers),
        "read_only": True,
        "dry_run": True,
        "execution_mode": "current_latest_posterior_apply_runbook_no_apply",
        "generated_at": generated,
        "db": str(db),
        "scope": {
            "fact_ids_requested": explicit_fact_ids,
            "candidate_fact_ids": candidate_fact_ids,
            "apply_required": apply_required,
            "since_hours": float(since_hours),
            "limit": int(limit),
            "max_items_output": int(max_items_output),
        },
        "ready_for_controlled_apply_now": ready_for_controlled_apply_now,
        "start_blockers": sorted(set(str(item) for item in start_blockers if str(item or "").strip())),
        "risk_blockers": sorted(set(str(item) for item in risk_blockers if str(item or "").strip())),
        "maintenance_window": {
            "writer_quiescent": bool(maintenance.get("writer_quiescent")),
            "required_stop_labels": list(maintenance.get("required_stop_labels") or []),
            "stop_authorization": dict(_as_mapping(maintenance.get("stop_authorization"))),
        },
        "current_latest_posterior_materialization": dict(current_latest),
        "confirmation_summary": confirmation_summary,
        "single_fact_preflights": single_fact_preflights,
        "shared_backup": {
            "path": str(backup),
            "command": backup_command,
            "exists": backup.exists(),
            "reuse_with_apply_flag": "--backup",
        },
        "operator_sequence": sequence,
        "guards": _guards(),
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
        "next_boundary": (
            "operator_can_execute_sequence_after_writer_quiescence_and_confirmation"
            if ready_for_controlled_apply_now
            else "no_current_latest_posterior_apply_required"
            if not apply_required and not risk_blockers
            else "resolve_start_or_risk_blockers_before_controlled_apply"
        ),
    }


def compact_runbook(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "current_latest_posterior_apply_runbook_summary_no_apply",
        "ready_for_controlled_apply_now": bool(packet.get("ready_for_controlled_apply_now")),
        "start_blockers": list(packet.get("start_blockers") or []),
        "risk_blockers": list(packet.get("risk_blockers") or []),
        "scope": dict(_as_mapping(packet.get("scope"))),
        "apply_required": bool(_as_mapping(packet.get("scope")).get("apply_required")),
        "maintenance_window": dict(_as_mapping(packet.get("maintenance_window"))),
        "shared_backup": dict(_as_mapping(packet.get("shared_backup"))),
        "single_fact_preflights": [
            {
                "fact_id": row.get("fact_id"),
                "ok": bool(row.get("ok")),
                "risk_level": row.get("risk_level"),
                "blockers": list(row.get("blockers") or []),
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "projected_target_bucket": row.get("projected_target_bucket"),
                "projected_target_probability": row.get("projected_target_probability"),
                "uses_shared_backup": bool(row.get("uses_shared_backup")),
                "apply_command_available": bool(row.get("apply_command")),
            }
            for row in _as_sequence(packet.get("single_fact_preflights"))
        ],
        "operator_sequence": [
            {
                "order": row.get("order"),
                "step": row.get("step"),
                "fact_id": row.get("fact_id"),
                "required": bool(row.get("required")),
                "writes_archive_db": bool(row.get("writes_archive_db")),
                "writes_temp_archive_db": bool(row.get("writes_temp_archive_db")),
                "command": row.get("command"),
                "enabled_now": bool(row.get("enabled_now")),
                "blocked_until": list(row.get("blocked_until") or []),
                "preconditions": list(row.get("preconditions") or []),
                "candidate_fact_ids": list(row.get("candidate_fact_ids") or []),
                "candidate_count": int(row.get("candidate_count") or 0),
                "pass_condition": row.get("pass_condition"),
            }
            for row in _as_sequence(packet.get("operator_sequence"))
        ],
        "guards": dict(_as_mapping(packet.get("guards"))),
        "boundary": dict(_as_mapping(packet.get("boundary"))),
        "next_boundary": packet.get("next_boundary"),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-ids", default="", help="Optional comma-separated exact event_fact_history ids.")
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    packet = build_runbook(
        args.db,
        fact_ids=_parse_fact_ids(args.fact_ids),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
    )
    payload = compact_runbook(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
