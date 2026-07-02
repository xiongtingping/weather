from __future__ import annotations

import math
import os
from typing import Any, Dict, Mapping, Optional

from posterior_settlement_runtime import (
    ACTION_SWITCH_BUY,
    LANE_NO_REMOTE,
    LANE_OBSERVE,
    LANE_YES_TARGET,
    STRATEGY_FAMILY as POSTERIOR_SETTLEMENT_FAMILY,
    classify_posterior_settlement_candidate,
)


EPS = 1e-9

WEATHER_STRATEGY_FIELD_NAMES = (
    "strategy_family",
    "strategy_lane",
    "entry_alpha_gate_source",
    "take_profit_enabled",
    "stop_loss_enabled",
    "settlement_exit_only",
    "target_bucket",
    "target_posterior",
    "target_realization_reliability",
    "target_realization_reliability_source",
    "second_target_bucket",
    "second_target_posterior",
    "remote_no_bucket",
    "remote_no_reason",
    "remote_bucket_realization_prob",
    "bucket_switch_reason",
    "posterior_settlement_candidate_bucket",
    "posterior_settlement_candidate_bucket_prob",
    "posterior_settlement_bucket_distance",
    "posterior_settlement_entry_allowed",
    "posterior_settlement_entry_block_reason",
    "posterior_settlement_sort_score",
    "posterior_settlement_size_scale",
    "previous_top_bucket",
    "current_top_bucket",
    "top_bucket_changed",
    "held_target_bucket",
    "held_target_side",
    "current_position_target_bucket",
    "current_position_side",
    "has_active_position",
    "has_active_yes_position",
    "trajectory_support_state",
    "unrealizable_triggered",
    "trigger_type",
    "action",
    "reason_codes",
    "new_target_bucket",
    "new_target_posterior",
    "new_target_realization_reliability",
    "new_target_trajectory_support_state",
    "held_target_posterior",
    "switch_reference_bucket",
    "switch_reference_posterior",
    "new_target_posterior_lead",
    "target_unrealizable_consecutive_snapshots",
    "switch_buy_allowed",
    "new_buy_allowed_this_snapshot",
    "target_adjustment_action",
    "target_adjustment_dedupe_key",
    "target_adjustment_decision_version",
    "weather_strategy_family",
    "weather_strategy_kind",
    "weather_strategy_entry_style",
    "weather_strategy_confidence",
    "weather_strategy_positive_ev",
    "weather_strategy_ev_gate_pass",
    "weather_strategy_ev_gap",
    "weather_strategy_profit_value",
    "weather_strategy_profit_floor",
    "weather_strategy_raw_ev",
    "weather_strategy_price",
    "weather_strategy_stage",
    "weather_strategy_mode",
    "weather_strategy_edge_weight",
    "weather_strategy_settlement_weight",
    "weather_strategy_settlement_eligible",
    "weather_strategy_settlement_eligible_reason",
    "weather_strategy_entry_allowed",
    "weather_strategy_entry_block_reason",
    "weather_strategy_edge_tier",
    "weather_strategy_edge_tier_min",
    "weather_strategy_edge_tier_reason",
    "weather_strategy_edge_tier_size_ratio",
    "weather_strategy_reverse_edge",
    "weather_strategy_reverse_edge_action",
    "weather_strategy_market_overpriced_yes",
    "weather_strategy_tail_no",
    "weather_strategy_season",
    "weather_strategy_season_size_scale",
    "weather_strategy_season_size_reason",
    "weather_strategy_phase_size_scale",
    "weather_strategy_phase_size_reason",
    "weather_strategy_consensus_label",
    "weather_strategy_consensus_model",
    "weather_strategy_consensus_auto_label",
    "weather_strategy_consensus_market_label",
    "weather_strategy_consensus_model_label",
    "weather_strategy_consensus_gap",
    "weather_strategy_adjacent_risk",
    "weather_strategy_adjacent_gap",
    "weather_strategy_adjacent_open_count",
    "weather_strategy_adjacent_open_outcomes",
    "weather_strategy_adjacent_max_open_per_event",
    "weather_strategy_variance_skip",
    "weather_strategy_variance_skip_reason",
    "weather_strategy_variance_sigma",
    "weather_strategy_variance_model_spread",
    "weather_strategy_size_ratio_suggested",
    "weather_strategy_size_ratio_applied",
    "weather_strategy_size_ratio_reason",
    "weather_strategy_exit_plan",
    "weather_strategy_target_exit_price",
    "weather_strategy_rules_ok",
    "weather_strategy_rules_na_reason",
    "weather_strategy_settlement_station",
    "weather_strategy_source_kind",
    "weather_strategy_source_confidence",
    "weather_strategy_source_fit_tier",
    "weather_strategy_source_trust_tier",
    "weather_strategy_source_usable_for_settlement",
    "weather_strategy_source_station_id",
    "weather_strategy_source_station_expected",
    "weather_strategy_source_station_match",
    "weather_strategy_source_station_match_reason",
    "weather_strategy_source_na_reason",
    "weather_strategy_observation_source",
    "weather_strategy_observation_value",
    "weather_strategy_repricing_signal",
    "weather_strategy_market_data_mode",
)


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return bool(default)
    parsed = _to_bool(val)
    return bool(default) if parsed is None else bool(parsed)


