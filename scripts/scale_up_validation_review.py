#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import paper_observation_rolling_validation, small_capital_validation_review  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_SINCE_DAYS = 42
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_MAX_SCALE_PER_ORDER_USDC = 25.0
DEFAULT_MAX_SCALE_DAILY_USDC = 100.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    return float(out) if out > 0 else float(default)


def _csv_values(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value or "").split(","):
            text = part.strip()
            if text:
                out.append(text)
    return out


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    pilot_cities: Sequence[str],
    since_days: int = DEFAULT_SINCE_DAYS,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    max_scale_per_order_usdc: float = DEFAULT_MAX_SCALE_PER_ORDER_USDC,
    max_scale_daily_usdc: float = DEFAULT_MAX_SCALE_DAILY_USDC,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []

    small_cap = small_capital_validation_review.build_report(
        db_path,
        pilot_cities=pilot_cities,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    if not small_cap.get("ok"):
        blockers.append("small_capital_validation_not_passed")
        blockers.extend(f"small_cap:{code}" for code in small_cap.get("blockers") or [])
    warnings.extend(f"small_cap:{code}" for code in small_cap.get("warnings") or [])

    validation_window = (
        small_cap.get("validation_window")
        if isinstance(small_cap.get("validation_window"), dict)
        else {}
    )
    min_days = int(validation_window.get("min_days") or 28)
    if int(since_days) < min_days:
        blockers.append("scale_up_validation_window_below_min_days")

    pilot_scope = (
        small_cap.get("pilot_scope")
        if isinstance(small_cap.get("pilot_scope"), dict)
        else {}
    )
    pilot_count = len(list(pilot_scope.get("cities") or []))
    if pilot_count < 1:
        blockers.append("scale_up_pilot_city_missing")
    if pilot_count > 2:
        blockers.append("scale_up_pilot_city_above_max")
    if not bool(pilot_scope.get("secondary_only")):
        blockers.append("scale_up_requires_secondary_pilot_only")

    per_order = _positive_float(max_scale_per_order_usdc, DEFAULT_MAX_SCALE_PER_ORDER_USDC)
    daily = _positive_float(max_scale_daily_usdc, DEFAULT_MAX_SCALE_DAILY_USDC)
    if daily < per_order:
        blockers.append("scale_up_daily_cap_below_order_cap")
    small_caps = (
        small_cap.get("small_capital_caps")
        if isinstance(small_cap.get("small_capital_caps"), dict)
        else {}
    )
    small_per_order = _positive_float(
        small_caps.get("max_per_order_usdc"),
        small_capital_validation_review.DEFAULT_MAX_PER_ORDER_USDC,
    )
    small_daily = _positive_float(
        small_caps.get("max_daily_usdc"),
        small_capital_validation_review.DEFAULT_MAX_DAILY_USDC,
    )
    if per_order > max(5.0 * small_per_order, DEFAULT_MAX_SCALE_PER_ORDER_USDC):
        blockers.append("scale_up_per_order_jump_too_large")
    if daily > max(5.0 * small_daily, DEFAULT_MAX_SCALE_DAILY_USDC):
        blockers.append("scale_up_daily_jump_too_large")

    paper_observation_validation = paper_observation_rolling_validation.build_report(
        db_path,
        pilot_cities=pilot_cities,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    if not paper_observation_validation.get("ok"):
        blockers.append("paper_observation_validation_not_passed")
        blockers.extend(
            f"paper_observation:{code}"
            for code in paper_observation_validation.get("blockers") or []
        )
    warnings.extend(
        f"paper_observation:{code}"
        for code in paper_observation_validation.get("warnings") or []
    )

    ok = not blockers
    return {
        "ok": bool(ok),
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "scale_up_caps": {
            "max_per_order_usdc": per_order,
            "max_daily_usdc": daily,
        },
        "pilot_scope": pilot_scope,
        "small_capital_review": small_cap,
        "paper_observation_validation": paper_observation_validation,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "next_mainline_task": "limited_scale_up_enable_review" if ok else "scale_up_validation_review",
        "next_actions": (
            [
                "enable_limited_scale_only_for_validated_secondary_pilot_cities",
                "keep_major_cities_settlement_only_or_paper_until_separate_validation",
                "continue_daily_scale_up_readiness_review",
            ]
            if ok
            else [
                "finish_paper_and_small_capital_validation_before_scaling",
                "keep_entry_batch_limited_to_1_to_2_secondary_pilot_cities",
            ]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only scale-up validation review.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pilot-city", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--max-scale-per-order-usdc", type=float, default=DEFAULT_MAX_SCALE_PER_ORDER_USDC)
    parser.add_argument("--max-scale-daily-usdc", type=float, default=DEFAULT_MAX_SCALE_DAILY_USDC)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(
        Path(args.db),
        pilot_cities=_csv_values(args.pilot_city),
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        max_scale_per_order_usdc=float(args.max_scale_per_order_usdc),
        max_scale_daily_usdc=float(args.max_scale_daily_usdc),
    )
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
