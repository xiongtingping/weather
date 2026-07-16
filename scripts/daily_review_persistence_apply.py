#!/usr/bin/env python3
"""Controlled daily review persistence apply entrypoint.

Defaults to no-apply. The only supported writes are the rows produced from the
same settled-event scope as `daily_review_persistence_batch_plan.py`:
`daily_weather_reviews`, `daily_calibration_records`, and
`bias_correction_candidates`. Parameter change events require a separate flag
and confirmation phrase, and still do not change production parameters.
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

from daily_review_persistence_writer import (  # noqa: E402
    apply_daily_review_persistence_plan,
    build_daily_review_persistence_plan,
)
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402
from scripts import daily_weather_review_report  # noqa: E402


SCHEMA_VERSION = "daily_review_persistence_apply_contract_v1"
CLI_RESULT_SCHEMA = "daily_review_persistence_apply_cli_result_v1"
CONFIRMATION_PHRASE = "APPLY_DAILY_REVIEW_PERSISTENCE"
PARAMETER_EVENT_CONFIRMATION_PHRASE = "APPLY_DAILY_REVIEW_PARAMETER_EVENTS"


def _file_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": "", "exists": False, "size_bytes": None}
    resolved = Path(path).expanduser().resolve()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "size_bytes": resolved.stat().st_size if resolved.exists() else None,
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _date_only(raw: Any) -> str:
    text = str(raw or "").strip()
    return text[:10] if len(text) >= 10 else text


def _effective_item_keys(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in plan.get("items") or []:
        item_map = _as_mapping(item)
        slug = str(item_map.get("slug") or "").strip()
        event_date = _date_only(item_map.get("event_date"))
        if not slug or not event_date:
            continue
        out.append(
            {
                "slug": slug,
                "event_date": event_date,
                "event_run_id": item_map.get("event_run_id"),
                "settlement_actual": item_map.get("settlement_actual"),
                "review_id": item_map.get("review_id"),
                "fact_snapshot_id": item_map.get("fact_snapshot_id"),
                "first_bad_link": item_map.get("first_bad_link"),
            }
        )
    return out


def _preflight_checks(
    *,
    contract_plan: Mapping[str, Any],
    db: Path,
    backup_path: Path | None,
    allow_apply: bool,
    confirmation_phrase: str,
    writer_quiescent: bool,
    allow_parameter_event_apply: bool,
    parameter_event_confirmation_phrase: str,
) -> list[dict[str, Any]]:
    backup_state = _file_state(backup_path)
    backup_ok = (
        bool(backup_state.get("exists"))
        and int(backup_state.get("size_bytes") or 0) > 0
        and str(backup_state.get("path") or "") != str(db)
    )
    ready = bool(contract_plan.get("can_apply_after_schema_and_confirmation"))
    parameter_event_confirmed = (
        bool(allow_parameter_event_apply)
        and parameter_event_confirmation_phrase == PARAMETER_EVENT_CONFIRMATION_PHRASE
    )
    return [
        {
            "name": "batch_plan_ready",
            "status": "pass" if ready else "blocked",
            "hard_blocker": not ready,
            "reason": (
                "batch_plan_ready_after_schema_lineage_and_confirmation"
                if ready
                else "batch_plan_not_ready_for_persistence_apply"
            ),
            "apply_blockers": list(contract_plan.get("apply_blockers") or []),
        },
        {
            "name": "apply_flag",
            "status": "pass" if allow_apply else "required",
            "hard_blocker": ready and not allow_apply,
            "reason": "allow_apply_true" if allow_apply else "daily_review_persistence_requires_explicit_apply_flag",
        },
        {
            "name": "confirmation_phrase",
            "status": "pass" if confirmation_phrase == CONFIRMATION_PHRASE else "required",
            "hard_blocker": ready and confirmation_phrase != CONFIRMATION_PHRASE,
            "reason": (
                "confirmation_phrase_matches"
                if confirmation_phrase == CONFIRMATION_PHRASE
                else "confirmation_phrase_missing_or_mismatched"
            ),
        },
        {
            "name": "fresh_archive_db_backup",
            "status": "pass" if backup_ok else "required",
            "hard_blocker": ready and not backup_ok,
            "reason": "backup_path_exists_and_is_not_db" if backup_ok else "backup_path_required_before_persistence_apply",
            "backup_path": backup_state.get("path"),
            "backup_exists": backup_state.get("exists"),
            "backup_size_bytes": backup_state.get("size_bytes"),
        },
        {
            "name": "writer_quiescence",
            "status": "pass" if writer_quiescent else "required",
            "hard_blocker": ready and not writer_quiescent,
            "reason": "caller_asserted_writer_quiescence" if writer_quiescent else "requires_explicit_writer_quiescent_assertion",
        },
        {
            "name": "parameter_event_apply",
            "status": "pass" if parameter_event_confirmed else "skipped_or_required",
            "hard_blocker": bool(allow_parameter_event_apply and not parameter_event_confirmed),
            "reason": (
                "parameter_event_apply_confirmed"
                if parameter_event_confirmed
                else "parameter_events_skipped_by_default_or_confirmation_missing"
            ),
            "allow_parameter_event_apply": bool(allow_parameter_event_apply),
        },
        {
            "name": "historical_backfill",
            "status": "blocked_by_design",
            "hard_blocker": False,
            "reason": "persistence_apply_targets_current_settled_scope_only_not_bulk_backfill",
        },
    ]


def build_daily_review_persistence_apply_contract(
    db_path: Path | str = batch_plan.DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 100,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
    backup_path: Path | None = None,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    writer_quiescent: bool = False,
    allow_parameter_event_apply: bool = False,
    parameter_event_confirmation_phrase: str = "",
    prebuilt_batch_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    visible_limit = max(int(max_items_output), int(limit))
    plan = dict(prebuilt_batch_plan or {})
    plan_source = "prebuilt_batch_plan" if plan else "rebuilt_batch_plan"
    if not plan:
        plan = batch_plan.build_batch_plan(
            db,
            since_days=int(since_days),
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=int(limit),
            max_items_output=visible_limit,
            include_ui_snapshot_audit=False,
            include_replay_audit=False,
            lineage_decision_manifest=lineage_decision_manifest,
        )
    item_keys = _effective_item_keys(plan)
    preflight_checks = _preflight_checks(
        contract_plan=plan,
        db=db,
        backup_path=backup_path,
        allow_apply=allow_apply,
        confirmation_phrase=confirmation_phrase,
        writer_quiescent=writer_quiescent,
        allow_parameter_event_apply=allow_parameter_event_apply,
        parameter_event_confirmation_phrase=parameter_event_confirmation_phrase,
    )
    apply_blockers = [str(item.get("name")) for item in preflight_checks if item.get("hard_blocker")]
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(plan.get("ok", True)),
        "read_only": True,
        "dry_run": True,
        "execution_mode": "daily_review_persistence_apply_contract_no_apply",
        "apply_supported": True,
        "applied": False,
        "ready_to_apply_now": not apply_blockers,
        "db": str(db),
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "parameter_event_confirmation_phrase_required": PARAMETER_EVENT_CONFIRMATION_PHRASE,
        "batch_plan_schema": plan.get("schema"),
        "batch_plan_source": plan_source,
        "batch_plan_ready_after_confirmation": bool(plan.get("can_apply_after_schema_and_confirmation")),
        "settled_event_count": plan.get("settled_event_count"),
        "effective_settled_event_count": plan.get("effective_settled_event_count"),
        "excluded_settled_event_count": plan.get("excluded_settled_event_count"),
        "planned_review_count": plan.get("planned_review_count"),
        "planned_calibration_record_count": plan.get("planned_calibration_record_count"),
        "planned_candidate_count": plan.get("planned_candidate_count"),
        "planned_parameter_event_count": plan.get("planned_parameter_event_count"),
        "aggregate_row_counts": dict(plan.get("aggregate_row_counts") or {}),
        "apply_blockers": apply_blockers,
        "batch_apply_blockers": list(plan.get("apply_blockers") or []),
        "preflight_checks": preflight_checks,
        "item_keys": item_keys,
        "item_keys_truncated": len(item_keys) < int(plan.get("effective_settled_event_count") or 0),
        "lineage_decision_apply_readiness": dict(plan.get("lineage_decision_apply_readiness") or {}),
        "controlled_apply_command": (
            "python3 scripts/daily_review_persistence_apply.py --lineage-decision-manifest <manifest.json> "
            f"--apply --confirm {CONFIRMATION_PHRASE} --backup <backup.bak> --writer-quiescent"
        ),
        "post_apply_verification_commands": [
            "python3 scripts/daily_review_coverage_audit.py --since-days 45 --max-items-output 20",
            "python3 scripts/ssot_chain_replay_audit.py --since-hours 168 --limit 100 --max-signals-output 20",
            "python3 scripts/check_ssot_alignment.py --immutable",
        ],
        "boundary": {
            "daily_review_persistence_included": True,
            "lineage_repair_included": False,
            "schema_apply_included": False,
            "historical_backfill_included": False,
            "parameter_events_skipped_unless_separately_confirmed": True,
            "launchd_restart_included": False,
            "orders_or_trade_decisions_included": False,
        },
        "guards": {
            "executes_sql": False,
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
            "run_controlled_daily_review_persistence_apply_after_confirmation"
            if not apply_blockers
            else "resolve_daily_review_persistence_apply_preflight_blockers"
        ),
    }


def _review_plan_for_item(db: Path, item: Mapping[str, Any]) -> Mapping[str, Any]:
    report = daily_weather_review_report.build_report(
        db,
        slug=str(item.get("slug") or ""),
        event_date=_date_only(item.get("event_date")),
        include_ui_snapshot_audit=False,
        include_replay_audit=False,
        settlement_override={
            "settlement_actual": item.get("settlement_actual"),
            "settlement_actual_source": "daily_review_persistence_apply.item_key",
            "event_run_id": item.get("event_run_id"),
        },
        since_hours=24 * 90,
        limit=100,
    )
    return build_daily_review_persistence_plan(_as_mapping(report.get("review")))


def apply_daily_review_persistence_batch(
    db_path: Path | str = batch_plan.DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 100,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
    backup_path: Path | None = None,
    allow_apply: bool = False,
    confirmation_phrase: str = "",
    writer_quiescent: bool = False,
    allow_parameter_event_apply: bool = False,
    parameter_event_confirmation_phrase: str = "",
) -> dict[str, Any]:
    contract = build_daily_review_persistence_apply_contract(
        db_path,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=max_items_output,
        lineage_decision_manifest=lineage_decision_manifest,
        backup_path=backup_path,
        allow_apply=allow_apply,
        confirmation_phrase=confirmation_phrase,
        writer_quiescent=writer_quiescent,
        allow_parameter_event_apply=allow_parameter_event_apply,
        parameter_event_confirmation_phrase=parameter_event_confirmation_phrase,
    )
    if contract.get("apply_blockers"):
        return {
            "schema": CLI_RESULT_SCHEMA,
            "ok": False,
            "applied": False,
            "read_only": True,
            "dry_run": True,
            "execution_mode": "daily_review_persistence_apply_blocked",
            "contract": contract,
            "guards": dict(contract.get("guards") or {}),
        }
    db = Path(db_path).expanduser().resolve()
    applied_results: list[dict[str, Any]] = []
    aggregate_row_counts = {
        "daily_weather_reviews": 0,
        "daily_calibration_records": 0,
        "bias_correction_candidates": 0,
        "parameter_change_events": 0,
    }
    try:
        with sqlite3.connect(db, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            for item in contract.get("item_keys") or []:
                item_map = dict(_as_mapping(item))
                plan = _review_plan_for_item(db, item_map)
                result = apply_daily_review_persistence_plan(
                    conn,
                    plan,
                    allow_apply=True,
                    allow_parameter_event_apply=bool(allow_parameter_event_apply),
                )
                if not result.get("ok"):
                    raise RuntimeError(json.dumps(result, ensure_ascii=False, sort_keys=True))
                row_counts = dict(_as_mapping(result.get("row_counts")))
                for key in aggregate_row_counts:
                    aggregate_row_counts[key] += int(row_counts.get(key) or 0)
                applied_results.append(
                    {
                        "slug": item_map.get("slug"),
                        "event_date": item_map.get("event_date"),
                        "review_id": plan.get("review_id"),
                        "fact_snapshot_id": plan.get("fact_snapshot_id"),
                        "status": result.get("status"),
                        "row_counts": row_counts,
                        "parameter_events_skipped": result.get("parameter_events_skipped"),
                    }
                )
            conn.commit()
    except Exception as exc:
        return {
            "schema": CLI_RESULT_SCHEMA,
            "ok": False,
            "applied": bool(applied_results),
            "read_only": False,
            "dry_run": False,
            "execution_mode": "daily_review_persistence_apply_failed",
            "contract": contract,
            "applied_results": applied_results,
            "error": f"{exc.__class__.__name__}: {exc}",
            "guards": {
                "executes_sql": bool(applied_results),
                "writes_archive_db": bool(applied_results),
                "historical_backfill": False,
                "fetches_external_weather_sources": False,
                "places_orders": False,
                "restarts_launchd": False,
                "changes_parameters": False,
            },
        }
    return {
        "schema": CLI_RESULT_SCHEMA,
        "ok": True,
        "applied": True,
        "read_only": False,
        "dry_run": False,
        "execution_mode": "daily_review_persistence_apply_completed",
        "contract": contract,
        "applied_results": applied_results,
        "aggregate_row_counts": aggregate_row_counts,
        "parameter_events_apply_enabled": bool(allow_parameter_event_apply),
        "guards": {
            "executes_sql": True,
            "writes_archive_db": True,
            "writes_daily_weather_reviews": aggregate_row_counts["daily_weather_reviews"] > 0,
            "writes_daily_calibration_records": aggregate_row_counts["daily_calibration_records"] > 0,
            "writes_bias_correction_candidates": aggregate_row_counts["bias_correction_candidates"] > 0,
            "writes_parameter_change_events": aggregate_row_counts["parameter_change_events"] > 0,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=batch_plan.DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=100)
    parser.add_argument("--lineage-decision-manifest", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="", help=f"Required exact phrase for --apply: {CONFIRMATION_PHRASE}")
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--allow-parameter-event-apply", action="store_true")
    parser.add_argument(
        "--confirm-parameter-event",
        default="",
        help=f"Required exact phrase with --allow-parameter-event-apply: {PARAMETER_EVENT_CONFIRMATION_PHRASE}",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manifest = batch_plan.load_lineage_decision_manifest_file(args.lineage_decision_manifest)
    common = {
        "since_days": int(args.since_days),
        "since_date": args.since_date,
        "slug": args.slug,
        "event_date": args.event_date,
        "market_scope": args.market_scope,
        "limit": int(args.limit),
        "max_items_output": max(int(args.max_items_output), int(args.limit)),
        "lineage_decision_manifest": manifest,
        "backup_path": args.backup,
        "allow_apply": bool(args.apply),
        "confirmation_phrase": str(args.confirm or ""),
        "writer_quiescent": bool(args.writer_quiescent),
        "allow_parameter_event_apply": bool(args.allow_parameter_event_apply),
        "parameter_event_confirmation_phrase": str(args.confirm_parameter_event or ""),
    }
    if not args.apply:
        contract = build_daily_review_persistence_apply_contract(args.db, **common)
        print(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    result = apply_daily_review_persistence_batch(args.db, **common)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
