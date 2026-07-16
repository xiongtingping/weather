#!/usr/bin/env python3
"""Build/enqueue bounded auto-trade batches from the weather strategy plan.

Default mode is read-only: it prints the batch that would be processed. Pass
``--enqueue`` to create an ``auto_trade_jobs`` row whose payload restricts the
worker to the selected event keys.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.weather_strategy_plan import DB_PATH, build_strategy_plan  # noqa: E402
import scan_weather_market as swm  # noqa: E402
from posterior_settlement_runtime import (  # noqa: E402
    ACTION_EXIT_CLEAR,
    ACTION_SWITCH_BUY,
    STRATEGY_FAMILY as POSTERIOR_SETTLEMENT_FAMILY,
    bucket_directional_distance,
    evaluate_realtime_target_adjustment,
    event_target_context,
)

TARGET_REALIZATION_SWITCH_RECOMMENDATION = "target_realization_switch"
TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION = "target_realization_order_intent"
TARGET_REALIZATION_STRATEGY = "target_realization_binary_settlement"
DRY_RUN_ORDER_INTENT_RECOMMENDATION = "dry_run_order_intent"

DEFAULT_RECOMMENDATIONS = (
    TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION,
    DRY_RUN_ORDER_INTENT_RECOMMENDATION,
    "settlement_hold_review",
)
DEFAULT_FOCUS_LANES = (
    "d0_physical_or_settlement",
    "major_settlement",
)
PROTECTED_FOCUS_LANES = {
    "d0_physical_or_settlement",
}
MAJOR_SETTLEMENT_FOCUS_LANE = "major_settlement"
DEFAULT_PROTECTED_FOCUS_MIN_SHARE = 0.70
DEFAULT_MAJOR_SETTLEMENT_MAX_SHARE = 0.30
DEFAULT_MIN_ENTRY_EDGE = 0.10
DEFAULT_RESOURCE_FOCUS_POLICY = "warn"
DEFAULT_REQUIRE_D0_PHYSICAL_EVIDENCE = True
DEFAULT_OBSERVATION_TARGET_PACKAGE_PATH = ROOT / "data/reports/paper_observation_target_package_latest.json"
RESOURCE_FOCUS_POLICIES = {"off", "warn", "enforce"}
RECOMMENDATION_RANK = {name: idx for idx, name in enumerate(DEFAULT_RECOMMENDATIONS)}
RECOMMENDATION_RANK[TARGET_REALIZATION_SWITCH_RECOMMENDATION] = -1
FOCUS_RANK = {name: idx for idx, name in enumerate(DEFAULT_FOCUS_LANES)}
ENTRY_CANDIDATE_RECOMMENDATIONS = {
    TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION,
    DRY_RUN_ORDER_INTENT_RECOMMENDATION,
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


SSOT_EXECUTION_CONTEXT_FIELDS = (
    "target_realization_capital_plan",
    "target_switch_context",
    "trajectory_momentum_impact",
    "temperature_path_distribution",
    "target_bucket_selection",
    "target_realization_capital_plan_version",
    "capital_plan_target_bucket",
    "capital_plan_target_posterior",
    "capital_plan_second_target_bucket",
    "capital_plan_second_target_posterior",
    "capital_plan_summary",
    "sell_old_buy_new_legs",
    "sell_old_legs",
    "buy_new_leg",
    "buy_new_legs",
    "buy_old_reverse_legs",
    "active_position_legs",
    "temperature_path_trade_follow_allowed",
    "temperature_path_trade_follow_reason",
)


def _opposite_trade_side(side: Any) -> str:
    side_key = str(side or "").strip().upper()
    return "NO" if side_key == "YES" else "YES" if side_key == "NO" else ""


def _ssot_execution_context_from_fact(fact: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(fact, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key in (
        "target_realization_capital_plan",
        "target_switch_context",
        "temperature_path_distribution",
        "target_bucket_selection",
    ):
        value = fact.get(key)
        if isinstance(value, Mapping):
            out[key] = dict(value)
    switch_context = out.get("target_switch_context")
    if isinstance(switch_context, Mapping):
        impact = switch_context.get("trajectory_momentum_impact")
        if isinstance(impact, Mapping):
            out["trajectory_momentum_impact"] = dict(impact)
    plan = out.get("target_realization_capital_plan")
    if isinstance(plan, Mapping):
        out["target_realization_capital_plan_version"] = plan.get("version")
        out["capital_plan_target_bucket"] = plan.get("target_bucket")
        out["capital_plan_target_posterior"] = plan.get("target_posterior")
        out["capital_plan_second_target_bucket"] = plan.get("second_target_bucket")
        out["capital_plan_second_target_posterior"] = plan.get("second_target_posterior")
        out["capital_plan_summary"] = plan.get("summary")
        for key in (
            "sell_old_buy_new_legs",
            "sell_old_legs",
            "buy_new_leg",
            "buy_new_legs",
            "buy_old_reverse_legs",
            "active_position_legs",
        ):
            value = plan.get(key)
            if value not in (None, "", [], {}):
                out[key] = value
        if plan.get("temperature_path_trade_follow_allowed") is not None:
            out["temperature_path_trade_follow_allowed"] = plan.get(
                "temperature_path_trade_follow_allowed"
            )
        if plan.get("temperature_path_trade_follow_reason") not in (None, ""):
            out["temperature_path_trade_follow_reason"] = plan.get(
                "temperature_path_trade_follow_reason"
            )
    path = out.get("temperature_path_distribution")
    if isinstance(path, Mapping):
        trade_follow = path.get("trade_follow")
        if isinstance(trade_follow, Mapping):
            if "temperature_path_trade_follow_allowed" not in out and trade_follow.get("allowed") is not None:
                out["temperature_path_trade_follow_allowed"] = trade_follow.get("allowed")
            if "temperature_path_trade_follow_reason" not in out and trade_follow.get("reason") not in (None, ""):
                out["temperature_path_trade_follow_reason"] = trade_follow.get("reason")
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _copy_ssot_execution_context(
    target: Dict[str, Any],
    source: Mapping[str, Any],
) -> None:
    for key in SSOT_EXECUTION_CONTEXT_FIELDS:
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if value in ([], {}):
            continue
        target[key] = value


def _live_mode_enabled() -> bool:
    enabled = str(os.environ.get("PM_AUTO_TRADE_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    dry_run = str(os.environ.get("PM_AUTO_TRADE_DRY_RUN") or "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return bool(enabled and not dry_run)


def _dry_run_order_intent_allowed() -> bool:
    if str(os.environ.get("PM_AUTO_TRADE_ENTRY_ALLOW_DRY_RUN_INTENT_IN_LIVE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    return not _live_mode_enabled()


def _csv_values(values: Sequence[str], defaults: Sequence[str]) -> List[str]:
    out: List[str] = []
    raw_values = values or []
    if not raw_values:
        raw_values = [",".join(defaults)]
    for value in raw_values:
        for part in str(value or "").split(","):
            text = part.strip()
            if text:
                out.append(text)
    seen = set()
    deduped: List[str] = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _city_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def _write_scope_is_chengdu_zuuu() -> bool:
    return swm._archive_write_scope_is_chengdu_zuuu()


def _row_allowed_by_write_scope(row: Mapping[str, Any]) -> bool:
    if not _write_scope_is_chengdu_zuuu():
        return True
    return swm._archive_write_allowed_for_chengdu_zuuu(
        slug=str(row.get("slug") or ""),
        title=str(row.get("title") or ""),
        city=str(row.get("city_key") or row.get("city") or ""),
        station_id=str(row.get("station_id") or row.get("source_station_id") or ""),
        resolution_source=str(row.get("resolution_source") or ""),
    )


def _default_pilot_cities_from_reports(limit: int = 2) -> List[str]:
    try:
        from scripts import small_capital_validation_review

        return [
            str(city)
            for city in small_capital_validation_review._default_pilot_cities(limit)[0]
            if str(city).strip()
        ]
    except Exception:
        return []


def _row_sort_key(row: Mapping[str, Any]) -> Tuple[int, int, int, float, float]:
    recommendation = str(row.get("strategy_recommendation") or "")
    focus_lane = str(row.get("focus_lane") or "")
    tier = str(row.get("weather_strategy_edge_tier") or "")
    tier_rank = {"normal_trade": 0, "probe": 1, "shadow_only": 2, "blocked": 3}.get(tier, 4)
    edge = swm.to_float(row.get("edge_tradeability_profit_value"))
    usable = swm.to_float(row.get("entry_usable_edge"))
    return (
        RECOMMENDATION_RANK.get(recommendation, 999),
        FOCUS_RANK.get(focus_lane, 999),
        tier_rank,
        -(edge if edge is not None else -1.0),
        -(usable if usable is not None else -1.0),
    )


def _load_observation_target_package(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _observation_target_city_keys(package: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in package.get("city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        if int(item.get("needed_resolved_rows") or 0) <= 0:
            continue
        key = _city_key(str(item.get("city_key") or item.get("city") or ""))
        if key:
            out.add(key)
    return out


def _observation_expansion_city_keys(package: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in package.get("observation_expansion_city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        key = _city_key(str(item.get("city_key") or item.get("city") or ""))
        if key:
            out.add(key)
    return out


def _observation_recommended_pilot_city_keys(package: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in package.get("recommended_pilot_city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        key = _city_key(str(item.get("city_key") or item.get("city") or ""))
        if key:
            out.add(key)
    return out


def _recommended_pilot_strategy_items(package: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    out: List[Mapping[str, Any]] = []
    for item in package.get("recommended_pilot_city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        city = _city_key(str(item.get("city_key") or item.get("city") or ""))
        if not city:
            continue
        blockers = {str(value or "") for value in item.get("blockers") or []}
        strategies = sorted(
            code.split(":", 1)[1]
            for code in blockers
            if code.startswith("strategy_resolved_rows_below_min:")
            and code.split(":", 1)[1]
        )
        if not strategies:
            strategies = ["reverse_edge_buy_no_sell_yes"]
        for strategy in strategies:
            out.append(
                {
                    "city": city,
                    "target_strategy": strategy,
                    "needed_resolved_rows": int(item.get("needed_resolved_rows") or 0),
                    "needed_additional_wins_for_hit_rate": int(
                        item.get("needed_additional_wins_for_hit_rate") or 0
                    ),
                    "recommended_observation_action": (
                        "prioritize_recommended_pilot_reverse_no_candidates"
                        if strategy == "reverse_edge_buy_no_sell_yes"
                        else "prioritize_recommended_pilot_paper_observation_candidates"
                    ),
                    "observation_mode": str(
                        item.get("observation_mode") or "paper_only_not_small_capital"
                    ),
                }
            )
    return out


def _observation_target_strategy_items(package: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    out: List[Mapping[str, Any]] = []
    for item in package.get("strategy_targets") or []:
        if not isinstance(item, Mapping):
            continue
        needed_rows = int(item.get("needed_resolved_rows") or 0)
        needed_wins = int(item.get("needed_additional_wins_for_hit_rate") or 0)
        if needed_rows <= 0 and needed_wins <= 0:
            continue
        out.append(item)
    return out


def _row_matches_observation_strategy(row: Mapping[str, Any], target_strategy: str) -> bool:
    strategy = str(target_strategy or "").strip()
    lane = str(row.get("focus_lane") or "")
    rec = str(row.get("strategy_recommendation") or "")
    reverse_action = str(row.get("reverse_edge_execution_action") or "")
    reverse_strategy_action = str(row.get("weather_strategy_reverse_edge_action") or "")
    stage = str(row.get("weather_strategy_stage") or "")
    if strategy == "reverse_edge_buy_no_sell_yes":
        return bool(
            reverse_action == "BUY_NO"
            or reverse_strategy_action == "buy_no_equivalent_to_sell_yes"
            or rec == "priority_tail_no_reverse_edge"
            or lane == "tail_no_reverse_edge"
        )
    if strategy == "d0_physical_settlement_hybrid":
        return bool(lane == "d0_physical_or_settlement" and stage.startswith("d0"))
    if strategy == "secondary_or_physical_edge":
        return lane in {"secondary_edge", "d0_physical_or_settlement"}
    return False


def _apply_observation_target_focus(
    candidates: Sequence[Dict[str, Any]],
    package: Mapping[str, Any],
) -> Dict[str, Any]:
    city_keys = _observation_target_city_keys(package)
    expansion_city_keys = _observation_expansion_city_keys(package)
    recommended_pilot_city_keys = _observation_recommended_pilot_city_keys(package)
    strategy_items = _observation_target_strategy_items(package)
    recommended_strategy_items = _recommended_pilot_strategy_items(package)
    target_rows = 0
    expansion_rows = 0
    recommended_strategy_rows = 0
    by_strategy: Dict[str, int] = {}
    by_recommended_strategy: Dict[str, int] = {}
    by_city: Dict[str, int] = {}
    by_expansion_city: Dict[str, int] = {}
    by_recommended_pilot_city: Dict[str, int] = {}

    for row in candidates:
        city_key = _city_key(str(row.get("city_key") or ""))
        city_match = bool(city_key and city_key in city_keys)
        recommended_pilot_city_match = bool(
            city_key and city_key in recommended_pilot_city_keys
        )
        expansion_city_match = bool(city_key and city_key in expansion_city_keys)
        best_priority = 0
        best_item: Optional[Mapping[str, Any]] = None
        best_recommended_item: Optional[Mapping[str, Any]] = None
        for item in strategy_items:
            target_strategy = str(item.get("target_strategy") or "")
            if not _row_matches_observation_strategy(row, target_strategy):
                continue
            needed_rows = int(item.get("needed_resolved_rows") or 0)
            needed_wins = int(item.get("needed_additional_wins_for_hit_rate") or 0)
            priority = 100 + needed_rows * 10 + needed_wins * 20
            if target_strategy == "reverse_edge_buy_no_sell_yes":
                priority += 15
            if city_match:
                priority += 25
            if priority > best_priority:
                best_priority = priority
                best_item = item
        if recommended_pilot_city_match:
            for item in recommended_strategy_items:
                if _city_key(str(item.get("city") or "")) != city_key:
                    continue
                target_strategy = str(item.get("target_strategy") or "")
                if not _row_matches_observation_strategy(row, target_strategy):
                    continue
                needed_rows = int(item.get("needed_resolved_rows") or 0)
                needed_wins = int(item.get("needed_additional_wins_for_hit_rate") or 0)
                priority = 95 + needed_rows * 8 + needed_wins * 15
                if target_strategy == "reverse_edge_buy_no_sell_yes":
                    priority += 20
                if priority > best_priority:
                    best_priority = priority
                    best_item = item
                    best_recommended_item = item
        if best_item is None and city_match:
            best_priority = 25
        if best_item is None and recommended_pilot_city_match:
            best_priority = max(best_priority, 35)
        if best_item is None and expansion_city_match:
            best_priority = max(best_priority, 12)

        matched = best_priority > 0
        row["observation_target_match"] = matched
        row["observation_target_priority"] = int(best_priority)
        row["observation_target_city_match"] = city_match
        row["observation_recommended_pilot_city_match"] = recommended_pilot_city_match
        row["observation_expansion_city_match"] = expansion_city_match
        if best_item is not None:
            strategy = str(best_item.get("target_strategy") or "")
            row["observation_target_strategy"] = strategy
            row["observation_target_needed_resolved_rows"] = int(
                best_item.get("needed_resolved_rows") or 0
            )
            row["observation_target_needed_additional_wins"] = int(
                best_item.get("needed_additional_wins_for_hit_rate") or 0
            )
            row["observation_target_recommended_action"] = best_item.get(
                "recommended_observation_action"
            )
            by_strategy[strategy or "unknown"] = by_strategy.get(strategy or "unknown", 0) + 1
        if best_recommended_item is not None:
            rec_strategy = str(best_recommended_item.get("target_strategy") or "unknown")
            row["observation_recommended_pilot_target_strategy"] = rec_strategy
            recommended_strategy_rows += 1
            by_recommended_strategy[rec_strategy] = (
                by_recommended_strategy.get(rec_strategy, 0) + 1
            )
        if city_match and city_key:
            by_city[city_key] = by_city.get(city_key, 0) + 1
        if expansion_city_match and city_key:
            by_expansion_city[city_key] = by_expansion_city.get(city_key, 0) + 1
            expansion_rows += 1
        if recommended_pilot_city_match and city_key:
            by_recommended_pilot_city[city_key] = (
                by_recommended_pilot_city.get(city_key, 0) + 1
            )
        if matched:
            target_rows += 1

    return {
        "enabled": True,
        "package_path": str(package.get("package_path") or ""),
        "package_ok": bool(package),
        "target_city_keys": sorted(city_keys),
        "recommended_pilot_city_keys": sorted(recommended_pilot_city_keys),
        "expansion_city_keys": sorted(expansion_city_keys),
        "target_strategy_count": len(strategy_items),
        "recommended_pilot_strategy_count": len(recommended_strategy_items),
        "candidate_target_rows": int(target_rows),
        "candidate_expansion_rows": int(expansion_rows),
        "candidate_recommended_pilot_strategy_rows": int(recommended_strategy_rows),
        "candidate_target_rows_by_strategy": dict(sorted(by_strategy.items())),
        "candidate_recommended_pilot_rows_by_strategy": dict(
            sorted(by_recommended_strategy.items())
        ),
        "candidate_target_rows_by_city": dict(sorted(by_city.items())),
        "candidate_recommended_pilot_rows_by_city": dict(
            sorted(by_recommended_pilot_city.items())
        ),
        "candidate_expansion_rows_by_city": dict(sorted(by_expansion_city.items())),
    }


def _recommended_pilot_city_diagnostics(
    *,
    recommended_city_keys: set[str],
    scoped_plan_rows_by_city: Mapping[str, int],
    candidate_rows_by_city: Mapping[str, int],
    skipped_by_city: Mapping[str, Mapping[str, int]],
    excluded_by_city: Mapping[str, Mapping[str, int]],
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for city in sorted(recommended_city_keys):
        skipped = dict(skipped_by_city.get(city) or {})
        excluded = dict(excluded_by_city.get(city) or {})
        action_summary = _diagnostic_action_summary(skipped, excluded)
        out[city] = {
            "scoped_plan_rows": int(scoped_plan_rows_by_city.get(city) or 0),
            "candidate_rows": int(candidate_rows_by_city.get(city) or 0),
            "skipped_diagnostics": dict(sorted(skipped.items())),
            "excluded_entry_reasons": dict(sorted(excluded.items())),
            "primary_reason": action_summary.get("primary_reason") or "",
            "primary_action": action_summary.get("primary_action") or "",
            "recommended_actions": action_summary.get("recommended_actions") or [],
        }
    return out


def _diagnostic_action(reason: str) -> str:
    code = str(reason or "").strip()
    if code == "settlement_watch_not_entry":
        return "wait_for_settlement_label_or_review_only_no_new_entry"
    if code == "market_settled_or_past_no_new_entry":
        return "wait_for_next_open_event_date_or_use_settlement_review_only"
    if code == "physical_bayes_no_positive_raw_edge":
        return "wait_for_new_metar_or_new_market_until_physical_bayes_edge_turns_positive"
    if code == "physical_bayes_positive_raw_edge_tradeability_missing":
        return "inspect_quote_depth_spread_and_tradeability_inputs"
    if code == "physical_bayes_thin_raw_edge_tradeability_missing":
        return "paper_observe_until_lcb_edge_or_liquidity_thickens"
    if code in {"entry_usable_edge_nonpositive", "entry_usable_edge_size_zero"}:
        return "keep_blocked_until_risk_discounted_edge_turns_positive"
    if code in {"edge_lcb_too_thin_after_fees", "shadow_only_edge_below_probe"}:
        return "paper_observe_only_until_lcb_edge_reaches_entry_floor"
    if code == "variance_high":
        return "skip_until_bayesian_variance_contracts"
    if code == "d0_physical_evidence_missing":
        return "wait_for_metar_radiation_or_physical_likelihood_evidence"
    if code.startswith("settlement_quality_"):
        return "verify_resolution_source_station_before_settlement_entry"
    if code == "not_entry_candidate_recommendation":
        return "do_not_enter_non_entry_recommendations"
    return "review_city_reason_before_relaxing_any_gate"


def _diagnostic_action_summary(
    skipped: Mapping[str, int],
    excluded: Mapping[str, int],
) -> Dict[str, Any]:
    combined: Dict[str, int] = {}
    for source in (skipped, excluded):
        for reason, count in (source or {}).items():
            key = str(reason or "")
            try:
                n = int(count or 0)
            except Exception:
                n = 0
            if not key or n <= 0:
                continue
            combined[key] = combined.get(key, 0) + n
    ranked = sorted(combined.items(), key=lambda item: (-int(item[1]), str(item[0])))
    actions: Dict[str, Dict[str, Any]] = {}
    for reason, count in ranked:
        action = _diagnostic_action(reason)
        bucket = actions.setdefault(action, {"action": action, "count": 0, "reasons": []})
        bucket["count"] = int(bucket.get("count") or 0) + int(count)
        bucket["reasons"].append({"reason": reason, "count": int(count)})
    action_rows = sorted(
        actions.values(),
        key=lambda item: (-int(item.get("count") or 0), str(item.get("action") or "")),
    )
    primary_reason = ranked[0][0] if ranked else ""
    return {
        "primary_reason": primary_reason,
        "primary_action": _diagnostic_action(primary_reason) if primary_reason else "",
        "recommended_actions": action_rows,
    }


def _target_focused_sort_key(row: Mapping[str, Any]) -> Tuple[int, Tuple[int, int, int, float, float]]:
    return (-int(row.get("observation_target_priority") or 0), _row_sort_key(row))


def _row_entry_edge_value(row: Mapping[str, Any]) -> Optional[float]:
    return swm.to_float(row.get("edge_tradeability_profit_value"))


def _slug_matches_pilot_city(slug: str, pilot_city_set: Sequence[str]) -> bool:
    if not pilot_city_set:
        return True
    text = _city_key(slug)
    return any(city and city in text for city in pilot_city_set)


def _first_row_text(rows: Sequence[Mapping[str, Any]], key: str) -> str:
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _first_row_float(rows: Sequence[Mapping[str, Any]], *keys: str) -> Optional[float]:
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in keys:
            value = swm.to_float(row.get(key))
            if value is not None:
                return value
    return None


def _load_latest_fact_payload(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """
            SELECT h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            ORDER BY h.fact_as_of DESC, h.id DESC
            LIMIT 1
            """,
            (slug, event_date),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    try:
        payload = json.loads(str(row["fact_json"] or ""))
    except (TypeError, json.JSONDecodeError, KeyError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _apply_capital_plan_target_to_context(
    context: Dict[str, Any],
    ssot_execution_context: Mapping[str, Any],
) -> None:
    plan = ssot_execution_context.get("target_realization_capital_plan")
    if (
        not isinstance(plan, Mapping)
        or plan.get("ok") is not True
        or str(plan.get("version") or "").strip() != "strong_posterior_three_leg_strategy_v2"
    ):
        return
    target_bucket = str(plan.get("target_bucket") or "").strip()
    if not target_bucket:
        return
    context["target_bucket"] = target_bucket
    context["ssot_target_bucket"] = target_bucket
    posterior = swm.to_float(plan.get("target_posterior"))
    if posterior is not None and math.isfinite(posterior):
        context["target_posterior"] = float(posterior)
        context["new_target_posterior"] = float(posterior)
    second_bucket = str(plan.get("second_target_bucket") or "").strip()
    if second_bucket:
        context["second_target_bucket"] = second_bucket
    second_posterior = swm.to_float(plan.get("second_target_posterior"))
    if second_posterior is not None and math.isfinite(second_posterior):
        context["second_target_posterior"] = float(second_posterior)


def _capital_plan_leg_position_effect(role: str, side: str) -> Tuple[str, str]:
    role_key = str(role or "").strip()
    side_key = str(side or "").strip().upper()
    if role_key == "top_yes_wave" and side_key == "YES":
        return "open_or_add_top_yes_wave", "buy_top_yes_wave"
    if role_key == "second_yes" and side_key == "YES":
        return "open_or_add_second_yes", "buy_second_yes"
    if role_key == "strongly_excluded_no" and side_key == "NO":
        return "open_or_add_strongly_excluded_no", "buy_strongly_excluded_no"
    return "open_or_add_new_bucket_side", "buy_new_bucket_side"


def _target_realization_env_float(name: str) -> Optional[float]:
    value = swm.to_float(os.environ.get(name))
    if value is None or not math.isfinite(float(value)):
        return None
    return float(value)


def _target_realization_side_notional_cap_usdc(
    side: str,
    leg: Optional[Mapping[str, Any]] = None,
    plan: Optional[Mapping[str, Any]] = None,
) -> Optional[float]:
    leg_ref = leg if isinstance(leg, Mapping) else {}
    plan_ref = plan if isinstance(plan, Mapping) else {}
    if str(plan_ref.get("version") or "").strip() != "strong_posterior_three_leg_strategy_v2":
        return None
    cap = swm.to_float(leg_ref.get("target_notional_cap_usdc"))
    if cap is None:
        cap = swm.to_float(leg_ref.get("max_notional_usdc"))
    return float(cap) if cap is not None and math.isfinite(float(cap)) and float(cap) > 0 else None


def _ssot_capital_plan_leg_candidate_rows(
    *,
    slug: str,
    event_date: str,
    fact: Mapping[str, Any],
    ssot_execution_context: Mapping[str, Any],
    fact_as_of: Any = None,
) -> List[Dict[str, Any]]:
    plan = ssot_execution_context.get("target_realization_capital_plan")
    if (
        not isinstance(plan, Mapping)
        or plan.get("ok") is not True
        or str(plan.get("version") or "").strip() != "strong_posterior_three_leg_strategy_v2"
    ):
        return []
    raw_legs = plan.get("legs")
    if not isinstance(raw_legs, list):
        return []
    target_bucket = str(plan.get("target_bucket") or fact.get("target_bucket") or "").strip()
    target_posterior = swm.to_float(plan.get("target_posterior"))
    second_bucket = ""
    second_posterior = None
    rows: List[Dict[str, Any]] = []
    for leg in raw_legs:
        if not isinstance(leg, Mapping):
            continue
        action = str(leg.get("action") or "").strip().lower()
        if action != "add":
            continue
        outcome = str(leg.get("bucket") or leg.get("outcome") or "").strip()
        side = str(leg.get("side") or "").strip().upper()
        if not outcome or side not in {"YES", "NO"}:
            continue
        role = str(
            leg.get("role")
            or leg.get("target_realization_leg_role")
            or "capital_plan_leg"
        ).strip()
        allowed_side = {"top_yes_wave": "YES", "second_yes": "YES", "strongly_excluded_no": "NO"}
        if allowed_side.get(role) != side:
            continue
        plan_notional = swm.to_float(leg.get("target_notional"))
        if plan_notional is None:
            plan_notional = swm.to_float(leg.get("delta_notional"))
        if plan_notional is None or not math.isfinite(plan_notional) or plan_notional <= 0:
            continue
        notional_cap = _target_realization_side_notional_cap_usdc(side, leg, plan)
        notional_cap_applied = False
        if (
            notional_cap is not None
            and math.isfinite(float(notional_cap))
            and float(plan_notional) > float(notional_cap) + 1e-9
        ):
            plan_notional = float(notional_cap)
            notional_cap_applied = True
        price = swm.to_float(leg.get("price") or leg.get("ask") or leg.get("order_price"))
        if price is not None and (not math.isfinite(price) or price <= 0):
            price = None
        size = swm.to_float(leg.get("target_size") or leg.get("delta_size"))
        if (size is None or notional_cap_applied) and price is not None and price > 0:
            size = float(plan_notional) / float(price)
        posterior = swm.to_float(leg.get("posterior"))
        prob_for_size = swm.to_float(leg.get("prob_for_size"))
        q_entry = prob_for_size if prob_for_size is not None else posterior
        if side == "NO" and q_entry is not None:
            q_entry = 1.0 - float(q_entry)
        edge_value = None
        if q_entry is not None and price is not None:
            edge_value = float(q_entry) - float(price)
        position_effect, semantics = _capital_plan_leg_position_effect(role, side)
        row_out: Dict[str, Any] = {
            "slug": slug,
            "event_date": event_date,
            "title": str(fact.get("title") or ""),
            "city_key": _city_key(slug),
            "outcome": outcome,
            "side": side,
            "strategy": TARGET_REALIZATION_STRATEGY,
            "strategy_family": POSTERIOR_SETTLEMENT_FAMILY,
            "entry_strategy_family": POSTERIOR_SETTLEMENT_FAMILY,
            "weather_strategy_family": POSTERIOR_SETTLEMENT_FAMILY,
            "strategy_recommendation": TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION,
            "focus_lane": "d0_physical_or_settlement",
            "weather_strategy_stage": "d0_target_realization",
            "weather_strategy_mode": "target_realization",
            "weather_strategy_entry_allowed": True,
            "weather_strategy_entry_block_reason": "",
            "weather_strategy_edge_tier": "normal_trade",
            "target_realization_batch_reason": "target_realization_capital_plan_leg",
            "target_realization_plan_action": "add",
            "target_realization_leg_role": role,
            "target_realization_execution_action": "BUY",
            "target_realization_position_effect": position_effect,
            "sell_old_buy_new_semantics": semantics,
            "target_realization_plan_notional": float(plan_notional),
            "target_realization_plan_notional_source": "ui_snapshot_ssot",
            "target_realization_plan_notional_cap_usdc": notional_cap,
            "target_notional_cap_usdc": notional_cap,
            "target_realization_plan_notional_cap_applied": notional_cap_applied,
            "kelly_target_notional": float(plan_notional),
            "kelly_sizing_source": "ui_snapshot_ssot_capital_plan",
            "order_size_source": "ui_snapshot_ssot_capital_plan",
            "posterior_settlement_entry_allowed": True,
            "posterior_settlement_entry_block_reason": "",
            "posterior_settlement_size_scale": 1.0,
            "posterior_settlement_sort_score": q_entry,
            "auto_trade_ssot_diagnostic": False,
            "entry_ok": True,
            "entry_reasons": ["target_realization_capital_plan_add"],
            "target_bucket": target_bucket or None,
            "ssot_target_bucket": target_bucket or None,
            "posterior_settlement_target_bucket": target_bucket or None,
            "target_posterior": target_posterior,
            "posterior_settlement_target_posterior": target_posterior,
            "new_target_bucket": target_bucket or None,
            "new_target_posterior": target_posterior,
            "current_top_bucket": target_bucket or None,
            "posterior_settlement_current_top_bucket": target_bucket or None,
            "current_top_posterior": target_posterior,
            "posterior_settlement_current_top_posterior": target_posterior,
            "second_target_bucket": second_bucket or None,
            "second_target_posterior": second_posterior,
            "leg_bucket": outcome,
            "posterior_settlement_candidate_bucket": outcome,
            "posterior_settlement_candidate_bucket_prob": posterior,
            "q_bayes_physical_likelihood_enabled": True,
            "weather_likelihood_feature_count": 1,
            "edge_tradeability_profit_value": edge_value,
            "edge_tradeability_pass": True,
            "edge_tradeability_blocked": False,
            "edge_tradeability_target_realization_override": True,
            "edge_tradeability_override_source": "target_realization_ssot_capital_plan",
            "entry_usable_edge": edge_value,
            "entry_usable_edge_size_scale": 1.0,
            "entry_usable_edge_na_reason": "",
            "ask": price,
            "price": price,
            "order_price": price,
            "q_entry": q_entry,
            "q_quote": q_entry,
            "q_calibrated": q_entry,
            "q_raw_static": q_entry,
            "size": size,
            "order_size": size,
            "target_realization_reliability": leg.get("target_realization_reliability"),
            "target_realization_reliability_source": leg.get(
                "target_realization_reliability_source"
            ),
            "target_realization_reliability_mode": leg.get(
                "target_realization_reliability_mode"
            ),
            "target_realization_threshold_c": leg.get("target_realization_threshold_c"),
            "target_realization_path_probability": leg.get(
                "target_realization_path_probability"
            ),
            "target_realization_path_probability_source": leg.get(
                "target_realization_path_probability_source"
            ),
            "strong_physical_exclusion_kind": leg.get("physical_exclusion_kind"),
            "strong_physical_exclusion_evidence": leg.get("physical_exclusion_evidence"),
            "resolution_source": fact.get("resolution_source"),
            "fact_as_of": fact_as_of,
        }
        _copy_ssot_execution_context(row_out, ssot_execution_context)
        if not _row_allowed_by_write_scope(row_out):
            continue
        rows.append(row_out)
    rows.sort(
        key=lambda item: (
            {"top_yes_wave": 0, "second_yes": 1, "strongly_excluded_no": 2}.get(str(item.get("target_realization_leg_role") or ""), 9),
            -float(swm.to_float(item.get("target_realization_plan_notional")) or 0.0),
            str(item.get("outcome") or ""),
        )
    )
    return rows


def _target_adjustment_candidate_rows(
    db_path: Path,
    event_dates: Sequence[str],
    *,
    pilot_city_set: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    date_keys = [str(item or "").split("T", 1)[0] for item in event_dates if str(item or "").strip()]
    date_keys = list(dict.fromkeys(date_keys))
    if not date_keys:
        return []
    placeholders = ",".join("?" for _ in date_keys)
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        return []
    try:
        try:
            rows = conn.execute(
                f"""
                SELECT slug, event_date, outcome, UPPER(COALESCE(side, '')) AS side, position_size
                FROM edge_trades
                WHERE event_date IN ({placeholders})
                  AND UPPER(COALESCE(side, '')) IN ('YES', 'NO')
                  AND (
                    COALESCE(state, '') IN ('entry', 'entry_pending', 'exit_pending')
                    OR COALESCE(position_size, 0) > 0.000001
                  )
                ORDER BY event_date, slug, COALESCE(position_size, 0) DESC, outcome
                """,
                date_keys,
            ).fetchall()
        except sqlite3.Error:
            rows = []
        active_by_event: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for row in rows:
            slug = str(row["slug"] or "").strip()
            event_date = str(row["event_date"] or "").strip().split("T", 1)[0]
            if not slug or not event_date:
                continue
            if not _slug_matches_pilot_city(slug, pilot_city_set):
                continue
            active_by_event.setdefault((slug, event_date), []).append(row)

        candidates: List[Dict[str, Any]] = []
        for (slug, event_date), active_rows in active_by_event.items():
            has_active_yes = any(
                str(active["side"] or "").strip().upper() == "YES"
                for active in active_rows
            )
            payload_rows = swm._load_latest_payload_rows(conn, slug, event_date)
            if not payload_rows:
                continue
            context = event_target_context(payload_rows)
            fact_payload = _load_latest_fact_payload(conn, slug, event_date)
            ssot_execution_context = _ssot_execution_context_from_fact(fact_payload)
            _apply_capital_plan_target_to_context(context, ssot_execution_context)
            if not context.get("target_bucket"):
                continue
            context.update(
                {
                    "slug": slug,
                    "event_date": event_date,
                    "weather_likelihood_refreshed_at": _first_row_text(
                        payload_rows,
                        "weather_likelihood_refreshed_at",
                    ),
                    "observed_today_max_c": _first_row_float(
                        payload_rows,
                        "observed_today_max_c",
                        "observed_max_c",
                        "zuuu_metar_raw_awc_today_max_c",
                        "wu_twc_observed_intraday_today_max_c",
                    ),
                    "observed_path_temp_c": _first_row_float(
                        payload_rows,
                        "observed_path_temp_c",
                        "observed_temp_c",
                        "current_temp_c",
                        "metar_temperature_c",
                    ),
                    "temperature_momentum_effective_c_per_hour": _first_row_float(
                        payload_rows,
                        "temperature_momentum_effective_c_per_hour",
                        "temperature_momentum_1h_c_per_hour",
                        "warming_rate_c_per_hour",
                    ),
                    "snapshot_id": _first_row_text(payload_rows, "created_at")
                    or _first_row_text(payload_rows, "event_run_id")
                    or f"{slug}:{event_date}",
                    "has_active_position": True,
                    "has_active_yes_position": has_active_yes,
                    "posterior_settlement_has_active_position": True,
                    "posterior_settlement_has_active_yes_position": has_active_yes,
                }
            )
            for active in active_rows:
                held_bucket = str(active["outcome"] or "").strip()
                held_side = str(active["side"] or "").strip().upper()
                if not held_bucket:
                    continue
                if held_side not in {"YES", "NO"}:
                    continue
                decision = evaluate_realtime_target_adjustment(
                    {
                        "previous_top_bucket": held_bucket,
                        "held_target_bucket": held_bucket,
                        "held_target_side": held_side,
                        "target_unrealizable_consecutive_snapshots": 2,
                    },
                    context,
                    held_target_bucket=held_bucket,
                )
                action = str(decision.get("target_adjustment_action") or decision.get("action") or "").strip()
                if action not in {ACTION_SWITCH_BUY, ACTION_EXIT_CLEAR}:
                    continue
                ssot_target_bucket = str(
                    context.get("ssot_target_bucket")
                    or context.get("target_bucket")
                    or decision.get("new_target_bucket")
                    or decision.get("target_bucket")
                    or ""
                ).strip()
                new_target_bucket = str(
                    decision.get("new_target_bucket")
                    or ssot_target_bucket
                    or ""
                ).strip()
                ssot_target_posterior = (
                    decision.get("new_target_posterior")
                    if decision.get("new_target_posterior") is not None
                    else context.get("target_posterior")
                )
                held_target_posterior = (
                    decision.get("held_target_posterior")
                    if decision.get("held_target_posterior") is not None
                    else decision.get("target_posterior")
                )
                current_top_bucket = str(
                    decision.get("current_top_bucket")
                    or context.get("current_top_bucket")
                    or context.get("target_bucket")
                    or ssot_target_bucket
                    or new_target_bucket
                    or ""
                ).strip()
                current_top_posterior = (
                    decision.get("current_top_posterior")
                    if decision.get("current_top_posterior") is not None
                    else ssot_target_posterior
                )
                switch_reference_bucket = str(
                    decision.get("switch_reference_bucket")
                    or held_bucket
                    or ""
                ).strip()
                switch_reference_posterior = (
                    decision.get("switch_reference_posterior")
                    if decision.get("switch_reference_posterior") is not None
                    else held_target_posterior
                )
                target_entry_side = str(
                    decision.get("new_target_side")
                    or decision.get("target_entry_side")
                    or context.get("target_entry_side")
                    or context.get("target_side")
                    or "YES"
                ).strip().upper()
                if target_entry_side not in {"YES", "NO"}:
                    target_entry_side = "YES"
                candidates.append(
                    {
                        "slug": slug,
                        "event_date": event_date,
                        "outcome": new_target_bucket or ssot_target_bucket or held_bucket,
                        "side": target_entry_side,
                        "strategy_recommendation": TARGET_REALIZATION_SWITCH_RECOMMENDATION,
                        "focus_lane": "d0_physical_or_settlement",
                        "weather_strategy_stage": "d0_target_realization",
                        "weather_strategy_mode": "target_realization",
                        "weather_strategy_entry_allowed": action == ACTION_SWITCH_BUY,
                        "weather_strategy_entry_block_reason": "",
                        "target_adjustment_action": action,
                        "switch_buy_allowed": bool(decision.get("switch_buy_allowed")),
                        "new_buy_allowed_this_snapshot": decision.get("new_buy_allowed_this_snapshot"),
                        "target_bucket": ssot_target_bucket or None,
                        "ssot_target_bucket": ssot_target_bucket or None,
                        "posterior_settlement_target_bucket": ssot_target_bucket or None,
                        "old_target_bucket": held_bucket,
                        "leg_bucket": held_bucket,
                        "target_entry_side": target_entry_side,
                        "new_target_side": target_entry_side,
                        "target_posterior": ssot_target_posterior,
                        "posterior_settlement_target_posterior": ssot_target_posterior,
                        "new_target_bucket": new_target_bucket or None,
                        "new_target_posterior": ssot_target_posterior,
                        "current_top_bucket": current_top_bucket or None,
                        "posterior_settlement_current_top_bucket": current_top_bucket or None,
                        "current_top_posterior": current_top_posterior,
                        "posterior_settlement_current_top_posterior": current_top_posterior,
                        "switch_reference_bucket": switch_reference_bucket or None,
                        "switch_reference_posterior": switch_reference_posterior,
                        "held_target_bucket": held_bucket,
                        "held_target_side": held_side,
                        "current_position_side": held_side,
                        "current_position_target_bucket": held_bucket,
                        "old_bucket_reverse_side": _opposite_trade_side(held_side),
                        "held_target_posterior": held_target_posterior,
                        "current_target_posterior": held_target_posterior,
                        "has_active_yes_position": has_active_yes,
                        "has_active_position": True,
                        "posterior_settlement_has_active_position": True,
                        "q_bayes_physical_likelihood_enabled": True,
                        "weather_likelihood_feature_count": swm.to_float(
                            _first_row_text(payload_rows, "weather_likelihood_feature_count")
                        ),
                        "target_realization_batch_reason": "active_position_target_adjustment",
                        "target_adjustment_reason_codes": decision.get("target_adjustment_reason_codes")
                        or decision.get("reason_codes")
                        or [],
                    }
                )
                _copy_ssot_execution_context(candidates[-1], ssot_execution_context)
                break
        return candidates
    finally:
        conn.close()


def _ssot_blocked_main_candidate_rows(
    db_path: Path,
    event_dates: Sequence[str],
    *,
    pilot_city_set: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    date_keys = [str(item or "").split("T", 1)[0] for item in event_dates if str(item or "").strip()]
    date_keys = list(dict.fromkeys(date_keys))
    if not date_keys:
        return []
    placeholders = ",".join("?" for _ in date_keys)
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        return []
    try:
        try:
            rows = conn.execute(
                f"""
                SELECT l.slug, l.event_date, h.fact_as_of, h.fact_json
                FROM event_fact_latest l
                JOIN event_fact_history h ON h.id = l.fact_id
                WHERE substr(l.event_date, 1, 10) IN ({placeholders})
                ORDER BY l.event_date, l.slug
                """,
                date_keys,
            ).fetchall()
        except sqlite3.Error:
            return []
        candidates: List[Dict[str, Any]] = []
        for row in rows:
            slug = str(row["slug"] or "").strip()
            event_date = str(row["event_date"] or "").strip().split("T", 1)[0]
            if not slug or not event_date:
                continue
            if not _slug_matches_pilot_city(slug, pilot_city_set):
                continue
            try:
                fact = json.loads(str(row["fact_json"] or ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(fact, dict):
                continue
            ssot = fact.get("auto_trade_ssot")
            if not isinstance(ssot, dict):
                ssot = {}
            ssot_execution_context = _ssot_execution_context_from_fact(fact)
            for leg_row in _ssot_capital_plan_leg_candidate_rows(
                slug=slug,
                event_date=event_date,
                fact=fact,
                ssot_execution_context=ssot_execution_context,
                fact_as_of=row["fact_as_of"],
            ):
                candidates.append(leg_row)
            cand = ssot.get("main_candidate")
            if not isinstance(cand, dict):
                continue
            entry_ok = bool(cand.get("entry_ok"))
            outcome = str(cand.get("outcome") or "").strip()
            side = str(cand.get("side") or "").strip().upper()
            if not outcome or side not in {"YES", "NO"}:
                continue
            entry_reasons = cand.get("entry_reasons")
            if not isinstance(entry_reasons, list):
                entry_reasons = []
            block_reason = str(
                cand.get("weather_strategy_entry_block_reason")
                or cand.get("posterior_settlement_entry_block_reason")
                or cand.get("entry_block_reason")
                or ""
            ).strip()
            if not entry_reasons and block_reason:
                entry_reasons = [block_reason]
            size_scale = swm.to_float(
                cand.get("entry_usable_edge_size_scale_adjusted")
                if cand.get("entry_usable_edge_size_scale_adjusted") is not None
                else cand.get("entry_usable_edge_size_scale")
            )
            if size_scale is None or not math.isfinite(size_scale):
                size_scale = swm.to_float(cand.get("posterior_settlement_size_scale"))
            if size_scale is None or not math.isfinite(size_scale):
                size_scale = 1.0 if entry_ok else 0.0
            target_bucket = str(
                cand.get("ssot_target_bucket")
                or cand.get("posterior_settlement_target_bucket")
                or ssot.get("ssot_target_bucket")
                or cand.get("target_bucket")
                or ssot.get("target_bucket")
                or fact.get("target_bucket")
                or ""
            ).strip()
            row_out: Dict[str, Any] = {
                "slug": slug,
                "event_date": event_date,
                "title": str(fact.get("title") or ""),
                "city_key": _city_key(slug),
                "outcome": outcome,
                "side": side,
                "strategy": "main",
                "strategy_recommendation": TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION,
                "focus_lane": "d0_physical_or_settlement",
                "weather_strategy_stage": "d0_target_realization",
                "weather_strategy_mode": str(
                    cand.get("weather_strategy_mode") or "ssot_main_candidate_diagnostic"
                ),
                "weather_strategy_entry_allowed": bool(entry_ok),
                "weather_strategy_entry_block_reason": block_reason,
                "weather_strategy_edge_tier": "normal_trade" if entry_ok else "blocked",
                "weather_strategy_reverse_edge": cand.get("weather_strategy_reverse_edge"),
                "weather_strategy_reverse_edge_action": cand.get("weather_strategy_reverse_edge_action"),
                "target_realization_batch_reason": (
                    "auto_trade_ssot_main_candidate"
                    if entry_ok
                    else "auto_trade_ssot_blocked_main_candidate"
                ),
                "auto_trade_ssot_diagnostic": True,
                "entry_ok": bool(entry_ok),
                "entry_reasons": [str(item) for item in entry_reasons if str(item or "").strip()],
                "target_bucket": target_bucket or None,
                "ssot_target_bucket": target_bucket or None,
                "posterior_settlement_target_bucket": target_bucket or None,
                "target_posterior": cand.get("target_posterior") or ssot.get("target_posterior"),
                "posterior_settlement_target_posterior": cand.get("target_posterior") or ssot.get("target_posterior"),
                "second_target_bucket": cand.get("second_target_bucket") or ssot.get("second_target_bucket"),
                "second_target_posterior": cand.get("second_target_posterior") or ssot.get("second_target_posterior"),
                "q_bayes_physical_likelihood_enabled": True,
                "weather_likelihood_feature_count": cand.get("weather_likelihood_feature_count") or 1,
                "edge_tradeability_profit_value": cand.get("edge_tradeability_profit_value")
                or cand.get("edge_realizable")
                or cand.get("exec_edge_entry"),
                "edge_tradeability_pass": cand.get("edge_tradeability_pass"),
                "edge_tradeability_reasons": cand.get("edge_tradeability_reasons"),
                "entry_usable_edge": cand.get("entry_usable_edge"),
                "entry_usable_edge_size_scale": float(size_scale),
                "entry_usable_edge_na_reason": "" if entry_ok else block_reason,
                "ask": cand.get("ask"),
                "q_entry": cand.get("q_entry"),
                "q_quote": cand.get("q_quote"),
                "q_calibrated": cand.get("q_calibrated"),
                "q_raw_static": cand.get("q_raw_static") or cand.get("q_raw"),
                "q_bayes_lcb": cand.get("q_bayes_lcb"),
                "spread": cand.get("spread"),
                "station_id": cand.get("station_id"),
                "source_station_id": cand.get("source_station_id"),
                "source_station_match": cand.get("source_station_match"),
                "source_fit_tier": cand.get("source_fit_tier"),
                "source_fit_na_reason": cand.get("source_fit_na_reason"),
                "resolution_source": cand.get("resolution_source"),
                "token_id": cand.get("token_id"),
                "fact_as_of": row["fact_as_of"],
            }
            _copy_ssot_execution_context(row_out, ssot_execution_context)
            if not _row_allowed_by_write_scope(row_out):
                continue
            candidates.append(row_out)
        return candidates
    finally:
        conn.close()


def _row_settlement_quality_block_reason(row: Mapping[str, Any]) -> str:
    lane = str(row.get("focus_lane") or "")
    recommendation = str(row.get("strategy_recommendation") or "")
    if lane != MAJOR_SETTLEMENT_FOCUS_LANE and recommendation != "settlement_hold_review":
        return ""
    if not bool(row.get("weather_strategy_settlement_eligible")):
        return "settlement_quality_not_eligible"
    if str(row.get("source_fit_tier") or "") != "settlement_source":
        return "settlement_quality_source_not_verified"
    if not str(row.get("station_id") or "").strip():
        return "settlement_quality_station_missing"
    if row.get("source_station_match") is not True:
        return "settlement_quality_station_not_matched"
    return ""


def _row_has_executable_entry_edge(
    row: Mapping[str, Any],
    *,
    min_entry_edge: float = DEFAULT_MIN_ENTRY_EDGE,
    require_d0_physical_evidence: bool = DEFAULT_REQUIRE_D0_PHYSICAL_EVIDENCE,
) -> bool:
    recommendation = str(row.get("strategy_recommendation") or "")
    if recommendation not in ENTRY_CANDIDATE_RECOMMENDATIONS:
        return True
    if _row_settlement_quality_block_reason(row):
        return False
    usable = swm.to_float(row.get("entry_usable_edge"))
    if usable is not None and usable <= 0:
        return False
    size_scale = swm.to_float(
        row.get("entry_usable_edge_size_scale_adjusted")
        if row.get("entry_usable_edge_size_scale_adjusted") is not None
        else row.get("entry_usable_edge_size_scale")
    )
    if size_scale is not None and size_scale <= 0:
        return False
    na_reason = str(row.get("entry_usable_edge_na_reason") or "").strip()
    if na_reason in {"entry_usable_edge_nonpositive", "entry_usable_edge_size_zero"}:
        return False
    if _row_d0_physical_evidence_block_reason(
        row,
        require_d0_physical_evidence=require_d0_physical_evidence,
    ):
        return False
    if recommendation == DRY_RUN_ORDER_INTENT_RECOMMENDATION:
        if not _dry_run_order_intent_allowed():
            return False
        return True
    if recommendation == TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION:
        return True
    entry_edge = _row_entry_edge_value(row)
    if entry_edge is None or entry_edge < float(min_entry_edge):
        return False
    return True


def _row_exclusion_reason(
    row: Mapping[str, Any],
    *,
    min_entry_edge: float = DEFAULT_MIN_ENTRY_EDGE,
    require_d0_physical_evidence: bool = DEFAULT_REQUIRE_D0_PHYSICAL_EVIDENCE,
) -> str:
    recommendation = str(row.get("strategy_recommendation") or "")
    if recommendation not in ENTRY_CANDIDATE_RECOMMENDATIONS:
        return ""
    if str(row.get("weather_strategy_stage") or "") == "settled_or_past":
        return "market_settled_or_past_no_new_entry"
    settlement_quality_reason = _row_settlement_quality_block_reason(row)
    if settlement_quality_reason:
        return settlement_quality_reason
    usable = swm.to_float(row.get("entry_usable_edge"))
    if usable is not None and usable <= 0:
        return "entry_usable_edge_nonpositive"
    size_scale = swm.to_float(
        row.get("entry_usable_edge_size_scale_adjusted")
        if row.get("entry_usable_edge_size_scale_adjusted") is not None
        else row.get("entry_usable_edge_size_scale")
    )
    if size_scale is not None and size_scale <= 0:
        return "entry_usable_edge_size_zero"
    na_reason = str(row.get("entry_usable_edge_na_reason") or "").strip()
    if na_reason in {"entry_usable_edge_nonpositive", "entry_usable_edge_size_zero"}:
        return na_reason
    d0_physical_reason = _row_d0_physical_evidence_block_reason(
        row,
        require_d0_physical_evidence=require_d0_physical_evidence,
    )
    if d0_physical_reason:
        return d0_physical_reason
    if recommendation == DRY_RUN_ORDER_INTENT_RECOMMENDATION:
        if not _dry_run_order_intent_allowed():
            return "dry_run_order_intent_disabled_in_live"
        return ""
    if recommendation == TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION:
        return ""
    entry_edge = _row_entry_edge_value(row)
    if entry_edge is None:
        return "entry_edge_missing"
    if entry_edge < float(min_entry_edge):
        return "entry_edge_below_strict_min"
    return ""


def _row_has_physical_likelihood(row: Mapping[str, Any]) -> bool:
    enabled = bool(row.get("q_bayes_physical_likelihood_enabled"))
    evidence = swm.to_float(row.get("q_bayes_physical_likelihood_evidence"))
    prob = swm.to_float(row.get("q_bayes_physical_likelihood_prob"))
    feature_count = swm.to_float(row.get("weather_likelihood_feature_count"))
    source = str(row.get("q_bayes_physical_likelihood_source") or "").strip()
    aux_source = str(row.get("weather_likelihood_aux_source") or "").strip()
    return bool(
        enabled
        or (evidence is not None and evidence > 0)
        or prob is not None
        or (feature_count is not None and feature_count > 0)
        or source
        or aux_source
    )


def _row_d0_physical_evidence_block_reason(
    row: Mapping[str, Any],
    *,
    require_d0_physical_evidence: bool = DEFAULT_REQUIRE_D0_PHYSICAL_EVIDENCE,
) -> str:
    if not require_d0_physical_evidence:
        return ""
    if str(row.get("focus_lane") or "") != "d0_physical_or_settlement":
        return ""
    if not str(row.get("weather_strategy_stage") or "").startswith("d0"):
        return ""
    if _row_has_physical_likelihood(row):
        return ""
    return "d0_physical_evidence_missing"


def _row_skipped_diagnostic(row: Mapping[str, Any], *, min_entry_edge: float = DEFAULT_MIN_ENTRY_EDGE) -> str:
    rec = str(row.get("strategy_recommendation") or "").strip()
    stage = str(row.get("weather_strategy_stage") or "").strip()
    block_reason = str(row.get("weather_strategy_entry_block_reason") or "").strip()
    tier_reason = str(row.get("weather_strategy_edge_tier_reason") or "").strip()
    na_reason = str(row.get("entry_usable_edge_na_reason") or "").strip()
    profit = swm.to_float(row.get("edge_tradeability_profit_value"))
    usable = swm.to_float(row.get("entry_usable_edge"))
    size_scale = swm.to_float(
        row.get("entry_usable_edge_size_scale_adjusted")
        if row.get("entry_usable_edge_size_scale_adjusted") is not None
        else row.get("entry_usable_edge_size_scale")
    )
    if stage == "settled_or_past":
        return "market_settled_or_past_no_new_entry"
    if rec == "settlement_dominant_watch":
        return "settlement_watch_not_entry"
    if rec == "no_new_edge_d0_locked":
        return "d0_locked_no_new_edge"
    if rec == "shadow_only":
        return tier_reason or "shadow_only_edge_below_probe"
    if block_reason == "variance_high":
        return "variance_high"
    if usable is not None and usable <= 0:
        return "entry_usable_edge_nonpositive"
    if size_scale is not None and size_scale <= 0:
        return "entry_usable_edge_size_zero"
    if na_reason:
        return na_reason
    if profit is None:
        if _row_has_physical_likelihood(row):
            lcb = swm.to_float(row.get("q_bayes_lcb"))
            ask = swm.to_float(row.get("ask"))
            if lcb is None or ask is None:
                return "physical_bayes_present_price_or_lcb_missing"
            raw_edge = float(lcb) - float(ask)
            if raw_edge >= float(min_entry_edge):
                return "physical_bayes_positive_raw_edge_tradeability_missing"
            if raw_edge > 0:
                return "physical_bayes_thin_raw_edge_tradeability_missing"
            return "physical_bayes_no_positive_raw_edge"
        if swm.to_float(row.get("q_entry")) is not None or swm.to_float(row.get("q_bayes_lcb")) is not None:
            return "tradeability_edge_missing"
        return "bayes_or_tradeability_missing"
    if profit < float(min_entry_edge):
        return "edge_lcb_too_thin_after_fees"
    if tier_reason:
        return tier_reason
    if block_reason:
        return block_reason
    if rec == "blocked_or_missing_edge":
        return "blocked_or_missing_edge"
    if rec == "blocked_by_weather_strategy":
        return "blocked_by_weather_strategy"
    return rec or "skipped_non_entry"


def _row_raw_lcb_edge(row: Mapping[str, Any]) -> Optional[float]:
    lcb = swm.to_float(row.get("q_bayes_lcb"))
    ask = swm.to_float(row.get("ask"))
    if lcb is None or ask is None:
        return None
    return float(lcb) - float(ask)


def _skipped_review_example(
    row: Mapping[str, Any],
    *,
    recommendation: str,
    diagnostic: str,
    raw_lcb_edge: Optional[float],
) -> Dict[str, Any]:
    return {
        "slug": row.get("slug"),
        "event_date": row.get("event_date"),
        "outcome": row.get("outcome"),
        "side": row.get("side"),
        "strategy_recommendation": recommendation,
        "skipped_diagnostic": diagnostic,
        "focus_lane": row.get("focus_lane"),
        "weather_strategy_stage": row.get("weather_strategy_stage"),
        "weather_strategy_mode": row.get("weather_strategy_mode"),
        "weather_strategy_edge_tier": row.get("weather_strategy_edge_tier"),
        "weather_strategy_entry_block_reason": row.get(
            "weather_strategy_entry_block_reason"
        ),
        "weather_strategy_edge_tier_reason": row.get("weather_strategy_edge_tier_reason"),
        "skipped_diagnostic_raw_lcb_edge": raw_lcb_edge,
        "edge_tradeability_profit_value": row.get("edge_tradeability_profit_value"),
        "entry_usable_edge": row.get("entry_usable_edge"),
        "entry_usable_edge_na_reason": row.get("entry_usable_edge_na_reason"),
        "q_bayes_lcb": row.get("q_bayes_lcb"),
        "q_bayes_posterior": row.get("q_bayes_posterior"),
        "ask": row.get("ask"),
        "bid": row.get("bid"),
        "spread": row.get("spread"),
        "q_bayes_physical_likelihood_enabled": row.get(
            "q_bayes_physical_likelihood_enabled"
        ),
        "weather_likelihood_feature_count": row.get("weather_likelihood_feature_count"),
        "q_bayes_sigma": row.get("q_bayes_sigma"),
        "q_bayes_bimodal_risk": row.get("q_bayes_bimodal_risk"),
    }


def _select_with_focus_quotas(
    candidates: Sequence[Dict[str, Any]],
    *,
    limit: int,
    protected_focus_min_share: float = DEFAULT_PROTECTED_FOCUS_MIN_SHARE,
    major_settlement_max_share: float = DEFAULT_MAJOR_SETTLEMENT_MAX_SHARE,
) -> List[Dict[str, Any]]:
    limit = max(1, int(limit or 1))
    protected_min = max(0, min(limit, int(math.floor(limit * max(0.0, protected_focus_min_share)))))
    major_max = max(0, min(limit, int(math.ceil(limit * max(0.0, major_settlement_max_share)))))
    selected: List[Dict[str, Any]] = []
    selected_ids: set[int] = set()

    def lane(row: Mapping[str, Any]) -> str:
        return str(row.get("focus_lane") or "")

    def add(row: Dict[str, Any]) -> bool:
        row_id = id(row)
        if row_id in selected_ids or len(selected) >= limit:
            return False
        if lane(row) == MAJOR_SETTLEMENT_FOCUS_LANE:
            major_count = sum(1 for item in selected if lane(item) == MAJOR_SETTLEMENT_FOCUS_LANE)
            if major_count >= major_max:
                return False
        selected.append(row)
        selected_ids.add(row_id)
        return True

    protected_candidates = [row for row in candidates if lane(row) in PROTECTED_FOCUS_LANES]
    for row in protected_candidates:
        if len(selected) >= protected_min:
            break
        add(row)

    for row in candidates:
        if len(selected) >= limit:
            break
        add(row)

    if len(selected) < limit:
        for row in candidates:
            row_id = id(row)
            if row_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(row_id)
            if len(selected) >= limit:
                break
    return selected


def _resource_focus_for_rows(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total = len(rows or [])
    structural = [
        row for row in rows or [] if str(row.get("focus_lane") or "") in PROTECTED_FOCUS_LANES
    ]
    major = [
        row for row in rows or [] if str(row.get("focus_lane") or "") == MAJOR_SETTLEMENT_FOCUS_LANE
    ]
    by_lane: Dict[str, int] = {}
    for row in rows or []:
        lane = str(row.get("focus_lane") or "unknown")
        by_lane[lane] = by_lane.get(lane, 0) + 1

    def share(count: int) -> Optional[float]:
        if total <= 0:
            return None
        return float(count) / float(total)

    structural_share = share(len(structural))
    major_share = share(len(major))
    return {
        "selected_count": int(total),
        "target_structural_edge_min_share": DEFAULT_PROTECTED_FOCUS_MIN_SHARE,
        "target_major_settlement_max_share": DEFAULT_MAJOR_SETTLEMENT_MAX_SHARE,
        "structural_edge_count": len(structural),
        "major_settlement_count": len(major),
        "structural_edge_share": structural_share,
        "major_settlement_share": major_share,
        "by_focus_lane": dict(sorted(by_lane.items())),
        "target_met": (
            None
            if total <= 0
            else bool(
                (structural_share or 0.0) >= DEFAULT_PROTECTED_FOCUS_MIN_SHARE
                and (major_share or 0.0) <= DEFAULT_MAJOR_SETTLEMENT_MAX_SHARE
            )
        ),
    }


def _settlement_quality_for_rows(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    settlement_rows = [
        row
        for row in rows or []
        if str(row.get("focus_lane") or "") == MAJOR_SETTLEMENT_FOCUS_LANE
        or str(row.get("strategy_recommendation") or "") == "settlement_hold_review"
    ]
    total = len(settlement_rows)
    eligible = [
        row for row in settlement_rows if bool(row.get("weather_strategy_settlement_eligible"))
    ]
    source_ok = [
        row for row in settlement_rows if str(row.get("source_fit_tier") or "") == "settlement_source"
    ]
    station_known = [
        row for row in settlement_rows if str(row.get("station_id") or "").strip()
    ]
    station_match = [
        row
        for row in settlement_rows
        if row.get("source_station_match") is True
        or (
            row.get("source_station_match") is None
            and str(row.get("source_fit_tier") or "") == "settlement_source"
            and str(row.get("station_id") or "").strip()
        )
    ]

    def rate(count: int) -> Optional[float]:
        if total <= 0:
            return None
        return float(count) / float(total)

    return {
        "selected_settlement_count": int(total),
        "settlement_eligible_count": len(eligible),
        "settlement_source_count": len(source_ok),
        "station_known_count": len(station_known),
        "station_match_count": len(station_match),
        "settlement_eligible_rate": rate(len(eligible)),
        "settlement_source_rate": rate(len(source_ok)),
        "station_match_rate": rate(len(station_match)),
        "target_met": (
            None
            if total <= 0
            else bool(
                len(eligible) == total
                and len(source_ok) == total
                and len(station_match) == total
            )
        ),
    }


def _physical_edge_quality_for_rows(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    d0_rows = [
        row
        for row in rows or []
        if str(row.get("weather_strategy_stage") or "").startswith("d0")
        and str(row.get("focus_lane") or "") == "d0_physical_or_settlement"
    ]
    total = len(d0_rows)

    def has_physical(row: Mapping[str, Any]) -> bool:
        enabled = bool(row.get("q_bayes_physical_likelihood_enabled"))
        evidence = swm.to_float(row.get("q_bayes_physical_likelihood_evidence"))
        prob = swm.to_float(row.get("q_bayes_physical_likelihood_prob"))
        feature_count = swm.to_float(row.get("weather_likelihood_feature_count"))
        source = str(row.get("q_bayes_physical_likelihood_source") or "").strip()
        aux_source = str(row.get("weather_likelihood_aux_source") or "").strip()
        return bool(
            enabled
            or (evidence is not None and evidence > 0)
            or prob is not None
            or (feature_count is not None and feature_count > 0)
            or source
            or aux_source
        )

    physical = [row for row in d0_rows if has_physical(row)]
    feature_rows = [
        row
        for row in d0_rows
        if (swm.to_float(row.get("weather_likelihood_feature_count")) or 0.0) > 0
    ]
    bimodal = [row for row in d0_rows if bool(row.get("q_bayes_bimodal_risk"))]
    by_aux: Dict[str, int] = {}
    for row in d0_rows:
        aux = str(row.get("weather_likelihood_aux_source") or "").strip() or "missing"
        by_aux[aux] = by_aux.get(aux, 0) + 1

    def rate(count: int) -> Optional[float]:
        if total <= 0:
            return None
        return float(count) / float(total)

    return {
        "selected_d0_count": int(total),
        "physical_evidence_count": len(physical),
        "feature_count_present_count": len(feature_rows),
        "bimodal_risk_count": len(bimodal),
        "physical_evidence_rate": rate(len(physical)),
        "feature_count_present_rate": rate(len(feature_rows)),
        "by_aux_source": dict(sorted(by_aux.items())),
        "target_met": None if total <= 0 else bool(len(physical) == total),
    }


def build_strategy_batch(
    db_path: Path,
    event_dates: Sequence[str],
    *,
    recommendations: Sequence[str] = DEFAULT_RECOMMENDATIONS,
    focus_lanes: Sequence[str] = DEFAULT_FOCUS_LANES,
    max_events: int = 3,
    market_scope: str = "highest_temperature",
    protected_focus_min_share: float = DEFAULT_PROTECTED_FOCUS_MIN_SHARE,
    major_settlement_max_share: float = DEFAULT_MAJOR_SETTLEMENT_MAX_SHARE,
    min_entry_edge: float = DEFAULT_MIN_ENTRY_EDGE,
    pilot_cities: Sequence[str] = (),
    resource_focus_policy: str = DEFAULT_RESOURCE_FOCUS_POLICY,
    require_d0_physical_evidence: bool = DEFAULT_REQUIRE_D0_PHYSICAL_EVIDENCE,
    observation_target_focus: bool = False,
    observation_target_package_path: Optional[Path] = None,
    observation_target_package: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    observation_package: Dict[str, Any] = {}
    if observation_target_focus:
        observation_package = (
            dict(observation_target_package)
            if isinstance(observation_target_package, Mapping)
            else _load_observation_target_package(observation_target_package_path)
        )
    report = build_strategy_plan(
        db_path,
        event_dates,
        rows_limit=0,
        top=max(50, int(max_events or 0)),
        market_scope=market_scope,
    )
    recommendation_set = {str(item) for item in recommendations if str(item).strip()}
    focus_set = {str(item) for item in focus_lanes if str(item).strip()}
    pilot_city_set = {_city_key(item) for item in pilot_cities if _city_key(str(item))}
    observation_scope_city_set = set(pilot_city_set)
    if observation_package:
        observation_scope_city_set.update(_observation_target_city_keys(observation_package))
        observation_scope_city_set.update(
            _observation_recommended_pilot_city_keys(observation_package)
        )
        observation_scope_city_set.update(_observation_expansion_city_keys(observation_package))
    recommended_pilot_city_set = (
        _observation_recommended_pilot_city_keys(observation_package)
        if observation_package
        else set()
    )
    candidates: List[Dict[str, Any]] = []
    plan_rows_seen = 0
    write_scope_filtered_rows = 0
    scoped_plan_rows_by_recommended_pilot_city: Dict[str, int] = {}
    skipped_by_recommendation: Dict[str, int] = {}
    skipped_by_diagnostic: Dict[str, int] = {}
    skipped_by_city_diagnostic: Dict[str, Dict[str, int]] = {}
    skipped_recommendation_examples: List[Dict[str, Any]] = []
    skipped_diagnostic_examples: Dict[str, List[Dict[str, Any]]] = {}
    considered_rows = 0
    excluded_rows = 0
    excluded_by_reason: Dict[str, int] = {}
    excluded_by_city_reason: Dict[str, Dict[str, int]] = {}
    excluded_examples: List[Dict[str, Any]] = []
    seen_keys = set()
    for plan in report.get("plans") or []:
        for row in plan.get("rows") or []:
            if not isinstance(row, dict):
                continue
            plan_rows_seen += 1
            if not _row_allowed_by_write_scope(row):
                write_scope_filtered_rows += 1
                continue
            rec = str(row.get("strategy_recommendation") or "")
            lane = str(row.get("focus_lane") or "")
            if focus_set and lane not in focus_set:
                continue
            city_key = _city_key(str(row.get("city_key") or ""))
            if observation_scope_city_set and city_key not in observation_scope_city_set:
                continue
            if city_key in recommended_pilot_city_set:
                scoped_plan_rows_by_recommended_pilot_city[city_key] = (
                    scoped_plan_rows_by_recommended_pilot_city.get(city_key, 0) + 1
                )
            if recommendation_set and rec not in recommendation_set:
                skipped_by_recommendation[rec or "unknown"] = (
                    skipped_by_recommendation.get(rec or "unknown", 0) + 1
                )
                skipped_diagnostic = _row_skipped_diagnostic(
                    row,
                    min_entry_edge=float(min_entry_edge),
                )
                raw_lcb_edge = _row_raw_lcb_edge(row)
                skipped_by_diagnostic[skipped_diagnostic] = (
                    skipped_by_diagnostic.get(skipped_diagnostic, 0) + 1
                )
                city_bucket = skipped_by_city_diagnostic.setdefault(city_key or "unknown", {})
                city_bucket[skipped_diagnostic] = city_bucket.get(skipped_diagnostic, 0) + 1
                diagnostic_bucket = skipped_diagnostic_examples.setdefault(
                    skipped_diagnostic,
                    [],
                )
                if len(diagnostic_bucket) < 5:
                    diagnostic_bucket.append(
                        _skipped_review_example(
                            row,
                            recommendation=rec,
                            diagnostic=skipped_diagnostic,
                            raw_lcb_edge=raw_lcb_edge,
                        )
                    )
                if len(skipped_recommendation_examples) < 8:
                    skipped_recommendation_examples.append(
                        _skipped_review_example(
                            row,
                            recommendation=rec,
                            diagnostic=skipped_diagnostic,
                            raw_lcb_edge=raw_lcb_edge,
                        )
                    )
                continue
            considered_rows += 1
            if rec not in ENTRY_CANDIDATE_RECOMMENDATIONS:
                excluded_rows += 1
                excluded_by_reason["not_entry_candidate_recommendation"] = (
                    excluded_by_reason.get("not_entry_candidate_recommendation", 0) + 1
                )
                city_bucket = excluded_by_city_reason.setdefault(city_key or "unknown", {})
                city_bucket["not_entry_candidate_recommendation"] = (
                    city_bucket.get("not_entry_candidate_recommendation", 0) + 1
                )
                if len(excluded_examples) < 8:
                    excluded_examples.append(
                        {
                            "slug": row.get("slug"),
                            "event_date": row.get("event_date"),
                            "outcome": row.get("outcome"),
                            "side": row.get("side"),
                            "strategy_recommendation": rec,
                            "focus_lane": lane,
                            "exclude_reason": "not_entry_candidate_recommendation",
                            "weather_strategy_stage": row.get("weather_strategy_stage"),
                            "weather_strategy_mode": row.get("weather_strategy_mode"),
                            "q_bayes_physical_likelihood_enabled": row.get(
                                "q_bayes_physical_likelihood_enabled"
                            ),
                            "weather_likelihood_feature_count": row.get(
                                "weather_likelihood_feature_count"
                            ),
                            "q_bayes_sigma": row.get("q_bayes_sigma"),
                            "q_bayes_bimodal_risk": row.get("q_bayes_bimodal_risk"),
                        }
                    )
                continue
            exclusion_reason = _row_exclusion_reason(
                row,
                min_entry_edge=float(min_entry_edge),
                require_d0_physical_evidence=bool(require_d0_physical_evidence),
            )
            if exclusion_reason:
                excluded_rows += 1
                excluded_by_reason[exclusion_reason] = excluded_by_reason.get(exclusion_reason, 0) + 1
                city_bucket = excluded_by_city_reason.setdefault(city_key or "unknown", {})
                city_bucket[exclusion_reason] = city_bucket.get(exclusion_reason, 0) + 1
                if len(excluded_examples) < 8:
                    excluded_examples.append(
                        {
                            "slug": row.get("slug"),
                            "event_date": row.get("event_date"),
                            "outcome": row.get("outcome"),
                            "side": row.get("side"),
                            "strategy_recommendation": rec,
                            "focus_lane": lane,
                            "exclude_reason": exclusion_reason,
                            "entry_usable_edge": row.get("entry_usable_edge"),
                            "entry_usable_edge_size_scale": row.get("entry_usable_edge_size_scale"),
                            "entry_usable_edge_size_scale_adjusted": row.get(
                                "entry_usable_edge_size_scale_adjusted"
                            ),
                            "entry_usable_edge_na_reason": row.get("entry_usable_edge_na_reason"),
                            "entry_edge_value": _row_entry_edge_value(row),
                            "min_entry_edge": float(min_entry_edge),
                            "weather_strategy_settlement_eligible": row.get(
                                "weather_strategy_settlement_eligible"
                            ),
                            "source_fit_tier": row.get("source_fit_tier"),
                            "station_id": row.get("station_id"),
                            "source_station_id": row.get("source_station_id"),
                            "source_station_match": row.get("source_station_match"),
                            "weather_strategy_season": row.get("weather_strategy_season"),
                            "weather_strategy_season_size_scale": row.get(
                                "weather_strategy_season_size_scale"
                            ),
                            "q_bayes_physical_likelihood_enabled": row.get(
                                "q_bayes_physical_likelihood_enabled"
                            ),
                            "q_bayes_physical_likelihood_source": row.get(
                                "q_bayes_physical_likelihood_source"
                            ),
                            "weather_likelihood_aux_source": row.get(
                                "weather_likelihood_aux_source"
                            ),
                            "weather_likelihood_feature_count": row.get(
                                "weather_likelihood_feature_count"
                            ),
                            "edge_tradeability_profit_value": row.get("edge_tradeability_profit_value"),
                        }
                    )
                continue
            slug = str(row.get("slug") or "").strip()
            event_date = str(row.get("event_date") or "").strip().split("T", 1)[0]
            if not slug or not event_date:
                continue
            key = (slug, event_date)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(dict(row))
    target_adjustment_rows = _target_adjustment_candidate_rows(
        db_path,
        event_dates,
        pilot_city_set=sorted(pilot_city_set),
    )
    target_adjustment_added = 0
    for row in target_adjustment_rows:
        slug = str(row.get("slug") or "").strip()
        event_date = str(row.get("event_date") or "").strip().split("T", 1)[0]
        if not slug or not event_date:
            continue
        key = (slug, event_date)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(dict(row))
        target_adjustment_added += 1
    ssot_main_candidate_rows = _ssot_blocked_main_candidate_rows(
        db_path,
        event_dates,
        pilot_city_set=sorted(pilot_city_set),
    )
    ssot_diagnostic_rows = [
        row for row in ssot_main_candidate_rows if not bool(row.get("entry_ok"))
    ]
    ssot_main_candidate_added = 0
    ssot_diagnostic_added = 0
    for row in ssot_main_candidate_rows:
        slug = str(row.get("slug") or "").strip()
        event_date = str(row.get("event_date") or "").strip().split("T", 1)[0]
        if not slug or not event_date:
            continue
        key = (slug, event_date)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(dict(row))
        ssot_main_candidate_added += 1
        if not bool(row.get("entry_ok")):
            ssot_diagnostic_added += 1
    observation_target_focus_summary: Dict[str, Any] = {
        "enabled": bool(observation_target_focus),
        "package_path": str(observation_target_package_path or ""),
        "package_ok": None,
        "candidate_target_rows": 0,
        "candidate_expansion_rows": 0,
    }
    if observation_target_focus:
        package = observation_package
        if package:
            package.setdefault("package_path", str(observation_target_package_path or ""))
            observation_target_focus_summary = _apply_observation_target_focus(
                candidates,
                package,
            )
            recommended_keys = set(
                observation_target_focus_summary.get("recommended_pilot_city_keys") or []
            )
            candidate_by_city = (
                observation_target_focus_summary.get(
                    "candidate_recommended_pilot_rows_by_city"
                )
                if isinstance(
                    observation_target_focus_summary.get(
                        "candidate_recommended_pilot_rows_by_city"
                    ),
                    Mapping,
                )
                else {}
            )
            observation_target_focus_summary[
                "recommended_pilot_city_diagnostics"
            ] = _recommended_pilot_city_diagnostics(
                recommended_city_keys={str(city) for city in recommended_keys},
                scoped_plan_rows_by_city=scoped_plan_rows_by_recommended_pilot_city,
                candidate_rows_by_city=candidate_by_city,
                skipped_by_city=skipped_by_city_diagnostic,
                excluded_by_city=excluded_by_city_reason,
            )
            candidates.sort(key=_target_focused_sort_key)
        else:
            observation_target_focus_summary["package_ok"] = False
            candidates.sort(key=_row_sort_key)
    else:
        candidates.sort(key=_row_sort_key)
    limit = max(1, int(max_events or 1))
    selected = _select_with_focus_quotas(
        candidates,
        limit=limit,
        protected_focus_min_share=protected_focus_min_share,
        major_settlement_max_share=major_settlement_max_share,
    )
    selected_resource_focus = _resource_focus_for_rows(selected)
    selected_settlement_quality = _settlement_quality_for_rows(selected)
    selected_physical_edge_quality = _physical_edge_quality_for_rows(selected)
    policy = str(resource_focus_policy or DEFAULT_RESOURCE_FOCUS_POLICY).strip().lower()
    if policy not in RESOURCE_FOCUS_POLICIES:
        policy = DEFAULT_RESOURCE_FOCUS_POLICY
    warnings: List[str] = []
    blockers: List[str] = []
    target_met = selected_resource_focus.get("target_met")
    if policy in {"warn", "enforce"} and target_met is False:
        code = "selected_resource_focus_target_not_met"
        if policy == "enforce":
            blockers.append(code)
        else:
            warnings.append(code)
    if selected_physical_edge_quality.get("target_met") is False:
        warnings.append("selected_d0_physical_evidence_missing")
    include_event_keys = [
        {"slug": str(row.get("slug") or ""), "event_date": str(row.get("event_date") or "")}
        for row in selected
    ]
    return {
        "ok": not blockers,
        "db_path": str(db_path),
        "event_dates": list(event_dates),
        "market_scope": market_scope,
        "recommendations": list(recommendations),
        "focus_lanes": list(focus_lanes),
        "max_events": limit,
        "protected_focus_min_share": float(protected_focus_min_share),
        "major_settlement_max_share": float(major_settlement_max_share),
        "min_entry_edge": float(min_entry_edge),
        "require_d0_physical_evidence": bool(require_d0_physical_evidence),
        "resource_focus_policy": policy,
        "pilot_cities": sorted(pilot_city_set),
        "observation_scope_cities": sorted(observation_scope_city_set),
        "observation_target_focus": observation_target_focus_summary,
        "warnings": warnings,
        "blockers": blockers,
        "plan_rows_seen": int(plan_rows_seen),
        "write_scope": "chengdu_zuuu" if _write_scope_is_chengdu_zuuu() else "all",
        "write_scope_filtered_rows": int(write_scope_filtered_rows),
        "skipped_recommendation_rows": int(sum(skipped_by_recommendation.values())),
        "skipped_recommendations": dict(sorted(skipped_by_recommendation.items())),
        "skipped_diagnostics": dict(sorted(skipped_by_diagnostic.items())),
        "skipped_diagnostics_by_city": {
            city: dict(sorted(reasons.items()))
            for city, reasons in sorted(skipped_by_city_diagnostic.items())
        },
        "skipped_diagnostic_examples": {
            key: value for key, value in sorted(skipped_diagnostic_examples.items())
        },
        "skipped_recommendation_examples": skipped_recommendation_examples,
        "considered_entry_rows": int(considered_rows),
        "excluded_entry_rows": int(excluded_rows),
        "excluded_entry_reasons": dict(sorted(excluded_by_reason.items())),
        "excluded_entry_reasons_by_city": {
            city: dict(sorted(reasons.items()))
            for city, reasons in sorted(excluded_by_city_reason.items())
        },
        "excluded_entry_examples": excluded_examples,
        "target_adjustment_candidate_events": int(len(target_adjustment_rows)),
        "target_adjustment_candidate_events_added": int(target_adjustment_added),
        "ssot_main_candidate_events": int(len(ssot_main_candidate_rows)),
        "ssot_main_candidate_events_added": int(ssot_main_candidate_added),
        "ssot_diagnostic_candidate_events": int(len(ssot_diagnostic_rows)),
        "ssot_diagnostic_candidate_events_added": int(ssot_diagnostic_added),
        "candidate_events": len(candidates),
        "selected_events": len(selected),
        "selected_resource_focus": selected_resource_focus,
        "selected_settlement_quality": selected_settlement_quality,
        "selected_physical_edge_quality": selected_physical_edge_quality,
        "include_event_keys": include_event_keys,
        "selected_rows": selected,
        "plan_summary": [
            {
                "event_date": plan.get("event_date"),
                "summary": plan.get("summary"),
            }
            for plan in report.get("plans") or []
            if isinstance(plan, dict)
        ],
    }


def compact_batch_payload(batch: Mapping[str, Any]) -> Dict[str, Any]:
    selected_rows = []
    for row in batch.get("selected_rows") or []:
        if not isinstance(row, Mapping):
            continue
        selected_rows.append(
            {
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "outcome": row.get("outcome"),
                "side": row.get("side"),
                "strategy_recommendation": row.get("strategy_recommendation"),
                "focus_lane": row.get("focus_lane"),
                "weather_strategy_edge_tier": row.get("weather_strategy_edge_tier"),
                "weather_strategy_stage": row.get("weather_strategy_stage"),
                "weather_strategy_mode": row.get("weather_strategy_mode"),
                "weather_strategy_edge_weight": row.get("weather_strategy_edge_weight"),
                "weather_strategy_settlement_weight": row.get("weather_strategy_settlement_weight"),
                "weather_strategy_settlement_eligible": row.get("weather_strategy_settlement_eligible"),
                "q_bayes_physical_likelihood_enabled": row.get(
                    "q_bayes_physical_likelihood_enabled"
                ),
                "q_bayes_physical_likelihood_evidence": row.get(
                    "q_bayes_physical_likelihood_evidence"
                ),
                "q_bayes_physical_likelihood_source": row.get(
                    "q_bayes_physical_likelihood_source"
                ),
                "q_bayes_physical_likelihood_prob": row.get(
                    "q_bayes_physical_likelihood_prob"
                ),
                "weather_likelihood_features_source": row.get(
                    "weather_likelihood_features_source"
                ),
                "weather_likelihood_aux_source": row.get("weather_likelihood_aux_source"),
                "weather_likelihood_feature_count": row.get("weather_likelihood_feature_count"),
                "q_bayes_bimodal_risk": row.get("q_bayes_bimodal_risk"),
                "q_bayes_bimodal_size_scale": row.get("q_bayes_bimodal_size_scale"),
                "source_fit_tier": row.get("source_fit_tier"),
                "source_fit_na_reason": row.get("source_fit_na_reason"),
                "station_id": row.get("station_id"),
                "source_station_id": row.get("source_station_id"),
                "source_station_match": row.get("source_station_match"),
                "edge_tradeability_profit_value": row.get("edge_tradeability_profit_value"),
                "entry_usable_edge_size_scale_adjusted": row.get(
                    "entry_usable_edge_size_scale_adjusted"
                ),
                "weather_strategy_season": row.get("weather_strategy_season"),
                "weather_strategy_season_size_scale": row.get("weather_strategy_season_size_scale"),
                "weather_strategy_reverse_edge": row.get("weather_strategy_reverse_edge"),
                "weather_strategy_reverse_edge_action": row.get("weather_strategy_reverse_edge_action"),
                "reverse_edge_execution_side": row.get("reverse_edge_execution_side"),
                "reverse_edge_execution_action": row.get("reverse_edge_execution_action"),
                "entry_ok": row.get("entry_ok"),
                "entry_reasons": row.get("entry_reasons"),
                "auto_trade_ssot_diagnostic": row.get("auto_trade_ssot_diagnostic"),
                "target_realization_batch_reason": row.get("target_realization_batch_reason"),
                "target_realization_leg_role": row.get("target_realization_leg_role"),
                "target_realization_plan_action": row.get("target_realization_plan_action"),
                "target_realization_plan_notional": row.get(
                    "target_realization_plan_notional"
                ),
                "target_realization_plan_notional_source": row.get(
                    "target_realization_plan_notional_source"
                ),
                "kelly_target_notional": row.get("kelly_target_notional"),
                "kelly_sizing_source": row.get("kelly_sizing_source"),
                "order_size_source": row.get("order_size_source"),
                "posterior_settlement_candidate_bucket_prob": row.get(
                    "posterior_settlement_candidate_bucket_prob"
                ),
                "target_realization_reliability": row.get(
                    "target_realization_reliability"
                ),
                "target_adjustment_action": row.get("target_adjustment_action"),
                "target_adjustment_reason_codes": row.get("target_adjustment_reason_codes"),
                "switch_buy_allowed": row.get("switch_buy_allowed"),
                "new_buy_allowed_this_snapshot": row.get("new_buy_allowed_this_snapshot"),
                "target_bucket": row.get("target_bucket"),
                "target_posterior": row.get("target_posterior"),
                "new_target_bucket": row.get("new_target_bucket"),
                "new_target_posterior": row.get("new_target_posterior"),
                "held_target_bucket": row.get("held_target_bucket"),
                "held_target_side": row.get("held_target_side"),
                "current_position_side": row.get("current_position_side"),
                "old_bucket_reverse_side": row.get("old_bucket_reverse_side"),
                "held_target_posterior": row.get("held_target_posterior"),
                "current_top_bucket": row.get("current_top_bucket"),
                "has_active_position": row.get("has_active_position"),
                "has_active_yes_position": row.get("has_active_yes_position"),
                "observation_target_match": row.get("observation_target_match"),
                "observation_target_priority": row.get("observation_target_priority"),
                "observation_target_city_match": row.get("observation_target_city_match"),
                "observation_recommended_pilot_city_match": row.get(
                    "observation_recommended_pilot_city_match"
                ),
                "observation_expansion_city_match": row.get(
                    "observation_expansion_city_match"
                ),
                "observation_target_strategy": row.get("observation_target_strategy"),
            "observation_target_needed_resolved_rows": row.get(
                "observation_target_needed_resolved_rows"
            ),
            "observation_target_needed_additional_wins": row.get(
                "observation_target_needed_additional_wins"
            ),
            "observation_target_recommended_action": row.get(
                "observation_target_recommended_action"
            ),
            "target_realization_capital_plan_version": row.get(
                "target_realization_capital_plan_version"
            ),
            "capital_plan_target_bucket": row.get("capital_plan_target_bucket"),
            "capital_plan_second_target_bucket": row.get("capital_plan_second_target_bucket"),
            "trajectory_momentum_impact": row.get("trajectory_momentum_impact"),
        }
    )
    return {
        "ok": batch.get("ok"),
        "dry_run": batch.get("dry_run"),
        "db_path": batch.get("db_path"),
        "event_dates": batch.get("event_dates"),
        "market_scope": batch.get("market_scope"),
        "max_events": batch.get("max_events"),
        "protected_focus_min_share": batch.get("protected_focus_min_share"),
        "major_settlement_max_share": batch.get("major_settlement_max_share"),
        "min_entry_edge": batch.get("min_entry_edge"),
        "require_d0_physical_evidence": batch.get("require_d0_physical_evidence"),
        "resource_focus_policy": batch.get("resource_focus_policy"),
        "pilot_cities": batch.get("pilot_cities"),
        "observation_scope_cities": batch.get("observation_scope_cities"),
        "observation_target_focus": batch.get("observation_target_focus"),
        "warnings": batch.get("warnings"),
        "blockers": batch.get("blockers"),
        "plan_rows_seen": batch.get("plan_rows_seen"),
        "skipped_recommendation_rows": batch.get("skipped_recommendation_rows"),
        "skipped_recommendations": batch.get("skipped_recommendations"),
        "skipped_diagnostics": batch.get("skipped_diagnostics"),
        "skipped_diagnostics_by_city": batch.get("skipped_diagnostics_by_city"),
        "skipped_diagnostic_examples": batch.get("skipped_diagnostic_examples"),
        "skipped_recommendation_examples": batch.get("skipped_recommendation_examples"),
        "candidate_events": batch.get("candidate_events"),
        "considered_entry_rows": batch.get("considered_entry_rows"),
        "excluded_entry_rows": batch.get("excluded_entry_rows"),
        "excluded_entry_reasons": batch.get("excluded_entry_reasons"),
        "excluded_entry_reasons_by_city": batch.get("excluded_entry_reasons_by_city"),
        "excluded_entry_examples": batch.get("excluded_entry_examples"),
        "target_adjustment_candidate_events": batch.get("target_adjustment_candidate_events"),
        "target_adjustment_candidate_events_added": batch.get("target_adjustment_candidate_events_added"),
        "ssot_main_candidate_events": batch.get("ssot_main_candidate_events"),
        "ssot_main_candidate_events_added": batch.get("ssot_main_candidate_events_added"),
        "ssot_diagnostic_candidate_events": batch.get("ssot_diagnostic_candidate_events"),
        "ssot_diagnostic_candidate_events_added": batch.get("ssot_diagnostic_candidate_events_added"),
        "selected_events": batch.get("selected_events"),
        "selected_resource_focus": batch.get("selected_resource_focus"),
        "selected_settlement_quality": batch.get("selected_settlement_quality"),
        "selected_physical_edge_quality": batch.get("selected_physical_edge_quality"),
        "include_event_keys": batch.get("include_event_keys"),
        "selected_rows": selected_rows,
        "enqueue_result": batch.get("enqueue_result"),
    }


def _entry_expected_targets_from_selected_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    include_event_key: bool = False,
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        outcome = str(row.get("outcome") or "").strip()
        side = str(row.get("side") or "").strip().upper()
        if not outcome or side not in {"YES", "NO"}:
            continue
        key = (outcome, side)
        if key in seen:
            continue
        seen.add(key)
        raw_strategy = str(row.get("strategy") or "").strip().lower()
        row_recommendation = str(row.get("strategy_recommendation") or "").strip()
        row_mode = str(row.get("weather_strategy_mode") or "").strip().lower()
        if (
            row_recommendation
            in {TARGET_REALIZATION_SWITCH_RECOMMENDATION, TARGET_REALIZATION_ORDER_INTENT_RECOMMENDATION}
            or row_mode == "target_realization"
            or row.get("ssot_target_bucket") is not None
            or row.get("posterior_settlement_target_bucket") is not None
        ):
            strategy = TARGET_REALIZATION_STRATEGY
        elif raw_strategy in {"main", TARGET_REALIZATION_STRATEGY}:
            strategy = raw_strategy
        else:
            strategy = "legacy_diagnostic"
        entry_ok = True
        if "entry_ok" in row:
            entry_ok = bool(row.get("entry_ok"))
        raw_entry_reasons = row.get("entry_reasons")
        if isinstance(raw_entry_reasons, list):
            entry_reasons = [str(item) for item in raw_entry_reasons if str(item or "").strip()]
        else:
            entry_reasons = []
        if not entry_reasons:
            block_reason = str(
                row.get("weather_strategy_entry_block_reason")
                or row.get("entry_usable_edge_na_reason")
                or ""
            ).strip()
            if block_reason:
                entry_reasons = [block_reason]
        if not entry_reasons:
            entry_reasons = [str(row.get("strategy_recommendation") or "weather_strategy_batch")]
        target = {
                **(
                    {
                        "slug": str(row.get("slug") or ""),
                        "event_date": str(row.get("event_date") or ""),
                    }
                    if include_event_key
                    else {}
                ),
                "outcome": outcome,
                "side": side,
                "strategy": strategy,
                "entry_ok": bool(entry_ok),
                "entry_reasons": entry_reasons,
                "edge_score": row.get("entry_usable_edge_sort_score"),
                "edge_min": row.get("edge_tradeability_profit_value"),
                "exec_edge_entry": row.get("edge_tradeability_profit_value"),
                "edge_realizable": row.get("edge_tradeability_profit_value"),
                "edge_tradeability_profit_value": row.get("edge_tradeability_profit_value"),
                "edge_tradeability_pass": row.get("edge_tradeability_pass"),
                "ask": row.get("ask"),
                "q_min_bound": row.get("q_entry"),
                "spread": row.get("spread"),
                "q_entry": row.get("q_entry"),
                "q_quote": row.get("q_quote"),
                "q_calibrated": row.get("q_calibrated"),
                "q_raw": row.get("q_raw_static"),
                "entry_usable_edge": row.get("entry_usable_edge"),
                "entry_usable_edge_size_scale": (
                    row.get("entry_usable_edge_size_scale_adjusted")
                    if row.get("entry_usable_edge_size_scale_adjusted") is not None
                    else row.get("entry_usable_edge_size_scale")
                ),
                "entry_usable_edge_size_scale_before_season": row.get(
                    "entry_usable_edge_size_scale_before_season"
                ),
                "weather_strategy_season": row.get("weather_strategy_season"),
                "weather_strategy_season_size_scale": row.get("weather_strategy_season_size_scale"),
                "weather_strategy_stage": row.get("weather_strategy_stage"),
                "weather_strategy_mode": row.get("weather_strategy_mode"),
                "weather_strategy_edge_weight": row.get("weather_strategy_edge_weight"),
                "weather_strategy_settlement_weight": row.get("weather_strategy_settlement_weight"),
                "weather_strategy_settlement_eligible": row.get("weather_strategy_settlement_eligible"),
                "q_bayes_physical_likelihood_enabled": row.get(
                    "q_bayes_physical_likelihood_enabled"
                ),
                "q_bayes_physical_likelihood_evidence": row.get(
                    "q_bayes_physical_likelihood_evidence"
                ),
                "q_bayes_physical_likelihood_source": row.get(
                    "q_bayes_physical_likelihood_source"
                ),
                "q_bayes_physical_likelihood_prob": row.get(
                    "q_bayes_physical_likelihood_prob"
                ),
                "weather_likelihood_features_source": row.get(
                    "weather_likelihood_features_source"
                ),
                "weather_likelihood_aux_source": row.get("weather_likelihood_aux_source"),
                "weather_likelihood_feature_count": row.get("weather_likelihood_feature_count"),
                "q_bayes_bimodal_risk": row.get("q_bayes_bimodal_risk"),
                "q_bayes_bimodal_size_scale": row.get("q_bayes_bimodal_size_scale"),
                "source_fit_tier": row.get("source_fit_tier"),
                "source_fit_na_reason": row.get("source_fit_na_reason"),
                "station_id": row.get("station_id"),
                "source_station_id": row.get("source_station_id"),
                "source_station_match": row.get("source_station_match"),
                "weather_strategy_reverse_edge": row.get("weather_strategy_reverse_edge"),
                "weather_strategy_reverse_edge_action": row.get(
                    "weather_strategy_reverse_edge_action"
                ),
                "reverse_edge_execution_side": row.get("reverse_edge_execution_side"),
                "reverse_edge_execution_action": row.get("reverse_edge_execution_action"),
	                "target_realization_leg_role": row.get("target_realization_leg_role")
	                or "new_bucket_entry",
	                "target_realization_execution_action": "BUY",
	                "target_realization_position_effect": row.get(
                    "target_realization_position_effect"
                )
                or "open_or_add_new_bucket_side",
	                "sell_old_buy_new_semantics": row.get("sell_old_buy_new_semantics")
                or "buy_new_bucket_side",
	            }
        suffix = "yes" if side == "YES" else "no"
        for optional_field in (
            "entry_usable_edge_na_reason",
            "weather_strategy_entry_block_reason",
            "auto_trade_ssot_diagnostic",
            "target_realization_batch_reason",
            "strategy_family",
            "entry_strategy_family",
            "weather_strategy_family",
            "price",
            "order_price",
            "size",
            "order_size",
            "target_realization_plan_action",
            "target_realization_plan_notional",
            "target_realization_plan_notional_source",
            "kelly_target_notional",
            "kelly_sizing_source",
            "order_size_source",
            "posterior_settlement_entry_allowed",
            "posterior_settlement_entry_block_reason",
            "posterior_settlement_size_scale",
            "posterior_settlement_sort_score",
            "posterior_settlement_candidate_bucket",
            "posterior_settlement_candidate_bucket_prob",
	            "target_bucket",
	            "ssot_target_bucket",
	            "posterior_settlement_target_bucket",
	            "target_posterior",
	            "posterior_settlement_target_posterior",
                    "current_top_bucket",
                    "posterior_settlement_current_top_bucket",
                    "current_top_posterior",
                    "posterior_settlement_current_top_posterior",
	            "old_target_bucket",
	            "leg_bucket",
            "target_realization_max_entry_price",
            "target_realization_order_price_cap",
            "target_realization_plan_notional_cap_usdc",
            "target_notional_cap_usdc",
            "target_realization_plan_notional_cap_applied",
            "yes_dual_head_active",
            "yes_dual_head_gap_pp",
            "yes_dual_head_max_gap_pp",
            "target_realization_reliability",
            "target_realization_reliability_source",
            "target_realization_reliability_mode",
            "target_realization_threshold_c",
            "target_realization_path_probability",
            "target_realization_path_probability_source",
	            "held_target_bucket",
	            "held_target_side",
	            "current_position_side",
                    "current_position_target_bucket",
	            "old_bucket_reverse_side",
	            "held_target_posterior",
	            "current_target_posterior",
	            "new_target_bucket",
            "new_target_posterior",
            "switch_reference_bucket",
            "switch_reference_posterior",
            *SSOT_EXECUTION_CONTEXT_FIELDS,
        ):
            optional_value = row.get(optional_field)
            if optional_value is None:
                continue
            if isinstance(optional_value, str) and not optional_value.strip():
                continue
            target[optional_field] = optional_value
        token_id = str(
            row.get("token_id")
            or row.get(f"{suffix}_token")
            or row.get("yes_token" if side == "YES" else "no_token")
            or ""
        ).strip()
        if token_id:
            target["token_id"] = token_id
        targets.append(target)
        switch_like = (
            str(row.get("strategy_recommendation") or "").strip()
            == TARGET_REALIZATION_SWITCH_RECOMMENDATION
            or str(row.get("target_adjustment_action") or "").strip().upper() == ACTION_SWITCH_BUY
        )
        held_bucket = str(row.get("held_target_bucket") or "").strip()
        held_side = str(
            row.get("held_target_side")
            or row.get("current_position_side")
            or ""
        ).strip().upper()
        reverse_side = _opposite_trade_side(held_side)
        if switch_like and held_bucket and reverse_side:
            reverse_key = (held_bucket, reverse_side)
            if reverse_key not in seen:
                seen.add(reverse_key)
                reverse_reasons = list(entry_reasons)
                if "target_realization_switch_old_bucket_reverse" not in reverse_reasons:
                    reverse_reasons.append("target_realization_switch_old_bucket_reverse")
                reverse_target = {
                    **(
                        {
                            "slug": str(row.get("slug") or ""),
                            "event_date": str(row.get("event_date") or ""),
                        }
                        if include_event_key
                        else {}
                    ),
                    "outcome": held_bucket,
                    "side": reverse_side,
                    "strategy": strategy,
                    "entry_ok": bool(entry_ok),
                    "entry_reasons": reverse_reasons,
                    "target_realization_leg_role": "old_bucket_reverse",
                    "target_realization_batch_reason": row.get("target_realization_batch_reason"),
	                    "target_adjustment_action": row.get("target_adjustment_action"),
	                    "target_bucket": row.get("target_bucket"),
	                    "ssot_target_bucket": row.get("ssot_target_bucket") or row.get("target_bucket"),
	                    "posterior_settlement_target_bucket": row.get("posterior_settlement_target_bucket") or row.get("target_bucket"),
	                    "target_posterior": row.get("target_posterior"),
		                    "posterior_settlement_target_posterior": row.get("posterior_settlement_target_posterior") or row.get("target_posterior"),
		                    "current_top_bucket": row.get("current_top_bucket") or row.get("target_bucket"),
		                    "posterior_settlement_current_top_bucket": row.get("posterior_settlement_current_top_bucket") or row.get("current_top_bucket") or row.get("target_bucket"),
		                    "current_top_posterior": row.get("current_top_posterior") or row.get("target_posterior"),
		                    "posterior_settlement_current_top_posterior": row.get("posterior_settlement_current_top_posterior") or row.get("current_top_posterior") or row.get("target_posterior"),
		                    "new_target_bucket": row.get("new_target_bucket") or outcome,
		                    "new_target_posterior": row.get("new_target_posterior") or row.get("target_posterior"),
		                    "old_target_bucket": held_bucket,
		                    "leg_bucket": held_bucket,
		                    "held_target_bucket": held_bucket,
		                    "held_target_side": held_side,
		                    "held_target_posterior": row.get("held_target_posterior"),
		                    "current_target_posterior": row.get("current_target_posterior"),
		                    "current_position_target_bucket": held_bucket,
		                    "old_bucket_reverse_side": reverse_side,
		                    "switch_reference_bucket": row.get("switch_reference_bucket") or held_bucket,
		                    "switch_reference_posterior": row.get("switch_reference_posterior") or row.get("held_target_posterior"),
                    "target_realization_execution_action": "BUY",
                    "target_realization_position_effect": "open_or_add_old_bucket_opposite_side",
                    "sell_old_buy_new_semantics": "buy_old_bucket_opposite_side",
                    "paired_exit_outcome": held_bucket,
                    "paired_exit_side": held_side,
                    "paired_exit_execution_action": "SELL",
                    "weather_strategy_stage": row.get("weather_strategy_stage"),
                    "weather_strategy_mode": row.get("weather_strategy_mode"),
                }
                _copy_ssot_execution_context(reverse_target, row)
                targets.append(reverse_target)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/enqueue bounded weather strategy batches.")
    parser.add_argument("--db", default=str(DB_PATH), help="path to archive.db")
    parser.add_argument("--event-date", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--max-events", type=int, default=3)
    parser.add_argument("--recommendation", action="append", default=[])
    parser.add_argument("--focus-lane", action="append", default=[])
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--protected-focus-min-share", type=float, default=DEFAULT_PROTECTED_FOCUS_MIN_SHARE)
    parser.add_argument("--major-settlement-max-share", type=float, default=DEFAULT_MAJOR_SETTLEMENT_MAX_SHARE)
    parser.add_argument(
        "--min-entry-edge",
        type=float,
        default=float(os.environ.get("PM_WEATHER_STRATEGY_BATCH_MIN_ENTRY_EDGE", DEFAULT_MIN_ENTRY_EDGE)),
        help="minimum strict LCB/profit edge for auto-entry batch selection",
    )
    parser.add_argument(
        "--resource-focus-policy",
        choices=sorted(RESOURCE_FOCUS_POLICIES),
        default=os.environ.get("PM_RESOURCE_FOCUS_POLICY", DEFAULT_RESOURCE_FOCUS_POLICY),
        help="off=ignore, warn=surface only, enforce=block enqueue when selected focus target is missed",
    )
    parser.add_argument(
        "--allow-d0-without-physical-evidence",
        action="store_true",
        default=str(os.environ.get("PM_WEATHER_STRATEGY_ALLOW_D0_WITHOUT_PHYSICAL_EVIDENCE", "")).strip().lower()
        in {"1", "true", "yes", "on"},
        help="allow D0 physical/settlement lane entries without physical likelihood evidence",
    )
    parser.add_argument("--pilot-city", action="append", default=[], help="repeatable or comma-separated pilot city filter")
    parser.add_argument(
        "--observation-target-focus",
        action="store_true",
        default=str(os.environ.get("PM_WEATHER_STRATEGY_OBSERVATION_TARGET_FOCUS", "")).strip().lower()
        in {"1", "true", "yes", "on"},
        help="prioritize otherwise-qualified candidates that fill current paper observation target gaps",
    )
    parser.add_argument(
        "--observation-target-package",
        default=os.environ.get(
            "PM_PAPER_OBSERVATION_TARGET_JSON",
            str(DEFAULT_OBSERVATION_TARGET_PACKAGE_PATH),
        ),
        help="path to paper_observation_target_package JSON used for target-focused sorting",
    )
    parser.add_argument("--enqueue", action="store_true", help="write an auto_trade_jobs row")
    parser.add_argument("--compact", action="store_true", help="print compact JSON for logs")
    parser.add_argument("--source", default="weather_strategy_batch")
    parser.add_argument("--priority", type=int, default=140)
    parser.add_argument("--force", action="store_true", help="force enqueue even if a job is active")
    args = parser.parse_args()

    event_dates = _csv_values(args.event_date, [swm.today_date_str("America/New_York")])
    recommendations = _csv_values(args.recommendation, DEFAULT_RECOMMENDATIONS)
    focus_lanes = _csv_values(args.focus_lane, DEFAULT_FOCUS_LANES)
    pilot_defaults = _csv_values([os.environ.get("PM_SMALL_CAPITAL_PILOT_CITIES", "")], [])
    if not pilot_defaults:
        pilot_defaults = _default_pilot_cities_from_reports(2)
    pilot_cities = _csv_values(args.pilot_city, pilot_defaults)
    batch = build_strategy_batch(
        Path(args.db),
        event_dates,
        recommendations=recommendations,
        focus_lanes=focus_lanes,
        max_events=max(1, int(args.max_events or 1)),
        market_scope=str(args.market_scope or "highest_temperature"),
        protected_focus_min_share=float(args.protected_focus_min_share),
        major_settlement_max_share=float(args.major_settlement_max_share),
        min_entry_edge=float(args.min_entry_edge),
        pilot_cities=pilot_cities,
        resource_focus_policy=str(args.resource_focus_policy or DEFAULT_RESOURCE_FOCUS_POLICY),
        require_d0_physical_evidence=not bool(args.allow_d0_without_physical_evidence),
        observation_target_focus=bool(args.observation_target_focus),
        observation_target_package_path=Path(args.observation_target_package),
    )
    live_auto_trade = _env_bool("PM_AUTO_TRADE_ENABLED", False) and not _env_bool(
        "PM_AUTO_TRADE_DRY_RUN",
        True,
    )
    live_strategy_batch_enqueue_enabled = _env_bool(
        "PM_AUTO_TRADE_ENTRY_LIVE_STRATEGY_BATCH_ENQUEUE_ENABLED",
        False,
    )
    if args.enqueue and not batch.get("ok"):
        batch["enqueue_result"] = {
            "ok": False,
            "enqueued": False,
            "reason": "batch_policy_blocked",
            "blockers": batch.get("blockers") or [],
        }
    elif args.enqueue and live_auto_trade and not live_strategy_batch_enqueue_enabled:
        batch["enqueue_result"] = {
            "ok": True,
            "enqueued": False,
            "reason": "live_strategy_batch_enqueue_disabled_direct_path_only",
            "include_event_keys": batch.get("include_event_keys") or [],
        }
    elif args.enqueue and batch.get("include_event_keys"):
        entry_expected_targets = _entry_expected_targets_from_selected_rows(
            batch.get("selected_rows") or [],
            include_event_key=True,
        )
        run_date = str(event_dates[0] or swm.today_date_str("America/New_York"))
        payload = {
            "requested_from": "weather_strategy_batch",
            "include_event_keys": batch["include_event_keys"],
            "strategy_batch": {
                "recommendations": recommendations,
                "focus_lanes": focus_lanes,
                "selected_events": batch.get("selected_events"),
                "protected_focus_min_share": batch.get("protected_focus_min_share"),
                "major_settlement_max_share": batch.get("major_settlement_max_share"),
                "min_entry_edge": batch.get("min_entry_edge"),
                "require_d0_physical_evidence": batch.get("require_d0_physical_evidence"),
                "resource_focus_policy": batch.get("resource_focus_policy"),
                "observation_target_focus": batch.get("observation_target_focus") or {},
                "warnings": batch.get("warnings") or [],
                "blockers": batch.get("blockers") or [],
                "selected_resource_focus": batch.get("selected_resource_focus") or {},
                "selected_settlement_quality": batch.get("selected_settlement_quality") or {},
                "selected_physical_edge_quality": batch.get("selected_physical_edge_quality") or {},
                "pilot_cities": batch.get("pilot_cities"),
            },
        }
        if entry_expected_targets:
            payload["entry_expected_targets"] = entry_expected_targets
        try:
            max_retry = int(float(os.environ.get("PM_AUTO_TRADE_STRATEGY_BATCH_MAX_RETRY", "0")))
        except Exception:
            max_retry = 0
        max_retry = max(0, min(max_retry, 3))
        batch["enqueue_result"] = swm.enqueue_auto_trade_job(
            run_date,
            source=str(args.source or "weather_strategy_batch"),
            priority=int(args.priority or 0),
            payload=payload,
            max_retry=max_retry,
            force_enqueue=bool(args.force),
        )
    elif args.enqueue:
        batch["enqueue_result"] = {"ok": False, "enqueued": False, "reason": "no_selected_events"}
    else:
        batch["dry_run"] = True
    output = compact_batch_payload(batch) if args.compact else batch
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
