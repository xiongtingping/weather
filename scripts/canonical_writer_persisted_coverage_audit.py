#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.posterior_ssot_fact_coverage_audit import audit_fact_payload

DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "canonical_writer_persisted_coverage_audit_v1"
PERSISTED_COVERAGE_CONTRACT_SCHEMA = "fact_writer_persisted_coverage_contract_v1"
WRITE_OWNER = "weather_fact_snapshot_writer"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


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


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _since_text(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(hours)))).isoformat(
        timespec="seconds"
    )


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _load_fact_rows(
    conn: sqlite3.Connection,
    *,
    since_hours: float,
    limit: int,
    latest_only: bool,
    fact_id: str = "",
    slug: str = "",
    event_date: str = "",
    active_only: bool = False,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if fact_id:
        clauses.append("h.id = ?")
        params.append(int(fact_id))
    if slug:
        clauses.append("h.slug = ?")
        params.append(slug)
    if event_date:
        clauses.append("h.event_date = ?")
        params.append(event_date)
    if not fact_id and since_hours > 0:
        clauses.append("h.fact_as_of >= ?")
        params.append(_since_text(float(since_hours)))
    if active_only:
        clauses.append("h.event_date >= ?")
        params.append(_today_et())
    latest_join = "JOIN event_fact_latest l ON l.fact_id = h.id" if latest_only else ""
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of,
               h.composer_version, h.fact_json, h.created_at
        FROM event_fact_history h
        {latest_join}
        {where}
        ORDER BY h.id DESC
        LIMIT ?
        """,
        [*params, max(1, int(limit))],
    ).fetchall()


def _writer_marker_sources(payload: Mapping[str, Any]) -> list[str]:
    sources: list[str] = []
    for key in ("phase0_fact_json_contract", "lineage_state", "fact_layer_state", "db_write_guard"):
        value = _as_mapping(payload.get(key))
        if value.get("write_owner") == WRITE_OWNER:
            sources.append(key)
    return sources


def _persisted_contract(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    phase0 = _as_mapping(payload.get("phase0_fact_json_contract"))
    return _as_mapping(phase0.get("persisted_coverage_contract"))


def _has_strict_marker(payload: Mapping[str, Any]) -> bool:
    contract = _persisted_contract(payload)
    return (
        contract.get("schema") == PERSISTED_COVERAGE_CONTRACT_SCHEMA
        and contract.get("posterior_ssot_coverage_required") is True
    )


def _contract_marker_issues(payload: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    phase0 = _as_mapping(payload.get("phase0_fact_json_contract"))
    contract = _persisted_contract(payload)
    if phase0.get("write_owner") != WRITE_OWNER:
        issues.append("phase0_write_owner_not_canonical_writer")
    if phase0.get("historical_backfill") is not False:
        issues.append("phase0_historical_backfill_not_false")
    missing = phase0.get("missing_required_keys_after_merge_fill")
    if isinstance(missing, list) and missing:
        issues.append("phase0_required_keys_missing")
    if contract.get("schema") != PERSISTED_COVERAGE_CONTRACT_SCHEMA:
        issues.append("persisted_coverage_contract_schema_invalid")
    if contract.get("posterior_ssot_coverage_required") is not True:
        issues.append("persisted_coverage_contract_posterior_ssot_not_required")
    if contract.get("phase_beta_hard_mask_required") is not True:
        issues.append("persisted_coverage_contract_phase_beta_hard_mask_not_required")
    if contract.get("selection_must_be_posterior_ssot_fact") is not True:
        issues.append("persisted_coverage_contract_selection_not_bound_to_ssot")
    if contract.get("historical_backfill") is not False:
        issues.append("persisted_coverage_contract_historical_backfill_not_false")
    for key in ("lineage_state", "fact_layer_state"):
        row = _as_mapping(payload.get(key))
        if row.get("write_owner") != WRITE_OWNER:
            issues.append(f"{key}_write_owner_not_canonical_writer")
    db_guard = _as_mapping(payload.get("db_write_guard"))
    if db_guard.get("write_owner") != WRITE_OWNER:
        issues.append("db_write_guard_write_owner_not_canonical_writer")
    for key in (
        "writes_archive_db",
        "writes_facts_sqlite",
        "writes_event_fact_history",
        "writes_event_fact_latest",
        "writes_event_fact_summary_cache",
        "places_orders",
        "restarts_launchd",
        "historical_backfill",
    ):
        if db_guard.get(key) is True:
            issues.append(f"db_write_guard_{key}_true_in_persisted_payload")
    return issues


def _project_payload_for_row(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    *,
    db_path: Path,
) -> dict[str, Any]:
    from posterior_ssot_projection_runtime import build_fact_projection_no_apply

    projection = build_fact_projection_no_apply(
        slug=str(row["slug"] or payload.get("slug") or ""),
        event_date=str(row["event_date"] or payload.get("event_date") or ""),
        run_date=str(row["run_date"] or payload.get("run_date") or row["event_date"] or ""),
        fact_as_of=str(row["fact_as_of"] or payload.get("fact_as_of") or ""),
        payload=payload,
        current_fact_id=int(row["id"] or 0),
        current_fact_as_of=str(row["fact_as_of"] or ""),
        composer_version=str(row["composer_version"] or ""),
        db_path=db_path,
        write_reason="canonical_writer_persisted_coverage_projection",
    )
    projected = projection.get("projected_payload")
    return {
        "payload": projected if isinstance(projected, dict) else dict(payload),
        "projection_mode": str(projection.get("projection_mode") or ""),
        "projection_context": dict(_as_mapping(projection.get("context"))),
        "projection_issues": list(projection.get("issues") or []),
    }


def _item_for_row(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    *,
    persisted_payload: Mapping[str, Any] | None = None,
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(row)
    writer_sources = _writer_marker_sources(payload)
    strict_marker = _has_strict_marker(payload)
    posterior_audit = audit_fact_payload(payload, meta=meta)
    persisted_audit = audit_fact_payload(persisted_payload or payload, meta=meta)
    marker_issues = _contract_marker_issues(payload) if strict_marker else []
    issues = list(posterior_audit.get("issues") or []) + marker_issues
    item = {
        "ok": not issues,
        "fact_id": row["id"],
        "slug": row["slug"],
        "event_date": row["event_date"],
        "fact_as_of": row["fact_as_of"],
        "composer_version": row["composer_version"],
        "canonical_writer_marker_sources": writer_sources,
        "strict_persisted_coverage_marker": bool(strict_marker),
        "posterior_ssot_fact_present": bool(posterior_audit.get("posterior_ssot_fact_present")),
        "posterior_ssot_fact_available": bool(posterior_audit.get("posterior_ssot_fact_available")),
        "fact_snapshot_id": posterior_audit.get("fact_snapshot_id"),
        "posterior_run_id": posterior_audit.get("posterior_run_id"),
        "phase": posterior_audit.get("phase"),
        "beta_weak": _to_float(posterior_audit.get("beta_weak")),
        "beta_strong": _to_float(posterior_audit.get("beta_strong")),
        "hard_mask_reason": posterior_audit.get("hard_mask_reason"),
        "target_bucket": posterior_audit.get("target_bucket"),
        "target_probability": posterior_audit.get("target_probability"),
        "issues": sorted(set(str(issue) for issue in issues if str(issue))),
        "legacy_unmarked_issues": []
        if strict_marker
        else sorted(set(str(issue) for issue in posterior_audit.get("issues") or [] if str(issue))),
        "persisted_issues": sorted(set(str(issue) for issue in persisted_audit.get("issues") or [] if str(issue))),
    }
    projection_map = _as_mapping(projection)
    if projection_map:
        item["projection_mode"] = str(projection_map.get("projection_mode") or "")
        item["projection_context"] = dict(_as_mapping(projection_map.get("projection_context")))
        item["projection_issues"] = list(projection_map.get("projection_issues") or [])
    return item


def build_canonical_writer_persisted_coverage_audit(
    db_path: Path | str = DEFAULT_DB,
    *,
    fact_id: str = "",
    slug: str = "",
    event_date: str = "",
    since_hours: float = 168.0,
    limit: int = 100,
    max_items_output: int = 20,
    latest_only: bool = False,
    require_marked: bool = False,
    project_forward: bool = False,
    active_only: bool = False,
) -> dict[str, Any]:
    db = Path(db_path)
    warnings: list[str] = []
    blockers: list[str] = []
    issue_counts: Counter[str] = Counter()
    strict_items: list[dict[str, Any]] = []
    legacy_items: list[dict[str, Any]] = []
    ignored_count = 0
    with _connect_ro(db) as conn:
        if not _table_exists(conn, "event_fact_history"):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "read_only": True,
                "db": str(db),
                "blockers": ["event_fact_history_missing"],
                "warnings": [],
                "guards": _guards(),
                "coverage": {"checked_fact_count": 0},
                "items": [],
            }
        if latest_only and not _table_exists(conn, "event_fact_latest"):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "read_only": True,
                "db": str(db),
                "blockers": ["event_fact_latest_missing_for_latest_only_scope"],
                "warnings": [],
                "guards": _guards(),
                "coverage": {"checked_fact_count": 0},
                "items": [],
            }
        rows = _load_fact_rows(
            conn,
            fact_id=fact_id,
            slug=slug,
            event_date=event_date,
            since_hours=float(since_hours),
            limit=int(limit),
            latest_only=bool(latest_only),
            active_only=bool(active_only),
        )
    for row in rows:
        raw = _safe_json(row["fact_json"])
        if not isinstance(raw, Mapping):
            ignored_count += 1
            continue
        writer_sources = _writer_marker_sources(raw)
        if not writer_sources:
            ignored_count += 1
            continue
        analysis_payload = raw
        projection_meta: dict[str, Any] = {}
        if project_forward:
            projection = _project_payload_for_row(row, raw, db_path=db)
            analysis_payload = _as_mapping(projection.get("payload")) or raw
            projection_meta = {
                "projection_mode": projection.get("projection_mode"),
                "projection_context": projection.get("projection_context"),
                "projection_issues": projection.get("projection_issues"),
            }
        item = _item_for_row(
            row,
            analysis_payload,
            persisted_payload=raw,
            projection=projection_meta,
        )
        if item["strict_persisted_coverage_marker"]:
            strict_items.append(item)
            for issue in item["issues"]:
                issue_counts[str(issue)] += 1
        else:
            legacy_items.append(item)
    if not strict_items:
        warnings.append("no_strict_persisted_coverage_marker_rows_in_scope")
        if require_marked:
            blockers.append("strict_persisted_coverage_marker_rows_missing")
    if any(not item.get("ok") for item in strict_items):
        blockers.append("canonical_writer_marked_persisted_coverage_incomplete")
    legacy_issue_count = sum(1 for item in legacy_items if item.get("legacy_unmarked_issues"))
    if legacy_items:
        warnings.append("legacy_canonical_writer_rows_without_persisted_coverage_marker_present")
    if legacy_issue_count:
        warnings.append("legacy_canonical_writer_rows_have_non_blocking_coverage_issues")

    items = strict_items + legacy_items
    compact_items = items[: max(0, int(max_items_output))]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "db": str(db),
        "scope": {
            "fact_id": str(fact_id or ""),
            "slug": str(slug or ""),
            "event_date": str(event_date or ""),
            "since_hours": float(since_hours),
            "limit": int(limit),
            "latest_only": bool(latest_only),
            "require_marked": bool(require_marked),
            "project_forward": bool(project_forward),
            "active_only": bool(active_only),
        },
        "coverage_mode": (
            "canonical_writer_forward_projection_no_apply"
            if project_forward
            else "canonical_writer_persisted_fact_json_strict_marker"
        ),
        "coverage": {
            "checked_fact_count": len(strict_items) + len(legacy_items),
            "strict_marked_fact_count": len(strict_items),
            "strict_marked_ok_count": sum(1 for item in strict_items if item.get("ok")),
            "strict_marked_issue_count": sum(1 for item in strict_items if not item.get("ok")),
            "legacy_unmarked_canonical_count": len(legacy_items),
            "legacy_unmarked_issue_count": legacy_issue_count,
            "ignored_noncanonical_or_invalid_count": ignored_count,
        },
        "issue_counts": dict(sorted(issue_counts.items())),
        "blockers": blockers,
        "warnings": warnings,
        "guards": _guards(),
        "items": compact_items,
        "items_truncated": len(items) > len(compact_items),
        "next_boundary": (
            "canonical_writer_marked_persisted_coverage_ok"
            if not blockers
            else "fix_future_writer_persisted_fact_contract_before_downstream_consumers"
        ),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit for canonical writer persisted fact coverage.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-id", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--require-marked", action="store_true")
    parser.add_argument("--project-forward", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = build_canonical_writer_persisted_coverage_audit(
        db_path=args.db,
        fact_id=str(args.fact_id or ""),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        latest_only=bool(args.latest_only),
        require_marked=bool(args.require_marked),
        project_forward=bool(args.project_forward),
        active_only=bool(args.active_only),
    )
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "coverage_mode": report["coverage_mode"],
            "scope": report["scope"],
            "coverage": report["coverage"],
            "issue_counts": report["issue_counts"],
            "blockers": report["blockers"],
            "warnings": report["warnings"],
            "guards": report["guards"],
            "next_boundary": report["next_boundary"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
