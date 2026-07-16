#!/usr/bin/env python3
"""Controlled parameter-change event transition contract/apply.

This entrypoint only appends rows to ``parameter_change_events``. It never
changes production parameters, restarts services, places orders, or backfills
history. The state transition itself is governed by an operator-reviewed
manifest and the state machine in ``parameter_change_governance.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parameter_change_governance import STATE_TRANSITIONS  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "parameter_change_event_transition_contract_v1"
CLI_RESULT_SCHEMA = "parameter_change_event_transition_apply_result_v1"
MANIFEST_SCHEMA = "parameter_change_event_transition_manifest_v1"
EVENT_SCHEMA = "parameter_change_event_transition_v1"
CONFIRMATION_PHRASE = "APPLY_PARAMETER_CHANGE_EVENT_TRANSITIONS"
REQUIRED_COLUMNS = ("id", "calibration_candidate_id", "state", "changed_by", "change_json", "created_at")


def _guards(*, writes_archive_db: bool = False, writes_parameter_change_events: bool = False) -> dict[str, bool]:
    return {
        "executes_sql": bool(writes_archive_db),
        "writes_archive_db": bool(writes_archive_db),
        "writes_parameter_change_events": bool(writes_parameter_change_events),
        "changes_parameters": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _connect(db: Path) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_write(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("PRAGMA table_info(parameter_change_events)").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["name"] or "") for row in rows}


def _schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    columns = _table_columns(conn)
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    return {
        "schema": "parameter_change_event_transition_schema_status_v1",
        "ok": bool(columns and not missing),
        "table": "parameter_change_events",
        "exists": bool(columns),
        "columns": sorted(columns),
        "required_columns": list(REQUIRED_COLUMNS),
        "missing_columns": missing,
    }


def _latest_states(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, calibration_candidate_id, state, changed_by, change_json, created_at
        FROM parameter_change_events
        WHERE calibration_candidate_id IS NOT NULL
          AND trim(CAST(calibration_candidate_id AS TEXT)) <> ''
        ORDER BY id ASC
        """
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidate_id = _first_text(row["calibration_candidate_id"])
        if not candidate_id:
            continue
        latest[candidate_id] = {
            "id": row["id"],
            "calibration_candidate_id": candidate_id,
            "state": _first_text(row["state"]),
            "changed_by": _first_text(row["changed_by"]),
            "created_at": _first_text(row["created_at"]),
            "change_json": _first_text(row["change_json"]),
        }
    return latest


def _file_state(path: Path | None, *, db: Path) -> dict[str, Any]:
    if path is None:
        return {"path": "", "exists": False, "size_bytes": None, "is_db_path": False}
    resolved = Path(path).expanduser().resolve()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "size_bytes": resolved.stat().st_size if resolved.exists() else None,
        "is_db_path": str(resolved) == str(db),
    }


