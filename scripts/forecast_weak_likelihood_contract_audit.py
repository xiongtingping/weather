#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "data" / "archive.db"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cutoff_iso(since_hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(since_hours)))).isoformat()


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _rows(
    conn: sqlite3.Connection,
    *,
    since_hours: float,
    latest_only: bool,
    active_only: bool,
    limit: int,
) -> list[sqlite3.Row]:
    where = "WHERE h.slug LIKE 'highest-temperature-in-chengdu-%'"
    params: list[Any] = []
    if since_hours > 0:
        where += " AND h.fact_as_of >= ?"
        params.append(_cutoff_iso(since_hours))
    if active_only:
        where += " AND h.event_date >= ?"
        params.append(_today_et())
    if latest_only:
        sql = f"""
            SELECT h.*
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            {where}
            ORDER BY h.event_date DESC, h.fact_as_of DESC, h.id DESC
            LIMIT ?
        """
    else:
        sql = f"""
            SELECT h.*
            FROM event_fact_history h
            {where}
            ORDER BY h.fact_as_of DESC, h.id DESC
            LIMIT ?
        """
    params.append(max(1, int(limit)))
    return list(conn.execute(sql, tuple(params)).fetchall())


def _bucket_labels(payload: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    posterior = payload.get("posterior_temperature_distribution")
    if isinstance(posterior, list):
        for row in posterior:
            if isinstance(row, Mapping):
                label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
                if label:
                    labels.append(label)
    if labels:
        return list(dict.fromkeys(labels))
    for key in ("forecast_weak_likelihood", "prior_distribution", "posterior_temperature_distribution_map"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return [str(label) for label in value.keys() if str(label)]
    for key in ("distribution", "rows"):
        rows = payload.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping):
                    label = str(row.get("outcome") or row.get("bucket") or row.get("label") or "").strip()
                    if label:
                        labels.append(label)
    return list(dict.fromkeys(labels))


def _project_payload(row: sqlite3.Row, payload: Mapping[str, Any], *, db_path: Path) -> dict[str, Any]:
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
        write_reason="forecast_weak_likelihood_contract_projection",
    )
    projected = projection.get("projected_payload")
    return projected if isinstance(projected, dict) else dict(payload)


def build_forecast_weak_likelihood_contract_audit(
    *,
    db_path: Path = DEFAULT_DB,
    since_hours: float = 168.0,
    latest_only: bool = False,
    active_only: bool = False,
    project_forward: bool = False,
    limit: int = 50,
    max_items_output: int = 20,
) -> dict[str, Any]:
    from forecast_weak_likelihood_runtime import (
        build_forecast_weak_likelihood,
        build_forecast_weak_likelihood_contract,
    )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = _rows(conn, since_hours=since_hours, latest_only=latest_only, active_only=active_only, limit=limit)
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    for row in rows:
        payload = _parse_json(row["fact_json"])
        if project_forward:
            payload = _project_payload(row, payload, db_path=db_path)
        labels = _bucket_labels(payload)
        trace = _as_mapping(payload.get("source_acquisition_trace"))
        source_observations = trace.get("source_observations") if isinstance(trace.get("source_observations"), list) else []
        output = build_forecast_weak_likelihood(
            buckets=labels,
            source_observations=source_observations,
            rounding_rule=str(payload.get("rounding_rule") or "rounded_degrees"),
            event_date=str(row["event_date"] or payload.get("event_date") or ""),
            phase=str(payload.get("phase") or ""),
        )
        contract = build_forecast_weak_likelihood_contract(output)
        issues = [str(issue) for issue in contract.get("issues") or [] if str(issue)]
        for issue in issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
        items.append(
            {
                "ok": bool(contract.get("ok")),
                "fact_id": int(row["id"] or 0),
                "slug": str(row["slug"] or ""),
                "event_date": str(row["event_date"] or ""),
                "fact_as_of": str(row["fact_as_of"] or ""),
                "coverage_mode": "forward_writer_projection_no_apply" if project_forward else "persisted_fact_json",
                "forecast_weak_likelihood_contract": contract,
                "runtime_na_reasons": output.get("na_reasons") or [],
                "included_source_count": contract.get("included_source_count"),
                "source_group_count": contract.get("source_group_count"),
            }
        )
    issue_items = [item for item in items if not item.get("ok")]
    return {
        "schema": "forecast_weak_likelihood_contract_audit_v1",
        "ok": not issue_items,
        "read_only": True,
        "db": str(db_path),
        "coverage_mode": "forward_writer_projection_no_apply" if project_forward else "persisted_fact_json",
        "scope": {
            "since_hours": float(since_hours),
            "latest_only": bool(latest_only),
            "active_only": bool(active_only),
            "project_forward": bool(project_forward),
            "limit": int(limit),
        },
        "coverage": {
            "checked_fact_count": len(items),
            "ok_fact_count": len(items) - len(issue_items),
            "issue_fact_count": len(issue_items),
        },
        "issue_counts": issue_counts,
        "items": items[: max(0, int(max_items_output))],
        "items_truncated": len(items) > max(0, int(max_items_output)),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "forecast_weak_likelihood_contract_ok_continue_to_metar_strong_likelihood"
            if not issue_items
            else "fix_forecast_weak_likelihood_contract_before_strong_likelihood"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for forecast weak likelihood contract.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--project-forward", action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-items-output", type=int, default=20)
    args = parser.parse_args()
    report = build_forecast_weak_likelihood_contract_audit(
        db_path=Path(args.db),
        since_hours=args.since_hours,
        latest_only=bool(args.latest_only),
        active_only=bool(args.active_only),
        project_forward=bool(args.project_forward),
        limit=args.limit,
        max_items_output=args.max_items_output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
