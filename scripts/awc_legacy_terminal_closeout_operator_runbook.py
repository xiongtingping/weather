#!/usr/bin/env python3
"""Read-only operator runbook for AWC legacy terminal closeout.

The runbook turns the AWC terminal closeout packet and apply contract into an
ordered operator checklist. It never writes archive.db, creates backups, writes
manifest files, pauses services, restarts launchd, fetches weather sources, or
places orders.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import awc_legacy_terminal_closeout_apply_contract as apply_contract  # noqa: E402
from scripts import awc_legacy_terminal_closeout_decision_packet as decision_packet  # noqa: E402
from scripts import awc_legacy_terminal_closeout_review_report as review_report  # noqa: E402
from scripts import controlled_apply_maintenance_window_packet as maintenance_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "awc_legacy_terminal_closeout_operator_runbook_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _load_json_file(path: Path | str | None) -> dict[str, Any] | None:
    if not path:
        return None
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_awc_hard_source_signals": False,
        "writes_signal_to_order_timing": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _step(
    *,
    order: int,
    phase: str,
    step: str,
    category: str,
    command: str = "",
    required: bool = True,
    read_only_command: bool = True,
    writes_archive_db: bool = False,
    writes_manifest_file: bool = False,
    creates_backup: bool = False,
    restarts_launchd: bool = False,
    confirmation_phrase: str = "",
    pass_condition: str = "",
    blocked_until: Sequence[str] = (),
    execution_allowed_now: bool = False,
) -> dict[str, Any]:
    return {
        "order": int(order),
        "phase": phase,
        "step": step,
        "category": category,
        "command": command,
        "required": bool(required),
        "read_only_command": bool(read_only_command),
        "writes_archive_db": bool(writes_archive_db),
        "writes_manifest_file": bool(writes_manifest_file),
        "creates_backup": bool(creates_backup),
        "restarts_launchd": bool(restarts_launchd),
        "requires_operator_confirmation_phrase": bool(confirmation_phrase),
        "confirmation_phrase": confirmation_phrase,
        "pass_condition": pass_condition,
        "blocked_until": [str(item) for item in blocked_until if str(item)],
        "execution_allowed_now": bool(execution_allowed_now),
    }


def _start_blockers(
    *,
    packet: Mapping[str, Any],
    contract: Mapping[str, Any],
    maintenance: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if int(packet.get("candidate_count") or 0) <= 0:
        blockers.append("awc_legacy_terminal_closeout_no_candidates")
    for item in _as_sequence(packet.get("hard_blockers")):
        text = str(item or "")
        if text and text not in blockers:
            blockers.append(text)
    for item in _as_sequence(contract.get("blockers")):
        text = str(item or "")
        if text and text not in blockers:
            blockers.append(text)
    if not bool(maintenance.get("writer_quiescent")):
        blockers.append("writer_quiescence_not_satisfied")
    stop_auth = _as_mapping(maintenance.get("stop_authorization"))
    for item in _as_sequence(stop_auth.get("blockers")):
        text = str(item or "")
        if text and text not in blockers:
            blockers.append(text)
    return blockers


def _operator_sequence(
    *,
    db: Path,
    manifest_path: Path,
    review_report_path: Path,
    backup_path: Path,
    packet: Mapping[str, Any],
    contract: Mapping[str, Any],
    maintenance: Mapping[str, Any],
    since_hours: float,
    limit: int,
    max_items_output: int,
    start_blockers: Sequence[str],
) -> list[dict[str, Any]]:
    operator_commands = _as_mapping(maintenance.get("operator_commands"))
    pause_commands = [str(item) for item in _as_sequence(operator_commands.get("suggested_pause_commands")) if str(item)]
    resume_commands = [str(item) for item in _as_sequence(operator_commands.get("suggested_resume_commands")) if str(item)]
    operator_contract = _as_mapping(contract.get("operator_contract"))
    materialize_command = (
        "python3 scripts/awc_legacy_terminal_closeout_review_report.py "
        f"--db {_q(db)} --since-hours {float(since_hours):g} --limit {int(limit)} "
        f"--max-items-output {int(max_items_output)} --emit-draft-manifest "
        f"> {_q(manifest_path)}"
    )
    review_report_command = (
        "python3 scripts/awc_legacy_terminal_closeout_review_report.py "
        f"--db {_q(db)} --since-hours {float(since_hours):g} --limit {int(limit)} "
        f"--max-items-output {int(max_items_output)} > {_q(review_report_path)}"
    )
    audit_command = (
        "python3 scripts/awc_legacy_terminal_closeout_apply_contract.py "
        f"--db {_q(db)} --decision-manifest {_q(manifest_path)} --since-hours {float(since_hours):g} "
        f"--limit {int(limit)} --max-items-output {int(max_items_output)} --summary"
    )
    backup_command = str(operator_contract.get("backup_command") or "")
    if backup_command and str(operator_contract.get("backup_path") or "") != str(backup_path):
        backup_command = (
            f"mkdir -p {_q(backup_path.parent)} && sqlite3 {_q(db)} "
            f"\".backup {_q(backup_path)}\""
        )
    verify_quiescence_command = str(
        operator_contract.get("verify_writer_quiescence_command")
        or operator_commands.get("verify_writer_quiescence")
        or ""
    )
    apply_command = (
        "python3 scripts/awc_legacy_terminal_closeout_apply.py "
        f"--db {_q(db)} --decision-manifest {_q(manifest_path)} --backup {_q(backup_path)} "
        f"--writer-quiescent --confirm {apply_contract.CONFIRMATION_PHRASE} "
        f"--since-hours {float(since_hours):g} --limit {int(limit)} --max-items-output {int(max_items_output)}"
    )
    post_apply = [str(item) for item in _as_sequence(operator_contract.get("post_apply_verification_commands")) if str(item)]
    return [
        _step(
            order=1,
            phase="review",
            step="emit_review_report",
            category="review_report",
            command=review_report_command,
            read_only_command=False,
            pass_condition="operator receives evidence summary for every AWC terminal closeout candidate",
            blocked_until=[],
            execution_allowed_now=int(packet.get("candidate_count") or 0) > 0,
        ),
        _step(
            order=2,
            phase="review",
            step="emit_decision_manifest_template",
            category="manifest_template",
            command=materialize_command,
            read_only_command=False,
            writes_manifest_file=True,
            pass_condition=(
                "operator receives draft manifest derived from the same review report; "
                "final terminal evidence may be prefilled, operator-review candidates remain undecided"
            ),
            blocked_until=["review_report_materialized"],
            execution_allowed_now=int(packet.get("candidate_count") or 0) > 0,
        ),
        _step(
            order=3,
            phase="review",
            step="operator_reviews_decision_manifest",
            category="manual_review",
            command=f"open {_q(manifest_path)}",
            read_only_command=False,
            writes_manifest_file=True,
            pass_condition="each candidate has an explicit non-undecided operator decision and review reason where required",
            blocked_until=["decision_manifest_template_materialized"],
        ),
        _step(
            order=4,
            phase="preflight",
            step="audit_reviewed_decision_manifest",
            category="read_only_preflight",
            command=audit_command,
            pass_condition="apply contract ok=true and operation_count matches reviewed decisions",
            blocked_until=["operator_reviewed_decision_manifest"],
            execution_allowed_now=True,
        ),
        _step(
            order=5,
            phase="maintenance",
            step="pause_writer_services",
            category="service_pause",
            command=" && ".join(pause_commands) or "no_writer_services_to_pause",
            read_only_command=False,
            restarts_launchd=bool(pause_commands),
            pass_condition="maintenance packet no longer reports active writer services",
            blocked_until=["reviewed_decision_manifest_audit_passes"],
            execution_allowed_now=bool(pause_commands),
        ),
        _step(
            order=6,
            phase="maintenance",
            step="verify_writer_quiescence",
            category="read_only_preflight",
            command=verify_quiescence_command,
            pass_condition="writer_quiescent == true and blocker_count == 0",
            blocked_until=["writer_services_paused"],
            execution_allowed_now=True,
        ),
        _step(
            order=7,
            phase="backup",
            step="create_fresh_backup",
            category="backup",
            command=backup_command,
            read_only_command=False,
            creates_backup=True,
            pass_condition="fresh backup file exists immediately before apply",
            blocked_until=["writer_quiescence_passes_immediately_before_backup"],
        ),
        _step(
            order=8,
            phase="apply",
            step="apply_awc_legacy_terminal_closeout",
            category="archive_db_apply",
            command=apply_command,
            read_only_command=False,
            writes_archive_db=True,
            confirmation_phrase=apply_contract.CONFIRMATION_PHRASE,
            pass_condition="apply report ok=true and applied_count equals contract operation_count",
            blocked_until=[
                "reviewed_decision_manifest_audit_passes",
                "fresh_backup_exists",
                "writer_quiescence_passes_immediately_before_apply",
                "operator_enters_confirmation_phrase",
            ],
        ),
        _step(
            order=9,
            phase="verify",
            step="verify_awc_terminal_coverage",
            category="verification",
            command=post_apply[0] if post_apply else "",
            pass_condition="awc_hard_source_terminal_coverage_gate has no AWC terminal blockers",
            blocked_until=["apply_awc_legacy_terminal_closeout_completed"],
            execution_allowed_now=bool(post_apply),
        ),
        _step(
            order=10,
            phase="verify",
            step="verify_joint_gate",
            category="verification",
            command=post_apply[1] if len(post_apply) > 1 else "",
            pass_condition="full joint gate advances past AWC terminal coverage dependency",
            blocked_until=["verify_awc_terminal_coverage_passes"],
            execution_allowed_now=len(post_apply) > 1,
        ),
        _step(
            order=11,
            phase="restore",
            step="resume_writer_services",
            category="service_resume",
            command=" && ".join(resume_commands) or "no_writer_services_to_resume",
            read_only_command=False,
            restarts_launchd=bool(resume_commands),
            pass_condition="writer services restored after successful apply or operator aborts maintenance window",
            blocked_until=["post_apply_verification_complete_or_operator_aborts_after_pause"],
        ),
    ]


def build_operator_runbook(
    db_path: Path | str = DEFAULT_DB,
    *,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    since_hours: float = 24.0 * 90.0,
    limit: int = 500,
    max_items_output: int = 8,
    decision_manifest: Mapping[str, Any] | None = None,
    decision_manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    stamp = str(generated_at or _now_stamp()).replace(":", "").replace("-", "")
    manifest_path = (
        Path(decision_manifest_path).expanduser().resolve()
        if decision_manifest_path
        else backup_root / f"archive.db.awc-legacy-terminal-closeout-{stamp}.decision-manifest.json"
    )
    review_path = backup_root / f"archive.db.awc-legacy-terminal-closeout-{stamp}.review-report.json"
    manifest = dict(decision_manifest or _load_json_file(decision_manifest_path) or {})
    packet = decision_packet.build_packet(
        db,
        since_hours=float(since_hours),
        limit=int(limit),
        max_items_output=int(max_items_output),
        decision_manifest=manifest,
    )
    contract = apply_contract.build_contract(
        db,
        decision_manifest=manifest,
        closeout_packet=packet,
        since_hours=float(since_hours),
        limit=int(limit),
        max_items_output=int(max_items_output),
        backup_dir=backup_root,
    )
    maintenance = maintenance_packet.build_maintenance_window_packet(db, max_items_output=int(max_items_output))
    return build_operator_runbook_from_components(
        db_path=db,
        backup_dir=backup_root,
        generated_at=stamp,
        since_hours=float(since_hours),
        limit=int(limit),
        max_items_output=int(max_items_output),
        manifest_path=manifest_path,
        review_report_path=review_path,
        closeout_packet=packet,
        apply_contract_report=contract,
        maintenance_report=maintenance,
    )


def build_operator_runbook_from_components(
    *,
    db_path: Path | str,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    generated_at: str | None = None,
    since_hours: float = 24.0 * 90.0,
    limit: int = 500,
    max_items_output: int = 8,
    manifest_path: Path | str | None = None,
    review_report_path: Path | str | None = None,
    closeout_packet: Mapping[str, Any],
    apply_contract_report: Mapping[str, Any],
    maintenance_report: Mapping[str, Any],
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    stamp = str(generated_at or _now_stamp()).replace(":", "").replace("-", "")
    manifest_path = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path
        else backup_root / f"archive.db.awc-legacy-terminal-closeout-{stamp}.decision-manifest.json"
    )
    review_report_path = (
        Path(review_report_path).expanduser().resolve()
        if review_report_path
        else backup_root / f"archive.db.awc-legacy-terminal-closeout-{stamp}.review-report.json"
    )
    backup_path = backup_root / f"archive.db.awc-legacy-terminal-closeout-{stamp}.bak"
    packet = dict(closeout_packet)
    contract = dict(apply_contract_report)
    maintenance = dict(maintenance_report)
    blockers = _start_blockers(packet=packet, contract=contract, maintenance=maintenance)
    sequence = _operator_sequence(
        db=db,
        manifest_path=manifest_path,
        review_report_path=review_report_path,
        backup_path=backup_path,
        packet=packet,
        contract=contract,
        maintenance=maintenance,
        since_hours=float(since_hours),
        limit=int(limit),
        max_items_output=int(max_items_output),
        start_blockers=blockers,
    )
    ready = bool(contract.get("ok") and maintenance.get("writer_quiescent") is True and not blockers)
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "awc_legacy_terminal_closeout_operator_runbook_no_apply",
        "db": str(db),
        "generated_at": generated_at or stamp,
        "ready_for_controlled_apply_now": ready,
        "start_blockers": blockers,
        "next_boundary": (
            "run_awc_legacy_terminal_closeout_operator_sequence"
            if ready
            else "review_awc_terminal_manifest_and_satisfy_writer_quiescence_before_apply"
        ),
        "artifacts": {
            "review_report_path": str(review_report_path),
            "review_report_exists": review_report_path.exists(),
            "decision_manifest_path": str(manifest_path),
            "decision_manifest_exists": manifest_path.exists(),
            "shared_backup_path": str(backup_path),
            "shared_backup_exists": backup_path.exists(),
        },
        "awc_terminal_scope": {
            "candidate_count": int(packet.get("candidate_count") or 0),
            "recommended_materialize_count": int(packet.get("recommended_materialize_count") or 0),
            "operator_review_required_count": int(packet.get("operator_review_required_count") or 0),
            "manifest_provided": bool(_as_mapping(packet.get("decision_manifest_audit")).get("manifest_provided")),
            "contract_operation_count": int(contract.get("operation_count") or 0),
        },
        "maintenance_window": {
            "writer_quiescent": bool(maintenance.get("writer_quiescent")),
            "required_stop_labels": list(maintenance.get("required_stop_labels") or []),
            "operator_commands": dict(_as_mapping(maintenance.get("operator_commands"))),
            "stop_authorization": dict(_as_mapping(maintenance.get("stop_authorization"))),
            "next_boundary": maintenance.get("next_boundary"),
        },
        "operator_sequence": sequence,
        "impact_summary": {
            "archive_db_apply_steps": [row.get("step") for row in sequence if row.get("writes_archive_db")],
            "review_report_steps": [row.get("step") for row in sequence if row.get("category") == "review_report"],
            "manifest_write_steps": [row.get("step") for row in sequence if row.get("writes_manifest_file")],
            "backup_steps": [row.get("step") for row in sequence if row.get("creates_backup")],
            "service_control_steps": [
                row.get("step")
                for row in sequence
                if row.get("category") in {"service_pause", "service_resume"} and row.get("restarts_launchd")
            ],
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "changes_parameters": False,
            "historical_backfill": False,
        },
        "confirmation_phrases": {
            "apply_awc_legacy_terminal_closeout": apply_contract.CONFIRMATION_PHRASE,
        },
        "guards": _guards(),
        "closeout_packet_summary": decision_packet.compact_packet(packet),
        "apply_contract_summary": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
    }


def compact_operator_runbook(runbook: Mapping[str, Any]) -> dict[str, Any]:
    sequence = _as_sequence(runbook.get("operator_sequence"))
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": runbook.get("schema"),
        "ok": bool(runbook.get("ok")),
        "read_only": bool(runbook.get("read_only")),
        "dry_run": bool(runbook.get("dry_run")),
        "execution_mode": "awc_legacy_terminal_closeout_operator_runbook_summary_no_apply",
        "db": runbook.get("db"),
        "ready_for_controlled_apply_now": bool(runbook.get("ready_for_controlled_apply_now")),
        "start_blockers": list(runbook.get("start_blockers") or []),
        "next_boundary": runbook.get("next_boundary"),
        "artifacts": dict(_as_mapping(runbook.get("artifacts"))),
        "awc_terminal_scope": dict(_as_mapping(runbook.get("awc_terminal_scope"))),
        "maintenance_window": dict(_as_mapping(runbook.get("maintenance_window"))),
        "operator_sequence": [
            {
                "order": row.get("order"),
                "phase": row.get("phase"),
                "step": row.get("step"),
                "category": row.get("category"),
                "command": row.get("command"),
                "writes_archive_db": bool(row.get("writes_archive_db")),
                "writes_manifest_file": bool(row.get("writes_manifest_file")),
                "creates_backup": bool(row.get("creates_backup")),
                "restarts_launchd": bool(row.get("restarts_launchd")),
                "requires_operator_confirmation_phrase": bool(row.get("requires_operator_confirmation_phrase")),
                "confirmation_phrase": row.get("confirmation_phrase") or "",
                "blocked_until": list(row.get("blocked_until") or []),
                "execution_allowed_now": bool(row.get("execution_allowed_now")),
            }
            for row in sequence
        ],
        "impact_summary": dict(_as_mapping(runbook.get("impact_summary"))),
        "confirmation_phrases": dict(_as_mapping(runbook.get("confirmation_phrases"))),
        "guards": dict(_as_mapping(runbook.get("guards"))),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--generated-at", default="")
    parser.add_argument("--since-hours", type=float, default=24.0 * 90.0)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--decision-manifest", default="")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runbook = build_operator_runbook(
        args.db,
        backup_dir=args.backup_dir,
        generated_at=str(args.generated_at or ""),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        decision_manifest_path=args.decision_manifest or None,
    )
    payload: Mapping[str, Any] = compact_operator_runbook(runbook) if args.summary else runbook
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if runbook.get("read_only") else 2


if __name__ == "__main__":
    raise SystemExit(main())
