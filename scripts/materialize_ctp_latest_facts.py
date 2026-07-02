#!/usr/bin/env python3
"""Materialize CTP v1 posterior distributions onto latest temperature facts.

Default mode is read-only. Use --apply only after reviewing the reported impact:
it appends new event_fact_history rows and repoints event_fact_latest.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"

import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from bayesian_likelihood_runtime import estimate_peak_temperature_from_features  # noqa: E402
from chengdu_intraday_summary import _physical_weather_modifier  # noqa: E402
from temperature_posterior_backtest import _ctp_probs, _posterior_temperature_distribution  # noqa: E402


REQUIRED_POSTERIOR_KEYS = {
    "grid",
    "cdf_values",
    "quantiles_c",
    "quantile_grid",
    "bucket_probabilities",
}

SUMMARY_CACHE_VERSION = "event_fact_summary_cache_v6_awc_raw_observation_compact"

ROW_PROBABILITY_FIELDS = (
    "q_bayes_posterior",
    "q_temperature_bucket_posterior",
    "q_contract_payout_posterior",
    "q_entry",
    "q_raw",
    "q_round",
    "q_calibrated",
    "q_quote",
    "target_bucket_probability",
)

BLOCK_ROW_PROBABILITY_FIELDS = (
    "posterior",
    "probability",
    "q_bayes_posterior",
    "q_temperature_bucket_posterior",
    "q_contract_payout_posterior",
    "q_entry",
    "q_quote",
    "q_calibrated",
    "target_bucket_probability",
)

LEGACY_INTERVAL_PROBABILITY_FIELDS = (
    "q_bayes_lcb",
    "q_bayes_ucb",
    "q_bayes_lcb_no",
    "q_bayes_ucb_no",
    "q_temperature_bucket_lcb",
    "q_temperature_bucket_ucb",
    "q_temperature_bucket_lcb_no",
    "q_temperature_bucket_ucb_no",
    "q_contract_payout_lcb",
    "q_contract_payout_ucb",
    "q_contract_payout_lcb_no",
    "q_contract_payout_ucb_no",
    "q_calibrated_lcb",
    "q_calibrated_ucb",
    "q_calibrated_lcb_no",
    "q_calibrated_ucb_no",
    "q_quote_lcb",
    "q_quote_ucb",
    "q_quote_lcb_no",
    "q_quote_ucb_no",
)


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _posterior_complete(payload: Mapping[str, Any]) -> bool:
    posterior = payload.get("posterior_temperature_distribution")
    return isinstance(posterior, Mapping) and REQUIRED_POSTERIOR_KEYS.issubset(posterior.keys())


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _bucket_label(value: Mapping[str, Any]) -> str:
    return str(
        value.get("outcome")
        or value.get("bucket")
        or value.get("label")
        or value.get("target_bucket")
        or ""
    ).strip()


def _set_probability_field(
    row: Dict[str, Any],
    key: str,
    prob: float,
    *,
    preserve_suffix: str = "_pre_ctp_materialization",
) -> None:
    old = _to_float(row.get(key))
    if old is not None:
        old_norm = float(old) / 100.0 if float(old) > 1.0 else float(old)
        if abs(old_norm - float(prob)) > 1e-12:
            row.setdefault(f"{key}{preserve_suffix}", old_norm)
    row[key] = float(prob)
    row[f"{key}_pct"] = float(prob) * 100.0


def _clear_legacy_interval_probability_fields(row: Dict[str, Any]) -> None:
    """CTP materialized facts use posterior as SSOT; interval aliases are legacy diagnostics."""
    removed = False
    for key in LEGACY_INTERVAL_PROBABILITY_FIELDS:
        if key in row:
            row.pop(key, None)
            removed = True
        pct_key = f"{key}_pct"
        if pct_key in row:
            row.pop(pct_key, None)
            removed = True
    if removed:
        row["legacy_interval_probability_fields_cleared_by_ctp_materializer"] = True


def _sync_ctp_observed_constraint_aliases(row: Dict[str, Any], prob: float) -> None:
    has_observed_alias = any(
        key in row
        for key in (
            "q_posterior_observed_constraints_applied",
            "q_posterior_observed_constraints_post_prob",
            "q_posterior_observed_constraints",
            "q_posterior_observed_constraints_hard_zero",
            "posterior_settlement_candidate_bucket_hard_zero",
        )
    )
    if not has_observed_alias:
        return
    posterior = max(0.0, min(1.0, float(prob)))
    hard_zero = posterior <= 1e-12 and bool(
        row.get("q_posterior_observed_constraints_hard_zero")
        or row.get("posterior_settlement_candidate_bucket_hard_zero")
    )
    row["q_posterior_observed_constraints_post_prob"] = 0.0 if hard_zero else posterior
    if "q_posterior_observed_constraints" in row:
        row["q_posterior_observed_constraints"] = 0.0 if hard_zero else posterior
    row["q_posterior_observed_constraints_hard_zero"] = bool(hard_zero)
    row["posterior_settlement_candidate_bucket_hard_zero"] = bool(hard_zero)
    if hard_zero:
        return
    for key in (
        "q_posterior_observed_constraints_reason_codes",
        "q_posterior_observed_constraints_hard_zero_reasons",
        "posterior_settlement_candidate_bucket_hard_zero_reasons",
    ):
        if key in row:
            row[key] = []


def _sync_distribution_row_probability(
    row: Dict[str, Any],
    *,
    label: str,
    prob: float,
    posterior: Mapping[str, Any],
) -> None:
    for key in ROW_PROBABILITY_FIELDS:
        if key in row or key in {
            "q_bayes_posterior",
            "q_temperature_bucket_posterior",
            "target_bucket_probability",
        }:
            _set_probability_field(row, key, prob)
    _clear_legacy_interval_probability_fields(row)
    _sync_ctp_observed_constraint_aliases(row, prob)
    no_prob = 1.0 - float(prob)
    for key in (
        "q_bayes_posterior_no",
        "q_temperature_bucket_posterior_no",
        "q_contract_payout_posterior_no",
        "q_entry_no",
        "q_raw_no",
        "q_round_no",
        "q_calibrated_no",
        "q_quote_no",
        "target_bucket_probability_no",
    ):
        if key in row:
            _set_probability_field(row, key, no_prob)
    row["target_bucket_probability_field"] = "posterior_temperature_distribution.bucket_probabilities"
    row["posterior_field"] = "posterior_temperature_distribution.bucket_probabilities"
    row["posterior_engine"] = str(posterior.get("version") or "continuous_temperature_posterior_v1")
    row["posterior_ssot_write_aligned"] = True
    row["posterior_ssot_write_source"] = "continuous_temperature_posterior_v1_materialized_latest"
    row["q_temperature_bucket_label"] = label


def _sync_row_like_block_probability(
    block: Dict[str, Any],
    probs: Mapping[str, float],
) -> None:
    label = _bucket_label(block)
    if not label or label not in probs:
        return
    yes_prob = max(0.0, min(1.0, float(probs[label])))
    side = str(block.get("side") or "").strip().upper()
    q_prob = 1.0 - yes_prob if side == "NO" else yes_prob
    for key in BLOCK_ROW_PROBABILITY_FIELDS:
        if key not in block:
            continue
        if key in {"posterior", "probability", "target_bucket_probability"}:
            _set_probability_field(block, key, yes_prob)
        else:
            _set_probability_field(block, key, q_prob)
    if side == "NO":
        for key in ("prob_for_size", "remote_no_sizing_probability"):
            if key in block:
                block[key] = 1.0 - yes_prob
    elif "prob_for_size" in block:
        block["prob_for_size"] = yes_prob
    if "posterior_field" in block:
        block["posterior_field"] = "posterior_temperature_distribution.bucket_probabilities"
    _clear_legacy_interval_probability_fields(block)
    _sync_ctp_observed_constraint_aliases(block, yes_prob)


def _sync_target_container_probability(
    block: Dict[str, Any],
    probs: Mapping[str, float],
    ranked: List[Tuple[str, float]],
) -> None:
    target = str(block.get("target_bucket") or block.get("ssot_target_bucket") or "").strip()
    if target and target in probs:
        prob = max(0.0, min(1.0, float(probs[target])))
        for key in (
            "target_probability",
            "target_posterior",
            "target_bucket_probability",
            "target_bucket_posterior",
            "posterior_settlement_target_posterior",
            "new_target_posterior",
            "current_top_posterior",
            "q_bayes_posterior",
            "q_temperature_bucket_posterior",
            "q_entry",
            "q_quote",
        ):
            if key in block:
                _set_probability_field(block, key, prob)
        for key in (
            "target_probability_pct",
            "target_bucket_probability_pct",
            "target_bucket_posterior_pct",
            "posterior_highest_probability_pct",
            "target_final_probability_pct",
            "final_bucket_probability_pct",
            "posterior_final_bucket_probability_pct",
            "cumulative_posterior_final_bucket_probability_pct",
        ):
            if key in block:
                block[key] = prob * 100.0
    second = str(block.get("second_target_bucket") or "").strip()
    if not second and target:
        for label, prob in ranked:
            if label != target:
                second = label
                block["second_target_bucket"] = label
                break
    if second and second in probs:
        prob = max(0.0, min(1.0, float(probs[second])))
        for key in (
            "second_target_probability",
            "second_target_posterior",
            "second_target_bucket_posterior",
        ):
            if key in block:
                _set_probability_field(block, key, prob)
        for key in (
            "second_target_probability_pct",
            "second_target_posterior_pct",
            "second_target_bucket_posterior_pct",
        ):
            if key in block:
                block[key] = prob * 100.0


def _walk_sync_probability_blocks(value: Any, probs: Mapping[str, float], ranked: List[Tuple[str, float]]) -> None:
    if isinstance(value, dict):
        _sync_row_like_block_probability(value, probs)
        _sync_target_container_probability(value, probs, ranked)
        for child in list(value.values()):
            _walk_sync_probability_blocks(child, probs, ranked)
    elif isinstance(value, list):
        for child in value:
            _walk_sync_probability_blocks(child, probs, ranked)


def _rescale_capital_plan_from_probabilities(plan: Dict[str, Any], probs: Mapping[str, float]) -> None:
    legs = plan.get("legs")
    if not isinstance(legs, list):
        return
    yes_pool = _to_float(plan.get("yes_pool_usdc")) or 0.0
    no_pool = _to_float(plan.get("no_pool_usdc")) or 0.0
    kelly_fraction = _to_float(plan.get("kelly_fraction"))
    if kelly_fraction is None:
        kelly_fraction = 1.0
    yes_cap = _to_float(plan.get("yes_single_leg_max_notional_usdc")) or 20.0
    env_no_cap = _to_float(os.environ.get("PM_TARGET_REALIZATION_REMOTE_NO_MAX_NOTIONAL_USDC"))
    if env_no_cap is not None and env_no_cap > 0:
        no_cap = float(env_no_cap)
    else:
        no_cap = max(100.0, float(_to_float(plan.get("remote_no_single_leg_max_notional_usdc")) or 0.0))
    plan["remote_no_single_leg_max_notional_usdc"] = no_cap
    target_bucket = str(plan.get("target_bucket") or "").strip()
    second_bucket = str(plan.get("second_target_bucket") or "").strip()
    target_prob = max(0.0, min(1.0, float(probs.get(target_bucket, 0.0)))) if target_bucket in probs else None
    second_prob = max(0.0, min(1.0, float(probs.get(second_bucket, 0.0)))) if second_bucket in probs else None
    dual_gap_pp = None
    dual_max_gap_pp = _to_float(plan.get("yes_dual_head_max_gap_pp"))
    if dual_max_gap_pp is None:
        dual_max_gap_pp = _to_float(os.environ.get("PM_TARGET_REALIZATION_DUAL_YES_MAX_GAP_PP"))
    if dual_max_gap_pp is None:
        dual_max_gap_pp = 8.0
    if target_prob is not None and second_prob is not None:
        dual_gap_pp = abs(float(target_prob) - float(second_prob)) * 100.0
    dual_active = bool(
        second_bucket
        and dual_gap_pp is not None
        and dual_gap_pp <= float(dual_max_gap_pp) + 1e-9
    )
    plan["yes_dual_head_active"] = dual_active
    plan["yes_dual_head_gap_pp"] = dual_gap_pp
    plan["yes_dual_head_max_gap_pp"] = float(dual_max_gap_pp)
    remote_indices: List[int] = []
    remote_weight_sum = 0.0
    for idx, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue
        label = _bucket_label(leg)
        if label not in probs:
            continue
        prob = max(0.0, min(1.0, float(probs[label])))
        side = str(leg.get("side") or "").strip().upper()
        role = str(leg.get("role") or "").strip()
        reliability = _to_float(leg.get("target_realization_reliability"))
        if reliability is None:
            reliability = 1.0
        price = _to_float(leg.get("price"))
        current_notional = _to_float(leg.get("current_notional")) or 0.0
        if role == "remote_no" and side == "NO":
            remote_indices.append(idx)
            remote_weight_sum += max(0.0, min(1.0, 1.0 - prob))
            continue
        if side != "YES" or role not in {"target_yes", "second_target_yes"}:
            continue
        if role == "second_target_yes" and not dual_active:
            leg["yes_dual_head_active"] = False
            leg["yes_dual_head_gap_pp"] = dual_gap_pp
            leg["yes_dual_head_max_gap_pp"] = float(dual_max_gap_pp)
            leg["block_reason"] = "second_target_gap_too_wide"
            leg["target_notional_before_caps"] = 0.0
            leg["target_notional"] = 0.0
            leg["delta_notional"] = -current_notional
            if price is not None and price > 1e-9:
                leg["target_size"] = 0.0
                leg["delta_size"] = -current_notional / price
            leg["action"] = "over_target" if current_notional > 0.50 else "hold"
            continue
        target_notional_before_caps = max(0.0, yes_pool * float(kelly_fraction) * prob * float(reliability))
        target_notional = min(target_notional_before_caps, yes_cap) if yes_cap > 0 else target_notional_before_caps
        delta = target_notional - current_notional
        leg["target_notional_before_caps"] = target_notional_before_caps
        leg["target_notional_cap_usdc"] = yes_cap
        leg["target_notional_cap_applied"] = bool(yes_cap > 0 and target_notional_before_caps > yes_cap + 1e-9)
        leg["target_notional"] = target_notional
        leg["delta_notional"] = delta
        leg["yes_dual_head_active"] = dual_active if role == "second_target_yes" else leg.get("yes_dual_head_active")
        leg["yes_dual_head_gap_pp"] = dual_gap_pp if role in {"target_yes", "second_target_yes"} else leg.get("yes_dual_head_gap_pp")
        leg["yes_dual_head_max_gap_pp"] = float(dual_max_gap_pp) if role in {"target_yes", "second_target_yes"} else leg.get("yes_dual_head_max_gap_pp")
        if price is not None and price > 1e-9:
            leg["target_size"] = target_notional / price
            leg["delta_size"] = delta / price
        leg["action"] = "add" if delta > 0.50 else "over_target" if delta < -0.50 else "hold"
    if not remote_indices or remote_weight_sum <= 1e-12:
        return
    remote_pool = max(0.0, no_pool * float(kelly_fraction))
    plan["remote_no_sizing_pool_usdc"] = remote_pool
    for idx in remote_indices:
        leg = legs[idx]
        if not isinstance(leg, dict):
            continue
        label = _bucket_label(leg)
        prob = max(0.0, min(1.0, float(probs.get(label, 0.0))))
        weight = max(0.0, min(1.0, 1.0 - prob))
        price = _to_float(leg.get("price"))
        current_notional = _to_float(leg.get("current_notional")) or 0.0
        target_notional_before_caps = remote_pool * weight / remote_weight_sum
        target_notional = min(target_notional_before_caps, no_cap) if no_cap > 0 else target_notional_before_caps
        delta = target_notional - current_notional
        leg["remote_no_sizing_mode"] = "ring_probability_weighted_by_no_posterior"
        leg["remote_no_sizing_weight"] = weight
        leg["remote_no_sizing_weight_sum"] = remote_weight_sum
        leg["remote_no_sizing_pool_usdc"] = remote_pool
        leg["target_notional_before_caps"] = target_notional_before_caps
        leg["target_notional_cap_usdc"] = no_cap
        leg["target_notional_cap_applied"] = bool(no_cap > 0 and target_notional_before_caps > no_cap + 1e-9)
        leg["target_notional"] = target_notional
        leg["delta_notional"] = delta
        if price is not None and price > 1e-9:
            leg["target_size"] = target_notional / price
            leg["delta_size"] = delta / price
        leg["action"] = "add" if delta > 0.50 else "over_target" if delta < -0.50 else "hold"


def _recompute_candidate_edge_fields(block: Dict[str, Any]) -> None:
    q_entry = _to_float(block.get("q_entry") or block.get("q_bayes_posterior"))
    ask = _to_float(block.get("ask") or block.get("best_ask"))
    if q_entry is None or ask is None:
        return
    edge = float(q_entry) - float(ask)
    for key in ("edge_min", "exec_edge_entry", "edge_realizable", "edge_realizable_v2"):
        if key in block:
            block[key] = edge
    for key in ("edge_score", "exec_edge_score", "edge_realizable_score"):
        if key in block:
            block[key] = edge * 100.0
    if "edge_tradeability_profit_value" in block:
        block["edge_tradeability_profit_value"] = edge


def _sync_posterior_probability_aliases(
    payload: Dict[str, Any],
    *,
    probs: Mapping[str, float],
    ranked: List[Tuple[str, float]],
) -> None:
    for key in (
        "target_bucket_selection",
        "posterior_target_bucket_selection",
        "target_bucket_outlook",
        "temperature_path_distribution",
        "target_realization_capital_plan",
        "target_switch_context",
        "auto_trade_ssot",
        "chengdu_intraday_summary",
    ):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            _walk_sync_probability_blocks(value, probs, ranked)
    plan = payload.get("target_realization_capital_plan")
    if isinstance(plan, dict):
        _rescale_capital_plan_from_probabilities(plan, probs)
        plan["posterior_aliases_synced_by_ctp_materializer"] = True
    ssot = payload.get("auto_trade_ssot")
    if isinstance(ssot, dict):
        for candidate_key in ("main_candidate", "model_yes_top_candidate"):
            candidate = ssot.get(candidate_key)
            if isinstance(candidate, dict):
                _recompute_candidate_edge_fields(candidate)
        ssot["posterior_aliases_synced_by_ctp_materializer"] = True
    provenance = dict(payload.get("fact_provenance") or {})
    provenance["ctp_probability_alias_sync"] = {
        "applied": True,
        "source": "posterior_temperature_distribution.bucket_probabilities",
        "synced_blocks": [
            "distribution",
            "target_bucket_selection",
            "posterior_target_bucket_selection",
            "target_realization_capital_plan",
            "auto_trade_ssot",
        ],
    }
    payload["fact_provenance"] = provenance


def _rebuild_execution_blocks_from_ctp_distribution(payload: Dict[str, Any]) -> None:
    try:
        from ui import server as ui_server  # Imported after CTP replay; UI import mutates env defaults.
    except Exception as exc:
        payload["ctp_execution_blocks_rebuild_error"] = f"{exc.__class__.__name__}: {exc}"
        return
    try:
        sanitized = dict(payload)
        for key in (
            "target_bucket_outlook",
            "temperature_path_distribution",
            "target_realization_capital_plan",
            "target_switch_context",
            "auto_trade_ssot",
            "chengdu_intraday_summary",
        ):
            sanitized.pop(key, None)
        selection = payload.get("posterior_target_bucket_selection")
        if isinstance(selection, Mapping):
            sanitized["target_bucket_selection"] = dict(selection)
            sanitized["posterior_target_bucket_selection"] = dict(selection)
        plan = ui_server._build_target_realization_capital_plan(sanitized)
        if isinstance(plan, Mapping):
            payload["target_realization_capital_plan"] = dict(plan)
            sanitized["target_realization_capital_plan"] = dict(plan)
        ssot = ui_server._safe_build_auto_trade_ssot(sanitized)
        if isinstance(ssot, Mapping):
            payload["auto_trade_ssot"] = dict(ssot)
        payload["ctp_execution_blocks_rebuilt_by_materializer"] = True
        provenance = dict(payload.get("fact_provenance") or {})
        provenance["ctp_execution_blocks_rebuild"] = {
            "applied": True,
            "source": "posterior_temperature_distribution.bucket_probabilities",
            "order": "ctp_replay_then_ui_execution_blocks",
        }
        payload["fact_provenance"] = provenance
    except Exception as exc:
        payload["ctp_execution_blocks_rebuild_error"] = f"{exc.__class__.__name__}: {exc}"


def _payload_likelihood_features(payload: Mapping[str, Any]) -> Dict[str, Any]:
    for key in ("weather_likelihood_features", "weather_likelihood_features_compact"):
        value = payload.get(key)
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _sync_regime_v1_momentum_fields(features: Dict[str, Any], peak: Mapping[str, Any]) -> bool:
    terms = peak.get("energy_balance_terms") if isinstance(peak.get("energy_balance_terms"), Mapping) else {}
    momentum = peak.get("temperature_momentum") if isinstance(peak.get("temperature_momentum"), Mapping) else {}
    confirmed_windows = str(terms.get("temperature_trajectory_confirmed_momentum_windows") or "").strip()
    uses_confirmed = bool(terms.get("temperature_trajectory_uses_confirmed_momentum"))
    if not uses_confirmed and not confirmed_windows:
        return False
    updates: Dict[str, Any] = {
        "temperature_momentum_posterior_gate_allowed": bool(uses_confirmed),
        "temperature_momentum_posterior_gate_state": "confirmed" if uses_confirmed else "unconfirmed",
        "temperature_momentum_posterior_gate_reason": (
            "regime_v1_confirmed_momentum_windows" if uses_confirmed else "regime_v1_confirmation_missing"
        ),
        "temperature_momentum_posterior_confirmed_windows": confirmed_windows,
        "temperature_momentum_posterior_confirmed_window_count": len(
            [part for part in confirmed_windows.split(",") if part.strip()]
        ),
    }
    for key, source_key in (
        ("temperature_momentum_effective_c_per_hour", "effective_rate_c_per_hour"),
        ("temperature_momentum_context_effective_c_per_hour", "context_effective_rate_c_per_hour"),
        ("temperature_momentum_context_warming_watch", "context_warming_watch"),
        ("temperature_momentum_realization_state", "state"),
        ("temperature_momentum_sustained_warming", "sustained_warming"),
    ):
        if source_key in momentum:
            updates[key] = momentum.get(source_key)
    changed = False
    for key, value in updates.items():
        if features.get(key) != value:
            features[key] = value
            changed = True
    return changed


def _refresh_peak_estimator(payload: Dict[str, Any]) -> bool:
    refreshed = False
    for key in ("weather_likelihood_features", "weather_likelihood_features_compact"):
        value = payload.get(key)
        if not isinstance(value, Mapping) or not value:
            continue
        features = dict(value)
        estimator_input = dict(features)
        estimator_input.pop("peak_estimator", None)
        peak = estimate_peak_temperature_from_features(estimator_input)
        if not isinstance(peak, Mapping) or not peak.get("enabled"):
            continue
        old_peak = features.get("peak_estimator") if isinstance(features.get("peak_estimator"), Mapping) else {}
        next_peak = dict(peak)
        old_regime = old_peak.get("observed_temperature_trajectory_regime_v1") if isinstance(old_peak, Mapping) else {}
        new_regime = peak.get("observed_temperature_trajectory_regime_v1")
        peak_changed = dict(old_peak) != next_peak
        sync_changed = _sync_regime_v1_momentum_fields(features, peak)
        if not peak_changed and not sync_changed:
            continue
        features["peak_estimator"] = next_peak
        features["peak_estimator_refreshed_by_ctp_materializer"] = True
        if isinstance(old_regime, Mapping) and isinstance(new_regime, Mapping):
            features["peak_estimator_previous_temperature_trajectory_path_mu_c"] = old_regime.get("path_mu_c")
            features["peak_estimator_refreshed_temperature_trajectory_path_mu_c"] = new_regime.get("path_mu_c")
        payload[key] = features
        refreshed = True
    if refreshed:
        payload["ctp_peak_estimator_refreshed_by_materializer"] = True
    return refreshed


def _refresh_physical_weather_modifier(payload: Dict[str, Any]) -> bool:
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping) or not features:
        features = payload.get("weather_likelihood_features_compact")
    if not isinstance(features, Mapping) or not features:
        return False
    groups = payload.get("weather_likelihood_feature_groups")
    modifier = _physical_weather_modifier(features, groups if isinstance(groups, Mapping) else None)
    if not isinstance(modifier, Mapping) or not modifier:
        return False
    changed = dict(payload.get("physical_weather_modifier") or {}) != dict(modifier)
    payload["physical_weather_modifier"] = dict(modifier)
    payload["physical_weather_state"] = modifier.get("state")
    payload["physical_weather_peak_mu_adjustment_c"] = modifier.get("peak_mu_adjustment_c")
    payload["physical_weather_path_probability_scale"] = modifier.get("path_probability_scale")
    physical_likelihood = (
        dict(payload.get("physical_likelihood") or {})
        if isinstance(payload.get("physical_likelihood"), Mapping)
        else {}
    )
    if physical_likelihood.get("weather_modifier") != modifier:
        changed = True
    physical_likelihood["weather_modifier"] = dict(modifier)
    physical_likelihood["weather_modifier_state"] = modifier.get("state")
    physical_likelihood["weather_modifier_peak_mu_adjustment_c"] = modifier.get("peak_mu_adjustment_c")
    physical_likelihood["weather_modifier_path_probability_scale"] = modifier.get("path_probability_scale")
    payload["physical_likelihood"] = physical_likelihood
    payload["physical_weather_modifier_refreshed_by_ctp_materializer"] = True
    return changed


def _available_regime_v1(payload: Mapping[str, Any]) -> Dict[str, Any]:
    features = _payload_likelihood_features(payload)
    peak = features.get("peak_estimator")
    if not isinstance(peak, Mapping):
        return {}
    regime = peak.get("observed_temperature_trajectory_regime_v1")
    if not isinstance(regime, Mapping):
        return {}
    available = bool(regime.get("available")) and _to_float(regime.get("path_mu_c")) is not None and (
        _to_float(regime.get("path_sigma_c")) or 0.0
    ) > 0.0
    return dict(regime) if available else {}


def _regime_v1_available(payload: Mapping[str, Any]) -> bool:
    return bool(_available_regime_v1(payload))


def _posterior_has_temperature_trajectory_component(posterior: Any) -> bool:
    if not isinstance(posterior, Mapping):
        return False
    source_components = posterior.get("source_components")
    if isinstance(source_components, Mapping):
        for group in source_components.get("groups") or []:
            if not isinstance(group, Mapping):
                continue
            if str(group.get("group") or "") == "temperature_trajectory":
                return True
            for member in group.get("members") or []:
                if not isinstance(member, Mapping):
                    continue
                raw = member.get("raw") if isinstance(member.get("raw"), Mapping) else {}
                if str(member.get("source_id") or raw.get("source") or "") == "temperature_trajectory":
                    return True
                if str(raw.get("source") or "") == "observed_temperature_trajectory_regime_v1":
                    return True
        for component in source_components.get("components") or []:
            if not isinstance(component, Mapping):
                continue
            raw = component.get("raw") if isinstance(component.get("raw"), Mapping) else {}
            if str(component.get("group") or "") == "temperature_trajectory":
                return True
            if str(component.get("source_id") or "") == "temperature_trajectory":
                return True
            if str(raw.get("source") or "") == "observed_temperature_trajectory_regime_v1":
                return True
    updates = posterior.get("likelihood_updates")
    if isinstance(updates, Mapping):
        for update in updates.get("updates") or []:
            if not isinstance(update, Mapping):
                continue
            if str(update.get("source_id") or update.get("source") or "") == "temperature_trajectory":
                return True
    return False


def _posterior_temperature_trajectory_precision(posterior: Any) -> Optional[float]:
    if not isinstance(posterior, Mapping):
        return None
    source_components = posterior.get("source_components")
    if not isinstance(source_components, Mapping):
        return None
    for group in source_components.get("groups") or []:
        if not isinstance(group, Mapping):
            continue
        if str(group.get("group") or "") == "temperature_trajectory":
            precision = _to_float(group.get("effective_precision"))
            if precision is not None:
                return precision
        for member in group.get("members") or []:
            if not isinstance(member, Mapping):
                continue
            raw = member.get("raw") if isinstance(member.get("raw"), Mapping) else {}
            if str(member.get("source_id") or raw.get("source") or "") != "temperature_trajectory" and (
                str(raw.get("source") or "") != "observed_temperature_trajectory_regime_v1"
            ):
                continue
            precision = _to_float(member.get("precision"))
            if precision is None:
                precision = _to_float(raw.get("precision"))
            if precision is not None:
                return precision
    for component in source_components.get("components") or []:
        if not isinstance(component, Mapping):
            continue
        raw = component.get("raw") if isinstance(component.get("raw"), Mapping) else {}
        if (
            str(component.get("group") or "") != "temperature_trajectory"
            and str(component.get("source_id") or "") != "temperature_trajectory"
            and str(raw.get("source") or "") != "observed_temperature_trajectory_regime_v1"
        ):
            continue
        precision = _to_float(component.get("precision"))
        if precision is None:
            precision = _to_float(raw.get("precision"))
        if precision is not None:
            return precision
    return None


def _posterior_regime_v1_replay_reason(payload: Mapping[str, Any]) -> str:
    if not _posterior_complete(payload):
        return ""
    regime = _available_regime_v1(payload)
    posterior = payload.get("posterior_temperature_distribution")
    if not regime:
        if _posterior_has_temperature_trajectory_component(posterior):
            return "stale_temperature_trajectory_component_without_available_regime_v1"
        return ""
    if not _posterior_has_temperature_trajectory_component(posterior):
        return "available_regime_v1_missing_temperature_trajectory_component"
    expected_precision = _to_float(
        regime.get("precision")
        or regime.get("effective_evidence")
        or regime.get("evidence")
    )
    actual_precision = _posterior_temperature_trajectory_precision(posterior)
    if expected_precision is None or expected_precision <= 0:
        return "available_regime_v1_missing_temperature_trajectory_precision"
    if actual_precision is None:
        return "available_regime_v1_missing_temperature_trajectory_precision"
    tolerance = max(1e-6, abs(float(expected_precision)) * 1e-6)
    if abs(float(actual_precision) - float(expected_precision)) > tolerance:
        return "available_regime_v1_temperature_trajectory_precision_changed"
    if not isinstance(regime.get("forcing_onset_probs"), Mapping):
        return "available_regime_v1_missing_forcing_onset_probs"
    if not isinstance(regime.get("forcing_contributors"), list):
        return "available_regime_v1_missing_forcing_contributors"
    features = _payload_likelihood_features(payload)
    peak = features.get("peak_estimator") if isinstance(features.get("peak_estimator"), Mapping) else {}
    if isinstance(peak, Mapping):
        terms = peak.get("energy_balance_terms") if isinstance(peak.get("energy_balance_terms"), Mapping) else {}
        confirmed_windows = str(terms.get("temperature_trajectory_confirmed_momentum_windows") or "").strip()
        uses_confirmed = bool(terms.get("temperature_trajectory_uses_confirmed_momentum"))
        if uses_confirmed and (
            features.get("temperature_momentum_posterior_gate_allowed") is not True
            or str(features.get("temperature_momentum_posterior_confirmed_windows") or "").strip()
            != confirmed_windows
        ):
            return "available_regime_v1_momentum_gate_sync_changed"
    return ""


def _posterior_needs_regime_v1_replay(payload: Mapping[str, Any]) -> bool:
    return bool(_posterior_regime_v1_replay_reason(payload))


def _posterior_top_bucket(posterior: Mapping[str, Any]) -> Tuple[str, Optional[float]]:
    rows = posterior.get("bucket_probabilities") if isinstance(posterior, Mapping) else None
    values: Dict[str, float] = {}
    if isinstance(rows, Mapping):
        for key, value in rows.items():
            prob = _to_float(value)
            if prob is not None:
                values[str(key)] = max(0.0, min(1.0, float(prob)))
    elif isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            label = str(row.get("outcome") or row.get("label") or "").strip()
            prob = _to_float(row.get("probability") if row.get("probability") is not None else row.get("prob"))
            if label and prob is not None:
                values[label] = max(0.0, min(1.0, float(prob)))
    if not values:
        return "", None
    return max(values.items(), key=lambda item: (float(item[1]), item[0]))


def _ranked_posterior_buckets(probs: Mapping[str, float]) -> List[Tuple[str, float]]:
    rows: List[Tuple[str, float]] = []
    for label, prob in probs.items():
        value = _to_float(prob)
        if not label or value is None:
            continue
        rows.append((str(label), max(0.0, min(1.0, float(value)))))
    return sorted(rows, key=lambda item: (float(item[1]), item[0]), reverse=True)


def _sync_target_selection_block(
    block: Any,
    *,
    target_bucket: str,
    target_probability: float,
    second_bucket: str = "",
    second_probability: Optional[float] = None,
) -> Dict[str, Any]:
    out = dict(block) if isinstance(block, Mapping) else {}
    probability = max(0.0, min(1.0, float(target_probability)))
    out.update(
        {
            "target_bucket": target_bucket,
            "target_probability": probability,
            "target_probability_pct": probability * 100.0,
            "target_posterior": probability,
            "target_bucket_probability": probability,
            "target_bucket_probability_pct": probability * 100.0,
            "target_probability_field": "posterior_temperature_distribution.bucket_probabilities",
            "target_bucket_source": "posterior_temperature_distribution",
            "target_bucket_source_semantics": "continuous_temperature_posterior_v1",
            "source": "continuous_temperature_posterior_v1_materialized_latest",
            "ctp_latest_materialized": True,
        }
    )
    if second_bucket and second_probability is not None:
        second_prob = max(0.0, min(1.0, float(second_probability)))
        out["second_target_bucket"] = second_bucket
        out["second_target_posterior"] = second_prob
        out["second_target_probability"] = second_prob
        out["second_target_probability_pct"] = second_prob * 100.0
    return out


def _sync_posterior_target_derived_blocks(
    payload: Dict[str, Any],
    *,
    top: Tuple[str, float],
    ranked: List[Tuple[str, float]],
) -> None:
    target_bucket, target_probability = top
    second_bucket = ""
    second_probability: Optional[float] = None
    for label, prob in ranked:
        if label != target_bucket:
            second_bucket = label
            second_probability = prob
            break
    selection = _sync_target_selection_block(
        payload.get("target_bucket_selection"),
        target_bucket=target_bucket,
        target_probability=target_probability,
        second_bucket=second_bucket,
        second_probability=second_probability,
    )
    payload["target_bucket_selection"] = selection
    payload["posterior_target_bucket_selection"] = dict(selection)
    alignment = dict(payload.get("single_ssot_target_alignment")) if isinstance(
        payload.get("single_ssot_target_alignment"), Mapping
    ) else {}
    alignment.update(
        {
            "version": alignment.get("version") or "single_event_fact_latest_canonical_target_v1",
            "source": "posterior_temperature_distribution.bucket_probabilities",
            "target_bucket": target_bucket,
            "target_probability_pct": float(target_probability) * 100.0,
            "target_probability": float(target_probability),
            "synced_by": "continuous_temperature_posterior_v1_materialized_latest",
        }
    )
    payload["single_ssot_target_alignment"] = alignment
    plan = payload.get("target_realization_capital_plan")
    if isinstance(plan, Mapping):
        plan_out = dict(plan)
        plan_out["target_bucket"] = target_bucket
        plan_out["target_posterior"] = float(target_probability)
        plan_out["target_bucket_selection_source"] = "posterior_temperature_distribution"
        if second_bucket and second_probability is not None:
            plan_out["second_target_bucket"] = second_bucket
            plan_out["second_target_posterior"] = float(second_probability)
        payload["target_realization_capital_plan"] = plan_out


def _temperature_trajectory_component_meta(
    posterior: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    source_components = posterior.get("source_components") if isinstance(posterior, Mapping) else None
    candidates: List[Mapping[str, Any]] = []
    if isinstance(source_components, Mapping):
        raw_components = source_components.get("components")
        if isinstance(raw_components, list):
            candidates.extend(component for component in raw_components if isinstance(component, Mapping))
        raw_groups = source_components.get("groups")
        if isinstance(raw_groups, list):
            for group in raw_groups:
                if not isinstance(group, Mapping):
                    continue
                raw_members = group.get("members")
                if isinstance(raw_members, list):
                    candidates.extend(member for member in raw_members if isinstance(member, Mapping))
    for component in candidates:
        raw = component.get("raw") if isinstance(component.get("raw"), Mapping) else {}
        if (
            str(component.get("group") or "") != "temperature_trajectory"
            and str(component.get("source_id") or "") != "temperature_trajectory"
            and str(raw.get("source") or "") != "observed_temperature_trajectory_regime_v1"
        ):
            continue
        regime = _available_regime_v1(payload)
        precision = _to_float(component.get("precision"))
        if precision is None:
            precision = _to_float(raw.get("precision"))
        mean_c = _to_float(component.get("mean_c"))
        if mean_c is None:
            mean_c = _to_float(raw.get("mean_c") if raw.get("mean_c") is not None else raw.get("path_mu_c"))
        path_mu_c = _to_float(raw.get("path_mu_c"))
        if path_mu_c is None:
            path_mu_c = _to_float(regime.get("path_mu_c"))
        if path_mu_c is None:
            path_mu_c = mean_c
        sigma_c = _to_float(component.get("sigma_c"))
        if sigma_c is None:
            sigma_c = _to_float(raw.get("sigma_c") if raw.get("sigma_c") is not None else raw.get("path_sigma_c"))
        path_sigma_c = _to_float(raw.get("path_sigma_c"))
        if path_sigma_c is None:
            path_sigma_c = _to_float(regime.get("path_sigma_c"))
        if path_sigma_c is None:
            path_sigma_c = sigma_c
        rate = _to_float(raw.get("rate_c_per_hour"))
        if rate is None:
            rate = _to_float(raw.get("rate_hat_c_per_hour"))
        if rate is None:
            rate = _to_float(regime.get("rate_hat_c_per_hour"))
        accel = _to_float(raw.get("accel_hat_c_per_hour2"))
        if accel is None:
            accel = _to_float(regime.get("accel_hat_c_per_hour2"))
        evidence_base = _to_float(raw.get("evidence_base"))
        if evidence_base is None:
            evidence_base = _to_float(regime.get("evidence_base"))
        if evidence_base is None:
            evidence_base = precision
        evidence_scale = _to_float(raw.get("evidence_scale"))
        if evidence_scale is None:
            evidence_scale = _to_float(regime.get("evidence_scale"))
        if evidence_scale is None:
            evidence_scale = (
                float(precision) / max(1e-9, float(evidence_base))
                if precision is not None and evidence_base is not None and evidence_base > 0.0
                else 0.0
            )
        return {
            "enabled": precision is not None and precision > 0.0,
            "evidence": precision,
            "evidence_base": evidence_base,
            "evidence_scale": evidence_scale,
            "source": raw.get("source") or component.get("source") or "observed_temperature_trajectory_regime_v1",
            "status": "enabled" if precision is not None and precision > 0.0 else "zero_scaled",
            "mu_c": mean_c,
            "sigma_c": sigma_c,
            "path_mu_c": path_mu_c,
            "path_sigma_c": path_sigma_c,
            "rate_c_per_hour": rate,
            "rate_hat_c_per_hour": rate,
            "accel_hat_c_per_hour2": accel,
            "forcing_turn_probability": raw.get("forcing_turn_probability") or regime.get("forcing_turn_probability"),
            "forcing_onset_probs": raw.get("forcing_onset_probs") or regime.get("forcing_onset_probs"),
            "forcing_contributors": raw.get("forcing_contributors") or regime.get("forcing_contributors"),
            "regime_probs": raw.get("regime_probs") or regime.get("regime_probs"),
            "role": component.get("role") or raw.get("role") or "observed_realization",
            "source_class": raw.get("source_class") or "airport_temperature_path",
            "dedupe_policy": raw.get("dedupe_policy") or regime.get("dedupe_policy") or "",
            "weaker_than_hard_source": True,
        }
    return {}


def _inactive_temperature_trajectory_meta(payload: Mapping[str, Any]) -> Dict[str, Any]:
    features = _payload_likelihood_features(payload)
    peak = features.get("peak_estimator") if isinstance(features.get("peak_estimator"), Mapping) else {}
    peak_source = str(peak.get("source") or "").strip()
    peak_window_end = _to_float(peak.get("peak_window_end_hour_local"))
    lock_threshold = _to_float(peak.get("lock_threshold_hour_local"))
    hour_local = _to_float(peak.get("hour_local"))
    if peak_source in {"locked_trusted_observed", "locked_observed"}:
        return {
            "enabled": False,
            "evidence": 0.0,
            "status": f"{peak_source}_after_peak_window",
            "na_reason": "hard_observed_peak_locked_after_peak_window",
            "source": "observed_temperature_trajectory_regime_v1",
            "role": "observed_realization",
            "source_class": "airport_temperature_path",
            "peak_estimator_source": peak_source,
            "peak_window_end_hour_local": peak_window_end,
            "lock_threshold_hour_local": lock_threshold,
            "hour_local": hour_local,
        }
    regime = peak.get("observed_temperature_trajectory_regime_v1") if isinstance(peak, Mapping) else {}
    if isinstance(regime, Mapping) and regime:
        reason = str(regime.get("na_reason") or "").strip() or "regime_v1_not_available"
        return {
            "enabled": False,
            "evidence": 0.0,
            "status": "regime_v1_unavailable_after_ctp_materialization",
            "na_reason": reason,
            "source": regime.get("source") or "observed_temperature_trajectory_regime_v1",
            "role": regime.get("role") or "observed_realization",
            "source_class": regime.get("source_class") or "airport_temperature_path",
            "peak_estimator_source": peak_source,
        }
    return {
        "enabled": False,
        "evidence": 0.0,
        "status": "unavailable_after_ctp_materialization",
        "na_reason": "temperature_trajectory_component_missing_after_ctp_materialization",
        "source": "observed_temperature_trajectory_regime_v1",
        "role": "observed_realization",
        "source_class": "airport_temperature_path",
        "peak_estimator_source": peak_source,
    }


def _probability_matches(value: Any, expected: float, *, tolerance: float = 1e-9) -> bool:
    actual = _to_float(value)
    if actual is None:
        return True
    actual_norm = float(actual) / 100.0 if float(actual) > 1.0 else float(actual)
    return abs(actual_norm - float(expected)) <= tolerance


def _payload_has_stale_ctp_probability_aliases(payload: Mapping[str, Any]) -> bool:
    posterior = payload.get("posterior_temperature_distribution")
    if not isinstance(posterior, Mapping):
        return False
    probs = _posterior_probs(posterior)
    if not probs:
        return False
    rows = payload.get("distribution")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            label = _bucket_label(row)
            if label not in probs:
                continue
            expected = max(0.0, min(1.0, float(probs[label])))
            for key in (
                "q_bayes_posterior",
                "q_temperature_bucket_posterior",
                "target_bucket_probability",
            ):
                if key in row and not _probability_matches(row.get(key), expected):
                    return True
            if any(key in row for key in LEGACY_INTERVAL_PROBABILITY_FIELDS):
                return True
            hard_zero = bool(
                row.get("q_posterior_observed_constraints_hard_zero")
                or row.get("posterior_settlement_candidate_bucket_hard_zero")
            )
            if hard_zero and expected <= 1e-12:
                observed_expected = 0.0
            else:
                observed_expected = expected
            for key in (
                "q_posterior_observed_constraints",
                "q_posterior_observed_constraints_post_prob",
            ):
                if key in row and not _probability_matches(row.get(key), observed_expected):
                    return True
    plan = payload.get("target_realization_capital_plan")
    if isinstance(plan, Mapping):
        target = str(plan.get("target_bucket") or "").strip()
        if target in probs and not _probability_matches(plan.get("target_posterior"), probs[target]):
            return True
        second = str(plan.get("second_target_bucket") or "").strip()
        if second in probs and not _probability_matches(plan.get("second_target_posterior"), probs[second]):
            return True
        for leg in plan.get("legs") if isinstance(plan.get("legs"), list) else []:
            if not isinstance(leg, Mapping):
                continue
            label = _bucket_label(leg)
            if label not in probs:
                continue
            expected = max(0.0, min(1.0, float(probs[label])))
            if "posterior" in leg and not _probability_matches(leg.get("posterior"), expected):
                return True
            if str(leg.get("side") or "").strip().upper() == "NO" and "prob_for_size" in leg:
                if not _probability_matches(leg.get("prob_for_size"), 1.0 - expected):
                    return True
    ssot = payload.get("auto_trade_ssot")
    if isinstance(ssot, Mapping):
        for candidate_key in ("main_candidate", "model_yes_top_candidate"):
            candidate = ssot.get(candidate_key)
            if not isinstance(candidate, Mapping):
                continue
            label = _bucket_label(candidate)
            if label not in probs:
                continue
            side = str(candidate.get("side") or "").strip().upper()
            expected = 1.0 - float(probs[label]) if side == "NO" else float(probs[label])
            for key in (
                "q_bayes_posterior",
                "q_entry",
                "target_posterior",
            ):
                if key in candidate and not _probability_matches(candidate.get(key), expected):
                    return True
            if any(key in candidate for key in LEGACY_INTERVAL_PROBABILITY_FIELDS):
                return True
    return False


def _payload_needs_top_level_ctp_sync(payload: Mapping[str, Any]) -> bool:
    if not _posterior_complete(payload):
        return False
    posterior = payload.get("posterior_temperature_distribution")
    if not isinstance(posterior, Mapping):
        return False
    if _payload_has_stale_ctp_probability_aliases(payload):
        return True
    has_temperature_trajectory = _posterior_has_temperature_trajectory_component(posterior)
    if not has_temperature_trajectory:
        return False
    top_label, top_probability = _posterior_top_bucket(posterior)
    if top_label and str(payload.get("posterior_highest_bucket_label") or "") != top_label:
        return True
    if top_probability is not None:
        expected_pct = float(top_probability) * 100.0
        expected_prob = max(0.0, min(1.0, float(top_probability)))
        current_pct = _to_float(payload.get("posterior_highest_probability_pct"))
        if current_pct is None or abs(float(current_pct) - expected_pct) > 1e-6:
            return True
        target_pct = _to_float(payload.get("target_bucket_probability_pct"))
        if target_pct is None or abs(float(target_pct) - expected_pct) > 1e-6:
            return True
        for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
            selection = payload.get(key)
            if not isinstance(selection, Mapping):
                return True
            if str(selection.get("target_bucket") or "") != top_label:
                return True
            selection_prob = _to_float(selection.get("target_posterior"))
            if selection_prob is None:
                selection_prob = _to_float(selection.get("target_probability"))
            if selection_prob is None or abs(float(selection_prob) - expected_prob) > 1e-9:
                return True
            selection_pct = _to_float(selection.get("target_probability_pct"))
            if selection_pct is None or abs(float(selection_pct) - expected_pct) > 1e-6:
                return True
        plan = payload.get("target_realization_capital_plan")
        if isinstance(plan, Mapping):
            plan_prob = _to_float(plan.get("target_posterior"))
            if str(plan.get("target_bucket") or "") != top_label:
                return True
            if plan_prob is None or abs(float(plan_prob) - expected_prob) > 1e-9:
                return True
    evidence = _to_float(payload.get("q_bayes_temperature_trajectory_evidence"))
    if evidence is None or evidence <= 0.0:
        return True
    return False


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _ensure_summary_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            run_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_fact_summary_cache_key
        ON event_fact_summary_cache(slug, event_date, fact_as_of)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_fact_summary_cache_run_date
        ON event_fact_summary_cache(run_date, event_date, fact_as_of)
        """
    )


