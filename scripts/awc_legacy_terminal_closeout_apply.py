#!/usr/bin/env python3
"""Controlled apply for reviewed AWC legacy terminal closeout operations."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import awc_hard_source_signal_ledger_writer as awc_signal_writer  # noqa: E402
import signal_to_order_ledger_writer as signal_timing_writer  # noqa: E402
from scripts import awc_legacy_terminal_closeout_apply_contract as apply_contract  # noqa: E402
from scripts import controlled_apply_maintenance_window_packet as maintenance_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "awc_legacy_terminal_closeout_apply_v1"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _guards(*, applying: bool) -> dict[str, bool]:
    return {
        "writes_archive_db": bool(applying),
        "writes_awc_hard_source_signals": bool(applying),
        "writes_signal_to_order_timing": bool(applying),
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "executes_sql_writes": bool(applying),
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _backup_status(path: str | Path | None) -> dict[str, Any]:
    backup = Path(path or "").expanduser() if path else Path("")
    exists = bool(path) and backup.exists() and backup.is_file()
    return {
        "path": str(backup) if path else "",
        "exists": exists,
        "size_bytes": int(backup.stat().st_size) if exists else 0,
    }


def _writer_quiescence_status(
    db_path: Path,
    *,
    writer_quiescent: bool,
    maintenance_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not writer_quiescent:
        return {
            "checked": False,
            "writer_quiescent": False,
            "ok": False,
            "source": "writer_quiescent_flag_not_provided",
        }
    if maintenance_report is not None:
        report = dict(maintenance_report)
    else:
        report = maintenance_packet.build_maintenance_window_packet(db_path, max_items_output=8)
    return {
        "checked": True,
        "writer_quiescent": bool(report.get("writer_quiescent")),
        "ok": bool(report.get("ok") and report.get("writer_quiescent")),
        "source": "controlled_apply_maintenance_window_packet",
        "blocker_count": int(_as_mapping(report.get("counts")).get("writer_quiescence_blocker_count") or 0),
        "required_stop_labels": list(report.get("required_stop_labels") or []),
    }


def _preflight(
    contract: Mapping[str, Any],
    *,
    confirm: str,
    backup: str | Path | None,
    writer_quiescent: bool,
    maintenance_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    blockers: list[str] = []
    operator_contract = _as_mapping(contract.get("operator_contract"))
    backup = _backup_status(backup)
    writer = _writer_quiescence_status(
        Path(str(contract.get("db") or DEFAULT_DB)).expanduser(),
        writer_quiescent=bool(writer_quiescent),
        maintenance_report=maintenance_report,
    )
    if contract.get("ok") is not True:
        blockers.append("apply_contract_not_ready")
    if str(confirm or "") != apply_contract.CONFIRMATION_PHRASE:
        blockers.append("confirmation_phrase_missing_or_invalid")
    if not backup.get("exists"):
        blockers.append("backup_file_missing")
    elif int(backup.get("size_bytes") or 0) <= 0:
        blockers.append("backup_file_empty")
    if operator_contract.get("requires_writer_quiescence") is not True:
        blockers.append("apply_contract_writer_quiescence_not_required")
    if not writer.get("ok"):
        blockers.append("writer_quiescence_not_satisfied")
    if int(contract.get("operation_count") or 0) <= 0:
        blockers.append("no_apply_operations")
    return {
        "ok": not blockers,
        "blockers": sorted(set(blockers)),
        "backup": backup,
        "writer_quiescence": writer,
        "confirmation_phrase_ok": str(confirm or "") == apply_contract.CONFIRMATION_PHRASE,
    }


def _apply_operations(
    db_path: Path,
    operations: Sequence[Mapping[str, Any]],
    *,
    applied_at: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            for raw in operations:
                op = _as_mapping(raw)
                payload = _as_mapping(op.get("payload"))
                if not payload:
                    raise ValueError(f"operation_payload_missing:{op.get('signal_id')}")
                awc_result = awc_signal_writer.upsert_awc_hard_source_signal(
                    conn,
                    payload,
                    created_at=applied_at,
                )
                timing_result = signal_timing_writer.upsert_signal_to_order_timing(
                    conn,
                    payload,
                    created_at=applied_at,
                )
                if not awc_result.get("ok") or not timing_result.get("ok"):
                    raise RuntimeError(
                        "awc_legacy_terminal_closeout_writer_failed:"
                        f"{op.get('signal_id')}:{awc_result.get('status')}:{timing_result.get('status')}"
                    )
                rows.append(
                    {
                        "signal_id": op.get("signal_id"),
                        "terminal_state": op.get("terminal_state"),
                        "awc_hard_source_signal": awc_result,
                        "signal_to_order_timing": timing_result,
                    }
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "applied_count": len(rows),
        "items": rows,
    }


def build_apply_report(
    db_path: Path | str = DEFAULT_DB,
    *,
    decision_manifest: Mapping[str, Any] | None = None,
    decision_manifest_path: str | Path | None = None,
    backup: str | Path | None = None,
    writer_quiescent: bool = False,
    confirm: str = "",
    since_hours: float = 24.0 * 90.0,
    limit: int = 1000,
    max_items_output: int = 20,
    maintenance_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    manifest = dict(decision_manifest or _load_json(decision_manifest_path))
    contract = apply_contract.build_contract(
        db,
        decision_manifest=manifest,
        since_hours=float(since_hours),
        limit=int(limit),
        max_items_output=int(max_items_output),
    )
    preflight = _preflight(
        contract,
        confirm=str(confirm or ""),
        backup=backup,
        writer_quiescent=bool(writer_quiescent),
        maintenance_report=maintenance_report,
    )
    if not preflight.get("ok"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "applied": False,
            "execution_mode": "awc_legacy_terminal_closeout_apply_blocked",
            "db": str(db),
            "decision_manifest_path": str(decision_manifest_path or ""),
            "contract_summary": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
            "preflight": preflight,
            "applied_count": 0,
            "items": [],
            "blockers": list(preflight.get("blockers") or []),
            "guards": _guards(applying=False),
            "next_boundary": "resolve_awc_legacy_terminal_closeout_apply_preflight_before_retry",
        }
    applied_at = _now_utc()
    result = _apply_operations(
        db,
        [_as_mapping(item) for item in _as_sequence(contract.get("operations"))],
        applied_at=applied_at,
    )
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "applied": True,
        "execution_mode": "awc_legacy_terminal_closeout_apply_controlled",
        "db": str(db),
        "decision_manifest_path": str(decision_manifest_path or ""),
        "applied_at": applied_at,
        "contract_summary": apply_contract.compact_contract(contract, max_items_output=int(max_items_output)),
        "preflight": preflight,
        "applied_count": int(result.get("applied_count") or 0),
        "items": list(result.get("items") or [])[:visible_limit],
        "items_truncated": int(result.get("applied_count") or 0) > visible_limit,
        "blockers": [],
        "guards": _guards(applying=True),
        "next_boundary": "run_awc_terminal_coverage_and_joint_gate_post_apply_verification",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--decision-manifest", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--since-hours", type=float, default=24.0 * 90.0)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-items-output", type=int, default=8)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_apply_report(
        args.db,
        decision_manifest_path=args.decision_manifest,
        backup=args.backup,
        writer_quiescent=bool(args.writer_quiescent),
        confirm=str(args.confirm or ""),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
