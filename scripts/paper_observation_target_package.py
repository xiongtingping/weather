#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pilot_city_validation_matrix, small_capital_validation_review  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT_JSON = ROOT / "data" / "reports" / "paper_observation_target_package_latest.json"
DEFAULT_OUT_MD = ROOT / "data" / "reports" / "paper_observation_target_package_latest.md"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_EXPANSION_PRIORITY_CITIES = ("buenos aires", "cape town", "atlanta")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _csv_values(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value or "").split(","):
            text = part.strip().lower().replace("-", " ")
            if text and text not in out:
                out.append(text)
    return out


def _priority_expansion_cities() -> list[str]:
    raw = os.environ.get(
        "PM_OBSERVATION_EXPANSION_PRIORITY_CITIES",
        ",".join(DEFAULT_EXPANSION_PRIORITY_CITIES),
    )
    return _csv_values([raw])


def _ceil_needed_wins(wins: int, resolved: int, min_hit_rate: float) -> int:
    if resolved <= 0:
        return 1
    if resolved and wins / resolved >= float(min_hit_rate):
        return 0
    # Solve (wins + x) / (resolved + x) >= min_hit_rate with x as additional wins.
    denom = 1.0 - float(min_hit_rate)
    if denom <= 0:
        return max(1, resolved - wins)
    needed = (float(min_hit_rate) * float(resolved) - float(wins)) / denom
    return max(0, int(math.ceil(needed - 1e-12)))


def _strategy_target(
    city: str,
    strategy: str,
    item: Mapping[str, Any],
    *,
    min_strategy_resolved_rows: int,
    min_hit_rate: float,
) -> dict[str, Any]:
    resolved = int(item.get("resolved_rows") or 0)
    wins = int(item.get("wins") or 0)
    pending = int(item.get("pending_rows") or 0)
    needed_resolved = max(0, int(min_strategy_resolved_rows) - resolved)
    needed_wins = _ceil_needed_wins(wins, resolved, float(min_hit_rate))
    priority = 0
    if strategy == "reverse_edge_buy_no_sell_yes":
        priority += 40
    if strategy == "d0_physical_settlement_hybrid":
        priority += 35
    priority += max(0, needed_resolved) * 10 + max(0, needed_wins) * 8
    priority += max(0, pending) * 2
    return {
        "city": city,
        "target_strategy": strategy,
        "resolved_rows": resolved,
        "pending_rows": pending,
        "wins": wins,
        "losses": int(item.get("losses") or 0),
        "hit_rate": item.get("hit_rate"),
        "needed_resolved_rows": needed_resolved,
        "needed_additional_wins_for_hit_rate": needed_wins,
        "priority": priority,
        "recommended_observation_action": (
            "prioritize_reverse_no_tail_candidates"
            if strategy == "reverse_edge_buy_no_sell_yes"
            else (
                "prioritize_d0_physical_settlement_candidates"
                if strategy == "d0_physical_settlement_hybrid"
                else "prioritize_secondary_physical_edge_candidates"
            )
        ),
    }


def _city_target(
    city_item: Mapping[str, Any],
    *,
    min_resolved_rows: int,
    min_hit_rate: float,
) -> dict[str, Any]:
    city = str(city_item.get("city") or "")
    resolved = int(city_item.get("resolved_rows") or 0)
    wins = int(city_item.get("wins") or 0)
    pending = int(city_item.get("pending_rows") or 0)
    city_needed = max(0, int(min_resolved_rows) - resolved)
    city_win_needed = _ceil_needed_wins(wins, resolved, float(min_hit_rate))
    return {
        "city": city,
        "ok": bool(city_item.get("ok")),
        "resolved_rows": resolved,
        "pending_rows": pending,
        "wins": wins,
        "losses": int(city_item.get("losses") or 0),
        "hit_rate": city_item.get("hit_rate"),
        "needed_resolved_rows": city_needed,
        "needed_additional_wins_for_hit_rate": city_win_needed,
        "blockers": city_item.get("blockers") or [],
        "priority": city_needed * 10 + city_win_needed * 8 + pending * 2,
    }


def _expansion_city_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    blockers = {str(value) for value in (item.get("blockers") or [])}
    hit_rate = item.get("hit_rate")
    try:
        hit_value = float(hit_rate)
    except (TypeError, ValueError):
        hit_value = 0.0
    priority_cities = _priority_expansion_cities()
    city = str(item.get("city") or "")
    priority_rank = (
        priority_cities.index(city)
        if city in priority_cities
        else len(priority_cities) + 1
    )
    return (
        priority_rank,
        "hit_rate_below_min" in blockers,
        hit_rate is None,
        -int(item.get("pending_rows") or 0),
        -hit_value,
        -int(item.get("resolved_rows") or 0),
        int(item.get("needed_resolved_rows") or 0),
        str(item.get("city") or ""),
    )


