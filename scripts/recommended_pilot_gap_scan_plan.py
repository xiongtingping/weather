#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_AUDIT = ROOT / "data" / "reports" / "strategy_objective_completion_audit_latest.json"
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "recommended_pilot_gap_scan_plan_latest.json"
DEFAULT_TARGET_PACKAGE = ROOT / "data" / "reports" / "paper_observation_target_package_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _csv_values(values: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value or "").split(","):
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return out


def _slug_city(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def _targeted_status(audit: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = audit.get("current_strategy_batch_summary")
    if not isinstance(summary, Mapping):
        return {}
    status = summary.get("targeted_candidate_status")
    return status if isinstance(status, Mapping) else {}


def _validation_from_audit(audit: Mapping[str, Any]) -> Mapping[str, Any]:
    status = _targeted_status(audit)
    validation = status.get("recommended_pilot_observation_validation")
    return validation if isinstance(validation, Mapping) else {}


def _open_market_slugs_by_city(audit: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    status = _targeted_status(audit)
    readiness = status.get("recommended_pilot_settlement_readiness")
    if not isinstance(readiness, Mapping):
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    by_city = readiness.get("by_city")
    if not isinstance(by_city, Mapping):
        return out
    for city_raw, item in by_city.items():
        if not isinstance(item, Mapping):
            continue
        city = _slug_city(city_raw)
        if not city:
            continue
        for market in item.get("eligibility_markets_preview") or []:
            if not isinstance(market, Mapping):
                continue
            status_text = str(market.get("status") or "").strip()
            if status_text != "settlement_not_closed":
                continue
            slug = str(market.get("slug") or "").strip()
            if not slug:
                continue
            row = {
                "slug": slug,
                "event_date": str(market.get("event_date") or "").split("T", 1)[0],
                "status": status_text,
            }
            out.setdefault(city, []).append(row)
    return out


def _event_start_date(audit: Mapping[str, Any]) -> str:
    days = []
    for value in audit.get("event_dates") or []:
        text = str(value or "").split("T", 1)[0].strip()
        if text:
            days.append(text)
    return min(days) if days else datetime.now(timezone.utc).date().isoformat()


def _fallback_open_markets_for_city(
    db_path: Path,
    *,
    city: str,
    audit: Mapping[str, Any],
    limit: int,
) -> list[dict[str, str]]:
    if not city or limit <= 0:
        return []
    if not db_path.exists():
        return []
    like_slug = f"highest-temperature-in-{city}-%"
    start_date = _event_start_date(audit)
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT slug, event_date, MAX(updated_at) AS latest_at
            FROM market_tokens
            WHERE slug LIKE ?
              AND event_date >= ?
              AND COALESCE(market_closed, 0) = 0
              AND COALESCE(market_archived, 0) = 0
              AND COALESCE(market_active, 1) != 0
            GROUP BY slug, event_date
            ORDER BY event_date ASC, latest_at DESC
            LIMIT ?
            """,
            (like_slug, start_date, int(limit)),
        )
        rows = cur.fetchall() or []
    except Exception:
        rows = []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    out: list[dict[str, str]] = []
    for row in rows:
        slug = str(row[0] or "").strip()
        event_date = str(row[1] or "").split("T", 1)[0]
        if not slug:
            continue
        out.append(
            {
                "slug": slug,
                "event_date": event_date,
                "status": "open_market_fallback",
            }
        )
    return out


def _strategy_gaps(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gap in item.get("strategy_gaps") or []:
        if not isinstance(gap, Mapping):
            continue
        strategy = str(gap.get("target_strategy") or "").strip()
        directive = str(gap.get("scan_directive") or "").strip()
        needed = int(gap.get("needed_resolved_rows") or 0)
        if not strategy and not directive and needed <= 0:
            continue
        out.append(
            {
                "target_strategy": strategy or "unknown",
                "needed_resolved_rows": max(0, needed),
                "resolved_rows": int(gap.get("resolved_rows") or 0),
                "scan_directive": directive
                or (
                    "scan_open_tail_no_reverse_edge_candidates"
                    if strategy == "reverse_edge_buy_no_sell_yes"
                    else "scan_open_secondary_physical_edge_candidates"
                ),
            }
        )
    return out


def _inferred_wins(*, explicit: Any, resolved_rows: int, hit_rate: Any) -> int:
    try:
        if explicit is not None:
            return int(explicit)
    except Exception:
        pass
    try:
        if hit_rate is not None and int(resolved_rows) > 0:
            return max(0, int(round(float(hit_rate) * int(resolved_rows))))
    except Exception:
        pass
    return 0


def _target_gap_queue_from_audit(
    audit: Mapping[str, Any],
    *,
    target_package: Mapping[str, Any] | None = None,
    include_expansion_candidates: bool = False,
) -> list[dict[str, Any]]:
    summary = target_package if isinstance(target_package, Mapping) and target_package else None
    if not isinstance(summary, Mapping):
        summary = audit.get("paper_observation_target_package_summary")
    if not isinstance(summary, Mapping):
        return []
    matrix_by_city: dict[str, Mapping[str, Any]] = {}
    matrix = audit.get("pilot_city_validation_matrix_summary")
    if isinstance(matrix, Mapping):
        for row in matrix.get("top_cities") or []:
            if not isinstance(row, Mapping):
                continue
            city = _slug_city(row.get("city"))
            if city:
                matrix_by_city[city] = row
    strategy_by_city: dict[str, list[dict[str, Any]]] = {}
    for row in summary.get("strategy_targets") or []:
        if not isinstance(row, Mapping):
            continue
        city = _slug_city(row.get("city"))
        if not city:
            continue
        strategy = str(row.get("target_strategy") or "").strip()
        directive = str(row.get("scan_directive") or "").strip()
        needed = int(row.get("needed_resolved_rows") or 0)
        if not strategy and needed <= 0:
            continue
        strategy_by_city.setdefault(city, []).append(
            {
                "target_strategy": strategy or "unknown",
                "needed_resolved_rows": max(0, needed),
                "resolved_rows": int(row.get("resolved_rows") or 0),
                "scan_directive": directive
                or (
                    "scan_open_tail_no_reverse_edge_candidates"
                    if strategy == "reverse_edge_buy_no_sell_yes"
                    else (
                        "scan_open_d0_physical_settlement_candidates"
                        if strategy == "d0_physical_settlement_hybrid"
                        else "scan_open_secondary_physical_edge_candidates"
                    )
                ),
            }
        )

    queue: list[dict[str, Any]] = []
    source_rows: list[tuple[str, Mapping[str, Any]]] = []
    for row in summary.get("city_targets") or []:
        if isinstance(row, Mapping):
            source_rows.append(("paper_target_pilot", row))
    if include_expansion_candidates:
        for row in summary.get("observation_expansion_city_targets") or []:
            if isinstance(row, Mapping):
                source_rows.append(("paper_expansion_candidate", row))

    for observation_mode, row in source_rows:
        if not isinstance(row, Mapping):
            continue
        city = _slug_city(row.get("city"))
        if not city:
            continue
        matrix_row = matrix_by_city.get(city, {})
        resolved_rows = int(row.get("resolved_rows") or matrix_row.get("resolved_rows") or 0)
        hit_rate = row.get("hit_rate", matrix_row.get("hit_rate"))
        queue.append(
            {
                "city": city,
                "observation_mode": observation_mode,
                "needed_resolved_rows": int(row.get("needed_resolved_rows") or 0),
                "resolved_rows": resolved_rows,
                "pending_rows": int(row.get("pending_rows") or matrix_row.get("pending_rows") or 0),
                "wins": _inferred_wins(
                    explicit=row.get("wins", matrix_row.get("wins")),
                    resolved_rows=resolved_rows,
                    hit_rate=hit_rate,
                ),
                "hit_rate": hit_rate,
                "strategy_gaps": strategy_by_city.get(city) or [],
            }
        )
    return queue


def _round_robin_priority_slugs(plan: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = [item for item in plan if isinstance(item, Mapping)]
    priority_slugs: list[str] = []
    max_len = 0
    for item in rows:
        max_len = max(max_len, len(item.get("open_market_slugs") or []))
    for idx in range(max_len):
        for item in rows:
            slugs = item.get("open_market_slugs") or []
            if idx >= len(slugs):
                continue
            text = str(slugs[idx] or "").strip()
            if text and text not in priority_slugs:
                priority_slugs.append(text)
    return priority_slugs


def build_plan(
    audit_path: Path = DEFAULT_AUDIT,
    *,
    db_path: Path = DEFAULT_DB,
    target_package_path: Path | None = None,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    cities: Sequence[str] | None = None,
    include_expansion_candidates: bool = False,
    open_market_fallback_limit: int = 2,
) -> dict[str, Any]:
    audit = _load_json(audit_path)
    target_package = _load_json(target_package_path) if target_package_path else {}
    validation = _validation_from_audit(audit)
    open_slugs_by_city = _open_market_slugs_by_city(audit)
    city_filter = {_slug_city(city) for city in cities or [] if _slug_city(city)}
    plan: list[dict[str, Any]] = []
    queue = list(validation.get("sample_gap_queue") or [])
    seen_cities = {
        _slug_city(item.get("city"))
        for item in queue
        if isinstance(item, Mapping) and _slug_city(item.get("city"))
    }
    for item in _target_gap_queue_from_audit(
        audit,
        target_package=target_package,
        include_expansion_candidates=include_expansion_candidates,
    ):
        city = _slug_city(item.get("city")) if isinstance(item, Mapping) else ""
        if city and city not in seen_cities:
            queue.append(item)
            seen_cities.add(city)

    for item in queue:
        if not isinstance(item, Mapping):
            continue
        city = _slug_city(item.get("city"))
        if not city or (city_filter and city not in city_filter):
            continue
        strategy_gaps = _strategy_gaps(item)
        directives = []
        for gap in strategy_gaps:
            directive = str(gap.get("scan_directive") or "").strip()
            if directive and directive not in directives:
                directives.append(directive)
        if not directives:
            directives.append("scan_open_recommended_pilot_paper_observation_candidates")
        needed_resolved = max(0, int(item.get("needed_resolved_rows") or 0))
        open_markets = list(open_slugs_by_city.get(city) or [])
        if not open_markets:
            open_markets = _fallback_open_markets_for_city(
                db_path,
                city=city,
                audit=audit,
                limit=max(0, int(open_market_fallback_limit)),
            )
        priority_slugs = []
        for market in open_markets:
            slug = str(market.get("slug") or "").strip()
            if slug and slug not in priority_slugs:
                priority_slugs.append(slug)
        plan.append(
            {
                "city": city,
                "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
                "observation_mode": str(
                    item.get("observation_mode") or "paper_only_not_small_capital"
                ),
                "needed_resolved_rows": needed_resolved,
                "resolved_rows": int(item.get("resolved_rows") or 0),
                "pending_rows": int(item.get("pending_rows") or 0),
                "wins": int(item.get("wins") or 0),
                "hit_rate": item.get("hit_rate"),
                "strategy_gaps": strategy_gaps,
                "scan_directives": directives,
                "open_market_slugs": priority_slugs,
                "open_markets": open_markets,
                "daily_scan_focus": (
                    "prioritize_open_highest_temperature_markets_for_city_and_strategy"
                ),
                "safety": {
                    "read_only": True,
                    "paper_only": True,
                    "no_backfill": True,
                    "no_live_orders": True,
                },
            }
        )
    city_count = len({item["city"] for item in plan})
    strategy_count = sum(len(item.get("strategy_gaps") or []) for item in plan)
    priority_slugs = _round_robin_priority_slugs(plan)
    return {
        "ok": not plan,
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_path": str(audit_path),
        "target_package_path": str(target_package_path) if target_package_path else "",
        "db_path": str(db_path),
        "audit_loaded": bool(audit),
        "target_package_loaded": bool(target_package),
        "audit_generated_at": str(audit.get("generated_at") or "") if audit else "",
        "target_package_generated_at": (
            str(target_package.get("generated_at") or "") if target_package else ""
        ),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "city_filter": sorted(city_filter),
        "include_expansion_candidates": bool(include_expansion_candidates),
        "queue_count": len(plan),
        "city_count": city_count,
        "strategy_gap_count": strategy_count,
        "priority_slug_count": len(priority_slugs),
        "priority_slugs": priority_slugs,
        "plan": plan,
        "blockers": [] if not plan else ["recommended_pilot_sample_gap_open"],
        "next_action": (
            "recommended_pilot_sample_gaps_closed"
            if not plan
            else "prioritize_recommended_pilot_gap_scan_candidates"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only scan plan for recommended pilot paper-observation gaps."
    )
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--target-package", default=str(DEFAULT_TARGET_PACKAGE))
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--city", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument(
        "--include-expansion-candidates",
        action="store_true",
        help="include paper-only expansion cities beyond the current 1-2 pilot scope",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_plan(
        Path(args.audit),
        db_path=Path(args.db),
        target_package_path=Path(args.target_package),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        cities=_csv_values(args.city),
        include_expansion_candidates=bool(args.include_expansion_candidates),
    )
    out_path = Path(args.out) if str(args.out or "").strip() else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
