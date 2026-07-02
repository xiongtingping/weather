#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import paper_observation_tracking_report  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "paper_observation_rolling_validation_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_SINCE_DAYS = 42
DEFAULT_MIN_VALIDATION_DAYS = 28
DEFAULT_MAX_VALIDATION_DAYS = 42
DEFAULT_MIN_RESOLVED_ROWS = 10
DEFAULT_MIN_STRATEGY_RESOLVED_ROWS = 3
DEFAULT_MIN_HIT_RATE = 0.55


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _rate(num: int, den: int) -> float | None:
    return (int(num) / int(den)) if int(den) else None


def _avg(values: Sequence[float]) -> float | None:
    nums = [float(v) for v in values if math.isfinite(float(v))]
    return (sum(nums) / len(nums)) if nums else None


def _is_reference_row(row: Mapping[str, Any]) -> bool:
    action = str(row.get("review_action") or "")
    risk_edge = _to_float(row.get("risk_edge"))
    size_scale = _to_float(row.get("size_scale"))
    if not action.startswith("paper_track_"):
        return False
    if risk_edge is None or risk_edge <= 0:
        return False
    if size_scale is not None and size_scale <= 0:
        return False
    return True


def _strategy_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    resolved = 0
    pending = 0
    wins = 0
    risk_edges: list[float] = []
    raw_edges: list[float] = []
    statuses: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    for row in rows:
        status = str(row.get("tracking_status") or "")
        statuses[status] += 1
        actions[str(row.get("review_action") or "")] += 1
        if status == "pending_settlement_label":
            pending += 1
        label = row.get("label")
        if label in {0, 1}:
            resolved += 1
            wins += int(label)
        risk = _to_float(row.get("risk_edge"))
        raw = _to_float(row.get("raw_lcb_edge"))
        if risk is not None:
            risk_edges.append(float(risk))
        if raw is not None:
            raw_edges.append(float(raw))
    return {
        "rows": total,
        "resolved_rows": resolved,
        "pending_rows": pending,
        "wins": wins,
        "losses": max(0, resolved - wins),
        "hit_rate": _rate(wins, resolved),
        "avg_risk_edge": _avg(risk_edges),
        "avg_raw_lcb_edge": _avg(raw_edges),
        "by_tracking_status": dict(sorted(statuses.items())),
        "by_review_action": dict(sorted(actions.items())),
    }


def _reference_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if isinstance(row, Mapping) and _is_reference_row(row)]


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    pilot_cities: Sequence[str],
    since_days: int = DEFAULT_SINCE_DAYS,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    min_validation_days: int = DEFAULT_MIN_VALIDATION_DAYS,
    max_validation_days: int = DEFAULT_MAX_VALIDATION_DAYS,
    min_resolved_rows: int = DEFAULT_MIN_RESOLVED_ROWS,
    min_strategy_resolved_rows: int = DEFAULT_MIN_STRATEGY_RESOLVED_ROWS,
    min_hit_rate: float = DEFAULT_MIN_HIT_RATE,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    since = max(1, int(since_days))
    min_days = max(1, int(min_validation_days))
    max_days = max(min_days, int(max_validation_days))
    if since < min_days:
        blockers.append("paper_observation_window_below_min_days")
    if since > max_days:
        warnings.append("paper_observation_window_above_recommended_max_days")

    tracking = paper_observation_tracking_report.build_report(
        db_path,
        pilot_cities=pilot_cities,
        since_days=since,
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    all_rows = [row for row in tracking.get("rows") or [] if isinstance(row, Mapping)]
    rows = _reference_rows(all_rows)
    by_strategy_rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        strategy = str(row.get("candidate_strategy") or "unknown")
        by_strategy_rows.setdefault(strategy, []).append(row)

    overall = _strategy_stats(rows)
    by_strategy = {
        strategy: _strategy_stats(strategy_rows)
        for strategy, strategy_rows in sorted(by_strategy_rows.items())
    }

    resolved = int(overall.get("resolved_rows") or 0)
    hit_rate = _to_float(overall.get("hit_rate"))
    if resolved < int(min_resolved_rows):
        blockers.append("paper_observation_resolved_rows_below_min")
    if hit_rate is None:
        blockers.append("paper_observation_hit_rate_missing")
    elif hit_rate < float(min_hit_rate):
        blockers.append("paper_observation_hit_rate_below_min")

    for strategy, item in by_strategy.items():
        strategy_resolved = int(item.get("resolved_rows") or 0)
        strategy_hit_rate = _to_float(item.get("hit_rate"))
        if strategy_resolved < int(min_strategy_resolved_rows):
            blockers.append(f"strategy_resolved_rows_below_min:{strategy}")
        elif strategy_hit_rate is not None and strategy_hit_rate < float(min_hit_rate):
            blockers.append(f"strategy_hit_rate_below_min:{strategy}")

    if int(overall.get("pending_rows") or 0) > 0:
        warnings.append("paper_observation_pending_labels")
    if not rows:
        blockers.append("paper_observation_rows_missing")

    ok = not blockers
    return {
        "ok": bool(ok),
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": since,
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "validation_window": {
            "min_days": min_days,
            "max_recommended_days": max_days,
            "within_required_window": since >= min_days,
        },
        "thresholds": {
            "min_resolved_rows": int(min_resolved_rows),
            "min_strategy_resolved_rows": int(min_strategy_resolved_rows),
            "min_hit_rate": float(min_hit_rate),
        },
        "pilot_scope": tracking.get("pilot_scope") or {},
        "tracking_summary": tracking.get("summary") or {},
        "reference_filter": {
            "included_rows": len(rows),
            "excluded_rows": max(0, len(all_rows) - len(rows)),
            "rule": "paper_track rows only; risk_edge must be positive; size_scale must be positive when present",
        },
        "overall": overall,
        "by_candidate_strategy": by_strategy,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "next_mainline_task": (
            "small_capital_scale_up_review"
            if ok
            else "paper_observation_rolling_validation"
        ),
        "next_actions": (
            [
                "review_small_capital_scale_up_with_validated_observation_strategies",
                "keep_major_cities_separate_settlement_review",
            ]
            if ok
            else [
                "continue_collecting_paper_observation_labels_for_4_to_6_weeks",
                "keep_small_capital_or_scale_up_blocked_until_strategy_layers_pass",
            ]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only 4-6 week validation for paper observation candidates.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pilot-city", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--min-validation-days", type=int, default=DEFAULT_MIN_VALIDATION_DAYS)
    parser.add_argument("--max-validation-days", type=int, default=DEFAULT_MAX_VALIDATION_DAYS)
    parser.add_argument("--min-resolved-rows", type=int, default=DEFAULT_MIN_RESOLVED_ROWS)
    parser.add_argument("--min-strategy-resolved-rows", type=int, default=DEFAULT_MIN_STRATEGY_RESOLVED_ROWS)
    parser.add_argument("--min-hit-rate", type=float, default=DEFAULT_MIN_HIT_RATE)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cities = paper_observation_tracking_report.pilot_observation_review.small_capital_validation_review._csv_values(
        args.pilot_city
    )
    report = build_report(
        Path(args.db),
        pilot_cities=cities,
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        min_validation_days=int(args.min_validation_days),
        max_validation_days=int(args.max_validation_days),
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
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
