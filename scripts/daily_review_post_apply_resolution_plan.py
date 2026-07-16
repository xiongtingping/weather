#!/usr/bin/env python3
"""Read-only resolution plan for daily review post-apply blockers.

This planner does not apply schema, repair lineage, persist reviews,
rematerialize facts, rebuild signal ledgers, restart launchd, or place orders.
It combines the controlled daily-review packet with fact-level posterior and
replay audits so the remaining post-apply blockers have one operator-facing
resolution surface.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import daily_review_controlled_apply_confirmation_packet as master_packet  # noqa: E402
from scripts import daily_review_persistence_batch_plan as batch_plan  # noqa: E402
from scripts import daily_review_post_apply_acceptance_audit  # noqa: E402
from scripts import posterior_ssot_rematerialization_plan  # noqa: E402
from scripts import posterior_ssot_fact_coverage_audit  # noqa: E402
from scripts import ssot_chain_replay_audit  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "daily_review_post_apply_resolution_plan_v1"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _counter(values: Sequence[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for value in values:
        text = str(value or "").strip() or "missing"
        counts[text] += 1
    return dict(sorted(counts.items()))


def _guards() -> dict[str, bool]:
    return {
        "executes_sql_writes": False,
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_daily_weather_reviews": False,
        "writes_daily_calibration_records": False,
        "writes_bias_correction_candidates": False,
        "writes_parameter_change_events": False,
        "writes_signal_to_order_timing": False,
        "writes_auto_trade_decisions": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _load_fact_payload(conn: sqlite3.Connection, fact_id: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        fact_id_int = int(fact_id or 0)
    except Exception:
        fact_id_int = 0
    if fact_id_int <= 0:
        return {}, {"available": False, "reason": "fact_id_missing"}
    row = conn.execute(
        """
        SELECT id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        FROM event_fact_history
        WHERE id = ?
        LIMIT 1
        """,
        (fact_id_int,),
    ).fetchone()
    if row is None:
        return {}, {"available": False, "reason": "fact_row_not_found", "fact_id": fact_id_int}
    payload = _safe_json(row["fact_json"])
    meta = {
        "available": True,
        "id": int(row["id"] or 0),
        "run_date": str(row["run_date"] or ""),
        "slug": str(row["slug"] or ""),
        "event_date": str(row["event_date"] or ""),
        "fact_as_of": str(row["fact_as_of"] or ""),
        "composer_version": str(row["composer_version"] or ""),
        "created_at": str(row["created_at"] or ""),
    }
    return dict(payload) if isinstance(payload, Mapping) else {}, meta


def _override_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_override = payload.get("hard_source_posterior_override")
    raw_meta = payload.get("hard_source_posterior_override_meta")
    override = raw_override if isinstance(raw_override, Mapping) else raw_meta if isinstance(raw_meta, Mapping) else {}
    return {
        "present": bool(raw_override or override),
        "source": str(override.get("source") or override.get("value_source") or ""),
        "constraint_mode": str(override.get("constraint_mode") or ""),
        "final_bucket_lock": bool(override.get("final_bucket_lock")),
        "final_lock_reason": str(override.get("final_lock_reason") or ""),
        "target_bucket": override.get("target_bucket"),
        "value_c": override.get("value_c"),
        "value_at": override.get("value_at"),
        "latest_at": override.get("latest_at"),
        "fetched_at": override.get("fetched_at"),
    }


def _posterior_resolution_for_payload(
    *,
    payload: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    audit = posterior_ssot_fact_coverage_audit.audit_fact_payload(payload, meta=meta)
    issues = [str(issue) for issue in audit.get("issues") or [] if str(issue or "").strip()]
    fact_id = int(meta.get("id") or 0)
    recommended_action = "none"
    if issues:
        recommended_action = (
            "run_posterior_ssot_rematerialization_plan_then_controlled_fact_writer_apply"
            if "posterior_temperature_distribution_missing" not in issues
            else "manual_hierarchical_posterior_rebuild_required"
        )
    return {
        "fact_id": fact_id,
        "slug": meta.get("slug"),
        "event_date": meta.get("event_date"),
        "fact_as_of": meta.get("fact_as_of"),
        "ok": bool(audit.get("ok")),
        "issues": issues,
        "target_bucket": audit.get("target_bucket"),
        "target_probability": audit.get("target_probability"),
        "phase": audit.get("phase"),
        "beta_weak": audit.get("beta_weak"),
        "beta_strong": audit.get("beta_strong"),
        "recommended_action": recommended_action,
        "no_apply_probe_command": (
            f"python3 scripts/posterior_ssot_rematerialization_plan.py --fact-id {fact_id} --max-items-output 1"
            if issues and fact_id
            else ""
        ),
    }


def _replay_anchor_resolution_for_payload(
    *,
    payload: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    fact_id = int(meta.get("id") or 0)
    hard_expected = ssot_chain_replay_audit._payload_has_hard_source_signal_expected(payload)
    signal_id = ssot_chain_replay_audit._signal_id_from_mapping(payload)
    override = _override_summary(payload)
    candidate = bool(hard_expected and not signal_id)
    source = str(override.get("source") or "")
    constraint_mode = str(override.get("constraint_mode") or "")
    if not candidate:
        action = "none"
    elif source == "zuuu_metar_raw_awc" and constraint_mode == "hard_source_lower_bound_constraint":
        action = "manual_legacy_awc_signal_anchor_rebuild_or_mark_as_reviewed_legacy_no_signal"
    else:
        action = "inspect_replay_anchor_before_acceptance"
    return {
        "fact_id": fact_id,
        "slug": meta.get("slug"),
        "event_date": meta.get("event_date"),
        "hard_source_signal_expected": bool(hard_expected),
        "signal_id": signal_id,
        "candidate": candidate,
        "override": override,
        "recommended_action": action,
        "no_apply_probe_command": (
            f"python3 scripts/ssot_chain_replay_audit.py --fact-id {fact_id} --max-signals-output 3"
            if candidate and fact_id
            else ""
        ),
    }


def _normalize_fact_ids(values: Sequence[Any]) -> list[int]:
    out: list[int] = []
    for raw in values:
        try:
            fact_id = int(raw or 0)
        except Exception:
            continue
        if fact_id > 0 and fact_id not in out:
            out.append(fact_id)
    return out


def _acceptance_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    coverage = _as_mapping(report.get("coverage"))
    return {
        "schema": str(report.get("schema") or ""),
        "ok": bool(report.get("ok")),
        "blockers": list(report.get("blockers") or []),
        "warnings": list(report.get("warnings") or []),
        "raw_settled_event_total_count": int(coverage.get("raw_settled_event_total_count") or 0),
        "settled_event_count": int(coverage.get("settled_event_count") or 0),
        "excluded_settled_event_count": int(coverage.get("excluded_settled_event_count") or 0),
        "coverage_scope_complete": bool(coverage.get("coverage_scope_complete")),
        "coverage_scope_truncated_count": int(coverage.get("coverage_scope_truncated_count") or 0),
        "reviewed_event_count": int(coverage.get("reviewed_event_count") or 0),
        "missing_review_count": int(coverage.get("missing_review_count") or 0),
        "chain_ok_count": int(coverage.get("chain_ok_count") or 0),
        "chain_blocked_count": int(coverage.get("chain_blocked_count") or 0),
        "item_blocker_counts": dict(_as_mapping(coverage.get("item_blocker_counts"))),
    }


def _key(slug: Any, event_date: Any) -> tuple[str, str]:
    return str(slug or "").strip(), str(event_date or "").strip()[:10]


def _batch_item_index(batch: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    items = list(_as_sequence(batch.get("items"))) + list(_as_sequence(batch.get("excluded_items")))
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in items:
        if isinstance(item, Mapping):
            out[_key(item.get("slug"), item.get("event_date"))] = item
    return out


def _daily_review_missing_resolution(
    *,
    acceptance: Mapping[str, Any],
    batch: Mapping[str, Any],
    max_items_output: int,
) -> dict[str, Any]:
    items_by_key = _batch_item_index(batch)
    candidates: list[dict[str, Any]] = []
    missing_fact_policy_items: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    visible_limit = max(0, int(max_items_output))
    for sample in _as_sequence(acceptance.get("item_blocker_samples")):
        if not isinstance(sample, Mapping):
            continue
        blockers = [str(value or "").strip() for value in sample.get("blockers") or []]
        if "daily_review_missing" not in blockers:
            continue
        slug, event_date = _key(sample.get("slug"), sample.get("event_date"))
        item = _as_mapping(items_by_key.get((slug, event_date)))
        latest_fact = _as_mapping(sample.get("latest_fact"))
        row_counts = _as_mapping(item.get("persistence_row_counts"))
        lineage_blockers = [str(value or "").strip() for value in item.get("lineage_apply_blockers") or []]
        if "fact_snapshot_not_found" in blockers or not latest_fact.get("available"):
            state = "missing_fact_snapshot_policy_required"
        elif item and not lineage_blockers and int(row_counts.get("daily_weather_reviews") or 0) >= 1:
            state = "ready_for_controlled_persistence_apply"
        else:
            state = "blocked_review_plan_incomplete"
        row = {
            "slug": slug,
            "event_date": event_date,
            "state": state,
            "blockers": blockers,
            "event_run_id": item.get("event_run_id"),
            "review_id": item.get("review_id") or sample.get("review_id") or "",
            "fact_snapshot_id": item.get("fact_snapshot_id")
            or latest_fact.get("fact_snapshot_id")
            or "",
            "fact_id": latest_fact.get("fact_id") or _as_mapping(item.get("fact")).get("id"),
            "posterior_run_id": latest_fact.get("posterior_run_id") or "",
            "lineage_apply_blockers": lineage_blockers,
            "persistence_row_counts": dict(row_counts),
            "next_boundary": (
                "include_in_confirmed_daily_review_persistence_apply"
                if state == "ready_for_controlled_persistence_apply"
                else "choose_exclude_rebuild_or_wait_policy_for_missing_fact_snapshot"
                if state == "missing_fact_snapshot_policy_required"
                else "fix_daily_review_report_or_persistence_plan_before_apply"
            ),
        }
        if state == "ready_for_controlled_persistence_apply":
            candidates.append(row)
        elif state == "missing_fact_snapshot_policy_required":
            missing_fact_policy_items.append(row)
        else:
            blocked_items.append(row)

    return {
        "schema": "daily_review_missing_resolution_v1",
        "read_only": True,
        "candidate_count": len(candidates),
        "missing_fact_policy_count": len(missing_fact_policy_items),
        "blocked_count": len(blocked_items),
        "items": candidates[:visible_limit] if visible_limit else [],
        "items_truncated": len(candidates) > visible_limit,
        "missing_fact_policy_items": missing_fact_policy_items[:visible_limit] if visible_limit else [],
        "missing_fact_policy_items_truncated": len(missing_fact_policy_items) > visible_limit,
        "blocked_items": blocked_items[:visible_limit] if visible_limit else [],
        "blocked_items_truncated": len(blocked_items) > visible_limit,
        "recommended_action": (
            "run_controlled_daily_review_persistence_apply_after_confirmation"
            if candidates and not blocked_items
            else "resolve_blocked_daily_review_items_before_persistence_apply"
            if blocked_items
            else "choose_missing_fact_snapshot_policy_before_persistence_apply"
            if missing_fact_policy_items
            else "no_missing_daily_review_resolution_required"
        ),
        "guards": _guards(),
    }


def _posterior_rematerialization_probe(
    db_path: Path,
    *,
    fact_ids: Sequence[Any],
    max_items_output: int,
) -> dict[str, Any]:
    normalized_fact_ids = _normalize_fact_ids(fact_ids)
    visible_limit = max(0, int(max_items_output))
    if not normalized_fact_ids:
        return {
            "available": False,
            "reason": "posterior_candidate_fact_ids_empty",
            "fact_ids": [],
            "checked_fact_count": 0,
            "candidate_count": 0,
            "projected_ok_count": 0,
            "projected_issue_count": 0,
            "can_forward_writer_fix_all_candidates": False,
            "action_counts": {},
            "issue_family_counts": {},
            "current_issue_counts": {},
            "items": [],
            "items_truncated": False,
        }

    remat = posterior_ssot_rematerialization_plan.build_plan(
        db_path,
        fact_ids=normalized_fact_ids,
        since_hours=0,
        limit=len(normalized_fact_ids),
        latest_only=False,
        max_items_output=visible_limit,
    )
    forward = _as_mapping(remat.get("forward_write_acceptance"))
    historical = _as_mapping(remat.get("historical_rematerialization_plan"))
    return {
        "available": True,
        "schema": remat.get("schema"),
        "read_only": bool(remat.get("read_only")),
        "guards": dict(_as_mapping(remat.get("guards"))),
        "fact_ids": normalized_fact_ids,
        "checked_fact_count": int(forward.get("checked_fact_count") or 0),
        "candidate_count": int(historical.get("candidate_count") or 0),
        "projected_ok_count": int(forward.get("projected_ok_count") or 0),
        "projected_issue_count": int(forward.get("projected_issue_count") or 0),
        "can_forward_writer_fix_all_candidates": bool(
            forward.get("can_forward_writer_fix_all_candidates")
        ),
        "action_counts": dict(_as_mapping(historical.get("action_counts"))),
        "issue_family_counts": dict(_as_mapping(historical.get("issue_family_counts"))),
        "current_issue_counts": dict(_as_mapping(historical.get("current_issue_counts"))),
        "items": list(_as_sequence(remat.get("candidates")))[:visible_limit] if visible_limit else [],
        "items_truncated": bool(remat.get("candidates_truncated")),
        "next_boundary": remat.get("next_boundary"),
    }


def build_resolution_plan(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    missing_fact_decision: str = master_packet.DEFAULT_MISSING_FACT_DECISION,
    master_confirmation_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    operation_max_items = max(1, int(limit), int(max_items_output))
    master = dict(master_confirmation_packet or {})
    if not master:
        master = master_packet.build_confirmation_packet(
            db,
            since_days=since_days,
            since_date=since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
            max_items_output=operation_max_items,
            missing_fact_decision=missing_fact_decision,
        )
    manifest = dict(_as_mapping(master.get("proposed_manifest_json")))
    batch = batch_plan.build_batch_plan(
        db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=operation_max_items,
        lineage_decision_manifest=manifest,
    )
    acceptance = daily_review_post_apply_acceptance_audit.build_report(
        db,
        since_days=since_days,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
        max_items_output=operation_max_items,
        include_ui_snapshot_audit=False,
        lineage_decision_manifest=manifest,
    )
    items = [dict(_as_mapping(item)) for item in _as_sequence(batch.get("items"))]
    posterior_items: list[dict[str, Any]] = []
    replay_items: list[dict[str, Any]] = []
    fact_ids: list[int] = []
    with _connect_ro(db) as conn:
        for item in items:
            fact = _as_mapping(item.get("fact"))
            fact_id = int(fact.get("id") or 0)
            if fact_id <= 0:
                continue
            fact_ids.append(fact_id)
            payload, meta = _load_fact_payload(conn, fact_id)
            if not payload:
                continue
            posterior = _posterior_resolution_for_payload(payload=payload, meta=meta)
            if posterior.get("issues"):
                posterior_items.append(posterior)
            replay = _replay_anchor_resolution_for_payload(payload=payload, meta=meta)
            if replay.get("candidate"):
                replay_items.append(replay)

    decisions = [
        dict(_as_mapping(row))
        for row in _as_sequence(manifest.get("decisions"))
        if isinstance(row, Mapping)
    ]
    excluded = [row for row in decisions if str(row.get("decision") or "") == "exclude_sample"]
    confirm_repair = [row for row in decisions if str(row.get("decision") or "") == "confirm_repair"]
    posterior_issue_counts = Counter(
        issue for item in posterior_items for issue in _as_sequence(item.get("issues"))
    )
    visible_limit = max(0, int(max_items_output))
    posterior_fact_ids = [int(item.get("fact_id") or 0) for item in posterior_items]
    posterior_rematerialization_probe = _posterior_rematerialization_probe(
        db,
        fact_ids=posterior_fact_ids,
        max_items_output=visible_limit,
    )
    unresolved_counts = {
        "missing_fact_snapshot_excluded": len(excluded),
        "posterior_ssot_fact_candidates": len(posterior_items),
        "legacy_awc_signal_anchor_candidates": len(replay_items),
    }
    acceptance_blockers = [str(value or "").strip() for value in acceptance.get("blockers") or []]
    daily_review_resolution = _daily_review_missing_resolution(
        acceptance=acceptance,
        batch=batch,
        max_items_output=visible_limit,
    )
    resolution_complete = not any(unresolved_counts.values()) and not acceptance_blockers
    operator_resolution_surface_ready = bool(
        not daily_review_resolution.get("blocked_count")
        and not (
            set(acceptance_blockers)
            - {
                "daily_review_missing",
            }
        )
    )
    return {
        "ok": resolution_complete,
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "daily_review_post_apply_resolution_plan_no_apply",
        "db": str(db),
        "scope": {
            "since_days": int(since_days),
            "since_date": since_date,
            "slug": slug,
            "event_date": event_date,
            "market_scope": market_scope,
            "limit": int(limit),
            "max_items_output": int(max_items_output),
            "operation_max_items_output": int(operation_max_items),
            "missing_fact_decision": missing_fact_decision,
        },
        "resolution_complete": resolution_complete,
        "operator_resolution_surface_ready": operator_resolution_surface_ready,
        "acceptance_summary": _acceptance_summary(acceptance),
        "unresolved_counts": unresolved_counts,
        "daily_review_missing_resolution": daily_review_resolution,
        "batch_plan_summary": {
            "ok": bool(batch.get("ok")),
            "settled_event_count": int(batch.get("settled_event_count") or 0),
            "effective_settled_event_count": int(batch.get("effective_settled_event_count") or 0),
            "excluded_settled_event_count": int(batch.get("excluded_settled_event_count") or 0),
            "planned_review_count": int(batch.get("planned_review_count") or 0),
            "planned_calibration_record_count": int(batch.get("planned_calibration_record_count") or 0),
            "can_apply_after_schema_and_confirmation": bool(
                batch.get("can_apply_after_schema_and_confirmation")
            ),
            "apply_blockers": list(batch.get("apply_blockers") or []),
            "blockers": list(batch.get("blockers") or []),
            "lineage_apply_blocker_counts": dict(_as_mapping(batch.get("lineage_apply_blocker_counts"))),
        },
        "lineage_resolution": {
            "proposed_manifest_decision_counts": dict(_as_mapping(master.get("proposed_manifest_decision_counts"))),
            "confirm_repair_count": len(confirm_repair),
            "excluded_sample_count": len(excluded),
            "excluded_samples": excluded[:visible_limit] if visible_limit else [],
            "lineage_apply_readiness": dict(
                _as_mapping(batch.get("lineage_decision_apply_readiness"))
            ),
            "recommended_action": (
                "keep_excluded_sample_out_of_daily_review_persistence_or_manual_rebuild_fact_snapshot"
                if excluded
                else "lineage_scope_has_no_excluded_missing_fact_samples"
            ),
        },
        "posterior_ssot_resolution": {
            "candidate_count": len(posterior_items),
            "candidate_fact_ids": _normalize_fact_ids(posterior_fact_ids),
            "issue_counts": dict(sorted(posterior_issue_counts.items())),
            "rematerialization_probe": posterior_rematerialization_probe,
            "recommended_action": (
                "review_no_apply_rematerialization_candidates_before_any_controlled_fact_writer_apply"
                if posterior_items
                else "no_posterior_ssot_resolution_required"
            ),
            "items": posterior_items[:visible_limit] if visible_limit else [],
            "items_truncated": len(posterior_items) > visible_limit,
        },
        "replay_anchor_resolution": {
            "candidate_count": len(replay_items),
            "source_counts": _counter([_as_mapping(item.get("override")).get("source") for item in replay_items]),
            "constraint_mode_counts": _counter(
                [_as_mapping(item.get("override")).get("constraint_mode") for item in replay_items]
            ),
            "recommended_action": (
                "manual_legacy_awc_signal_anchor_resolution_before_post_apply_acceptance"
                if replay_items
                else "no_legacy_awc_signal_anchor_resolution_required"
            ),
            "items": replay_items[:visible_limit] if visible_limit else [],
            "items_truncated": len(replay_items) > visible_limit,
        },
        "source_counts": {
            "effective_fact_count": len(fact_ids),
            "batch_item_count": len(items),
            "settled_event_count": int(batch.get("settled_event_count") or 0),
            "effective_settled_event_count": int(batch.get("effective_settled_event_count") or 0),
            "excluded_settled_event_count": int(batch.get("excluded_settled_event_count") or 0),
        },
        "next_boundary": (
            "run_controlled_daily_review_persistence_apply_after_confirmation"
            if daily_review_resolution.get("candidate_count")
            and not daily_review_resolution.get("blocked_count")
            and not any(
                value
                for key, value in unresolved_counts.items()
                if key != "missing_fact_snapshot_excluded"
            )
            else
            "resolve_post_apply_blocker_families_before_production_controlled_apply"
            if any(unresolved_counts.values()) or acceptance_blockers
            else "rerun_daily_review_controlled_apply_temp_drill_and_joint_plan_gate"
        ),
        "guards": _guards(),
    }


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
    parser.add_argument(
        "--missing-fact-decision",
        choices=master_packet.lineage_runbook.confirmation_packet.MISSING_FACT_DECISIONS,
        default=master_packet.DEFAULT_MISSING_FACT_DECISION,
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    result = build_resolution_plan(
        db_path=args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        missing_fact_decision=args.missing_fact_decision,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
