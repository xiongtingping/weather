#!/usr/bin/env python3
"""Build a read-only batch persistence plan for settled daily reviews.

The batch plan is the pre-apply review packet: it enumerates settled markets,
builds the same single-market daily review report for each event, and summarizes
the compact `daily_weather_reviews` / calibration candidate / parameter event
rows that would be written later by the controlled writer.

It never writes archive.db, never creates tables, never backfills history, never
fetches external weather sources, never changes parameters, never restarts
launchd, and never places orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daily_review_persistence_writer import daily_review_persistence_schema_status
from lineage_identity_runtime import build_correlation_id, build_fact_snapshot_id
from scripts import daily_review_coverage_audit, daily_weather_review_report


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "daily_review_persistence_batch_plan_v1"
LINEAGE_REPAIR_CANDIDATE_SCHEMA = "daily_review_lineage_repair_candidate_v1"
LINEAGE_READINESS_SCHEMA = "daily_review_batch_lineage_readiness_v1"
LINEAGE_OPERATOR_PACKET_SCHEMA = "daily_review_lineage_repair_operator_packet_v1"
LINEAGE_DECISION_MANIFEST_SCHEMA = "daily_review_lineage_repair_decision_manifest_v1"
LINEAGE_DECISION_MANIFEST_AUDIT_SCHEMA = "daily_review_lineage_repair_decision_manifest_audit_v1"
LINEAGE_DECISION_APPLY_READINESS_SCHEMA = "daily_review_lineage_decision_apply_readiness_v1"
LINEAGE_REPAIR_APPLY_CONTRACT_SCHEMA = "daily_review_lineage_repair_apply_contract_v1"
LINEAGE_REPAIR_CONFIRMATION_PHRASE = "APPLY_DAILY_REVIEW_LINEAGE_REPAIR"
LINEAGE_DECISIONS = (
    "confirm_repair",
    "exclude_sample",
    "wait_for_new_fact",
    "manual_rebuild_fact_snapshot",
)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _date_only(raw: Any) -> str:
    text = str(raw or "").strip()
    return text[:10] if len(text) >= 10 else text


def _counter(values: Sequence[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        text = str(value or "").strip() or "missing"
        out[text] = out.get(text, 0) + 1
    return dict(sorted(out.items()))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sum_row_counts(target: dict[str, int], raw_counts: Mapping[str, Any]) -> None:
    for key, value in raw_counts.items():
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            count = 0
        target[str(key)] = int(target.get(str(key), 0)) + count


def _item_repair_correlation_id(item: Mapping[str, Any]) -> str:
    return _text(_as_mapping(item.get("lineage_repair_candidate")).get("repair_correlation_id"))


def _file_state(path: Path | str | None) -> dict[str, Any]:
    if not path:
        return {"path": "", "exists": False, "size_bytes": None}
    resolved = Path(path).expanduser().resolve()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "size_bytes": resolved.stat().st_size if resolved.exists() else None,
    }


def _candidate_fact_snapshot_id(
    *,
    slug: str,
    event_date: str,
    fact: Mapping[str, Any],
) -> tuple[str, str]:
    fact_as_of = str(fact.get("fact_as_of") or fact.get("created_at") or "").strip()
    if not fact_as_of:
        return "", "not_available_fact_as_of_missing"
    return (
        build_fact_snapshot_id(
            slug=str(slug or ""),
            event_date=_date_only(event_date),
            fact_as_of=fact_as_of,
            source_event_ids=(),
        ),
        "deterministic_preview_from_legacy_fact_row_without_source_event_ids",
    )


def _lineage_repair_candidate(
    *,
    event: Mapping[str, Any],
    report: Mapping[str, Any],
    lineage_apply_blockers: Sequence[str],
) -> dict[str, Any]:
    blockers = [str(value or "").strip() for value in lineage_apply_blockers if str(value or "").strip()]
    if not blockers:
        return {}

    fact = _as_mapping(report.get("fact"))
    slug = str(event.get("slug") or fact.get("slug") or "")
    event_date = _date_only(event.get("event_date") or fact.get("event_date"))
    fact_id = str(fact.get("id") or "")
    report_blockers = {str(value or "").strip() for value in report.get("blockers") or []}
    fact_missing = "fact_snapshot_not_found" in report_blockers or not fact
    if fact_missing:
        repair_type = "missing_fact_snapshot_for_settled_event"
        target_table = "event_fact_history"
        target_row_id = ""
        missing_fields = ["event_fact_history.row"]
        candidate_fact_snapshot_id = ""
        candidate_fact_snapshot_id_source = "not_available_fact_snapshot_row_missing"
        reason = "settled_event_has_no_replayable_fact_snapshot"
        next_boundary = "select_or_rebuild_fact_snapshot_requires_explicit_user_confirmation"
    else:
        repair_type = "legacy_fact_snapshot_lineage_repair"
        target_table = "event_fact_history"
        target_row_id = fact_id
        missing_fields = list(blockers)
        candidate_fact_snapshot_id, candidate_fact_snapshot_id_source = _candidate_fact_snapshot_id(
            slug=slug,
            event_date=event_date,
            fact=fact,
        )
        reason = "legacy_fact_snapshot_missing_fact_snapshot_id"
        next_boundary = "confirmed_lineage_repair_or_exclude_legacy_sample_before_daily_review_apply"

    return {
        "schema": LINEAGE_REPAIR_CANDIDATE_SCHEMA,
        "read_only": True,
        "execution_mode": "lineage_repair_no_apply_candidate",
        "repair_correlation_id": build_correlation_id(
            "daily_review_lineage_repair",
            slug,
            event_date,
            fact_id,
            blockers,
        ),
        "state": "proposed_no_apply",
        "repair_type": repair_type,
        "reason": reason,
        "slug": slug,
        "event_date": event_date,
        "event_run_id": event.get("id"),
        "target_table": target_table,
        "target_row_id": target_row_id,
        "missing_fields": missing_fields,
        "lineage_apply_blockers": blockers,
        "candidate_fact_snapshot_id": candidate_fact_snapshot_id,
        "candidate_fact_snapshot_id_source": candidate_fact_snapshot_id_source,
        "write_owner_if_confirmed": "weather_fact_snapshot_writer_or_lineage_repair_contract",
        "blocked_from_daily_review_apply_until_repaired": True,
        "can_auto_apply": False,
        "requires_explicit_user_confirmation": True,
        "requires_archive_db_backup": True,
        "historical_backfill_required": True,
        "writes_archive_db_if_confirmed": True,
        "guards": {
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_boundary": next_boundary,
    }


def _lineage_decision_options(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    repair_type = str(candidate.get("repair_type") or "")
    base_guards = {
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_daily_weather_reviews": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }
    options: list[dict[str, Any]] = []
    if repair_type == "legacy_fact_snapshot_lineage_repair":
        options.append(
            {
                "decision": "confirm_repair",
                "label": "confirm deterministic lineage repair for this legacy fact",
                "allowed_for_repair_type": True,
                "unblocks_daily_review_persistence_after_apply": True,
                "requires_archive_db_backup": True,
                "requires_explicit_user_confirmation": True,
                "requires_candidate_fact_snapshot_id": True,
                "requires_source_event_ids_review": True,
                "write_owner_if_later_applied": "weather_fact_snapshot_writer_or_lineage_repair_contract",
                "later_apply_effect": "merge_fill_fact_snapshot_id_for_target_legacy_fact_only",
                "guards": base_guards,
            }
        )
    else:
        options.append(
            {
                "decision": "confirm_repair",
                "label": "not available until a concrete fact snapshot row is selected or rebuilt",
                "allowed_for_repair_type": False,
                "unblocks_daily_review_persistence_after_apply": False,
                "requires_archive_db_backup": True,
                "requires_explicit_user_confirmation": True,
                "later_apply_effect": "blocked_select_or_rebuild_fact_snapshot_first",
                "guards": base_guards,
            }
        )
    options.extend(
        [
            {
                "decision": "exclude_sample",
                "label": "exclude this settled event from the first daily review persistence apply",
                "allowed_for_repair_type": True,
                "unblocks_daily_review_persistence_after_apply": True,
                "requires_archive_db_backup": False,
                "requires_explicit_user_confirmation": True,
                "later_apply_effect": "daily_review_batch_apply_must_filter_out_this_event_run_id",
                "guards": base_guards,
            },
            {
                "decision": "wait_for_new_fact",
                "label": "do not repair now; wait until a future fact snapshot with complete lineage exists",
                "allowed_for_repair_type": True,
                "unblocks_daily_review_persistence_after_apply": False,
                "requires_archive_db_backup": False,
                "requires_explicit_user_confirmation": True,
                "later_apply_effect": "keeps_this_event_blocked_from_daily_review_persistence",
                "guards": base_guards,
            },
            {
                "decision": "manual_rebuild_fact_snapshot",
                "label": "manually rebuild or select a replacement fact snapshot before persistence",
                "allowed_for_repair_type": True,
                "unblocks_daily_review_persistence_after_apply": False,
                "requires_archive_db_backup": True,
                "requires_explicit_user_confirmation": True,
                "requires_source_lineage_review": True,
                "later_apply_effect": "separate_rebuild_or_selection_contract_required_before_batch_apply",
                "guards": base_guards,
            },
        ]
    )
    return options


def build_lineage_repair_operator_packet(
    candidates: Sequence[Mapping[str, Any]],
    *,
    total_settled_event_count: int = 0,
) -> dict[str, Any]:
    candidate_items = [dict(_as_mapping(candidate)) for candidate in candidates if isinstance(candidate, Mapping)]
    items: list[dict[str, Any]] = []
    for candidate in candidate_items:
        options = _lineage_decision_options(candidate)
        allowed_options = [option for option in options if option.get("allowed_for_repair_type")]
        items.append(
            {
                "repair_correlation_id": candidate.get("repair_correlation_id"),
                "state": "operator_decision_required",
                "slug": candidate.get("slug"),
                "event_date": candidate.get("event_date"),
                "event_run_id": candidate.get("event_run_id"),
                "repair_type": candidate.get("repair_type"),
                "reason": candidate.get("reason"),
                "target_table": candidate.get("target_table"),
                "target_row_id": candidate.get("target_row_id"),
                "missing_fields": list(candidate.get("missing_fields") or []),
                "candidate_fact_snapshot_id": candidate.get("candidate_fact_snapshot_id"),
                "allowed_decision_count": len(allowed_options),
                "allowed_decisions": [option.get("decision") for option in allowed_options],
                "decision_options": options,
                "must_decide_before_daily_review_persistence_apply": True,
            }
        )
    return {
        "schema": LINEAGE_OPERATOR_PACKET_SCHEMA,
        "read_only": True,
        "execution_mode": "lineage_repair_operator_decision_no_apply_packet",
        "candidate_count": len(items),
        "total_settled_event_count": int(total_settled_event_count or 0),
        "candidate_type_counts": _counter([item.get("repair_type") for item in items]),
        "operator_decision_states": {
            "undecided": len(items),
            "confirmed_repair": 0,
            "excluded_sample": 0,
            "waiting_for_new_fact": 0,
            "manual_rebuild_required": 0,
        },
        "allowed_decisions": [
            "confirm_repair",
            "exclude_sample",
            "wait_for_new_fact",
            "manual_rebuild_fact_snapshot",
        ],
        "daily_review_persistence_apply_gate": {
            "blocked": bool(items),
            "reason": (
                "lineage_repair_candidates_require_operator_decision"
                if items
                else ""
            ),
            "unblock_condition": (
                "each candidate must be confirmed_repair_applied or excluded_sample_before_persistence"
                if items
                else "no_lineage_repair_candidates"
            ),
        },
        "items": items,
        "guards": {
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "operator_choose_repair_exclude_wait_or_rebuild_for_each_candidate"
            if items
            else "no_lineage_operator_decision_required"
        ),
    }


def _decision_input_rows(decisions: Any) -> list[Mapping[str, Any]]:
    if not decisions:
        return []
    if isinstance(decisions, Mapping):
        embedded = None
        for key in ("decisions", "items", "candidate_decisions"):
            if key in decisions:
                embedded = decisions.get(key)
                break
        if embedded is not None or "schema" in decisions:
            return [row for row in _as_sequence(embedded) if isinstance(row, Mapping)]
        rows: list[Mapping[str, Any]] = []
        for key, value in decisions.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("repair_correlation_id", key)
                rows.append(row)
            else:
                rows.append({"repair_correlation_id": key, "decision": value})
        return rows
    return [row for row in _as_sequence(decisions) if isinstance(row, Mapping)]


def _decision_rows_by_repair_id(decisions: Any) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    rows_by_id: dict[str, Mapping[str, Any]] = {}
    duplicates: list[str] = []
    for row in _decision_input_rows(decisions):
        repair_id = _text(
            row.get("repair_correlation_id")
            or row.get("candidate_repair_correlation_id")
            or row.get("id")
        )
        if not repair_id:
            continue
        if repair_id in rows_by_id:
            duplicates.append(repair_id)
        rows_by_id[repair_id] = row
    return rows_by_id, sorted(set(duplicates))


def build_lineage_repair_decision_manifest(
    candidates: Sequence[Mapping[str, Any]],
    *,
    decisions: Any = None,
    decided_by: str = "",
    decided_at: str = "",
) -> dict[str, Any]:
    candidate_items = [dict(_as_mapping(candidate)) for candidate in candidates if isinstance(candidate, Mapping)]
    decision_rows, duplicate_ids = _decision_rows_by_repair_id(decisions)
    rows: list[dict[str, Any]] = []
    for candidate in candidate_items:
        repair_id = _text(candidate.get("repair_correlation_id"))
        raw = dict(_as_mapping(decision_rows.get(repair_id)))
        decision = _text(raw.get("decision")) or "undecided"
        rows.append(
            {
                "repair_correlation_id": repair_id,
                "decision": decision,
                "reason": _text(raw.get("reason")),
                "decided_by": _text(raw.get("decided_by") or raw.get("operator") or decided_by),
                "decided_at": _text(raw.get("decided_at") or decided_at),
                "slug": candidate.get("slug"),
                "event_date": candidate.get("event_date"),
                "event_run_id": candidate.get("event_run_id"),
                "repair_type": candidate.get("repair_type"),
                "target_table": candidate.get("target_table"),
                "target_row_id": candidate.get("target_row_id"),
                "candidate_fact_snapshot_id": candidate.get("candidate_fact_snapshot_id"),
                "state": "operator_decision_recorded_no_apply"
                if decision in LINEAGE_DECISIONS
                else "operator_decision_required",
                "guards": {
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
    return {
        "schema": LINEAGE_DECISION_MANIFEST_SCHEMA,
        "read_only": True,
        "execution_mode": "lineage_repair_decision_manifest_no_apply",
        "candidate_count": len(candidate_items),
        "decision_count": len(rows),
        "duplicate_repair_correlation_ids": duplicate_ids,
        "allowed_decisions": list(LINEAGE_DECISIONS),
        "decisions": rows,
        "guards": {
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
            "review_or_apply_lineage_decision_manifest_to_batch_plan_scope"
            if rows
            else "no_lineage_repair_candidates"
        ),
    }


def build_lineage_repair_decision_manifest_template(
    plan: Mapping[str, Any],
    *,
    decisions: Any = None,
    decided_by: str = "",
    decided_at: str = "",
) -> dict[str, Any]:
    candidates = [dict(_as_mapping(candidate)) for candidate in plan.get("lineage_repair_candidates") or []]
    manifest = build_lineage_repair_decision_manifest(
        candidates,
        decisions=decisions,
        decided_by=decided_by,
        decided_at=decided_at,
    )
    manifest["template_context"] = {
        "schema": "daily_review_lineage_repair_decision_manifest_template_context_v1",
        "source_batch_plan_schema": plan.get("schema"),
        "db": plan.get("db"),
        "since_date": plan.get("since_date"),
        "market_scope": plan.get("market_scope"),
        "settled_event_count": plan.get("settled_event_count"),
        "effective_settled_event_count_before_manifest": plan.get("effective_settled_event_count"),
        "planned_review_count_before_manifest": plan.get("planned_review_count"),
        "lineage_repair_candidate_count": plan.get("lineage_repair_candidate_count"),
        "candidate_source_truncated": bool(plan.get("lineage_repair_candidates_truncated")),
        "apply_blockers_before_manifest": list(plan.get("apply_blockers") or []),
        "lineage_apply_blocker_counts_before_manifest": dict(
            plan.get("raw_lineage_apply_blocker_counts") or plan.get("lineage_apply_blocker_counts") or {}
        ),
    }
    manifest["operator_instructions"] = {
        "schema": "daily_review_lineage_repair_decision_manifest_operator_instructions_v1",
        "edit_field": "decisions[].decision",
        "default_decision": "undecided",
        "allowed_decisions": list(LINEAGE_DECISIONS),
        "decision_semantics": {
            "confirm_repair": "selects the repair path only; current apply remains blocked until the lineage repair is actually applied and the batch plan is rerun",
            "exclude_sample": "excludes this settled event from the current daily review persistence apply scope",
            "wait_for_new_fact": "keeps this settled event blocked until a future fact snapshot with complete lineage exists",
            "manual_rebuild_fact_snapshot": "keeps this settled event blocked until a separate manual rebuild or selection contract supplies a valid fact snapshot",
        },
        "required_review_before_use": [
            "repair_correlation_id",
            "slug",
            "event_date",
            "event_run_id",
            "repair_type",
            "target_table",
            "target_row_id",
            "candidate_fact_snapshot_id",
        ],
        "recheck_command": (
            "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 "
            "--max-items-output 20 --lineage-decision-manifest <manifest.json>"
        ),
    }
    manifest["template_complete"] = bool(
        not manifest["template_context"]["candidate_source_truncated"]
        and manifest.get("decision_count") == manifest["template_context"]["lineage_repair_candidate_count"]
    )
    manifest["guards"]["writes_manifest_file"] = False
    manifest["guards"]["executes_sql"] = False
    manifest["next_boundary"] = (
        "operator_fill_manifest_then_rerun_batch_plan_with_lineage_decision_manifest"
        if candidates
        else "no_lineage_repair_candidates"
    )
    return manifest


def audit_lineage_repair_decision_manifest(
    candidates: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_items = [dict(_as_mapping(candidate)) for candidate in candidates if isinstance(candidate, Mapping)]
    manifest_payload = dict(_as_mapping(manifest))
    rows_by_id, duplicate_ids = _decision_rows_by_repair_id(manifest_payload)
    manifest_provided = bool(manifest_payload)
    candidate_ids = {_text(candidate.get("repair_correlation_id")) for candidate in candidate_items}
    unexpected_ids = sorted(repair_id for repair_id in rows_by_id if repair_id and repair_id not in candidate_ids)
    items: list[dict[str, Any]] = []
    excluded_repair_ids: list[str] = []
    excluded_event_run_ids: list[Any] = []
    invalid_items: list[dict[str, Any]] = []
    missing_items: list[dict[str, Any]] = []
    pending_items: list[dict[str, Any]] = []

    for candidate in candidate_items:
        repair_id = _text(candidate.get("repair_correlation_id"))
        raw_decision = rows_by_id.get(repair_id)
        decision = _text(_as_mapping(raw_decision).get("decision")) if raw_decision else ""
        allowed_decisions = [
            _text(option.get("decision"))
            for option in _lineage_decision_options(candidate)
            if option.get("allowed_for_repair_type")
        ]
        state = ""
        blocks_current_apply = True
        enters_current_apply_scope = False
        reason = _text(_as_mapping(raw_decision).get("reason")) if raw_decision else ""
        if raw_decision is None:
            state = "decision_missing"
        elif not decision or decision == "undecided":
            state = "undecided"
        elif decision not in LINEAGE_DECISIONS:
            state = "invalid_decision"
        elif decision not in allowed_decisions:
            state = "decision_not_allowed_for_repair_type"
        elif decision == "exclude_sample":
            state = "resolved_excluded_from_current_apply"
            blocks_current_apply = False
            excluded_repair_ids.append(repair_id)
            excluded_event_run_ids.append(candidate.get("event_run_id"))
        elif decision == "confirm_repair":
            state = "confirmed_repair_pending_lineage_apply"
            pending_items.append(
                {
                    "repair_correlation_id": repair_id,
                    "decision": decision,
                    "state": state,
                    "reason": reason,
                }
            )
        elif decision == "wait_for_new_fact":
            state = "pending_wait_for_new_fact"
            pending_items.append(
                {
                    "repair_correlation_id": repair_id,
                    "decision": decision,
                    "state": state,
                    "reason": reason,
                }
            )
        elif decision == "manual_rebuild_fact_snapshot":
            state = "pending_manual_rebuild"
            pending_items.append(
                {
                    "repair_correlation_id": repair_id,
                    "decision": decision,
                    "state": state,
                    "reason": reason,
                }
            )

        audit_item = {
            "repair_correlation_id": repair_id,
            "decision": decision or "missing",
            "state": state,
            "reason": reason,
            "slug": candidate.get("slug"),
            "event_date": candidate.get("event_date"),
            "event_run_id": candidate.get("event_run_id"),
            "repair_type": candidate.get("repair_type"),
            "allowed_decisions": allowed_decisions,
            "blocks_current_apply": bool(blocks_current_apply),
            "enters_current_apply_scope": bool(enters_current_apply_scope),
        }
        items.append(audit_item)
        if state in {"decision_missing", "undecided"}:
            missing_items.append(audit_item)
        elif state in {"invalid_decision", "decision_not_allowed_for_repair_type"}:
            invalid_items.append(audit_item)

    blocked_count = sum(1 for item in items if item.get("blocks_current_apply"))
    state_counts = _counter([item.get("state") for item in items])
    invalid_or_missing = bool(invalid_items or missing_items or duplicate_ids)
    return {
        "schema": LINEAGE_DECISION_MANIFEST_AUDIT_SCHEMA,
        "read_only": True,
        "execution_mode": "lineage_repair_decision_manifest_read_only_audit",
        "manifest_provided": manifest_provided,
        "manifest_schema": manifest_payload.get("schema"),
        "candidate_count": len(candidate_items),
        "decision_count": len(rows_by_id),
        "state_counts": state_counts,
        "excluded_candidate_count": len(excluded_repair_ids),
        "pending_resolution_count": len(pending_items),
        "invalid_decision_count": len(invalid_items),
        "missing_decision_count": len(missing_items),
        "duplicate_decision_count": len(duplicate_ids),
        "unexpected_decision_count": len(unexpected_ids),
        "blocked_candidate_count": blocked_count,
        "all_candidates_resolved_for_current_apply": bool(candidate_items) and blocked_count == 0,
        "blocking_for_current_apply": bool(blocked_count or invalid_or_missing),
        "excluded_repair_correlation_ids": excluded_repair_ids,
        "excluded_event_run_ids": excluded_event_run_ids,
        "pending_resolution_items": pending_items,
        "invalid_decisions": invalid_items,
        "missing_decisions": missing_items,
        "duplicate_repair_correlation_ids": duplicate_ids,
        "unexpected_repair_correlation_ids": unexpected_ids,
        "items": items,
        "apply_scope_filter": {
            "exclude_repair_correlation_ids": excluded_repair_ids,
            "exclude_event_run_ids": excluded_event_run_ids,
            "filter_reason": "operator_manifest_exclude_sample",
        },
        "guards": {
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
            "apply_current_batch_scope_after_exclusions"
            if candidate_items and blocked_count == 0
            else "repair_or_exclude_remaining_lineage_candidates"
            if candidate_items
            else "no_lineage_decision_manifest_required"
        ),
    }


def _lineage_decision_readiness_counts(manifest_audit: Mapping[str, Any]) -> dict[str, int]:
    counts = {
        "confirm_repair": 0,
        "exclude_sample": 0,
        "wait_for_new_fact": 0,
        "manual_rebuild_fact_snapshot": 0,
        "undecided": 0,
        "missing": 0,
        "invalid": 0,
        "not_allowed": 0,
    }
    manifest_provided = bool(manifest_audit.get("manifest_provided"))
    for item in manifest_audit.get("items") or []:
        item_map = _as_mapping(item)
        decision = _text(item_map.get("decision"))
        state = _text(item_map.get("state"))
        if state == "decision_missing":
            bucket = "missing" if manifest_provided else "undecided"
        elif state == "undecided":
            bucket = "undecided"
        elif state == "invalid_decision":
            bucket = "invalid"
        elif state == "decision_not_allowed_for_repair_type":
            bucket = "not_allowed"
        elif decision in LINEAGE_DECISIONS:
            bucket = decision
        else:
            bucket = "invalid"
        counts[bucket] = int(counts.get(bucket, 0)) + 1
    return counts


def _lineage_repair_decision_by_id(manifest: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    rows_by_id, _duplicates = _decision_rows_by_repair_id(_as_mapping(manifest))
    return rows_by_id


def _lineage_repair_apply_item(candidate: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    repair_id = _text(candidate.get("repair_correlation_id"))
    target_row_id = _text(candidate.get("target_row_id"))
    candidate_fact_snapshot_id = _text(candidate.get("candidate_fact_snapshot_id"))
    repair_type = _text(candidate.get("repair_type"))
    missing_fields = [_text(value) for value in candidate.get("missing_fields") or [] if _text(value)]
    blockers: list[str] = []
    if repair_type != "legacy_fact_snapshot_lineage_repair":
        blockers.append("unsupported_repair_type_for_merge_fill")
    if not target_row_id:
        blockers.append("target_row_id_missing")
    if not candidate_fact_snapshot_id:
        blockers.append("candidate_fact_snapshot_id_missing")
    if "fact_snapshot_id_missing" not in missing_fields:
        blockers.append("fact_snapshot_id_missing_not_declared")
    return {
        "schema": "daily_review_lineage_repair_apply_item_v1",
        "read_only": True,
        "execution_mode": "lineage_repair_apply_item_no_apply",
        "repair_correlation_id": repair_id,
        "decision": _text(decision.get("decision")),
        "decision_reason": _text(decision.get("reason")),
        "slug": candidate.get("slug"),
        "event_date": candidate.get("event_date"),
        "event_run_id": candidate.get("event_run_id"),
        "repair_type": repair_type,
        "target_table": candidate.get("target_table"),
        "target_row_id": target_row_id,
        "candidate_fact_snapshot_id": candidate_fact_snapshot_id,
        "candidate_fact_snapshot_id_source": candidate.get("candidate_fact_snapshot_id_source"),
        "missing_fields": missing_fields,
        "expected_merge_fill": {
            "mode": "merge_fill_only_if_missing",
            "target": "event_fact_history.fact_json",
            "paths": [
                {
                    "json_path": "fact_snapshot_id",
                    "value": candidate_fact_snapshot_id,
                    "overwrite_allowed": False,
                },
                {
                    "json_path": "lineage_state.fact_snapshot_id",
                    "value": candidate_fact_snapshot_id,
                    "overwrite_allowed": False,
                },
            ],
        },
        "sql_shape_preview": {
            "statement_type": "UPDATE",
            "table": "event_fact_history",
            "where": "id = :target_row_id AND fact_json lineage fields are missing",
            "sets": ["fact_json = merge_fill_json(fact_json, expected_merge_fill)"],
            "literal_sql_included": False,
            "reason": "actual apply implementation must use structured JSON parsing and merge-fill validation",
        },
        "item_blockers": blockers,
        "ready_for_controlled_apply_after_preflight": not blockers,
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


def build_lineage_repair_apply_contract(
    batch_plan: Mapping[str, Any],
    *,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
    backup_path: Path | str | None = None,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    writer_quiescent: bool = False,
) -> dict[str, Any]:
    candidates = [dict(_as_mapping(candidate)) for candidate in batch_plan.get("lineage_repair_candidates") or []]
    decision_by_id = _lineage_repair_decision_by_id(lineage_decision_manifest)
    confirmed_items: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    for candidate in candidates:
        repair_id = _text(candidate.get("repair_correlation_id"))
        decision = _as_mapping(decision_by_id.get(repair_id))
        decision_value = _text(decision.get("decision"))
        if decision_value == "confirm_repair":
            confirmed_items.append(_lineage_repair_apply_item(candidate, decision))
        elif decision_value:
            skipped_items.append(
                {
                    "repair_correlation_id": repair_id,
                    "decision": decision_value,
                    "reason": "not_a_lineage_repair_apply_decision",
                }
            )

    backup_state = _file_state(backup_path)
    backup_ok = (
        not confirmed_items
        or (
            bool(backup_state.get("exists"))
            and int(backup_state.get("size_bytes") or 0) > 0
            and str(backup_state.get("path") or "") != _text(batch_plan.get("db"))
        )
    )
    item_blockers = sorted(
        {
            blocker
            for item in confirmed_items
            for blocker in item.get("item_blockers") or []
            if _text(blocker)
        }
    )
    manifest_audit = _as_mapping(batch_plan.get("lineage_decision_manifest_audit"))
    preflight_checks = [
        {
            "name": "confirm_repair_decisions",
            "status": "pass" if confirmed_items else "not_required",
            "hard_blocker": False,
            "reason": "confirmed_repair_items_present" if confirmed_items else "no_confirm_repair_decisions",
            "confirmed_repair_count": len(confirmed_items),
        },
        {
            "name": "candidate_source_not_truncated",
            "status": "pass" if not batch_plan.get("lineage_repair_candidates_truncated") else "required",
            "hard_blocker": bool(batch_plan.get("lineage_repair_candidates_truncated")),
            "reason": (
                "all_candidates_available"
                if not batch_plan.get("lineage_repair_candidates_truncated")
                else "increase_max_items_output_before_repair_contract"
            ),
        },
        {
            "name": "manifest_valid_for_repair_scope",
            "status": "pass" if not manifest_audit.get("invalid_decision_count") else "required",
            "hard_blocker": bool(manifest_audit.get("invalid_decision_count")),
            "reason": (
                "manifest_has_no_invalid_decisions"
                if not manifest_audit.get("invalid_decision_count")
                else "manifest_contains_invalid_or_disallowed_decision"
            ),
        },
        {
            "name": "repair_item_validation",
            "status": "pass" if not item_blockers else "required",
            "hard_blocker": bool(item_blockers),
            "reason": "all_repair_items_valid" if not item_blockers else "repair_items_have_blockers",
            "item_blockers": item_blockers,
        },
        {
            "name": "apply_flag",
            "status": "pass" if allow_apply else "required",
            "hard_blocker": bool(confirmed_items) and not allow_apply,
            "reason": "allow_apply_true" if allow_apply else "lineage_repair_apply_requires_explicit_apply_flag",
        },
        {
            "name": "confirmation_phrase",
            "status": "pass" if confirmation_phrase == LINEAGE_REPAIR_CONFIRMATION_PHRASE else "required",
            "hard_blocker": bool(confirmed_items) and confirmation_phrase != LINEAGE_REPAIR_CONFIRMATION_PHRASE,
            "reason": (
                "confirmation_phrase_matches"
                if confirmation_phrase == LINEAGE_REPAIR_CONFIRMATION_PHRASE
                else "confirmation_phrase_missing_or_mismatched"
            ),
        },
        {
            "name": "fresh_archive_db_backup",
            "status": "pass" if backup_ok else "required",
            "hard_blocker": bool(confirmed_items) and not backup_ok,
            "reason": "backup_path_exists_and_is_not_db" if backup_ok else "backup_path_required_before_lineage_repair_apply",
            "backup_path": backup_state.get("path"),
            "backup_exists": backup_state.get("exists"),
            "backup_size_bytes": backup_state.get("size_bytes"),
        },
        {
            "name": "writer_quiescence",
            "status": "pass" if writer_quiescent else "required",
            "hard_blocker": bool(confirmed_items) and not writer_quiescent,
            "reason": "caller_asserted_writer_quiescence" if writer_quiescent else "requires_explicit_writer_quiescent_assertion",
        },
        {
            "name": "historical_backfill",
            "status": "blocked_by_design",
            "hard_blocker": False,
            "reason": "lineage_repair_contract_targets_confirmed_rows_only_not_bulk_backfill",
        },
    ]
    apply_blockers = [str(item.get("name")) for item in preflight_checks if item.get("hard_blocker")]
    return {
        "schema": LINEAGE_REPAIR_APPLY_CONTRACT_SCHEMA,
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "lineage_repair_apply_contract_no_apply",
        "apply_supported": True,
        "applied": False,
        "db": batch_plan.get("db"),
        "confirmation_phrase_required": LINEAGE_REPAIR_CONFIRMATION_PHRASE,
        "repair_item_count": len(confirmed_items),
        "skipped_decision_count": len(skipped_items),
        "repair_items": confirmed_items,
        "skipped_decisions": skipped_items,
        "preflight_checks": preflight_checks,
        "apply_blockers": apply_blockers,
        "post_apply_verification_commands": [
            "python3 scripts/daily_review_persistence_batch_plan.py --since-days 45 --max-items-output 20 --lineage-decision-manifest <manifest.json>",
            "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20",
            "python3 scripts/check_ssot_alignment.py --immutable",
        ],
        "controlled_apply_command": (
            "python3 scripts/daily_review_lineage_repair_apply.py --lineage-decision-manifest <manifest.json> "
            f"--apply --confirm {LINEAGE_REPAIR_CONFIRMATION_PHRASE} --backup <backup.bak> --writer-quiescent"
        ),
        "boundary": {
            "targeted_legacy_fact_lineage_repair_only": True,
            "bulk_history_backfill_included": False,
            "daily_review_persistence_included": False,
            "schema_apply_included": False,
            "launchd_restart_included": False,
            "orders_or_trade_decisions_included": False,
        },
        "guards": {
            "executes_sql": False,
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "run_controlled_lineage_repair_apply_only_after_user_confirmation"
            if confirmed_items
            else "no_lineage_repair_apply_contract_needed_without_confirm_repair"
        ),
    }


def build_lineage_decision_apply_readiness(
    batch_plan: Mapping[str, Any],
    lineage_repair_apply_contract: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_audit = _as_mapping(batch_plan.get("lineage_decision_manifest_audit"))
    schema_status = _as_mapping(batch_plan.get("daily_review_persistence_schema_status"))
    lineage_readiness = _as_mapping(batch_plan.get("lineage_readiness_state"))
    decision_counts = _lineage_decision_readiness_counts(manifest_audit)
    candidate_count = int(manifest_audit.get("candidate_count") or 0)
    manifest_provided = bool(manifest_audit.get("manifest_provided"))
    undecided_or_missing = (
        int(decision_counts.get("undecided") or 0)
        + int(decision_counts.get("missing") or 0)
        + int(decision_counts.get("invalid") or 0)
        + int(decision_counts.get("not_allowed") or 0)
        + int(manifest_audit.get("duplicate_decision_count") or 0)
    )
    operator_decisions_recorded = bool(candidate_count == 0 or (manifest_provided and undecided_or_missing == 0))
    pending_resolution_count = int(manifest_audit.get("pending_resolution_count") or 0)
    repair_item_count = int(lineage_repair_apply_contract.get("repair_item_count") or 0)
    repair_apply_blockers = [
        _text(value)
        for value in lineage_repair_apply_contract.get("apply_blockers") or []
        if _text(value)
    ]
    repair_item_blockers = sorted(
        {
            _text(blocker)
            for item in lineage_repair_apply_contract.get("repair_items") or []
            for blocker in _as_mapping(item).get("item_blockers") or []
            if _text(blocker)
        }
    )
    persistence_blockers = [
        _text(value)
        for value in batch_plan.get("apply_blockers") or []
        if _text(value)
    ]
    schema_ready = bool(schema_status.get("ok"))
    effective_settled_count = int(batch_plan.get("effective_settled_event_count") or 0)
    planned_review_count = int(batch_plan.get("planned_review_count") or 0)
    planned_calibration_record_count = int(batch_plan.get("planned_calibration_record_count") or 0)
    ready_after_confirmation = bool(batch_plan.get("can_apply_after_schema_and_confirmation"))
    repair_preflight_ready = bool(repair_item_count and not repair_item_blockers)
    repair_apply_ready_now = bool(repair_item_count and not repair_apply_blockers)
    remaining_hard_blockers = [
        blocker
        for blocker in persistence_blockers
        if blocker != "explicit_user_confirmation_required"
    ]

    if ready_after_confirmation:
        next_action = "run_controlled_daily_review_persistence_apply_after_explicit_confirmation"
    elif repair_item_count:
        next_action = "run_controlled_lineage_repair_apply_then_rerun_batch_plan"
    elif candidate_count and not operator_decisions_recorded:
        next_action = "complete_lineage_decision_manifest_or_exclude_samples"
    elif pending_resolution_count:
        next_action = "resolve_pending_lineage_decisions_before_daily_review_persistence"
    else:
        next_action = "fix_remaining_batch_plan_or_schema_blockers"

    return {
        "schema": LINEAGE_DECISION_APPLY_READINESS_SCHEMA,
        "read_only": True,
        "execution_mode": "lineage_decision_apply_readiness_no_apply",
        "raw_scope": {
            "settled_event_count": int(batch_plan.get("settled_event_count") or 0),
            "planned_review_count": int(batch_plan.get("raw_planned_review_count") or 0),
            "planned_calibration_record_count": int(batch_plan.get("raw_planned_calibration_record_count") or 0),
            "lineage_repair_candidate_count": int(batch_plan.get("lineage_repair_candidate_count") or 0),
            "lineage_apply_blocker_counts": dict(batch_plan.get("raw_lineage_apply_blocker_counts") or {}),
        },
        "effective_scope": {
            "settled_event_count": effective_settled_count,
            "planned_review_count": planned_review_count,
            "planned_calibration_record_count": planned_calibration_record_count,
            "excluded_settled_event_count": int(batch_plan.get("excluded_settled_event_count") or 0),
            "excluded_candidate_count": int(manifest_audit.get("excluded_candidate_count") or 0),
            "effective_lineage_repair_candidate_count": int(
                batch_plan.get("effective_lineage_repair_candidate_count") or 0
            ),
            "lineage_apply_blocker_counts": dict(batch_plan.get("lineage_apply_blocker_counts") or {}),
        },
        "operator_decision_counts": decision_counts,
        "decision_manifest": {
            "manifest_provided": manifest_provided,
            "manifest_schema": manifest_audit.get("manifest_schema"),
            "candidate_count": candidate_count,
            "decision_count": int(manifest_audit.get("decision_count") or 0),
            "pending_resolution_count": pending_resolution_count,
            "invalid_decision_count": int(manifest_audit.get("invalid_decision_count") or 0),
            "missing_decision_count": int(manifest_audit.get("missing_decision_count") or 0),
            "duplicate_decision_count": int(manifest_audit.get("duplicate_decision_count") or 0),
            "unexpected_decision_count": int(manifest_audit.get("unexpected_decision_count") or 0),
            "operator_decisions_recorded": operator_decisions_recorded,
            "excluded_event_run_ids": list(manifest_audit.get("excluded_event_run_ids") or []),
            "pending_resolution_items": list(manifest_audit.get("pending_resolution_items") or []),
        },
        "lineage_repair": {
            "repair_required": bool(lineage_readiness.get("repair_required")),
            "repair_item_count": repair_item_count,
            "repair_preflight_ready": repair_preflight_ready,
            "repair_apply_ready_now": repair_apply_ready_now,
            "repair_apply_blockers": repair_apply_blockers,
            "repair_item_blockers": repair_item_blockers,
            "contract_schema": lineage_repair_apply_contract.get("schema"),
            "contract_execution_mode": lineage_repair_apply_contract.get("execution_mode"),
        },
        "daily_review_persistence": {
            "ready_now": False,
            "ready_after_explicit_confirmation": ready_after_confirmation,
            "schema_ready": schema_ready,
            "planned_review_count_matches_effective_scope": bool(
                effective_settled_count > 0 and planned_review_count == effective_settled_count
            ),
            "planned_calibration_record_count_matches_effective_scope": bool(
                effective_settled_count > 0 and planned_calibration_record_count == effective_settled_count
            ),
            "remaining_hard_blockers": remaining_hard_blockers,
            "apply_blockers": persistence_blockers,
        },
        "gates": [
            {
                "gate": "daily_review_persistence_schema",
                "ready": schema_ready,
                "blocked": not schema_ready,
                "reason": "schema_ready" if schema_ready else "daily_review_persistence_schema_missing",
            },
            {
                "gate": "lineage_decision_manifest",
                "ready": operator_decisions_recorded,
                "blocked": not operator_decisions_recorded,
                "reason": (
                    "no_lineage_repair_candidates"
                    if candidate_count == 0
                    else "operator_decisions_recorded"
                    if operator_decisions_recorded
                    else "lineage_decision_manifest_missing_or_incomplete"
                ),
            },
            {
                "gate": "lineage_repair_apply_contract",
                "ready": repair_apply_ready_now,
                "preflight_ready": repair_preflight_ready,
                "blocked": bool(repair_item_count and repair_apply_blockers),
                "reason": (
                    "no_lineage_repair_apply_required"
                    if not repair_item_count
                    else "lineage_repair_apply_preconditions_satisfied"
                    if repair_apply_ready_now
                    else "lineage_repair_apply_requires_explicit_apply_flag_confirmation_backup_and_quiescence"
                ),
            },
            {
                "gate": "daily_review_persistence_apply",
                "ready": ready_after_confirmation,
                "ready_now": False,
                "blocked": not ready_after_confirmation,
                "reason": (
                    "daily_review_persistence_ready_after_explicit_confirmation"
                    if ready_after_confirmation
                    else "daily_review_persistence_has_remaining_blockers"
                ),
            },
        ],
        "guards": {
            "writes_archive_db": False,
            "writes_event_fact_history": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_action": next_action,
        "next_boundary": (
            "controlled_daily_review_persistence_apply_after_confirmation"
            if ready_after_confirmation
            else "lineage_repair_or_manifest_resolution_before_persistence"
            if candidate_count or repair_item_count
            else "schema_or_batch_plan_fix_before_persistence"
        ),
    }


def load_lineage_decision_manifest_file(path: Path | str | None) -> Mapping[str, Any]:
    if not path:
        return {}
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(str(manifest_path))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, Mapping) else {}


def _summarize_compact_items(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    all_blockers: list[str] = []
    all_warnings: list[str] = []
    all_lineage_apply_blockers: list[str] = []
    aggregate_row_counts: dict[str, int] = {}
    lineage_repair_candidates: list[dict[str, Any]] = []
    first_bad_links: list[str] = []
    review_ids: list[str] = []
    for item in items:
        all_blockers.extend(str(value) for value in item.get("report_blockers") or [])
        all_blockers.extend(str(value) for value in item.get("persistence_plan_blockers") or [])
        all_warnings.extend(str(value) for value in item.get("report_warnings") or [])
        all_lineage_apply_blockers.extend(str(value) for value in item.get("lineage_apply_blockers") or [])
        repair_candidate = _as_mapping(item.get("lineage_repair_candidate"))
        if repair_candidate:
            lineage_repair_candidates.append(dict(repair_candidate))
        _sum_row_counts(aggregate_row_counts, _as_mapping(item.get("persistence_row_counts")))
        first_bad_links.append(str(item.get("first_bad_link") or ""))
        review_id = str(item.get("review_id") or "")
        if review_id.strip():
            review_ids.append(review_id)
    return {
        "blockers": sorted({value for value in all_blockers if str(value or "").strip()}),
        "warnings": sorted({value for value in all_warnings if str(value or "").strip()}),
        "lineage_apply_blockers": all_lineage_apply_blockers,
        "lineage_apply_blocker_counts": _counter(all_lineage_apply_blockers),
        "lineage_repair_candidates": lineage_repair_candidates,
        "lineage_repair_type_counts": _counter(
            [str(item.get("repair_type") or "") for item in lineage_repair_candidates]
        ),
        "aggregate_row_counts": aggregate_row_counts,
        "first_bad_link_counts": _counter(first_bad_links),
        "review_ids": review_ids,
    }


def _compact_item(
    *,
    event: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    review = _as_mapping(report.get("review"))
    compact_plan = _as_mapping(report.get("daily_review_persistence_plan"))
    fact_snapshot_id = str(review.get("fact_snapshot_id") or compact_plan.get("fact_snapshot_id") or "")
    lineage_apply_blockers: list[str] = []
    if not fact_snapshot_id:
        lineage_apply_blockers.append("fact_snapshot_id_missing")
    lineage_repair_candidate = _lineage_repair_candidate(
        event=event,
        report=report,
        lineage_apply_blockers=lineage_apply_blockers,
    )
    return {
        "slug": str(event.get("slug") or ""),
        "event_date": _date_only(event.get("event_date")),
        "event_run_id": event.get("id"),
        "settlement_actual": event.get("settlement_actual"),
        "report_ok": bool(report.get("ok")),
        "report_blockers": list(report.get("blockers") or []),
        "report_warnings": list(report.get("warnings") or [])[:10],
        "fact": dict(_as_mapping(report.get("fact"))),
        "review_id": str(review.get("review_id") or compact_plan.get("review_id") or ""),
        "fact_snapshot_id": fact_snapshot_id,
        "first_bad_link": str(review.get("first_bad_link") or ""),
        "target_hit": _as_mapping(review.get("review_json")).get("target_hit"),
        "lineage_apply_blockers": lineage_apply_blockers,
        "lineage_ready_for_apply": not lineage_apply_blockers,
        "lineage_repair_required": bool(lineage_repair_candidate),
        "lineage_repair_candidate": lineage_repair_candidate,
        "persistence_plan_ok": compact_plan.get("ok") is not False,
        "persistence_plan_blockers": list(compact_plan.get("blockers") or []),
        "persistence_row_counts": dict(_as_mapping(compact_plan.get("row_counts"))),
        "persistence_row_preview": dict(_as_mapping(compact_plan.get("row_preview"))),
    }


def build_batch_plan(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    include_ui_snapshot_audit: bool = False,
    include_replay_audit: bool = False,
    ui_snapshot_mode: str = "summary",
    ui_snapshot_city_filter: str = "",
    since_hours: float = 24 * 90,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path)
    effective_since_date = _date_only(since_date) or daily_review_coverage_audit.since_date_from_days(
        int(since_days)
    )
    with _connect_readonly(db) as conn:
        schema_status = daily_review_persistence_schema_status(conn)
        settled_events = daily_review_coverage_audit.load_settled_events(
            conn,
            since_date=effective_since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
        )

    items: list[dict[str, Any]] = []
    for event in settled_events:
        settlement_actual = event.get("settlement_actual")
        report = daily_weather_review_report.build_report(
            db,
            slug=str(event.get("slug") or ""),
            event_date=_date_only(event.get("event_date")),
            include_ui_snapshot_audit=bool(include_ui_snapshot_audit),
            include_replay_audit=bool(include_replay_audit),
            ui_snapshot_mode=ui_snapshot_mode,
            ui_snapshot_city_filter=ui_snapshot_city_filter,
            settlement_override={
                "settlement_actual": settlement_actual,
                "settlement_actual_source": "event_runs.settlement_actual",
                "event_run_id": event.get("id"),
            },
            since_hours=since_hours,
            limit=100,
        )
        item = _compact_item(event=event, report=report)
        items.append(item)

    raw_summary = _summarize_compact_items(items)
    all_lineage_repair_candidates = list(raw_summary.get("lineage_repair_candidates") or [])
    lineage_decision_manifest_audit = audit_lineage_repair_decision_manifest(
        all_lineage_repair_candidates,
        lineage_decision_manifest,
    )
    excluded_repair_ids = {
        _text(value)
        for value in lineage_decision_manifest_audit.get("excluded_repair_correlation_ids") or []
        if _text(value)
    }
    effective_items: list[dict[str, Any]] = []
    excluded_items: list[dict[str, Any]] = []
    for item in items:
        repair_id = _item_repair_correlation_id(item)
        if repair_id and repair_id in excluded_repair_ids:
            excluded_items.append(item)
        else:
            effective_items.append(item)

    effective_summary = _summarize_compact_items(effective_items)
    settled_count = len(settled_events)
    effective_settled_count = len(effective_items)
    excluded_settled_count = len(excluded_items)
    aggregate_row_counts = dict(effective_summary.get("aggregate_row_counts") or {})
    raw_aggregate_row_counts = dict(raw_summary.get("aggregate_row_counts") or {})
    planned_review_count = int(aggregate_row_counts.get("daily_weather_reviews") or 0)
    planned_calibration_record_count = int(aggregate_row_counts.get("daily_calibration_records") or 0)
    raw_planned_review_count = int(raw_aggregate_row_counts.get("daily_weather_reviews") or 0)
    raw_planned_calibration_record_count = int(raw_aggregate_row_counts.get("daily_calibration_records") or 0)
    plan_blockers = list(effective_summary.get("blockers") or [])
    schema_ready = bool(schema_status.get("ok"))
    apply_blockers = ["explicit_user_confirmation_required"]
    if not schema_ready:
        apply_blockers.append("daily_review_persistence_schema_missing")
    if plan_blockers:
        apply_blockers.append("batch_plan_has_blockers")
    effective_lineage_apply_blockers = list(effective_summary.get("lineage_apply_blockers") or [])
    if effective_lineage_apply_blockers:
        apply_blockers.append("batch_plan_lineage_incomplete")
    if effective_settled_count and planned_review_count < effective_settled_count:
        apply_blockers.append("batch_plan_incomplete_review_rows")
    if effective_settled_count and planned_calibration_record_count < effective_settled_count:
        apply_blockers.append("batch_plan_incomplete_calibration_record_rows")
    if not settled_count:
        apply_blockers.append("no_settled_events_in_scope")
    if settled_count and not effective_settled_count:
        apply_blockers.append("no_effective_settled_events_after_exclusions")
    if lineage_decision_manifest_audit.get("manifest_provided"):
        if (
            lineage_decision_manifest_audit.get("invalid_decision_count")
            or lineage_decision_manifest_audit.get("missing_decision_count")
            or lineage_decision_manifest_audit.get("duplicate_decision_count")
        ):
            apply_blockers.append("lineage_decision_manifest_incomplete_or_invalid")
        if lineage_decision_manifest_audit.get("pending_resolution_count"):
            apply_blockers.append("lineage_decision_manifest_pending_resolution")
    visible_limit = max(0, int(max_items_output))
    visible_items = effective_items[:visible_limit] if visible_limit else []
    visible_excluded_items = excluded_items[:visible_limit] if visible_limit else []
    review_ids = list(effective_summary.get("review_ids") or [])
    lineage_apply_blocker_counts = dict(effective_summary.get("lineage_apply_blocker_counts") or {})
    raw_lineage_apply_blocker_counts = dict(raw_summary.get("lineage_apply_blocker_counts") or {})
    effective_lineage_repair_candidates = list(effective_summary.get("lineage_repair_candidates") or [])
    lineage_repair_type_counts = dict(raw_summary.get("lineage_repair_type_counts") or {})
    effective_lineage_repair_type_counts = dict(effective_summary.get("lineage_repair_type_counts") or {})
    visible_repair_candidates = all_lineage_repair_candidates[:visible_limit] if visible_limit else []
    lineage_ready_for_confirmed_apply = not effective_lineage_apply_blockers and not bool(
        lineage_decision_manifest_audit.get("blocking_for_current_apply")
        and effective_lineage_repair_candidates
    )
    lineage_operator_packet = build_lineage_repair_operator_packet(
        visible_repair_candidates,
        total_settled_event_count=settled_count,
    )
    batch_ok = not plan_blockers and (not effective_settled_count or planned_review_count == effective_settled_count)
    final_apply_blockers = sorted(set(apply_blockers))
    can_apply_after_schema_and_confirmation = bool(
        final_apply_blockers == ["explicit_user_confirmation_required"]
        and schema_ready
        and not plan_blockers
        and lineage_ready_for_confirmed_apply
        and effective_settled_count > 0
        and planned_review_count == effective_settled_count
        and planned_calibration_record_count == effective_settled_count
    )
    result = {
        "ok": batch_ok,
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "execution_mode": "batch_no_apply_persistence_plan",
        "db": str(db),
        "since_date": effective_since_date,
        "market_scope": str(market_scope or ""),
        "settled_event_count": settled_count,
        "effective_settled_event_count": effective_settled_count,
        "excluded_settled_event_count": excluded_settled_count,
        "planned_review_count": planned_review_count,
        "raw_planned_review_count": raw_planned_review_count,
        "planned_calibration_record_count": planned_calibration_record_count,
        "raw_planned_calibration_record_count": raw_planned_calibration_record_count,
        "planned_candidate_count": int(aggregate_row_counts.get("bias_correction_candidates") or 0),
        "planned_parameter_event_count": int(aggregate_row_counts.get("parameter_change_events") or 0),
        "aggregate_row_counts": aggregate_row_counts,
        "raw_aggregate_row_counts": raw_aggregate_row_counts,
        "first_bad_link_counts": dict(effective_summary.get("first_bad_link_counts") or {}),
        "review_ids": review_ids[:50],
        "review_ids_truncated": len(review_ids) > 50,
        "daily_review_persistence_schema_status": schema_status,
        "can_apply_now": False,
        "can_apply_after_schema_and_confirmation": can_apply_after_schema_and_confirmation,
        "lineage_apply_blocker_counts": lineage_apply_blocker_counts,
        "raw_lineage_apply_blocker_counts": raw_lineage_apply_blocker_counts,
        "lineage_repair_candidate_count": len(all_lineage_repair_candidates),
        "effective_lineage_repair_candidate_count": len(effective_lineage_repair_candidates),
        "lineage_repair_type_counts": lineage_repair_type_counts,
        "effective_lineage_repair_type_counts": effective_lineage_repair_type_counts,
        "lineage_repair_candidates": visible_repair_candidates,
        "lineage_repair_candidates_truncated": len(all_lineage_repair_candidates) > len(visible_repair_candidates),
        "lineage_repair_operator_packet": lineage_operator_packet,
        "lineage_decision_manifest_audit": lineage_decision_manifest_audit,
        "lineage_readiness_state": {
            "schema": LINEAGE_READINESS_SCHEMA,
            "ready_for_confirmed_apply": lineage_ready_for_confirmed_apply,
            "repair_required": bool(effective_lineage_repair_candidates),
            "repair_candidate_count": len(all_lineage_repair_candidates),
            "effective_repair_candidate_count": len(effective_lineage_repair_candidates),
            "operator_decision_required": bool(effective_lineage_repair_candidates),
            "operator_packet_schema": LINEAGE_OPERATOR_PACKET_SCHEMA,
            "decision_manifest_schema": LINEAGE_DECISION_MANIFEST_SCHEMA,
            "lineage_apply_blocker_counts": lineage_apply_blocker_counts,
            "raw_lineage_apply_blocker_counts": raw_lineage_apply_blocker_counts,
            "repair_type_counts": lineage_repair_type_counts,
            "effective_repair_type_counts": effective_lineage_repair_type_counts,
            "hard_stop_reason": (
                "fact_snapshot_id_missing_blocks_daily_review_persistence"
                if effective_lineage_apply_blockers
                else ""
            ),
            "owner_boundary": (
                "daily_review_persistence_writer consumes fact_snapshot_id; "
                "weather_fact_snapshot_writer or an explicit lineage repair contract owns legacy fact repair"
            ),
            "can_auto_repair": False,
            "requires_explicit_user_confirmation": bool(all_lineage_repair_candidates),
            "excluded_candidate_count": lineage_decision_manifest_audit.get("excluded_candidate_count"),
        },
        "apply_blockers": final_apply_blockers,
        "blockers": plan_blockers,
        "warnings": list(effective_summary.get("warnings") or []),
        "items": visible_items,
        "items_truncated": len(effective_items) > len(visible_items),
        "excluded_items": visible_excluded_items,
        "excluded_items_truncated": len(excluded_items) > len(visible_excluded_items),
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_daily_calibration_records": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "review_lineage_repair_candidates_before_daily_review_apply"
            if effective_lineage_apply_blockers
            else "operator_manifest_excluded_all_samples_wait_for_new_scope_or_include_confirmed_repair"
            if settled_count and not effective_settled_count
            else "review_batch_plan_then_apply_schema_and_persistence_with_confirmation"
            if not plan_blockers and planned_review_count
            else "fix_first_batch_plan_blocker_or_wait_for_settlement_scope"
        ),
    }
    result["lineage_repair_apply_contract"] = build_lineage_repair_apply_contract(
        result,
        lineage_decision_manifest=lineage_decision_manifest,
    )
    result["lineage_decision_apply_readiness"] = build_lineage_decision_apply_readiness(
        result,
        _as_mapping(result.get("lineage_repair_apply_contract")),
    )
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--include-ui-snapshot-audit", action="store_true")
    parser.add_argument("--include-replay-audit", action="store_true")
    parser.add_argument("--ui-snapshot-mode", choices=("full", "summary"), default="summary")
    parser.add_argument("--ui-snapshot-city-filter", default="")
    parser.add_argument("--since-hours", type=float, default=24 * 90)
    parser.add_argument(
        "--lineage-decision-manifest",
        type=Path,
        default=None,
        help="Read a no-apply lineage decision manifest JSON and filter current apply scope.",
    )
    parser.add_argument(
        "--emit-lineage-decision-manifest-template",
        action="store_true",
        help="Print a no-apply lineage decision manifest template for the current settled scope instead of the batch plan.",
    )
    parser.add_argument(
        "--emit-lineage-repair-apply-contract",
        action="store_true",
        help="Print a no-apply controlled lineage repair apply contract for confirm_repair decisions instead of the batch plan.",
    )
    parser.add_argument("--lineage-repair-backup", type=Path, default=None)
    parser.add_argument("--lineage-repair-confirm", default="")
    parser.add_argument("--lineage-repair-writer-quiescent", action="store_true")
    parser.add_argument("--decided-by", default="", help="Optional operator label to prefill in emitted manifest rows.")
    parser.add_argument("--decided-at", default="", help="Optional timestamp to prefill in emitted manifest rows.")
    args = parser.parse_args(argv)
    lineage_decision_manifest = load_lineage_decision_manifest_file(args.lineage_decision_manifest)
    max_items_output = int(args.max_items_output)
    if args.emit_lineage_decision_manifest_template:
        max_items_output = max(max_items_output, int(args.limit))
    plan = build_batch_plan(
        args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=max_items_output,
        include_ui_snapshot_audit=bool(args.include_ui_snapshot_audit),
        include_replay_audit=bool(args.include_replay_audit),
        ui_snapshot_mode=args.ui_snapshot_mode,
        ui_snapshot_city_filter=args.ui_snapshot_city_filter,
        since_hours=float(args.since_hours),
        lineage_decision_manifest={} if args.emit_lineage_decision_manifest_template else lineage_decision_manifest,
    )
    if args.emit_lineage_decision_manifest_template:
        manifest = build_lineage_repair_decision_manifest_template(
            plan,
            decisions=lineage_decision_manifest,
            decided_by=args.decided_by,
            decided_at=args.decided_at,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0 if manifest.get("template_complete") else 1
    if args.emit_lineage_repair_apply_contract:
        contract = build_lineage_repair_apply_contract(
            plan,
            lineage_decision_manifest=lineage_decision_manifest,
            backup_path=args.lineage_repair_backup,
            allow_apply=False,
            confirmation_phrase=args.lineage_repair_confirm,
            writer_quiescent=bool(args.lineage_repair_writer_quiescent),
        )
        print(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0 if contract.get("ok") else 1
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if plan.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
