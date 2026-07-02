#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import small_capital_validation_review  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "pilot_observation_review_latest.md"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_SINCE_DAYS = 42


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _format_pct(value: Any) -> str:
    num = _to_float(value)
    if num is None:
        return "-"
    return f"{num * 100:.1f}%"


def _format_price(value: Any) -> str:
    num = _to_float(value)
    if num is None:
        return "-"
    return f"{num:.3f}"


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    action = str(row.get("review_action") or "")
    action_rank = 0 if action.startswith("paper_track_") else 1
    raw_edge = _to_float(row.get("raw_lcb_edge")) or 0.0
    return (
        str(row.get("city") or ""),
        action_rank,
        -raw_edge,
        str(row.get("slug") or ""),
        str(row.get("outcome") or ""),
    )


def _compact_row(city: str, row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "city": city,
        "event_date": row.get("event_date"),
        "slug": row.get("slug"),
        "outcome": row.get("outcome"),
        "side": row.get("side"),
        "candidate_strategy": row.get("candidate_strategy"),
        "execution_direction": row.get("execution_direction"),
        "review_action": row.get("review_action"),
        "gap_reason": row.get("gap_reason"),
        "raw_lcb_edge": row.get("raw_lcb_edge"),
        "risk_edge": row.get("counterfactual_entry_usable_edge"),
        "size_scale": row.get("counterfactual_entry_usable_edge_size_scale"),
        "ask": row.get("ask"),
        "bid": row.get("bid"),
        "q_bayes_lcb": row.get("q_bayes_lcb"),
        "q_bayes_posterior": row.get("q_bayes_posterior"),
        "station_id": row.get("station_id"),
        "source_fit_tier": row.get("source_fit_tier"),
        "entry_probability_used": row.get("entry_probability_used"),
        "exit_probability_reference": row.get("exit_probability_reference"),
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    pilot_cities: Sequence[str],
    since_days: int = DEFAULT_SINCE_DAYS,
    market_scope: str = DEFAULT_MARKET_SCOPE,
) -> dict[str, Any]:
    small_report = small_capital_validation_review.build_report(
        db_path,
        pilot_cities=pilot_cities,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    candidate_review = (
        small_report.get("pilot_candidate_review")
        if isinstance(small_report.get("pilot_candidate_review"), dict)
        else {}
    )
    rows: list[dict[str, Any]] = []
    city_summary: dict[str, dict[str, Any]] = {}
    for city, bucket in (candidate_review.get("by_city") or {}).items():
        if not isinstance(bucket, Mapping):
            continue
        city_key = str(city)
        city_summary[city_key] = {
            "paper_observation_candidates": int(
                bucket.get("paper_observation_candidates") or 0
            ),
            "small_capital_candidates": int(bucket.get("small_capital_candidates") or 0),
            "by_review_action": dict(bucket.get("by_review_action") or {}),
            "by_candidate_strategy": dict(bucket.get("by_candidate_strategy") or {}),
        }
        for row in bucket.get("rows") or []:
            if isinstance(row, Mapping):
                rows.append(_compact_row(city_key, row))

    rows.sort(key=_row_sort_key)
    return {
        "ok": bool(small_report.get("ok")),
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "pilot_scope": small_report.get("pilot_scope") or {},
        "blockers": small_report.get("blockers") or [],
        "warnings": small_report.get("warnings") or [],
        "paper_observation_allowed": bool(candidate_review.get("paper_observation_allowed")),
        "small_capital_candidate_ready": bool(
            candidate_review.get("small_capital_candidate_ready")
        ),
        "event_dates": candidate_review.get("event_dates") or [],
        "city_summary": city_summary,
        "rows": rows,
        "next_mainline_task": (
            "paper_observation_candidate_review"
            if rows
            else "small_capital_validation_review"
        ),
        "next_actions": [
            "paper_track_listed_observation_candidates",
            "keep_small_capital_blocked_until_paper_validation_passes",
            "compare_observation_candidates_after_settlement_labels_arrive",
        ]
        if rows
        else [
            "continue_scanning_for_secondary_or_tail_no_paper_candidates",
            "keep_small_capital_blocked_until_paper_validation_passes",
        ],
    }


def format_markdown(report: Mapping[str, Any]) -> str:
    blockers = ",".join(str(x) for x in report.get("blockers") or []) or "-"
    lines = [
        "# Pilot Observation Review",
        "",
        f"- generated_at: {report.get('generated_at')}",
        f"- event_dates: {', '.join(str(x) for x in report.get('event_dates') or []) or '-'}",
        f"- market_scope: {report.get('market_scope')}",
        f"- ok: {bool(report.get('ok'))}",
        f"- paper_observation_allowed: {bool(report.get('paper_observation_allowed'))}",
        f"- small_capital_candidate_ready: {bool(report.get('small_capital_candidate_ready'))}",
        f"- blockers: {blockers}",
        "",
        "## City Summary",
        "",
        "| city | obs | small-cap | reverse NO | D0/Settlement | excluded |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for city, item in sorted((report.get("city_summary") or {}).items()):
        if not isinstance(item, Mapping):
            continue
        strategies = item.get("by_candidate_strategy") if isinstance(item.get("by_candidate_strategy"), Mapping) else {}
        actions = item.get("by_review_action") if isinstance(item.get("by_review_action"), Mapping) else {}
        lines.append(
            "| {city} | {obs} | {small} | {reverse} | {d0} | {excluded} |".format(
                city=city,
                obs=int(item.get("paper_observation_candidates") or 0),
                small=int(item.get("small_capital_candidates") or 0),
                reverse=int(strategies.get("reverse_edge_buy_no_sell_yes") or 0),
                d0=int(strategies.get("d0_physical_settlement_hybrid") or 0),
                excluded=int(actions.get("exclude_until_tradeability_gap_fixed") or 0),
            )
        )
    lines.extend(
        [
            "",
            "## Candidate Rows",
            "",
            "| # | city | action | strategy | direction | outcome | raw edge | risk edge | size | ask | bid | station | reason |",
            "|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for idx, row in enumerate(report.get("rows") or [], start=1):
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {idx} | {city} | {action} | {strategy} | {direction} | {outcome} | {raw} | {risk} | {size} | {ask} | {bid} | {station} | {reason} |".format(
                idx=idx,
                city=str(row.get("city") or ""),
                action=str(row.get("review_action") or ""),
                strategy=str(row.get("candidate_strategy") or ""),
                direction=str(row.get("execution_direction") or ""),
                outcome=str(row.get("outcome") or ""),
                raw=_format_pct(row.get("raw_lcb_edge")),
                risk=_format_pct(row.get("risk_edge")),
                size=_format_pct(row.get("size_scale")),
                ask=_format_price(row.get("ask")),
                bid=_format_price(row.get("bid")),
                station=str(row.get("station_id") or ""),
                reason=str(row.get("gap_reason") or ""),
            )
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only pilot observation candidate table.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pilot-city", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="write rendered report to this path; pass empty string to only print",
    )
    args = parser.parse_args()

    cities = small_capital_validation_review._csv_values(args.pilot_city)
    report = build_report(
        Path(args.db),
        pilot_cities=cities,
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
    )
    text = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if args.format == "json"
        else format_markdown(report)
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
