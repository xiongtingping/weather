from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import weather_fact_snapshot_writer


APPLY_RESULT_SCHEMA = "daily_review_lineage_repair_apply_result_v1"
CONTRACT_SCHEMA = "daily_review_lineage_repair_apply_contract_v1"
RISK_AUDIT_SCHEMA = "daily_review_lineage_repair_risk_audit_v1"
CONFIRMATION_PHRASE = "APPLY_DAILY_REVIEW_LINEAGE_REPAIR"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _file_state(path: Path | str | None) -> dict[str, Any]:
    if not path:
        return {"path": "", "exists": False, "size_bytes": None}
    resolved = Path(path).expanduser().resolve()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "size_bytes": resolved.stat().st_size if resolved.exists() else None,
    }


def _json_load_mapping(raw: Any) -> tuple[dict[str, Any], str]:
    if isinstance(raw, Mapping):
        return dict(raw), ""
    try:
        payload = json.loads(str(raw or "{}"))
    except Exception as exc:
        return {}, f"fact_json_parse_error:{exc.__class__.__name__}"
    if not isinstance(payload, Mapping):
        return {}, "fact_json_not_object"
    return dict(payload), ""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _value_missing(value: Any) -> bool:
    return value in (None, "", [], {})


def _candidate_paths(item: Mapping[str, Any]) -> dict[str, str]:
    fact_snapshot_id = _text(item.get("candidate_fact_snapshot_id"))
    paths = _as_mapping(item.get("expected_merge_fill")).get("paths")
    out: dict[str, str] = {}
    for row in _as_sequence(paths):
        row_map = _as_mapping(row)
        path = _text(row_map.get("json_path"))
        value = _text(row_map.get("value"))
        if path and value:
            out[path] = value
    if fact_snapshot_id:
        out.setdefault("fact_snapshot_id", fact_snapshot_id)
        out.setdefault("lineage_state.fact_snapshot_id", fact_snapshot_id)
    return out


