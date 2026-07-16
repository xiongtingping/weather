#!/usr/bin/env python3
"""Read-only posterior SSOT rematerialization planning.

This script separates two questions that should not be mixed:

1. Is the current forward writer capable of producing a valid posterior SSOT
   fact for the same payload shape?
2. Which already-persisted historical fact rows would require an explicitly
   confirmed rematerialization or rebuild to make the database pass coverage?

It never writes archive.db, does not fetch weather sources, and does not place
orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import posterior_ssot_fact_coverage_audit as coverage_audit
import weather_fact_snapshot_writer as fact_writer


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "posterior_ssot_rematerialization_plan_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"


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


def _parse_fact_ids(value: str | Sequence[Any] | None = None) -> list[int]:
    values: Sequence[Any]
    if value is None:
        values = []
    elif isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = [value]
    out: list[int] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            fact_id = int(text)
        except Exception:
            continue
        if fact_id > 0 and fact_id not in out:
            out.append(fact_id)
    return out


def _since_text(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(hours)))).isoformat(
        timespec="seconds"
    )


def _load_rows(
    conn: sqlite3.Connection,
    *,
    fact_id: str = "",
    fact_ids: Sequence[int] | None = None,
    slug: str = "",
    event_date: str = "",
    since_hours: float = 168.0,
    limit: int = 100,
    latest_only: bool = False,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    explicit_fact_ids = _parse_fact_ids([*(_parse_fact_ids(fact_id)), *(fact_ids or [])])
    if explicit_fact_ids:
        placeholders = ",".join("?" for _ in explicit_fact_ids)
        clauses.append(f"h.id IN ({placeholders})")
        params.extend(explicit_fact_ids)
    if slug:
        clauses.append("h.slug = ?")
        params.append(slug)
    if event_date:
        clauses.append("h.event_date = ?")
        params.append(event_date)
    if not explicit_fact_ids and since_hours > 0:
        clauses.append("h.fact_as_of >= ?")
        params.append(_since_text(float(since_hours)))
    latest_join = (
        "JOIN event_fact_latest l ON l.fact_id = h.id"
        if latest_only
        else "LEFT JOIN event_fact_latest l ON l.slug = h.slug AND l.event_date = h.event_date"
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of,
               h.composer_version, h.fact_json, h.created_at,
               l.fact_id AS latest_fact_id,
               CASE WHEN l.fact_id = h.id THEN 1 ELSE 0 END AS is_latest_fact
        FROM event_fact_history h
        {latest_join}
        {where}
        ORDER BY h.id DESC
        LIMIT ?
        """,
        [*params, max(1, int(limit))],
    ).fetchall()


def _issue_family(issues: Sequence[str]) -> str:
    issue_set = {str(issue) for issue in issues}
    if not issue_set:
        return "none"
    if "posterior_runtime_policy_stale" in issue_set:
        return "runtime_policy_materialization_gap"
    if "posterior_prior_coverage_refresh_needed" in issue_set:
        return "prior_coverage_materialization_gap"
    if "fact_json_invalid" in issue_set:
        return "manual_inspection_required"
    if "posterior_temperature_distribution_missing" in issue_set:
        return "requires_full_posterior_rebuild"
    if "posterior_ssot_fact_missing" in issue_set:
        return "missing_posterior_ssot_fact"
    if "posterior_ssot_fact_target_below_hard_temperature_floor" in issue_set:
        return "hard_floor_target_invalidation"
    if any(issue.endswith("_missing") for issue in issue_set):
        return "lineage_or_phase_materialization_gap"
    if any("selection" in issue for issue in issue_set):
        return "target_selection_resync"
    return "posterior_ssot_contract_gap"


