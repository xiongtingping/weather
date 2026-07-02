#!/usr/bin/env python3
"""Read-only review for Bayes raw-edge rows missing tradeability fields."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402
from scripts.weather_strategy_plan import DB_PATH, build_strategy_plan  # noqa: E402

DEFAULT_MIN_RAW_EDGE = 0.10


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _raw_lcb_edge(row: Mapping[str, Any]) -> Optional[float]:
    lcb = _to_float(row.get("q_bayes_lcb"))
    ask = _to_float(row.get("ask"))
    if lcb is None or ask is None:
        return None
    return float(lcb) - float(ask)


def _has_physical_likelihood(row: Mapping[str, Any]) -> bool:
    return bool(
        _truthy(row.get("q_bayes_physical_likelihood_enabled"))
        or (_to_float(row.get("q_bayes_physical_likelihood_evidence")) or 0.0) > 0
        or (_to_float(row.get("weather_likelihood_feature_count")) or 0.0) > 0
        or str(row.get("q_bayes_physical_likelihood_source") or "").strip()
        or str(row.get("weather_likelihood_aux_source") or "").strip()
    )


def _is_gap_row(row: Mapping[str, Any], *, min_raw_edge: float) -> bool:
    if _to_float(row.get("edge_tradeability_profit_value")) is not None:
        return False
    if not _has_physical_likelihood(row):
        return False
    raw_edge = _raw_lcb_edge(row)
    return bool(raw_edge is not None and raw_edge >= float(min_raw_edge))


def _review_context(row: Mapping[str, Any], *, raw_edge: float) -> Dict[str, Any]:
    fact = row.get("_fact_profile") if isinstance(row.get("_fact_profile"), Mapping) else {}
    ask = _to_float(row.get("ask"))
    bid = _to_float(row.get("bid"))
    spread = _to_float(row.get("spread"))
    q_lcb = _to_float(row.get("q_bayes_lcb"))
    station = str(
        row.get("station_id")
        or row.get("source_station_id")
        or fact.get("station_id")
        or ""
    ).strip()
    resolution_source = str(row.get("resolution_source") or fact.get("resolution_source") or "").strip()
    profile = (
        swm.weather_resolution_source_profile(resolution_source, station_id=station)
        if resolution_source or station
        else {}
    )
    source_fit_tier = str(
        row.get("source_fit_tier") or profile.get("source_fit_tier") or ""
    ).strip()
    source_match = row.get("source_station_match")
    if source_match is None:
        source_match = profile.get("source_station_match")
    source_usable = profile.get("source_usable_for_settlement")
    if source_usable is None and source_fit_tier == "settlement_source" and station:
        source_usable = True
    rules_ok = bool(
        source_fit_tier == "settlement_source"
        and source_match is not False
        and source_usable is not False
        and station
    )

    context: Dict[str, Any] = {
        "edge_tradeability_profit_value": float(raw_edge),
        "ask": ask,
        "exec_entry_price": ask,
        "weather_strategy_price": ask,
        "q_lcb": q_lcb,
        "q_entry_lcb": q_lcb,
        "bid": bid,
        "best_bid": bid,
        "spread": spread,
        "event_date": row.get("event_date"),
        "slug": row.get("slug"),
        "resolution_source": resolution_source,
        "weather_strategy_season": row.get("weather_strategy_season"),
        "weather_strategy_rules_ok": rules_ok,
        "weather_strategy_settlement_station": station,
        "station_id": station,
        "weather_strategy_source_usable_for_settlement": bool(source_usable),
    }
    if str(row.get("weather_strategy_mode") or "") == "settlement_dominant":
        context["allow_hold_to_settlement"] = True
    return context


def _load_fact_profiles(db_path: Path, event_dates: Sequence[str]) -> Dict[tuple[str, str], Dict[str, Any]]:
    if not db_path.exists():
        return {}
    placeholders = ",".join("?" for _ in event_dates)
    if not placeholders:
        return {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT latest.slug, latest.event_date, hist.fact_json
            FROM event_fact_latest latest
            JOIN event_fact_history hist ON hist.id = latest.fact_id
            WHERE latest.event_date IN ({placeholders})
            """,
            tuple(str(day).split("T", 1)[0] for day in event_dates),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        try:
            fact = json.loads(str(row["fact_json"] or "{}"))
        except Exception:
            fact = {}
        if not isinstance(fact, dict):
            fact = {}
        out[(str(row["slug"] or ""), str(row["event_date"] or "").split("T", 1)[0])] = {
            "station_id": str(fact.get("station_id") or "").strip(),
            "resolution_source": str(fact.get("resolution_source") or "").strip(),
            "market_var": str(fact.get("market_var") or "").strip(),
            "market_unit": str(fact.get("market_unit") or "").strip(),
            "location_basis": str(fact.get("location_basis") or "").strip(),
        }
    return out


