#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import paper_observation_tracking_report, tradeability_gap_review  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "pilot_city_validation_matrix_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_MIN_RESOLVED_ROWS = 10
DEFAULT_MIN_STRATEGY_RESOLVED_ROWS = 3
DEFAULT_MIN_HIT_RATE = 0.55


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_event_dates() -> list[str]:
    tz = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone.utc
    today = datetime.now(tz).date()
    return [today.isoformat(), (today + timedelta(days=1)).isoformat()]


def _city_from_slug(slug: Any) -> str:
    text = str(slug or "").strip().lower()
    match = re.match(r"^highest-temperature-in-(.*?)-on-", text)
    if not match:
        return ""
    return match.group(1).replace("-", " ").strip()


def _csv_values(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value or "").split(","):
            text = part.strip().lower().replace("-", " ")
            if text and text not in out:
                out.append(text)
    return out


def _rate(num: int, den: int) -> float | None:
    return (int(num) / int(den)) if int(den) else None


def _stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    resolved = 0
    wins = 0
    pending = 0
    by_strategy: dict[str, Counter[str]] = {}
    for row in rows:
        strategy = str(row.get("candidate_strategy") or "unknown")
        status = str(row.get("tracking_status") or "")
        by_strategy.setdefault(strategy, Counter())[status] += 1
        if status == "pending_settlement_label":
            pending += 1
        label = row.get("label")
        if label in {0, 1}:
            resolved += 1
            wins += int(label)
    strategies: dict[str, dict[str, Any]] = {}
    for strategy, counter in sorted(by_strategy.items()):
        strategy_rows = [row for row in rows if str(row.get("candidate_strategy") or "unknown") == strategy]
        strategy_resolved = sum(1 for row in strategy_rows if row.get("label") in {0, 1})
        strategy_wins = sum(int(row.get("label") or 0) for row in strategy_rows if row.get("label") in {0, 1})
        strategies[strategy] = {
            "rows": len(strategy_rows),
            "resolved_rows": strategy_resolved,
            "pending_rows": int(counter.get("pending_settlement_label") or 0),
            "wins": strategy_wins,
            "losses": max(0, strategy_resolved - strategy_wins),
            "hit_rate": _rate(strategy_wins, strategy_resolved),
            "by_tracking_status": dict(sorted(counter.items())),
        }
    return {
        "rows": len(rows),
        "resolved_rows": resolved,
        "pending_rows": pending,
        "wins": wins,
        "losses": max(0, resolved - wins),
        "hit_rate": _rate(wins, resolved),
        "by_candidate_strategy": strategies,
    }


