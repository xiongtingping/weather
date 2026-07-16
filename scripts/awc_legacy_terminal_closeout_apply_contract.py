#!/usr/bin/env python3
"""Read-only apply contract for AWC legacy terminal closeout.

The contract consumes ``awc_legacy_terminal_closeout_decision_packet`` and
projects the exact writer-owned ledger operations needed to materialize reviewed
terminal states. It never writes archive.db, creates backups, writes manifests,
fetches sources, restarts services, or places orders.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import awc_legacy_terminal_closeout_decision_packet as decision_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
SCHEMA_VERSION = "awc_legacy_terminal_closeout_apply_contract_v1"
CONFIRMATION_PHRASE = "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT"
ALLOWED_WRITE_TARGETS = {"awc_hard_source_signals", "signal_to_order_timing"}
ALLOWED_WRITERS = {"upsert_awc_hard_source_signal", "upsert_signal_to_order_timing"}


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_awc_hard_source_signals": False,
        "writes_signal_to_order_timing": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _q(value: Path | str) -> str:
    return shlex.quote(str(value))


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _load_json_file(path: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    if not path:
        return None
    payload_path = Path(path).expanduser()
    with payload_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _operation_contract(raw: Mapping[str, Any]) -> dict[str, Any]:
    op = _as_mapping(raw)
    signal_id = str(op.get("signal_id") or "").strip()
    terminal_state = str(op.get("terminal_state") or "").strip()
    terminal_reason = str(op.get("terminal_reason") or "").strip()
    write_targets = [str(item or "") for item in _as_sequence(op.get("write_targets"))]
    writers = [str(item or "") for item in _as_sequence(op.get("uses_writers"))]
    payload = _as_mapping(op.get("payload"))
    payload_timing = _as_mapping(payload.get("signal_to_order_timing"))
    blockers: list[str] = []
    if not signal_id:
        blockers.append("signal_id_missing")
    if terminal_state not in decision_packet.FINAL_TERMINAL_STATES:
        blockers.append("terminal_state_not_final")
    if not terminal_reason:
        blockers.append("terminal_reason_missing")
    if set(write_targets) != ALLOWED_WRITE_TARGETS:
        blockers.append("write_targets_not_exactly_awc_signal_and_timing_ledgers")
    if set(writers) != ALLOWED_WRITERS:
        blockers.append("writers_not_exactly_awc_signal_and_timing_writers")
    if str(payload.get("signal_id") or "").strip() != signal_id:
        blockers.append("payload_signal_id_mismatch")
    if str(payload_timing.get("signal_id") or "").strip() != signal_id:
        blockers.append("timing_signal_id_mismatch")
    if str(payload_timing.get("terminal_state") or "").strip() != terminal_state:
        blockers.append("timing_terminal_state_mismatch")
    if not str(payload_timing.get("terminal_at") or "").strip():
        blockers.append("terminal_at_missing")
    return {
        "signal_id": signal_id,
        "operation": "upsert_awc_legacy_terminal_ledgers",
        "terminal_state": terminal_state,
        "terminal_reason": terminal_reason,
        "write_targets": write_targets,
        "uses_writers": writers,
        "payload": payload,
        "blockers": blockers,
    }


def build_contract(
    db_path: Path | str = DEFAULT_DB,
    *,
    decision_manifest: Mapping[str, Any] | None = None,
    closeout_packet: Mapping[str, Any] | None = None,
    since_hours: float = 24.0 * 90.0,
    limit: int = 1000,
    max_items_output: int = 20,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup_root = Path(backup_dir).expanduser().resolve()
    backup_path = backup_root / "archive.db.awc-legacy-terminal-closeout.bak"
    packet = dict(
        closeout_packet
        or decision_packet.build_packet(
            db,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
            decision_manifest=decision_manifest,
        )
    )
    audit = _as_mapping(packet.get("decision_manifest_audit"))
    raw_operations = [_as_mapping(item) for item in _as_sequence(audit.get("operations"))]
    operations = [_operation_contract(item) for item in raw_operations]
    operation_blockers = [
        {"signal_id": op.get("signal_id"), "blockers": list(op.get("blockers") or [])}
        for op in operations
        if op.get("blockers")
    ]
    blockers: list[str] = []
    if packet.get("ok") is not True:
        blockers.append("awc_legacy_terminal_closeout_packet_not_ready")
        blockers.extend(str(item) for item in _as_sequence(packet.get("hard_blockers")) if str(item))
    if not audit.get("manifest_provided"):
        blockers.append("decision_manifest_required")
    if not operations:
        blockers.append("no_apply_operations")
    if operation_blockers:
        blockers.append("apply_operations_not_safe")
    ok = not blockers
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "awc_legacy_terminal_closeout_apply_contract_no_apply",
        "db": str(db),
        "closeout_packet_summary": decision_packet.compact_packet(packet),
        "operation_count": len(operations),
        "operations": operations,
        "operations_truncated": False,
        "operation_blockers": operation_blockers,
        "blockers": sorted(set(blockers)),
        "operator_contract": {
            "schema": "awc_legacy_terminal_closeout_apply_operator_contract_v1",
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "allow_apply_now": False,
            "backup_path": str(backup_path),
            "backup_command": f"mkdir -p {_q(backup_root)} && sqlite3 {_q(db)} \".backup {_q(backup_path)}\"",
            "verify_writer_quiescence_command": (
                f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                "--summary --max-items-output 8"
            ),
            "apply_command_template": (
                "python3 scripts/awc_legacy_terminal_closeout_apply.py "
                f"--db {_q(db)} --decision-manifest <reviewed-manifest.json> "
                f"--backup {_q(backup_path)} --writer-quiescent --confirm {CONFIRMATION_PHRASE}"
            ),
            "post_apply_verification_commands": [
                "python3 scripts/awc_hard_source_terminal_coverage_audit.py --summary --since-hours 720 --limit 500 --max-items-output 8",
                "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary --limit 100 --max-items-output 8",
            ],
        },
        "guards": _guards(),
        "next_boundary": (
            "run_awc_legacy_terminal_closeout_apply_after_backup_writer_quiescence_and_confirmation"
            if ok
            else "resolve_awc_legacy_terminal_closeout_apply_contract_blockers"
        ),
        "operations_preview": operations[:visible_limit],
        "operations_preview_truncated": len(operations) > visible_limit,
    }


def compact_contract(contract: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    operations = _as_sequence(contract.get("operations"))
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": str(contract.get("schema") or SCHEMA_VERSION) + "_summary",
        "source_schema": contract.get("schema"),
        "ok": bool(contract.get("ok")),
        "read_only": bool(contract.get("read_only")),
        "dry_run": bool(contract.get("dry_run")),
        "db": contract.get("db"),
        "operation_count": int(contract.get("operation_count") or 0),
        "operations": operations[:visible_limit],
        "operations_truncated": len(operations) > visible_limit,
        "operation_blockers": _as_sequence(contract.get("operation_blockers"))[:visible_limit],
        "blockers": _as_sequence(contract.get("blockers"))[:50],
        "operator_contract": dict(_as_mapping(contract.get("operator_contract"))),
        "closeout_packet_summary": dict(_as_mapping(contract.get("closeout_packet_summary"))),
        "guards": dict(_as_mapping(contract.get("guards"))),
        "next_boundary": contract.get("next_boundary"),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--decision-manifest", default="")
    parser.add_argument("--since-hours", type=float, default=24.0 * 90.0)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    contract = build_contract(
        args.db,
        decision_manifest=_load_json_file(args.decision_manifest),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        backup_dir=args.backup_dir,
    )
    payload: Mapping[str, Any] = (
        compact_contract(contract, max_items_output=int(args.max_items_output))
        if args.summary
        else contract
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if contract.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