def _merge_fill_operation_summary(
    operations: Sequence[Any],
    *,
    sample_limit: int = 8,
) -> dict[str, Any]:
    action_counts: Counter[str] = Counter()
    samples: list[dict[str, str]] = []
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        action = str(operation.get("action") or "")
        path = str(operation.get("path") or "")
        action_counts[action or "unknown"] += 1
        if len(samples) < max(0, int(sample_limit)):
            samples.append({"action": action, "path": path})
    total = sum(action_counts.values())
    return {
        "count": total,
        "action_counts": dict(sorted(action_counts.items())),
        "samples": samples,
        "truncated": total > len(samples),
    }


def _payload_lineage(payload: Mapping[str, Any]) -> dict[str, str]:
    lineage = _as_mapping(payload.get("lineage_state"))
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    return {
        "fact_snapshot_id": str(
            payload.get("fact_snapshot_id")
            or lineage.get("fact_snapshot_id")
            or ssot.get("fact_snapshot_id")
            or ""
        ),
        "posterior_run_id": str(
            payload.get("posterior_run_id")
            or lineage.get("posterior_run_id")
            or ssot.get("posterior_run_id")
            or ""
        ),
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(table),),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _review_anchor_counts(db_path: Path, fact_snapshot_ids: Sequence[str]) -> dict[str, int]:
    ids = sorted({str(value or "").strip() for value in fact_snapshot_ids if str(value or "").strip()})
    if not ids:
        return {}
    with _connect_ro(db_path) as conn:
        if not _table_exists(conn, "daily_weather_reviews"):
            return {}
        placeholders = ",".join("?" for _ in ids)
        try:
            rows = conn.execute(
                f"""
                SELECT fact_snapshot_id, COUNT(*) AS count
                FROM daily_weather_reviews
                WHERE fact_snapshot_id IN ({placeholders})
                GROUP BY fact_snapshot_id
                """,
                ids,
            ).fetchall()
        except sqlite3.Error:
            return {}
    return {str(row["fact_snapshot_id"] or ""): int(row["count"] or 0) for row in rows}


def _candidate_impact_surface(
    candidate: Mapping[str, Any],
    *,
    daily_review_anchor_count: int,
) -> dict[str, Any]:
    is_latest = bool(candidate.get("is_latest_fact"))
    latest_fact_id = int(candidate.get("latest_fact_id") or 0)
    projection = _as_mapping(candidate.get("projection"))
    projected_ok = bool(projection.get("projected_ok"))
    if is_latest:
        production_latest_impact = "current_latest_fact_has_posterior_ssot_gap"
        ssot_runtime_impact = "current_ui_decision_review_would_need_forward_projection_or_controlled_apply"
        operator_priority = "highest_repair_latest_fact_before_downstream_closeout"
    elif latest_fact_id > 0:
        production_latest_impact = "historical_fact_superseded_by_latest_fact"
        ssot_runtime_impact = "no_current_latest_ssot_impact"
        operator_priority = "history_governance_only_after_latest_chain_is_green"
    else:
        production_latest_impact = "latest_pointer_missing_or_scope_unlinked"
        ssot_runtime_impact = "cannot_prove_current_latest_impact_without_latest_pointer"
        operator_priority = "inspect_latest_pointer_before_apply"

    if daily_review_anchor_count > 0:
        daily_review_impact = "persisted_daily_review_anchor_points_to_candidate_fact"
    elif is_latest:
        daily_review_impact = "latest_fact_candidate_may_affect_future_daily_review"
    else:
        daily_review_impact = "no_persisted_daily_review_anchor_for_candidate"

    if projected_ok:
        safe_next_action = "review_then_controlled_fact_writer_apply_if_operator_wants_history_materialized"
    else:
        safe_next_action = "rebuild_or_manual_inspection_before_any_apply"
    return {
        "schema": "posterior_ssot_rematerialization_impact_v1",
        "production_latest_impact": production_latest_impact,
        "ssot_runtime_impact": ssot_runtime_impact,
        "daily_review_impact": daily_review_impact,
        "daily_review_anchor_count": int(daily_review_anchor_count),
        "operator_priority": operator_priority,
        "safe_next_action": safe_next_action,
        "requires_operator_confirmation": True,
        "can_be_fixed_by_forward_writer": projected_ok,
        "no_apply_plan_only": True,
    }