def _classify_gap(row: Mapping[str, Any], audit: Mapping[str, Any]) -> str:
    if str(row.get("weather_strategy_stage") or "").strip() == "settled_or_past":
        return "market_settled_or_past_no_new_entry"
    if audit.get("q_bayes_variance_gate_pass") is False:
        return str(audit.get("q_bayes_variance_skip_reason") or "variance_high")
    if audit.get("entry_edge_false_edge"):
        return str(audit.get("entry_edge_false_edge_type") or "entry_edge_false_edge")
    if audit.get("entry_usable_edge_low_price_block_size"):
        return str(audit.get("entry_usable_edge_low_price_na_reason") or "low_price_exit_block")
    if str(audit.get("entry_usable_edge_station_na_reason") or ""):
        if not str(row.get("station_id") or row.get("source_station_id") or "").strip():
            return "station_missing"
        return str(audit.get("entry_usable_edge_station_na_reason"))
    usable = _to_float(audit.get("entry_usable_edge"))
    size = _to_float(audit.get("entry_usable_edge_size_scale"))
    if usable is None:
        return str(audit.get("entry_usable_edge_na_reason") or "entry_usable_edge_missing")
    if usable <= 0:
        return "entry_usable_edge_nonpositive_after_penalties"
    if size is not None and size <= 0:
        return "entry_usable_edge_size_zero_after_penalties"
    if str(row.get("strategy_recommendation") or "") in {"no_new_edge_d0_locked", "settlement_hold_review"}:
        return str(row.get("strategy_recommendation") or "stage_not_new_entry")
    return "paper_review_candidate"


def _candidate_strategy(row: Mapping[str, Any]) -> str:
    side = str(row.get("side") or "").strip().upper()
    focus_lane = str(row.get("focus_lane") or "").strip()
    mode = str(row.get("weather_strategy_mode") or "").strip()
    recommendation = str(row.get("strategy_recommendation") or "").strip()
    reverse_edge = _truthy(row.get("weather_strategy_reverse_edge")) or (
        side == "NO" and focus_lane == "tail_no_reverse_edge"
    )
    tail_no = _truthy(row.get("weather_strategy_tail_no")) or (
        side == "NO" and "tail_no" in focus_lane
    )
    settlement_eligible = _truthy(row.get("weather_strategy_settlement_eligible"))
    if reverse_edge or (side == "NO" and tail_no):
        return "reverse_edge_buy_no_sell_yes"
    if mode == "settlement_only":
        return "settlement_review"
    if settlement_eligible and focus_lane == "d0_physical_or_settlement":
        return "d0_physical_settlement_hybrid"
    if mode == "settlement_dominant" or "settlement" in recommendation:
        return "settlement_review"
    return "secondary_or_physical_edge"


def _review_action(row: Mapping[str, Any], audit: Mapping[str, Any], *, gap_reason: str) -> str:
    if gap_reason != "paper_review_candidate":
        return "exclude_until_tradeability_gap_fixed"
    usable_edge = _to_float(audit.get("entry_usable_edge"))
    size_scale = _to_float(audit.get("entry_usable_edge_size_scale"))
    if usable_edge is None or usable_edge <= 0:
        return "exclude_until_positive_risk_discounted_edge"
    if size_scale is not None and size_scale <= 0:
        return "paper_observe_only_size_zero"
    strategy = _candidate_strategy(row)
    if strategy == "reverse_edge_buy_no_sell_yes":
        return "paper_track_reverse_edge_no_side"
    if strategy in {"settlement_review", "d0_physical_settlement_hybrid"}:
        return "paper_track_d0_physical_or_settlement"
    return "paper_track_secondary_edge"