def _env_float(name: str, default: float) -> float:
    val = _to_float(os.getenv(name))
    if val is None:
        return float(default)
    return float(val)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "pass", "ok"}:
        return True
    if text in {"0", "false", "no", "off", "fail", "blocked"}:
        return False
    return None


def _first_float(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> Optional[float]:
    for key in keys:
        val = _to_float(candidate.get(key))
        if val is not None:
            return val
        if context is not None:
            val = _to_float(context.get(key))
            if val is not None:
                return val
    return None


def _first_str(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> str:
    for key in keys:
        val = candidate.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
        if context is not None:
            val = context.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def _first_bool(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> Optional[bool]:
    for key in keys:
        val = _to_bool(candidate.get(key))
        if val is not None:
            return val
        if context is not None:
            val = _to_bool(context.get(key))
            if val is not None:
                return val
    return None


def _side_suffix(candidate: Mapping[str, Any]) -> str:
    return "_yes" if str(candidate.get("side") or "").strip().upper() == "YES" else "_no"


def _strategy_stage(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> str:
    stage_hint = _first_str(
        candidate,
        context,
        "weather_strategy_stage",
        "q_bayes_z_stage",
        "q_calibration_z_stage",
        "q_quote_calibration_z_stage",
        "edge_tradeability_signal_z_stage",
    ).lower()
    if stage_hint in {"d2_plus", "d1", "d0_morning", "d0_afternoon", "d0_locked"}:
        return "d0_afternoon_or_locked" if stage_hint in {"d0_afternoon", "d0_locked"} else stage_hint
    days = _first_float(candidate, context, "days_to_event", "event_days_to_event", "dte")
    if days is None:
        return "unknown"
    if days >= 2:
        return "d2_plus"
    if days >= 1:
        return "d1"
    return "d0_morning"


def _stage_weights(stage: str) -> tuple[str, float, float]:
    if stage == "d2_plus":
        return "edge_dominant", 0.75, 0.25
    if stage == "d1":
        return "balanced", 0.50, 0.50
    if stage == "d0_morning":
        return "settlement_dominant", 0.30, 0.70
    if stage == "d0_afternoon_or_locked":
        return "settlement_only", 0.0, 1.0
    return "edge_dominant", 0.70, 0.30


def _edge_tier(profit_value: Optional[float]) -> tuple[str, float, str, float]:
    if not _env_bool("PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED", True):
        return "legacy", 0.0, "strict_edge_disabled", 1.0
    normal_min = max(0.0, _env_float("PM_WEATHER_STRATEGY_NORMAL_EDGE_MIN", 0.10))
    probe_min = max(0.0, _env_float("PM_WEATHER_STRATEGY_PROBE_EDGE_MIN", 0.075))
    probe_ratio = max(0.0, _env_float("PM_WEATHER_STRATEGY_PROBE_SIZE_RATIO", 0.25))
    if profit_value is None:
        return "blocked", normal_min, "edge_lcb_missing", 0.0
    if profit_value + EPS >= normal_min:
        return "normal_trade", normal_min, "edge_lcb_meets_strict_min", 1.0
    if profit_value + EPS >= probe_min:
        return "probe", probe_min, "edge_lcb_in_probe_band", probe_ratio
    return "shadow_only", probe_min, "edge_lcb_too_thin_after_fees", 0.0


def _variance_skip(sigma: Optional[float], model_spread: Optional[float]) -> tuple[bool, str]:
    if not _env_bool("PM_WEATHER_STRATEGY_VARIANCE_SKIP_ENABLED", True):
        return False, ""
    sigma_max = max(0.0, _env_float("PM_WEATHER_STRATEGY_VARIANCE_SKIP_SIGMA_MAX", 0.08))
    spread_max = max(0.0, _env_float("PM_WEATHER_STRATEGY_VARIANCE_SKIP_MODEL_SPREAD_MAX", 0.12))
    if sigma is not None and sigma >= sigma_max:
        return True, "q_sigma_high"
    if model_spread is not None and model_spread >= spread_max:
        return True, "model_spread_high"
    return False, ""


def _event_month(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> Optional[int]:
    month = _first_float(candidate, context, "event_month", "observation_month")
    if month is not None and 1 <= int(month) <= 12:
        return int(month)
    text = _first_str(candidate, context, "event_date", "date", "market_date")
    if len(text) >= 7:
        try:
            return int(text[5:7])
        except ValueError:
            return None
    return None


def _season_scale(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> tuple[str, float, str]:
    month = _event_month(candidate, context)
    if month in {9, 10, 11}:
        return "autumn", max(0.0, _env_float("PM_WEATHER_STRATEGY_AUTUMN_SIZE_SCALE", 0.65)), "autumn_lower_size"
    if month is None:
        return "unknown", 1.0, ""
    return "normal", 1.0, ""


def _tail_no_candidate(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    side: str,
    q_entry: Optional[float],
) -> bool:
    if side != "NO":
        return False
    label = " ".join(
        filter(
            None,
            [
                _first_str(candidate, context, "outcome", "label", "market_top_label"),
                _first_str(candidate, context, "bucket_label", "condition_label"),
            ],
        )
    ).lower()
    if any(token in label for token in ("or higher", "or above", "above", "over", "or lower", "or below", "below", "under")):
        return True
    return bool(q_entry is not None and q_entry >= _env_float("PM_WEATHER_STRATEGY_TAIL_NO_Q_MIN", 0.75))


def _multiply_size_ratio_once(candidate: Dict[str, Any], flag: str, ratio: float) -> None:
    if bool(candidate.get(flag)):
        return
    current_extra = _to_float(candidate.get("strategy_size_ratio_extra"))
    if current_extra is None:
        current_extra = 1.0
    candidate["strategy_size_ratio_extra"] = max(0.0, float(current_extra) * max(0.0, float(ratio)))
    candidate[flag] = True


def classify_weather_strategy_candidate(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    side = str(candidate.get("side") or "").strip().upper()
    suffix = _side_suffix(candidate)
    price = _first_float(candidate, context, "price", "order_price", "ask", "best_ask")
    if side == "NO":
        price = _first_float(candidate, context, "price", "order_price", "ask", "best_ask_no")

    q_entry = _first_float(
        candidate,
        context,
        "edge_tradeability_bayes_q",
        f"edge_tradeability_bayes_q{suffix}",
        "q_entry",
        f"q_entry{suffix}",
        "q_temperature_bucket_posterior",
        f"q_temperature_bucket_posterior{suffix}",
        "q_bayes_posterior",
        f"q_bayes_posterior{suffix}",
        "q_bayes_lcb",
        f"q_bayes_lcb{suffix}",
        "q_min_bound",
        "q_edge",
        "q_quote",
        f"q_quote{suffix}",
        "q_calibrated",
        f"q_calibrated{suffix}",
    )
    raw_ev = None
    if q_entry is not None and price is not None:
        raw_ev = float(q_entry) - float(price)

    dead_entry_ask_floor = _env_float(
        "PM_WEATHER_STRATEGY_MIN_ENTRY_ASK",
        _env_float("PM_WEATHER_STRATEGY_MIN_YES_ENTRY_ASK", 0.01),
    )
    dead_entry_ask_blocked = False
    if price is not None and math.isfinite(float(price)):
        dead_entry_ask_blocked = float(price) <= float(dead_entry_ask_floor) + EPS

    profit_value = _first_float(
        candidate,
        context,
        "edge_tradeability_profit_value",
        "edge_tradeability_edge_lcb",
        "exec_edge_entry",
        "edge_realizable",
        "entry_usable_edge",
        f"edge_tradeability_profit_value{suffix}",
        f"edge_tradeability_edge_lcb{suffix}",
    )
    profit_floor = _first_float(
        candidate,
        context,
        "edge_tradeability_profit_min",
        "edge_tradeability_edge_lcb_min",
        "lcb_global_edge_lcb_min",
        f"edge_tradeability_profit_min{suffix}",
        f"edge_tradeability_edge_lcb_min{suffix}",
    )
    if profit_floor is None:
        profit_floor = 0.0
    profit_floor = max(0.0, float(profit_floor))
    positive_ev = bool(profit_value is not None and profit_value > EPS)
    if profit_floor <= EPS:
        ev_gate_pass = positive_ev
    else:
        ev_gate_pass = bool(profit_value is not None and profit_value + EPS >= profit_floor)
    ev_gap = float(profit_value) - profit_floor if profit_value is not None else None

    auto_label = _first_str(candidate, context, "lcb_consensus_yes_top_auto_label")
    market_label = _first_str(
        candidate,
        context,
        "lcb_consensus_yes_top_market_label",
        "market_top_label",
    )
    model_label = _first_str(candidate, context, "lcb_consensus_yes_top_model_label", "lcb_consensus_yes_top_ecmwf_label")
    consensus_matched = bool(
        side == "YES"
        and auto_label
        and market_label
        and model_label
        and auto_label == market_label == model_label
    )
    if not consensus_matched:
        consensus_matched = bool(
            side == "YES"
            and _first_bool(candidate, context, "lcb_consensus_yes_top_matched") is True
        )
    consensus_label = auto_label if consensus_matched else ""

    auto_q = _first_float(candidate, context, "lcb_consensus_yes_top_auto_q")
    auto_second_q = _first_float(candidate, context, "lcb_consensus_yes_top_auto_second_q")
    consensus_gap = None
    adjacent_risk = False
    if auto_q is not None and auto_second_q is not None:
        consensus_gap = float(auto_q) - float(auto_second_q)
        adjacent_risk = consensus_gap < 0.08
    sigma = _first_float(
        candidate,
        context,
        "q_calibrated_sigma",
        "q_quote_sigma",
        "q_bayes_sigma",
        f"q_calibrated_sigma{suffix}",
        f"q_quote_sigma{suffix}",
        f"q_bayes_sigma{suffix}",
    )
    if sigma is not None and sigma >= 0.06:
        adjacent_risk = True
    model_spread = _first_float(candidate, context, "q_round_model_spread")
    if model_spread is not None and model_spread >= 0.08:
        adjacent_risk = True
    variance_skip, variance_skip_reason = _variance_skip(sigma, model_spread)

    spread = _first_float(candidate, context, "spread", f"spread{suffix}")
    depth = _first_float(
        candidate,
        context,
        "depth_usdc",
        "lcb_global_depth_usdc",
        "ask_depth_total",
        "ask_depth_topN",
        "best_ask_size",
        f"ask_depth_total{suffix}",
        f"ask_depth_topN{suffix}",
        f"best_ask_size{suffix}",
    )
    low_liquidity = bool(
        (spread is not None and spread >= 0.03)
        or (depth is not None and depth > 0 and depth < 100.0)
    )

    tradeability_pass = _first_bool(candidate, context, "edge_tradeability_pass", f"edge_tradeability_pass{suffix}")
    high_confidence = bool(consensus_matched and positive_ev and tradeability_pass is True)
    if high_confidence:
        kind = "high_confidence_consensus"
        confidence = "high"
    elif positive_ev and low_liquidity:
        kind = "repricing_maker"
        confidence = "medium"
    elif adjacent_risk:
        kind = "adjacent_bucket_management"
        confidence = "watch"
    else:
        kind = "standard_edge"
        confidence = "medium" if positive_ev else "low"

    target_exit = _first_float(
        candidate,
        context,
        "edge_realizable_target_exit_price",
        "target_exit_price",
        f"edge_realizable_target_exit_price{suffix}",
    )
    exit_plan = "repricing_exit_target" if target_exit is not None else "hold_only_while_tradeability_persists"

    rules_ok = _first_bool(
        candidate,
        context,
        "rules_contract_ok",
        "settlement_rules_ok",
        "contract_rules_ok",
        "weather_strategy_rules_ok",
    )
    rules_missing_text = _first_str(
        candidate,
        context,
        "rules_na_reason",
        "rules_missing_reasons",
        "weather_strategy_rules_na_reason",
    )
    if rules_ok is None:
        rules_na_reason = rules_missing_text or "rules_contract_status_missing"
    elif rules_ok is False:
        rules_na_reason = rules_missing_text or "rules_contract_not_verified"
    else:
        rules_na_reason = ""
    station = _first_str(
        candidate,
        context,
        "settlement_station",
        "weather_strategy_settlement_station",
        "weather_station",
        "station_id",
        "resolution_station",
        "contract_station",
    )
    source_kind = _first_str(
        candidate,
        context,
        "source_kind",
        "contract_source_kind",
        "observed_nowcast_source_kind",
        "weather_strategy_source_kind",
    )
    source_confidence = _first_float(
        candidate,
        context,
        "source_confidence",
        "contract_source_confidence",
        "observed_nowcast_source_confidence",
        "weather_strategy_source_confidence",
    )
    source_fit_tier = _first_str(
        candidate,
        context,
        "source_fit_tier",
        "contract_source_fit_tier",
        "observed_nowcast_source_fit_tier",
        "weather_strategy_source_fit_tier",
        "source_trust_tier",
        "contract_source_trust_tier",
        "observed_nowcast_source_trust_tier",
        "weather_strategy_source_trust_tier",
    )
    source_usable = _first_bool(
        candidate,
        context,
        "source_usable_for_settlement",
        "contract_source_usable_for_settlement",
        "observed_nowcast_source_usable",
        "weather_strategy_source_usable_for_settlement",
    )
    source_station_id = _first_str(
        candidate,
        context,
        "source_station_id",
        "contract_source_station_id",
        "observed_nowcast_source_station_id",
        "weather_strategy_source_station_id",
    )
    source_station_expected = _first_str(
        candidate,
        context,
        "source_station_expected",
        "contract_source_station_expected",
        "weather_strategy_source_station_expected",
    )
    source_station_match = _first_bool(
        candidate,
        context,
        "source_station_match",
        "contract_source_station_match",
        "weather_strategy_source_station_match",
    )
    source_station_match_reason = _first_str(
        candidate,
        context,
        "source_station_match_reason",
        "contract_source_station_match_reason",
        "weather_strategy_source_station_match_reason",
    )
    source_na_reason = _first_str(
        candidate,
        context,
        "source_na_reason",
        "contract_source_na_reason",
        "observed_nowcast_source_na_reason",
        "weather_strategy_source_na_reason",
    )
    observation_source = _first_str(candidate, context, "observed_nowcast_source")
    observation_value = _first_float(candidate, context, "observed_nowcast_value")

    if source_usable is False:
        rules_ok = False
        rules_na_reason = source_na_reason or "resolution_source_untrusted"
        high_confidence = False
    if source_station_match is False:
        rules_ok = False
        rules_na_reason = source_station_match_reason or source_na_reason or "resolution_station_mismatch"
        high_confidence = False

    settlement_eligible = bool(rules_ok is True and source_usable is True and station)
    if settlement_eligible:
        settlement_eligible_reason = "rules_station_source_verified"
    elif rules_ok is False:
        settlement_eligible_reason = rules_na_reason or "rules_not_verified"
    elif source_usable is False:
        settlement_eligible_reason = source_na_reason or "resolution_source_untrusted"
    elif not station:
        settlement_eligible_reason = "settlement_station_missing"
    else:
        settlement_eligible_reason = "settlement_source_status_missing"

    stage = _strategy_stage(candidate, context)
    mode, edge_weight, settlement_weight = _stage_weights(stage)
    edge_tier, edge_tier_min, edge_tier_reason, edge_tier_size_ratio = _edge_tier(profit_value)
    reverse_edge = bool(side == "NO" and positive_ev)
    tail_no = _tail_no_candidate(candidate, context, side, q_entry)
    season, season_size_scale, season_size_reason = _season_scale(candidate, context)

    repricing_signal = 0.0
    if profit_value is not None and profit_value > 0:
        repricing_signal += float(profit_value)
    elif raw_ev is not None and raw_ev > 0:
        repricing_signal += float(raw_ev)
    if positive_ev and low_liquidity:
        repricing_signal += 0.005
    if consensus_matched:
        repricing_signal += 0.003
    if (
        observation_value is not None
        and source_usable is True
        and source_confidence is not None
        and source_confidence >= 0.8
    ):
        repricing_signal += 0.005

    if high_confidence:
        kind = "high_confidence_consensus"
        confidence = "high"
    elif positive_ev and low_liquidity and repricing_signal > EPS:
        kind = "repricing_maker"
        confidence = "medium"
    elif adjacent_risk:
        kind = "adjacent_bucket_management"
        confidence = "watch"
    else:
        kind = "standard_edge"
        confidence = "medium" if positive_ev else "low"

    if kind in {"high_confidence_consensus", "repricing_maker"}:
        entry_style = "maker_repricing"
    elif positive_ev:
        entry_style = "maker_probe"
    else:
        entry_style = "observe"

    size_ratio_suggested = 0.5 if adjacent_risk else 1.0
    size_ratio_reason = "adjacent_bucket_risk" if adjacent_risk else ""
    entry_allowed = bool(entry_style != "observe")
    entry_block_reason = ""

    if reverse_edge:
        kind = "tail_no_mispricing" if tail_no else "reverse_edge_no"
        confidence = "high" if tail_no and edge_tier == "normal_trade" else confidence
        entry_style = "maker_no_reverse_edge"

    if dead_entry_ask_blocked:
        entry_allowed = False
        entry_style = "observe"
        entry_block_reason = "entry_ask_too_low_for_entry"

    if mode == "settlement_only" and not settlement_eligible:
        entry_allowed = False
        entry_style = "observe"
        entry_block_reason = "d0_locked_no_new_edge_without_settlement_source"
    elif variance_skip:
        entry_allowed = False
        entry_style = "observe"
        entry_block_reason = "variance_high"
    elif edge_tier in {"blocked", "shadow_only"}:
        entry_allowed = False
        entry_style = "observe"
        entry_block_reason = edge_tier_reason

    if mode == "settlement_only" and settlement_eligible:
        kind = "settlement_terminal"
        confidence = "high" if positive_ev else "medium"
        entry_style = "settlement_hold" if positive_ev else "observe"
        entry_allowed = bool(positive_ev and edge_tier not in {"blocked", "shadow_only"})
        if not entry_allowed and not entry_block_reason:
            entry_block_reason = edge_tier_reason if edge_tier in {"blocked", "shadow_only"} else "settlement_terminal_no_positive_ev"

    phase_size_scale = edge_weight
    phase_size_reason = f"{mode}_resource_weight"
    if entry_style == "settlement_hold":
        phase_size_scale = settlement_weight
    if not _env_bool("PM_WEATHER_STRATEGY_STAGE_SIZE_ENABLED", True):
        phase_size_scale = 1.0
        phase_size_reason = "stage_size_disabled"

    posterior_profile = classify_posterior_settlement_candidate(candidate, context)
    posterior_allowed = bool(posterior_profile.get("posterior_settlement_entry_allowed"))
    posterior_block_reason = str(
        posterior_profile.get("posterior_settlement_entry_block_reason") or ""
    )
    target_adjustment_action = _first_str(
        candidate,
        context,
        "target_adjustment_action",
        "posterior_settlement_adjustment_action",
        "action",
    )
    has_active_yes_position = _first_bool(
        candidate,
        context,
        "has_active_yes_position",
        "posterior_settlement_has_active_yes_position",
    )
    if has_active_yes_position is None:
        has_active_yes_position = False
    candidate_bucket = _first_str(
        candidate,
        context,
        "posterior_settlement_candidate_bucket",
        "bucket_label",
        "outcome",
    ).lower()
    held_bucket = _first_str(
        candidate,
        context,
        "held_target_bucket",
        "posterior_settlement_held_target_bucket",
        "current_position_target_bucket",
    ).lower()
    same_as_held_target = bool(candidate_bucket and held_bucket and candidate_bucket == held_bucket)
    if bool(has_active_yes_position) and side == "YES":
        if target_adjustment_action == ACTION_SWITCH_BUY:
            posterior_allowed = bool(
                posterior_allowed
                and _first_bool(candidate, context, "switch_buy_allowed") is not False
            )
            if not posterior_allowed and not posterior_block_reason:
                posterior_block_reason = "switch_buy_gate_blocked"
        elif same_as_held_target:
            posterior_allowed = False
            posterior_block_reason = "target_adjustment_hold_no_rebalance"
        elif target_adjustment_action:
            posterior_allowed = False
            posterior_block_reason = f"target_adjustment_action_{target_adjustment_action.lower()}"
    if dead_entry_ask_blocked:
        posterior_allowed = False
        posterior_block_reason = "entry_ask_too_low_for_entry"
    elif variance_skip:
        posterior_allowed = False
        posterior_block_reason = "variance_high"
    elif mode == "settlement_only" and not settlement_eligible:
        posterior_allowed = False
        posterior_block_reason = "d0_locked_no_new_entry_without_settlement_source"
    posterior_lane = str(posterior_profile.get("strategy_lane") or LANE_OBSERVE)
    posterior_entry_style = (
        "settlement_yes_target"
        if posterior_lane == LANE_YES_TARGET
        else "settlement_no_remote"
        if posterior_lane == LANE_NO_REMOTE
        else "observe"
    )

    profile = {
        "weather_strategy_family": POSTERIOR_SETTLEMENT_FAMILY,
        "weather_strategy_kind": kind,
        "weather_strategy_entry_style": posterior_entry_style,
        "weather_strategy_confidence": confidence,
        "weather_strategy_positive_ev": positive_ev,
        "weather_strategy_ev_gate_pass": ev_gate_pass,
        "weather_strategy_ev_gap": ev_gap,
        "weather_strategy_profit_value": profit_value,
        "weather_strategy_profit_floor": profit_floor,
        "weather_strategy_raw_ev": raw_ev,
        "weather_strategy_price": price,
        "weather_strategy_stage": stage,
        "weather_strategy_mode": mode,
        "weather_strategy_edge_weight": edge_weight,
        "weather_strategy_settlement_weight": settlement_weight,
        "weather_strategy_settlement_eligible": settlement_eligible,
        "weather_strategy_settlement_eligible_reason": settlement_eligible_reason,
        "weather_strategy_entry_allowed": bool(posterior_allowed),
        "weather_strategy_entry_block_reason": posterior_block_reason or entry_block_reason,
        "weather_strategy_edge_tier": "diagnostic_only_not_entry_gate",
        "weather_strategy_edge_tier_min": edge_tier_min,
        "weather_strategy_edge_tier_reason": edge_tier_reason,
        "weather_strategy_edge_tier_size_ratio": 1.0,
        "weather_strategy_reverse_edge": reverse_edge,
        "weather_strategy_reverse_edge_action": "buy_no_equivalent_to_sell_yes" if reverse_edge else "",
        "weather_strategy_market_overpriced_yes": reverse_edge,
        "weather_strategy_tail_no": tail_no,
        "weather_strategy_season": season,
        "weather_strategy_season_size_scale": season_size_scale,
        "weather_strategy_season_size_reason": season_size_reason,
        "weather_strategy_phase_size_scale": phase_size_scale,
        "weather_strategy_phase_size_reason": phase_size_reason,
        "weather_strategy_consensus_label": consensus_label,
        "weather_strategy_consensus_model": _first_str(candidate, context, "lcb_consensus_yes_top_model"),
        "weather_strategy_consensus_auto_label": auto_label,
        "weather_strategy_consensus_market_label": market_label,
        "weather_strategy_consensus_model_label": model_label,
        "weather_strategy_consensus_gap": consensus_gap,
        "weather_strategy_adjacent_risk": adjacent_risk,
        "weather_strategy_adjacent_gap": consensus_gap,
        "weather_strategy_variance_skip": variance_skip,
        "weather_strategy_variance_skip_reason": variance_skip_reason,
        "weather_strategy_variance_sigma": sigma,
        "weather_strategy_variance_model_spread": model_spread,
        "weather_strategy_size_ratio_suggested": size_ratio_suggested,
        "weather_strategy_size_ratio_applied": False,
        "weather_strategy_size_ratio_reason": size_ratio_reason,
        "weather_strategy_exit_plan": "hold_to_settlement",
        "weather_strategy_target_exit_price": None,
        "weather_strategy_rules_ok": rules_ok,
        "weather_strategy_rules_na_reason": rules_na_reason,
        "weather_strategy_settlement_station": station,
        "source_fit_tier": source_fit_tier,
        "source_trust_tier": source_fit_tier,
        "weather_strategy_source_kind": source_kind,
        "weather_strategy_source_confidence": source_confidence,
        "weather_strategy_source_fit_tier": source_fit_tier,
        "weather_strategy_source_trust_tier": source_fit_tier,
        "weather_strategy_source_usable_for_settlement": source_usable,
        "weather_strategy_source_station_id": source_station_id,
        "weather_strategy_source_station_expected": source_station_expected,
        "weather_strategy_source_station_match": source_station_match,
        "weather_strategy_source_station_match_reason": source_station_match_reason,
        "weather_strategy_source_na_reason": source_na_reason,
        "weather_strategy_observation_source": observation_source,
        "weather_strategy_observation_value": observation_value,
        "weather_strategy_repricing_signal": repricing_signal,
        "weather_strategy_market_data_mode": "websocket_preferred",
    }
    profile.update(posterior_profile)
    return profile


def apply_weather_strategy_profile(
    candidate: Dict[str, Any],
    context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    adjacent_ratio_already_applied = bool(candidate.get("weather_strategy_size_ratio_applied"))
    profile = classify_weather_strategy_candidate(candidate, context)
    candidate.update(profile)
    candidate.setdefault("order_type", "GTC")

    if str(profile.get("strategy_family") or "").strip() == POSTERIOR_SETTLEMENT_FAMILY:
        candidate["strategy_variant"] = POSTERIOR_SETTLEMENT_FAMILY
        if profile.get("posterior_settlement_entry_allowed"):
            priority = _to_float(candidate.get("priority"))
            if priority is None or priority < 1300:
                candidate["priority"] = 1300

    kind = str(profile.get("weather_strategy_kind") or "")
    if kind == "high_confidence_consensus":
        if not str(candidate.get("strategy_variant") or "").strip():
            candidate["strategy_variant"] = "consensus_yes_top"
        priority = _to_float(candidate.get("priority"))
        if priority is None or priority < 1200:
            candidate["priority"] = 1200
    elif kind == "repricing_maker":
        if not str(candidate.get("strategy_variant") or "").strip():
            candidate["strategy_variant"] = "repricing_maker"

    if bool(profile.get("weather_strategy_adjacent_risk")) and not adjacent_ratio_already_applied:
        current_extra = _to_float(candidate.get("strategy_size_ratio_extra"))
        if current_extra is None:
            current_extra = 1.0
        suggested = _to_float(profile.get("weather_strategy_size_ratio_suggested"))
        if suggested is None:
            suggested = 1.0
        candidate["strategy_size_ratio_extra"] = max(0.0, float(current_extra) * float(suggested))
        candidate["weather_strategy_size_ratio_applied"] = True
    elif adjacent_ratio_already_applied and bool(profile.get("weather_strategy_adjacent_risk")):
        candidate["weather_strategy_size_ratio_applied"] = True

    _multiply_size_ratio_once(
        candidate,
        "weather_strategy_edge_tier_size_applied",
        _to_float(profile.get("weather_strategy_edge_tier_size_ratio")) or 0.0,
    )
    _multiply_size_ratio_once(
        candidate,
        "weather_strategy_phase_size_applied",
        _to_float(profile.get("weather_strategy_phase_size_scale")) or 0.0,
    )
    _multiply_size_ratio_once(
        candidate,
        "weather_strategy_season_size_applied",
        _to_float(profile.get("weather_strategy_season_size_scale")) or 0.0,
    )
    if profile.get("weather_strategy_entry_allowed") is False:
        _multiply_size_ratio_once(candidate, "weather_strategy_entry_block_size_applied", 0.0)

    return profile