def _writer_projection_for_row(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    *,
    db_path: Path,
) -> dict[str, Any]:
    from posterior_ssot_projection_runtime import build_fact_projection_no_apply

    slug = str(payload.get("slug") or row["slug"] or "")
    event_date = str(payload.get("event_date") or row["event_date"] or "")
    run_date = str(payload.get("run_date") or row["run_date"] or event_date)
    fact_as_of = str(payload.get("fact_as_of") or row["fact_as_of"] or "")
    projection = build_fact_projection_no_apply(
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        fact_as_of=fact_as_of,
        payload=payload,
        current_fact_id=int(row["id"] or 0),
        current_fact_as_of=str(row["fact_as_of"] or ""),
        composer_version=str(row["composer_version"] or fact_writer.PARSER_VERSION),
        write_reason="posterior_ssot_rematerialization_plan",
        db_path=db_path,
    )
    plan = _as_mapping(projection.get("plan"))
    projected_payload = projection.get("projected_payload") if isinstance(projection.get("projected_payload"), Mapping) else {}
    projected_audit = coverage_audit.audit_fact_payload(
        projected_payload,
        meta={
            "id": row["id"],
            "slug": row["slug"],
            "event_date": row["event_date"],
            "fact_as_of": row["fact_as_of"],
            "composer_version": row["composer_version"],
        },
    ) if projected_payload else {"ok": False, "issues": ["writer_projection_payload_missing"]}
    projected_ssot = _as_mapping(projected_payload.get("posterior_ssot_fact"))
    actions = [
        {
            "action": str(action.get("action") or ""),
            "target_table": str(action.get("target_table") or ""),
            "would_write": bool(action.get("would_write")),
            "apply_status": str(action.get("apply_status") or ""),
            "block_reason": str(action.get("block_reason") or ""),
        }
        for action in list(plan.get("plan_actions") or [])
        if isinstance(action, Mapping)
    ]
    merge_fill_summary = _merge_fill_operation_summary(list(plan.get("merge_fill_operations") or []))
    return {
        "writer_plan_schema": plan.get("schema"),
        "writer_plan_ok": bool(plan.get("ok")),
        "writer_execution_mode": plan.get("execution_mode"),
        "writer_write_latest": bool(plan.get("write_latest")),
        "writer_apply_supported": bool(plan.get("apply_supported")),
        "writer_payload_changed_by_merge_fill": bool(plan.get("payload_changed_by_merge_fill")),
        "writer_projection_mode": str(projection.get("projection_mode") or ""),
        "writer_projection_context": dict(_as_mapping(projection.get("context"))),
        "writer_projection_issues": list(projection.get("issues") or []),
        "writer_merge_fill_operation_count": merge_fill_summary["count"],
        "writer_merge_fill_action_counts": merge_fill_summary["action_counts"],
        "writer_merge_fill_operation_samples": merge_fill_summary["samples"],
        "writer_merge_fill_operations_truncated": merge_fill_summary["truncated"],
        "writer_plan_actions": actions,
        "projected_ok": bool(projected_audit.get("ok")),
        "projected_issues": list(projected_audit.get("issues") or []),
        "projected_target_bucket": projected_ssot.get("target_bucket"),
        "projected_target_probability": projected_ssot.get("target_probability"),
        "projected_phase": projected_audit.get("phase"),
        "projected_phase_reason": projected_audit.get("phase_reason"),
        "projected_beta_weak": projected_audit.get("beta_weak"),
        "projected_beta_strong": projected_audit.get("beta_strong"),
        "projected_hard_temperature_floor_c": projected_audit.get("hard_temperature_floor_c"),
    }