def _recommended_pilot_city_targets(
    expansion_city_targets: Sequence[Mapping[str, Any]],
    *,
    requested_cities: Sequence[str],
    max_pilot_cities: int = 2,
) -> list[dict[str, Any]]:
    requested_count = len(_csv_values(requested_cities))
    open_slots = max(0, int(max_pilot_cities) - requested_count)
    if open_slots <= 0:
        return []
    rows: list[dict[str, Any]] = []
    for item in expansion_city_targets:
        if not isinstance(item, Mapping):
            continue
        city = str(item.get("city") or "").strip()
        if not city:
            continue
        rows.append(
            {
                **dict(item),
                "recommended_pilot_action": "add_second_pilot_city_for_paper_observation",
                "recommendation_reason": "best_expansion_city_tracking_profile",
                "observation_mode": "paper_only_not_small_capital",
            }
        )
        if len(rows) >= open_slots:
            break
    return rows


def build_package(
    db_path: Path = DEFAULT_DB,
    *,
    event_dates: Sequence[str] = (),
    market_scope: str = DEFAULT_MARKET_SCOPE,
    cities: Sequence[str] = (),
    limit: int = 250,
    min_resolved_rows: int = pilot_city_validation_matrix.DEFAULT_MIN_RESOLVED_ROWS,
    min_strategy_resolved_rows: int = pilot_city_validation_matrix.DEFAULT_MIN_STRATEGY_RESOLVED_ROWS,
    min_hit_rate: float = pilot_city_validation_matrix.DEFAULT_MIN_HIT_RATE,
    max_cities: int = 5,
) -> dict[str, Any]:
    event_day_values = [str(day) for day in (event_dates or pilot_city_validation_matrix.default_event_dates()) if str(day).strip()]
    matrix = pilot_city_validation_matrix.build_report(
        db_path,
        event_dates=event_day_values,
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        cities=cities,
        limit=max(1, int(limit)),
        min_resolved_rows=int(min_resolved_rows),
        min_strategy_resolved_rows=int(min_strategy_resolved_rows),
        min_hit_rate=float(min_hit_rate),
    )
    discovery_matrix = matrix
    requested_cities = _csv_values(cities)
    if requested_cities:
        discovery_matrix = pilot_city_validation_matrix.build_report(
            db_path,
            event_dates=event_day_values,
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            cities=(),
            limit=max(1, int(limit)),
            min_resolved_rows=int(min_resolved_rows),
            min_strategy_resolved_rows=int(min_strategy_resolved_rows),
            min_hit_rate=float(min_hit_rate),
        )
    city_targets: list[dict[str, Any]] = []
    strategy_targets: list[dict[str, Any]] = []
    for city_item in (matrix.get("cities") or [])[: max(1, int(max_cities))]:
        if not isinstance(city_item, Mapping):
            continue
        city = str(city_item.get("city") or "")
        city_targets.append(
            _city_target(
                city_item,
                min_resolved_rows=int(min_resolved_rows),
                min_hit_rate=float(min_hit_rate),
            )
        )
        for strategy, strategy_item in (city_item.get("by_candidate_strategy") or {}).items():
            if not isinstance(strategy_item, Mapping):
                continue
            target = _strategy_target(
                city,
                str(strategy),
                strategy_item,
                min_strategy_resolved_rows=int(min_strategy_resolved_rows),
                min_hit_rate=float(min_hit_rate),
            )
            if target["needed_resolved_rows"] > 0 or target["needed_additional_wins_for_hit_rate"] > 0:
                strategy_targets.append(target)
    city_targets.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("city") or "")))
    requested_city_set = set(requested_cities)
    expansion_city_targets = [
        _city_target(
            item,
            min_resolved_rows=int(min_resolved_rows),
            min_hit_rate=float(min_hit_rate),
        )
        for item in (discovery_matrix.get("cities") or [])
        if isinstance(item, Mapping)
        and str(item.get("city") or "") not in requested_city_set
        and int(item.get("rows") or 0) > 0
    ]
    expansion_city_targets.sort(key=_expansion_city_sort_key)
    recommended_pilot_city_targets = _recommended_pilot_city_targets(
        expansion_city_targets,
        requested_cities=requested_cities,
        max_pilot_cities=2,
    )
    strategy_targets.sort(
        key=lambda item: (
            -int(item.get("priority") or 0),
            str(item.get("city") or ""),
            str(item.get("target_strategy") or ""),
        )
    )
    return {
        "ok": bool(matrix.get("ok")),
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
        "source": "pilot_city_validation_matrix",
        "matrix_city_count": matrix.get("city_count"),
        "city_targets": city_targets,
        "recommended_pilot_city_targets": recommended_pilot_city_targets,
        "observation_expansion_city_targets": expansion_city_targets[: max(1, int(max_cities))],
        "strategy_targets": strategy_targets,
        "next_mainline_task": (
            "rerun_strategy_objective_completion_audit"
            if matrix.get("ok")
            else "collect_targeted_paper_observation_samples"
        ),
        "next_actions": [
            "prioritize_reverse_no_tail_candidates_until_strategy_resolved_rows_reach_min",
            "prioritize_d0_physical_settlement_candidates_until_hit_rate_clears_gate",
            "rebuild_settlement_labels_when_pending_markets_resolve",
        ],
    }