def _load_manifest(path: Path | str | None) -> dict[str, Any]:
    if not path:
        return {}
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _manifest_transitions(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = manifest.get("transitions")
    if raw is None:
        raw = manifest.get("items")
    return [dict(_as_mapping(item)) for item in _as_sequence(raw) if isinstance(item, Mapping)]


def _measurement_windows(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    windows = _as_mapping(evidence.get("windows"))
    if not windows:
        windows = _as_mapping(evidence.get("measurement_windows"))
    return windows


def _transition_blockers(item: Mapping[str, Any], current_state: str) -> list[str]:
    candidate_id = _first_text(item.get("calibration_candidate_id"))
    from_state = _first_text(item.get("from_state"))
    to_state = _first_text(item.get("to_state"), item.get("state"))
    blockers: list[str] = []
    if not candidate_id:
        blockers.append("calibration_candidate_id_missing")
    if not current_state:
        blockers.append("current_parameter_change_event_missing")
    if not from_state:
        blockers.append("from_state_missing")
    if not to_state:
        blockers.append("to_state_missing")
    if from_state and current_state and from_state != current_state:
        blockers.append("from_state_does_not_match_current_state")
    allowed = STATE_TRANSITIONS.get(from_state, ())
    if from_state and to_state and to_state not in allowed:
        blockers.append("transition_not_allowed_by_state_machine")
    if to_state == "measured":
        evidence = _as_mapping(item.get("measurement_evidence"))
        windows = _measurement_windows(evidence)
        for key in ("7d", "30d", "90d"):
            if key not in windows:
                blockers.append(f"measurement_window_missing:{key}")
        if not _first_text(evidence.get("primary_metric"), item.get("primary_metric")):
            blockers.append("measurement_primary_metric_missing")
    if to_state == "kept":
        evidence = _as_mapping(item.get("decision_evidence"))
        if not _first_text(evidence.get("keep_reason"), item.get("keep_reason")):
            blockers.append("keep_reason_missing")
        if not _first_text(evidence.get("measurement_summary"), item.get("measurement_summary")):
            blockers.append("measurement_summary_missing")
    if to_state == "rolled_back":
        evidence = _as_mapping(item.get("decision_evidence"))
        if not _first_text(evidence.get("rollback_reason"), item.get("rollback_reason")):
            blockers.append("rollback_reason_missing")
        if not _as_mapping(evidence.get("rollback_plan")) and not _as_mapping(item.get("rollback_plan")):
            blockers.append("rollback_plan_missing")
    return blockers


def _operation_from_item(item: Mapping[str, Any], current: Mapping[str, Any], *, created_at: str) -> dict[str, Any]:
    to_state = _first_text(item.get("to_state"), item.get("state"))
    from_state = _first_text(item.get("from_state"))
    changed_by = _first_text(item.get("changed_by"), "parameter_change_event_transition_apply")
    candidate_id = _first_text(item.get("calibration_candidate_id"))
    transition_payload = {
        "schema": EVENT_SCHEMA,
        "calibration_candidate_id": candidate_id,
        "from_state": from_state,
        "to_state": to_state,
        "previous_event_id": current.get("id"),
        "previous_event_created_at": current.get("created_at"),
        "changed_by": changed_by,
        "measurement_evidence": dict(_as_mapping(item.get("measurement_evidence"))),
        "decision_evidence": dict(_as_mapping(item.get("decision_evidence"))),
        "operator_note": _first_text(item.get("operator_note")),
        "manifest_item": dict(item),
    }
    return {
        "table": "parameter_change_events",
        "operation": "append_parameter_change_transition_event",
        "calibration_candidate_id": candidate_id,
        "from_state": from_state,
        "to_state": to_state,
        "insert_row": {
            "calibration_candidate_id": candidate_id,
            "state": to_state,
            "changed_by": changed_by,
            "change_json": json.dumps(transition_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "created_at": created_at,
        },
        "where_guard": {
            "current_state_must_equal": from_state,
            "append_only": True,
        },
    }


def build_contract(
    db_path: Path | str = DEFAULT_DB,
    *,
    manifest: Mapping[str, Any] | None = None,
    backup_path: Path | None = None,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    writer_quiescent: bool = False,
    created_at: str = "",
    max_items_output: int = 20,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    manifest_map = dict(manifest or {})
    created = _first_text(created_at, "operator_supplied_at_apply_time")
    visible_limit = max(0, int(max_items_output))
    blockers: list[str] = []
    operations: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    if manifest_map.get("schema") and manifest_map.get("schema") != MANIFEST_SCHEMA:
        blockers.append("manifest_schema_unrecognized")
    transitions = _manifest_transitions(manifest_map)
    if not transitions:
        blockers.append("transition_manifest_empty_or_missing")
    if not db.exists():
        blockers.append("archive_db_missing")
        schema_status = {
            "schema": "parameter_change_event_transition_schema_status_v1",
            "ok": False,
            "table": "parameter_change_events",
            "exists": False,
            "missing_columns": list(REQUIRED_COLUMNS),
        }
        latest_states: dict[str, dict[str, Any]] = {}
    else:
        with _connect(db) as conn:
            schema_status = _schema_status(conn)
            if not schema_status.get("ok"):
                blockers.append("parameter_change_events_schema_not_ready")
                latest_states = {}
            else:
                latest_states = _latest_states(conn)
                for item in transitions:
                    candidate_id = _first_text(item.get("calibration_candidate_id"))
                    current = latest_states.get(candidate_id, {})
                    item_blockers = _transition_blockers(item, _first_text(current.get("state")))
                    if item_blockers:
                        blocked_items.append(
                            {
                                "calibration_candidate_id": candidate_id,
                                "from_state": item.get("from_state"),
                                "to_state": item.get("to_state") or item.get("state"),
                                "current_state": current.get("state"),
                                "blockers": item_blockers,
                            }
                        )
                    else:
                        operations.append(_operation_from_item(item, current, created_at=created))
    if blocked_items:
        blockers.append("transition_items_blocked")
    backup_state = _file_state(backup_path, db=db)
    backup_ok = (
        bool(backup_state.get("exists"))
        and int(backup_state.get("size_bytes") or 0) > 0
        and not bool(backup_state.get("is_db_path"))
    )
    preflight_checks = [
        {
            "name": "apply_flag",
            "status": "pass" if allow_apply else "required",
            "hard_blocker": bool(operations and not allow_apply),
            "reason": "allow_apply_true" if allow_apply else "requires_explicit_apply_flag",
        },
        {
            "name": "confirmation_phrase",
            "status": "pass" if confirmation_phrase == CONFIRMATION_PHRASE else "required",
            "hard_blocker": bool(operations and confirmation_phrase != CONFIRMATION_PHRASE),
            "reason": "confirmation_phrase_matches" if confirmation_phrase == CONFIRMATION_PHRASE else "confirmation_phrase_missing_or_mismatched",
        },
        {
            "name": "fresh_archive_db_backup",
            "status": "pass" if backup_ok else "required",
            "hard_blocker": bool(operations and not backup_ok),
            "reason": "backup_path_exists_and_is_not_db" if backup_ok else "backup_path_required_before_parameter_event_transition_apply",
            "backup_path": backup_state.get("path"),
            "backup_exists": backup_state.get("exists"),
            "backup_size_bytes": backup_state.get("size_bytes"),
        },
        {
            "name": "writer_quiescence",
            "status": "pass" if writer_quiescent else "required",
            "hard_blocker": bool(operations and not writer_quiescent),
            "reason": "caller_asserted_writer_quiescence" if writer_quiescent else "requires_explicit_writer_quiescent_assertion",
        },
    ]
    apply_blockers = [str(item["name"]) for item in preflight_checks if item.get("hard_blocker")]
    ok = not blockers
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "parameter_change_event_transition_contract_no_apply",
        "db": str(db),
        "manifest_schema": manifest_map.get("schema"),
        "schema_status": schema_status,
        "operation_count": len(operations),
        "operations": operations[:visible_limit],
        "operations_truncated": len(operations) > visible_limit,
        "blocked_item_count": len(blocked_items),
        "blocked_items": blocked_items[:visible_limit],
        "blocked_items_truncated": len(blocked_items) > visible_limit,
        "blockers": sorted(set(blockers)),
        "apply_blockers": apply_blockers,
        "preflight_checks": preflight_checks,
        "latest_state_count": len(latest_states),
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "guards": _guards(),
        "boundary": {
            "append_only_parameter_change_events": True,
            "changes_production_parameters": False,
            "requires_manifest": True,
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "allows_state_machine_skip": False,
        },
        "next_boundary": (
            "run_controlled_parameter_change_event_transition_apply"
            if ok and operations and not apply_blockers
            else "no_parameter_change_event_transition_apply_required"
            if ok and not operations
            else "resolve_parameter_change_event_transition_blockers"
        ),
    }


def apply_transitions(
    db_path: Path | str = DEFAULT_DB,
    *,
    manifest: Mapping[str, Any],
    backup_path: Path | None,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    writer_quiescent: bool = False,
    created_at: str = "",
    max_items_output: int = 20,
) -> dict[str, Any]:
    contract = build_contract(
        db_path,
        manifest=manifest,
        backup_path=backup_path,
        allow_apply=allow_apply,
        confirmation_phrase=confirmation_phrase,
        writer_quiescent=writer_quiescent,
        created_at=created_at,
        max_items_output=max(max_items_output, 1000000),
    )
    if contract.get("blockers") or contract.get("apply_blockers"):
        return {
            "schema": CLI_RESULT_SCHEMA,
            "ok": False,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "parameter_change_event_transition_apply_blocked",
            "contract": contract,
            "guards": _guards(),
        }
    operations = [dict(_as_mapping(item)) for item in _as_sequence(contract.get("operations")) if isinstance(item, Mapping)]
    if not operations:
        return {
            "schema": CLI_RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "parameter_change_event_transition_apply_noop",
            "contract": contract,
            "guards": _guards(),
        }
    db = Path(db_path).expanduser().resolve()
    applied: list[dict[str, Any]] = []
    try:
        with _connect_write(db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for operation in operations:
                row = _as_mapping(operation.get("insert_row"))
                current = _latest_states(conn).get(str(operation.get("calibration_candidate_id") or ""), {})
                if _first_text(current.get("state")) != _first_text(operation.get("from_state")):
                    raise RuntimeError("current_state_changed_before_apply")
                conn.execute(
                    """
                    INSERT INTO parameter_change_events(
                        calibration_candidate_id, state, changed_by, change_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("calibration_candidate_id"),
                        row.get("state"),
                        row.get("changed_by"),
                        row.get("change_json"),
                        row.get("created_at"),
                    ),
                )
                applied.append(
                    {
                        "calibration_candidate_id": operation.get("calibration_candidate_id"),
                        "from_state": operation.get("from_state"),
                        "to_state": operation.get("to_state"),
                    }
                )
            conn.commit()
    except Exception as exc:
        return {
            "schema": CLI_RESULT_SCHEMA,
            "ok": False,
            "applied": bool(applied),
            "read_only": False,
            "dry_run": False,
            "execution_mode": "parameter_change_event_transition_apply_failed",
            "contract": contract,
            "applied_transitions": applied,
            "error": f"{exc.__class__.__name__}: {exc}",
            "guards": _guards(writes_archive_db=bool(applied), writes_parameter_change_events=bool(applied)),
        }
    return {
        "schema": CLI_RESULT_SCHEMA,
        "ok": True,
        "applied": True,
        "read_only": False,
        "dry_run": False,
        "execution_mode": "parameter_change_event_transition_apply_completed",
        "contract": contract,
        "applied_transition_count": len(applied),
        "applied_transitions": applied[:max(0, int(max_items_output))],
        "applied_transitions_truncated": len(applied) > max(0, int(max_items_output)),
        "guards": _guards(writes_archive_db=True, writes_parameter_change_events=True),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="", help=f"Required exact phrase for --apply: {CONFIRMATION_PHRASE}")
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--created-at", default="")
    parser.add_argument("--max-items-output", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = _load_manifest(args.manifest)
    if not args.apply:
        contract = build_contract(
            args.db,
            manifest=manifest,
            backup_path=args.backup,
            allow_apply=False,
            confirmation_phrase=str(args.confirm or ""),
            writer_quiescent=bool(args.writer_quiescent),
            created_at=str(args.created_at or ""),
            max_items_output=int(args.max_items_output),
        )
        print(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0 if contract.get("ok") else 2
    result = apply_transitions(
        args.db,
        manifest=manifest,
        backup_path=args.backup,
        allow_apply=True,
        confirmation_phrase=str(args.confirm or ""),
        writer_quiescent=bool(args.writer_quiescent),
        created_at=str(args.created_at or ""),
        max_items_output=int(args.max_items_output),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
