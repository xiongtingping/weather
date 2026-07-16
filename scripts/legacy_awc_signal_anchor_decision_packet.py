#!/usr/bin/env python3
"""Read-only decision packet for legacy AWC signal-anchor candidates.

This packet turns legacy AWC lower-bound replay candidates into an explicit
operator decision manifest. It does not rebuild signal ledgers, write archive.db,
fetch sources, restart launchd, or place orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_post_apply_resolution_plan as resolution_plan  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "legacy_awc_signal_anchor_decision_packet_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"
MANIFEST_SCHEMA_VERSION = "legacy_awc_signal_anchor_decision_manifest_v1"
MANIFEST_AUDIT_SCHEMA_VERSION = "legacy_awc_signal_anchor_decision_manifest_audit_v1"

DECISION_UNDECIDED = "undecided"
DECISION_REVIEWED_NO_SIGNAL = "mark_reviewed_legacy_no_signal_anchor"
DECISION_MANUAL_REBUILD = "manual_rebuild_signal_anchor_with_reviewed_source_evidence"
DECISION_WAIT = "wait_for_new_fact_or_dedicated_signal_anchor_writer"
ALLOWED_DECISIONS = (
    DECISION_UNDECIDED,
    DECISION_REVIEWED_NO_SIGNAL,
    DECISION_MANUAL_REBUILD,
    DECISION_WAIT,
)


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
        "writes_event_fact_summary_cache": False,
        "writes_signal_to_order_timing": False,
        "writes_auto_trade_decisions": False,
        "writes_manifest_file": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _candidate_key(*, fact_id: Any, source: Any, constraint_mode: Any) -> str:
    return "legacy_awc_anchor:{fact_id}:{source}:{constraint_mode}".format(
        fact_id=str(fact_id or "").strip() or "missing_fact_id",
        source=str(source or "").strip() or "missing_source",
        constraint_mode=str(constraint_mode or "").strip() or "missing_constraint_mode",
    )


def _candidate_from_replay_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    override = _as_mapping(raw.get("override"))
    fact_id = int(raw.get("fact_id") or 0)
    source = str(override.get("source") or raw.get("source") or "").strip()
    constraint_mode = str(
        override.get("constraint_mode") or raw.get("constraint_mode") or ""
    ).strip()
    decision_id = _candidate_key(
        fact_id=fact_id,
        source=source,
        constraint_mode=constraint_mode,
    )
    return {
        "decision_id": decision_id,
        "fact_id": fact_id,
        "slug": str(raw.get("slug") or "").strip(),
        "event_date": str(raw.get("event_date") or "").strip(),
        "source": source,
        "constraint_mode": constraint_mode,
        "target_bucket": override.get("target_bucket"),
        "value_c": override.get("value_c"),
        "value_at": override.get("value_at"),
        "latest_at": override.get("latest_at"),
        "fetched_at": override.get("fetched_at"),
        "final_bucket_lock": bool(override.get("final_bucket_lock")),
        "final_lock_reason": str(override.get("final_lock_reason") or ""),
        "hard_source_signal_expected": bool(raw.get("hard_source_signal_expected")),
        "signal_id": str(raw.get("signal_id") or ""),
        "no_apply_probe_command": str(raw.get("no_apply_probe_command") or ""),
        "allowed_operator_decisions": list(ALLOWED_DECISIONS),
        "boundary": "legacy_awc_lower_bound_signal_anchor_no_auto_rebuild",
    }


def _candidate_rows_from_replay_resolution(replay_resolution: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _as_sequence(replay_resolution.get("items")):
        raw_map = _as_mapping(raw)
        if not raw_map:
            continue
        candidate = _candidate_from_replay_item(raw_map)
        decision_id = candidate["decision_id"]
        if decision_id in seen:
            continue
        seen.add(decision_id)
        rows.append(candidate)
    rows.sort(key=lambda row: (str(row.get("event_date") or ""), int(row.get("fact_id") or 0)))
    return rows


def build_decision_manifest(
    candidates: Sequence[Mapping[str, Any]],
    *,
    default_decision: str = DECISION_UNDECIDED,
) -> dict[str, Any]:
    if default_decision not in ALLOWED_DECISIONS:
        default_decision = DECISION_UNDECIDED
    decisions = []
    for candidate in candidates:
        item = _as_mapping(candidate)
        decisions.append(
            {
                "decision_id": item.get("decision_id"),
                "fact_id": item.get("fact_id"),
                "slug": item.get("slug"),
                "event_date": item.get("event_date"),
                "source": item.get("source"),
                "constraint_mode": item.get("constraint_mode"),
                "decision": default_decision,
                "allowed_decisions": list(ALLOWED_DECISIONS),
                "review_reason": "",
                "source_evidence_ref": item.get("no_apply_probe_command") or "",
                "operator": "",
                "reviewed_at": "",
            }
        )
    return {
        "schema": MANIFEST_SCHEMA_VERSION,
        "read_only_template": True,
        "decision_count": len(decisions),
        "decisions": decisions,
        "boundary": "operator_manifest_only_no_signal_ledger_write",
    }


def load_decision_manifest_file(path: Path | str | None) -> Mapping[str, Any]:
    if not path:
        return {}
    manifest_path = Path(path).expanduser()
    if not manifest_path.exists():
        raise FileNotFoundError(str(manifest_path))
    with manifest_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, Mapping) else {}


def _decision_status(decision: str, *, review_evidence_present: bool) -> tuple[str, str, bool, bool]:
    if decision == DECISION_REVIEWED_NO_SIGNAL:
        if review_evidence_present:
            return "reviewed_exception", "", True, True
        return (
            "pending_review_evidence",
            "review_reason_or_source_evidence_ref_required",
            True,
            False,
        )
    if decision == DECISION_MANUAL_REBUILD:
        return (
            "pending_dedicated_writer",
            "manual_rebuild_requires_dedicated_signal_anchor_writer",
            True,
            False,
        )
    if decision == DECISION_WAIT:
        return (
            "pending_future_source_or_writer",
            "waiting_for_new_fact_or_dedicated_signal_anchor_writer",
            True,
            False,
        )
    return "undecided", "operator_decision_required", False, False


def audit_decision_manifest(
    candidates: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    manifest_map = _as_mapping(manifest)
    manifest_provided = bool(manifest_map)
    candidate_by_id = {
        str(_as_mapping(candidate).get("decision_id") or ""): _as_mapping(candidate)
        for candidate in candidates
        if str(_as_mapping(candidate).get("decision_id") or "")
    }
    decision_rows = [
        _as_mapping(row)
        for row in _as_sequence(manifest_map.get("decisions"))
        if isinstance(row, Mapping)
    ]
    decision_by_id: dict[str, Mapping[str, Any]] = {}
    duplicates: list[str] = []
    unexpected: list[str] = []
    for row in decision_rows:
        decision_id = str(row.get("decision_id") or "").strip()
        if not decision_id:
            unexpected.append("missing_decision_id")
            continue
        if decision_id in decision_by_id:
            duplicates.append(decision_id)
            continue
        if decision_id not in candidate_by_id:
            unexpected.append(decision_id)
            continue
        decision_by_id[decision_id] = row

    items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    missing_decision_ids: list[str] = []
    invalid_decision_ids: list[str] = []
    review_evidence_missing_ids: list[str] = []
    pending_ids: list[str] = []
    dedicated_writer_required_ids: list[str] = []
    reviewed_exception_ids: list[str] = []

    for decision_id, candidate in candidate_by_id.items():
        row = decision_by_id.get(decision_id, {})
        decision = str(row.get("decision") or DECISION_UNDECIDED).strip()
        if not manifest_provided or decision_id not in decision_by_id:
            decision = DECISION_UNDECIDED
            missing_decision_ids.append(decision_id)
        if decision not in ALLOWED_DECISIONS:
            invalid_decision_ids.append(decision_id)
            status = "invalid"
            reason = "decision_not_allowed"
            route_recorded = False
            acceptance_resolved = False
        else:
            review_evidence_present = bool(
                str(row.get("review_reason") or "").strip()
                or str(row.get("source_evidence_ref") or "").strip()
            )
            status, reason, route_recorded, acceptance_resolved = _decision_status(
                decision,
                review_evidence_present=review_evidence_present,
            )
            if status == "pending_review_evidence":
                review_evidence_missing_ids.append(decision_id)
            if status in {"undecided", "pending_review_evidence", "pending_future_source_or_writer"}:
                pending_ids.append(decision_id)
            if status == "pending_dedicated_writer":
                pending_ids.append(decision_id)
                dedicated_writer_required_ids.append(decision_id)
            if status == "reviewed_exception":
                reviewed_exception_ids.append(decision_id)
        counts[decision] += 1
        item = {
            "decision_id": decision_id,
            "fact_id": candidate.get("fact_id"),
            "slug": candidate.get("slug"),
            "event_date": candidate.get("event_date"),
            "source": candidate.get("source"),
            "constraint_mode": candidate.get("constraint_mode"),
            "decision": decision,
            "status": status,
            "blocking_reason": reason,
            "operator_route_recorded": route_recorded,
            "acceptance_resolved": acceptance_resolved,
            "source_evidence_ref": row.get("source_evidence_ref") or candidate.get("no_apply_probe_command"),
        }
        items.append(item)

    candidate_count = len(candidate_by_id)
    operator_route_complete = bool(
        candidate_count
        and not missing_decision_ids
        and not invalid_decision_ids
        and not duplicates
        and not unexpected
        and all(bool(item.get("operator_route_recorded")) for item in items)
    )
    acceptance_exception_complete = bool(
        candidate_count
        and not invalid_decision_ids
        and not duplicates
        and not unexpected
        and len(reviewed_exception_ids) == candidate_count
    )
    hard_blockers: list[str] = []
    if candidate_count and not manifest_provided:
        hard_blockers.append("legacy_awc_signal_anchor_decision_manifest_missing")
    if missing_decision_ids:
        hard_blockers.append("legacy_awc_signal_anchor_decision_missing")
    if invalid_decision_ids or duplicates or unexpected:
        hard_blockers.append("legacy_awc_signal_anchor_decision_manifest_invalid")
    if review_evidence_missing_ids:
        hard_blockers.append("legacy_awc_signal_anchor_review_evidence_missing")
    if dedicated_writer_required_ids:
        hard_blockers.append("legacy_awc_signal_anchor_dedicated_writer_required")
    if pending_ids and not dedicated_writer_required_ids:
        hard_blockers.append("legacy_awc_signal_anchor_decision_pending")

    return {
        "schema": MANIFEST_AUDIT_SCHEMA_VERSION,
        "manifest_schema": manifest_map.get("schema"),
        "manifest_provided": manifest_provided,
        "candidate_count": candidate_count,
        "decision_count": len(decision_rows),
        "decision_counts": dict(sorted(counts.items())),
        "missing_decision_count": len(missing_decision_ids),
        "invalid_decision_count": len(invalid_decision_ids),
        "duplicate_decision_count": len(duplicates),
        "unexpected_decision_count": len(unexpected),
        "review_evidence_missing_count": len(review_evidence_missing_ids),
        "dedicated_writer_required_count": len(dedicated_writer_required_ids),
        "reviewed_exception_count": len(reviewed_exception_ids),
        "operator_route_complete": operator_route_complete,
        "acceptance_exception_complete": acceptance_exception_complete,
        "decision_required": bool(candidate_count and not acceptance_exception_complete),
        "hard_blockers": hard_blockers,
        "items": items,
        "duplicate_decision_ids": duplicates,
        "unexpected_decision_ids": unexpected,
        "missing_decision_ids": missing_decision_ids,
        "invalid_decision_ids": invalid_decision_ids,
        "pending_decision_ids": pending_ids,
        "review_evidence_missing_ids": review_evidence_missing_ids,
        "dedicated_writer_required_ids": dedicated_writer_required_ids,
        "boundary": "read_only_decision_audit_no_signal_ledger_mutation",
    }


def build_decision_packet_from_replay_resolution(
    replay_resolution: Mapping[str, Any],
    *,
    decision_manifest: Mapping[str, Any] | None = None,
    max_items_output: int = 20,
) -> dict[str, Any]:
    candidates = _candidate_rows_from_replay_resolution(_as_mapping(replay_resolution))
    manifest_template = build_decision_manifest(candidates)
    manifest_audit = audit_decision_manifest(candidates, decision_manifest)
    visible_limit = max(0, int(max_items_output))
    source_counts = Counter(str(row.get("source") or "missing") for row in candidates)
    constraint_counts = Counter(str(row.get("constraint_mode") or "missing") for row in candidates)
    hard_blockers = list(manifest_audit.get("hard_blockers") or [])
    return {
        "schema": SCHEMA_VERSION,
        "ok": not hard_blockers,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "legacy_awc_signal_anchor_decision_packet_no_apply",
        "candidate_count": len(candidates),
        "candidate_items": candidates[:visible_limit] if visible_limit else [],
        "candidate_items_truncated": len(candidates) > visible_limit,
        "source_counts": dict(sorted(source_counts.items())),
        "constraint_mode_counts": dict(sorted(constraint_counts.items())),
        "decision_manifest_template": manifest_template,
        "decision_manifest_audit": {
            **manifest_audit,
            "items": list(manifest_audit.get("items") or [])[:visible_limit] if visible_limit else [],
            "items_truncated": len(manifest_audit.get("items") or []) > visible_limit,
        },
        "allowed_operator_decisions": list(ALLOWED_DECISIONS),
        "hard_blockers": hard_blockers,
        "boundary": {
            "automatic_signal_ledger_rebuild_included": False,
            "operator_manifest_only": True,
            "reviewed_legacy_no_signal_is_explicit_exception": True,
            "manual_rebuild_requires_dedicated_writer": True,
            "historical_backfill_included": False,
        },
        "guards": _guards(),
        "next_boundary": (
            "provide_reviewed_legacy_no_signal_manifest_or_build_dedicated_signal_anchor_writer"
            if hard_blockers
            else "feed_manifest_to_post_apply_resolution_confirmation_packet"
        ),
    }


def compact_decision_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    audit = _as_mapping(packet.get("decision_manifest_audit"))
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": packet.get("schema"),
        "ok": bool(packet.get("ok")),
        "read_only": bool(packet.get("read_only")),
        "dry_run": bool(packet.get("dry_run")),
        "execution_mode": "legacy_awc_signal_anchor_decision_packet_summary_no_apply",
        "candidate_count": int(packet.get("candidate_count") or 0),
        "source_counts": dict(_as_mapping(packet.get("source_counts"))),
        "constraint_mode_counts": dict(_as_mapping(packet.get("constraint_mode_counts"))),
        "allowed_operator_decisions": list(packet.get("allowed_operator_decisions") or []),
        "decision_manifest": {
            "schema": MANIFEST_SCHEMA_VERSION,
            "decision_count": int(_as_mapping(packet.get("decision_manifest_template")).get("decision_count") or 0),
        },
        "decision_manifest_audit": {
            "schema": audit.get("schema"),
            "manifest_provided": bool(audit.get("manifest_provided")),
            "candidate_count": int(audit.get("candidate_count") or 0),
            "decision_count": int(audit.get("decision_count") or 0),
            "decision_counts": dict(_as_mapping(audit.get("decision_counts"))),
            "missing_decision_count": int(audit.get("missing_decision_count") or 0),
            "invalid_decision_count": int(audit.get("invalid_decision_count") or 0),
            "duplicate_decision_count": int(audit.get("duplicate_decision_count") or 0),
            "unexpected_decision_count": int(audit.get("unexpected_decision_count") or 0),
            "review_evidence_missing_count": int(audit.get("review_evidence_missing_count") or 0),
            "dedicated_writer_required_count": int(audit.get("dedicated_writer_required_count") or 0),
            "reviewed_exception_count": int(audit.get("reviewed_exception_count") or 0),
            "operator_route_complete": bool(audit.get("operator_route_complete")),
            "acceptance_exception_complete": bool(audit.get("acceptance_exception_complete")),
            "decision_required": bool(audit.get("decision_required")),
            "hard_blockers": list(audit.get("hard_blockers") or []),
        },
        "boundary": dict(_as_mapping(packet.get("boundary"))),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "hard_blockers": list(packet.get("hard_blockers") or []),
        "next_boundary": packet.get("next_boundary"),
    }


def build_decision_packet(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolution = resolution_plan.build_resolution_plan(
        Path(db_path).expanduser().resolve(),
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max(1, int(limit), int(max_items_output)),
    )
    packet = build_decision_packet_from_replay_resolution(
        _as_mapping(resolution.get("replay_anchor_resolution")),
        decision_manifest=decision_manifest,
        max_items_output=max_items_output,
    )
    packet["db"] = str(Path(db_path).expanduser().resolve())
    packet["scope"] = {
        "since_days": int(since_days),
        "since_date": since_date,
        "slug": slug,
        "event_date": event_date,
        "market_scope": market_scope,
        "limit": int(limit),
        "max_items_output": int(max_items_output),
    }
    packet["resolution_summary"] = {
        "schema": resolution.get("schema"),
        "unresolved_counts": dict(_as_mapping(resolution.get("unresolved_counts"))),
        "source_counts": dict(_as_mapping(resolution.get("source_counts"))),
    }
    return packet


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--decision-manifest", type=Path, default=None)
    parser.add_argument("--emit-decision-manifest-template", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = load_decision_manifest_file(args.decision_manifest)
    packet = build_decision_packet(
        args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        decision_manifest=manifest,
    )
    if args.emit_decision_manifest_template:
        payload = packet.get("decision_manifest_template") or {}
    else:
        payload = compact_decision_packet(packet) if args.summary else packet
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if packet.get("read_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
