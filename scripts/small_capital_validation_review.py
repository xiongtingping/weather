#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import paper_trading_validation, tradeability_gap_review  # noqa: E402
from scripts.weather_strategy_plan import MAJOR_WEATHER_CITIES  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "small_capital_validation_review_latest.json"
DEFAULT_SINCE_DAYS = 42
DEFAULT_MIN_VALIDATION_DAYS = 28
DEFAULT_MAX_VALIDATION_DAYS = 42
DEFAULT_MIN_PILOT_CITIES = 1
DEFAULT_MAX_PILOT_CITIES = 2
DEFAULT_MAX_PER_ORDER_USDC = 5.0
DEFAULT_MAX_DAILY_USDC = 25.0
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_OBJECTIVE_AUDIT_REPORT = ROOT / "data" / "reports" / "strategy_objective_completion_audit_latest.json"
DEFAULT_PILOT_CITY_MATRIX_REPORT = ROOT / "data" / "reports" / "pilot_city_validation_matrix_latest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _csv_values(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value or "").split(","):
            text = part.strip()
            if text:
                out.append(text)
    seen: set[str] = set()
    deduped: list[str] = []
    for item in out:
        key = _city_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _csv_env_values(names: Sequence[str]) -> list[str]:
    return _csv_values([os.environ.get(name, "") for name in names])


def _city_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _auto_pilot_cities_from_objective_report(path: Path, limit: int) -> list[str]:
    data = _load_json(path)
    candidates = data.get("pilot_cities") or data.get("recommended_pilot_cities") or []
    out: list[str] = []
    for city in _csv_values(candidates if isinstance(candidates, list) else [str(candidates or "")]):
        key = _city_key(city)
        if not key or key in MAJOR_WEATHER_CITIES:
            continue
        out.append(key)
        if len(out) >= max(1, int(limit or 1)):
            break
    return _csv_values(out)


def _auto_pilot_cities_from_matrix_report(path: Path, limit: int) -> list[str]:
    data = _load_json(path)
    out: list[str] = []
    for item in data.get("cities") or []:
        if not isinstance(item, dict):
            continue
        key = _city_key(str(item.get("city") or ""))
        if not key or key in MAJOR_WEATHER_CITIES:
            continue
        blockers = {str(code or "") for code in item.get("blockers") or []}
        if "hit_rate_below_min" in blockers:
            continue
        out.append(key)
        if len(out) >= max(1, int(limit or 1)):
            break
    return _csv_values(out)


def _default_pilot_cities(max_pilot_cities: int) -> tuple[list[str], str]:
    env_cities = _csv_env_values(["PM_SMALL_CAPITAL_PILOT_CITIES", "PM_PAPER_VALIDATION_CITY"])
    if env_cities:
        return env_cities[: max(1, int(max_pilot_cities or 1))], "env"
    objective_cities = _auto_pilot_cities_from_objective_report(
        DEFAULT_OBJECTIVE_AUDIT_REPORT,
        max_pilot_cities,
    )
    if objective_cities:
        return objective_cities, "objective_audit_latest"
    matrix_cities = _auto_pilot_cities_from_matrix_report(
        DEFAULT_PILOT_CITY_MATRIX_REPORT,
        max_pilot_cities,
    )
    if matrix_cities:
        return matrix_cities, "pilot_city_validation_matrix_latest"
    return [], "none"


def _positive_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    return out if out > 0 else float(default)


def _slug_city_key(slug: Any) -> str:
    text = str(slug or "").strip().lower()
    prefix = "highest-temperature-in-"
    suffix = "-on-"
    if text.startswith(prefix) and suffix in text:
        return _city_key(text[len(prefix) : text.rfind(suffix)])
    return ""


def _latest_gap_event_dates(db_path: Path) -> list[str]:
    try:
        return paper_trading_validation._latest_event_dates(db_path, limit=2)
    except Exception:
        return []