def _execution_direction(row: Mapping[str, Any]) -> str:
    side = str(row.get("side") or "").strip().upper()
    if side == "NO":
        return "buy_no_equivalent_to_sell_yes"
    if side == "YES":
        return "buy_yes"
    return ""


def _format_pct(value: Any) -> str:
    num = _to_float(value)
    if num is None:
        return "-"
    return f"{num * 100:.1f}%"


def _format_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Tradeability Gap Paper Review",
        "",
        f"- event_dates: {', '.join(str(x) for x in payload.get('event_dates') or [])}",
        f"- market_scope: {payload.get('market_scope')}",
        f"- min_raw_edge: {_format_pct(payload.get('min_raw_edge'))}",
        f"- gap_rows: {payload.get('gap_rows')}",
        f"- by_gap_reason: {json.dumps(payload.get('by_gap_reason') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- by_review_action: {json.dumps(payload.get('by_review_action') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- by_candidate_strategy: {json.dumps(payload.get('by_candidate_strategy') or {}, ensure_ascii=False, sort_keys=True)}",
        "",
        "| # | action | strategy | direction | slug | outcome | raw LCB edge | risk edge | size | reason | station | source tier |",
        "|---:|---|---|---|---|---|---:|---:|---:|---|---|---|",
    ]
    for idx, row in enumerate(payload.get("rows") or [], start=1):
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {idx} | {action} | {strategy} | {direction} | {slug} | {outcome} | {raw_edge} | {risk_edge} | {size} | {reason} | {station} | {tier} |".format(
                idx=idx,
                action=str(row.get("review_action") or ""),
                strategy=str(row.get("candidate_strategy") or ""),
                direction=str(row.get("execution_direction") or ""),
                slug=str(row.get("slug") or ""),
                outcome=str(row.get("outcome") or ""),
                raw_edge=_format_pct(row.get("raw_lcb_edge")),
                risk_edge=_format_pct(row.get("counterfactual_entry_usable_edge")),
                size=_format_pct(row.get("counterfactual_entry_usable_edge_size_scale")),
                reason=str(row.get("gap_reason") or ""),
                station=str(row.get("station_id") or ""),
                tier=str(row.get("source_fit_tier") or ""),
            )
        )
    return "\n".join(lines)