def _candidate_action(current_issues: Sequence[str], projection: Mapping[str, Any]) -> str:
    if not current_issues:
        return "none"
    if projection.get("projected_ok"):
        return "controlled_append_or_repoint_with_fact_writer_after_operator_confirmation"
    projected_issues = {str(issue) for issue in projection.get("projected_issues") or []}
    if "posterior_temperature_distribution_missing" in projected_issues:
        return "rebuild_hierarchical_posterior_before_fact_writer_apply"
    if "writer_projection_payload_missing" in projected_issues:
        return "manual_fact_payload_inspection_required"
    return "manual_review_before_rematerialization"


def _projection_runtime_policy_stale(projection: Mapping[str, Any]) -> bool:
    context = _as_mapping(projection.get("writer_projection_context"))
    policy_context = _as_mapping(context.get("runtime_policy_context"))
    return bool(policy_context.get("requires_contextual_projection"))


def _projection_prior_coverage_refresh_needed(projection: Mapping[str, Any]) -> bool:
    context = _as_mapping(projection.get("writer_projection_context"))
    return str(context.get("prior_source") or "") == "temperature_prior_runtime_coverage_refresh"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "places_orders": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
        "executes_sql_writes": False,
    }


def build_plan(
    db_path: Path | str = DEFAULT_DB,
    *,
    fact_id: str = "",
    fact_ids: Sequence[int] | None = None,
    slug: str = "",
    event_date: str = "",
    since_hours: float = 168.0,
    limit: int = 100,
    latest_only: bool = False,
    max_items_output: int = 20,
) -> dict[str, Any]:
    db = Path(db_path)
    explicit_fact_ids = _parse_fact_ids([*(_parse_fact_ids(fact_id)), *(fact_ids or [])])
    runtime_policy_probe_enabled = bool(explicit_fact_ids or latest_only or slug or event_date)
    candidates: list[dict[str, Any]] = []
    current_issue_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    impact_counts: Counter[str] = Counter()
    daily_review_impact_counts: Counter[str] = Counter()
    projected_ok_count = 0
    checked_count = 0
    candidate_fact_snapshot_ids: list[str] = []
    with _connect_ro(db) as conn:
        rows = _load_rows(
            conn,
            fact_id=fact_id,
            fact_ids=explicit_fact_ids,
            slug=slug,
            event_date=event_date,
            since_hours=float(since_hours),
            limit=int(limit),
            latest_only=bool(latest_only),
        )
    for row in rows:
        payload_raw = _safe_json(row["fact_json"])
        if not isinstance(payload_raw, Mapping):
            current_audit = {
                "ok": False,
                "issues": ["fact_json_invalid"],
                "hard_temperature_floor_c": None,
            }
            projection = {
                "projected_ok": False,
                "projected_issues": ["fact_json_invalid"],
            }
        else:
            current_audit = coverage_audit.audit_fact_payload(payload_raw, meta=dict(row))
            current_issues = [str(issue) for issue in current_audit.get("issues") or []]
            projection = (
                _writer_projection_for_row(row, payload_raw, db_path=db)
                if current_issues or runtime_policy_probe_enabled
                else {
                    "projected_ok": True,
                    "projected_issues": [],
                    "writer_projection_mode": "coverage_ok_short_circuit",
                    "writer_plan_ok": True,
                    "writer_write_latest": False,
                    "writer_apply_supported": False,
                    "writer_merge_fill_operation_count": 0,
                    "writer_merge_fill_action_counts": {},
                    "writer_merge_fill_operation_samples": [],
                    "writer_merge_fill_operations_truncated": False,
                    "writer_plan_actions": [],
                }
            )
        lineage = _payload_lineage(payload_raw) if isinstance(payload_raw, Mapping) else {}
        checked_count += 1
        current_issues = [str(issue) for issue in current_audit.get("issues") or []]
        if _projection_runtime_policy_stale(projection) and "posterior_runtime_policy_stale" not in current_issues:
            current_issues.append("posterior_runtime_policy_stale")
        if (
            _projection_prior_coverage_refresh_needed(projection)
            and "posterior_prior_coverage_refresh_needed" not in current_issues
        ):
            current_issues.append("posterior_prior_coverage_refresh_needed")
        for issue in current_issues:
            current_issue_counts[issue] += 1
        family = _issue_family(current_issues)
        action = _candidate_action(current_issues, projection)
        if projection.get("projected_ok"):
            projected_ok_count += 1
        if current_issues:
            family_counts[family] += 1
            action_counts[action] += 1
            current_fact_snapshot_id = str(lineage.get("fact_snapshot_id") or "")
            if current_fact_snapshot_id:
                candidate_fact_snapshot_ids.append(current_fact_snapshot_id)
            candidates.append(
                {
                    "fact_id": int(row["id"] or 0),
                    "latest_fact_id": int(row["latest_fact_id"] or 0),
                    "is_latest_fact": bool(row["is_latest_fact"]),
                    "slug": str(row["slug"] or ""),
                    "event_date": str(row["event_date"] or ""),
                    "run_date": str(row["run_date"] or ""),
                    "fact_as_of": str(row["fact_as_of"] or ""),
                    "composer_version": str(row["composer_version"] or ""),
                    "current_fact_snapshot_id": current_fact_snapshot_id,
                    "current_posterior_run_id": str(lineage.get("posterior_run_id") or ""),
                    "current_issues": current_issues,
                    "issue_family": family,
                    "current_target_bucket": current_audit.get("target_bucket"),
                    "current_target_probability": current_audit.get("target_probability"),
                    "current_hard_temperature_floor_c": current_audit.get("hard_temperature_floor_c"),
                    "recommended_action": action,
                    "projection": projection,
                }
            )
    review_anchor_counts = _review_anchor_counts(db, candidate_fact_snapshot_ids)
    for candidate in candidates:
        fact_snapshot_id = str(candidate.get("current_fact_snapshot_id") or "")
        impact = _candidate_impact_surface(
            candidate,
            daily_review_anchor_count=int(review_anchor_counts.get(fact_snapshot_id, 0)),
        )
        candidate["impact"] = impact
        impact_counts[str(impact.get("production_latest_impact") or "unknown")] += 1
        daily_review_impact_counts[str(impact.get("daily_review_impact") or "unknown")] += 1
    can_forward_writer_fix_all_candidates = bool(candidates) and all(
        bool(candidate.get("projection", {}).get("projected_ok")) for candidate in candidates
    )
    current_latest_issue_count = sum(1 for candidate in candidates if bool(candidate.get("is_latest_fact")))
    historical_only_issue_count = len(candidates) - current_latest_issue_count
    current_latest_projected_ok_count = sum(
        1
        for candidate in candidates
        if bool(candidate.get("is_latest_fact"))
        and bool(_as_mapping(candidate.get("projection")).get("projected_ok"))
    )
    current_latest_projected_issue_count = current_latest_issue_count - current_latest_projected_ok_count
    output_limit = max(0, int(max_items_output))
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "db": str(db),
        "scope": {
            "fact_id": str(fact_id or ""),
            "fact_ids": [int(value) for value in _parse_fact_ids(fact_ids)],
            "slug": str(slug or ""),
            "event_date": str(event_date or ""),
            "since_hours": float(since_hours),
            "limit": int(limit),
            "latest_only": bool(latest_only),
        },
        "guards": _guards(),
        "forward_write_acceptance": {
            "checked_fact_count": checked_count,
            "current_issue_fact_count": len(candidates),
            "projected_ok_count": projected_ok_count,
            "projected_issue_count": checked_count - projected_ok_count,
            "can_forward_writer_fix_all_candidates": can_forward_writer_fix_all_candidates,
            "source": "coverage_ok_short_circuit_plus_posterior_ssot_projection_runtime_for_issue_rows",
            "does_not_execute_apply": True,
        },
        "historical_rematerialization_plan": {
            "candidate_count": len(candidates),
            "current_latest_issue_count": current_latest_issue_count,
            "historical_only_issue_count": historical_only_issue_count,
            "current_latest_projected_ok_count": current_latest_projected_ok_count,
            "current_latest_projected_issue_count": current_latest_projected_issue_count,
            "action_counts": dict(sorted(action_counts.items())),
            "issue_family_counts": dict(sorted(family_counts.items())),
            "impact_counts": dict(sorted(impact_counts.items())),
            "daily_review_impact_counts": dict(sorted(daily_review_impact_counts.items())),
            "current_issue_counts": dict(sorted(current_issue_counts.items())),
            "production_latest_chain_blocked_by_candidates": current_latest_issue_count > 0,
            "requires_operator_confirmation": bool(candidates),
            "recommended_apply_mode": "controlled_fact_writer_apply_or_full_posterior_rebuild_after_review",
            "no_apply_plan_only": True,
        },
        "candidates": candidates[:output_limit] if output_limit else [],
        "candidates_truncated": len(candidates) > output_limit,
        "next_boundary": (
            "review_candidates_then_run_temp_drill_or_explicit_controlled_apply_plan"
            if candidates
            else "posterior_ssot_coverage_has_no_rematerialization_candidates_in_scope"
        ),
    }