def format_markdown(package: Mapping[str, Any]) -> str:
    lines = [
        "# Paper Observation Target Package",
        "",
        f"- generated_at: {package.get('generated_at')}",
        f"- market_scope: {package.get('market_scope')}",
        f"- event_dates: {', '.join(str(x) for x in package.get('event_dates') or []) or '-'}",
        f"- ok: {bool(package.get('ok'))}",
        f"- next_mainline_task: {package.get('next_mainline_task')}",
        "",
        "## City Targets",
        "",
        "| city | resolved | pending | hit_rate | need resolved | need wins | blockers |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in package.get("city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        hit = item.get("hit_rate")
        hit_text = "-" if hit is None else f"{float(hit):.3f}"
        lines.append(
            "| {city} | {resolved} | {pending} | {hit} | {need_resolved} | {need_wins} | {blockers} |".format(
                city=item.get("city") or "",
                resolved=int(item.get("resolved_rows") or 0),
                pending=int(item.get("pending_rows") or 0),
                hit=hit_text,
                need_resolved=int(item.get("needed_resolved_rows") or 0),
                need_wins=int(item.get("needed_additional_wins_for_hit_rate") or 0),
                blockers=", ".join(str(x) for x in item.get("blockers") or []) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## Recommended Pilot City Targets",
            "",
            "| city | action | mode | resolved | pending | hit_rate |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for item in package.get("recommended_pilot_city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        hit = item.get("hit_rate")
        hit_text = "-" if hit is None else f"{float(hit):.3f}"
        lines.append(
            "| {city} | {action} | {mode} | {resolved} | {pending} | {hit} |".format(
                city=item.get("city") or "",
                action=item.get("recommended_pilot_action") or "",
                mode=item.get("observation_mode") or "",
                resolved=int(item.get("resolved_rows") or 0),
                pending=int(item.get("pending_rows") or 0),
                hit=hit_text,
            )
        )
    lines.extend(
        [
            "",
            "## Observation Expansion City Targets",
            "",
            "| city | resolved | pending | hit_rate | need resolved | need wins | blockers |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in package.get("observation_expansion_city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        hit = item.get("hit_rate")
        hit_text = "-" if hit is None else f"{float(hit):.3f}"
        lines.append(
            "| {city} | {resolved} | {pending} | {hit} | {need_resolved} | {need_wins} | {blockers} |".format(
                city=item.get("city") or "",
                resolved=int(item.get("resolved_rows") or 0),
                pending=int(item.get("pending_rows") or 0),
                hit=hit_text,
                need_resolved=int(item.get("needed_resolved_rows") or 0),
                need_wins=int(item.get("needed_additional_wins_for_hit_rate") or 0),
                blockers=", ".join(str(x) for x in item.get("blockers") or []) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## Strategy Targets",
            "",
            "| city | strategy | resolved | pending | hit_rate | need resolved | need wins | action |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in package.get("strategy_targets") or []:
        if not isinstance(item, Mapping):
            continue
        hit = item.get("hit_rate")
        hit_text = "-" if hit is None else f"{float(hit):.3f}"
        lines.append(
            "| {city} | {strategy} | {resolved} | {pending} | {hit} | {need_resolved} | {need_wins} | {action} |".format(
                city=item.get("city") or "",
                strategy=item.get("target_strategy") or "",
                resolved=int(item.get("resolved_rows") or 0),
                pending=int(item.get("pending_rows") or 0),
                hit=hit_text,
                need_resolved=int(item.get("needed_resolved_rows") or 0),
                need_wins=int(item.get("needed_additional_wins_for_hit_rate") or 0),
                action=item.get("recommended_observation_action") or "",
            )
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build read-only paper observation target package.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--event-date", action="append", default=[])
    parser.add_argument("--city", action="append", default=[])
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--max-cities", type=int, default=5)
    parser.add_argument("--out-json", default=str(DEFAULT_OUT_JSON))
    parser.add_argument("--out-md", default=str(DEFAULT_OUT_MD))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    event_dates = args.event_date or pilot_city_validation_matrix.default_event_dates()
    cities = _csv_values(args.city)
    if not cities:
        cities = [
            str(city).replace("-", " ")
            for city in small_capital_validation_review._default_pilot_cities(2)[0]
        ]
    package = build_package(
        Path(args.db),
        event_dates=event_dates,
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        cities=cities,
        limit=max(1, int(args.limit)),
        max_cities=max(1, int(args.max_cities)),
    )
    if args.out_json:
        out_json = Path(args.out_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(format_markdown(package) + "\n", encoding="utf-8")
    print(json.dumps(package, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
