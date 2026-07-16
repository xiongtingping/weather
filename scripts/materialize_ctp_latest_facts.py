#!/usr/bin/env python3
"""Materialize CTP v1 posterior distributions onto latest temperature facts.

Default mode is read-only. Use --apply only after reviewing the reported impact:
it appends new event_fact_history rows and repoints event_fact_latest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"

import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from bayesian_likelihood_runtime import estimate_peak_temperature_from_features  # noqa: E402
from chengdu_intraday_summary import _physical_weather_modifier  # noqa: E402
from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from source_observation_runtime import build_source_observation  # noqa: E402
from temperature_posterior_backtest import _ctp_probs, _posterior_temperature_distribution  # noqa: E402


REQUIRED_POSTERIOR_KEYS = {
    "grid",
    "cdf_values",
    "quantiles_c",
    "quantile_grid",
    "bucket_probabilities",
}

SUMMARY_CACHE_VERSION = "event_fact_summary_cache_v7_posterior_ssot_sections"
CTP_MATERIALIZER_COMPOSER_VERSION = "continuous_temperature_posterior_v1_materialized_latest"
CTP_MATERIALIZER_SOURCE_ID = "ctp_latest_materializer"

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
    if side != "NO" and "prob_for_size" in block:
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
        new_regime = next_peak.get("observed_temperature_trajectory_regime_v1")
        peak_source = str(next_peak.get("source") or "").strip()
        if (
            isinstance(old_regime, Mapping)
            and old_regime
            and not isinstance(new_regime, Mapping)
            and peak_source not in {"locked_trusted_observed", "locked_observed"}
        ):
            next_peak["observed_temperature_trajectory_regime_v1"] = dict(old_regime)
            new_regime = next_peak.get("observed_temperature_trajectory_regime_v1")
        peak_changed = dict(old_peak) != next_peak
        sync_changed = _sync_regime_v1_momentum_fields(features, next_peak)
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
    features = _payload_likelihood_features(payload)
    peak = features.get("peak_estimator") if isinstance(features.get("peak_estimator"), Mapping) else {}
    if str(peak.get("source") or "").strip() in {"locked_trusted_observed", "locked_observed"}:
        return {}
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
        regime_precision = _to_float(
            regime.get("precision")
            if regime.get("precision") is not None
            else regime.get("effective_evidence")
            if regime.get("effective_evidence") is not None
            else regime.get("evidence")
        )
        if regime_precision is not None:
            precision = regime_precision
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
    regime = _available_regime_v1(payload)
    if regime:
        precision = _to_float(
            regime.get("precision")
            if regime.get("precision") is not None
            else regime.get("effective_evidence")
            if regime.get("effective_evidence") is not None
            else regime.get("evidence")
        )
        evidence_base = _to_float(regime.get("evidence_base"))
        evidence_scale = _to_float(regime.get("evidence_scale"))
        return {
            "enabled": precision is not None and precision > 0.0,
            "evidence": precision,
            "evidence_base": evidence_base,
            "evidence_scale": evidence_scale,
            "source": regime.get("source") or "observed_temperature_trajectory_regime_v1",
            "status": "enabled" if precision is not None and precision > 0.0 else "zero_scaled",
            "mu_c": _to_float(regime.get("mean_c") if regime.get("mean_c") is not None else regime.get("path_mu_c")),
            "sigma_c": _to_float(
                regime.get("sigma_c") if regime.get("sigma_c") is not None else regime.get("path_sigma_c")
            ),
            "path_mu_c": _to_float(regime.get("path_mu_c")),
            "path_sigma_c": _to_float(regime.get("path_sigma_c")),
            "rate_c_per_hour": _to_float(regime.get("rate_hat_c_per_hour")),
            "rate_hat_c_per_hour": _to_float(regime.get("rate_hat_c_per_hour")),
            "accel_hat_c_per_hour2": _to_float(regime.get("accel_hat_c_per_hour2")),
            "forcing_turn_probability": regime.get("forcing_turn_probability"),
            "forcing_onset_probs": regime.get("forcing_onset_probs"),
            "forcing_contributors": regime.get("forcing_contributors"),
            "regime_probs": regime.get("regime_probs"),
            "role": regime.get("role") or "observed_realization",
            "source_class": regime.get("source_class") or "airport_temperature_path",
            "dedupe_policy": regime.get("dedupe_policy") or "",
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


def _write_temperature_trajectory_overlay_fields(
    payload: Dict[str, Any],
    meta: Mapping[str, Any],
    *,
    active: bool,
) -> None:
    strength = _to_float(meta.get("evidence"))
    strength_base = _to_float(meta.get("evidence_base"))
    strength_scale = _to_float(meta.get("evidence_scale"))
    if strength is None:
        strength = 0.0
    payload["temperature_trajectory_overlay_strength"] = float(strength)
    if strength_base is not None:
        payload["temperature_trajectory_overlay_strength_base"] = float(strength_base)
    else:
        payload.pop("temperature_trajectory_overlay_strength_base", None)
    if strength_scale is not None:
        payload["temperature_trajectory_overlay_strength_scale"] = float(strength_scale)
    else:
        payload.pop("temperature_trajectory_overlay_strength_scale", None)
    overlay = {
        "schema": "temperature_trajectory_overlay_strength_v1",
        "role": "metar_strong_likelihood_internal_diagnostic",
        "active": bool(active),
        "strength": float(strength),
        "strength_base": strength_base,
        "strength_scale": strength_scale,
        "source": meta.get("source"),
        "status": meta.get("status"),
        "na_reason": meta.get("na_reason"),
        "replacement": "layer_contributions.metar_strong_likelihood",
        "production_probability_allowed": False,
    }
    payload["temperature_trajectory_overlay"] = {
        key: value
        for key, value in overlay.items()
        if value not in (None, "")
    }


def _drop_temperature_trajectory_source_components(posterior: Any) -> None:
    if not isinstance(posterior, dict):
        return
    source_components = posterior.get("source_components")
    if not isinstance(source_components, dict):
        return

    def _is_temperature_trajectory_component(item: Any) -> bool:
        if not isinstance(item, Mapping):
            return False
        raw = item.get("raw") if isinstance(item.get("raw"), Mapping) else {}
        return bool(
            str(item.get("group") or "") == "temperature_trajectory"
            or str(item.get("source_id") or "") == "temperature_trajectory"
            or str(raw.get("source") or "") == "observed_temperature_trajectory_regime_v1"
        )

    components = source_components.get("components")
    if isinstance(components, list):
        source_components["components"] = [
            item for item in components if not _is_temperature_trajectory_component(item)
        ]
    groups = source_components.get("groups")
    if isinstance(groups, list):
        next_groups: List[Any] = []
        for group in groups:
            if _is_temperature_trajectory_component(group):
                continue
            if isinstance(group, dict):
                members = group.get("members")
                if isinstance(members, list):
                    group = dict(group)
                    group["members"] = [
                        member for member in members if not _is_temperature_trajectory_component(member)
                    ]
            next_groups.append(group)
        source_components["groups"] = next_groups


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
    strength = _to_float(payload.get("temperature_trajectory_overlay_strength"))
    if strength is None or strength <= 0.0:
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
    try:
        row = conn.execute(
            "SELECT 1 FROM event_fact_summary_cache WHERE fact_id = ? LIMIT 1",
            (int(fact_id),),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row)


def _upsert_summary_cache(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    created_at: str,
) -> Dict[str, Any]:
    if int(fact_id or 0) <= 0 or not isinstance(payload, Mapping):
        return {"ok": False, "block_reason": "invalid_fact_or_payload"}
    import weather_fact_snapshot_writer

    plan = weather_fact_snapshot_writer.build_fact_summary_cache_repair_plan(
        fact_id=int(fact_id),
        slug=str(row["slug"] or ""),
        event_date=str(row["event_date"] or ""),
        run_date=str(row["run_date"] or row["event_date"] or ""),
        fact_as_of=str(row["fact_as_of"] or ""),
        payload=payload,
        write_reason="ctp_materializer_summary_cache_repair",
        enable_apply_contract=True,
    )
    result = weather_fact_snapshot_writer.apply_fact_summary_cache_repair_plan(
        conn,
        plan,
        allow_apply=True,
        commit=False,
        created_at=created_at,
    )
    return dict(result)


def _posterior_probs(posterior: Mapping[str, Any]) -> Dict[str, float]:
    payload = {"posterior_temperature_distribution": posterior}
    return _ctp_probs(payload)


def _posterior_from_distribution_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    rows = payload.get("distribution")
    if not isinstance(rows, list):
        return {}
    raw_probs: List[Tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = _bucket_label(row)
        if not label:
            continue
        prob = None
        for key in ROW_PROBABILITY_FIELDS:
            prob = _to_float(row.get(key))
            if prob is not None:
                break
        if prob is None:
            continue
        raw_probs.append((label, max(0.0, float(prob))))
    total = sum(prob for _label, prob in raw_probs)
    if total <= 0.0:
        return {}
    bucket_probabilities = [
        {"outcome": label, "bucket": label, "probability": float(prob) / total}
        for label, prob in raw_probs
    ]
    return {
        "version": "continuous_temperature_posterior_v1_materialized_from_distribution",
        "target_variable": "settlement_daily_max_c",
        "status": "ok",
        "bucket_probabilities": bucket_probabilities,
        "cdf_values": [],
        "grid": [],
        "quantiles_c": {},
        "quantile_grid": [],
        "source_components": {},
        "calibration": {},
        "observation_constraints": {"d0_violation_count": 0},
        "fallback_source": "distribution_probability_fields",
    }


def _materialize_payload(payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    out = dict(payload)
    pre_refresh_regime_replay_reason = _posterior_regime_v1_replay_reason(out)
    peak_refreshed = _refresh_peak_estimator(out)
    regime_replay_reason = pre_refresh_regime_replay_reason or _posterior_regime_v1_replay_reason(out)
    regime_replay_needed = bool(regime_replay_reason)
    force_replay = bool(peak_refreshed or regime_replay_needed)
    posterior = _posterior_temperature_distribution(out, force_replay=force_replay)
    if not isinstance(posterior, Mapping) or not REQUIRED_POSTERIOR_KEYS.issubset(posterior.keys()):
        fallback_posterior = _posterior_from_distribution_payload(out)
        if fallback_posterior:
            posterior = fallback_posterior
            out["ctp_replay_reason"] = out.get("ctp_replay_reason") or "distribution_probability_fields_fallback"
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
        _write_temperature_trajectory_overlay_fields(out, trajectory_meta, active=True)
        for suffix in ("evidence", "evidence_base", "evidence_scale"):
            out.pop(f"q_bayes_temperature_trajectory_{suffix}", None)
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
        _drop_temperature_trajectory_source_components(out.get("posterior_temperature_distribution"))
        for key in list(out.keys()):
            if str(key).startswith("q_bayes_temperature_trajectory_"):
                out.pop(key, None)
        inactive_meta = _inactive_temperature_trajectory_meta(out)
        _write_temperature_trajectory_overlay_fields(out, inactive_meta, active=False)
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


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        if isinstance(row, Mapping):
            return row.get(key, default)
        return default


def _strip_stale_writer_metadata(payload: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(payload or {})
    for key in (
        "fact_snapshot_id",
        "source_observations",
        "source_acquisition_trace",
        "source_latency_trace",
        "fact_layer_state",
        "db_write_guard",
        "lineage_state",
        "phase0_fact_json_contract",
    ):
        out.pop(key, None)
    return out


def _materializer_source_trace(
    *,
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    parent_raw_json: str,
    created_at: str,
) -> Dict[str, Any]:
    fact_as_of = str(_row_value(row, "fact_as_of") or payload.get("fact_as_of") or "").strip()
    event_date = str(_row_value(row, "event_date") or payload.get("event_date") or "").strip()
    station_id = str(payload.get("station_id") or "ZUUU").strip()
    raw_sha256 = hashlib.sha256(str(parent_raw_json or "").encode("utf-8")).hexdigest()
    raw_ref = f"event_fact_history:{_row_value(row, 'id')}"
    source_payload = {
        "source_payload_schema": "ctp_materializer_source_payload_v1",
        "parent_fact_id": _row_value(row, "id"),
        "parent_fact_as_of": fact_as_of,
        "write_reason": "ctp_latest_materialization",
    }
    observation_obj = build_source_observation(
        source_id=CTP_MATERIALIZER_SOURCE_ID,
        station_id=station_id,
        event_date=event_date,
        valid_time=fact_as_of or created_at,
        fetched_at=created_at,
        response_completed_at=created_at,
        raw_ref=raw_ref,
        raw_payload=parent_raw_json,
        parser_version="ctp_materializer_source_trace_v1",
        overrides={
            "source_class": "unknown_source",
            "role": "derived_posterior_materialization",
            "hard_eligible": False,
            "demotion_reason": "derived_materialization_not_weather_observation",
        },
        payload=source_payload,
    )
    observation = observation_obj.as_dict(include_payload=True)
    source_event_id = observation_obj.source_event_id
    source_observation_id = observation_obj.source_observation_id
    return {
        "schema": "ctp_materializer_source_trace_v1",
        "read_only": True,
        "trace_built_at": created_at,
        "station_id": station_id,
        "event_date": event_date,
        "source_observations": [observation],
        "source_event_ids": [source_event_id],
        "source_observation_ids": [source_observation_id],
        "source_latency_trace": [
            {
                "source_event_id": source_event_id,
                "source_observation_id": source_observation_id,
                "source_id": CTP_MATERIALIZER_SOURCE_ID,
                "source_class": "unknown_source",
                "role": "derived_posterior_materialization",
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": fact_as_of or created_at,
                "fetched_at": created_at,
                "raw_ref": raw_ref,
                "raw_sha256": raw_sha256,
                "hard_eligible": False,
                "demotion_reason": "derived_materialization_not_weather_observation",
            }
        ],
        "source_class_counts": {"unknown_source": 1},
        "hard_observation_candidate_ids": [],
        "diagnostic_current_ids": [],
        "forecast_weak_ids": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "historical_backfill": False,
            "restarts_launchd": False,
        },
        "next_boundary": "weather_fact_snapshot_writer_apply_contract",
    }


def build_ctp_materialized_fact_snapshot_writer_apply_plan(
    *,
    row: Mapping[str, Any],
    original_payload: Mapping[str, Any],
    materialized_payload: Mapping[str, Any],
    parent_raw_json: str,
    created_at: str = "",
) -> Dict[str, Any]:
    import weather_fact_snapshot_writer

    created_at_text = str(created_at or datetime.now(timezone.utc).isoformat())
    slug = str(_row_value(row, "slug") or materialized_payload.get("slug") or "").strip()
    event_date = str(_row_value(row, "event_date") or materialized_payload.get("event_date") or "").strip()
    run_date = str(_row_value(row, "run_date") or materialized_payload.get("run_date") or event_date).strip()
    fact_as_of = str(_row_value(row, "fact_as_of") or materialized_payload.get("fact_as_of") or created_at_text).strip()
    payload_for_writer = _strip_stale_writer_metadata(materialized_payload)
    payload_for_writer.update(
        {
            "slug": slug,
            "event_date": event_date,
            "run_date": run_date,
            "fact_as_of": fact_as_of,
            "composer_version": CTP_MATERIALIZER_COMPOSER_VERSION,
            "parent_composer_version": str(_row_value(row, "composer_version") or ""),
            "parent_fact_id": _row_value(row, "id"),
            "parent_fact_as_of": fact_as_of,
            "ctp_materializer_state": {
                "schema": "ctp_materializer_state_v1",
                "write_owner": "weather_fact_snapshot_writer",
                "write_reason": "ctp_latest_materialization",
                "source_id": CTP_MATERIALIZER_SOURCE_ID,
                "parent_fact_id": _row_value(row, "id"),
                "parent_fact_as_of": fact_as_of,
                "fact_as_of_preserved_from_parent": True,
                "created_at_represents_write_time_only": True,
                "historical_backfill": False,
            },
        }
    )
    source_trace = _materializer_source_trace(
        row=row,
        payload=original_payload,
        parent_raw_json=parent_raw_json,
        created_at=created_at_text,
    )
    return weather_fact_snapshot_writer.build_fact_snapshot_write_plan(
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        fact_as_of=fact_as_of,
        incoming_payload=payload_for_writer,
        current_fact_id=int(_row_value(row, "id") or 0) or None,
        current_fact_as_of=fact_as_of,
        current_payload=original_payload,
        source_acquisition_trace=source_trace,
        composer_version=CTP_MATERIALIZER_COMPOSER_VERSION,
        write_reason="ctp_latest_materialization",
        posterior_run_id=str(
            original_payload.get("posterior_run_id")
            or (
                original_payload.get("lineage_state", {}).get("posterior_run_id")
                if isinstance(original_payload.get("lineage_state"), Mapping)
                else ""
            )
            or ""
        ),
        include_payload=True,
        enable_apply_contract=True,
        write_latest=True,
    )


def _date_key(value: Any) -> str:
    return str(value or "").strip().split("T", 1)[0]


def _is_historical_event_date(event_date: Any, *, today_key: str) -> bool:
    event_key = _date_key(event_date)
    today = _date_key(today_key)
    return bool(event_key and today and event_key < today)


@contextmanager
def _connect_run_db(db_path: Path, *, apply: bool, timeout_sec: float):
    timeout_val = max(1.0, float(timeout_sec or 30.0))
    busy_ms = int(max(1000.0, timeout_val * 1000.0))
    if apply:
        with connect_archive_db_rw(
            db_path,
            owner="materialize_ctp_latest_facts.apply",
            timeout_sec=timeout_val,
            busy_timeout_ms=busy_ms,
            row_factory=True,
        ) as conn:
            yield conn
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout_val)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        yield conn
    finally:
        conn.close()


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
    allow_historical: bool = False,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    with _connect_run_db(db_path, apply=bool(apply), timeout_sec=float(timeout_sec or 30.0)) as conn:
        rows = _candidate_rows(conn, slug_like, limit)
        report: Dict[str, Any] = {
            "ok": True,
            "apply": bool(apply),
            "allow_historical": bool(allow_historical),
            "today_et": today_et,
            "db_path": str(db_path),
            "slug_like": slug_like,
            "scanned_latest_facts": len(rows),
            "already_complete": 0,
            "would_append_event_fact_history_rows": 0,
            "applied_event_fact_history_rows": 0,
            "would_upsert_event_fact_summary_cache_rows": 0,
            "applied_event_fact_summary_cache_rows": 0,
            "historical_apply_blocked": 0,
            "items": [],
        }
        for row in rows:
            raw_fact_json = row["fact_json"]
            payload = _json_obj(raw_fact_json)
            if not payload or not isinstance(payload.get("distribution"), list):
                continue
            historical_event = _is_historical_event_date(row["event_date"], today_key=today_et)
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
                        "historical_event": bool(historical_event),
                    }
                    if apply and historical_event and not allow_historical:
                        item["summary_cache_upsert_blocked_reason"] = "historical_apply_requires_explicit_allow"
                        report["historical_apply_blocked"] += 1
                        report["ok"] = False
                        report["items"].append(item)
                        continue
                    if apply:
                        summary_result = _upsert_summary_cache(
                            conn,
                            fact_id=int(row["id"]),
                            row=row,
                            payload=payload,
                            created_at=now,
                        )
                        if summary_result.get("ok") and summary_result.get("summary_cache_written"):
                            item["summary_cache_upserted"] = True
                            item["writer_apply_result_schema"] = summary_result.get("schema")
                            item["write_owner"] = summary_result.get("write_owner")
                            report["applied_event_fact_summary_cache_rows"] += 1
                        else:
                            item["summary_cache_upsert_blocked_reason"] = str(
                                summary_result.get("block_reason")
                                or summary_result.get("error")
                                or "summary_cache_repair_writer_apply_failed"
                            )
                            report["ok"] = False
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
            if historical_event and not allow_historical:
                item["apply_blocked_reason"] = "historical_apply_requires_explicit_allow"
                item["historical_event"] = True
                report["historical_apply_blocked"] += 1
                report["ok"] = False
                continue
            plan = build_ctp_materialized_fact_snapshot_writer_apply_plan(
                row=row,
                original_payload=payload,
                materialized_payload=next_payload,
                parent_raw_json=str(raw_fact_json or ""),
                created_at=now,
            )
            import weather_fact_snapshot_writer

            apply_result = weather_fact_snapshot_writer.apply_fact_snapshot_write_plan(
                conn,
                plan,
                allow_apply=True,
                commit=False,
                created_at=now,
            )
            apply_summary = weather_fact_snapshot_writer.compact_fact_snapshot_writer_apply_result_for_lineage(
                apply_result
            )
            item["writer_apply_result_summary"] = apply_summary
            if not apply_result.get("ok"):
                item["apply_blocked_reason"] = str(
                    apply_result.get("block_reason") or apply_result.get("error") or "writer_apply_failed"
                )
                report["ok"] = False
                continue
            new_fact_id = int(apply_result.get("fact_id") or 0)
            if apply_result.get("summary_cache_written"):
                item["summary_cache_upserted"] = True
                report["applied_event_fact_summary_cache_rows"] += 1
            report["would_upsert_event_fact_summary_cache_rows"] += 1
            item["new_fact_id"] = new_fact_id
            item["writer_apply_result_schema"] = apply_result.get("schema")
            item["write_owner"] = apply_result.get("write_owner")
            item["latest_repointed"] = bool(apply_result.get("latest_repointed"))
            item["post_apply_persisted_verification_ok"] = bool(
                apply_summary.get("post_apply_persisted_verification_ok")
            )
            report["applied_event_fact_history_rows"] += 1
        if apply:
            conn.commit()
        return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug-like", default="%temperature%")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--allow-historical",
        action="store_true",
        help="Allow --apply to write materialized facts for event_date before ET today",
    )
    args = parser.parse_args()
    report = run(
        Path(args.db),
        apply=bool(args.apply),
        slug_like=str(args.slug_like),
        limit=int(args.limit) if int(args.limit or 0) > 0 else None,
        timeout_sec=float(args.timeout_sec or 30.0),
        allow_historical=bool(args.allow_historical),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