def _summary_cache_exists(conn: sqlite3.Connection, fact_id: int) -> bool:
    _ensure_summary_cache_table(conn)
    row = conn.execute(
        "SELECT 1 FROM event_fact_summary_cache WHERE fact_id = ? LIMIT 1",
        (int(fact_id),),
    ).fetchone()
    return bool(row)


def _upsert_summary_cache(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    created_at: str,
) -> bool:
    if int(fact_id or 0) <= 0 or not isinstance(payload, Mapping):
        return False
    _ensure_summary_cache_table(conn)
    summary = dict(payload)
    summary.setdefault("slug", row["slug"])
    summary.setdefault("event_date", row["event_date"])
    summary.setdefault("run_date", row["run_date"] or row["event_date"])
    summary.setdefault("fact_as_of", row["fact_as_of"])
    summary["summary_cache_source"] = "event_fact_summary_cache"
    summary["summary_cache_version"] = SUMMARY_CACHE_VERSION
    conn.execute(
        """
        INSERT OR REPLACE INTO event_fact_summary_cache(
            fact_id, slug, event_date, run_date, fact_as_of, summary_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(fact_id),
            str(row["slug"] or ""),
            str(row["event_date"] or ""),
            str(row["run_date"] or row["event_date"] or ""),
            str(row["fact_as_of"] or ""),
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            created_at,
        ),
    )
    return True


def _posterior_probs(posterior: Mapping[str, Any]) -> Dict[str, float]:
    payload = {"posterior_temperature_distribution": posterior}
    return _ctp_probs(payload)


def _materialize_payload(payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    out = dict(payload)
    pre_refresh_regime_replay_reason = _posterior_regime_v1_replay_reason(out)
    peak_refreshed = _refresh_peak_estimator(out)
    regime_replay_reason = pre_refresh_regime_replay_reason or _posterior_regime_v1_replay_reason(out)
    regime_replay_needed = bool(regime_replay_reason)
    force_replay = bool(peak_refreshed or regime_replay_needed)
    posterior = _posterior_temperature_distribution(out, force_replay=force_replay)
    if not isinstance(posterior, Mapping) or not REQUIRED_POSTERIOR_KEYS.issubset(posterior.keys()):
        return out, {
            "ok": False,
            "reason": "ctp_replay_missing_required_keys",
            "posterior_status": posterior.get("status") if isinstance(posterior, Mapping) else None,
        }
    probs = _posterior_probs(posterior)
    if not probs:
        return out, {"ok": False, "reason": "ctp_bucket_probabilities_empty"}
    rows = out.get("distribution")
    if isinstance(rows, list):
        next_rows: List[Any] = []
        for row in rows:
            if not isinstance(row, Mapping):
                next_rows.append(row)
                continue
            next_row = dict(row)
            label = str(next_row.get("outcome") or next_row.get("label") or "").strip()
            if label in probs:
                prob = max(0.0, min(1.0, float(probs[label])))
                _sync_distribution_row_probability(
                    next_row,
                    label=label,
                    prob=prob,
                    posterior=posterior,
                )
            next_rows.append(next_row)
        out["distribution"] = next_rows
    top = max(probs.items(), key=lambda item: (float(item[1]), item[0]))
    ranked = _ranked_posterior_buckets(probs)
    out["posterior_temperature_distribution"] = dict(posterior)
    out["posterior_temperature_distribution_source"] = "continuous_temperature_posterior_v1_materialized_latest"
    out["posterior_highest_bucket_label"] = top[0]
    out["posterior_highest_probability_pct"] = float(top[1]) * 100.0
    out["target_bucket"] = top[0]
    out["ssot_target_bucket"] = top[0]
    out["target_bucket_probability_pct"] = float(top[1]) * 100.0
    out["target_bucket_source"] = "posterior_temperature_distribution"
    out["target_bucket_probability_field"] = "posterior_temperature_distribution.bucket_probabilities"
    _sync_posterior_target_derived_blocks(out, top=(top[0], float(top[1])), ranked=ranked)
    _sync_posterior_probability_aliases(out, probs=probs, ranked=ranked)
    _rebuild_execution_blocks_from_ctp_distribution(out)
    _sync_posterior_probability_aliases(out, probs=probs, ranked=ranked)
    physical_refreshed = _refresh_physical_weather_modifier(out)
    out["ctp_latest_materialized_at"] = datetime.now(timezone.utc).isoformat()
    if physical_refreshed:
        out["ctp_physical_weather_modifier_refreshed"] = True
    if regime_replay_needed:
        out["ctp_replay_reason"] = regime_replay_reason
    elif peak_refreshed:
        out["ctp_replay_reason"] = "peak_estimator_refreshed_by_materializer"
    trajectory_meta = _temperature_trajectory_component_meta(posterior, out)
    if trajectory_meta:
        out["q_bayes_temperature_trajectory_evidence"] = trajectory_meta.get("evidence")
        out["q_bayes_temperature_trajectory_evidence_base"] = trajectory_meta.get("evidence_base")
        out["q_bayes_temperature_trajectory_evidence_scale"] = trajectory_meta.get("evidence_scale")
        out["q_bayes_temperature_trajectory_enabled"] = trajectory_meta.get("enabled")
        out["q_bayes_temperature_trajectory_source"] = trajectory_meta.get("source")
        out["q_bayes_temperature_trajectory_status"] = trajectory_meta.get("status")
        out["q_bayes_temperature_trajectory_mu_c"] = trajectory_meta.get("mu_c")
        out["q_bayes_temperature_trajectory_sigma_c"] = trajectory_meta.get("sigma_c")
        out["q_bayes_temperature_trajectory_path_mu_c"] = trajectory_meta.get("path_mu_c")
        out["q_bayes_temperature_trajectory_path_sigma_c"] = trajectory_meta.get("path_sigma_c")
        out["q_bayes_temperature_trajectory_rate_c_per_hour"] = trajectory_meta.get("rate_c_per_hour")
        out["q_bayes_temperature_trajectory_rate_hat_c_per_hour"] = trajectory_meta.get("rate_hat_c_per_hour")
        out["q_bayes_temperature_trajectory_accel_hat_c_per_hour2"] = trajectory_meta.get(
            "accel_hat_c_per_hour2"
        )
        out["q_bayes_temperature_trajectory_role"] = trajectory_meta.get("role")
        out["q_bayes_temperature_trajectory_source_class"] = trajectory_meta.get("source_class")
        out["q_bayes_temperature_trajectory_dedupe_policy"] = trajectory_meta.get("dedupe_policy")
        out["q_bayes_temperature_trajectory_weaker_than_hard_source"] = trajectory_meta.get(
            "weaker_than_hard_source"
        )
        out["q_bayes_temperature_trajectory_forcing_turn_probability"] = trajectory_meta.get(
            "forcing_turn_probability"
        )
        out["q_bayes_temperature_trajectory_forcing_onset_probs"] = trajectory_meta.get("forcing_onset_probs")
        out["q_bayes_temperature_trajectory_forcing_contributors"] = trajectory_meta.get("forcing_contributors")
        out["q_bayes_temperature_trajectory_regime_probs"] = trajectory_meta.get("regime_probs")
    else:
        for key in list(out.keys()):
            if str(key).startswith("q_bayes_temperature_trajectory_"):
                out.pop(key, None)
        inactive_meta = _inactive_temperature_trajectory_meta(out)
        out["q_bayes_temperature_trajectory_evidence"] = inactive_meta.get("evidence")
        out["q_bayes_temperature_trajectory_enabled"] = inactive_meta.get("enabled")
        out["q_bayes_temperature_trajectory_status"] = inactive_meta.get("status")
        out["q_bayes_temperature_trajectory_na_reason"] = inactive_meta.get("na_reason")
        out["q_bayes_temperature_trajectory_source"] = inactive_meta.get("source")
        out["q_bayes_temperature_trajectory_role"] = inactive_meta.get("role")
        out["q_bayes_temperature_trajectory_source_class"] = inactive_meta.get("source_class")
        out["q_bayes_temperature_trajectory_peak_estimator_source"] = inactive_meta.get(
            "peak_estimator_source"
        )
        out["q_bayes_temperature_trajectory_peak_window_end_hour_local"] = inactive_meta.get(
            "peak_window_end_hour_local"
        )
        out["q_bayes_temperature_trajectory_lock_threshold_hour_local"] = inactive_meta.get(
            "lock_threshold_hour_local"
        )
        out["q_bayes_temperature_trajectory_hour_local"] = inactive_meta.get("hour_local")
    return out, {
        "ok": True,
        "posterior_status": posterior.get("status"),
        "bucket_count": len(probs),
        "top_label": top[0],
        "top_probability": float(top[1]),
        "forced_replay_reason": (
            regime_replay_reason
            if regime_replay_needed
            else "peak_estimator_refreshed_by_materializer"
            if peak_refreshed
            else ""
        ),
        "peak_estimator_refreshed": bool(peak_refreshed),
        "physical_weather_modifier_refreshed": bool(physical_refreshed),
    }


def _candidate_rows(conn: sqlite3.Connection, slug_like: str, limit: Optional[int]) -> List[sqlite3.Row]:
    limit_sql = " LIMIT ?" if limit is not None and limit > 0 else ""
    params: List[Any] = [slug_like]
    if limit is not None and limit > 0:
        params.append(int(limit))
    return conn.execute(
        f"""
        SELECT h.*
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE lower(h.slug) LIKE lower(?)
        ORDER BY h.event_date, h.slug
        {limit_sql}
        """,
        params,
    ).fetchall()


def run(
    db_path: Path,
    *,
    apply: bool,
    slug_like: str,
    limit: Optional[int],
    timeout_sec: float = 30.0,
) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db_path), timeout=max(1.0, float(timeout_sec or 30.0)))
    conn.execute(f"PRAGMA busy_timeout={int(max(1000.0, float(timeout_sec or 30.0) * 1000.0))}")
    conn.row_factory = sqlite3.Row
    rows = _candidate_rows(conn, slug_like, limit)
    history_cols = _table_columns(conn, "event_fact_history")
    now = datetime.now(timezone.utc).isoformat()
    report: Dict[str, Any] = {
        "ok": True,
        "apply": bool(apply),
        "db_path": str(db_path),
        "slug_like": slug_like,
        "scanned_latest_facts": len(rows),
        "already_complete": 0,
        "would_append_event_fact_history_rows": 0,
        "applied_event_fact_history_rows": 0,
        "would_upsert_event_fact_summary_cache_rows": 0,
        "applied_event_fact_summary_cache_rows": 0,
        "items": [],
    }
    try:
        for row in rows:
            payload = _json_obj(row["fact_json"])
            if not payload or not isinstance(payload.get("distribution"), list):
                continue
            if (
                _posterior_complete(payload)
                and not _posterior_needs_regime_v1_replay(payload)
                and not _payload_needs_top_level_ctp_sync(payload)
            ):
                report["already_complete"] += 1
                summary_missing = False
                try:
                    summary_missing = not _summary_cache_exists(conn, int(row["id"] or 0))
                except sqlite3.Error:
                    summary_missing = False
                if summary_missing:
                    report["would_upsert_event_fact_summary_cache_rows"] += 1
                    item = {
                        "slug": row["slug"],
                        "event_date": row["event_date"],
                        "fact_id": row["id"],
                        "fact_as_of": row["fact_as_of"],
                        "ok": True,
                        "already_complete": True,
                        "summary_cache_missing": True,
                    }
                    if apply and _upsert_summary_cache(
                        conn,
                        fact_id=int(row["id"]),
                        row=row,
                        payload=payload,
                        created_at=now,
                    ):
                        item["summary_cache_upserted"] = True
                        report["applied_event_fact_summary_cache_rows"] += 1
                    report["items"].append(item)
                continue
            next_payload, meta = _materialize_payload(payload)
            item = {
                "slug": row["slug"],
                "event_date": row["event_date"],
                "fact_id": row["id"],
                "fact_as_of": row["fact_as_of"],
                **meta,
            }
            report["items"].append(item)
            if not meta.get("ok"):
                report["ok"] = False
                continue
            report["would_append_event_fact_history_rows"] += 1
            if not apply:
                continue
            insert_cols = [col for col in history_cols if col != "id"]
            values = []
            for col in insert_cols:
                if col == "fact_json":
                    values.append(json.dumps(next_payload, ensure_ascii=False, separators=(",", ":")))
                elif col == "created_at":
                    values.append(now)
                else:
                    values.append(row[col])
            placeholders = ",".join("?" for _ in insert_cols)
            col_sql = ",".join(insert_cols)
            cur = conn.execute(
                f"INSERT INTO event_fact_history({col_sql}) VALUES ({placeholders})",
                values,
            )
            new_fact_id = int(cur.lastrowid)
            conn.execute(
                """
                UPDATE event_fact_latest
                SET fact_id = ?, updated_at = ?
                WHERE slug = ? AND event_date = ?
                """,
                (new_fact_id, now, row["slug"], row["event_date"]),
            )
            if _upsert_summary_cache(
                conn,
                fact_id=new_fact_id,
                row=row,
                payload=next_payload,
                created_at=now,
            ):
                item["summary_cache_upserted"] = True
                report["applied_event_fact_summary_cache_rows"] += 1
            report["would_upsert_event_fact_summary_cache_rows"] += 1
            item["new_fact_id"] = new_fact_id
            report["applied_event_fact_history_rows"] += 1
        if apply:
            conn.commit()
    except Exception as exc:
        if apply:
            conn.rollback()
        report["ok"] = False
        report["error"] = str(exc)
    finally:
        conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug-like", default="%temperature%")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = run(
        Path(args.db),
        apply=bool(args.apply),
        slug_like=str(args.slug_like),
        limit=int(args.limit) if int(args.limit or 0) > 0 else None,
        timeout_sec=float(args.timeout_sec or 30.0),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