def build_review(
    db_path: Path,
    event_dates: Sequence[str],
    *,
    min_raw_edge: float = DEFAULT_MIN_RAW_EDGE,
    limit: int = 25,
    market_scope: str = "highest_temperature",
) -> Dict[str, Any]:
    fact_profiles = _load_fact_profiles(db_path, event_dates)
    report = build_strategy_plan(
        db_path,
        event_dates,
        rows_limit=0,
        top=max(50, int(limit or 0)),
        market_scope=market_scope,
    )
    rows: List[Dict[str, Any]] = []
    by_reason: Counter[str] = Counter()
    by_review_action: Counter[str] = Counter()
    by_candidate_strategy: Counter[str] = Counter()
    for plan in report.get("plans") or []:
        for row in plan.get("rows") or []:
            if not isinstance(row, Mapping) or not _is_gap_row(row, min_raw_edge=min_raw_edge):
                continue
            raw_edge = _raw_lcb_edge(row)
            if raw_edge is None:
                continue
            row_with_fact = dict(row)
            fact_profile = fact_profiles.get(
                (str(row.get("slug") or ""), str(row.get("event_date") or "").split("T", 1)[0]),
                {},
            )
            if fact_profile:
                row_with_fact["_fact_profile"] = fact_profile
            context = _review_context(row_with_fact, raw_edge=float(raw_edge))
            audit = swm._build_entry_usable_edge_audit(context)
            reason = _classify_gap(row_with_fact, audit)
            review_action = _review_action(row_with_fact, audit, gap_reason=reason)
            candidate_strategy = _candidate_strategy(row_with_fact)
            by_reason[reason] += 1
            by_review_action[review_action] += 1
            by_candidate_strategy[candidate_strategy] += 1
            rows.append(
                {
                    "slug": row.get("slug"),
                    "event_date": row.get("event_date"),
                    "outcome": row.get("outcome"),
                    "side": row.get("side"),
                    "candidate_strategy": candidate_strategy,
                    "execution_direction": _execution_direction(row_with_fact),
                    "review_action": review_action,
                    "strategy_recommendation": row.get("strategy_recommendation"),
                    "focus_lane": row.get("focus_lane"),
                    "weather_strategy_stage": row.get("weather_strategy_stage"),
                    "weather_strategy_mode": row.get("weather_strategy_mode"),
                    "weather_strategy_reverse_edge": row.get("weather_strategy_reverse_edge"),
                    "weather_strategy_tail_no": row.get("weather_strategy_tail_no"),
                    "weather_strategy_settlement_eligible": row.get(
                        "weather_strategy_settlement_eligible"
                    ),
                    "entry_probability_used": "q_bayes_lcb",
                    "exit_probability_reference": (
                        "q_bayes_posterior_mean_then_hold_edge_realizable_for_open_positions"
                    ),
                    "raw_lcb_edge": float(raw_edge),
                    "q_bayes_lcb": row.get("q_bayes_lcb"),
                    "q_bayes_posterior": row.get("q_bayes_posterior"),
                    "ask": row.get("ask"),
                    "bid": row.get("bid"),
                    "spread": row.get("spread"),
                    "weather_likelihood_feature_count": row.get(
                        "weather_likelihood_feature_count"
                    ),
                    "source_fit_tier": row.get("source_fit_tier"),
                    "station_id": context.get("station_id"),
                    "resolution_source": context.get("resolution_source"),
                    "station_profile_source": "event_fact_latest" if fact_profile else "decision_row",
                    "source_station_id": row.get("source_station_id"),
                    "source_station_match": row.get("source_station_match"),
                    "gap_reason": reason,
                    "counterfactual_entry_usable_edge": audit.get("entry_usable_edge"),
                    "counterfactual_entry_usable_edge_size_scale": audit.get(
                        "entry_usable_edge_size_scale"
                    ),
                    "risk_discount_positive": (
                        (_to_float(audit.get("entry_usable_edge")) or 0.0) > 0
                    ),
                    "counterfactual_penalty_total": audit.get("entry_usable_edge_penalty_total"),
                    "counterfactual_boundary_status": audit.get("entry_edge_boundary_status"),
                    "counterfactual_boundary_action": audit.get("entry_edge_boundary_action"),
                    "counterfactual_low_price_bucket": audit.get(
                        "entry_usable_edge_low_price_bucket"
                    ),
                    "counterfactual_low_price_na_reason": audit.get(
                        "entry_usable_edge_low_price_na_reason"
                    ),
                    "counterfactual_station_na_reason": audit.get(
                        "entry_usable_edge_station_na_reason"
                    ),
                }
            )

    rows.sort(
        key=lambda item: (
            0 if item.get("gap_reason") == "paper_review_candidate" else 1,
            -float(item.get("raw_lcb_edge") or 0.0),
            str(item.get("slug") or ""),
        )
    )
    rows_out = rows[: max(0, int(limit or 0))] if limit else rows
    return {
        "ok": True,
        "db_path": str(db_path),
        "event_dates": list(event_dates),
        "market_scope": market_scope,
        "min_raw_edge": float(min_raw_edge),
        "gap_rows": len(rows),
        "by_gap_reason": dict(sorted(by_reason.items())),
        "by_review_action": dict(sorted(by_review_action.items())),
        "by_candidate_strategy": dict(sorted(by_candidate_strategy.items())),
        "rows": rows_out,
        "next_mainline_task": "review_tradeability_gap_paper_candidates_before_small_capital",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--event-date", action="append", default=[])
    parser.add_argument("--min-raw-edge", type=float, default=DEFAULT_MIN_RAW_EDGE)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    if not args.event_date:
        parser.error("--event-date is required")
    payload = build_review(
        Path(args.db),
        args.event_date,
        min_raw_edge=float(args.min_raw_edge),
        limit=int(args.limit),
        market_scope=str(args.market_scope or "highest_temperature"),
    )
    if args.format == "markdown":
        print(_format_markdown(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