def compact_plan(report: Mapping[str, Any]) -> dict[str, Any]:
    forward = _as_mapping(report.get("forward_write_acceptance"))
    remat = _as_mapping(report.get("historical_rematerialization_plan"))
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "db": report.get("db"),
        "scope": dict(_as_mapping(report.get("scope"))),
        "guards": dict(_as_mapping(report.get("guards"))),
        "forward_write_acceptance": {
            "checked_fact_count": int(forward.get("checked_fact_count") or 0),
            "current_issue_fact_count": int(forward.get("current_issue_fact_count") or 0),
            "projected_ok_count": int(forward.get("projected_ok_count") or 0),
            "projected_issue_count": int(forward.get("projected_issue_count") or 0),
            "can_forward_writer_fix_all_candidates": bool(
                forward.get("can_forward_writer_fix_all_candidates")
            ),
            "does_not_execute_apply": bool(forward.get("does_not_execute_apply")),
        },
        "historical_rematerialization_plan": {
            "candidate_count": int(remat.get("candidate_count") or 0),
            "current_latest_issue_count": int(remat.get("current_latest_issue_count") or 0),
            "historical_only_issue_count": int(remat.get("historical_only_issue_count") or 0),
            "current_latest_projected_ok_count": int(
                remat.get("current_latest_projected_ok_count") or 0
            ),
            "current_latest_projected_issue_count": int(
                remat.get("current_latest_projected_issue_count") or 0
            ),
            "action_counts": dict(_as_mapping(remat.get("action_counts"))),
            "issue_family_counts": dict(_as_mapping(remat.get("issue_family_counts"))),
            "impact_counts": dict(_as_mapping(remat.get("impact_counts"))),
            "daily_review_impact_counts": dict(_as_mapping(remat.get("daily_review_impact_counts"))),
            "current_issue_counts": dict(_as_mapping(remat.get("current_issue_counts"))),
            "production_latest_chain_blocked_by_candidates": bool(
                remat.get("production_latest_chain_blocked_by_candidates")
            ),
            "requires_operator_confirmation": bool(remat.get("requires_operator_confirmation")),
            "recommended_apply_mode": remat.get("recommended_apply_mode"),
            "no_apply_plan_only": bool(remat.get("no_apply_plan_only")),
        },
        "candidate_output_count": len(report.get("candidates") or []),
        "candidates_truncated": bool(report.get("candidates_truncated")),
        "next_boundary": report.get("next_boundary"),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-id", default="")
    parser.add_argument("--fact-ids", default="", help="Comma-separated event_fact_history ids")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    result = build_plan(
        db_path=args.db,
        fact_id=str(args.fact_id or ""),
        fact_ids=_parse_fact_ids(str(args.fact_ids or "")),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        latest_only=bool(args.latest_only),
        max_items_output=int(args.max_items_output),
    )
    payload = compact_plan(result) if bool(args.summary) else result
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