def _city_blockers(
    stats: Mapping[str, Any],
    *,
    min_resolved_rows: int,
    min_strategy_resolved_rows: int,
    min_hit_rate: float,
) -> list[str]:
    blockers: list[str] = []
    resolved = int(stats.get("resolved_rows") or 0)
    hit_rate = stats.get("hit_rate")
    if resolved < int(min_resolved_rows):
        blockers.append("resolved_rows_below_min")
    if hit_rate is None:
        blockers.append("hit_rate_missing")
    elif float(hit_rate) < float(min_hit_rate):
        blockers.append("hit_rate_below_min")
    for strategy, item in (stats.get("by_candidate_strategy") or {}).items():
        strategy_resolved = int(item.get("resolved_rows") or 0)
        strategy_hit_rate = item.get("hit_rate")
        if strategy_resolved < int(min_strategy_resolved_rows):
            blockers.append(f"strategy_resolved_rows_below_min:{strategy}")
        elif strategy_hit_rate is not None and float(strategy_hit_rate) < float(min_hit_rate):
            blockers.append(f"strategy_hit_rate_below_min:{strategy}")
    if not int(stats.get("rows") or 0):
        blockers.append("paper_observation_rows_missing")
    return sorted(set(blockers))


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    event_dates: Sequence[str] = (),
    market_scope: str = DEFAULT_MARKET_SCOPE,
    cities: Sequence[str] = (),
    limit: int = 250,
    min_resolved_rows: int = DEFAULT_MIN_RESOLVED_ROWS,
    min_strategy_resolved_rows: int = DEFAULT_MIN_STRATEGY_RESOLVED_ROWS,
    min_hit_rate: float = DEFAULT_MIN_HIT_RATE,
) -> dict[str, Any]:
    event_day_values = [str(day) for day in (event_dates or default_event_dates()) if str(day).strip()]
    review = tradeability_gap_review.build_review(
        db_path,
        event_day_values,
        limit=max(1, int(limit)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    requested = set(_csv_values(cities))
    compact_rows: list[dict[str, Any]] = []
    for row in review.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        city = _city_from_slug(row.get("slug"))
        if not city:
            continue
        if requested and city not in requested:
            continue
        if not str(row.get("review_action") or "").startswith("paper_track_"):
            continue
        compact = dict(row)
        compact["city"] = city
        compact_rows.append(compact)

    keys = [paper_observation_tracking_report._row_key(row) for row in compact_rows]
    labels = paper_observation_tracking_report._load_calibration_labels(db_path, keys)
    settlements = paper_observation_tracking_report._load_settlements(db_path, keys)
    tracked_rows = [
        paper_observation_tracking_report._attach_result(row, labels=labels, settlements=settlements)
        for row in compact_rows
    ]

    by_city_rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in tracked_rows:
        by_city_rows.setdefault(str(row.get("city") or ""), []).append(row)

    city_rows: list[dict[str, Any]] = []
    for city, rows in sorted(by_city_rows.items()):
        stats = _stats(rows)
        blockers = _city_blockers(
            stats,
            min_resolved_rows=int(min_resolved_rows),
            min_strategy_resolved_rows=int(min_strategy_resolved_rows),
            min_hit_rate=float(min_hit_rate),
        )
        city_rows.append(
            {
                "city": city,
                "ok": not blockers,
                "blockers": blockers,
                **stats,
            }
        )
    city_rows.sort(
        key=lambda item: (
            not bool(item.get("ok")),
            -(int(item.get("resolved_rows") or 0)),
            -(float(item.get("hit_rate") or 0.0)),
            str(item.get("city") or ""),
        )
    )
    return {
        "ok": all(bool(item.get("ok")) for item in city_rows) if city_rows else False,
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "event_dates": event_day_values,
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "thresholds": {
            "min_resolved_rows": int(min_resolved_rows),
            "min_strategy_resolved_rows": int(min_strategy_resolved_rows),
            "min_hit_rate": float(min_hit_rate),
        },
        "city_count": len(city_rows),
        "cities": city_rows,
        "next_mainline_task": (
            "paper_observation_rolling_validation"
            if not any(bool(item.get("ok")) for item in city_rows)
            else "strategy_objective_completion_audit_for_best_pilot_city"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only pilot city validation matrix.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--event-date", action="append", default=[])
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--city", action="append", default=[])
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--min-resolved-rows", type=int, default=DEFAULT_MIN_RESOLVED_ROWS)
    parser.add_argument("--min-strategy-resolved-rows", type=int, default=DEFAULT_MIN_STRATEGY_RESOLVED_ROWS)
    parser.add_argument("--min-hit-rate", type=float, default=DEFAULT_MIN_HIT_RATE)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    event_dates = args.event_date or default_event_dates()
    report = build_report(
        Path(args.db),
        event_dates=event_dates,
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        cities=_csv_values(args.city),
        limit=max(1, int(args.limit)),
        min_resolved_rows=int(args.min_resolved_rows),
        min_strategy_resolved_rows=int(args.min_strategy_resolved_rows),
        min_hit_rate=float(args.min_hit_rate),
    )
    text = json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("cities") else 1


if __name__ == "__main__":
    raise SystemExit(main())
