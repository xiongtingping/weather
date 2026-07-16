#!/usr/bin/env python3
"""Read-only decision packet for duplicate fact_snapshot_id payloads.

This packet classifies event_fact_history rows whose fact_json reused the same
fact_snapshot_id across multiple physical rows. It chooses a canonical row per
duplicate group and proposes new deterministic fact_snapshot_id values for the
non-canonical rows. It never writes archive.db.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lineage_identity_runtime import build_fact_snapshot_id, canonical_json  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "production_identity_duplicate_fact_snapshot_packet_v1"
MANIFEST_SCHEMA_VERSION = "production_identity_duplicate_fact_snapshot_decision_manifest_v1"
MANIFEST_AUDIT_SCHEMA_VERSION = "production_identity_duplicate_fact_snapshot_decision_manifest_audit_v1"
ALLOWED_DECISIONS = [
    "keep_existing_fact_snapshot_id",
    "regenerate_fact_snapshot_id_required",
    "manual_review_required",
    "exclude_legacy_row_from_identity_merge_fill",
]
LATEST_ANCHOR_POLICIES = [
    "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor",
    "repoint_latest_to_natural_fact_snapshot_owner_after_duplicate_apply",
    "manual_review_required",
]


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _connect(db: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_loads_map(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


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


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def _source_event_ids(payload: Mapping[str, Any]) -> list[str]:
    values = payload.get("source_event_ids")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        return [_text(item) for item in values if _text(item)]
    trace = payload.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        trace_values = trace.get("source_event_ids")
        if isinstance(trace_values, Sequence) and not isinstance(trace_values, (str, bytes, bytearray)):
            return [_text(item) for item in trace_values if _text(item)]
    return []


def _latest_fact_ids(conn: sqlite3.Connection) -> set[int]:
    try:
        rows = conn.execute("SELECT fact_id FROM event_fact_latest").fetchall()
    except sqlite3.Error:
        return set()
    return {int(row["fact_id"]) for row in rows if str(row["fact_id"] or "").isdigit()}


def _event_fact_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for row in conn.execute(
        """
        SELECT id, slug, event_date, fact_as_of, fact_json
        FROM event_fact_history
        ORDER BY id
        """
    ).fetchall():
        payload = _json_loads_map(row["fact_json"])
        fact_snapshot_id = _text(payload.get("fact_snapshot_id"))
        if not fact_snapshot_id:
            continue
        rows.append(
            {
                "id": int(row["id"]),
                "slug": _text(row["slug"]),
                "event_date": _text(row["event_date"])[:10],
                "fact_as_of": _text(row["fact_as_of"]),
                "fact_snapshot_id": fact_snapshot_id,
                "posterior_run_id": _text(payload.get("posterior_run_id")),
                "payload_hash": _payload_hash(payload),
                "source_event_ids": _source_event_ids(payload),
                "payload_schema": _text(
                    payload.get("fact_payload_schema")
                    or payload.get("posterior_runtime_schema")
                    or payload.get("amsc_observation_bundle_schema")
                    or payload.get("schema")
                ),
            }
        )
    return rows


def _choose_canonical(rows: list[dict[str, Any]], latest_ids: set[int]) -> tuple[int, str, list[str]]:
    natural_owners = [
        row
        for row in rows
        if _proposed_new_fact_snapshot_id(row) == str(row.get("fact_snapshot_id") or "")
    ]
    latest_in_group = [row for row in rows if int(row["id"]) in latest_ids]
    blockers: list[str] = []
    if len(natural_owners) == 1:
        natural_owner_id = int(natural_owners[0]["id"])
        if latest_in_group and all(int(row["id"]) != natural_owner_id for row in latest_in_group):
            blockers.append("latest_row_is_not_natural_fact_snapshot_owner")
        return natural_owner_id, "natural_key_regenerates_existing_fact_snapshot_id", blockers
    if len(natural_owners) > 1:
        blockers.append("multiple_rows_regenerate_existing_fact_snapshot_id")
        return int(max(natural_owners, key=lambda item: int(item["id"]))["id"]), "highest_natural_owner_row_after_multiple_natural_owners", blockers
    if len(latest_in_group) == 1:
        return int(latest_in_group[0]["id"]), "event_fact_latest_references_row", blockers
    if len(latest_in_group) > 1:
        blockers.append("duplicate_group_has_multiple_latest_rows")
        return int(max(latest_in_group, key=lambda item: int(item["id"]))["id"]), "highest_latest_row_id_after_multiple_latest_rows", blockers
    return int(max(rows, key=lambda item: (str(item.get("fact_as_of") or ""), int(item["id"])))["id"]), "highest_fact_as_of_then_id", blockers


def _proposed_new_fact_snapshot_id(row: Mapping[str, Any]) -> str:
    return build_fact_snapshot_id(
        slug=_text(row.get("slug")),
        event_date=_text(row.get("event_date")),
        fact_as_of=_text(row.get("fact_as_of")),
        source_event_ids=list(row.get("source_event_ids") or []),
    )


def _decision_rows_by_row_id(manifest: Mapping[str, Any]) -> tuple[dict[int, dict[str, Any]], list[int], list[str]]:
    rows_by_id: dict[int, dict[str, Any]] = {}
    duplicate_ids: list[int] = []
    malformed_ids: list[str] = []
    for row in _as_sequence(manifest.get("decisions")):
        row_map = _as_mapping(row)
        raw_row_id = row_map.get("row_id")
        try:
            row_id = int(raw_row_id)
        except (TypeError, ValueError):
            malformed_ids.append(str(raw_row_id or "missing_row_id"))
            continue
        if row_id in rows_by_id:
            duplicate_ids.append(row_id)
            continue
        rows_by_id[row_id] = row_map
    return rows_by_id, sorted(set(duplicate_ids)), sorted(set(malformed_ids))


def audit_decision_manifest(
    expected_decisions: Sequence[Mapping[str, Any]],
    raw_blockers: Sequence[str],
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_map = _as_mapping(manifest)
    manifest_provided = bool(manifest_map)
    rows_by_id, duplicate_row_ids, malformed_row_ids = _decision_rows_by_row_id(manifest_map)
    expected_by_row_id = {
        int(row["row_id"]): dict(row)
        for row in expected_decisions
        if str(row.get("row_id") or "").isdigit()
    }
    unexpected_row_ids = sorted(row_id for row_id in rows_by_id if row_id not in expected_by_row_id)
    missing_row_ids: list[int] = []
    invalid_row_ids: list[int] = []
    mismatch_row_ids: list[int] = []
    pending_row_ids: list[int] = []
    ready_row_ids: list[int] = []
    blocker_row_ids: list[int] = []
    items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    for row_id, expected in sorted(expected_by_row_id.items()):
        manifest_row = rows_by_id.get(row_id)
        expected_decision = _text(expected.get("decision"))
        decision = _text(_as_mapping(manifest_row).get("decision")) if manifest_row else ""
        reason = _text(_as_mapping(manifest_row).get("reason")) if manifest_row else ""
        if not manifest_provided or manifest_row is None:
            state = "decision_missing"
            decision = "missing"
            missing_row_ids.append(row_id)
            blocks_current_apply = True
        elif decision not in ALLOWED_DECISIONS:
            state = "invalid_decision"
            invalid_row_ids.append(row_id)
            blocks_current_apply = True
        elif decision in {"manual_review_required", "exclude_legacy_row_from_identity_merge_fill"}:
            state = "pending_operator_resolution"
            pending_row_ids.append(row_id)
            blocks_current_apply = True
        elif decision != expected_decision:
            state = "decision_mismatch_expected_action"
            mismatch_row_ids.append(row_id)
            blocks_current_apply = True
        elif expected.get("blockers"):
            state = "row_blocker_requires_manual_review"
            blocker_row_ids.append(row_id)
            blocks_current_apply = True
        else:
            state = "ready_for_duplicate_resolution_apply"
            ready_row_ids.append(row_id)
            blocks_current_apply = False
        counts[decision] += 1
        items.append(
            {
                "row_id": row_id,
                "decision": decision,
                "expected_decision": expected_decision,
                "state": state,
                "reason": reason,
                "current_fact_snapshot_id": expected.get("current_fact_snapshot_id"),
                "proposed_fact_snapshot_id": expected.get("proposed_fact_snapshot_id"),
                "requires_payload_update": bool(expected.get("requires_payload_update")),
                "requires_table_column_update": bool(expected.get("requires_table_column_update")),
                "blocks_current_apply": bool(blocks_current_apply),
                "blockers": list(expected.get("blockers") or []),
            }
        )

    latest_anchor_policy = _text(manifest_map.get("latest_anchor_policy"))
    latest_anchor_policy_required = "latest_row_is_not_natural_fact_snapshot_owner" in set(raw_blockers or [])
    latest_anchor_policy_ready = True
    latest_anchor_policy_blocker = ""
    if latest_anchor_policy_required:
        if not latest_anchor_policy:
            latest_anchor_policy_ready = False
            latest_anchor_policy_blocker = "latest_anchor_policy_missing"
        elif latest_anchor_policy not in LATEST_ANCHOR_POLICIES:
            latest_anchor_policy_ready = False
            latest_anchor_policy_blocker = "latest_anchor_policy_invalid"
        elif latest_anchor_policy == "manual_review_required":
            latest_anchor_policy_ready = False
            latest_anchor_policy_blocker = "latest_anchor_policy_manual_review_required"

    hard_blockers: list[str] = []
    if expected_by_row_id and not manifest_provided:
        hard_blockers.append("duplicate_fact_snapshot_decision_manifest_missing")
    if manifest_provided and manifest_map.get("schema") != MANIFEST_SCHEMA_VERSION:
        hard_blockers.append("duplicate_fact_snapshot_decision_manifest_schema_invalid")
    if missing_row_ids:
        hard_blockers.append("duplicate_fact_snapshot_decision_missing")
    if invalid_row_ids or duplicate_row_ids or unexpected_row_ids or malformed_row_ids:
        hard_blockers.append("duplicate_fact_snapshot_decision_manifest_invalid")
    if mismatch_row_ids:
        hard_blockers.append("duplicate_fact_snapshot_decision_mismatch")
    if pending_row_ids:
        hard_blockers.append("duplicate_fact_snapshot_decision_pending")
    if blocker_row_ids:
        hard_blockers.append("duplicate_fact_snapshot_row_blocker")
    if latest_anchor_policy_blocker:
        hard_blockers.append(latest_anchor_policy_blocker)

    return {
        "schema": MANIFEST_AUDIT_SCHEMA_VERSION,
        "read_only": True,
        "execution_mode": "production_identity_duplicate_fact_snapshot_decision_manifest_read_only_audit",
        "manifest_schema": manifest_map.get("schema"),
        "manifest_provided": manifest_provided,
        "expected_decision_count": len(expected_by_row_id),
        "decision_count": len(rows_by_id),
        "decision_counts": dict(sorted(counts.items())),
        "ready_decision_count": len(ready_row_ids),
        "missing_decision_count": len(missing_row_ids),
        "invalid_decision_count": len(invalid_row_ids),
        "duplicate_decision_count": len(duplicate_row_ids),
        "unexpected_decision_count": len(unexpected_row_ids),
        "malformed_decision_count": len(malformed_row_ids),
        "mismatch_decision_count": len(mismatch_row_ids),
        "pending_resolution_count": len(pending_row_ids),
        "row_blocker_count": len(blocker_row_ids),
        "latest_anchor_policy_required": latest_anchor_policy_required,
        "latest_anchor_policy": latest_anchor_policy,
        "latest_anchor_policy_ready": latest_anchor_policy_ready,
        "allowed_latest_anchor_policies": list(LATEST_ANCHOR_POLICIES),
        "all_rows_ready_for_duplicate_resolution_apply": bool(expected_by_row_id) and len(ready_row_ids) == len(expected_by_row_id),
        "blocking_for_current_apply": bool(hard_blockers),
        "hard_blockers": hard_blockers,
        "ready_row_ids": ready_row_ids,
        "missing_row_ids": missing_row_ids,
        "invalid_row_ids": invalid_row_ids,
        "duplicate_row_ids": duplicate_row_ids,
        "unexpected_row_ids": unexpected_row_ids,
        "malformed_row_ids": malformed_row_ids,
        "mismatch_row_ids": mismatch_row_ids,
        "pending_row_ids": pending_row_ids,
        "row_blocker_ids": blocker_row_ids,
        "items": items,
        "guards": _guards(),
        "next_boundary": (
            "feed_duplicate_decision_manifest_to_identity_merge_fill_apply_preflight"
            if expected_by_row_id and not hard_blockers
            else "operator_complete_duplicate_decision_manifest"
            if expected_by_row_id
            else "duplicate_decision_manifest_not_required"
        ),
    }


def build_packet(
    db_path: Path,
    *,
    immutable: bool = False,
    max_items_output: int = 20,
    decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    if not db.exists():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "db": str(db),
            "blockers": ["archive_db_missing"],
            "guards": _guards(),
        }

    with _connect(db, immutable=immutable) as conn:
        latest_ids = _latest_fact_ids(conn)
        rows = _event_fact_rows(conn)

    by_fact_snapshot_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_fact_snapshot_id[str(row["fact_snapshot_id"])].append(row)
    duplicate_groups = {
        fact_snapshot_id: group
        for fact_snapshot_id, group in by_fact_snapshot_id.items()
        if len(group) > 1
    }

    all_existing_ids = {str(row["fact_snapshot_id"]) for row in rows}
    proposed_id_counts: Counter[str] = Counter()
    groups: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    blockers: list[str] = []
    noncanonical_count = 0
    canonical_count = 0

    for fact_snapshot_id, group_rows in sorted(duplicate_groups.items()):
        canonical_id, canonical_reason, canonical_blockers = _choose_canonical(group_rows, latest_ids)
        blockers.extend(canonical_blockers)
        row_items: list[dict[str, Any]] = []
        payload_hashes = sorted({str(row["payload_hash"]) for row in group_rows})
        posterior_run_ids = sorted({str(row["posterior_run_id"]) for row in group_rows if str(row["posterior_run_id"])})
        for row in group_rows:
            is_canonical = int(row["id"]) == int(canonical_id)
            if is_canonical:
                canonical_count += 1
                action = "keep_existing_fact_snapshot_id"
                proposed_id = fact_snapshot_id
                row_blockers: list[str] = []
            else:
                noncanonical_count += 1
                action = "regenerate_fact_snapshot_id_required"
                proposed_id = _proposed_new_fact_snapshot_id(row)
                proposed_id_counts[proposed_id] += 1
                row_blockers = []
                if proposed_id == fact_snapshot_id:
                    row_blockers.append("proposed_fact_snapshot_id_equals_duplicate_original")
                if proposed_id in all_existing_ids and proposed_id != fact_snapshot_id:
                    row_blockers.append("proposed_fact_snapshot_id_conflicts_existing_payload")
                blockers.extend(row_blockers)
            item = {
                "row_id": int(row["id"]),
                "is_canonical": is_canonical,
                "action": action,
                "fact_as_of": row.get("fact_as_of"),
                "posterior_run_id": row.get("posterior_run_id"),
                "payload_hash": row.get("payload_hash"),
                "source_event_ids": list(row.get("source_event_ids") or []),
                "current_fact_snapshot_id": fact_snapshot_id,
                "proposed_fact_snapshot_id": proposed_id,
                "blockers": row_blockers,
            }
            row_items.append(item)
            decisions.append(
                {
                    "row_id": int(row["id"]),
                    "current_fact_snapshot_id": fact_snapshot_id,
                    "decision": action,
                    "proposed_fact_snapshot_id": proposed_id,
                    "reason": "canonical_row_keeps_duplicate_id" if is_canonical else "noncanonical_row_requires_unique_snapshot_id",
                    "requires_payload_update": not is_canonical,
                    "requires_table_column_update": True,
                    "blockers": row_blockers,
                }
            )
        groups.append(
            {
                "fact_snapshot_id": fact_snapshot_id,
                "row_count": len(group_rows),
                "canonical_row_id": canonical_id,
                "canonical_reason": canonical_reason,
                "canonical_blockers": canonical_blockers,
                "payload_hash_count": len(payload_hashes),
                "posterior_run_id_count": len(posterior_run_ids),
                "posterior_run_ids": posterior_run_ids,
                "rows": row_items,
                "classification": (
                    "requires_noncanonical_regeneration"
                    if not canonical_blockers
                    else "manual_review_required"
                ),
            }
        )

    for proposed_id, count in proposed_id_counts.items():
        if count > 1:
            blockers.append("proposed_fact_snapshot_id_duplicate_within_packet")
            for decision in decisions:
                if decision.get("proposed_fact_snapshot_id") == proposed_id and decision.get("decision") != "keep_existing_fact_snapshot_id":
                    decision.setdefault("blockers", []).append("proposed_fact_snapshot_id_duplicate_within_packet")

    blocker_values = sorted(set(blockers))
    manifest_audit = audit_decision_manifest(decisions, blocker_values, decision_manifest)
    manifest_hard_blockers = list(manifest_audit.get("hard_blockers") or [])
    clearable_blockers = set()
    if (
        "latest_row_is_not_natural_fact_snapshot_owner" in blocker_values
        and bool(manifest_audit.get("latest_anchor_policy_ready"))
    ):
        clearable_blockers.add("latest_row_is_not_natural_fact_snapshot_owner")
    unresolved_packet_blockers = [blocker for blocker in blocker_values if blocker not in clearable_blockers]
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not unresolved_packet_blockers and not manifest_hard_blockers,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_duplicate_fact_snapshot_packet_no_apply",
        "db": str(db),
        "duplicate_group_count": len(groups),
        "duplicate_row_count": sum(len(group) for group in duplicate_groups.values()),
        "canonical_row_count": canonical_count,
        "noncanonical_row_count": noncanonical_count,
        "groups": groups[:visible_limit],
        "groups_truncated": len(groups) > visible_limit,
        "operator_manifest_template": {
            "schema": MANIFEST_SCHEMA_VERSION,
            "decision_count": len(decisions),
            "allowed_decisions": list(ALLOWED_DECISIONS),
            "latest_anchor_policy": (
                "manual_review_required"
                if "latest_row_is_not_natural_fact_snapshot_owner" in blocker_values
                else ""
            ),
            "latest_anchor_policy_required": bool(
                "latest_row_is_not_natural_fact_snapshot_owner" in blocker_values
            ),
            "allowed_latest_anchor_policies": list(LATEST_ANCHOR_POLICIES),
            "decisions": decisions[:visible_limit],
            "decisions_truncated": len(decisions) > visible_limit,
        },
        "decision_manifest_audit": {
            **manifest_audit,
            "items": list(manifest_audit.get("items") or [])[:visible_limit],
            "items_truncated": len(manifest_audit.get("items") or []) > visible_limit,
        },
        "blockers": blocker_values,
        "manifest_resolved_blockers": sorted(clearable_blockers),
        "hard_blockers": sorted(set(unresolved_packet_blockers + manifest_hard_blockers)),
        "guards": _guards(),
        "next_boundary": (
            str(manifest_audit.get("next_boundary") or "")
            if groups and manifest_hard_blockers
            else "feed_duplicate_decision_manifest_to_identity_merge_fill_apply_preflight"
            if groups
            else "duplicate_fact_snapshot_packet_not_required"
        ),
    }


def compact_packet(packet: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    visible_limit = max(0, int(max_items_output))
    compact_groups: list[dict[str, Any]] = []
    for group in list(packet.get("groups") or [])[:visible_limit]:
        group_map = dict(_as_mapping(group))
        rows = list(group_map.get("rows") or [])
        group_map["rows"] = rows[:visible_limit]
        group_map["rows_truncated"] = len(rows) > visible_limit
        compact_groups.append(group_map)
    audit = dict(_as_mapping(packet.get("decision_manifest_audit")))
    audit_items = list(audit.get("items") or [])
    audit["items"] = audit_items[:visible_limit]
    audit["items_truncated"] = bool(audit.get("items_truncated") or len(audit_items) > visible_limit)
    return {
        "schema": str(packet.get("schema") or SCHEMA_VERSION) + "_summary",
        "source_schema": str(packet.get("schema") or ""),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "db": str(packet.get("db") or ""),
        "duplicate_group_count": int(packet.get("duplicate_group_count") or 0),
        "duplicate_row_count": int(packet.get("duplicate_row_count") or 0),
        "canonical_row_count": int(packet.get("canonical_row_count") or 0),
        "noncanonical_row_count": int(packet.get("noncanonical_row_count") or 0),
        "groups": compact_groups,
        "groups_truncated": bool(packet.get("groups_truncated")),
        "operator_manifest_template": dict(packet.get("operator_manifest_template") or {}),
        "decision_manifest_audit": audit,
        "blockers": list(packet.get("blockers") or [])[:50],
        "hard_blockers": list(packet.get("hard_blockers") or [])[:50],
        "guards": dict(packet.get("guards") or {}),
        "next_boundary": str(packet.get("next_boundary") or ""),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only duplicate fact_snapshot_id decision packet.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--decision-manifest", default="")
    parser.add_argument("--emit-decision-manifest-template", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    packet = build_packet(
        Path(args.db),
        immutable=bool(args.immutable),
        max_items_output=1000000 if args.emit_decision_manifest_template else int(args.max_items_output),
        decision_manifest=_load_json_file(args.decision_manifest),
    )
    output: Mapping[str, Any]
    if args.emit_decision_manifest_template:
        output = dict(_as_mapping(packet.get("operator_manifest_template")))
    else:
        output = compact_packet(packet, max_items_output=int(args.max_items_output)) if args.summary else packet
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    if args.emit_decision_manifest_template:
        return 0
    return 0 if packet.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
