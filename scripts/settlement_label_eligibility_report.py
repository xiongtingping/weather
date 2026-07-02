#!/usr/bin/env python3
"""Read-only settlement label eligibility report.

This report explains, per market/date, whether Bayesian entry decisions can be
safely converted into calibration labels from event_runs.settlement_actual.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_settlement_calibration_labels import (
    APPLY_CONFIRM_PHRASE,
    DEFAULT_DB,
    DEFAULT_MARKET_SCOPE,
    _cutoff_iso,
    _event_date_key,
    _event_date_mismatch,
    _is_closed_settlement_row,
    _json_obj,
    _latest_fact_payload,
    _settlement_hard_observation_reject_reason,
    _settlement_source_reject_reason,
    _settlement_unit_boundary_reject_reason,
    _to_float,
    _truthy,
    bucket_hit,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _market_scope_sql(scope: str, *, alias: str = "") -> str:
    scope_key = str(scope or "").strip().lower()
    prefix = f"{alias}." if alias else ""
    if scope_key in {"", "all", "weather"}:
        return ""
    if scope_key in {"highest_temperature", "highest-temp", "highest_temp", "temperature_2m_max"}:
        slug = f"lower({prefix}slug)"
        title = f"lower({prefix}title)"
        return f"""
          AND (
            {slug} LIKE 'highest-temperature-in-%'
            OR {slug} LIKE '%high-temperature%'
            OR {title} LIKE '%highest temperature%'
            OR {title} LIKE '%high temperature%'
          )
          AND {slug} NOT LIKE 'lowest-temperature-in-%'
          AND {title} NOT LIKE '%lowest temperature%'
        """
    return ""


def _decision_rows(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    city: str,
    market_scope: str,
    bayes_only: bool,
) -> List[sqlite3.Row]:
    city_like = f"%{str(city or '').lower()}%"
    market_sql = _market_scope_sql(market_scope, alias="d")
    label_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(calibration_labels)").fetchall()}
    existing_label_expr = "c.id" if "id" in label_cols else "c.decision_id"
    rows = conn.execute(
        f"""
        SELECT
          d.id, d.slug, d.event_date, d.title, d.outcome, d.side, d.details_json,
          d.created_at, {existing_label_expr} AS existing_label_id
        FROM auto_trade_decisions d
        LEFT JOIN calibration_labels c ON c.decision_id = d.id
        WHERE d.action = 'entry'
          AND datetime(d.created_at) >= datetime(?)
          AND (? = '' OR lower(d.slug) LIKE ? OR lower(d.title) LIKE ?)
          {market_sql}
        ORDER BY d.id ASC
        """,
        (since_iso, str(city or "").lower(), city_like, city_like),
    ).fetchall()
    if not bayes_only:
        return rows
    return [row for row in rows if _truthy(_json_obj(row["details_json"]).get("q_bayes_enabled"))]


def _event_rows_by_key(
    conn: sqlite3.Connection,
    keys: List[Tuple[str, str]],
) -> Dict[Tuple[str, str], List[sqlite3.Row]]:
    if not keys:
        return {}
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_runs)").fetchall()}
    settlement_expr = "settlement_actual" if "settlement_actual" in cols else "NULL AS settlement_actual"
    market_closed_expr = "market_closed" if "market_closed" in cols else "NULL AS market_closed"
    event_end_expr = "event_end_utc" if "event_end_utc" in cols else "NULL AS event_end_utc"
    resolution_source_expr = (
        "resolution_source" if "resolution_source" in cols else "NULL AS resolution_source"
    )
    station_id_expr = "station_id" if "station_id" in cols else "NULL AS station_id"
    rounding_rule_expr = "rounding_rule" if "rounding_rule" in cols else "NULL AS rounding_rule"
    market_unit_expr = "market_unit" if "market_unit" in cols else "NULL AS market_unit"
    native_value_expr = (
        "settlement_native_value"
        if "settlement_native_value" in cols
        else "NULL AS settlement_native_value"
    )
    native_unit_expr = (
        "settlement_native_unit"
        if "settlement_native_unit" in cols
        else "NULL AS settlement_native_unit"
    )
    native_precision_expr = (
        "settlement_native_precision"
        if "settlement_native_precision" in cols
        else "NULL AS settlement_native_precision"
    )
    title_expr = "title" if "title" in cols else "NULL AS title"
    out: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
    for slug, event_date in sorted(set(keys)):
        rows = conn.execute(
            f"""
            SELECT id, slug, event_date, {title_expr}, {settlement_expr}, {market_closed_expr},
                   {event_end_expr}, {resolution_source_expr}, {station_id_expr},
                   {rounding_rule_expr}, {market_unit_expr}, {native_value_expr},
                   {native_unit_expr}, {native_precision_expr}, created_at
            FROM event_runs
            WHERE slug = ? AND event_date = ?
            ORDER BY created_at DESC, id DESC
            """,
            (slug, event_date),
        ).fetchall()
        out[(slug, event_date)] = rows
    return out


def _label_counts_for_rows(rows: List[sqlite3.Row], actual: float) -> Tuple[int, Counter[str], Counter[str]]:
    planned = 0
    label_counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for row in rows:
        if row["existing_label_id"] is not None:
            skipped["already_labeled"] += 1
            continue
        hit = bucket_hit(actual, str(row["outcome"] or ""))
        if hit is None:
            skipped["bucket_unparseable"] += 1
            continue
        side = str(row["side"] or "").strip().upper()
        if side not in {"YES", "NO"}:
            skipped["side_invalid"] += 1
            continue
        side_hit = bool(hit) if side == "YES" else not bool(hit)
        label_counts[str(1 if side_hit else 0)] += 1
        planned += 1
    return planned, label_counts, skipped


def _classify_market(
    key: Tuple[str, str],
    decisions: List[sqlite3.Row],
    event_rows: List[sqlite3.Row],
    *,
    fact_payload: Optional[Dict[str, Any]] = None,
    allow_open_events: bool,
    strict_resolution_source: bool,
    now_utc: datetime,
    today_et: str,
) -> Dict[str, Any]:
    slug, event_date = key
    title = str(decisions[0]["title"] or "") if decisions else ""
    if _event_date_mismatch(slug, event_date):
        return {
            "slug": slug,
            "event_date": event_date,
            "title": title,
            "status": "event_date_mismatch",
            "decision_count": len(decisions),
            "planned_labels": 0,
        }
    if not event_rows:
        return {
            "slug": slug,
            "event_date": event_date,
            "title": title,
            "status": "event_run_missing",
            "decision_count": len(decisions),
            "planned_labels": 0,
        }

    latest = event_rows[0]
    closed_rows = [
        row
        for row in event_rows
        if allow_open_events
        or _is_closed_settlement_row(row, now_utc=now_utc, today_et=today_et)
    ]
    closed_actual_rows = [
        row for row in closed_rows if _to_float(row["settlement_actual"]) is not None
    ]

    base = {
        "slug": slug,
        "event_date": event_date,
        "title": title or str(latest["title"] or ""),
        "decision_count": len(decisions),
        "event_run_count": len(event_rows),
        "latest_event_run_id": int(latest["id"] or 0),
        "latest_event_run_at": str(latest["created_at"] or ""),
        "latest_market_closed": int(latest["market_closed"] or 0) if latest["market_closed"] is not None else None,
        "latest_event_end_utc": str(latest["event_end_utc"] or ""),
        "latest_settlement_actual": _to_float(latest["settlement_actual"]),
        "latest_resolution_source": str(latest["resolution_source"] or ""),
        "latest_station_id": str(latest["station_id"] or ""),
    }

    if not closed_rows:
        base.update({"status": "settlement_not_closed", "planned_labels": 0})
        return base
    if closed_rows and not closed_actual_rows:
        base.update({"status": "closed_without_settlement_actual", "planned_labels": 0})
        return base
    if not closed_actual_rows:
        base.update({"status": "settlement_missing", "planned_labels": 0})
        return base

    settlement = closed_actual_rows[0]
    boundary_reject_reason = _settlement_unit_boundary_reject_reason(settlement)
    if boundary_reject_reason:
        base.update(
            {
                "status": boundary_reject_reason,
                "planned_labels": 0,
                "settlement_event_run_id": int(settlement["id"] or 0),
                "settlement_actual": _to_float(settlement["settlement_actual"]),
                "resolution_source": str(settlement["resolution_source"] or ""),
                "station_id": str(settlement["station_id"] or ""),
            }
        )
        return base
    hard_observation_reject_reason = _settlement_hard_observation_reject_reason(
        settlement,
        fact_payload or {},
    )
    if hard_observation_reject_reason:
        base.update(
            {
                "status": hard_observation_reject_reason,
                "planned_labels": 0,
                "settlement_event_run_id": int(settlement["id"] or 0),
                "settlement_actual": _to_float(settlement["settlement_actual"]),
                "resolution_source": str(settlement["resolution_source"] or ""),
                "station_id": str(settlement["station_id"] or ""),
            }
        )
        return base
    if strict_resolution_source:
        reject_reason = _settlement_source_reject_reason(settlement)
        if reject_reason:
            base.update(
                {
                    "status": reject_reason,
                    "planned_labels": 0,
                    "settlement_event_run_id": int(settlement["id"] or 0),
                    "settlement_actual": _to_float(settlement["settlement_actual"]),
                    "resolution_source": str(settlement["resolution_source"] or ""),
                    "station_id": str(settlement["station_id"] or ""),
                }
            )
            return base

    actual = _to_float(settlement["settlement_actual"])
    planned, label_counts, skipped = _label_counts_for_rows(decisions, float(actual))
    status = "label_ready" if planned > 0 else "no_unlabeled_parseable_decisions"
    base.update(
        {
            "status": status,
            "planned_labels": planned,
            "label_counts": dict(label_counts),
            "decision_skipped": dict(skipped),
            "settlement_event_run_id": int(settlement["id"] or 0),
            "settlement_actual": actual,
            "resolution_source": str(settlement["resolution_source"] or ""),
            "station_id": str(settlement["station_id"] or ""),
        }
    )
    return base


def build_report(
    db_path: Path,
    *,
    since_days: int = 30,
    city: str = "",
    market_scope: str = DEFAULT_MARKET_SCOPE,
    bayes_only: bool = True,
    allow_open_events: bool = False,
    strict_resolution_source: bool = True,
    limit: int = 100,
) -> Dict[str, Any]:
    since_iso = _cutoff_iso(since_days)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        decisions = _decision_rows(
            conn,
            since_iso=since_iso,
            city=city,
            market_scope=market_scope,
            bayes_only=bayes_only,
        )
        by_key: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for row in decisions:
            key = (str(row["slug"] or ""), _event_date_key(row["event_date"]))
            by_key.setdefault(key, []).append(row)
        event_rows = _event_rows_by_key(conn, list(by_key.keys()))
        fact_payloads = {
            key: _latest_fact_payload(conn, str(key[0] or ""), str(key[1] or ""))
            for key in by_key.keys()
        }
    finally:
        conn.close()

    now_utc = datetime.now(timezone.utc)
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    markets = [
        _classify_market(
            key,
            rows,
            event_rows.get(key) or [],
            fact_payload=fact_payloads.get(key) or {},
            allow_open_events=allow_open_events,
            strict_resolution_source=strict_resolution_source,
            now_utc=now_utc,
            today_et=today_et,
        )
        for key, rows in sorted(by_key.items())
    ]
    status_counts = Counter(str(item.get("status") or "unknown") for item in markets)
    ready_markets = [item for item in markets if item.get("status") == "label_ready"]
    planned_labels = sum(int(item.get("planned_labels") or 0) for item in ready_markets)
    base_apply_command = (
        f".venv/bin/python scripts/build_settlement_calibration_labels.py --since-days {max(1, int(since_days))} "
        f"--market-scope {market_scope} --apply --confirm-apply {APPLY_CONFIRM_PHRASE}"
    )
    if city:
        base_apply_command += f" --city {city}"
    markets_sorted = sorted(
        markets,
        key=lambda item: (
            0 if item.get("status") == "label_ready" else 1,
            str(item.get("status") or ""),
            str(item.get("event_date") or ""),
            str(item.get("slug") or ""),
        ),
    )
    return {
        "ok": True,
        "read_only": True,
        "db": str(db_path),
        "generated_at": _utc_now(),
        "since_days": max(1, int(since_days)),
        "city_filter": str(city or ""),
        "market_scope": str(market_scope or ""),
        "bayes_only": bool(bayes_only),
        "allow_open_events": bool(allow_open_events),
        "strict_resolution_source": bool(strict_resolution_source),
        "decision_rows": len(decisions),
        "market_count": len(markets),
        "ready_market_count": len(ready_markets),
        "planned_labels": int(planned_labels),
        "status_counts": dict(status_counts),
        "markets": markets_sorted[: max(1, int(limit))] if limit else markets_sorted,
        "apply_command": base_apply_command if planned_labels > 0 else "",
        "next_mainline_task": (
            "review_settlement_label_plan_then_apply" if planned_labels > 0 else "wait_or_repair_settlement_inputs"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only settlement label eligibility report.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--city", default="")
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE, help="highest_temperature|all")
    parser.add_argument("--all-decisions", action="store_true")
    parser.add_argument("--allow-open-events", action="store_true")
    parser.add_argument("--allow-unverified-resolution-source", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        since_days=int(args.since_days),
        city=str(args.city or ""),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        bayes_only=not bool(args.all_decisions),
        allow_open_events=bool(args.allow_open_events),
        strict_resolution_source=not bool(args.allow_unverified_resolution_source),
        limit=max(0, int(args.limit or 0)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