def _validate_row_against_item(row: sqlite3.Row, item: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    target_table = _text(item.get("target_table"))
    if target_table and target_table != "event_fact_history":
        blockers.append("target_table_unsupported")
    row_keys = set(row.keys())
    if "slug" in row_keys and _text(row["slug"]) != _text(item.get("slug")):
        blockers.append("target_row_slug_mismatch")
    if "event_date" in row_keys and _text(row["event_date"])[:10] != _text(item.get("event_date"))[:10]:
        blockers.append("target_row_event_date_mismatch")
    return blockers


def _merge_fill_payload(payload: Mapping[str, Any], item: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    out = copy.deepcopy(dict(payload))
    operations: list[dict[str, Any]] = []
    blockers: list[str] = []
    paths = _candidate_paths(item)
    candidate_fact_snapshot_id = _text(paths.get("fact_snapshot_id"))
    if not candidate_fact_snapshot_id:
        blockers.append("candidate_fact_snapshot_id_missing")
        return out, operations, blockers

    existing_root = _text(out.get("fact_snapshot_id"))
    if existing_root and existing_root != candidate_fact_snapshot_id:
        blockers.append("fact_snapshot_id_conflict")
    elif not existing_root:
        out["fact_snapshot_id"] = candidate_fact_snapshot_id
        operations.append(
            {
                "json_path": "fact_snapshot_id",
                "action": "filled_missing",
                "value": candidate_fact_snapshot_id,
            }
        )
    else:
        operations.append(
            {
                "json_path": "fact_snapshot_id",
                "action": "already_matching",
                "value": candidate_fact_snapshot_id,
            }
        )

    lineage_state = out.get("lineage_state")
    if not isinstance(lineage_state, Mapping):
        lineage_state = {}
    lineage_out = copy.deepcopy(dict(lineage_state))
    existing_lineage = _text(lineage_out.get("fact_snapshot_id"))
    if existing_lineage and existing_lineage != candidate_fact_snapshot_id:
        blockers.append("lineage_state_fact_snapshot_id_conflict")
    elif not existing_lineage:
        lineage_out["fact_snapshot_id"] = candidate_fact_snapshot_id
        out["lineage_state"] = lineage_out
        operations.append(
            {
                "json_path": "lineage_state.fact_snapshot_id",
                "action": "filled_missing",
                "value": candidate_fact_snapshot_id,
            }
        )
    else:
        out["lineage_state"] = lineage_out
        operations.append(
            {
                "json_path": "lineage_state.fact_snapshot_id",
                "action": "already_matching",
                "value": candidate_fact_snapshot_id,
            }
        )

    return out, operations, sorted(set(blockers))


def _payload_path_value(payload: Mapping[str, Any], json_path: str) -> Any:
    if json_path == "fact_snapshot_id":
        return _as_mapping(payload).get("fact_snapshot_id")
    if json_path == "lineage_state.fact_snapshot_id":
        return _as_mapping(_as_mapping(payload).get("lineage_state")).get("fact_snapshot_id")
    return None


def _lineage_repair_diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for operation in operations:
        path = _text(operation.get("json_path"))
        if not path:
            continue
        action = _text(operation.get("action"))
        diffs.append(
            {
                "json_path": path,
                "action": action,
                "current_value": _payload_path_value(before, path),
                "candidate_value": operation.get("value"),
                "new_value": _payload_path_value(after, path),
                "overwrite_allowed": False,
                "would_write": action == "filled_missing",
            }
        )
    return diffs


def _lineage_repair_post_apply_prediction(
    repaired_payload: Mapping[str, Any],
    item: Mapping[str, Any],
    blockers: Sequence[str],
) -> dict[str, Any]:
    candidate_fact_snapshot_id = _text(item.get("candidate_fact_snapshot_id"))
    root_after = _text(_payload_path_value(repaired_payload, "fact_snapshot_id"))
    lineage_after = _text(_payload_path_value(repaired_payload, "lineage_state.fact_snapshot_id"))
    expected_removed = (
        not blockers
        and bool(candidate_fact_snapshot_id)
        and root_after == candidate_fact_snapshot_id
        and lineage_after == candidate_fact_snapshot_id
    )
    return {
        "candidate_fact_snapshot_id": candidate_fact_snapshot_id,
        "fact_snapshot_id_after_merge_fill": root_after,
        "lineage_state_fact_snapshot_id_after_merge_fill": lineage_after,
        "fact_snapshot_id_present_after_merge_fill": bool(root_after),
        "lineage_state_fact_snapshot_id_present_after_merge_fill": bool(lineage_after),
        "lineage_blocker_expected_removed": expected_removed,
        "batch_plan_lineage_repair_candidate_expected_removed": expected_removed,
    }


def preview_lineage_repair_contract(
    target: sqlite3.Connection,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Read-only audit of the exact merge-fill operations a contract would apply."""
    repair_items = [dict(_as_mapping(item)) for item in _as_sequence(contract.get("repair_items"))]
    schema_blockers: list[str] = []
    if contract.get("schema") != CONTRACT_SCHEMA:
        schema_blockers.append("invalid_contract_schema")

    target.row_factory = sqlite3.Row
    audited_items: list[dict[str, Any]] = []
    blockers: list[str] = list(schema_blockers)
    changed_count = 0
    expected_removed_count = 0
    already_satisfied_count = 0

    for item in repair_items:
        target_row_id = _text(item.get("target_row_id"))
        item_blockers: list[str] = []
        current_payload: dict[str, Any] = {}
        repaired_payload: dict[str, Any] = {}
        payload_loaded = False
        operations: list[dict[str, Any]] = []
        expected_diff: list[dict[str, Any]] = []
        row_context: dict[str, Any] = {}

        try:
            target_row_id_int = int(target_row_id)
        except ValueError:
            target_row_id_int = 0
        if target_row_id_int <= 0:
            item_blockers.append("target_row_id_invalid")
        else:
            row = target.execute(
                "SELECT * FROM event_fact_history WHERE id = ? LIMIT 1",
                (target_row_id_int,),
            ).fetchone()
            if row is None:
                item_blockers.append("target_row_not_found")
            else:
                row_context = {
                    "target_row_id": target_row_id_int,
                    "slug": row["slug"] if "slug" in row.keys() else "",
                    "event_date": row["event_date"] if "event_date" in row.keys() else "",
                    "fact_as_of": row["fact_as_of"] if "fact_as_of" in row.keys() else "",
                    "created_at": row["created_at"] if "created_at" in row.keys() else "",
                }
                item_blockers.extend(_validate_row_against_item(row, item))
                current_payload, parse_error = _json_load_mapping(row["fact_json"])
                if parse_error:
                    item_blockers.append(parse_error)
                else:
                    payload_loaded = True
                    repaired_payload, operations, merge_blockers = _merge_fill_payload(current_payload, item)
                    item_blockers.extend(merge_blockers)
                    expected_diff = _lineage_repair_diff(current_payload, repaired_payload, operations)

        item_blockers = sorted({_text(value) for value in item_blockers if _text(value)})
        changed = payload_loaded and _json_dumps(repaired_payload) != _json_dumps(current_payload)
        if changed:
            changed_count += 1
        if not changed and not item_blockers and operations:
            already_satisfied_count += 1
        post_apply_prediction = _lineage_repair_post_apply_prediction(repaired_payload, item, item_blockers)
        if post_apply_prediction["lineage_blocker_expected_removed"]:
            expected_removed_count += 1
        blockers.extend(item_blockers)
        audited_items.append(
            {
                "schema": "daily_review_lineage_repair_risk_audit_item_v1",
                "read_only": True,
                "repair_correlation_id": item.get("repair_correlation_id"),
                "target_table": item.get("target_table"),
                "target_row_id": target_row_id,
                "slug": item.get("slug"),
                "event_date": item.get("event_date"),
                "row_context": row_context,
                "candidate_fact_snapshot_id": item.get("candidate_fact_snapshot_id"),
                "current_values": {
                    "fact_snapshot_id": _payload_path_value(current_payload, "fact_snapshot_id"),
                    "lineage_state_fact_snapshot_id": _payload_path_value(
                        current_payload,
                        "lineage_state.fact_snapshot_id",
                    ),
                    "fact_snapshot_id_missing": _value_missing(
                        _payload_path_value(current_payload, "fact_snapshot_id")
                    ),
                    "lineage_state_fact_snapshot_id_missing": _value_missing(
                        _payload_path_value(current_payload, "lineage_state.fact_snapshot_id")
                    ),
                },
                "expected_diff": expected_diff,
                "would_change": changed,
                "no_change_already_satisfied": bool(not changed and not item_blockers and operations),
                "blockers": item_blockers,
                "post_apply_prediction": post_apply_prediction,
                "guards": {
                    "executes_sql": False,
                    "writes_archive_db": False,
                    "writes_event_fact_history": False,
                    "writes_daily_weather_reviews": False,
                    "historical_backfill": False,
                    "fetches_external_weather_sources": False,
                    "places_orders": False,
                    "restarts_launchd": False,
                    "changes_parameters": False,
                },
            }
        )

    unique_blockers = sorted({_text(value) for value in blockers if _text(value)})
    risk_level = "high" if unique_blockers else "low" if repair_items else "none"
    return {
        "schema": RISK_AUDIT_SCHEMA,
        "ok": not unique_blockers,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "lineage_repair_risk_audit_no_apply",
        "contract_schema": contract.get("schema"),
        "repair_item_count": len(repair_items),
        "audited_item_count": len(audited_items),
        "would_change_count": changed_count,
        "already_satisfied_count": already_satisfied_count,
        "lineage_blocker_expected_removed_count": expected_removed_count,
        "risk_level": risk_level,
        "blockers": unique_blockers,
        "items": audited_items,
        "post_apply_prediction_summary": {
            "lineage_blocker_expected_removed_count": expected_removed_count,
            "lineage_blocker_expected_remaining_count": max(len(repair_items) - expected_removed_count, 0),
        },
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "review_risk_audit_before_controlled_lineage_repair_apply"
            if repair_items
            else "no_lineage_repair_items"
        ),
    }


def _preflight_checks(
    contract: Mapping[str, Any],
    *,
    allow_apply: bool,
    confirmation_phrase: str,
    backup_path: Path | str | None,
    writer_quiescent: bool,
) -> list[dict[str, Any]]:
    repair_items = list(_as_sequence(contract.get("repair_items")))
    backup_state = _file_state(backup_path)
    db_path = _text(contract.get("db"))
    backup_ok = (
        not repair_items
        or (
            bool(backup_state.get("exists"))
            and int(backup_state.get("size_bytes") or 0) > 0
            and _text(backup_state.get("path")) != db_path
        )
    )
    return [
        {
            "name": "contract_schema",
            "status": "pass" if contract.get("schema") == CONTRACT_SCHEMA else "required",
            "hard_blocker": contract.get("schema") != CONTRACT_SCHEMA,
            "reason": "contract_schema_matches" if contract.get("schema") == CONTRACT_SCHEMA else "invalid_contract_schema",
        },
        {
            "name": "repair_items_present",
            "status": "pass" if repair_items else "not_required",
            "hard_blocker": False,
            "reason": "repair_items_present" if repair_items else "no_confirm_repair_items",
            "repair_item_count": len(repair_items),
        },
        {
            "name": "apply_flag",
            "status": "pass" if allow_apply else "required",
            "hard_blocker": bool(repair_items) and not allow_apply,
            "reason": "allow_apply_true" if allow_apply else "lineage_repair_apply_requires_explicit_apply_flag",
        },
        {
            "name": "confirmation_phrase",
            "status": "pass" if confirmation_phrase == CONFIRMATION_PHRASE else "required",
            "hard_blocker": bool(repair_items) and confirmation_phrase != CONFIRMATION_PHRASE,
            "reason": (
                "confirmation_phrase_matches"
                if confirmation_phrase == CONFIRMATION_PHRASE
                else "confirmation_phrase_missing_or_mismatched"
            ),
        },
        {
            "name": "fresh_archive_db_backup",
            "status": "pass" if backup_ok else "required",
            "hard_blocker": bool(repair_items) and not backup_ok,
            "reason": "backup_path_exists_and_is_not_db" if backup_ok else "backup_path_required_before_lineage_repair_apply",
            "backup_path": backup_state.get("path"),
            "backup_exists": backup_state.get("exists"),
            "backup_size_bytes": backup_state.get("size_bytes"),
        },
        {
            "name": "writer_quiescence",
            "status": "pass" if writer_quiescent else "required",
            "hard_blocker": bool(repair_items) and not writer_quiescent,
            "reason": "caller_asserted_writer_quiescence" if writer_quiescent else "requires_explicit_writer_quiescent_assertion",
        },
        {
            "name": "historical_backfill",
            "status": "blocked_by_design",
            "hard_blocker": False,
            "reason": "lineage_repair_apply_targets_contract_items_only",
        },
    ]


def _blocked_result(
    contract: Mapping[str, Any],
    *,
    preflight_checks: Sequence[Mapping[str, Any]],
    blockers: Sequence[str],
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": APPLY_RESULT_SCHEMA,
        "ok": False,
        "applied": False,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "lineage_repair_apply_blocked",
        "reason": reason,
        "contract_schema": contract.get("schema"),
        "repair_item_count": len(list(_as_sequence(contract.get("repair_items")))),
        "preflight_checks": [dict(_as_mapping(item)) for item in preflight_checks],
        "blockers": sorted({_text(item) for item in blockers if _text(item)}),
        "applied_items": [],
        "skipped_items": [],
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
    }


def apply_lineage_repair_contract(
    target: sqlite3.Connection,
    contract: Mapping[str, Any],
    *,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    backup_path: Path | str | None = None,
    writer_quiescent: bool = False,
) -> dict[str, Any]:
    preflight_checks = _preflight_checks(
        contract,
        allow_apply=allow_apply,
        confirmation_phrase=confirmation_phrase,
        backup_path=backup_path,
        writer_quiescent=writer_quiescent,
    )
    preflight_blockers = [str(item.get("name")) for item in preflight_checks if item.get("hard_blocker")]
    if preflight_blockers:
        return _blocked_result(
            contract,
            preflight_checks=preflight_checks,
            blockers=preflight_blockers,
            reason="preflight_blocked",
        )
    repair_items = [dict(_as_mapping(item)) for item in _as_sequence(contract.get("repair_items"))]
    if not repair_items:
        return {
            "schema": APPLY_RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "lineage_repair_apply_noop",
            "reason": "no_repair_items",
            "repair_item_count": 0,
            "preflight_checks": [dict(_as_mapping(item)) for item in preflight_checks],
            "blockers": [],
            "applied_items": [],
            "skipped_items": [],
            "guards": {
                "executes_sql": False,
                "writes_archive_db": False,
                "writes_event_fact_history": False,
                "writes_daily_weather_reviews": False,
                "historical_backfill": False,
                "fetches_external_weather_sources": False,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
            },
        }

    writer_result = weather_fact_snapshot_writer.apply_event_fact_history_merge_fill_repair_contract(
        target,
        contract,
        allow_apply=True,
        commit=True,
    )
    if not writer_result.get("ok"):
        return _blocked_result(
            contract,
            preflight_checks=preflight_checks,
            blockers=writer_result.get("blockers") or [writer_result.get("block_reason") or "writer_apply_blocked"],
            reason=str(writer_result.get("block_reason") or "writer_apply_blocked"),
        )

    applied_items = [dict(_as_mapping(item)) for item in _as_sequence(writer_result.get("applied_items"))]
    skipped_items = [dict(_as_mapping(item)) for item in _as_sequence(writer_result.get("skipped_items"))]
    wrote = bool(writer_result.get("applied"))
    return {
        "schema": APPLY_RESULT_SCHEMA,
        "ok": True,
        "applied": wrote,
        "read_only": not wrote,
        "dry_run": False,
        "execution_mode": "lineage_repair_apply_completed" if wrote else "lineage_repair_apply_already_satisfied",
        "reason": "applied" if wrote else "already_satisfied",
        "repair_item_count": len(repair_items),
        "applied_count": len(applied_items),
        "skipped_count": len(skipped_items),
        "write_owner": writer_result.get("write_owner") or weather_fact_snapshot_writer.WRITE_OWNER,
        "writer_result_schema": writer_result.get("schema"),
        "preflight_checks": [dict(_as_mapping(item)) for item in preflight_checks],
        "blockers": [],
        "applied_items": applied_items,
        "skipped_items": skipped_items,
        "guards": {
            "executes_sql": wrote,
            "writes_archive_db": wrote,
            "writes_event_fact_history": wrote,
            "writes_daily_weather_reviews": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
    }