def _build_pilot_candidate_review(
    db_path: Path,
    *,
    pilot_city_keys: Sequence[str],
    market_scope: str,
) -> dict[str, Any]:
    event_dates = _latest_gap_event_dates(db_path)
    empty_city = {
        city: {
            "paper_observation_candidates": 0,
            "small_capital_candidates": 0,
            "by_review_action": {},
            "by_candidate_strategy": {},
            "rows": [],
        }
        for city in pilot_city_keys
    }
    if not event_dates:
        return {
            "ok": True,
            "event_dates": [],
            "paper_observation_allowed": False,
            "small_capital_candidate_ready": False,
            "by_city": empty_city,
            "source": "tradeability_gap_review",
        }
    try:
        review = tradeability_gap_review.build_review(
            db_path,
            event_dates,
            limit=50,
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    except Exception as exc:
        return {
            "ok": False,
            "event_dates": event_dates,
            "error": f"{type(exc).__name__}:{exc}",
            "paper_observation_allowed": False,
            "small_capital_candidate_ready": False,
            "by_city": empty_city,
            "source": "tradeability_gap_review",
        }

    by_city = dict(empty_city)
    for row in review.get("rows") or []:
        if not isinstance(row, dict):
            continue
        city = _slug_city_key(row.get("slug"))
        if city not in by_city:
            continue
        action = str(row.get("review_action") or "")
        strategy = str(row.get("candidate_strategy") or "")
        city_bucket = by_city[city]
        city_bucket["rows"].append(row)
        city_bucket["by_review_action"][action] = int(city_bucket["by_review_action"].get(action, 0)) + 1
        city_bucket["by_candidate_strategy"][strategy] = int(
            city_bucket["by_candidate_strategy"].get(strategy, 0)
        ) + 1
        if action.startswith("paper_track_"):
            city_bucket["paper_observation_candidates"] += 1

    total_paper = sum(int(item.get("paper_observation_candidates") or 0) for item in by_city.values())
    return {
        "ok": bool(review.get("ok", True)),
        "event_dates": event_dates,
        "paper_observation_allowed": total_paper > 0,
        "small_capital_candidate_ready": False,
        "by_city": by_city,
        "source": "tradeability_gap_review",
        "review_summary": {
            "gap_rows": review.get("gap_rows"),
            "by_gap_reason": review.get("by_gap_reason") or {},
            "by_review_action": review.get("by_review_action") or {},
            "by_candidate_strategy": review.get("by_candidate_strategy") or {},
        },
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    pilot_cities: Sequence[str],
    since_days: int = DEFAULT_SINCE_DAYS,
    min_validation_days: int = DEFAULT_MIN_VALIDATION_DAYS,
    max_validation_days: int = DEFAULT_MAX_VALIDATION_DAYS,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    min_pilot_cities: int = DEFAULT_MIN_PILOT_CITIES,
    max_pilot_cities: int = DEFAULT_MAX_PILOT_CITIES,
    max_per_order_usdc: float = DEFAULT_MAX_PER_ORDER_USDC,
    max_daily_usdc: float = DEFAULT_MAX_DAILY_USDC,
) -> dict[str, Any]:
    requested_cities = _csv_values(pilot_cities)
    if requested_cities:
        cities = requested_cities
        pilot_city_source = "explicit"
    else:
        cities, pilot_city_source = _default_pilot_cities(max_pilot_cities)
    city_keys = [_city_key(city) for city in cities]
    blockers: list[str] = []
    warnings: list[str] = []

    if len(city_keys) < int(min_pilot_cities):
        blockers.append("pilot_city_below_min")
    if len(city_keys) > int(max_pilot_cities):
        blockers.append("pilot_city_above_max")
    min_days = max(1, int(min_validation_days))
    max_days = max(min_days, int(max_validation_days))
    if int(since_days) < min_days:
        blockers.append("small_capital_validation_window_below_min_days")
    if int(since_days) > max_days:
        warnings.append("small_capital_validation_window_above_recommended_max_days")

    major_cities = [city for city in city_keys if city in MAJOR_WEATHER_CITIES]
    if major_cities:
        blockers.append("pilot_city_not_secondary")

    per_order_cap = _positive_float(max_per_order_usdc, DEFAULT_MAX_PER_ORDER_USDC)
    daily_cap = _positive_float(max_daily_usdc, DEFAULT_MAX_DAILY_USDC)
    if daily_cap < per_order_cap:
        blockers.append("daily_cap_below_order_cap")
    if per_order_cap > 25.0:
        blockers.append("per_order_cap_too_large_for_small_capital")
    if daily_cap > 100.0:
        blockers.append("daily_cap_too_large_for_small_capital")

    city_reports: list[dict[str, Any]] = []
    pilot_candidate_review = _build_pilot_candidate_review(
        db_path,
        pilot_city_keys=city_keys,
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    if not pilot_candidate_review.get("ok", True):
        warnings.append("pilot_candidate_review_error")
    for city in city_keys:
        report = paper_trading_validation.build_report(
            db_path,
            since_days=max(1, int(since_days)),
            city=city,
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
        city_reports.append(
            {
                "city": city,
                "ok": bool(report.get("ok")),
                "blockers": report.get("blockers") or [],
                "warnings": report.get("warnings") or [],
                "paper_status": report.get("paper_status") or {},
            }
        )
        if not report.get("ok"):
            blockers.append(f"paper_validation_not_passed:{city}")
        warnings.extend(f"paper_warning:{city}:{code}" for code in report.get("warnings") or [])
        candidate_city = (pilot_candidate_review.get("by_city") or {}).get(city, {})
        if int(candidate_city.get("paper_observation_candidates") or 0) > 0:
            warnings.append(f"paper_pilot_candidates_pending_review:{city}")

    ok = not blockers
    return {
        "ok": bool(ok),
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "validation_window": {
            "min_days": int(min_days),
            "max_recommended_days": int(max_days),
            "within_required_window": bool(int(since_days) >= min_days),
        },
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "pilot_scope": {
            "cities": city_keys,
            "source": pilot_city_source,
            "min_cities": int(min_pilot_cities),
            "max_cities": int(max_pilot_cities),
            "major_cities": major_cities,
            "secondary_only": not major_cities,
        },
        "small_capital_caps": {
            "max_per_order_usdc": per_order_cap,
            "max_daily_usdc": daily_cap,
        },
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "city_reports": city_reports,
        "pilot_candidate_review": pilot_candidate_review,
        "next_mainline_task": "small_capital_pilot_enable_review" if ok else "small_capital_validation_review",
        "next_actions": (
            [
                "enable_small_capital_only_for_listed_secondary_pilot_cities",
                "keep_major_cities_in_settlement_review_or_paper_mode",
                "monitor_small_capital_pilot_for_4_to_6_weeks_before_scaling",
            ]
            if ok
            else [
                "review_tradeability_gap_paper_candidates_for_listed_pilot_cities",
                "finish_paper_trading_validation_for_1_to_2_secondary_pilot_cities",
                "keep_auto_trading_in_dry_run_or_paper_mode_until_review_passes",
            ]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only small-capital pilot validation review.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pilot-city", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--min-validation-days", type=int, default=DEFAULT_MIN_VALIDATION_DAYS)
    parser.add_argument("--max-validation-days", type=int, default=DEFAULT_MAX_VALIDATION_DAYS)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--min-pilot-cities", type=int, default=DEFAULT_MIN_PILOT_CITIES)
    parser.add_argument("--max-pilot-cities", type=int, default=DEFAULT_MAX_PILOT_CITIES)
    parser.add_argument("--max-per-order-usdc", type=float, default=DEFAULT_MAX_PER_ORDER_USDC)
    parser.add_argument("--max-daily-usdc", type=float, default=DEFAULT_MAX_DAILY_USDC)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(
        Path(args.db),
        pilot_cities=_csv_values(args.pilot_city),
        since_days=int(args.since_days),
        min_validation_days=int(args.min_validation_days),
        max_validation_days=int(args.max_validation_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        min_pilot_cities=int(args.min_pilot_cities),
        max_pilot_cities=int(args.max_pilot_cities),
        max_per_order_usdc=float(args.max_per_order_usdc),
        max_daily_usdc=float(args.max_daily_usdc),
    )
    text = json.dumps(report, ensure_ascii=False, indent=None if args.json else 2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
