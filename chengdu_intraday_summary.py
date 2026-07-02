#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

try:
    from bayesian_likelihood_runtime import precipitation_evidence_profile as _runtime_precipitation_evidence_profile
except Exception:  # pragma: no cover - runtime helper is optional in isolated tests.
    _runtime_precipitation_evidence_profile = None

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "archive.db"
CHENGDU_SLUG_LIKE = "%chengdu%"
CHENGDU_TZ = timezone(timedelta(hours=8))
TEMPERATURE_MOMENTUM_WINDOWS: Tuple[str, ...] = (
    "24h",
    "12h",
    "6h",
    "3h",
    "2h",
    "1h",
    "45m",
    "30m",
    "15m",
    "10m",
    "5m",
    "3m",
    "2m",
    "1m",
)
TEMPERATURE_MOMENTUM_HOURS: Dict[str, float] = {
    "24h": 24.0,
    "12h": 12.0,
    "6h": 6.0,
    "3h": 3.0,
    "2h": 2.0,
    "1h": 1.0,
    "45m": 45.0 / 60.0,
    "30m": 30.0 / 60.0,
    "15m": 15.0 / 60.0,
    "10m": 10.0 / 60.0,
    "5m": 5.0 / 60.0,
    "3m": 3.0 / 60.0,
    "2m": 2.0 / 60.0,
    "1m": 1.0 / 60.0,
}
TEMPERATURE_MOMENTUM_WEIGHTS: Dict[str, float] = {
    "1h": 0.48,
    "45m": 0.12,
    "30m": 0.12,
    "15m": 0.08,
    "10m": 0.07,
    "5m": 0.05,
    "3m": 0.04,
    "2m": 0.025,
    "1m": 0.015,
    "2h": 0.0,
    "3h": 0.0,
    "6h": 0.0,
    "12h": 0.0,
    "24h": 0.0,
}
TEMPERATURE_MOMENTUM_RECENT_WINDOWS: Tuple[str, ...] = ("1m", "2m", "3m", "5m", "10m", "15m", "30m", "45m", "1h")
TEMPERATURE_MOMENTUM_POSTERIOR_CONFIRM_WINDOWS: Tuple[str, ...] = ("30m", "45m", "1h")
TEMPERATURE_RISE_RATE_STABLE_WINDOWS: Tuple[str, ...] = (
    "1h",
    "45m",
    "30m",
    "15m",
    "10m",
    "5m",
    "3m",
    "2m",
    "1m",
    "2h",
    "3h",
    "6h",
    "12h",
    "24h",
)
TEMPERATURE_RISE_RATE_MIN_LAST_PAIR_HOURS = 10.0 / 60.0
TEMPERATURE_RISE_RATE_UNTRUSTED_ABS_MAX = 6.0


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _temperature_rise_rate_window(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in TEMPERATURE_MOMENTUM_HOURS or text == "last_pair" else ""


def _temperature_momentum_posterior_gate(
    rates: Mapping[str, Any],
    *,
    state: str,
    effective: Any,
) -> Dict[str, Any]:
    confirmed = [
        label
        for label in TEMPERATURE_MOMENTUM_POSTERIOR_CONFIRM_WINDOWS
        if _to_float(rates.get(label)) is not None and float(rates[label]) >= 0.25
    ]
    positive_short = [
        label
        for label in ("1m", "2m", "3m", "5m", "10m", "15m")
        if _to_float(rates.get(label)) is not None and float(rates[label]) >= 0.35
    ]
    one_hour_core = _to_float(rates.get("1h"))
    core_context_windows = [
        label
        for label in ("2h", "3h", "6h")
        if _to_float(rates.get(label)) is not None and float(rates[label]) >= 0.35
    ]
    eff = _to_float(effective)
    stable_confirmed_allowed = bool(
        eff is not None
        and eff >= 0.20
        and (
            len(confirmed) >= 3
            or (
                len(confirmed) >= 2
                and len(core_context_windows) >= 2
            )
        )
        and str(state or "") not in {"cooling", "not_realized", "flat", "physically_limited_warming"}
    )
    one_hour_core_allowed = bool(
        eff is not None
        and eff >= 0.35
        and one_hour_core is not None
        and one_hour_core >= 0.50
        and bool(core_context_windows)
        and str(state or "") in {"sustained_warming", "emerging_warming"}
    )
    allowed = bool(
        (
            eff is not None
            and eff >= 0.25
            and len(confirmed) >= 2
            and str(state or "") in {"sustained_warming", "emerging_warming", "weak_warming"}
        )
        or stable_confirmed_allowed
        or one_hour_core_allowed
    )
    if allowed:
        gate_state = "confirmed"
        if stable_confirmed_allowed:
            reason = "stable_multi_window_confirmed"
        elif one_hour_core_allowed:
            reason = "one_hour_core_confirmed"
        else:
            reason = "multi_window_confirmed"
    elif positive_short and len(confirmed) < 2:
        gate_state = "short_window_watch"
        reason = "short_window_warming_requires_30m_1h_confirmation"
    elif str(state or "") in {"cooling", "not_realized", "flat", "physically_limited_warming"}:
        gate_state = "not_warming"
        reason = f"momentum_state_{state}"
    else:
        gate_state = "unconfirmed"
        reason = "multi_window_confirmation_missing"
    return {
        "allowed": allowed,
        "state": gate_state,
        "reason": reason,
        "confirmed_windows": confirmed,
        "confirmed_positive_window_count": len(confirmed),
        "positive_short_windows": positive_short,
        "one_hour_core_rate_c_per_hour": _round(one_hour_core, 2),
        "core_context_windows": core_context_windows,
    }


def _trusted_temperature_rise_rate(features: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(features, Mapping):
        return {
            "value": None,
            "trusted": False,
            "source": "",
            "window": "",
            "na_reason": "weather_likelihood_features_missing",
        }
    selected_rates, selected_meta = _selected_temperature_momentum_rates(features)
    for selected_window in ("1h", "45m", "30m", "15m", "10m", "5m", "3m", "2m", "1m"):
        selected_rate = _to_float(selected_rates.get(selected_window))
        if selected_rate is not None:
            return {
                "value": float(selected_rate),
                "trusted": True,
                "source": f"{selected_meta.get('core_source') or selected_meta.get('source') or 'temperature_momentum'}:{selected_window}",
                "window": selected_window,
                "na_reason": "",
            }
    rate = _to_float(features.get("temperature_rise_rate_c_per_hour"))
    source = str(features.get("temperature_rise_rate_source") or "").strip()
    window = _temperature_rise_rate_window(features.get("temperature_rise_rate_window"))
    raw_pair_hours = _to_float(features.get("temperature_rise_rate_raw_pair_actual_hours"))
    if not window and ":" in source:
        window = _temperature_rise_rate_window(source.rsplit(":", 1)[-1])
    if rate is not None:
        if window in TEMPERATURE_RISE_RATE_STABLE_WINDOWS:
            return {
                "value": float(rate),
                "trusted": True,
                "source": source,
                "window": window,
                "na_reason": "",
            }
        if window == "last_pair" and raw_pair_hours is not None and raw_pair_hours >= TEMPERATURE_RISE_RATE_MIN_LAST_PAIR_HOURS:
            return {
                "value": float(rate),
                "trusted": True,
                "source": source,
                "window": window,
                "na_reason": "",
            }
        if (
            not window
            and not source
            and raw_pair_hours is None
            and abs(float(rate)) <= TEMPERATURE_RISE_RATE_UNTRUSTED_ABS_MAX
        ):
            return {
                "value": float(rate),
                "trusted": True,
                "source": "legacy_fact_without_window",
                "window": "",
                "na_reason": "",
            }
    stable_rate = _to_float(features.get("temperature_trajectory_stable_rate_c_per_hour"))
    stable_window = _temperature_rise_rate_window(features.get("temperature_trajectory_stable_rate_window"))
    stable_source = str(features.get("temperature_trajectory_stable_rate_source") or "").strip()
    if stable_rate is not None and stable_window in TEMPERATURE_RISE_RATE_STABLE_WINDOWS:
        return {
            "value": float(stable_rate),
            "trusted": True,
            "source": stable_source,
            "window": stable_window,
            "na_reason": "",
        }
    upstream_reason = str(features.get("temperature_rise_rate_na_reason") or "").strip()
    if upstream_reason:
        reason = upstream_reason
    elif rate is not None:
        reason = "unstable_temperature_rise_rate_suppressed"
    else:
        reason = "stable_window_missing"
    return {
        "value": None,
        "trusted": False,
        "source": source or stable_source,
        "window": window or stable_window,
        "na_reason": reason,
    }


def _temperature_rise_rate_value(features: Mapping[str, Any]) -> Optional[float]:
    return _to_float(_trusted_temperature_rise_rate(features).get("value"))


def _pct(value: Any, decimals: int = 2) -> Optional[float]:
    num = _to_float(value)
    if num is None:
        return None
    return round(num * 100.0, decimals)


def _clamp_probability(value: Any) -> Optional[float]:
    num = _to_float(value)
    if num is None:
        return None
    return max(0.0, min(1.0, float(num)))


def _bayes_chain_for_distribution_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    components_raw = row.get("q_temperature_bucket_score_components")
    components = dict(components_raw) if isinstance(components_raw, Mapping) else {}
    prior_strength = _to_float(row.get("q_bayes_prior_strength"))
    evidence_keys = (
        "q_bayes_model_evidence",
        "q_bayes_wu_forecast_evidence",
        "q_bayes_taf_peak_evidence",
        "q_bayes_observed_nowcast_evidence",
        "q_bayes_temperature_trajectory_evidence",
        "q_bayes_independent_physics_residual_evidence",
        "q_bayes_climo_regime_evidence",
        "q_bayes_post_cool_rebound_regime_prior_evidence",
        "q_bayes_post_cool_rebound_intraday_likelihood_evidence",
        "q_bayes_post_cool_rebound_risk_evidence",
    )
    if prior_strength is None or prior_strength <= 0:
        alpha_total = _to_float(row.get("q_temperature_bucket_alpha_total"))
        evidence_total = sum(
            float(value)
            for value in (_to_float(row.get(key)) for key in evidence_keys)
            if value is not None
        )
        if alpha_total is not None:
            inferred = float(alpha_total) - evidence_total
            if inferred > 0:
                prior_strength = inferred

    history_component = _to_float(components.get("history"))
    prior_prob = _clamp_probability(
        row.get("q_prior")
        if row.get("q_prior") is not None
        else row.get("q_bayes_prior_prob")
        if row.get("q_bayes_prior_prob") is not None
        else row.get("q_temperature_bucket_prior")
    )
    if prior_prob is None and history_component is not None and prior_strength is not None and prior_strength > 0:
        prior_prob = _clamp_probability(float(history_component) / float(prior_strength))

    likelihood_specs = (
        (
            "model",
            "q_bayes_model_evidence",
            ("q_bayes_model_prob", "q_ensemble_qm_bucket", "q_model_temperature_bucket_round"),
        ),
        ("wu_forecast", "q_bayes_wu_forecast_evidence", ("q_bayes_wu_forecast_prob",)),
        ("taf_peak", "q_bayes_taf_peak_evidence", ("q_bayes_taf_peak_prob",)),
        (
            "observed_nowcast",
            "q_bayes_observed_nowcast_evidence",
            ("q_bayes_observed_nowcast_prob", "q_bayes_nowcast_prob"),
        ),
        (
            "temperature_trajectory",
            "q_bayes_temperature_trajectory_evidence",
            ("q_bayes_temperature_trajectory_prob", "q_temperature_trajectory"),
        ),
        (
            "independent_physics_residual",
            "q_bayes_independent_physics_residual_evidence",
            (
                "q_bayes_independent_physics_residual_prob",
                "q_bayes_independent_physics_residual_observed_prob",
                "q_bayes_physical_likelihood_prob",
                "q_bayes_physical_likelihood_observed_prob",
            ),
        ),
        ("climo_regime", "q_bayes_climo_regime_evidence", ("q_bayes_climo_regime_prob",)),
        (
            "post_cool_rebound_regime_prior",
            "q_bayes_post_cool_rebound_regime_prior_evidence",
            ("q_bayes_post_cool_rebound_regime_prior_prob",),
        ),
        (
            "post_cool_rebound_intraday_likelihood",
            "q_bayes_post_cool_rebound_intraday_likelihood_evidence",
            ("q_bayes_post_cool_rebound_intraday_likelihood_prob",),
        ),
    )
    likelihood_component_total = 0.0
    likelihood_evidence_total = 0.0
    likelihood_parts: Dict[str, Dict[str, Any]] = {}
    for name, evidence_key, prob_keys in likelihood_specs:
        component = _to_float(components.get(name))
        evidence = _to_float(row.get(evidence_key))
        prob: Optional[float] = None
        for prob_key in prob_keys:
            prob = _clamp_probability(row.get(prob_key))
            if prob is not None:
                break
        if component is None and evidence is not None and evidence > 0 and prob is not None:
            component = float(prob) * float(evidence)
        if component is not None:
            likelihood_component_total += float(component)
        if evidence is not None and evidence > 0:
            likelihood_evidence_total += float(evidence)
        part: Dict[str, Any] = {}
        if evidence is not None:
            part["evidence"] = float(evidence)
        if component is not None:
            part["component"] = float(component)
            if prob is None and evidence is not None and evidence > 0:
                prob = _clamp_probability(float(component) / float(evidence))
        if prob is not None:
            part["prob"] = float(prob)
        if part:
            likelihood_parts[name] = part

    likelihood_prob = _clamp_probability(
        row.get("q_likelihood")
        if row.get("q_likelihood") is not None
        else row.get("q_bayes_likelihood_prob")
    )
    if likelihood_evidence_total > 0:
        likelihood_prob = _clamp_probability(likelihood_component_total / likelihood_evidence_total)

    if not (
        prior_prob is not None
        or likelihood_prob is not None
        or likelihood_parts
        or components
        or prior_strength is not None
    ):
        return {}
    return {
        "prior_prob": prior_prob,
        "likelihood_prob": likelihood_prob,
        "prior_component": history_component,
        "likelihood_component": likelihood_component_total,
        "prior_strength": prior_strength,
        "likelihood_evidence": likelihood_evidence_total,
        "alpha": _to_float(row.get("q_temperature_bucket_alpha")),
        "alpha_total": _to_float(row.get("q_temperature_bucket_alpha_total")),
        "components": components,
        "likelihood_parts": likelihood_parts,
        "source": str(row.get("q_bayes_source") or ""),
    }


def _round(value: Any, decimals: int = 2) -> Optional[float]:
    num = _to_float(value)
    if num is None:
        return None
    return round(num, decimals)


def _observed_path_temperature_c(observed_current_c: Any, observed_today_max_c: Any) -> Tuple[Optional[float], str]:
    current = _to_float(observed_current_c)
    today_max = _to_float(observed_today_max_c)
    if current is not None and today_max is not None:
        if float(today_max) > float(current) + 0.01:
            return float(today_max), "observed_today_max_floor"
        return float(current), "observed_current_temp"
    if today_max is not None:
        return float(today_max), "observed_today_max"
    if current is not None:
        return float(current), "observed_current_temp"
    return None, "missing"


def _observed_current_temperature_c(features: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    if not isinstance(features, Mapping):
        return None, "missing"
    source_hint = str(features.get("observed_temp_source") or "").strip()
    demoted_wu_current = bool(
        _source_is_wu_twc_intraday(source_hint)
        and _wu_twc_intraday_demote_reason(features)
    )
    awc_date_mismatch = (
        _to_bool(features.get("zuuu_metar_raw_awc_date_mismatch")) is True
        or _to_bool(features.get("metar_observation_date_mismatch")) is True
    )
    wu_current_mismatch = "event_date_mismatch" in str(
        features.get("wu_twc_observed_intraday_current_error")
        or features.get("wu_twc_observed_intraday_max_error")
        or ""
    )
    candidates = [
        ("observed_current_temp_c", features.get("observed_current_temp_c"), "observed_current_temp", not demoted_wu_current),
        ("observed_temp_c", features.get("observed_temp_c"), source_hint or "observed_temp_c", not demoted_wu_current),
        (
            "zuuu_metar_raw_awc_latest_temp_c",
            features.get("zuuu_metar_raw_awc_latest_temp_c"),
            "zuuu_metar_raw_awc_latest_temp",
            not awc_date_mismatch,
        ),
        ("metar_temperature_c", features.get("metar_temperature_c"), "metar_temperature_c", not awc_date_mismatch),
        (
            "wu_twc_observed_intraday_current_temp_c",
            features.get("wu_twc_observed_intraday_current_temp_c"),
            "wu_twc_observed_intraday_current",
            not wu_current_mismatch,
        ),
    ]
    for _key, value, source, allowed in candidates:
        if not allowed:
            continue
        current = _to_float(value)
        if current is not None:
            return float(current), source
    return None, "missing"


def _source_is_wu_twc_intraday(source: Any) -> bool:
    text = str(source or "").strip().lower()
    return text.startswith("wu_twc_observed_intraday") or "wunderground" in text or "weather_com" in text


def _flatten_text_tokens(value: Any) -> List[str]:
    if isinstance(value, Mapping):
        out: List[str] = []
        for item in value.values():
            out.extend(_flatten_text_tokens(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_flatten_text_tokens(item))
        return out
    text = str(value or "").strip().lower()
    return [text] if text else []


def _wu_twc_intraday_demote_reason(features: Mapping[str, Any]) -> str:
    if not isinstance(features, Mapping):
        return ""
    alert = features.get("weather_source_delay_deformation_alert")
    alert_map = alert if isinstance(alert, Mapping) else {}
    alert_text = " ".join(_flatten_text_tokens(alert_map))
    if _to_bool(alert_map.get("alert")) and (
        "wu" in alert_text
        or "twc" in alert_text
        or "nowcast" in alert_text
        or "temperature_deformation" in alert_text
        or "temperature_mismatch" in alert_text
    ):
        return "wu_twc_intraday_deformation_alert"
    field_text = " ".join(
        token
        for key in (
            "weather_source_alert_codes",
            "wu_twc_observed_intraday_current_error",
            "wu_twc_observed_intraday_max_error",
            "wu_twc_observed_intraday_history_error",
            "wu_twc_observed_intraday_date_mismatch_reason",
            "wu_twc_observed_intraday_event_date_mismatch_reason",
            "observed_today_max_hard_source_demoted_reason",
        )
        for token in _flatten_text_tokens(features.get(key))
    )
    if "current_observation_event_date_mismatch" in field_text or "event_date_mismatch" in field_text:
        return "wu_twc_intraday_event_date_mismatch"
    if any(
        _to_bool(features.get(key))
        for key in (
            "wu_twc_observed_intraday_date_mismatch",
            "wu_twc_observed_intraday_event_date_mismatch",
            "wu_twc_observed_intraday_current_observation_event_date_mismatch",
        )
    ):
        return "wu_twc_intraday_event_date_mismatch"
    return ""


def _hard_observed_today_max_c(features: Mapping[str, Any]) -> Tuple[Optional[float], str, str]:
    if not isinstance(features, Mapping):
        return None, "", "features_missing"
    awc_mismatch = (
        _to_bool(features.get("zuuu_metar_raw_awc_date_mismatch")) is True
        or _to_bool(features.get("metar_observation_date_mismatch")) is True
    )
    awc_max = _to_float(features.get("zuuu_metar_raw_awc_today_max_c"))
    if awc_max is not None and not awc_mismatch:
        return float(awc_max), "zuuu_metar_raw_awc", ""
    generic = _to_float(features.get("observed_today_max_c"))
    generic_source = str(features.get("observed_today_max_source") or "").strip()
    if generic is None:
        return None, "", "observed_today_max_missing"
    if _source_is_wu_twc_intraday(generic_source):
        demote = _wu_twc_intraday_demote_reason(features)
        if demote:
            return None, generic_source, demote
        return None, generic_source, "wu_twc_intraday_auxiliary_not_hard_source"
    if features.get("observed_today_max_trusted") is False:
        return None, generic_source, "observed_today_max_untrusted"
    return float(generic), generic_source or "observed_today_max_c", ""


def _selected_temperature_momentum_rates(features: Mapping[str, Any]) -> Tuple[Dict[str, float], Dict[str, Any]]:
    generic_rates: Dict[str, float] = {}
    awc_rates: Dict[str, float] = {}
    if not isinstance(features, Mapping):
        return {}, {"source": "", "core_source": "", "supplement_source": "", "wu_twc_supplement_demoted_reason": ""}
    for label in TEMPERATURE_MOMENTUM_WINDOWS:
        generic = _to_float(features.get(f"temperature_momentum_{label}_c_per_hour"))
        if generic is not None:
            generic_rates[label] = float(generic)
        awc = _to_float(features.get(f"zuuu_metar_raw_awc_temperature_momentum_{label}_c_per_hour"))
        if awc is not None:
            awc_rates[label] = float(awc)
    source = str(features.get("temperature_momentum_source") or "").strip()
    if source == "zuuu_metar_raw_awc":
        for label, rate in generic_rates.items():
            awc_rates.setdefault(label, rate)
    demote = _wu_twc_intraday_demote_reason(features)
    selected: Dict[str, float] = {}
    supplement_rates: Dict[str, float] = {}
    if awc_rates:
        if "1h" in awc_rates:
            selected["1h"] = awc_rates["1h"]
        if not demote:
            for label in ("45m", "30m", "15m", "10m", "5m", "3m", "2m", "1m"):
                if label in generic_rates and label not in selected:
                    selected[label] = generic_rates[label]
                    supplement_rates[label] = generic_rates[label]
        return selected or awc_rates, {
            "source": "zuuu_metar_raw_awc_core",
            "core_source": "zuuu_metar_raw_awc",
            "supplement_source": source if supplement_rates else "",
            "supplement_rates_c_per_hour": supplement_rates,
            "context_rates_c_per_hour": {
                label: awc_rates[label]
                for label in ("24h", "12h", "6h", "3h", "2h")
                if label in awc_rates
            },
            "wu_twc_supplement_demoted_reason": demote,
        }
    return {
        label: generic_rates[label]
        for label in TEMPERATURE_MOMENTUM_WINDOWS
        if label in generic_rates
    }, {
        "source": source or "temperature_momentum",
        "core_source": source or "temperature_momentum",
        "supplement_source": "",
        "supplement_rates_c_per_hour": {},
        "context_rates_c_per_hour": {},
        "wu_twc_supplement_demoted_reason": demote,
    }


def _temperature_momentum_rate_fields(rates: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    return {
        f"temperature_momentum_{label}_c_per_hour": _round(rates.get(label), 2)
        for label in TEMPERATURE_MOMENTUM_WINDOWS
    }


def _temperature_momentum_metric_fields(rates: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    return {
        f"momentum_{label}_c_per_hour": _round(rates.get(label), 2)
        for label in TEMPERATURE_MOMENTUM_WINDOWS
    }


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _chengdu_seasonal_peak_hour_local(event_date: Any) -> float:
    text = str(event_date or "").strip()
    month = None
    match = re.match(r"^\d{4}-(\d{2})-\d{2}", text)
    if match:
        try:
            month = int(match.group(1))
        except ValueError:
            month = None
    table = {
        1: 14.5,
        2: 15.0,
        3: 15.0,
        4: 15.5,
        5: 15.75,
        6: 16.0,
        7: 16.0,
        8: 16.0,
        9: 15.75,
        10: 15.25,
        11: 15.0,
        12: 14.5,
    }
    return float(table.get(month or 0, 16.0))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


FORECAST_PEAK_TIME_KEYS: Tuple[str, ...] = (
    "wu_forecast_peak_at",
    "weather_com_forecast_peak_at",
    "twc_forecast_peak_at",
    "taf_forecast_peak_at",
    "wu_forecast_peak_valid_local",
)


def _event_date_key(value: Any) -> str:
    return str(value or "").strip().split("T", 1)[0]


def _parse_forecast_peak_datetime(value: Any, *, key: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        default_tz = CHENGDU_TZ if key.endswith("_local") or "valid_local" in key else timezone.utc
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def _peak_hour_distribution_from_features(
    features: Mapping[str, Any],
    *,
    event_date: Any = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    event_key = _event_date_key(event_date or features.get("metar_target_event_date"))
    raw = features.get("expected_peak_hour_distribution") or features.get("peak_hour_distribution")
    items: List[Tuple[float, float]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            hour = _to_float(key)
            prob = _to_float(value)
            if hour is not None and prob is not None and float(prob) > 0:
                items.append((max(0.0, min(23.99, float(hour))), float(prob)))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            valid_local = str(row.get("valid_time_local") or row.get("expected_peak_at_local") or "").strip()
            if event_key and valid_local:
                dt = _parse_forecast_peak_datetime(valid_local, key="expected_peak_at_local")
                if dt is not None and dt.astimezone(CHENGDU_TZ).date().isoformat() != event_key:
                    continue
            hour = _to_float(row.get("hour_local") or row.get("hour") or row.get("peak_hour"))
            prob = _to_float(row.get("prob") or row.get("probability") or row.get("weight"))
            if hour is not None and prob is not None and float(prob) > 0:
                items.append((max(0.0, min(23.99, float(hour))), float(prob)))
    total = sum(prob for _, prob in items)
    if not items or total <= 0:
        return None, {}
    center = sum(hour * prob for hour, prob in items) / total
    start = _to_float(features.get("expected_peak_window_start_hour_local") or features.get("peak_window_start_hour_local"))
    end = _to_float(features.get("expected_peak_window_end_hour_local") or features.get("peak_window_end_hour_local"))
    if start is None:
        start = min(hour for hour, _ in items)
    if end is None:
        end = max(hour for hour, _ in items)
    return max(0.0, min(23.99, float(center))), {
        "expected_peak_hour_local": _round(center, 2),
        "expected_peak_hour_source": str(features.get("expected_peak_hour_source") or "expected_peak_hour_distribution"),
        "expected_peak_at_local": str(features.get("expected_peak_at_local") or ""),
        "expected_peak_window_start_hour_local": _round(start, 2),
        "expected_peak_window_end_hour_local": _round(end, 2),
        "expected_peak_window_source": str(features.get("expected_peak_window_source") or "expected_peak_hour_distribution"),
        "expected_peak_hour_distribution": [
            {"hour_local": float(hour), "prob": float(prob) / total}
            for hour, prob in items
        ],
    }


def _forecast_peak_hour_from_features(
    features: Mapping[str, Any],
    *,
    event_date: Any = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    dist_hour, dist_meta = _peak_hour_distribution_from_features(features, event_date=event_date)
    if dist_hour is not None:
        return dist_hour, dist_meta
    event_key = _event_date_key(event_date or features.get("metar_target_event_date"))
    for key in FORECAST_PEAK_TIME_KEYS:
        dt = _parse_forecast_peak_datetime(features.get(key), key=key)
        if dt is None:
            continue
        local_dt = dt.astimezone(CHENGDU_TZ)
        if event_key and local_dt.date().isoformat() != event_key:
            continue
        hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
        clamped_hour = max(0.0, min(23.99, float(hour)))
        return clamped_hour, {
            "expected_peak_hour_local": _round(clamped_hour, 2),
            "expected_peak_hour_source": key,
            "expected_peak_at_local": local_dt.isoformat(),
        }
    return None, {"expected_peak_hour_source": "forecast_peak_time_missing"}


def _expected_peak_hour_from_features(
    features: Mapping[str, Any],
    *,
    event_date: Any = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    peak_hour, meta = _forecast_peak_hour_from_features(features, event_date=event_date)
    if peak_hour is not None:
        return peak_hour, meta
    for key in ("local_peak_hour", "expected_peak_hour_local", "peak_hour_local"):
        value = _to_float(features.get(key))
        if value is None:
            continue
        clamped_hour = max(0.0, min(23.99, float(value)))
        return clamped_hour, {
            "expected_peak_hour_local": _round(clamped_hour, 2),
            "expected_peak_hour_source": str(features.get("expected_peak_hour_source") or key),
        }
    fallback_hour = _chengdu_seasonal_peak_hour_local(event_date or features.get("metar_target_event_date"))
    return fallback_hour, {
        "expected_peak_hour_local": _round(fallback_hour, 2),
        "expected_peak_hour_source": "chengdu_monthly_seasonal_peak_hour_prior_v1",
    }


def _merge_forecast_peak_timing_from_rows(
    features: Dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    merge_keys = FORECAST_PEAK_TIME_KEYS + (
        "expected_peak_hour_distribution",
        "peak_hour_distribution",
        "expected_peak_hour_local",
        "expected_peak_hour_source",
        "expected_peak_at_local",
        "expected_peak_window_start_hour_local",
        "expected_peak_window_end_hour_local",
        "expected_peak_window_source",
        "wu_forecast_peak_time_source",
        "wu_forecast_peak_hour_distribution",
        "wu_forecast_peak_window_start_hour_local",
        "wu_forecast_peak_window_end_hour_local",
        "weather_com_forecast_peak_time_source",
        "weather_com_forecast_peak_hour_distribution",
        "weather_com_forecast_peak_window_start_hour_local",
        "weather_com_forecast_peak_window_end_hour_local",
        "twc_forecast_peak_time_source",
        "twc_forecast_peak_hour_distribution",
        "twc_forecast_peak_window_start_hour_local",
        "twc_forecast_peak_window_end_hour_local",
        "taf_forecast_peak_issued_at",
        "taf_forecast_peak_valid_start_at",
        "taf_forecast_peak_valid_end_at",
        "taf_forecast_peak_time_source",
    )
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in merge_keys:
            if features.get(key) not in (None, ""):
                continue
            value = row.get(key)
            if value not in (None, ""):
                features[key] = value


def _parse_created_at(value: Any) -> float:
    dt = _parse_datetime(value)
    if dt is None:
        return 0.0
    return dt.timestamp()


def _bucket_temp(label: Any) -> Optional[float]:
    text = str(label or "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return _to_float(match.group(0))


def _is_cumulative_bucket(label: Any) -> bool:
    text = str(label or "").strip().lower()
    return "or below" in text or "or higher" in text or "below" in text or "higher" in text


def _is_single_temperature_bucket(label: Any) -> bool:
    text = str(label or "").strip()
    return bool(text and _bucket_temp(text) is not None and not _is_cumulative_bucket(text))


def _bucket_sort_key(row: Mapping[str, Any]) -> Tuple[int, float, str]:
    label = str(row.get("outcome") or row.get("label") or "")
    temp = _bucket_temp(label)
    text = label.lower()
    if "or below" in text or text.startswith("<="):
        return (0, temp if temp is not None else -999.0, label)
    if "or higher" in text or text.startswith(">="):
        return (2, temp if temp is not None else 999.0, label)
    return (1, temp if temp is not None else 999.0, label)


def _chengdu_slug_for_date(event_date: str) -> str:
    try:
        dt = datetime.fromisoformat(str(event_date or "").strip())
    except ValueError:
        return ""
    month = calendar.month_name[dt.month].lower()
    return f"highest-temperature-in-chengdu-on-{month}-{dt.day}-{dt.year}"


def _event_date_from_chengdu_slug(slug: Any) -> str:
    text = str(slug or "").strip().lower()
    match = re.search(r"highest-temperature-in-chengdu-on-([a-z]+)-(\d{1,2})-(\d{4})", text)
    if not match:
        return ""
    month_name, day_text, year_text = match.groups()
    try:
        month = list(calendar.month_name).index(month_name.capitalize())
        day = int(day_text)
        year = int(year_text)
        return datetime(year, month, day).date().isoformat()
    except (ValueError, IndexError):
        return ""


def _row_slug_date_matches(row: sqlite3.Row) -> bool:
    slug_date = _event_date_from_chengdu_slug(row["slug"])
    event_date = str(row["event_date"] or "").split("T", 1)[0]
    return bool(slug_date and event_date and slug_date == event_date)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    timeout = float(os.environ.get("PM_CHENGDU_INTRADAY_DB_TIMEOUT_SEC", "1.5") or 1.5)
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={max(100, int(timeout * 1000))}")
    return conn


def _opposite_trade_side(side: Any) -> str:
    text = str(side or "").strip().upper()
    if text == "YES":
        return "NO"
    if text == "NO":
        return "YES"
    return ""


def _active_position_legs(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
) -> List[Dict[str, Any]]:
    slug_key = str(slug or "").strip()
    event_key = str(event_date or "").split("T", 1)[0]
    if not slug_key or not event_key:
        return []
    try:
        rows = conn.execute(
            """
            SELECT slug, event_date, outcome, UPPER(COALESCE(side, '')) AS side,
                   state, position_size, avg_price, entry_price, updated_at, entry_at
            FROM edge_trades
            WHERE slug = ?
              AND event_date = ?
              AND UPPER(COALESCE(side, '')) IN ('YES', 'NO')
              AND COALESCE(position_size, 0) > 0.000001
            ORDER BY COALESCE(position_size, 0) DESC, outcome, side
            """,
            (slug_key, event_key),
        ).fetchall()
    except sqlite3.Error:
        return []
    positions: List[Dict[str, Any]] = []
    for row in rows:
        outcome = str(row["outcome"] or "").strip()
        side = str(row["side"] or "").strip().upper()
        size = _to_float(row["position_size"])
        if not outcome or side not in {"YES", "NO"} or size is None or size <= 0:
            continue
        avg = _to_float(row["avg_price"])
        if avg is None:
            avg = _to_float(row["entry_price"])
        positions.append(
            {
                "outcome": outcome,
                "side": side,
                "position_size": float(size),
                "avg_price": avg,
                "current_notional": float(size) * float(avg) if avg is not None else None,
                "state": str(row["state"] or ""),
                "source": "edge_trades",
                "updated_at": str(row["updated_at"] or ""),
                "entry_at": str(row["entry_at"] or ""),
            }
        )
    return positions


def _latest_event_run(conn: sqlite3.Connection, event_date: Optional[str] = None) -> Optional[sqlite3.Row]:
    if event_date:
        exact_slug = _chengdu_slug_for_date(event_date)
        if exact_slug:
            row = conn.execute(
                """
                SELECT id, run_date, event_date, slug, title, payload_json, created_at
                FROM event_runs
                WHERE slug = ? AND event_date = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (exact_slug, event_date),
            ).fetchone()
            if row is not None:
                return row
    if not event_date:
        rows = conn.execute(
            """
            SELECT id, run_date, event_date, slug, title, payload_json, created_at
            FROM event_runs INDEXED BY idx_event_runs_created_at
            WHERE slug LIKE ?
            ORDER BY created_at DESC
            LIMIT 80
            """,
            (CHENGDU_SLUG_LIKE,),
        ).fetchall()
        for row in rows:
            if _row_slug_date_matches(row):
                return row
        if rows:
            return rows[0]
    params: List[Any] = [CHENGDU_SLUG_LIKE]
    where = "slug LIKE ?"
    if event_date:
        where += " AND event_date = ?"
        params.append(event_date)
    sql = f"""
        SELECT id, run_date, event_date, slug, title, payload_json, created_at
        FROM event_runs
        WHERE {where}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """
    return conn.execute(sql, params).fetchone()


def _latest_fact_snapshot(conn: sqlite3.Connection, event_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    params: List[Any] = []
    where = "h.slug LIKE ?"
    params.append(CHENGDU_SLUG_LIKE)
    if event_date:
        exact_slug = _chengdu_slug_for_date(event_date)
        if exact_slug:
            where = "h.slug = ? AND h.event_date = ?"
            params = [exact_slug, event_date]
        else:
            where += " AND h.event_date = ?"
            params.append(event_date)
    try:
        row = conn.execute(
            f"""
            SELECT h.id, h.run_date, h.event_date, h.slug, h.fact_as_of, h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE {where}
            ORDER BY h.fact_as_of DESC, h.id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    if row is None:
        return None
    payload_text = str(row["fact_json"] or "")
    try:
        payload = json.loads(payload_text or "{}")
    except Exception:
        payload = {}
    title = str(payload.get("title") or "") if isinstance(payload, Mapping) else ""
    return {
        "id": int(row["id"]),
        "run_date": str(row["run_date"] or ""),
        "event_date": str(row["event_date"] or ""),
        "slug": str(row["slug"] or ""),
        "title": title,
        "payload_json": payload_text,
        "created_at": str(row["fact_as_of"] or ""),
        "snapshot_source": "event_fact_latest",
    }


def _recent_payloads(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    meta_rows = conn.execute(
        """
        SELECT id, created_at
        FROM event_runs
        WHERE slug = ? AND event_date = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (slug, event_date, limit),
    ).fetchall()
    if not meta_rows:
        return []
    parsed_meta: List[Tuple[int, str, datetime]] = []
    for row in meta_rows:
        created = str(row["created_at"] or "")
        parsed_dt = _parse_datetime(created)
        if parsed_dt is None:
            continue
        parsed_meta.append((int(row["id"]), created, parsed_dt))
    if not parsed_meta:
        return []
    latest_dt = parsed_meta[0][2]
    selected_ids: Dict[int, None] = {}
    selected_ids[parsed_meta[0][0]] = None
    latest_local = latest_dt.astimezone(CHENGDU_TZ)
    hour_targets = {
        (latest_local.replace(minute=0, second=0, microsecond=0) - timedelta(hours=offset)).isoformat()
        for offset in range(0, 7)
    }
    by_hour: Dict[str, Tuple[int, str, datetime]] = {}
    for item in parsed_meta:
        local_dt = item[2].astimezone(CHENGDU_TZ)
        bucket = local_dt.replace(minute=0, second=0, microsecond=0).isoformat()
        if bucket in hour_targets and bucket not in by_hour:
            by_hour[bucket] = item
    for item in by_hour.values():
        selected_ids[item[0]] = None
    for horizon in (1, 2, 3):
        candidates: List[Tuple[float, Tuple[int, str, datetime]]] = []
        for item in parsed_meta[1:]:
            elapsed_h = (latest_dt - item[2]).total_seconds() / 3600.0
            if elapsed_h <= 0:
                continue
            if elapsed_h >= max(0.5, float(horizon) * 0.65):
                candidates.append((abs(elapsed_h - float(horizon)), item))
        if candidates:
            selected_ids[min(candidates, key=lambda entry: entry[0])[1][0]] = None
    wanted_ids = list(selected_ids.keys())
    placeholders = ",".join("?" for _ in wanted_ids)
    rows = conn.execute(
        """
        SELECT id, payload_json, created_at
        FROM event_runs
        WHERE id IN (
        """
        + placeholders
        + """
        )
        ORDER BY created_at ASC, id ASC
        """,
        wanted_ids,
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload["_event_run_id"] = int(row["id"])
        payload["_created_at"] = str(row["created_at"] or "")
        out.append(payload)
    out.sort(key=lambda item: _parse_created_at(item.get("_created_at")))
    return out


def _hydrate_fact_payload_temperature_features(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        features = {}
    features_out = dict(features)
    summary = payload.get("chengdu_intraday_summary")
    summary_metrics = (
        summary.get("key_metrics")
        if isinstance(summary, Mapping) and isinstance(summary.get("key_metrics"), Mapping)
        else {}
    )
    direct_metrics = payload.get("key_metrics") if isinstance(payload.get("key_metrics"), Mapping) else {}
    metrics: Dict[str, Any] = {}
    for source in (summary_metrics, direct_metrics):
        for key, value in source.items():
            if value not in (None, "") and key not in metrics:
                metrics[key] = value
    for key in (
        "observed_temp_c",
        "observed_today_max_c",
        "observed_path_temp_c",
        "observed_path_temp_source",
        "temperature_rise_rate_c_per_hour",
        "temperature_rise_rate_source",
        "temperature_rise_rate_window",
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_realization_state",
        "temperature_momentum_sustained_warming",
        "soft_forecast_peak_c",
        "weather_com_forecast_peak_c",
        "weather_com_forecast_peak_at",
        "weather_com_forecast_peak_time_source",
        "peak_estimator_mu_c",
        "peak_estimator_sigma_c",
        "peak_estimator_source",
        "peak_estimator_observed_today_max_source",
        "peak_estimator_raw_observed_today_max_c",
        "peak_estimator_raw_observed_today_max_source",
        "peak_estimator_raw_observed_today_max_demoted_reason",
        "dewpoint_c",
        "cloud_cover_pct",
        "cloud_cover_low_pct",
        "cloud_cover_mid_pct",
        "cloud_cover_high_pct",
        "metar_effective_cloud_cover_pct",
        "solar_radiation",
        "observed_precip_rate_mm_per_h",
        "rain_rate_mm_per_h",
        "precipitation_rate_mm_per_h",
        "metar_rain_observed",
        "metar_no_precip_observed",
        "metar_cavok_observed",
        "awc_metar_no_precip_observed",
        "is_raining_observed",
        "precip_nowcast_1_3h_available",
        "precip_nowcast_1_3h_risk_state",
        "precip_nowcast_1_3h_max_rate_mm_per_h",
        "precip_nowcast_1_3h_total_mm",
        "precip_nowcast_1_3h_max_probability_pct",
        "precip_nowcast_1_3h_next_precip_at",
        "precip_nowcast_1_3h_checked_at",
        "precip_nowcast_1_3h_source",
    ):
        if features_out.get(key) in (None, "") and metrics.get(key) not in (None, ""):
            features_out[key] = metrics.get(key)
    momentum_rates = metrics.get("momentum_rates_c_per_hour")
    if not isinstance(momentum_rates, Mapping):
        momentum_rates = metrics.get("temperature_momentum_rates_c_per_hour")
    if isinstance(momentum_rates, Mapping):
        for label in TEMPERATURE_MOMENTUM_WINDOWS:
            key = f"temperature_momentum_{label}_c_per_hour"
            if features_out.get(key) in (None, "") and momentum_rates.get(label) not in (None, ""):
                features_out[key] = momentum_rates.get(label)
    for label in TEMPERATURE_MOMENTUM_WINDOWS:
        key = f"temperature_momentum_{label}_c_per_hour"
        if features_out.get(key) in (None, "") and metrics.get(key) not in (None, ""):
            features_out[key] = metrics.get(key)
    if features_out.get("observed_temp_c") in (None, "") and payload.get("current_temp_c") not in (None, ""):
        features_out["observed_temp_c"] = payload.get("current_temp_c")
    if features_out.get("observed_today_max_c") in (None, "") and payload.get("observed_high_c") not in (None, ""):
        features_out["observed_today_max_c"] = payload.get("observed_high_c")
    payload["weather_likelihood_features"] = features_out
    return payload


def _recent_fact_payloads(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    limit: int = 2000,
    max_points: int = 12,
) -> List[Dict[str, Any]]:
    try:
        meta_rows = conn.execute(
            """
            SELECT id, fact_as_of
            FROM event_fact_history
            WHERE slug = ? AND event_date = ?
            ORDER BY fact_as_of DESC, id DESC
            LIMIT ?
            """,
            (slug, event_date, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not meta_rows:
        return []
    parsed_meta: List[Tuple[int, str, datetime]] = []
    for row in meta_rows:
        created = str(row["fact_as_of"] or "")
        parsed_dt = _parse_datetime(created)
        if parsed_dt is None:
            continue
        parsed_meta.append((int(row["id"]), created, parsed_dt))
    if not parsed_meta:
        return []
    latest_dt = parsed_meta[0][2]
    selected_ids: Dict[int, None] = {parsed_meta[0][0]: None}
    latest_local = latest_dt.astimezone(CHENGDU_TZ)
    hour_targets = {
        (latest_local.replace(minute=0, second=0, microsecond=0) - timedelta(hours=offset)).isoformat()
        for offset in range(0, 9)
    }
    by_hour: Dict[str, Tuple[int, str, datetime]] = {}
    for item in parsed_meta:
        local_dt = item[2].astimezone(CHENGDU_TZ)
        bucket = local_dt.replace(minute=0, second=0, microsecond=0).isoformat()
        if bucket in hour_targets and bucket not in by_hour:
            by_hour[bucket] = item
    for item in by_hour.values():
        selected_ids[item[0]] = None
    for horizon in (0.5, 1, 2, 3, 6):
        candidates: List[Tuple[float, Tuple[int, str, datetime]]] = []
        for item in parsed_meta[1:]:
            elapsed_h = (latest_dt - item[2]).total_seconds() / 3600.0
            if elapsed_h <= 0:
                continue
            if elapsed_h >= max(10.0 / 60.0, float(horizon) * 0.65):
                candidates.append((abs(elapsed_h - float(horizon)), item))
        if candidates:
            selected_ids[min(candidates, key=lambda entry: entry[0])[1][0]] = None
    wanted_ids = list(selected_ids.keys())[: max(2, int(max_points))]
    if not wanted_ids:
        return []
    placeholders = ",".join("?" for _ in wanted_ids)
    rows = conn.execute(
        f"""
        SELECT id, fact_json, fact_as_of
        FROM event_fact_history
        WHERE id IN ({placeholders})
        ORDER BY fact_as_of ASC, id ASC
        """,
        wanted_ids,
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["fact_json"] or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload = _hydrate_fact_payload_temperature_features(payload)
        payload["_event_run_id"] = int(row["id"])
        payload["_created_at"] = str(row["fact_as_of"] or "")
        payload["_snapshot_source"] = "event_fact_history"
        out.append(payload)
    out.sort(key=lambda item: _parse_created_at(item.get("_created_at")))
    return out


def _merge_recent_payloads(*groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for item in group:
            if not isinstance(item, Mapping):
                continue
            payload = dict(item)
            key = str(payload.get("_created_at") or "")
            if not key:
                key = f"{payload.get('_snapshot_source') or ''}:{payload.get('_event_run_id') or len(by_key)}"
            existing = by_key.get(key)
            if existing is None or str(payload.get("_snapshot_source") or "") == "event_fact_history":
                by_key[key] = payload
    out = list(by_key.values())
    out.sort(key=lambda item: _parse_created_at(item.get("_created_at")))
    return out


def _recent_payloads_until(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    created_at: str,
    limit: int = 240,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, payload_json, created_at
        FROM event_runs
        WHERE slug = ? AND event_date = ? AND created_at <= ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (slug, event_date, created_at, limit),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload["_event_run_id"] = int(row["id"])
        payload["_created_at"] = str(row["created_at"] or "")
        out.append(payload)
    return out


def _distribution_rows(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in payload.get("rows") or payload.get("distribution") or []:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("outcome") or raw.get("label") or "").strip()
        if not label:
            continue
        q_cal = _to_float(raw.get("q_calibrated"))
        q_bayes_pre_observed = _to_float(raw.get("q_bayes_posterior_pre_observed_constraints"))
        q_bayes = _to_float(raw.get("q_bayes_posterior"))
        if q_bayes is None:
            q_bayes = q_bayes_pre_observed
        if q_bayes is None:
            q_bayes = _to_float(raw.get("q_raw"))
        q_temperature_bucket_posterior_pre_observed = _to_float(
            raw.get("q_temperature_bucket_posterior_pre_observed_constraints")
        )
        q_temperature_bucket_posterior = _to_float(raw.get("q_temperature_bucket_posterior"))
        if q_temperature_bucket_posterior is None:
            q_temperature_bucket_posterior = q_temperature_bucket_posterior_pre_observed
        phys_observed = _to_float(raw.get("q_bayes_physical_likelihood_observed_prob"))
        reason_codes = raw.get("q_posterior_observed_constraints_reason_codes")
        if not isinstance(reason_codes, list):
            reason_codes = []
        item = {
            "outcome": label,
            "q_raw": _to_float(raw.get("q_raw")),
            "q_round": _to_float(raw.get("q_round")),
            "q_model_temperature_bucket_round": _to_float(
                raw.get("q_model_temperature_bucket_round")
            ),
            "q_ensemble_qm_raw_bucket": _to_float(raw.get("q_ensemble_qm_raw_bucket")),
            "q_ensemble_qm_bucket": _to_float(raw.get("q_ensemble_qm_bucket")),
            "q_calibrated": q_cal,
            "q_calibrated_pct": _pct(q_cal),
            "q_bayes_posterior": q_bayes,
            "q_bayes_posterior_pct": _pct(q_bayes),
            "q_bayes_posterior_pre_observed_constraints": q_bayes_pre_observed,
            "q_bayes_posterior_pre_observed_constraints_pct": _pct(q_bayes_pre_observed),
            "q_temperature_bucket_posterior": q_temperature_bucket_posterior,
            "q_temperature_bucket_posterior_pct": _pct(q_temperature_bucket_posterior),
            "q_temperature_bucket_posterior_pre_observed_constraints": q_temperature_bucket_posterior_pre_observed,
            "q_temperature_bucket_posterior_pre_observed_constraints_pct": _pct(
                q_temperature_bucket_posterior_pre_observed
            ),
            "q_posterior_observed_constraints_applied": _to_bool(
                raw.get("q_posterior_observed_constraints_applied")
            ),
            "q_posterior_observed_constraints_hard_zero": _to_bool(
                raw.get("q_posterior_observed_constraints_hard_zero")
            ),
            "q_posterior_observed_constraints_reason_codes": list(reason_codes),
            "best_bid": _to_float(raw.get("best_bid")),
            "best_ask": _to_float(raw.get("best_ask")),
            "best_bid_no": _to_float(raw.get("best_bid_no")),
            "best_ask_no": _to_float(raw.get("best_ask_no")),
            "outcome_volume": _to_float(raw.get("outcome_volume")),
            "volume_24h": _to_float(raw.get("volume_24h")),
            "volume_total": _to_float(raw.get("volume_total")),
            "liquidity": _to_float(raw.get("liquidity")),
            "display_probability_market_dead": _to_bool(raw.get("display_probability_market_dead")),
            "display_probability_source": str(raw.get("display_probability_source") or "").strip() or None,
            "display_probability_market_invalid": _to_bool(raw.get("display_probability_market_invalid")),
            "display_probability_market_invalid_reason": str(raw.get("display_probability_market_invalid_reason") or "").strip() or None,
            "display_probability_market_dead_reason": str(raw.get("display_probability_market_dead_reason") or "").strip() or None,
            "is_cumulative_bucket": _is_cumulative_bucket(label),
            "q_physical_enabled": bool(raw.get("q_bayes_physical_likelihood_enabled")),
            "q_physical_evidence": _to_float(raw.get("q_bayes_physical_likelihood_evidence")) or 0.0,
            "q_physical_observed_prob": phys_observed,
            "q_physical_observed_prob_pct": _pct(phys_observed, 4),
        }
        diagnostic_keys = (
            "q_bayes_source",
            "q_bayes_prior_strength",
            "q_temperature_bucket_alpha",
            "q_temperature_bucket_alpha_total",
            "q_bayes_model_evidence",
            "q_bayes_model_prob",
            "q_bayes_wu_forecast_evidence",
            "q_bayes_wu_forecast_prob",
            "q_bayes_taf_peak_evidence",
            "q_bayes_taf_peak_prob",
            "q_bayes_observed_nowcast_evidence",
            "q_bayes_observed_nowcast_prob",
            "q_bayes_nowcast_evidence",
            "q_bayes_nowcast_prob",
            "q_bayes_independent_physics_residual_evidence",
            "q_bayes_independent_physics_residual_prob",
            "q_bayes_independent_physics_residual_observed_prob",
            "q_bayes_climo_regime_evidence",
            "q_bayes_climo_regime_prob",
            "q_bayes_post_cool_rebound_regime_prior_evidence",
            "q_bayes_post_cool_rebound_regime_prior_prob",
            "q_bayes_post_cool_rebound_intraday_likelihood_evidence",
            "q_bayes_post_cool_rebound_intraday_likelihood_prob",
            "q_bayes_post_cool_rebound_risk_evidence",
            "q_bayes_post_cool_rebound_risk_prob",
            "q_bayes_physical_likelihood_prob",
            "q_bayes_physical_likelihood_observed_prob",
            "q_bayes_physical_likelihood_evidence",
            "q_bayes_physical_likelihood_source",
            "q_bayes_wu_forecast_status",
            "q_bayes_taf_peak_status",
            "wu_forecast_peak_at",
            "wu_forecast_peak_valid_local",
            "wu_forecast_peak_time_source",
            "weather_com_forecast_peak_at",
            "weather_com_forecast_peak_time_source",
            "twc_forecast_peak_at",
            "twc_forecast_peak_time_source",
            "taf_forecast_peak_at",
            "taf_forecast_peak_issued_at",
            "taf_forecast_peak_valid_start_at",
            "taf_forecast_peak_valid_end_at",
            "taf_forecast_peak_time_source",
        )
        for key in diagnostic_keys:
            value = raw.get(key)
            if value not in (None, ""):
                item[key] = value
        components_raw = raw.get("q_temperature_bucket_score_components")
        if isinstance(components_raw, Mapping):
            item["q_temperature_bucket_score_components"] = dict(components_raw)
        component_details_raw = raw.get("q_temperature_bucket_score_component_details")
        if isinstance(component_details_raw, Mapping):
            item["q_temperature_bucket_score_component_details"] = dict(component_details_raw)
        bayes_chain = _bayes_chain_for_distribution_row(raw)
        if bayes_chain:
            item["bayes_chain"] = bayes_chain
            if bayes_chain.get("prior_prob") is not None:
                item["q_prior"] = bayes_chain.get("prior_prob")
            if bayes_chain.get("likelihood_prob") is not None:
                item["q_likelihood"] = bayes_chain.get("likelihood_prob")
        if raw.get("q_posterior_observed_constraints_applied"):
            item["q_posterior_observed_constraints_primary_for_outlook"] = True
            item["q_posterior_observed_constraints_primary_reason"] = (
                "final_outlook_uses_current_fact_gated_posterior"
            )
        out.append(item)
    out.sort(key=_bucket_sort_key)
    return out


def _append_current_payload_snapshot(
    recent: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    *,
    snapshot_id: Any,
    created_at: str,
    source: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [dict(item) for item in recent if isinstance(item, Mapping)]
    current = dict(payload)
    current["_event_run_id"] = snapshot_id
    current["_created_at"] = str(created_at or "")
    current["_snapshot_source"] = str(source or "")
    current_dt = _parse_datetime(created_at)
    replaced = False
    for idx, existing in enumerate(out):
        if str(existing.get("_created_at") or "") == str(created_at or ""):
            out[idx] = current
            replaced = True
            break
        existing_dt = _parse_datetime(existing.get("_created_at"))
        if current_dt is not None and existing_dt is not None and abs((current_dt - existing_dt).total_seconds()) < 1.0:
            out[idx] = current
            replaced = True
            break
    if not replaced:
        out.append(current)
    out.sort(key=lambda item: _parse_created_at(item.get("_created_at")))
    return out


def _latest_market_tick_snapshot(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    expected_labels = {
        str(row.get("outcome") or row.get("label") or "").strip()
        for row in rows
        if str(row.get("outcome") or row.get("label") or "").strip()
    }
    expected_from_rows = len(expected_labels)
    token_count: Optional[int] = None
    token_updated_at = ""
    try:
        token_row = conn.execute(
            """
            SELECT COUNT(DISTINCT outcome) AS n, MAX(updated_at) AS updated_at
            FROM market_tokens
            WHERE slug = ?
              AND event_date = ?
              AND COALESCE(market_closed, 0) = 0
              AND COALESCE(market_archived, 0) = 0
            """,
            (slug, event_date),
        ).fetchone()
        if token_row is not None:
            token_count = int(token_row["n"] or 0)
            token_updated_at = str(token_row["updated_at"] or "")
    except sqlite3.Error:
        token_count = None
    expected = max(expected_from_rows, int(token_count or 0))
    try:
        tick_rows = conn.execute(
            """
            SELECT outcome, side, best_bid, best_ask, mid, spread, source, source_detail, created_at
            FROM event_market_ticks
            WHERE slug = ?
              AND event_date = ?
              AND UPPER(COALESCE(side, 'YES')) = 'YES'
            ORDER BY created_at DESC, id DESC
            LIMIT 500
            """,
            (slug, event_date),
        ).fetchall()
    except sqlite3.Error:
        return {
            "available": False,
            "reason": "event_market_ticks_unavailable",
            "slug": slug,
            "event_date": event_date,
            "expected_outcomes": expected or None,
            "expected_from_rows": expected_from_rows,
            "expected_from_market_tokens": token_count,
            "token_updated_at": token_updated_at,
            "covered_outcomes": 0,
            "coverage_ratio": None,
            "is_complete": False,
            "needs_full_rest_snapshot": bool(expected),
            "latest_tick_at": "",
            "version": "market_tick_snapshot_overlay_v1",
        }

    by_outcome: Dict[str, Dict[str, Any]] = {}
    latest_dt: Optional[datetime] = None
    latest_text = ""
    for row in tick_rows:
        label = str(row["outcome"] or "").strip()
        if not label or label in by_outcome:
            continue
        tick = {
            "outcome": label,
            "best_bid": _price_probability(row["best_bid"]),
            "best_ask": _price_probability(row["best_ask"]),
            "mid": _price_probability(row["mid"]),
            "spread": _price_probability(row["spread"]),
            "source": str(row["source"] or ""),
            "source_detail": str(row["source_detail"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
        by_outcome[label] = tick
        dt = _parse_datetime(tick["created_at"])
        if dt is not None and (latest_dt is None or dt > latest_dt):
            latest_dt = dt
            latest_text = tick["created_at"]

    covered = len(by_outcome)
    coverage = (float(covered) / float(expected)) if expected else None
    is_complete = bool(expected and covered >= expected)
    return {
        "available": bool(by_outcome),
        "reason": "ok" if by_outcome else "no_market_ticks",
        "slug": slug,
        "event_date": event_date,
        "expected_outcomes": expected or None,
        "expected_from_rows": expected_from_rows,
        "expected_from_market_tokens": token_count,
        "token_updated_at": token_updated_at,
        "covered_outcomes": covered,
        "coverage_ratio": _round(coverage, 4),
        "is_complete": is_complete,
        "needs_full_rest_snapshot": bool(expected and covered < expected),
        "latest_tick_at": latest_text,
        "latest_by_outcome": by_outcome,
        "version": "market_tick_snapshot_overlay_v1",
    }


def _apply_market_tick_overlay(
    rows: Sequence[Mapping[str, Any]],
    tick_snapshot: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    by_outcome = tick_snapshot.get("latest_by_outcome")
    if not isinstance(by_outcome, Mapping) or not by_outcome:
        return [dict(row) for row in rows]
    out: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        label = str(row.get("outcome") or row.get("label") or "").strip()
        tick = by_outcome.get(label)
        if isinstance(tick, Mapping):
            bid = _price_probability(tick.get("best_bid"))
            ask = _price_probability(tick.get("best_ask"))
            mid = _price_probability(tick.get("mid"))
            spread = _price_probability(tick.get("spread"))
            if bid is not None:
                row["best_bid"] = bid
                row["best_ask_no"] = max(0.0, min(1.0, 1.0 - bid))
            if ask is not None:
                row["best_ask"] = ask
                row["best_bid_no"] = max(0.0, min(1.0, 1.0 - ask))
            if mid is not None:
                row["market_mid"] = mid
            if spread is not None:
                row["market_spread"] = spread
            row["market_tick_overlay_applied"] = True
            row["market_tick_overlay_source"] = str(tick.get("source") or "")
            row["market_tick_overlay_at"] = str(tick.get("created_at") or "")
        else:
            row["market_tick_overlay_applied"] = False
        out.append(row)
    return out


def _row_market_probability(row: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    for key in ("market_mid", "mid", "display_probability_market", "market_probability"):
        value = _price_probability(row.get(key))
        if value is not None:
            return value, key
    bid = _price_probability(row.get("best_bid"))
    ask = _price_probability(row.get("best_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0, "bid_ask_mid"
    if ask is not None:
        return ask, "best_ask"
    if bid is not None:
        return bid, "best_bid"
    return None, ""


def _market_top_bucket_reference(
    rows: Sequence[Mapping[str, Any]],
    tick_snapshot: Mapping[str, Any],
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for raw in rows:
        prob, source = _row_market_probability(raw)
        if prob is None:
            continue
        row = dict(raw)
        row["market_probability"] = prob
        row["market_probability_pct"] = _pct(prob)
        row["market_probability_source"] = source
        candidates.append(row)
    candidates.sort(
        key=lambda row: (
            float(row.get("market_probability") or -1.0),
            -_bucket_sort_key(row)[0],
        ),
        reverse=True,
    )
    top = candidates[0] if candidates else {}
    return {
        "available": bool(top),
        "outcome": str(top.get("outcome") or ""),
        "market_probability": top.get("market_probability"),
        "market_probability_pct": top.get("market_probability_pct"),
        "probability_source": top.get("market_probability_source"),
        "best_bid": top.get("best_bid"),
        "best_ask": top.get("best_ask"),
        "coverage_complete": bool(tick_snapshot.get("is_complete")),
        "coverage_ratio": tick_snapshot.get("coverage_ratio"),
        "covered_outcomes": tick_snapshot.get("covered_outcomes"),
        "expected_outcomes": tick_snapshot.get("expected_outcomes"),
        "latest_tick_at": tick_snapshot.get("latest_tick_at"),
        "needs_full_rest_snapshot": bool(tick_snapshot.get("needs_full_rest_snapshot")),
        "source": "event_market_ticks_overlay" if tick_snapshot.get("available") else "event_runs_payload_rows",
        "version": "market_top_bucket_reference_v1",
    }


def _apply_observed_max_display_guard(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    observed_max, observed_max_source, _observed_max_na_reason = _hard_observed_today_max_c(features)
    if observed_max is None:
        return [dict(row) for row in rows]
    guarded: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        label = str(row.get("outcome") or "")
        temp = _bucket_temp(label)
        text = label.lower()
        impossible_low_bucket = bool(
            temp is not None
            and ("or below" in text or text.startswith("<=") or "below" in text)
            and observed_max > float(temp) + 0.05
        )
        if impossible_low_bucket:
            row["impossible_by_observed_max"] = True
            row["impossible_reason"] = "observed_today_max_above_bucket_upper_bound"
            row["observed_today_max_c"] = _round(observed_max, 2)
            row["observed_today_max_source"] = observed_max_source
            row["q_calibrated_raw"] = row.get("q_calibrated")
            row["q_calibrated_pct_raw"] = row.get("q_calibrated_pct")
            row["q_bayes_posterior_raw"] = row.get("q_bayes_posterior")
            row["q_bayes_posterior_pct_raw"] = row.get("q_bayes_posterior_pct")
            row["q_temperature_bucket_posterior_raw"] = row.get("q_temperature_bucket_posterior")
            row["q_temperature_bucket_posterior_pct_raw"] = row.get("q_temperature_bucket_posterior_pct")
            row["q_calibrated"] = 0.0
            row["q_calibrated_pct"] = 0.0
            row["q_bayes_posterior"] = 0.0
            row["q_bayes_posterior_pct"] = 0.0
            row["q_temperature_bucket_posterior"] = 0.0
            row["q_temperature_bucket_posterior_pct"] = 0.0
        else:
            row["impossible_by_observed_max"] = False
        guarded.append(row)
    return guarded


def _apply_d0_observed_feasibility_guard(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    event_date: str,
    *,
    today_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    date_key = str(event_date or "").split("T", 1)[0]
    local_today = today_key or datetime.now(CHENGDU_TZ).date().isoformat()
    if date_key != local_today:
        return [dict(row) for row in rows]
    observed_max, observed_max_source, _observed_max_na_reason = _hard_observed_today_max_c(features)
    observed_temp = _to_float(features.get("observed_temp_c"))
    hard_source = str(
        features.get("observed_nowcast_source_kind")
        or features.get("observed_temp_source")
        or observed_max_source
        or ""
    ).lower()
    hard_observed = bool(
        observed_max is not None
        and observed_temp is not None
        and (
            "zuuu_metar_raw_awc" in hard_source
            or "metar" in hard_source
            or bool(features.get("observed_today_max_trusted"))
        )
    )
    if not hard_observed:
        return [dict(row) for row in rows]
    final_keys = (
        "temperature_observation_final_daily_max",
        "observed_today_max_is_final",
        "daily_max_observation_complete",
        "observed_today_max_final",
    )
    explicit_final_observed_max = any(_to_bool(features.get(key)) is True for key in final_keys)
    source_text = " ".join(
        [
            str(features.get(key) or "").strip().lower()
            for key in (
                "observed_nowcast_source_kind",
                "observed_nowcast_source",
                "observed_temp_source",
            )
        ]
        + [str(observed_max_source or "").strip().lower()]
    )
    official_final_source = any(
        token in source_text
        for token in (
            "official_daily_max",
            "final_daily_max",
            "settlement_actual",
            "wu_twc_final_settlement",
            "twc_final_settlement",
            "wunderground_final_settlement",
        )
    )
    if official_final_source:
        explicit_final_observed_max = True
    explicit_intraday_observed_max = any(
        key in features and _to_bool(features.get(key)) is False for key in final_keys
    ) or any(
        token in source_text
        for token in (
            "zuuu_metar_raw_awc",
            "metar",
            "awc",
            "airport",
            "wu_twc_observed_intraday",
            "wunderground",
            "weather.com",
            "twc",
            "nowcast",
            "history",
        )
    )
    final_observed_max = bool(explicit_final_observed_max or not explicit_intraday_observed_max)
    if not final_observed_max:
        guarded = []
        for raw in rows:
            row = dict(raw)
            row["observed_feasibility_applied"] = False
            row["observed_feasibility_skipped_reason"] = "intraday_observed_max_not_final"
            row["observed_feasibility_observed_temp_c"] = _round(observed_temp, 2)
            row["observed_feasibility_observed_max_c"] = _round(observed_max, 2)
            if not bool(row.get("impossible_by_observed_max")):
                q_cal_raw = _to_float(row.get("q_calibrated_raw"))
                if q_cal_raw is not None:
                    row["q_calibrated"] = q_cal_raw
                    row["q_calibrated_pct"] = _pct(q_cal_raw)
                q_bayes_raw = _to_float(row.get("q_bayes_posterior_raw"))
                if q_bayes_raw is not None:
                    row["q_bayes_posterior"] = q_bayes_raw
                    row["q_bayes_posterior_pct"] = _pct(q_bayes_raw)
                q_temperature_raw = _to_float(row.get("q_temperature_bucket_posterior_raw"))
                if q_temperature_raw is not None:
                    row["q_temperature_bucket_posterior"] = q_temperature_raw
                    row["q_temperature_bucket_posterior_pct"] = _pct(q_temperature_raw)
            row["suppressed_by_observed_feasibility"] = False
            guarded.append(row)
        return guarded

    guarded = [dict(row) for row in rows]
    factors: List[Optional[float]] = []
    has_factor = False
    for row in guarded:
        if bool(row.get("impossible_by_observed_max")):
            factors.append(0.0)
            continue
        factor = _to_float(row.get("q_physical_observed_prob"))
        if factor is None:
            factor = _to_float(row.get("q_bayes_physical_likelihood_observed_prob"))
        if factor is None:
            factors.append(None)
            continue
        factor = max(0.0, min(1.0, float(factor)))
        factors.append(factor)
        has_factor = True
    if not has_factor:
        return guarded

    raw_calibrated: List[Optional[float]] = []
    raw_bayes: List[Optional[float]] = []
    raw_temperature: List[Optional[float]] = []
    adjusted_calibrated: List[Optional[float]] = []
    adjusted_bayes: List[Optional[float]] = []
    adjusted_temperature: List[Optional[float]] = []
    for row, factor in zip(guarded, factors):
        # Make the guard idempotent across fact write/read cycles. Once a row has
        # been materialized, later passes must recompute from the preserved raw
        # posterior instead of multiplying the observed-feasibility factor again.
        q_cal = _to_float(row.get("q_calibrated_raw"))
        if q_cal is None:
            q_cal = _to_float(row.get("q_calibrated"))
        q_bayes = _to_float(row.get("q_bayes_posterior_raw"))
        if q_bayes is None:
            q_bayes = _to_float(row.get("q_bayes_posterior"))
        q_temperature = _to_float(row.get("q_temperature_bucket_posterior_raw"))
        if q_temperature is None:
            q_temperature = _to_float(row.get("q_temperature_bucket_posterior"))
        raw_calibrated.append(q_cal)
        raw_bayes.append(q_bayes)
        raw_temperature.append(q_temperature)
        if factor is None:
            adjusted_calibrated.append(q_cal)
            adjusted_bayes.append(q_bayes)
            adjusted_temperature.append(q_temperature)
        else:
            adjusted_calibrated.append((q_cal or 0.0) * factor if q_cal is not None else None)
            adjusted_bayes.append((q_bayes or 0.0) * factor if q_bayes is not None else None)
            adjusted_temperature.append((q_temperature or 0.0) * factor if q_temperature is not None else None)
    cal_sum = sum(float(v) for v in adjusted_calibrated if v is not None and math.isfinite(float(v)))
    bayes_sum = sum(float(v) for v in adjusted_bayes if v is not None and math.isfinite(float(v)))
    temperature_sum = sum(float(v) for v in adjusted_temperature if v is not None and math.isfinite(float(v)))
    if cal_sum <= 0 and bayes_sum <= 0 and temperature_sum <= 0:
        return guarded

    for idx, row in enumerate(guarded):
        factor = factors[idx]
        row["observed_feasibility_applied"] = True
        row["observed_feasibility_factor"] = _round(factor, 6) if factor is not None else None
        row["observed_feasibility_source"] = hard_source or "hard_observed_nowcast"
        row["observed_feasibility_observed_temp_c"] = _round(observed_temp, 2)
        row["observed_feasibility_observed_max_c"] = _round(observed_max, 2)
        if "q_calibrated_raw" not in row:
            row["q_calibrated_raw"] = raw_calibrated[idx]
            row["q_calibrated_pct_raw"] = _pct(raw_calibrated[idx])
        if "q_bayes_posterior_raw" not in row:
            row["q_bayes_posterior_raw"] = raw_bayes[idx]
            row["q_bayes_posterior_pct_raw"] = _pct(raw_bayes[idx])
        if "q_temperature_bucket_posterior_raw" not in row:
            row["q_temperature_bucket_posterior_raw"] = raw_temperature[idx]
            row["q_temperature_bucket_posterior_pct_raw"] = _pct(raw_temperature[idx])
        if factor is not None and factor <= 1e-6 and not bool(row.get("impossible_by_observed_max")):
            row["suppressed_by_observed_feasibility"] = True
            row["suppressed_reason"] = "observed_trajectory_near_zero_bucket_feasibility"
        else:
            row["suppressed_by_observed_feasibility"] = False
        if cal_sum > 0 and adjusted_calibrated[idx] is not None:
            q_cal_adj = max(0.0, float(adjusted_calibrated[idx]) / cal_sum)
            row["q_calibrated"] = q_cal_adj
            row["q_calibrated_pct"] = _pct(q_cal_adj)
        if bayes_sum > 0 and adjusted_bayes[idx] is not None:
            q_bayes_adj = max(0.0, float(adjusted_bayes[idx]) / bayes_sum)
            row["q_bayes_posterior"] = q_bayes_adj
            row["q_bayes_posterior_pct"] = _pct(q_bayes_adj)
        if temperature_sum > 0 and adjusted_temperature[idx] is not None:
            q_temperature_adj = max(0.0, float(adjusted_temperature[idx]) / temperature_sum)
            row["q_temperature_bucket_posterior"] = q_temperature_adj
            row["q_temperature_bucket_posterior_pct"] = _pct(q_temperature_adj)
    return guarded


def _soft_forecast_peak(features: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    for key, source in (
        ("settlement_source_forecast_peak_c", "wu_forecast_peak_c"),
        ("wu_forecast_peak_c", "wu_forecast_peak_c"),
        ("weather_com_forecast_peak_c", "weather_com_forecast_peak_c"),
        ("twc_forecast_peak_c", "twc_forecast_peak_c"),
        ("taf_forecast_peak_c", "taf_forecast_peak_c"),
        ("forecast_peak_temp_c", "forecast_peak_temp_c"),
        ("forecast_peak_temp", "forecast_peak_temp"),
        ("model_peak_temp_c", "model_peak_temp_c"),
    ):
        value = _to_float(features.get(key))
        if value is not None:
            return value, source
    return None, ""


def _temperature_trajectory_status(
    features: Mapping[str, Any],
    temperature_trend: Optional[Mapping[str, Any]] = None,
    feature_groups: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    observed = _to_float(features.get("observed_temp_c"))
    observed_max, observed_max_source, observed_max_na_reason = _hard_observed_today_max_c(features)
    observed_path, observed_path_source = _observed_path_temperature_c(observed, observed_max)
    rise_info = _trusted_temperature_rise_rate(features)
    rise = _to_float(rise_info.get("value"))
    forecast_peak = _to_float(features.get("forecast_peak_temp"))
    if forecast_peak is None:
        forecast_peak = _to_float(features.get("forecast_peak_temp_c"))
    soft_forecast, soft_source = _soft_forecast_peak(features)
    if forecast_peak is None:
        forecast_peak = soft_forecast
    physical_modifier = _physical_weather_modifier(features, feature_groups)
    physical_adjustment = _to_float(physical_modifier.get("peak_mu_adjustment_c"))
    physical_adjusted_peak = forecast_peak
    if (
        physical_adjusted_peak is not None
        and physical_adjustment is not None
        and abs(float(physical_adjustment)) > 1e-9
    ):
        physical_adjusted_peak = float(physical_adjusted_peak) + float(physical_adjustment)
        if observed_path is not None:
            physical_adjusted_peak = max(float(observed_path), float(physical_adjusted_peak))
    trend = temperature_trend if isinstance(temperature_trend, Mapping) else {}
    momentum = trend.get("temperature_momentum") if isinstance(trend.get("temperature_momentum"), Mapping) else {}
    momentum_rates = momentum.get("rates_c_per_hour") if isinstance(momentum.get("rates_c_per_hour"), Mapping) else {}
    has_momentum = any(_to_float(value) is not None for value in momentum_rates.values()) if isinstance(momentum_rates, Mapping) else False
    peak_meta = features.get("peak_estimator") if isinstance(features.get("peak_estimator"), Mapping) else {}
    regime_v1 = (
        peak_meta.get("observed_temperature_trajectory_regime_v1")
        if isinstance(peak_meta.get("observed_temperature_trajectory_regime_v1"), Mapping)
        else {}
    )
    regime_path_mu = _to_float(regime_v1.get("path_mu_c") if isinstance(regime_v1, Mapping) else None)
    regime_path_sigma = _to_float(regime_v1.get("path_sigma_c") if isinstance(regime_v1, Mapping) else None)
    regime_rate = _to_float(regime_v1.get("rate_hat_c_per_hour") if isinstance(regime_v1, Mapping) else None)
    regime_accel = _to_float(regime_v1.get("accel_hat_c_per_hour2") if isinstance(regime_v1, Mapping) else None)
    regime_forcing_turn_probability = _to_float(
        regime_v1.get("forcing_turn_probability") if isinstance(regime_v1, Mapping) else None
    )
    regime_forcing_onset_probs = (
        regime_v1.get("forcing_onset_probs")
        if isinstance(regime_v1, Mapping) and isinstance(regime_v1.get("forcing_onset_probs"), Mapping)
        else {}
    )
    regime_forcing_contributors = (
        regime_v1.get("forcing_contributors")
        if isinstance(regime_v1, Mapping) and isinstance(regime_v1.get("forcing_contributors"), list)
        else []
    )
    regime_evidence = _to_float(
        regime_v1.get("effective_evidence")
        if isinstance(regime_v1, Mapping) and regime_v1.get("effective_evidence") is not None
        else regime_v1.get("evidence")
        if isinstance(regime_v1, Mapping)
        else None
    )
    regime_available = bool(
        isinstance(regime_v1, Mapping)
        and regime_v1.get("available")
        and regime_path_mu is not None
        and regime_path_sigma is not None
        and regime_path_sigma > 0
    )
    has_observed_anchor = observed is not None or observed_max is not None
    has_feature_rate = rise is not None or has_momentum
    has_trajectory_rate = has_feature_rate or regime_rate is not None
    trajectory_plumbing_mismatch = bool(regime_available and not has_feature_rate and regime_rate is not None)
    gate_raw = features.get("temperature_momentum_posterior_gate_allowed")
    gate_allowed = None
    if isinstance(gate_raw, bool):
        gate_allowed = gate_raw
    elif str(gate_raw).strip().lower() in {"1", "true", "yes", "on"}:
        gate_allowed = True
    elif str(gate_raw).strip().lower() in {"0", "false", "no", "off"}:
        gate_allowed = False
    momentum_state = str(
        features.get("temperature_momentum_realization_state")
        or momentum.get("state")
        or "missing"
    ).strip().lower()
    momentum_effective = _to_float(
        features.get("temperature_momentum_effective_c_per_hour")
        if features.get("temperature_momentum_effective_c_per_hour") is not None
        else momentum.get("effective_rate_c_per_hour")
    )
    trajectory_effective = bool(
        regime_available
        and regime_evidence is not None
        and float(regime_evidence) > 0.0
        and gate_allowed is not False
    )
    non_positive_rate = bool(
        (rise is not None and float(rise) <= 0.0)
        or (momentum_effective is not None and float(momentum_effective) <= 0.0)
        or (
            not has_feature_rate
            and regime_rate is not None
            and float(regime_rate) <= 0.0
        )
    )
    missing: List[str] = []
    if not has_observed_anchor:
        missing.append("observed_temperature_missing")
    if not has_trajectory_rate:
        missing.append("temperature_momentum_or_rise_rate_missing")
    if forecast_peak is None:
        missing.append("forecast_peak_missing")
    upstream_reason = str(features.get("temperature_trajectory_na_reason") or "").strip()
    rise_na_reason = str(rise_info.get("na_reason") or "").strip()
    if rise_na_reason and rise is None and rise_na_reason not in missing:
        missing.append(rise_na_reason)
    if upstream_reason and upstream_reason not in missing:
        missing.insert(0, upstream_reason)
    available = bool(has_observed_anchor and has_trajectory_rate)
    if available and trajectory_effective:
        state = "available"
        label = "可用"
        summary = "实况温度轨迹已形成后验似然，可用于判断目标桶兑现。"
    elif available and (
        gate_allowed is False
        or momentum_state in {"cooling", "not_realized", "flat"}
        or not regime_available
        or (regime_evidence is not None and float(regime_evidence) <= 0.0)
        or non_positive_rate
    ):
        state = "not_effective"
        label = "未形成有效后验"
        if momentum_state == "cooling" or (rise is not None and float(rise) < 0.0):
            summary = "轨迹数据可用，但最新动量为降温，后验轨迹通道被门控拦截；只用于确认已观测最高，不提供继续升温似然。"
        elif not regime_available:
            summary = "轨迹数据可用，但 regime_v1 未提供可用后验轨迹；只用于确认已观测最高，不提供继续升温似然。"
        else:
            summary = "轨迹数据可用，但动量未确认持续升温，后验轨迹通道未启用。"
    else:
        state = "partial" if forecast_peak is not None or soft_forecast is not None else "missing"
        label = "数据不全" if state == "partial" else "缺失"
        summary = "实况温度轨迹数据不全；只展示后验/软预报，不作轨迹兑现判定。"
    physical_state = str(physical_modifier.get("state") or "").strip()
    if physical_state == "cooling" and physical_adjustment is not None:
        summary += f" AWC/METAR 或 WU/TWC 云雨证据压制后段升温，峰值路径修正 {_fmt_c(physical_adjustment, 2)}。"
    elif physical_state == "warming" and physical_adjustment is not None:
        summary += f" AWC/METAR 或 WU/TWC 晴空/少云证据支持升温兑现，峰值路径修正 +{_fmt_c(physical_adjustment, 2)}。"
    if trajectory_plumbing_mismatch:
        summary += " 诊断：regime_v1 已提供轨迹 rate，但行级动量字段未同步，已按 v1 字段补齐状态。"
    return {
        "available": available,
        "state": state,
        "label": label,
        "na_reason": ",".join(missing) if missing else "",
        "missing_fields": missing,
        "diagnostic_flags": ["trajectory_plumbing_mismatch"] if trajectory_plumbing_mismatch else [],
        "trajectory_plumbing_mismatch": trajectory_plumbing_mismatch,
        "observed_temp_c": _round(observed, 2),
        "observed_today_max_c": _round(observed_max, 2),
        "observed_today_max_source": observed_max_source,
        "observed_today_max_na_reason": observed_max_na_reason,
        "observed_path_temp_c": _round(observed_path, 2),
        "observed_path_temp_source": observed_path_source,
        "temperature_rise_rate_c_per_hour": _round(rise, 2),
        "temperature_rise_rate_source": str(rise_info.get("source") or ""),
        "temperature_rise_rate_window": str(rise_info.get("window") or ""),
        "temperature_rise_rate_trusted": bool(rise_info.get("trusted")),
        "temperature_rise_rate_na_reason": rise_na_reason,
        "temperature_trajectory_regime_v1_available": regime_available,
        "temperature_trajectory_path_mu_c": _round(regime_path_mu, 2),
        "temperature_trajectory_path_sigma_c": _round(regime_path_sigma, 2),
        "temperature_trajectory_evidence": _round(regime_evidence, 3),
        "temperature_trajectory_rate_hat_c_per_hour": _round(regime_rate, 2),
        "temperature_trajectory_accel_hat_c_per_hour2": _round(regime_accel, 3),
        "temperature_trajectory_forcing_turn_probability": _round(regime_forcing_turn_probability, 3),
        "temperature_trajectory_forcing_onset_probs": dict(regime_forcing_onset_probs),
        "temperature_trajectory_forcing_contributors": regime_forcing_contributors[:5],
        "temperature_trajectory_effective": trajectory_effective,
        "temperature_momentum_effective_c_per_hour": _round(momentum_effective, 2),
        "temperature_momentum_posterior_gate_allowed": gate_allowed,
        "forecast_peak_temp_c": _round(forecast_peak, 2),
        "forecast_peak_temp_base_c": _round(forecast_peak, 2),
        "physical_weather_adjusted_peak_c": _round(physical_adjusted_peak, 2),
        "physical_weather_state": physical_modifier.get("state"),
        "physical_weather_peak_mu_adjustment_c": physical_modifier.get("peak_mu_adjustment_c"),
        "physical_weather_modifier": physical_modifier,
        "soft_forecast_peak_c": _round(soft_forecast, 2),
        "soft_forecast_source": soft_source,
        "temperature_momentum_state": momentum_state,
        "summary": summary,
    }


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))


def _temperature_path_probability_for_bucket(
    label: Any,
    *,
    observed_max_c: Optional[float],
    peak_mu_c: Optional[float],
    peak_sigma_c: Optional[float],
    peak_mu_source: str = "peak_estimator_mu_c",
) -> Dict[str, Any]:
    text = str(label or "").strip()
    threshold = _bucket_temp(text)
    if not text or threshold is None:
        return {
            "available": False,
            "bucket": text,
            "reason": "bucket_threshold_missing",
        }
    is_low_boundary = _is_lower_boundary_bucket(text)
    mode = "hold_at_or_below_boundary" if is_low_boundary else "reach_at_or_above_lower_edge"
    if observed_max_c is not None and math.isfinite(float(observed_max_c)):
        if is_low_boundary and float(observed_max_c) > float(threshold) + 1e-9:
            return {
                "available": True,
                "bucket": text,
                "mode": mode,
                "probability": 0.0,
                "probability_pct": 0.0,
                "threshold_c": _round(threshold, 2),
                "source": "hard_observed_max_exceeded_boundary",
                "observed_max_c": _round(observed_max_c, 2),
            }
        if (not is_low_boundary) and float(observed_max_c) >= float(threshold) - 1e-9:
            return {
                "available": True,
                "bucket": text,
                "mode": mode,
                "probability": 1.0,
                "probability_pct": 100.0,
                "threshold_c": _round(threshold, 2),
                "source": "hard_observed_max_reached_lower_edge",
                "observed_max_c": _round(observed_max_c, 2),
            }
    if peak_mu_c is None or not math.isfinite(float(peak_mu_c)):
        return {
            "available": False,
            "bucket": text,
            "mode": mode,
            "threshold_c": _round(threshold, 2),
            "reason": "peak_estimator_mu_missing",
            "observed_max_c": _round(observed_max_c, 2),
        }
    sigma = peak_sigma_c if peak_sigma_c is not None and math.isfinite(float(peak_sigma_c)) else None
    if sigma is None or float(sigma) <= 0.05:
        sigma = 1.25
    z = (float(threshold) - float(peak_mu_c)) / max(float(sigma), 0.05)
    probability = _normal_cdf(z) if is_low_boundary else 1.0 - _normal_cdf(z)
    probability = max(0.0, min(1.0, float(probability)))
    return {
        "available": True,
        "bucket": text,
        "mode": mode,
        "probability": _round(probability, 4),
        "probability_pct": _pct(probability),
        "threshold_c": _round(threshold, 2),
        "source": "peak_estimator_error_band" if peak_mu_source == "peak_estimator_mu_c" else "soft_forecast_error_band",
        "peak_estimator_mu_source": peak_mu_source,
        "observed_max_c": _round(observed_max_c, 2),
        "peak_estimator_mu_c": _round(peak_mu_c, 2),
        "peak_estimator_sigma_c": _round(sigma, 2),
    }


def _temperature_path_reliability_label(score: float) -> Tuple[str, str]:
    if score >= 0.76:
        return "high", "High"
    if score >= 0.50:
        return "medium", "Medium"
    if score >= 0.28:
        return "low", "Low"
    return "missing", "缺失"


_TEMPERATURE_PATH_CALIBRATION_CACHE: Dict[str, Dict[str, Any]] = {}


def _temperature_path_calibration_path() -> str:
    return str(
        os.environ.get("PM_TEMPERATURE_PATH_CALIBRATION_PATH")
        or (DEFAULT_DB_PATH.parent / "temperature_path_calibration.json")
    )


def _load_temperature_path_calibration(path: Optional[str] = None) -> Dict[str, Any]:
    cal_path = str(path or _temperature_path_calibration_path() or "").strip()
    if not cal_path:
        return {}
    try:
        mtime = os.path.getmtime(cal_path)
    except Exception:
        return {}
    cached = _TEMPERATURE_PATH_CALIBRATION_CACHE.get(cal_path)
    if isinstance(cached, Mapping) and cached.get("mtime") == mtime and isinstance(cached.get("data"), Mapping):
        return dict(cached.get("data") or {})
    try:
        payload = json.loads(Path(cal_path).read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    data = payload if isinstance(payload, Mapping) else {}
    _TEMPERATURE_PATH_CALIBRATION_CACHE[cal_path] = {"mtime": mtime, "data": dict(data)}
    return dict(data)


def _temperature_path_peak_source_aliases(source: str) -> Tuple[str, ...]:
    text = str(source or "").strip()
    soft_aliases = {
        "forecast_peak_temp_c",
        "forecast_peak_temp",
        "settlement_source_forecast_peak_c",
        "wu_forecast_peak_c",
        "weather_com_forecast_peak_c",
        "twc_forecast_peak_c",
    }
    if text in soft_aliases:
        ordered = [
            text,
            "forecast_peak_temp_c",
            "settlement_source_forecast_peak_c",
            "wu_forecast_peak_c",
            "weather_com_forecast_peak_c",
            "twc_forecast_peak_c",
        ]
        return tuple(dict.fromkeys(item for item in ordered if item))
    return (text,) if text else ("",)


def _temperature_path_local_hour(features: Mapping[str, Any], refresh_cadence: Mapping[str, Any]) -> Optional[float]:
    for key in ("observation_hour_local", "local_hour", "hour_local"):
        value = _to_float(features.get(key))
        if value is not None:
            return float(value)
    latest = _parse_datetime(refresh_cadence.get("latest_observation_time_utc"))
    if latest is None:
        latest = _parse_datetime(refresh_cadence.get("latest_observation_time_local"))
    if latest is not None:
        return _local_hour_fraction(latest)
    return None


def _temperature_path_error_band(
    features: Mapping[str, Any],
    refresh_cadence: Mapping[str, Any],
    *,
    peak_mu_source: str,
    upstream_sigma_c: Optional[float],
) -> Dict[str, Any]:
    raw_sigma = _to_float(upstream_sigma_c)
    if raw_sigma is not None and raw_sigma > 0.05:
        return {
            "sigma_c": _round(raw_sigma, 3),
            "source": "upstream_peak_estimator_sigma",
            "calibration_available": False,
            "calibration_path": None,
            "sample_n": None,
            "generated_at": None,
            "fallback": False,
        }

    cal_path = _temperature_path_calibration_path()
    cal = _load_temperature_path_calibration(cal_path)
    station = str(
        features.get("station_id")
        or features.get("station")
        or features.get("loc_key")
        or ""
    ).strip()
    local_hour = _temperature_path_local_hour(features, refresh_cadence)
    source_aliases = set(_temperature_path_peak_source_aliases(peak_mu_source))
    min_sample_n = int(_to_float(cal.get("min_sample_n")) or 30) if cal else 30
    min_deployable_sigma = _to_float(cal.get("min_deployable_sigma_c")) if cal else None
    if min_deployable_sigma is None or min_deployable_sigma <= 0.05:
        min_deployable_sigma = 0.35
    skipped_entries: List[Dict[str, Any]] = []

    def _coverage_summary() -> Dict[str, Any]:
        report = cal.get("coverage_report") if isinstance(cal, Mapping) else {}
        if not isinstance(report, Mapping):
            return {}
        keys = (
            "sample_n",
            "hours_to_event_end_min",
            "hours_to_event_end_max",
            "near_close_sample_n",
            "near_close_share",
            "near_close_warning",
            "early_sample_n",
            "early_share",
        )
        return {key: report.get(key) for key in keys if report.get(key) is not None}

    def _entry_sample_n(entry: Mapping[str, Any]) -> int:
        value = entry.get("sample_n")
        if value is None:
            value = entry.get("n")
        try:
            return int(value or 0)
        except Exception:
            return 0

    def _entry_label(entry: Mapping[str, Any], scope: str) -> Dict[str, Any]:
        return {
            "scope": scope,
            **{
                key: entry.get(key)
                for key in (
                    "station_id",
                    "station",
                    "peak_mu_source",
                    "source",
                    "local_hour_min",
                    "local_hour_max",
                    "sample_n",
                    "sigma_c",
                    "rmse_c",
                    "mae_c",
                    "deployable_candidate",
                    "deploy_block_reason",
                )
                if entry.get(key) not in (None, "")
            },
        }

    def _usable_reason(entry: Mapping[str, Any]) -> Tuple[bool, str]:
        sigma = _to_float(entry.get("sigma_c") or entry.get("rmse_c") or entry.get("mae_c"))
        sample_n = _entry_sample_n(entry)
        if sample_n < min_sample_n:
            return False, "sample_n_below_minimum"
        if sigma is None or sigma <= 0.05:
            return False, "sigma_missing_or_invalid"
        if sigma < float(min_deployable_sigma):
            return False, "sigma_below_conservative_floor"
        if entry.get("deployable_candidate") is False:
            return False, str(entry.get("deploy_block_reason") or "calibration_entry_not_deployable")
        return True, ""

    def _maybe_accept(entry: Mapping[str, Any], scope: str) -> Optional[Mapping[str, Any]]:
        usable, reason = _usable_reason(entry)
        if usable:
            return entry
        skipped = _entry_label(entry, scope)
        skipped["reason"] = reason
        skipped_entries.append(skipped)
        return None

    matched_entry: Optional[Mapping[str, Any]] = None
    matched_scope = ""
    if isinstance(cal.get("buckets"), Sequence) and not isinstance(cal.get("buckets"), (str, bytes)):
        for raw in cal.get("buckets") or []:
            if not isinstance(raw, Mapping):
                continue
            raw_station = str(raw.get("station_id") or raw.get("station") or raw.get("loc_key") or "").strip()
            if raw_station and station and raw_station != station:
                continue
            raw_source = str(raw.get("peak_mu_source") or raw.get("source") or "").strip()
            if raw_source and raw_source not in source_aliases:
                continue
            hour_min = _to_float(raw.get("local_hour_min"))
            hour_max = _to_float(raw.get("local_hour_max"))
            if local_hour is not None and hour_min is not None and float(local_hour) < float(hour_min):
                continue
            if local_hour is not None and hour_max is not None and float(local_hour) > float(hour_max):
                continue
            accepted = _maybe_accept(raw, "bucket")
            if accepted is not None:
                matched_entry = accepted
                matched_scope = "bucket"
                break

    if matched_entry is None and isinstance(cal.get("by_peak_mu_source"), Mapping):
        by_source = cal.get("by_peak_mu_source", {})
        for alias in _temperature_path_peak_source_aliases(peak_mu_source):
            entry = by_source.get(alias) if isinstance(by_source, Mapping) else None
            if isinstance(entry, Mapping):
                accepted = _maybe_accept(entry, "by_peak_mu_source")
                if accepted is not None:
                    matched_entry = accepted
                    matched_scope = "by_peak_mu_source"
                    break

    if matched_entry is None and isinstance(cal.get("default"), Mapping):
        accepted = _maybe_accept(cal.get("default") or {}, "default")
        if accepted is not None:
            matched_entry = accepted
            matched_scope = "default"

    if matched_entry is not None:
        sigma = _to_float(
            matched_entry.get("sigma_c")
            or matched_entry.get("rmse_c")
            or matched_entry.get("mae_c")
        )
        return {
            "sigma_c": _round(sigma, 3),
            "source": "historical_error_calibration",
            "calibration_available": True,
            "calibration_path": cal_path,
            "sample_n": _entry_sample_n(matched_entry),
            "generated_at": str(cal.get("generated_at") or ""),
            "fallback": False,
            "match_scope": matched_scope,
            "requested_peak_mu_source": str(peak_mu_source or ""),
            "calibration_coverage": _coverage_summary(),
            "min_deployable_sigma_c": _round(min_deployable_sigma, 3),
            "skipped_calibration_entries": skipped_entries[:5],
            "matched": {
                key: matched_entry.get(key)
                for key in (
                    "station_id",
                    "station",
                    "peak_mu_source",
                    "local_hour_min",
                    "local_hour_max",
                    "mae_c",
                    "rmse_c",
                    "sigma_c",
                    "deployable_candidate",
                    "deploy_block_reason",
                )
                if matched_entry.get(key) not in (None, "")
            },
        }

    default_sigma = _to_float(cal.get("default_sigma_c")) if cal else None
    if default_sigma is None or default_sigma < float(min_deployable_sigma):
        default_sigma = 1.25
    return {
        "sigma_c": _round(default_sigma, 3),
        "source": "default_error_band",
        "calibration_available": bool(cal),
        "calibration_path": cal_path if cal else None,
        "sample_n": None,
        "generated_at": str(cal.get("generated_at") or "") if cal else None,
        "fallback": True,
        "fallback_reason": (
            "no_deployable_historical_error_calibration"
            if skipped_entries
            else "no_matching_historical_error_calibration"
        ),
        "calibration_coverage": _coverage_summary(),
        "min_deployable_sigma_c": _round(min_deployable_sigma, 3),
        "skipped_calibration_entries": skipped_entries[:5],
    }


def _temperature_path_distribution(
    rows: Sequence[Mapping[str, Any]],
    target_bucket_outlook: Mapping[str, Any],
    trajectory_status: Mapping[str, Any],
    realization_reliability: Mapping[str, Any],
    refresh_cadence: Mapping[str, Any],
    features: Mapping[str, Any],
    target_observation_state: Mapping[str, Any],
    feature_groups: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(target_bucket_outlook, Mapping) or not target_bucket_outlook.get("available"):
        return {
            "available": False,
            "version": "temperature_path_distribution_v1",
            "reason": "target_bucket_outlook_missing",
        }
    target_label = str(target_bucket_outlook.get("target_bucket") or "").strip()
    second_label = str(target_bucket_outlook.get("second_target_bucket") or "").strip()
    if not target_label:
        return {
            "available": False,
            "version": "temperature_path_distribution_v1",
            "reason": "target_bucket_missing",
        }
    observed_max, observed_max_source, observed_max_na_reason = _hard_observed_today_max_c(features)
    if observed_max is None:
        observed_max = _to_float(target_observation_state.get("observed_max_c"))
        observed_max_source = str(target_observation_state.get("source") or observed_max_source or "")
    observed_current_temp = _to_float(features.get("observed_temp_c"))
    observed_temp, observed_temp_source = _observed_path_temperature_c(observed_current_temp, observed_max)
    peak_mu = _to_float(features.get("peak_estimator_mu_c"))
    has_peak_estimator_mu = peak_mu is not None
    peak_mu_source = "peak_estimator_mu_c"
    if peak_mu is None:
        soft_peak, soft_source = _soft_forecast_peak(features)
        if soft_peak is not None:
            peak_mu = soft_peak
            peak_mu_source = soft_source or "soft_forecast_peak_c"
    physical_modifier = _physical_weather_modifier(features, feature_groups)
    base_peak_mu = peak_mu
    physical_adjustment = _to_float(physical_modifier.get("peak_mu_adjustment_c"))
    if (
        peak_mu is not None
        and physical_adjustment is not None
        and abs(float(physical_adjustment)) > 1e-9
    ):
        adjusted_peak = float(peak_mu) + float(physical_adjustment)
        if observed_temp is not None:
            adjusted_peak = max(float(observed_temp), adjusted_peak)
        peak_mu = adjusted_peak
    peak_sigma = _to_float(features.get("peak_estimator_sigma_c"))
    error_band = _temperature_path_error_band(
        features,
        refresh_cadence,
        peak_mu_source=peak_mu_source,
        upstream_sigma_c=peak_sigma,
    )
    effective_sigma = _to_float(error_band.get("sigma_c"))
    physical_sigma_add = _to_float(physical_modifier.get("peak_sigma_add_c"))
    if effective_sigma is not None and physical_sigma_add is not None and physical_sigma_add > 0:
        effective_sigma = max(0.05, float(effective_sigma) + float(physical_sigma_add))
    source_age_min = _to_float(refresh_cadence.get("observation_age_minutes"))
    source_stale = bool(refresh_cadence.get("source_stale"))
    trajectory_available = bool(trajectory_status.get("available"))
    sample_coverage = _to_float(realization_reliability.get("sample_coverage")) or 0.0
    confidence = _to_float(realization_reliability.get("confidence")) or 0.0
    sigma_score = 0.0
    if effective_sigma is not None:
        sigma_score = max(0.0, min(1.0, 1.0 - max(0.0, float(effective_sigma) - 0.35) / 2.65))
    freshness_score = 0.0
    if source_age_min is not None:
        freshness_score = max(0.0, min(1.0, 1.0 - max(0.0, float(source_age_min) - 20.0) / 160.0))
    elif not source_stale:
        freshness_score = 0.35
    if source_stale:
        freshness_score = min(freshness_score, 0.25)
    physical_path_scale = _to_float(physical_modifier.get("path_probability_scale"))
    if physical_path_scale is None or physical_path_scale <= 0:
        physical_path_scale = 1.0
    physical_state = str(physical_modifier.get("state") or "").strip()
    base_score = (
        0.25 * (1.0 if trajectory_available else 0.35)
        + 0.25 * max(0.0, min(1.0, sample_coverage))
        + 0.20 * max(0.0, min(1.0, confidence))
        + 0.20 * sigma_score
        + 0.10 * freshness_score
    )
    reliability_state, reliability_label = _temperature_path_reliability_label(base_score)

    row_by_label: Dict[str, Mapping[str, Any]] = {}
    for row in rows or []:
        if isinstance(row, Mapping):
            label = str(row.get("outcome") or row.get("label") or "").strip()
            if label:
                row_by_label[label] = row

    def _posterior(label: str) -> Tuple[Optional[float], Optional[float], str]:
        row = row_by_label.get(label) or {}
        prob, field = _target_bucket_probability(row)
        return prob, _pct(prob) if prob is not None else None, field

    def _leg(label: str, role: str) -> Dict[str, Any]:
        posterior, posterior_pct, posterior_field = _posterior(label)
        path = _temperature_path_probability_for_bucket(
            label,
            observed_max_c=observed_max,
            peak_mu_c=peak_mu,
            peak_sigma_c=effective_sigma,
            peak_mu_source=peak_mu_source,
        )
        path_prob = _to_float(path.get("probability"))
        threshold_c = _to_float(path.get("threshold_c"))
        path_prob_raw = path_prob
        weather_scale_applied = False
        if (
            path_prob is not None
            and abs(float(physical_path_scale) - 1.0) > 1e-9
            and threshold_c is not None
            and observed_temp is not None
            and float(threshold_c) > float(observed_temp) + 0.25
        ):
            path_prob = max(0.0, min(1.0, float(path_prob) * float(physical_path_scale)))
            weather_scale_applied = True
        path_lcb = max(0.0, path_prob - (1.0 - base_score) * 0.25) if path_prob is not None else None
        return {
            "role": role,
            "bucket": label,
            "posterior": posterior,
            "posterior_pct": posterior_pct,
            "posterior_field": posterior_field,
            "path_probability": _round(path_prob, 4) if path_prob is not None else None,
            "path_probability_pct": _pct(path_prob) if path_prob is not None else None,
            "path_probability_raw": _round(path_prob_raw, 4) if path_prob_raw is not None else None,
            "path_probability_raw_pct": _pct(path_prob_raw) if path_prob_raw is not None else None,
            "path_probability_weather_scale": _round(physical_path_scale, 3),
            "path_probability_weather_scale_applied": weather_scale_applied,
            "path_probability_lcb": _round(path_lcb, 4),
            "path_probability_lcb_pct": _pct(path_lcb) if path_lcb is not None else None,
            "path_probability_source": path.get("source"),
            "path_probability_mode": path.get("mode"),
            "path_probability_available": bool(path.get("available")),
            "path_probability_na_reason": path.get("reason"),
            "threshold_c": path.get("threshold_c"),
        }

    bucket_distribution: List[Dict[str, Any]] = []
    seen_bucket_labels: Set[str] = set()
    for label in row_by_label:
        if label in seen_bucket_labels:
            continue
        seen_bucket_labels.add(label)
        role = "target" if label == target_label else ("second_target" if label == second_label else "bucket")
        bucket_distribution.append(_leg(label, role))
    if target_label not in seen_bucket_labels:
        bucket_distribution.append(_leg(target_label, "target"))
        seen_bucket_labels.add(target_label)
    if second_label and second_label not in seen_bucket_labels:
        bucket_distribution.append(_leg(second_label, "second_target"))
        seen_bucket_labels.add(second_label)
    target_leg = next(
        (dict(leg, role="target") for leg in bucket_distribution if leg.get("bucket") == target_label),
        _leg(target_label, "target"),
    )
    second_leg = (
        next(
            (dict(leg, role="second_target") for leg in bucket_distribution if leg.get("bucket") == second_label),
            _leg(second_label, "second_target"),
        )
        if second_label
        else None
    )
    target_path = _to_float(target_leg.get("path_probability"))
    target_lcb = _to_float(target_leg.get("path_probability_lcb"))
    second_path = _to_float(second_leg.get("path_probability")) if second_leg else None
    second_posterior = _to_float(second_leg.get("posterior")) if second_leg else None
    target_posterior = _to_float(target_leg.get("posterior"))
    local_hour = _temperature_path_local_hour(features, refresh_cadence)
    expected_peak_hour = _to_float(refresh_cadence.get("peak_hour_local"))
    expected_peak_meta = {
        "expected_peak_hour_source": str(refresh_cadence.get("peak_hour_source") or ""),
    }
    if expected_peak_hour is None:
        expected_peak_hour, expected_peak_meta = _expected_peak_hour_from_features(features)
    trajectory_state = str(trajectory_status.get("state") or "").strip()
    still_in_warming_window = bool(
        trajectory_available
        and not source_stale
        and trajectory_state not in {"cooling", "post_peak_cooling", "ended", "unavailable"}
    )
    warming_window_not_developed = bool(
        still_in_warming_window
        and local_hour is not None
        and float(local_hour) < max(12.0, float(expected_peak_hour) - 3.0)
    )
    risk_codes: List[str] = []
    if not trajectory_available:
        risk_codes.append("temperature_trajectory_partial")
    if physical_state == "cooling":
        risk_codes.append("physical_weather_cooling_likelihood")
    elif physical_state == "warming":
        risk_codes.append("physical_weather_warming_likelihood")
    if source_stale:
        risk_codes.append("hard_source_stale")
    if reliability_state in {"low", "missing"}:
        risk_codes.append("path_reliability_low")
    if target_lcb is None:
        risk_codes.append("target_path_probability_missing")
    elif target_lcb < 0.35:
        risk_codes.append("target_path_lcb_low")
    if second_path is not None and second_posterior is not None and second_path >= 0.45 and second_posterior >= 0.20:
        risk_codes.append("second_target_risk_high")
    skipped_calibration_entries = (
        error_band.get("skipped_calibration_entries")
        if isinstance(error_band.get("skipped_calibration_entries"), Sequence)
        and not isinstance(error_band.get("skipped_calibration_entries"), (str, bytes))
        else []
    )
    if skipped_calibration_entries:
        risk_codes.append("path_calibration_entry_skipped")
    if str(error_band.get("fallback_reason") or "") == "no_deployable_historical_error_calibration":
        risk_codes.append("path_calibration_fallback_no_deployable_entry")
    calibration_coverage = (
        error_band.get("calibration_coverage")
        if isinstance(error_band.get("calibration_coverage"), Mapping)
        else {}
    )
    near_close_calibration_warning = calibration_coverage.get("near_close_warning") is True
    if near_close_calibration_warning:
        risk_codes.append("path_calibration_near_close_dominated")

    base_min_target_path_lcb = 0.35
    min_target_path_lcb_effective = base_min_target_path_lcb
    calibration_risk_adjustment = {
        "applied": False,
        "reason": "",
        "base_min_target_path_lcb": base_min_target_path_lcb,
        "lcb_add": 0.0,
        "local_hour": _round(local_hour, 2),
        "expected_peak_hour_local": _round(expected_peak_hour, 2),
        "expected_peak_hour_source": str(expected_peak_meta.get("expected_peak_hour_source") or ""),
        "warming_window_not_developed": warming_window_not_developed,
    }
    target_path_source = str(target_leg.get("path_probability_source") or "")
    hard_source_already_reached_target = target_path_source == "hard_observed_max_reached_lower_edge"
    if (
        near_close_calibration_warning
        and warming_window_not_developed
        and not hard_source_already_reached_target
    ):
        min_target_path_lcb_effective = 0.50
        calibration_risk_adjustment.update(
            {
                "applied": True,
                "reason": "near_close_calibration_early_warming_window",
                "lcb_add": _round(min_target_path_lcb_effective - base_min_target_path_lcb, 4),
            }
        )
        risk_codes.append("path_calibration_near_close_early_window_lcb_raised")
        if target_lcb is not None and target_lcb < min_target_path_lcb_effective:
            risk_codes.append("target_path_lcb_below_calibration_adjusted_min")

    trade_follow_allowed = bool(
        reliability_state in {"medium", "high"}
        and not source_stale
        and target_posterior is not None
        and target_posterior >= 0.15
        and target_lcb is not None
        and target_lcb >= min_target_path_lcb_effective
        and "second_target_risk_high" not in risk_codes
    )
    if trade_follow_allowed:
        trade_reason = "posterior_path_reliability_and_freshness_pass"
    elif "second_target_risk_high" in risk_codes:
        trade_reason = "blocked_second_target_path_risk_high"
    elif source_stale:
        trade_reason = "blocked_hard_source_stale"
    elif reliability_state in {"low", "missing"}:
        trade_reason = "blocked_path_reliability_low"
    elif target_lcb is None:
        trade_reason = "blocked_target_path_probability_missing"
    elif calibration_risk_adjustment["applied"] and target_lcb < min_target_path_lcb_effective:
        trade_reason = "blocked_near_close_calibration_early_window_lcb"
    elif target_lcb < min_target_path_lcb_effective:
        trade_reason = "blocked_target_path_lcb_low"
    else:
        trade_reason = "blocked_target_posterior_low"

    risk_explanation_parts = [
        f"目标 {target_label} 落桶后验 {target_leg.get('posterior_pct') if target_leg.get('posterior_pct') is not None else '—'}%，路径执行概率 {target_leg.get('path_probability_pct') if target_leg.get('path_probability_pct') is not None else '—'}%。",
    ]
    if second_leg:
        risk_explanation_parts.append(
            f"第二目标 {second_label} 落桶后验 {second_leg.get('posterior_pct') if second_leg.get('posterior_pct') is not None else '—'}%，路径执行概率 {second_leg.get('path_probability_pct') if second_leg.get('path_probability_pct') is not None else '—'}%。"
        )
    risk_explanation_parts.append(
        f"可靠性 {reliability_label}；硬源年龄 {source_age_min:.1f}min。" if source_age_min is not None else f"可靠性 {reliability_label}；硬源年龄未知。"
    )
    if physical_modifier.get("available"):
        risk_explanation_parts.append(
            f"物理天气修正：{physical_modifier.get('label') or physical_state}，峰值 {_fmt_c(physical_adjustment, 2)}，路径缩放 {physical_modifier.get('path_probability_scale')}。"
        )
    error_sigma = _to_float(error_band.get("sigma_c"))
    error_source = str(error_band.get("source") or "")
    calibration_note = (
        f"误差带 {error_sigma:.2f}°C（{error_source}"
        + (f"/{error_band.get('match_scope')}" if error_band.get("match_scope") else "")
        + "）。"
        if error_sigma is not None
        else f"误差带来源 {error_source or '未知'}。"
    )
    if skipped_calibration_entries:
        first_skip = skipped_calibration_entries[0] if isinstance(skipped_calibration_entries[0], Mapping) else {}
        calibration_note += (
            f" 已跳过不可部署校准 {first_skip.get('scope') or 'entry'}："
            f"{first_skip.get('reason') or first_skip.get('deploy_block_reason') or 'not_deployable'}。"
        )
    if error_band.get("fallback"):
        calibration_note += f" 回退原因：{error_band.get('fallback_reason') or 'unknown'}。"
    if calibration_coverage.get("near_close_warning") is True:
        near_share = _to_float(calibration_coverage.get("near_close_share"))
        near_n = calibration_coverage.get("near_close_sample_n")
        share_text = f"{near_share * 100:.1f}%" if near_share is not None else "偏高"
        calibration_note += f" 校准样本偏近收盘：near-close {near_n or '—'} 条，占比 {share_text}，早盘路径风险需保守看待。"
    risk_explanation_parts.append(calibration_note)
    if risk_codes:
        risk_explanation_parts.append("风险：" + "、".join(risk_codes) + "。")
    else:
        risk_explanation_parts.append("未触发主要路径风险。")
    market_rows_with_quote = [
        row for row in row_by_label.values()
        if _row_market_probability(row)[0] is not None
    ]
    hard_source_state = {
        "available": observed_max is not None or observed_temp is not None,
        "observed_max_c": _round(observed_max, 2),
        "observed_temp_c": _round(observed_temp, 2),
        "observed_current_temp_c": _round(observed_current_temp, 2),
        "observed_path_temp_c": _round(observed_temp, 2),
        "observed_path_temp_source": observed_temp_source,
        "source": str(observed_max_source or features.get("observed_temp_source") or target_observation_state.get("source") or "").strip(),
        "source_stale": source_stale,
        "latest_observation_time_local": refresh_cadence.get("latest_observation_time_local"),
        "na_reason": (
            ""
            if (observed_max is not None or observed_temp is not None)
            else (
                "observed_temperature_missing"
                if observed_max_na_reason == "observed_today_max_missing"
                else (observed_max_na_reason or "observed_temperature_missing")
            )
        ),
    }
    model_forecast_available = peak_mu is not None
    market_available = bool(market_rows_with_quote)
    calibration_source = str(error_band.get("source") or "")
    calibration_fallback = bool(error_band.get("fallback"))
    historical_calibration_available = calibration_source == "historical_error_calibration" and not calibration_fallback
    hard_upper_bound_active = bool(
        _to_bool(features.get("temperature_path_hard_upper_bound_active")) is True
        or _to_bool(features.get("hard_upper_bound_active")) is True
    )
    hard_upper_bound_c = _to_float(features.get("temperature_path_hard_upper_bound_c"))
    if hard_upper_bound_c is None:
        hard_upper_bound_c = _to_float(features.get("hard_upper_bound_c"))
    path_state = {
        "still_in_warming_window": still_in_warming_window,
        "warming_window_state": "still_open" if still_in_warming_window else "not_open",
        "warming_window_reason": (
            "trajectory_available_and_hard_source_fresh"
            if still_in_warming_window
            else ("hard_source_stale" if source_stale else (trajectory_state or "trajectory_unavailable"))
        ),
        "hard_upper_bound_active": hard_upper_bound_active,
        "hard_upper_bound_c": _round(hard_upper_bound_c, 2),
        "hard_upper_bound_reason": (
            str(features.get("temperature_path_hard_upper_bound_reason") or features.get("hard_upper_bound_reason") or "")
            if hard_upper_bound_active
            else "no_hard_upper_bound_from_sources"
        ),
    }
    risk_explanation_parts.append(
        "升温窗口："
        + ("仍在" if still_in_warming_window else "不在")
        + "；硬上限："
        + ("是" if hard_upper_bound_active else "否")
        + "。"
    )
    input_coverage = {
        "version": "temperature_path_input_coverage_v1",
        "realtime_hard_source": {
            "available": bool(hard_source_state["available"]),
            "source": hard_source_state["source"],
            "source_stale": source_stale,
            "na_reason": hard_source_state["na_reason"],
        },
        "model_forecast": {
            "available": bool(model_forecast_available),
            "peak_mu_source": peak_mu_source if peak_mu is not None else "",
            "peak_mu_c": _round(peak_mu, 2),
            "na_reason": "" if model_forecast_available else "peak_forecast_missing",
        },
        "market": {
            "available": market_available,
            "covered_outcomes": len(market_rows_with_quote),
            "expected_outcomes": len(row_by_label),
            "na_reason": "" if market_available else "market_probability_missing",
        },
        "historical_error_calibration": {
            "available": bool(historical_calibration_available),
            "source": calibration_source,
            "sample_n": error_band.get("sample_n"),
            "fallback": calibration_fallback,
            "na_reason": (
                ""
                if historical_calibration_available
                else str(error_band.get("fallback_reason") or "historical_error_calibration_missing")
            ),
        },
        "path_probability": {
            "available": bool(target_leg.get("path_probability_available")),
            "target_na_reason": target_leg.get("path_probability_na_reason") or "",
            "second_target_na_reason": (
                second_leg.get("path_probability_na_reason") if second_leg else "second_target_missing"
            ) or "",
            "na_reason": "" if target_leg.get("path_probability_available") else (target_leg.get("path_probability_na_reason") or "target_path_probability_missing"),
        },
    }
    return {
        "available": True,
        "version": "temperature_path_distribution_v1",
        "definition": "温度轨迹不是单点预测；用实时硬源、峰值误差带、动量覆盖率、新鲜度和目标/第二目标落桶后验生成路径概率与交易跟随许可。",
        "target_bucket": target_label,
        "second_target_bucket": second_label or None,
        "target_bucket_posterior": target_leg.get("posterior"),
        "target_bucket_posterior_pct": target_leg.get("posterior_pct"),
        "target_bucket_posterior_field": target_leg.get("posterior_field"),
        "target_bucket_path_probability": target_leg.get("path_probability"),
        "target_bucket_path_probability_pct": target_leg.get("path_probability_pct"),
        "target_bucket_path_probability_lcb": target_leg.get("path_probability_lcb"),
        "target_bucket_path_probability_lcb_pct": target_leg.get("path_probability_lcb_pct"),
        "target_bucket_path_probability_na_reason": target_leg.get("path_probability_na_reason"),
        "second_target_bucket_posterior": second_leg.get("posterior") if second_leg else None,
        "second_target_bucket_posterior_pct": second_leg.get("posterior_pct") if second_leg else None,
        "second_target_bucket_posterior_field": second_leg.get("posterior_field") if second_leg else None,
        "second_target_bucket_path_probability": second_leg.get("path_probability") if second_leg else None,
        "second_target_bucket_path_probability_pct": second_leg.get("path_probability_pct") if second_leg else None,
        "second_target_bucket_path_probability_lcb": second_leg.get("path_probability_lcb") if second_leg else None,
        "second_target_bucket_path_probability_lcb_pct": second_leg.get("path_probability_lcb_pct") if second_leg else None,
        "second_target_bucket_path_probability_na_reason": second_leg.get("path_probability_na_reason") if second_leg else None,
        "target": target_leg,
        "second_target": second_leg,
        "bucket_distribution": bucket_distribution,
        "bucket_distribution_count": len(bucket_distribution),
        "observed_temp_c": _round(observed_temp, 2),
        "observed_current_temp_c": _round(observed_current_temp, 2),
        "observed_path_temp_c": _round(observed_temp, 2),
        "observed_path_temp_source": observed_temp_source,
        "observed_today_max_c": _round(observed_max, 2),
        "observed_today_max_source": observed_max_source,
        "observed_today_max_na_reason": observed_max_na_reason,
        "base_peak_estimator_mu_c": _round(base_peak_mu, 2),
        "peak_estimator_mu_c": _round(peak_mu, 2),
        "peak_estimator_mu_source": peak_mu_source if peak_mu is not None else None,
        "peak_estimator_sigma_c": _round(effective_sigma, 2),
        "peak_estimator_sigma_raw_c": _round(peak_sigma, 2),
        "physical_weather_modifier": physical_modifier,
        "physical_weather_state": physical_modifier.get("state"),
        "physical_weather_peak_mu_adjustment_c": physical_modifier.get("peak_mu_adjustment_c"),
        "physical_weather_path_probability_scale": physical_modifier.get("path_probability_scale"),
        "error_band": error_band,
        "hard_source_state": hard_source_state,
        "path_state": path_state,
        "input_coverage": input_coverage,
        "trajectory_available": trajectory_available,
        "trajectory_state": trajectory_state,
        "trajectory_na_reason": str(trajectory_status.get("na_reason") or ""),
        "sample_coverage": _round(sample_coverage, 4),
        "freshness": {
            "source_stale": source_stale,
            "observation_age_minutes": _round(source_age_min, 1),
            "latest_observation_time_local": refresh_cadence.get("latest_observation_time_local"),
            "freshness_score": _round(freshness_score, 4),
        },
        "reliability": {
            "state": reliability_state,
            "label": reliability_label,
            "score": _round(base_score, 4),
            "confidence": _round(confidence, 4),
            "sigma_score": _round(sigma_score, 4),
        },
        "risk_codes": risk_codes,
        "risk_explanation": " ".join(risk_explanation_parts),
        "trade_follow": {
            "allowed": trade_follow_allowed,
            "reason": trade_reason,
            "min_target_path_lcb": base_min_target_path_lcb,
            "base_min_target_path_lcb": base_min_target_path_lcb,
            "min_target_path_lcb_effective": _round(min_target_path_lcb_effective, 4),
            "min_target_posterior": 0.15,
            "requires_reliability": "medium_or_high",
            "requires_fresh_hard_source": True,
            "calibration_risk_adjustment": calibration_risk_adjustment,
        },
        "semantics": {
            "posterior": "final_settlement_bucket_probability",
            "path_probability": "probability_remaining_temperature_path_reaches_or_holds_bucket_boundary",
        },
    }


def _high_temp_amplification_guard(features: Mapping[str, Any]) -> Dict[str, Any]:
    observed_current, observed_source = _observed_current_temperature_c(features)
    observed_max, observed_max_source, observed_max_na_reason = _hard_observed_today_max_c(features)
    rise_info = _trusted_temperature_rise_rate(features)
    rise = _to_float(rise_info.get("value"))
    soft_forecast, soft_source = _soft_forecast_peak(features)
    gap = soft_forecast - observed_current if observed_current is not None and soft_forecast is not None else None
    gap_threshold_c = _to_float(os.environ.get("PM_CHENGDU_HIGH_TEMP_GAP_GUARD_C"))
    if gap_threshold_c is None or not math.isfinite(float(gap_threshold_c)):
        gap_threshold_c = 2.0
    sustained_momentum_rate_threshold = _to_float(os.environ.get("PM_CHENGDU_SUSTAINED_MOMENTUM_MIN_C_PER_HOUR"))
    if sustained_momentum_rate_threshold is None or not math.isfinite(float(sustained_momentum_rate_threshold)):
        sustained_momentum_rate_threshold = 0.25
    momentum_rate = _to_float(features.get("temperature_momentum_effective_c_per_hour"))
    momentum_state = str(features.get("temperature_momentum_realization_state") or "").strip()
    momentum_warming = bool(
        (
            _to_bool(features.get("temperature_momentum_sustained_warming")) is True
            or momentum_state == "sustained_warming"
        )
        and momentum_rate is not None
        and float(momentum_rate) >= float(sustained_momentum_rate_threshold)
    )
    active = bool(
        gap is not None
        and gap > float(gap_threshold_c)
        and rise is not None
        and rise <= 0.0
        and not momentum_warming
    )
    if active:
        gap_text = f"{float(gap):.1f}°C" if gap is not None else "明显"
        rate_text = f"{float(rise):.2f}°C/h" if rise is not None else "不明"
        conclusion = f"实况升温落后软预报约 {gap_text}，当前升温率 {rate_text}，先压住高温桶放大。"
    elif observed_current is None or soft_forecast is None or rise is None:
        conclusion = "实况门控待评估：缺少实况温度、软预报或升温率。"
    else:
        conclusion = "实况门控未触发：温度差和升温率仍按常规证据权重处理。"
    return {
        "active": active,
        "rule": f"current_temp < soft_forecast - {_fmt_c(gap_threshold_c, 1)} and warming_rate <= 0 unless sustained momentum confirms warming",
        "action": "block_high_temp_bucket_amplification" if active else "allow_normal_evidence_weighting",
        "gap_threshold_c": _round(gap_threshold_c, 2),
        "sustained_momentum_rate_threshold_c_per_hour": _round(sustained_momentum_rate_threshold, 2),
        "current_temp_c": _round(observed_current, 2),
        "current_temp_source": observed_source,
        "observed_current_temp_c": _round(observed_current, 2),
        "observed_today_max_c": _round(observed_max, 2),
        "observed_today_max_source": observed_max_source,
        "observed_today_max_na_reason": observed_max_na_reason,
        "soft_forecast_peak_c": _round(soft_forecast, 2),
        "soft_forecast_source": soft_source,
        "forecast_gap_c": _round(gap, 2),
        "warming_rate_c_per_hour": _round(rise, 2),
        "warming_rate_source": str(rise_info.get("source") or ""),
        "warming_rate_window": str(rise_info.get("window") or ""),
        "warming_rate_trusted": bool(rise_info.get("trusted")),
        "warming_rate_na_reason": str(rise_info.get("na_reason") or ""),
        "temperature_momentum_effective_c_per_hour": _round(momentum_rate, 2),
        "temperature_momentum_realization_state": momentum_state or None,
        "suppressed_by_sustained_momentum": bool(momentum_warming),
        "conclusion": conclusion,
    }


def _payload_evidence_dt(payload: Mapping[str, Any]) -> Optional[datetime]:
    return _parse_datetime(payload.get("weather_likelihood_refreshed_at")) or _parse_datetime(payload.get("_created_at"))


def _local_hour_fraction(dt: datetime) -> float:
    local_dt = dt.astimezone(CHENGDU_TZ)
    return local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0 + local_dt.microsecond / 3_600_000_000.0


def _fmt_local_seconds(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(CHENGDU_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_utc_seconds(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _format_interval_label_sec(interval_sec: int) -> str:
    if interval_sec < 60:
        return f"{interval_sec}s"
    if interval_sec % 60 == 0:
        return f"{interval_sec // 60}min"
    return f"{interval_sec}s"


def _temperature_label_unit(label: Any, default_unit: str = "C") -> str:
    text = str(label or "").upper()
    if "°F" in text or re.search(r"\bF\b", text):
        return "F"
    if "°C" in text or re.search(r"\bC\b", text):
        return "C"
    return str(default_unit or "C").upper()


def _settlement_adjusted_value(value: float, label: Any, rounding_rule: str, market_unit: str = "C") -> float:
    unit = _temperature_label_unit(label, market_unit)
    rule = str(rounding_rule or "").strip().lower()
    if rule == "whole_degrees" and unit in {"C", "F"}:
        # WU/TWC settlement pages expose whole-degree display values; decimal
        # current/history readings are not a floor rule. 27.78C has already
        # exceeded a <=27C boundary and displays/settles into the 28C bucket
        # unless a final settlement source later says otherwise.
        return float(math.floor(float(value) + 0.5))
    return float(value)


def _bucket_label_for_observed_value(
    rows: Sequence[Mapping[str, Any]],
    value: Any,
    rounding_rule: str,
    market_unit: str = "C",
) -> str:
    actual = _to_float(value)
    if actual is None:
        return ""
    for row in rows or []:
        label = str(row.get("outcome") or row.get("label") or "").strip()
        if not label:
            continue
        text = label.lower()
        adjusted = _settlement_adjusted_value(actual, label, rounding_rule, market_unit)
        nums = [float(match) for match in re.findall(r"-?\d+(?:\.\d+)?", label)]
        if not nums:
            continue
        if "or below" in text or text.startswith("<=") or "below" in text:
            if adjusted <= nums[0]:
                return label
            continue
        if "or higher" in text or text.startswith(">=") or "higher" in text or "above" in text:
            if adjusted >= nums[0]:
                return label
            continue
        if len(nums) >= 2 and ("-" in label or " to " in text):
            low = min(nums[0], nums[1])
            high = max(nums[0], nums[1])
            if low <= adjusted <= high:
                return label
            continue
        if abs(adjusted - nums[0]) <= 1e-9:
            return label
    return ""


def _raw_decimal_floor_bucket_label(
    rows: Sequence[Mapping[str, Any]],
    value: Any,
    market_unit: str = "C",
) -> str:
    actual = _to_float(value)
    if actual is None:
        return ""
    return _bucket_label_for_observed_value(rows, math.floor(float(actual)), "", market_unit)


def _display_integer_bucket_label(
    rows: Sequence[Mapping[str, Any]],
    value: Any,
    market_unit: str = "C",
) -> str:
    actual = _to_float(value)
    if actual is None:
        return ""
    # WU/TWC settlement pages show whole-degree Celsius values. API decimals
    # converted from Fahrenheit are diagnostic, not a floor-to-bucket rule.
    display_value = float(math.floor(float(actual) + 0.5))
    return _bucket_label_for_observed_value(rows, display_value, "", market_unit)


def _nearest_single_bucket_label(
    rows: Sequence[Mapping[str, Any]],
    value: Any,
) -> str:
    actual = _to_float(value)
    if actual is None:
        return ""
    candidates: List[Tuple[float, str]] = []
    for row in rows or []:
        label = str(row.get("outcome") or row.get("label") or "").strip()
        if not label or _is_cumulative_bucket(label):
            continue
        temp = _bucket_temp(label)
        if temp is None:
            continue
        candidates.append((abs(float(actual) - float(temp)), label))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def _rounding_rule_label(rounding_rule: str) -> str:
    rule = str(rounding_rule or "").strip().lower()
    if rule == "whole_degrees":
        return "whole_degrees：最终按 WU history 页面整数摄氏展示落桶；实时 API 小数只作物理轨迹/诊断，不向下取整当结算"
    if rule == "rounded_degrees":
        return "rounded_degrees：按最近整数度落桶"
    return "未识别落桶规则：禁止把小数显示默认当四舍五入"


def _metar_temperature_token(raw_text: Any) -> Dict[str, Any]:
    text = str(raw_text or "")
    match = re.search(r"\b(M?\d{2})/(M?\d{2})\b", text)
    if not match:
        return {}

    def parse_metar_temp(token: str) -> Optional[float]:
        if not token:
            return None
        if token.startswith("M"):
            return -float(token[1:])
        return float(token)

    temp = parse_metar_temp(match.group(1))
    dewpoint = parse_metar_temp(match.group(2))
    return {
        "raw_text": text,
        "temperature_token": match.group(0),
        "temperature_raw_value": temp,
        "dewpoint_raw_value": dewpoint,
        "raw_unit": "C",
        "raw_unit_note": "METAR temperature/dewpoint group is reported in whole degrees Celsius",
    }


def _metar_pressure_hpa_token(raw_text: Any) -> Optional[float]:
    text = str(raw_text or "").strip().upper()
    if not text:
        return None
    q_match = re.search(r"\bQ(?P<hpa>\d{4})\b", text)
    if q_match:
        return float(int(q_match.group("hpa")))
    a_match = re.search(r"\bA(?P<inhg>\d{4})\b", text)
    if a_match:
        return float(int(a_match.group("inhg"))) / 100.0 * 33.8638866667
    return None


def _metar_weather_profile(raw_text: Any) -> Dict[str, Any]:
    text = str(raw_text or "").strip().upper()
    if not text:
        return {
            "available": False,
            "source": "metar_raw",
            "weather_tokens": [],
            "cloud_groups": [],
        }
    tokens = re.split(r"\s+", text)
    weather_tokens: List[str] = []
    cloud_groups: List[Dict[str, Any]] = []
    cover_map = {"FEW": 20.0, "SCT": 45.0, "BKN": 75.0, "OVC": 100.0}
    effective_cloud: Optional[float] = None
    low_cloud: Optional[float] = None
    mid_cloud: Optional[float] = None
    high_cloud: Optional[float] = None
    for token in tokens:
        clean = token.strip().upper()
        if not clean:
            continue
        wx_core = clean.lstrip("+-")
        if re.fullmatch(r"(VC)?(TS|SH|FZ)?(RA|DZ|SN|PL|GR|GS|UP)|VCSH|VCTS", wx_core):
            weather_tokens.append(clean)
        cloud_match = re.fullmatch(r"(FEW|SCT|BKN|OVC)(\d{3}|///)(CB|TCU)?", clean)
        if not cloud_match:
            continue
        amount, height_token, convective = cloud_match.groups()
        cover = cover_map.get(amount, 0.0)
        height_ft = None
        if height_token != "///":
            try:
                height_ft = int(height_token) * 100
            except ValueError:
                height_ft = None
        cloud_groups.append(
            {
                "amount": amount,
                "height_ft": height_ft,
                "cover_pct": cover,
                "convective": convective or "",
                "raw": clean,
            }
        )
        effective_cloud = max(effective_cloud or 0.0, cover)
        if height_ft is None:
            continue
        if height_ft <= 3000:
            low_cloud = max(low_cloud or 0.0, cover)
        elif height_ft <= 10000:
            mid_cloud = max(mid_cloud or 0.0, cover)
        else:
            high_cloud = max(high_cloud or 0.0, cover)
    cavok = "CAVOK" in tokens
    precip_tokens = [
        token for token in weather_tokens
        if any(code in token.lstrip("+-") for code in ("RA", "DZ", "SN", "PL", "GR", "GS", "UP", "SH"))
    ]
    convective_tokens = [
        token for token in weather_tokens
        if "TS" in token.lstrip("+-") or "SH" in token.lstrip("+-")
    ]
    present_weather_conflict = bool(cavok and precip_tokens)
    # In METAR, absent present-weather precipitation in an otherwise valid
    # observation means there is no reportable precipitation at observation time.
    no_precip = bool(not present_weather_conflict and not precip_tokens)
    return {
        "available": True,
        "source": "metar_raw",
        "raw": text,
        "cavok": cavok,
        "weather_tokens": weather_tokens,
        "precip_tokens": precip_tokens,
        "convective_tokens": convective_tokens,
        "precip_observed": bool(precip_tokens and not present_weather_conflict),
        "showery_or_convective": bool(convective_tokens and not present_weather_conflict),
        "no_precip_confirmed": no_precip,
        "present_weather_conflict": present_weather_conflict,
        "present_weather_conflict_reason": (
            "cavok_with_precipitation_present_weather"
            if present_weather_conflict
            else ""
        ),
        "cloud_groups": cloud_groups,
        "effective_cloud_cover_pct": _round(effective_cloud, 1),
        "low_cloud_cover_pct": _round(low_cloud, 1),
        "mid_cloud_cover_pct": _round(mid_cloud, 1),
        "high_cloud_cover_pct": _round(high_cloud, 1),
    }


def _merged_metar_weather_profile(features: Mapping[str, Any]) -> Dict[str, Any]:
    profiles: List[Dict[str, Any]] = []
    for key in ("zuuu_metar_raw_awc_latest_raw", "zuuu_metar_raw_awc_today_max_raw"):
        profile = _metar_weather_profile(features.get(key))
        if profile.get("available"):
            profile["source_field"] = key
            profiles.append(profile)
    if not profiles:
        return {"available": False, "source": "metar_raw", "weather_tokens": [], "cloud_groups": []}
    latest_present_weather_profiles = [
        profile for profile in profiles
        if profile.get("source_field") == "zuuu_metar_raw_awc_latest_raw"
    ]
    present_weather_profiles = latest_present_weather_profiles or profiles
    merged: Dict[str, Any] = {
        "available": True,
        "source": "metar_raw",
        "profiles": profiles[:3],
        "present_weather_source_fields": [
            str(profile.get("source_field") or "") for profile in present_weather_profiles
        ],
        "weather_tokens": [],
        "precip_tokens": [],
        "convective_tokens": [],
        "cloud_groups": [],
        "cavok": False,
        "precip_observed": False,
        "showery_or_convective": False,
        "no_precip_confirmed": False,
        "present_weather_conflict": False,
        "present_weather_conflict_reason": "",
    }
    for profile in present_weather_profiles:
        for key in ("weather_tokens", "precip_tokens", "convective_tokens"):
            value = profile.get(key)
            if isinstance(value, list):
                merged[key].extend(value)
        for key in ("cavok", "precip_observed", "showery_or_convective", "no_precip_confirmed", "present_weather_conflict"):
            merged[key] = bool(merged.get(key) or profile.get(key))
        if profile.get("present_weather_conflict_reason") and not merged.get("present_weather_conflict_reason"):
            merged["present_weather_conflict_reason"] = str(profile.get("present_weather_conflict_reason") or "")
    if merged.get("present_weather_conflict"):
        merged["precip_observed"] = False
        merged["showery_or_convective"] = False
        merged["no_precip_confirmed"] = False
    elif merged.get("precip_observed"):
        merged["no_precip_confirmed"] = False
    for profile in profiles:
        value = profile.get("cloud_groups")
        if isinstance(value, list):
            merged["cloud_groups"].extend(value)
    for out_key, profile_key in (
        ("effective_cloud_cover_pct", "effective_cloud_cover_pct"),
        ("low_cloud_cover_pct", "low_cloud_cover_pct"),
        ("mid_cloud_cover_pct", "mid_cloud_cover_pct"),
        ("high_cloud_cover_pct", "high_cloud_cover_pct"),
    ):
        vals = [_to_float(profile.get(profile_key)) for profile in profiles]
        nums = [float(v) for v in vals if v is not None]
        merged[out_key] = _round(max(nums), 1) if nums else None
    return merged


def _first_float_feature(features: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _to_float(features.get(key))
        if value is not None:
            return value
    return None


def _angular_distance_deg(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    diff = abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)
    return float(diff)


def _temperature_momentum_flags(features: Mapping[str, Any]) -> Dict[str, Any]:
    momentum_rate = _first_float_feature(
        features,
        "temperature_momentum_effective_c_per_hour",
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_rise_rate_c_per_hour",
        "zuuu_metar_raw_awc_temperature_momentum_1h_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
    )
    momentum_state = str(features.get("temperature_momentum_realization_state") or "").strip()
    momentum_up = bool(
        (momentum_rate is not None and float(momentum_rate) >= 0.25)
        or momentum_state in {"weak_warming", "emerging_warming", "sustained_warming"}
        or _truthy(features.get("temperature_momentum_sustained_warming"))
    )
    momentum_down = bool(
        (momentum_rate is not None and float(momentum_rate) <= -0.20)
        or momentum_state in {"cooling", "not_realized"}
    )
    return {
        "rate_c_per_hour": _round(momentum_rate, 2),
        "state": momentum_state,
        "up": momentum_up,
        "down": momentum_down,
    }


def _dewpoint_moisture_profile(
    features: Mapping[str, Any],
    *,
    cloud: Optional[float],
    low_cloud: Optional[float],
    no_precip: bool,
    precip_present: bool,
) -> Dict[str, Any]:
    raw_token = _metar_temperature_token(features.get("zuuu_metar_raw_awc_latest_raw"))
    observed_temp = _first_float_feature(
        features,
        "observed_temp_c",
        "observed_temp",
        "metar_temperature_c",
        "zuuu_metar_raw_awc_latest_temp_c",
    )
    if observed_temp is None:
        observed_temp = _to_float(raw_token.get("temperature_raw_value"))
    dewpoint = _first_float_feature(
        features,
        "dewpoint_c",
        "dew_point_c",
        "zuuu_metar_raw_awc_latest_dewpoint_c",
    )
    if dewpoint is None:
        dewpoint = _to_float(raw_token.get("dewpoint_raw_value"))
    previous_dewpoint = _first_float_feature(
        features,
        "awc_previous_dewpoint_c",
        "zuuu_metar_raw_awc_previous_dewpoint_c",
    )
    dewpoint_trend = _first_float_feature(
        features,
        "dewpoint_trend_c",
        "zuuu_metar_raw_awc_dewpoint_trend_c",
    )
    if dewpoint_trend is None and dewpoint is not None and previous_dewpoint is not None:
        dewpoint_trend = float(dewpoint) - float(previous_dewpoint)
    spread = None
    if observed_temp is not None and dewpoint is not None:
        spread = float(observed_temp) - float(dewpoint)
    momentum = _temperature_momentum_flags(features)
    high_dewpoint = bool(dewpoint is not None and float(dewpoint) >= 23.0)
    very_high_dewpoint = bool(dewpoint is not None and float(dewpoint) >= 24.5)
    low_dewpoint = bool(dewpoint is not None and float(dewpoint) <= 18.0)
    small_spread = bool(spread is not None and float(spread) <= 6.0)
    large_spread = bool(spread is not None and float(spread) >= 10.0)
    drying = bool(dewpoint_trend is not None and float(dewpoint_trend) <= -0.8)
    moistening = bool(dewpoint_trend is not None and float(dewpoint_trend) >= 0.8)
    dry_clear_context = bool(
        no_precip
        and not precip_present
        and (low_cloud is None or float(low_cloud) <= 25.0)
        and (cloud is None or float(cloud) <= 60.0)
    )
    wet_or_cloudy_context = bool(
        precip_present
        or (low_cloud is not None and float(low_cloud) >= 50.0)
        or (cloud is not None and float(cloud) >= 75.0)
    )
    if dewpoint is None:
        state = "missing"
        conclusion = "露点缺失"
    elif very_high_dewpoint or small_spread or (high_dewpoint and (wet_or_cloudy_context or moistening)):
        state = "moisture_constraint"
        conclusion = "露点高或温度露点差小，四川盆地湿球/低层高湿约束增强，压制最高温继续上探。"
    elif (low_dewpoint or drying or large_spread) and dry_clear_context and not momentum.get("down"):
        state = "dry_mixing_support"
        conclusion = "露点低或转干，叠加少云无雨与动量不弱，低层干化有利于午后边界层升温。"
    else:
        state = "neutral_context"
        conclusion = "露点处于中性或缺少云雨/动量配合，只作为湿球背景，不单独调桶。"
    return {
        "available": bool(state != "missing"),
        "source": "awc_metar_dewpoint_and_runtime_features",
        "role": "wet_bulb_boundary_layer_moisture_auxiliary",
        "override_allowed": False,
        "state": state,
        "conclusion": conclusion,
        "dewpoint_c": _round(dewpoint, 1),
        "observed_temp_c": _round(observed_temp, 1),
        "temp_dewpoint_spread_c": _round(spread, 1),
        "previous_dewpoint_c": _round(previous_dewpoint, 1),
        "dewpoint_trend_c": _round(dewpoint_trend, 2),
        "high_dewpoint": high_dewpoint,
        "low_dewpoint": low_dewpoint,
        "small_temp_dewpoint_spread": small_spread,
        "large_temp_dewpoint_spread": large_spread,
        "drying": drying,
        "moistening": moistening,
        "dry_clear_context": dry_clear_context,
        "wet_or_cloudy_context": wet_or_cloudy_context,
        "momentum_up": bool(momentum.get("up")),
        "momentum_down": bool(momentum.get("down")),
        "momentum_rate_c_per_hour": momentum.get("rate_c_per_hour"),
    }


def _pressure_profile(
    features: Mapping[str, Any],
    metar_profile: Mapping[str, Any],
    *,
    cloud: Optional[float],
    low_cloud: Optional[float],
    dewpoint_profile: Mapping[str, Any],
    no_precip: bool,
    precip_present: bool,
) -> Dict[str, Any]:
    raw_pressure = _metar_pressure_hpa_token(features.get("zuuu_metar_raw_awc_latest_raw"))
    pressure = _first_float_feature(
        features,
        "pressure_hpa",
        "awc_pressure_hpa",
        "zuuu_metar_raw_awc_latest_pressure_hpa",
    )
    if pressure is None:
        pressure = raw_pressure
    previous_pressure = _first_float_feature(
        features,
        "awc_previous_pressure_hpa",
        "zuuu_metar_raw_awc_previous_pressure_hpa",
    )
    trend = _first_float_feature(
        features,
        "pressure_trend_hpa_recent",
        "awc_pressure_trend_hpa",
        "zuuu_metar_raw_awc_pressure_trend_hpa",
        "pressure_trend_hpa_3h",
    )
    if trend is None and pressure is not None and previous_pressure is not None:
        trend = float(pressure) - float(previous_pressure)
    momentum = _temperature_momentum_flags(features)
    convective_cloud = any(
        str(group.get("convective") or "").strip().upper() in {"CB", "TCU"}
        for group in (metar_profile.get("cloud_groups") if isinstance(metar_profile.get("cloud_groups"), list) else [])
        if isinstance(group, Mapping)
    )
    convective_or_showery = bool(metar_profile.get("showery_or_convective") or convective_cloud)
    high_pressure = bool(pressure is not None and float(pressure) >= 1012.0)
    low_pressure = bool(pressure is not None and float(pressure) <= 1006.0)
    rising_or_steady = bool(trend is None or float(trend) >= -0.5)
    falling = bool(trend is not None and float(trend) <= -1.0)
    clear_subsidence_context = bool(
        high_pressure
        and rising_or_steady
        and no_precip
        and not precip_present
        and (low_cloud is None or float(low_cloud) <= 25.0)
        and (cloud is None or float(cloud) <= 60.0)
        and momentum.get("up")
    )
    unsettled_context = bool(
        (low_pressure or falling)
        and (
            precip_present
            or convective_or_showery
            or (low_cloud is not None and float(low_cloud) >= 50.0)
            or str(dewpoint_profile.get("state") or "") == "moisture_constraint"
        )
    )
    if pressure is None:
        state = "missing"
        conclusion = "气压缺失"
    elif clear_subsidence_context:
        state = "subsidence_warming_support"
        conclusion = "高压稳定叠加少云无雨和升温动量，代表下沉/辐射背景支持最高温兑现。"
    elif unsettled_context:
        state = "unsettled_cooling_risk"
        conclusion = "低压或快速降压叠加云雨/对流/高湿，代表扰动边界增强，压制高温桶。"
    else:
        state = "neutral_context"
        conclusion = "气压仅作环流背景；缺少云雨、露点或动量配合时不单独调整目标桶。"
    return {
        "available": bool(state != "missing"),
        "source": "awc_metar_qnh_or_runtime_pressure",
        "role": "subsidence_unsettled_boundary_auxiliary",
        "override_allowed": False,
        "state": state,
        "conclusion": conclusion,
        "pressure_hpa": _round(pressure, 1),
        "previous_pressure_hpa": _round(previous_pressure, 1),
        "pressure_trend_hpa": _round(trend, 2),
        "high_pressure": high_pressure,
        "low_pressure": low_pressure,
        "falling_pressure": falling,
        "rising_or_steady_pressure": rising_or_steady,
        "clear_subsidence_context": clear_subsidence_context,
        "unsettled_context": unsettled_context,
        "convective_or_showery": convective_or_showery,
        "momentum_up": bool(momentum.get("up")),
        "momentum_down": bool(momentum.get("down")),
        "momentum_rate_c_per_hour": momentum.get("rate_c_per_hour"),
    }


def _awc_wind_profile(
    features: Mapping[str, Any],
    metar_profile: Mapping[str, Any],
    *,
    cloud: Optional[float],
    low_cloud: Optional[float],
    dewpoint: Optional[float],
    no_precip: bool,
    precip_present: bool,
) -> Dict[str, Any]:
    direction = _first_float_feature(features, "awc_wind_dir_deg", "zuuu_metar_raw_awc_latest_wind_dir_deg")
    speed = _first_float_feature(features, "awc_wind_speed_mps", "zuuu_metar_raw_awc_latest_wind_speed_mps")
    gust = _first_float_feature(features, "awc_wind_gust_mps", "zuuu_metar_raw_awc_latest_wind_gust_mps")
    variable_from = _first_float_feature(
        features,
        "awc_wind_variable_from_deg",
        "zuuu_metar_raw_awc_latest_wind_variable_from_deg",
    )
    variable_to = _first_float_feature(
        features,
        "awc_wind_variable_to_deg",
        "zuuu_metar_raw_awc_latest_wind_variable_to_deg",
    )
    variable = bool(
        _truthy(features.get("awc_wind_variable"))
        or _truthy(features.get("zuuu_metar_raw_awc_latest_wind_variable"))
        or (variable_from is not None and variable_to is not None)
        or str(features.get("awc_wind_dir_text") or features.get("zuuu_metar_raw_awc_latest_wind_dir_text") or "").strip().upper() == "VRB"
    )
    previous_direction = _first_float_feature(
        features,
        "awc_previous_wind_dir_deg",
        "zuuu_metar_raw_awc_previous_wind_dir_deg",
    )
    previous_speed = _first_float_feature(
        features,
        "awc_previous_wind_speed_mps",
        "zuuu_metar_raw_awc_previous_wind_speed_mps",
    )
    previous_variable = bool(
        _truthy(features.get("awc_previous_wind_variable"))
        or _truthy(features.get("zuuu_metar_raw_awc_previous_wind_variable"))
        or str(
            features.get("awc_previous_wind_dir_text")
            or features.get("zuuu_metar_raw_awc_previous_wind_dir_text")
            or ""
        ).strip().upper() == "VRB"
    )
    direction_shift = _angular_distance_deg(previous_direction, direction)
    variable_span = _angular_distance_deg(variable_from, variable_to)
    cloud_groups = metar_profile.get("cloud_groups") if isinstance(metar_profile.get("cloud_groups"), list) else []
    convective_cloud = any(
        str(group.get("convective") or "").strip().upper() in {"CB", "TCU"}
        for group in cloud_groups
        if isinstance(group, Mapping)
    )
    convective_boundary = bool(metar_profile.get("showery_or_convective") or convective_cloud)
    momentum_rate = _first_float_feature(
        features,
        "temperature_momentum_effective_c_per_hour",
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_rise_rate_c_per_hour",
        "zuuu_metar_raw_awc_temperature_momentum_1h_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
    )
    momentum_state = str(features.get("temperature_momentum_realization_state") or "").strip()
    momentum_up = bool(
        (momentum_rate is not None and float(momentum_rate) >= 0.25)
        or momentum_state in {"weak_warming", "emerging_warming", "sustained_warming"}
        or _truthy(features.get("temperature_momentum_sustained_warming"))
    )
    momentum_down = bool(
        (momentum_rate is not None and float(momentum_rate) <= -0.20)
        or momentum_state in {"cooling", "not_realized"}
    )
    stable_direction = bool(direction is not None and not variable)
    weak_previous = bool(previous_variable or (previous_speed is not None and float(previous_speed) <= 2.5))
    stable_from_weak_variable = bool(stable_direction and weak_previous)
    dry_clear_context = bool(
        no_precip
        and not precip_present
        and (dewpoint is None or float(dewpoint) <= 20.5)
        and (low_cloud is None or float(low_cloud) <= 20.0)
        and (cloud is None or float(cloud) <= 55.0)
    )
    wet_convective_context = bool(
        precip_present
        or convective_boundary
        or (dewpoint is not None and float(dewpoint) >= 23.0)
        or (low_cloud is not None and float(low_cloud) >= 55.0)
    )
    wind_boundary_signal = bool(
        variable
        or (variable_span is not None and float(variable_span) >= 45.0)
        or (direction_shift is not None and float(direction_shift) >= 45.0)
        or (speed is not None and float(speed) >= 6.0)
        or (gust is not None and float(gust) >= 8.0)
    )
    if direction is None and speed is None and not variable:
        state = "missing"
        conclusion = "AWC 风组缺失"
    elif stable_direction and speed is not None and 1.5 <= float(speed) <= 6.5 and dry_clear_context and momentum_up:
        state = "warming_assist"
        conclusion = "稳定风叠加无雨、少云、较低露点和升温动量，只作为升温辅助。"
    elif wind_boundary_signal and wet_convective_context and (momentum_down or convective_boundary or precip_present):
        state = "cooling_assist"
        conclusion = "风场变化叠加阵雨/对流云/高湿或动量转弱，只作为高温受压辅助。"
    else:
        state = "neutral_context"
        conclusion = "风向/风速仅作通风、平流、对流边界背景，不单独调整目标桶。"
    return {
        "available": bool(state != "missing"),
        "source": "zuuu_metar_raw_awc_wind",
        "role": "ventilation_advection_convective_boundary_auxiliary",
        "override_allowed": False,
        "state": state,
        "conclusion": conclusion,
        "wind_dir_deg": _round(direction, 1),
        "wind_speed_mps": _round(speed, 2),
        "wind_gust_mps": _round(gust, 2),
        "wind_variable": variable,
        "wind_variable_from_deg": _round(variable_from, 1),
        "wind_variable_to_deg": _round(variable_to, 1),
        "wind_variable_span_deg": _round(variable_span, 1),
        "previous_wind_dir_deg": _round(previous_direction, 1),
        "previous_wind_speed_mps": _round(previous_speed, 2),
        "previous_wind_variable": previous_variable,
        "direction_shift_deg": _round(direction_shift, 1),
        "stable_direction": stable_direction,
        "stable_from_weak_variable": stable_from_weak_variable,
        "convective_boundary": convective_boundary,
        "dry_clear_context": dry_clear_context,
        "wet_convective_context": wet_convective_context,
        "momentum_up": momentum_up,
        "momentum_down": momentum_down,
        "momentum_rate_c_per_hour": _round(momentum_rate, 2),
    }


def _runtime_precip_profile(features: Mapping[str, Any], metar_profile: Mapping[str, Any]) -> Dict[str, Any]:
    augmented = dict(features or {})
    metar_conflict = bool(metar_profile.get("present_weather_conflict"))
    feature_conflict = bool(
        (
            _to_bool(augmented.get("metar_rain_observed"))
            or _to_bool(augmented.get("is_raining_observed"))
        )
        and (
            _to_bool(augmented.get("awc_metar_no_precip_observed"))
            or _to_bool(augmented.get("metar_no_precip_observed"))
            or _to_bool(augmented.get("metar_cavok_observed"))
        )
    )
    if metar_conflict or feature_conflict:
        augmented["metar_present_weather_conflict"] = True
        augmented["metar_present_weather_conflict_reason"] = (
            str(metar_profile.get("present_weather_conflict_reason") or "")
            or "metar_precip_and_no_precip_flags_conflict"
        )
        for key in (
            "metar_rain_observed",
            "is_raining_observed",
            "metar_no_precip_observed",
            "awc_metar_no_precip_observed",
            "metar_cavok_observed",
            "observed_precip_rate_mm_per_h",
            "precipitation_rate_mm_per_h",
        ):
            augmented.pop(key, None)
        augmented["precipitation_source"] = "metar_present_weather_conflict"
    elif metar_profile.get("precip_observed"):
        augmented.setdefault("metar_rain_observed", True)
        augmented.setdefault("observed_precip_rate_mm_per_h", 0.1)
        augmented.setdefault("precipitation_source", "zuuu_metar_raw_awc_present_weather")
    elif metar_profile.get("no_precip_confirmed"):
        augmented["metar_no_precip_observed"] = True
        augmented["awc_metar_no_precip_observed"] = True
        augmented["is_raining_observed"] = False
        augmented["observed_precip_rate_mm_per_h"] = 0.0
        augmented["precipitation_rate_mm_per_h"] = 0.0
        augmented["precipitation_source"] = "zuuu_metar_raw_awc_present_weather"
    if _runtime_precipitation_evidence_profile is not None:
        try:
            profile = _runtime_precipitation_evidence_profile(augmented)
            if isinstance(profile, Mapping):
                out = dict(profile)
                if metar_conflict or feature_conflict:
                    out.update(
                        {
                            "available_strong": False,
                            "available_proxy": False,
                            "evidence_state": "conflict",
                            "source_class": "metar_conflict",
                            "source_field": "metar_present_weather_profile",
                            "source": "metar_present_weather_conflict",
                            "rate_mm_per_h": None,
                            "precip_present": False,
                            "no_precip_confirmed": False,
                            "na_reason": str(augmented.get("metar_present_weather_conflict_reason") or "metar_present_weather_conflict"),
                            "is_real_radar_or_metar": False,
                            "is_proxy_only": False,
                        }
                    )
                return out
        except Exception:
            pass
    precip_present = bool(
        _to_bool(augmented.get("metar_rain_observed"))
        or _to_bool(augmented.get("is_raining_observed"))
        or metar_profile.get("precip_observed")
    )
    no_precip = bool(
        _to_bool(augmented.get("awc_metar_no_precip_observed"))
        or _to_bool(augmented.get("metar_no_precip_observed"))
        or _to_bool(augmented.get("metar_cavok_observed"))
        or metar_profile.get("no_precip_confirmed")
    )
    observed_rate = _first_float_feature(augmented, "observed_precip_rate_mm_per_h", "rain_rate_mm_per_h")
    if no_precip:
        observed_rate = 0.0
    return {
        "available_strong": bool(precip_present or no_precip),
        "available_proxy": observed_rate is not None and not bool(precip_present or no_precip),
        "evidence_state": "strong_precip" if precip_present else ("strong_no_precip" if no_precip else ("proxy" if observed_rate is not None else "missing")),
        "source_class": "metar" if bool(precip_present or no_precip) else ("weak_proxy" if observed_rate is not None else "missing"),
        "source_field": "metar_raw_present_weather" if bool(precip_present or no_precip) else "",
        "source": "zuuu_metar_raw_awc_present_weather" if bool(precip_present or no_precip) else "",
        "rate_mm_per_h": observed_rate if observed_rate is not None else (0.1 if precip_present else (0.0 if no_precip else None)),
        "precip_present": precip_present,
        "no_precip_confirmed": no_precip,
        "na_reason": "" if bool(precip_present or no_precip) else "real_radar_qpe_metar_or_verified_precip_missing",
        "is_real_radar_or_metar": bool(precip_present or no_precip),
        "is_proxy_only": bool(observed_rate is not None and not bool(precip_present or no_precip)),
    }


def _physical_weather_modifier(
    features: Mapping[str, Any],
    groups: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Summarize non-temperature weather evidence for trajectory and target-bucket Bayes.

    This is not a second settlement source. It only adjusts the intraday peak
    path when sky/precip evidence says continued warming is easier or harder.
    """
    features_map = features if isinstance(features, Mapping) else {}
    metar_profile = _merged_metar_weather_profile(features_map)
    precip_profile = _runtime_precip_profile(features_map, metar_profile)
    groups_map = groups if isinstance(groups, Mapping) else {}
    group = groups_map.get("precipitation_evaporative_cooling") if isinstance(groups_map, Mapping) else None
    if isinstance(group, Mapping) and str(precip_profile.get("evidence_state") or "") == "missing":
        profile = group.get("source_profile")
        if isinstance(profile, Mapping):
            precip_profile.update({k: v for k, v in profile.items() if v not in (None, "")})
        if group.get("evidence_state"):
            precip_profile["evidence_state"] = group.get("evidence_state")

    cloud = _first_float_feature(features_map, "metar_effective_cloud_cover_pct", "cloud_cover_pct", "open_meteo_cloud_cover_pct")
    metar_cloud = _to_float(metar_profile.get("effective_cloud_cover_pct"))
    if cloud is None and metar_cloud is not None:
        cloud = metar_cloud
    low_cloud = _first_float_feature(features_map, "cloud_cover_low_pct")
    metar_low_cloud = _to_float(metar_profile.get("low_cloud_cover_pct"))
    if low_cloud is None and metar_low_cloud is not None:
        low_cloud = metar_low_cloud
    mid_cloud = _first_float_feature(features_map, "cloud_cover_mid_pct")
    if mid_cloud is None:
        mid_cloud = _to_float(metar_profile.get("mid_cloud_cover_pct"))
    high_cloud = _first_float_feature(features_map, "cloud_cover_high_pct")
    if high_cloud is None:
        high_cloud = _to_float(metar_profile.get("high_cloud_cover_pct"))
    solar = _first_float_feature(features_map, "solar_radiation", "shortwave_radiation")
    dew = _first_float_feature(features_map, "dewpoint_c", "dew_point_c", "zuuu_metar_raw_awc_latest_dewpoint_c")
    if dew is None:
        dew = _to_float(_metar_temperature_token(features_map.get("zuuu_metar_raw_awc_latest_raw")).get("dewpoint_raw_value"))
    nowcast = _precip_nowcast_risk(features_map)

    components: List[Dict[str, Any]] = []

    def add_component(key: str, state: str, delta_c: float, evidence: float, reason: str, metrics: Optional[Dict[str, Any]] = None) -> None:
        components.append(
            {
                "key": key,
                "state": state,
                "peak_mu_delta_c": _round(delta_c, 3),
                "evidence": _round(max(0.0, min(1.0, evidence)), 3),
                "reason": reason,
                "metrics": metrics or {},
            }
        )

    precip_state = str(precip_profile.get("evidence_state") or "").strip()
    precip_present = bool(precip_profile.get("precip_present"))
    no_precip = bool(precip_profile.get("no_precip_confirmed"))
    precip_rate = _to_float(precip_profile.get("rate_mm_per_h"))
    dewpoint_profile = _dewpoint_moisture_profile(
        features_map,
        cloud=cloud,
        low_cloud=low_cloud,
        no_precip=no_precip,
        precip_present=precip_present,
    )
    profile_dew = _to_float(dewpoint_profile.get("dewpoint_c"))
    if dew is None and profile_dew is not None:
        dew = profile_dew
    wind_profile = _awc_wind_profile(
        features_map,
        metar_profile,
        cloud=cloud,
        low_cloud=low_cloud,
        dewpoint=dew,
        no_precip=no_precip,
        precip_present=precip_present,
    )
    pressure_profile = _pressure_profile(
        features_map,
        metar_profile,
        cloud=cloud,
        low_cloud=low_cloud,
        dewpoint_profile=dewpoint_profile,
        no_precip=no_precip,
        precip_present=precip_present,
    )
    if precip_present or precip_state in {"strong_precip", "precip", "strong_rain"}:
        delta = -0.95 if bool(precip_profile.get("available_strong")) else -0.35
        add_component(
            "observed_precipitation",
            "cooling",
            delta,
            0.95 if bool(precip_profile.get("available_strong")) else 0.45,
            "AWC/METAR 或可验证实况出现降水，后段升温兑现受压。",
            {
                "source": precip_profile.get("source"),
                "source_class": precip_profile.get("source_class"),
                "source_field": precip_profile.get("source_field"),
                "rate_mm_per_h": _round(precip_rate, 2),
            },
        )
    elif no_precip:
        add_component(
            "observed_no_precipitation",
            "warming",
            0.10,
            0.55,
            "AWC/METAR 确认无降水，移除降水冷却压制。",
            {
                "source": precip_profile.get("source"),
                "source_class": precip_profile.get("source_class"),
                "source_field": precip_profile.get("source_field"),
            },
        )
    elif precip_rate is not None and precip_rate > 0:
        add_component(
            "proxy_precipitation",
            "cooling",
            -0.20,
            0.25,
            "仅有代理降水信号，作为弱冷却风险，不当硬实况。",
            {"rate_mm_per_h": _round(precip_rate, 2)},
        )

    if str(nowcast.get("state") or "") == "cooling":
        add_component(
            "precip_nowcast_1_3h",
            "cooling",
            -0.35,
            0.45,
            "未来1-3小时降水 nowcast 升高，压制后续上探。",
            {
                "source": nowcast.get("source"),
                "checked_at": nowcast.get("checked_at"),
                "reference_time": nowcast.get("reference_time"),
                "nowcast_reference_time": nowcast.get("nowcast_reference_time"),
                "next_precip_at": nowcast.get("next_precip_at"),
                "risk_state": nowcast.get("risk_state"),
                "max_rate_mm_per_h": _round(nowcast.get("max_rate_mm_per_h"), 2),
                "max_probability_pct": _round(nowcast.get("max_probability_pct"), 1),
            },
        )
    elif str(nowcast.get("risk_state") or "") == "watch":
        add_component(
            "precip_nowcast_1_3h",
            "neutral",
            -0.10,
            0.20,
            "未来1-3小时有降水观察信号，只轻微提高升温阻力。",
            {
                "source": nowcast.get("source"),
                "checked_at": nowcast.get("checked_at"),
                "reference_time": nowcast.get("reference_time"),
                "nowcast_reference_time": nowcast.get("nowcast_reference_time"),
                "next_precip_at": nowcast.get("next_precip_at"),
                "risk_state": nowcast.get("risk_state"),
                "max_probability_pct": _round(nowcast.get("max_probability_pct"), 1),
            },
        )

    if low_cloud is not None and float(low_cloud) >= 60.0:
        add_component("low_cloud", "cooling", -0.45, 0.65, "低云偏多，短波加热受压。", {"low_cloud_pct": _round(low_cloud, 1)})
    elif cloud is not None and float(cloud) >= 80.0:
        add_component("cloud_cover", "cooling", -0.35, 0.55, "总云量偏高，升温空间下调。", {"cloud_cover_pct": _round(cloud, 1)})
    elif cloud is not None and float(cloud) <= 30.0 and (low_cloud is None or float(low_cloud) <= 15.0):
        add_component(
            "clear_low_cloud",
            "warming",
            0.15,
            0.35,
            "低云少/云量低，支持辐射升温，但仍需温度动量兑现。",
            {"cloud_cover_pct": _round(cloud, 1), "low_cloud_pct": _round(low_cloud, 1)},
        )

    if dewpoint_profile.get("state") == "moisture_constraint":
        add_component(
            "dewpoint_moisture_constraint",
            "cooling",
            -0.24,
            0.42,
            "露点高/温度露点差小，四川盆地低层高湿与湿球约束增强；只作为压高温桶辅助似然。",
            {
                **dewpoint_profile,
                "source_class": "dewpoint_auxiliary",
                "override_allowed": False,
            },
        )
    elif dewpoint_profile.get("state") == "dry_mixing_support":
        add_component(
            "dewpoint_dry_mixing_support",
            "warming",
            0.07,
            0.24,
            "露点低或转干，且少云无雨、动量不弱；只作为边界层干化升温辅助似然。",
            {
                **dewpoint_profile,
                "source_class": "dewpoint_auxiliary",
                "override_allowed": False,
            },
        )

    if solar is not None and cloud is not None:
        if float(solar) >= 550.0 and float(cloud) <= 45.0:
            add_component("solar_radiation", "warming", 0.15, 0.35, "太阳辐射较强且云量不高。", {"solar_radiation": _round(solar, 1)})
        elif float(solar) <= 150.0 and float(cloud) >= 60.0:
            add_component("solar_radiation", "cooling", -0.20, 0.30, "辐射偏弱且云量较高。", {"solar_radiation": _round(solar, 1)})

    if wind_profile.get("state") == "warming_assist":
        add_component(
            "wind_airmass_context",
            "warming",
            0.08,
            0.22,
            "AWC 风场稳定，且无雨、少云、露点不高、温度动量上行；只作为升温辅助，不覆盖温度轨迹。",
            {
                **wind_profile,
                "source_class": "metar_wind_auxiliary",
                "override_allowed": False,
            },
        )
    elif wind_profile.get("state") == "cooling_assist":
        add_component(
            "wind_airmass_context",
            "cooling",
            -0.12,
            0.30,
            "AWC 风场变化伴随阵雨/对流云/高湿或动量转弱；只作为高温受压辅助，不覆盖温度轨迹。",
            {
                **wind_profile,
                "source_class": "metar_wind_auxiliary",
                "override_allowed": False,
            },
        )

    if pressure_profile.get("state") == "subsidence_warming_support":
        add_component(
            "pressure_subsidence_context",
            "warming",
            0.06,
            0.18,
            "气压偏高且稳定，叠加少云无雨和升温动量；只作为四川盆地下沉增温辅助似然。",
            {
                **pressure_profile,
                "source_class": "pressure_auxiliary",
                "override_allowed": False,
            },
        )
    elif pressure_profile.get("state") == "unsettled_cooling_risk":
        add_component(
            "pressure_unsettled_context",
            "cooling",
            -0.12,
            0.28,
            "低压或快速降压伴随云雨/对流/高湿，扰动边界增强；只作为压高温桶辅助似然。",
            {
                **pressure_profile,
                "source_class": "pressure_auxiliary",
                "override_allowed": False,
            },
        )

    net_delta = sum(float(item.get("peak_mu_delta_c") or 0.0) for item in components)
    net_delta = max(-1.25, min(0.55, net_delta))
    cooling_evidence = sum(float(item.get("evidence") or 0.0) for item in components if item.get("state") == "cooling")
    warming_evidence = sum(float(item.get("evidence") or 0.0) for item in components if item.get("state") == "warming")
    if net_delta <= -0.25 or cooling_evidence - warming_evidence >= 0.45:
        state = "cooling"
        label = "云雨压制"
    elif net_delta >= 0.15 or warming_evidence - cooling_evidence >= 0.35:
        state = "warming"
        label = "辐射支持"
    elif components:
        state = "neutral"
        label = "中性"
    else:
        state = "missing"
        label = "缺失"
    path_scale = 1.0
    if state == "cooling":
        path_scale = max(0.65, 1.0 + net_delta * 0.22)
    elif state == "warming":
        path_scale = min(1.10, 1.0 + net_delta * 0.10)
    component_rows: List[Dict[str, Any]] = []
    for item in components:
        row = dict(item)
        component_delta = _to_float(row.get("peak_mu_delta_c"))
        component_scale = 1.0
        if component_delta is not None:
            if float(component_delta) < 0:
                component_scale = max(0.65, 1.0 + float(component_delta) * 0.22)
            elif float(component_delta) > 0:
                component_scale = min(1.10, 1.0 + float(component_delta) * 0.10)
        row["path_probability_scale"] = _round(component_scale, 3)
        component_rows.append(row)
    return {
        "available": bool(components),
        "version": "physical_weather_modifier_v1",
        "state": state,
        "label": label,
        "definition": "AWC/METAR 与 WU/TWC 的云、雨、阵雨、低云、辐射、露点、气压和 AWC 风场只调节盘中峰值路径，不替代温度硬实况、温度轨迹或最终结算。",
        "peak_mu_adjustment_c": _round(net_delta, 3),
        "path_probability_scale": _round(path_scale, 3),
        "peak_sigma_add_c": 0.15 if state == "cooling" else 0.0,
        "cooling_evidence": _round(cooling_evidence, 3),
        "warming_evidence": _round(warming_evidence, 3),
        "components": component_rows,
        "metar_weather_profile": metar_profile,
        "precipitation_profile": precip_profile,
        "cloud_cover_pct": _round(cloud, 1),
        "cloud_cover_low_pct": _round(low_cloud, 1),
        "cloud_cover_mid_pct": _round(mid_cloud, 1),
        "cloud_cover_high_pct": _round(high_cloud, 1),
        "solar_radiation": _round(solar, 1),
        "dewpoint_c": _round(dew, 1),
        "pressure_hpa": pressure_profile.get("pressure_hpa"),
        "pressure_trend_hpa": pressure_profile.get("pressure_trend_hpa"),
        "dewpoint_profile": dewpoint_profile,
        "pressure_profile": pressure_profile,
        "precip_nowcast_1_3h": nowcast,
        "wind_profile": wind_profile,
    }


def _source_observation_comparison(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    *,
    rounding_rule: str,
    market_unit: str = "C",
    reference_time: Optional[datetime] = None,
    event_date: Any = None,
) -> Dict[str, Any]:
    ref_dt = reference_time or datetime.now(timezone.utc)
    event_key = _event_date_key(
        event_date
        or features.get("metar_target_event_date")
        or features.get("zuuu_metar_raw_awc_target_event_date")
        or features.get("wu_twc_observed_intraday_target_event_date")
    )

    def _age_minutes(dt_text: Any) -> Optional[float]:
        dt = _parse_datetime(dt_text)
        if dt is None:
            return None
        return max(0.0, (ref_dt - dt).total_seconds() / 60.0)

    def _record(
        *,
        source_id: str,
        source_label: str,
        role: str,
        hardness: float,
        current_temp_c: Any,
        today_max_c: Any,
        observed_at: Any,
        source_url: Any = "",
        latest_raw: Any = "",
        raw_observations: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        current = _to_float(current_temp_c)
        today_max = _to_float(today_max_c)
        bucket_value = today_max if today_max is not None else current
        observed_dt = _parse_datetime(observed_at)
        observed_local_date = observed_dt.astimezone(CHENGDU_TZ).date().isoformat() if observed_dt else ""
        event_date_match = bool(event_key and observed_local_date and observed_local_date == event_key)
        usable_for_event = bool((not event_key or event_date_match) and bucket_value is not None)
        raw_floor_bucket = _raw_decimal_floor_bucket_label(rows, bucket_value, market_unit)
        display_bucket = (
            _display_integer_bucket_label(rows, bucket_value, market_unit)
            if str(rounding_rule or "").strip().lower() == "whole_degrees"
            else raw_floor_bucket
        )
        settlement_bucket = display_bucket or raw_floor_bucket
        visual_bucket = _nearest_single_bucket_label(rows, bucket_value)
        return {
            "source_id": source_id,
            "source_label": source_label,
            "role": role,
            "hardness": hardness,
            "current_temp_c": _round(current, 2),
            "today_max_c": _round(today_max, 2),
            "bucket_basis_value_c": _round(bucket_value, 2),
            "settlement_proxy_bucket": settlement_bucket,
            "settlement_display_bucket": display_bucket,
            "raw_decimal_floor_proxy_bucket": raw_floor_bucket,
            "visual_nearest_bucket": visual_bucket,
            "event_date": event_key,
            "observed_local_date": observed_local_date,
            "event_date_match": event_date_match if event_key else None,
            "usable_for_event": usable_for_event,
            "event_date_mismatch_reason": (
                "observation_local_date_not_target_event_date"
                if event_key and observed_local_date and observed_local_date != event_key
                else ""
            ),
            "bucket_mismatch_by_visual_rounding": bool(raw_floor_bucket and settlement_bucket and raw_floor_bucket != settlement_bucket),
            "bucket_mismatch_by_raw_floor": bool(raw_floor_bucket and settlement_bucket and raw_floor_bucket != settlement_bucket),
            "observed_at_utc": _fmt_utc_seconds(observed_dt),
            "observed_at_local": _fmt_local_seconds(observed_dt),
            "age_minutes": _round(_age_minutes(observed_at), 1),
            "source_url": str(source_url or ""),
            "latest_raw": str(latest_raw or ""),
            "raw_observations": dict(raw_observations or {}),
        }

    awc_latest_raw = features.get("zuuu_metar_raw_awc_latest_raw")
    awc_today_max_raw = features.get("zuuu_metar_raw_awc_today_max_raw")
    awc_raw_observations = {
        "today_max_metar": _metar_temperature_token(awc_today_max_raw),
        "latest_metar": _metar_temperature_token(awc_latest_raw),
        "fetch_meta": {
            "request_started_at": features.get("zuuu_metar_raw_awc_request_started_at"),
            "fetched_at": features.get("zuuu_metar_raw_awc_fetched_at"),
            "fetch_duration_ms": features.get("zuuu_metar_raw_awc_fetch_duration_ms"),
            "target_poll_seconds": features.get("zuuu_metar_raw_awc_target_poll_seconds"),
        },
    }
    awc_record = _record(
        source_id="zuuu_metar_raw_awc",
        source_label="AWC Raw METAR",
        role="官方机场 Raw METAR 硬实况/审计源",
        hardness=1.0,
        current_temp_c=features.get("zuuu_metar_raw_awc_latest_temp_c", features.get("metar_temperature_c")),
        today_max_c=features.get("zuuu_metar_raw_awc_today_max_c", features.get("metar_temperature_c")),
        observed_at=features.get("zuuu_metar_raw_awc_today_max_at") or features.get("zuuu_metar_raw_awc_latest_at"),
        source_url=features.get("zuuu_metar_raw_awc_url"),
        latest_raw=awc_latest_raw,
        raw_observations=awc_raw_observations,
    )
    wu_raw_observations = {
        "history_metric": {
            "raw_value": features.get("wu_twc_observed_intraday_history_raw_value"),
            "raw_unit": features.get("wu_twc_observed_intraday_history_raw_unit"),
            "observed_at": features.get("wu_twc_observed_intraday_history_raw_at"),
            "source_units": features.get("wu_twc_observed_intraday_history_units"),
        },
        "history_imperial": {
            "raw_value": features.get("wu_twc_observed_intraday_history_raw_imperial_value"),
            "raw_unit": features.get("wu_twc_observed_intraday_history_raw_imperial_unit"),
            "observed_at": features.get("wu_twc_observed_intraday_history_raw_imperial_at"),
            "source_units": "e",
        },
        "current_metric": {
            "raw_value": features.get("wu_twc_observed_intraday_current_metric_raw_value"),
            "raw_unit": features.get("wu_twc_observed_intraday_current_metric_raw_unit"),
            "max_since7am_raw_value": features.get("wu_twc_observed_intraday_current_metric_max_since7am_raw_value"),
            "observed_at": features.get("wu_twc_observed_intraday_current_metric_raw_at"),
            "source_units": "m",
        },
        "current_imperial": {
            "raw_value": features.get("wu_twc_observed_intraday_current_imperial_raw_value")
            or features.get("wu_twc_observed_intraday_current_raw_value"),
            "raw_unit": features.get("wu_twc_observed_intraday_current_imperial_raw_unit")
            or features.get("wu_twc_observed_intraday_current_raw_unit"),
            "max_since7am_raw_value": features.get("wu_twc_observed_intraday_current_imperial_max_since7am_raw_value")
            or features.get("wu_twc_observed_intraday_max_since7am_raw_value"),
            "observed_at": features.get("wu_twc_observed_intraday_current_imperial_raw_at")
            or features.get("wu_twc_observed_intraday_current_at"),
            "source_units": "e",
        },
        "fetch_meta": {
            "request_started_at": features.get("wu_twc_observed_intraday_request_started_at"),
            "fetched_at": features.get("wu_twc_observed_intraday_fetched_at"),
            "fetch_duration_ms": features.get("wu_twc_observed_intraday_fetch_duration_ms"),
            "target_poll_seconds": features.get("wu_twc_observed_intraday_target_poll_seconds"),
        },
    }
    wu_record = _record(
        source_id="wu_twc_observed_intraday",
        source_label="WU/TWC",
        role="Polymarket/WU 结算生态展示源/实时包装源",
        hardness=0.65,
        current_temp_c=features.get("wu_twc_observed_intraday_current_temp_c"),
        today_max_c=features.get("wu_twc_observed_intraday_today_max_c"),
        observed_at=features.get("wu_twc_observed_intraday_today_max_at")
        or features.get("wu_twc_observed_intraday_current_at"),
        source_url=features.get("wu_twc_observed_intraday_source_url")
        or features.get("wu_twc_observed_intraday_resolution_source"),
        raw_observations=wu_raw_observations,
    )
    awc_bucket = str(awc_record.get("settlement_proxy_bucket") or "")
    wu_bucket = str(wu_record.get("settlement_proxy_bucket") or "")
    awc_bucket_for_event = awc_bucket if awc_record.get("usable_for_event") else ""
    wu_bucket_for_event = wu_bucket if wu_record.get("usable_for_event") else ""
    same_bucket = bool(awc_bucket_for_event and wu_bucket_for_event and awc_bucket_for_event == wu_bucket_for_event)
    awc_dt = _parse_datetime(awc_record.get("observed_at_utc"))
    wu_dt = _parse_datetime(wu_record.get("observed_at_utc"))
    newer_source = ""
    newer_by_seconds: Optional[float] = None
    if awc_dt and wu_dt:
        diff = (wu_dt - awc_dt).total_seconds()
        if diff > 0:
            newer_source = "wu_twc_observed_intraday"
            newer_by_seconds = diff
        elif diff < 0:
            newer_source = "zuuu_metar_raw_awc"
            newer_by_seconds = abs(diff)
        else:
            newer_source = "tie"
            newer_by_seconds = 0.0
    primary_source = "zuuu_metar_raw_awc" if awc_bucket_for_event else ("wu_twc_observed_intraday" if wu_bucket_for_event else "")
    delta_c = None
    if _to_float(awc_record.get("bucket_basis_value_c")) is not None and _to_float(wu_record.get("bucket_basis_value_c")) is not None:
        delta_c = float(awc_record["bucket_basis_value_c"]) - float(wu_record["bucket_basis_value_c"])
    warning = ""
    if wu_record.get("bucket_mismatch_by_visual_rounding"):
        warning = (
            f"WU/TWC API 小数值 {wu_record.get('bucket_basis_value_c')}°C 的 raw-floor 诊断桶是 "
            f"{wu_record.get('raw_decimal_floor_proxy_bucket')}，但 WU 页面/整度展示代理桶是 "
            f"{wu_record.get('settlement_proxy_bucket')}。结算应看 WU history 整数摄氏展示；raw 小数只用于轨迹，不向下取整当最终结算。"
        )
    mismatch_notes = []
    for label, record in (("AWC/METAR", awc_record), ("WU/TWC", wu_record)):
        if event_key and record.get("settlement_proxy_bucket") and record.get("usable_for_event") is False:
            observed_date = str(record.get("observed_local_date") or "未知日期")
            mismatch_notes.append(f"{label} 最新观测属于 {observed_date}，不是事件日 {event_key}，仅作抓取健康/辅助观察")
    if mismatch_notes:
        warning = "；".join(mismatch_notes) + "；等待事件日当日硬实况刷新。"
    def _append_warning(text: str) -> None:
        nonlocal warning
        if not text:
            return
        warning = (warning + " " if warning else "") + text

    if awc_bucket_for_event and wu_bucket_for_event:
        if not same_bucket:
            _append_warning("AWC 与 WU/TWC 当前不在同一结算代理桶，按来源硬度优先看 AWC，同时等待 WU/TWC 结算展示源继续更新。")
        elif not warning:
            warning = "AWC 与 WU/TWC 当前结算代理桶一致，可互相校验；仍以最终 WU history 结算页为最终事实。"
    elif awc_bucket_for_event and not wu_bucket_for_event:
        _append_warning("当前只有事件日 AWC/METAR 可落桶；WU/TWC 包装源缺失、未更新或非事件日，不能用 WU/TWC 校验同桶。")
    elif wu_bucket_for_event and not awc_bucket_for_event:
        _append_warning("当前只有事件日 WU/TWC 包装源可落桶；缺少事件日 AWC/METAR 硬实况校验，降低硬事实置信。")
    elif not warning:
        warning = "AWC 与 WU/TWC 当前都缺少可落桶实况，等待下一次实况刷新。"
    verdict = (
        "硬实况支持 " + awc_bucket_for_event
        if awc_bucket_for_event
        else (
            "仅有 WU/TWC 包装源支持 " + wu_bucket_for_event
            if wu_bucket_for_event
            else (f"等待 {event_key} 当日可落桶硬实况" if event_key and (awc_bucket or wu_bucket) else "缺少可落桶实况")
        )
    )
    return {
        "rounding_rule": str(rounding_rule or ""),
        "rounding_rule_label": _rounding_rule_label(rounding_rule),
        "market_unit": str(market_unit or "C"),
        "primary_source": primary_source,
        "primary_bucket": awc_bucket_for_event or wu_bucket_for_event or "",
        "event_date": event_key,
        "event_observation_available": bool(awc_bucket_for_event or wu_bucket_for_event),
        "same_settlement_bucket": same_bucket,
        "newer_source": newer_source,
        "newer_by_seconds": _round(newer_by_seconds, 0),
        "delta_c_awc_minus_wu": _round(delta_c, 2),
        "verdict": verdict,
        "warning": warning.strip(),
        "awc": awc_record,
        "wu_twc": wu_record,
        "source_hardness_order": ["zuuu_metar_raw_awc", "wu_twc_observed_intraday"],
    }


def _temperature_stage_from_payload(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        return None
    dt = _payload_evidence_dt(payload)
    if dt is None:
        return None
    local_dt = dt.astimezone(CHENGDU_TZ)
    observed = _to_float(features.get("observed_temp_c"))
    observed_max, observed_max_source, observed_max_na_reason = _hard_observed_today_max_c(features)
    observed_path, observed_path_source = _observed_path_temperature_c(observed, observed_max)
    rise = _temperature_rise_rate_value(features)
    selected_rates, selected_momentum_meta = _selected_temperature_momentum_rates(features)
    has_momentum_field = any(
        _to_float(selected_rates.get(label)) is not None
        for label in TEMPERATURE_MOMENTUM_WINDOWS
    )
    if observed is None and observed_max is None and rise is None and not has_momentum_field:
        return None
    soft_forecast, soft_source = _soft_forecast_peak(features)
    gap = soft_forecast - observed_path if soft_forecast is not None and observed_path is not None else None
    groups = payload.get("weather_likelihood_feature_groups")
    physical_modifier = _physical_weather_modifier(features, groups if isinstance(groups, Mapping) else None)
    stage = {
        "timestamp": dt.isoformat(),
        "local_time": local_dt.strftime("%H:%M:%S"),
        "local_hour": int(local_dt.hour),
        "hour_bucket": local_dt.replace(minute=0, second=0, microsecond=0).isoformat(),
        "created_at": str(payload.get("_created_at") or ""),
        "event_run_id": payload.get("_event_run_id"),
        "observed_temp_c": _round(observed, 2),
        "observed_path_temp_c": _round(observed_path, 2),
        "observed_path_temp_source": observed_path_source,
        "observed_current_temp_c": _round(observed, 2),
        "observed_today_max_c": _round(observed_max, 2),
        "observed_today_max_source": observed_max_source,
        "observed_today_max_na_reason": observed_max_na_reason,
        "warming_rate_c_per_hour": _round(rise, 2),
        "temperature_momentum_effective_c_per_hour": _round(features.get("temperature_momentum_effective_c_per_hour"), 2),
        "temperature_momentum_realization_state": str(features.get("temperature_momentum_realization_state") or ""),
        "temperature_momentum_sustained_warming": bool(features.get("temperature_momentum_sustained_warming")),
        "temperature_momentum_source": str(selected_momentum_meta.get("source") or ""),
        "temperature_momentum_core_source": str(selected_momentum_meta.get("core_source") or ""),
        "temperature_momentum_supplement_source": str(selected_momentum_meta.get("supplement_source") or ""),
        "temperature_momentum_wu_twc_supplement_demoted_reason": str(
            selected_momentum_meta.get("wu_twc_supplement_demoted_reason") or ""
        ),
        "soft_forecast_peak_c": _round(soft_forecast, 2),
        "soft_forecast_source": soft_source,
        "forecast_gap_c": _round(gap, 2),
        "peak_estimator_mu_c": _round(features.get("peak_estimator_mu_c"), 2),
        "cloud_cover_pct": _round(features.get("cloud_cover_pct"), 1),
        "cloud_cover_low_pct": _round(features.get("cloud_cover_low_pct"), 1),
        "metar_effective_cloud_cover_pct": _round(features.get("metar_effective_cloud_cover_pct"), 1),
        "solar_radiation": _round(features.get("solar_radiation"), 1),
        "dewpoint_c": _round(features.get("dewpoint_c"), 1),
        "observed_precip_rate_mm_per_h": _round(features.get("observed_precip_rate_mm_per_h"), 2),
        "metar_rain_observed": features.get("metar_rain_observed"),
        "metar_cavok_observed": features.get("metar_cavok_observed"),
        "awc_metar_no_precip_observed": features.get("awc_metar_no_precip_observed"),
        "physical_weather_state": physical_modifier.get("state"),
        "physical_weather_peak_mu_adjustment_c": physical_modifier.get("peak_mu_adjustment_c"),
        "physical_weather_modifier": physical_modifier,
    }
    for label in TEMPERATURE_MOMENTUM_WINDOWS:
        stage[f"temperature_momentum_{label}_c_per_hour"] = _round(selected_rates.get(label), 2)
        awc_rate = _to_float(features.get(f"zuuu_metar_raw_awc_temperature_momentum_{label}_c_per_hour"))
        if awc_rate is not None:
            stage[f"awc_metar_temperature_momentum_{label}_c_per_hour"] = _round(awc_rate, 2)
    return stage


def _momentum_state_from_rates(rates: Mapping[str, Optional[float]]) -> Tuple[str, str, str]:
    vals = {
        key: value
        for key, value in ((key, _to_float(value)) for key, value in rates.items())
        if value is not None and abs(float(value)) <= TEMPERATURE_RISE_RATE_UNTRUSTED_ABS_MAX
    }
    nums = [value for value in vals.values() if value is not None]
    if not nums:
        return "missing", "缺失", "缺少 24h/12h/6h/3h/2h/1h/45m/30m/15m/10m/5m/3m/2m/1m 温度动量，暂不能确认升温潜力是否兑现。"
    pairs = [(value, TEMPERATURE_MOMENTUM_WEIGHTS.get(key, 1.0)) for key, value in vals.items() if value is not None]
    denom = sum(weight for _, weight in pairs) or 1.0
    effective = sum(value * weight for value, weight in pairs) / denom
    recent = [vals[key] for key in TEMPERATURE_MOMENTUM_RECENT_WINDOWS if vals.get(key) is not None]
    short = sum(float(value) for value in recent) / max(1, len(recent)) if recent else vals.get("1h")
    positive_count = sum(1 for value in nums if value >= 0.25)
    nonpositive_count = sum(1 for value in nums if value <= 0.0)
    if short is not None and short <= -0.20 and effective <= 0.0:
        return "cooling", "目标桶受阻", "短窗口动量转负且多窗口有效动量不正，目标高温桶兑现受阻。"
    if short is not None and short <= 0.0 and effective <= 0.10:
        return "not_realized", "未兑现", "当前短窗口动量不正，物理升温潜力不能放大高温桶。"
    if len(nums) >= 2 and short is not None and short >= 0.35 and positive_count >= 2 and effective >= 0.25:
        return "sustained_warming", "持续升温", "分钟级到小时级动量显示升温正在持续兑现。"
    if short is not None and short >= 0.35 and effective >= 0.10:
        return "emerging_warming", "升温初现", "短窗口升温较强，但仍需中长窗口继续确认持续性。"
    if effective >= 0.15 and nonpositive_count == 0:
        return "weak_warming", "弱升温", "多周期动量偏正但不强，只能小幅确认升温潜力。"
    if abs(effective) < 0.15:
        return "flat", "走平", "多周期温度动量接近走平，晴空等物理潜力暂未强兑现。"
    return "mixed", "分歧", "多窗口动量方向分歧，暂不按强持续升温处理。"


def _temperature_momentum_from_stages(stages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not stages:
        return {
            "available": False,
            "state": "missing",
            "label": "缺失",
            "rates_c_per_hour": {},
            "conclusion": "缺少小时级温度样本，暂不能判断升温潜力是否兑现。",
        }
    latest = stages[-1]
    latest_uses_daily_max_floor = str(latest.get("observed_path_temp_source") or "") == "observed_today_max_floor"
    latest_rates = {
        label: _to_float(latest.get(f"temperature_momentum_{label}_c_per_hour"))
        for label in TEMPERATURE_MOMENTUM_WINDOWS
    }
    rates = {key: value for key, value in latest_rates.items() if value is not None}
    source = str(latest.get("temperature_momentum_source") or "features")
    latest_dt = _parse_datetime(latest.get("timestamp"))
    latest_temp = _to_float(latest.get("observed_path_temp_c"))
    if latest_temp is None:
        latest_temp = _to_float(latest.get("observed_temp_c"))
    if latest_dt is not None and latest_temp is not None and not latest_uses_daily_max_floor:
        for key in TEMPERATURE_MOMENTUM_WINDOWS:
            if key in rates:
                continue
            horizon = TEMPERATURE_MOMENTUM_HOURS.get(key)
            if horizon is None:
                continue
            candidates: List[Tuple[float, Mapping[str, Any]]] = []
            for stage in stages[:-1]:
                prior_dt = _parse_datetime(stage.get("timestamp"))
                prior_temp = _to_float(stage.get("observed_path_temp_c"))
                if prior_temp is None:
                    prior_temp = _to_float(stage.get("observed_temp_c"))
                if prior_dt is None or prior_temp is None:
                    continue
                elapsed_h = (latest_dt - prior_dt).total_seconds() / 3600.0
                if elapsed_h <= 0:
                    continue
                min_elapsed_h = max(30.0 / 3600.0, float(horizon) * 0.65)
                max_elapsed_h = (
                    max(60.0 / 3600.0, float(horizon) * 2.25)
                    if horizon < 1.0
                    else max(float(horizon) + 0.75, float(horizon) * 1.5)
                )
                if elapsed_h >= min_elapsed_h and elapsed_h <= max_elapsed_h:
                    candidates.append((abs(elapsed_h - float(horizon)), stage))
            if not candidates:
                continue
            _, prior = min(candidates, key=lambda item: item[0])
            prior_dt = _parse_datetime(prior.get("timestamp"))
            prior_temp = _to_float(prior.get("observed_path_temp_c"))
            if prior_temp is None:
                prior_temp = _to_float(prior.get("observed_temp_c"))
            if prior_dt is None or prior_temp is None:
                continue
            elapsed_h = max(1e-6, (latest_dt - prior_dt).total_seconds() / 3600.0)
            rates[key] = (float(latest_temp) - float(prior_temp)) / elapsed_h
            source = "recent_snapshots"
    if "1h" not in rates:
        fallback = _to_float(latest.get("temperature_path_rate_c_per_hour"))
        if fallback is not None:
            rates["1h"] = fallback
            source = "latest_temperature_path_rate"
    if "1h" not in rates and not latest_uses_daily_max_floor:
        fallback = _to_float(latest.get("warming_rate_c_per_hour"))
        if fallback is not None:
            rates["1h"] = fallback
            source = "latest_warming_rate"
    state, label, conclusion = _momentum_state_from_rates(rates)
    trusted_rates = {
        key: value
        for key, value in rates.items()
        if value is not None and abs(float(value)) <= TEMPERATURE_RISE_RATE_UNTRUSTED_ABS_MAX
    }
    pairs = [(value, TEMPERATURE_MOMENTUM_WEIGHTS.get(key, 1.0)) for key, value in trusted_rates.items()]
    effective = sum(value * weight for value, weight in pairs) / max(1e-6, sum(weight for _, weight in pairs)) if pairs else None
    latest_observed_max = _to_float(latest.get("observed_today_max_c"))
    latest_soft_peak = _to_float(latest.get("soft_forecast_peak_c"))
    latest_peak_mu = _to_float(latest.get("peak_estimator_mu_c"))
    peak_reference = latest_peak_mu if latest_peak_mu is not None else latest_soft_peak
    physical_modifier = (
        latest.get("physical_weather_modifier")
        if isinstance(latest.get("physical_weather_modifier"), Mapping)
        else {}
    )
    physical_state = str(physical_modifier.get("state") or "").strip()
    physical_delta = _to_float(physical_modifier.get("peak_mu_adjustment_c"))
    physical_cooling = bool(physical_state == "cooling" and physical_delta is not None and float(physical_delta) <= -0.25)
    short_cooling_after_peak = any(
        _to_float(rates.get(key)) is not None and float(rates[key]) <= -0.50
        for key in ("1m", "2m", "3m", "5m", "10m", "15m", "30m")
    )
    if (
        state in {"cooling", "not_realized", "mixed"}
        and short_cooling_after_peak
        and latest_observed_max is not None
        and peak_reference is not None
        and float(latest_observed_max) >= float(peak_reference) - 0.25
    ):
        state = "post_peak_cooling"
        label = "已达高点后回落"
        conclusion = "已观测最高已达到或超过峰值参考；短窗口回落只压制更高桶，不否定已达到的目标桶。"
    elif physical_cooling and state in {"sustained_warming", "emerging_warming", "weak_warming", "mixed"}:
        state = "physically_limited_warming"
        label = "云雨压制升温"
        conclusion = (
            "温度动量虽有升温信号，但 AWC/METAR 或 WU/TWC 云雨物理证据显示后续上探受压；"
            "目标桶切换需按修正后的峰值路径判断。"
        )
    gate = _temperature_momentum_posterior_gate(trusted_rates, state=state, effective=effective)
    return {
        "available": bool(rates),
        "state": state,
        "label": label,
        "rates_c_per_hour": {key: _round(trusted_rates[key], 2) for key in TEMPERATURE_MOMENTUM_WINDOWS if key in trusted_rates},
        "suppressed_untrusted_rates_c_per_hour": {
            key: _round(rates[key], 2)
            for key in TEMPERATURE_MOMENTUM_WINDOWS
            if key in rates and key not in trusted_rates
        },
        "effective_rate_c_per_hour": _round(effective, 2),
        "sustained_warming": bool(state == "sustained_warming"),
        "posterior_gate_allowed": bool(gate["allowed"]),
        "posterior_gate_state": str(gate["state"]),
        "posterior_gate_reason": str(gate["reason"]),
        "posterior_confirmed_windows": list(gate["confirmed_windows"]),
        "posterior_confirmed_window_count": int(gate["confirmed_positive_window_count"]),
        "positive_short_windows": list(gate["positive_short_windows"]),
        "posterior_effective_rate_c_per_hour": _round(effective, 2) if gate["allowed"] else None,
        "source": source,
        "physical_weather_state": physical_state or None,
        "physical_weather_peak_mu_adjustment_c": _round(physical_delta, 3),
        "physical_weather_modifier": dict(physical_modifier) if physical_modifier else {},
        "conclusion": conclusion,
    }


def _temperature_stages(recent_payloads: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_hour: Dict[str, Dict[str, Any]] = {}
    for payload in recent_payloads:
        stage = _temperature_stage_from_payload(payload)
        if not stage:
            continue
        by_hour[str(stage["hour_bucket"])] = stage
    stages = sorted(by_hour.values(), key=lambda item: str(item.get("timestamp") or ""))
    for idx, stage in enumerate(stages):
        prev = stages[idx - 1] if idx > 0 else {}
        path_rate = None
        if idx > 0:
            current_dt = _parse_datetime(stage.get("timestamp"))
            prev_dt = _parse_datetime(prev.get("timestamp"))
            current_path_temp = _to_float(stage.get("observed_path_temp_c"))
            if current_path_temp is None:
                current_path_temp = _to_float(stage.get("observed_temp_c"))
            prev_path_temp = _to_float(prev.get("observed_path_temp_c"))
            if prev_path_temp is None:
                prev_path_temp = _to_float(prev.get("observed_temp_c"))
            if current_dt is not None and prev_dt is not None and current_path_temp is not None and prev_path_temp is not None:
                elapsed_h = (current_dt - prev_dt).total_seconds() / 3600.0
                if elapsed_h > 0:
                    path_rate = (float(current_path_temp) - float(prev_path_temp)) / elapsed_h
        stage["temperature_path_rate_c_per_hour"] = _round(path_rate, 2)
        use_path_rate = str(stage.get("observed_path_temp_source") or "") == "observed_today_max_floor"
        prev_uses_path_rate = str(prev.get("observed_path_temp_source") or "") == "observed_today_max_floor"
        current_rate = _to_float(stage.get("temperature_path_rate_c_per_hour")) if use_path_rate else None
        if current_rate is None:
            current_rate = _to_float(stage.get("warming_rate_c_per_hour"))
        prev_rate = _to_float(prev.get("temperature_path_rate_c_per_hour")) if prev_uses_path_rate else None
        if prev_rate is None:
            prev_rate = _to_float(prev.get("warming_rate_c_per_hour"))
        delta_rate = current_rate - prev_rate if current_rate is not None and prev_rate is not None else None
        if delta_rate is None:
            trend_state = "baseline"
            trend_label = "基线"
        elif delta_rate >= 0.5:
            trend_state = "strengthening"
            trend_label = "趋势变强"
        elif delta_rate <= -0.5:
            trend_state = "weakening"
            trend_label = "趋势变弱"
        else:
            trend_state = "stable"
            trend_label = "基本稳定"
        stage["warming_rate_delta_c_per_hour"] = _round(delta_rate, 2)
        stage["trend_state"] = trend_state
        stage["trend_label"] = trend_label
    latest = stages[-1] if stages else {}
    latest_state = str(latest.get("trend_state") or "missing")
    if latest_state == "weakening":
        conclusion = "最近一个小时级阶段显示升温趋势变弱。"
    elif latest_state == "strengthening":
        conclusion = "最近一个小时级阶段显示升温趋势变强。"
    elif latest_state == "stable":
        conclusion = "最近一个小时级阶段升温趋势基本稳定。"
    elif stages:
        conclusion = "已有小时级基线，等待下一小时确认趋势。"
    else:
        conclusion = "缺少小时级温度阶段样本，暂不能判断趋势强弱。"
    return {
        "available": bool(stages),
        "latest_trend_state": latest_state if stages else "missing",
        "latest_trend_label": str(latest.get("trend_label") or "缺失"),
        "conclusion": conclusion,
        "temperature_momentum": _temperature_momentum_from_stages(stages),
        "stages": stages[-10:],
    }


def _warming_pace_assessment(
    features: Mapping[str, Any],
    temperature_trend: Optional[Mapping[str, Any]] = None,
    event_date: Optional[str] = None,
) -> Dict[str, Any]:
    trend = temperature_trend if isinstance(temperature_trend, Mapping) else {}
    momentum = trend.get("temperature_momentum") if isinstance(trend.get("temperature_momentum"), Mapping) else {}
    current_temp, current_temp_source = _observed_current_temperature_c(features)
    peak_mu = _to_float(features.get("peak_estimator_mu_c"))
    peak_source = "peak_estimator_mu_c"
    if peak_mu is None:
        peak_mu, peak_source = _soft_forecast_peak(features)
        peak_source = peak_source or "soft_forecast_peak_c"
    actual_rate = _to_float(momentum.get("effective_rate_c_per_hour"))
    actual_source = "temperature_momentum_effective_c_per_hour"
    if actual_rate is None:
        actual_rate = _temperature_rise_rate_value(features)
        actual_source = "temperature_rise_rate_c_per_hour"
    observation_hour = _to_float(features.get("observation_hour_local"))
    if observation_hour is None:
        stages = trend.get("stages") if isinstance(trend.get("stages"), Sequence) and not isinstance(trend.get("stages"), (str, bytes)) else []
        latest = stages[-1] if stages else {}
        if isinstance(latest, Mapping):
            observation_hour = _to_float(latest.get("local_hour"))
    peak_hour, peak_meta = _expected_peak_hour_from_features(features, event_date=event_date)
    missing = []
    if current_temp is None:
        missing.append("observed_temperature_missing")
    if peak_mu is None:
        missing.append("peak_estimator_missing")
    if actual_rate is None:
        missing.append("warming_rate_missing")
    if observation_hour is None:
        missing.append("observation_hour_missing")
    if peak_hour is None:
        missing.append("peak_hour_missing")
    if missing:
        return {
            "available": False,
            "state": "unknown",
            "label": "节奏待确认",
            "tone": "warn",
            "missing_fields": missing,
            "conclusion": "缺少当前温度、峰值估计、升温率或本地时间，暂不能判断升温节奏。",
            "version": "warming_pace_assessment_v1",
        }

    hours_to_peak = float(peak_hour) - float(observation_hour)
    remaining_c = float(peak_mu) - float(current_temp)
    denominator_hours = max(0.25, hours_to_peak)
    required_rate = max(0.0, remaining_c) / denominator_hours
    ratio = float(actual_rate) / required_rate if required_rate >= 0.05 else None
    if remaining_c <= 0.0:
        if hours_to_peak > 0.25 and float(actual_rate) >= 0.60:
            state, label, tone = "fast", "升温快", "ok"
            conclusion = "当前温度已达到或超过峰值估计，且仍有正升温动量，升温节奏偏快。"
        else:
            state, label, tone = "on_track", "升温正常", "ok"
            conclusion = "当前温度已接近或达到峰值估计，后续主要观察是否继续刷新已观测最高。"
    elif hours_to_peak <= -0.50:
        if float(actual_rate) <= 0.10:
            state, label, tone = "on_track", "升温正常", "ok"
            conclusion = "预期最高温窗口已过，当前升温动量不强，节奏按峰后观察处理。"
        else:
            state, label, tone = "fast", "升温快", "ok"
            conclusion = "预期最高温窗口已过但仍在升温，存在继续刷新最高温的快节奏风险。"
    elif float(actual_rate) < max(0.12, required_rate * 0.65):
        state, label, tone = "lagging", "升温落后", "warn"
        conclusion = "当前有效升温率低于到达峰值估计所需节奏，升温进度落后。"
    elif float(actual_rate) > max(0.85, required_rate * 1.45):
        state, label, tone = "fast", "升温快", "ok"
        conclusion = "当前有效升温率明显高于剩余路径所需节奏，升温偏快。"
    else:
        state, label, tone = "on_track", "升温正常", "ok"
        conclusion = "当前有效升温率与剩余路径所需节奏大体匹配。"
    return {
        "available": True,
        "state": state,
        "label": label,
        "tone": tone,
        "conclusion": conclusion,
        "observed_temp_c": _round(current_temp, 2),
        "observed_temp_source": current_temp_source,
        "peak_estimator_mu_c": _round(peak_mu, 2),
        "peak_estimator_source": peak_source,
        "observation_hour_local": _round(observation_hour, 2),
        "peak_hour_local": _round(peak_hour, 2),
        "peak_hour_source": str(peak_meta.get("expected_peak_hour_source") or ""),
        "peak_at_local": peak_meta.get("expected_peak_at_local"),
        "hours_to_peak_window": _round(hours_to_peak, 2),
        "remaining_to_peak_c": _round(remaining_c, 2),
        "actual_rate_c_per_hour": _round(actual_rate, 2),
        "actual_rate_source": actual_source,
        "required_rate_c_per_hour": _round(required_rate, 2),
        "pace_ratio": _round(ratio, 2),
        "version": "warming_pace_assessment_v1",
    }


def _clamp01(value: Any) -> float:
    num = _to_float(value)
    if num is None:
        return 0.0
    return max(0.0, min(1.0, float(num)))


def _temperature_realization_reliability(
    temperature_trend: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    high_temp_guard: Mapping[str, Any],
    target_bucket: Optional[str] = None,
    target_observation_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    momentum = (
        temperature_trend.get("temperature_momentum")
        if isinstance(temperature_trend.get("temperature_momentum"), Mapping)
        else {}
    )
    rates = momentum.get("rates_c_per_hour") if isinstance(momentum.get("rates_c_per_hour"), Mapping) else {}
    vals = {str(key): _to_float(value) for key, value in rates.items() if _to_float(value) is not None}
    nums = list(vals.values())
    effective = _to_float(momentum.get("effective_rate_c_per_hour"))
    if effective is None and nums:
        pairs = [(value, TEMPERATURE_MOMENTUM_WEIGHTS.get(key, 1.0)) for key, value in vals.items()]
        denom = sum(weight for _, weight in pairs) or 1.0
        effective = sum(value * weight for value, weight in pairs) / denom
    coverage = min(1.0, len(nums) / float(len(TEMPERATURE_MOMENTUM_WINDOWS)))
    positive_ratio = sum(1 for value in nums if value >= 0.25) / max(1, len(nums))
    negative_ratio = sum(1 for value in nums if value <= -0.10) / max(1, len(nums))
    nonpositive_ratio = sum(1 for value in nums if value <= 0.0) / max(1, len(nums))

    warm_score = 0.20
    cool_score = 0.20
    neutral_score = 0.38 + (1.0 - coverage) * 0.55
    if effective is not None:
        warm_score += _clamp01((effective - 0.05) / 1.45) * 1.25
        cool_score += _clamp01((-effective + 0.05) / 1.05) * 1.25
        if abs(effective) < 0.15:
            neutral_score += 0.28
    recent_vals = [vals[key] for key in TEMPERATURE_MOMENTUM_RECENT_WINDOWS if vals.get(key) is not None]
    recent_rate = sum(float(value) for value in recent_vals) / max(1, len(recent_vals)) if recent_vals else vals.get("1h")
    if recent_rate is not None:
        warm_score += _clamp01((recent_rate - 0.10) / 1.40) * 0.70
        cool_score += _clamp01((-recent_rate + 0.05) / 0.90) * 0.70
    warm_score += positive_ratio * 0.55
    cool_score += max(negative_ratio, nonpositive_ratio * 0.55) * 0.55

    trend_state = str(temperature_trend.get("latest_trend_state") or "")
    if trend_state == "strengthening":
        warm_score += 0.25
    elif trend_state == "weakening":
        cool_score += 0.30
    elif trend_state == "stable":
        neutral_score += 0.10

    if bool(high_temp_guard.get("active")):
        cool_score += 0.40
        warm_score *= 0.72

    strong_no_precip = any(
        item.get("key") == "radar_precip_echo"
        and item.get("metrics", {}).get("evidence_state") == "strong_no_precip"
        for item in evidence
    )
    precip_cooling = any(item.get("key") == "radar_precip_echo" and item.get("state") == "cooling" for item in evidence)
    if strong_no_precip:
        warm_score += 0.12
    if precip_cooling:
        cool_score += 0.45
        warm_score *= 0.70

    scores = [warm_score, cool_score, neutral_score]
    max_score = max(scores)
    exp_scores = [math.exp(score - max_score) for score in scores]
    denom = sum(exp_scores) or 1.0
    warm_prob, cool_prob, neutral_prob = [value / denom for value in exp_scores]
    confidence = min(0.98, max(0.15, 0.30 + 0.55 * coverage + 0.15 * abs(warm_prob - cool_prob)))
    target_observed = target_observation_state if isinstance(target_observation_state, Mapping) else {}
    target_text = str(target_bucket or "").strip().lower()
    target_is_lower_boundary = "or below" in target_text or target_text.startswith("<=") or "below" in target_text
    realization_mode = "warming_needed"
    warming_label = "升温支持目标桶兑现"
    cooling_label = "受阻/降温压制目标桶"
    if target_is_lower_boundary:
        realization_mode = "lower_boundary_hold_needed"
        warming_label = "升温突破边界风险"
        cooling_label = "守住低边界支持"
    if bool(target_observed.get("exceeded")):
        label = "低边界已被硬实况突破" if target_is_lower_boundary else "目标桶已被硬实况突破"
        state = "exceeded"
        realization_mode = "observed_exceeded_target_bucket"
        warming_label = "仍支持目标桶" if not target_is_lower_boundary else "硬实况已突破低边界"
        cooling_label = "硬实况已突破目标桶" if not target_is_lower_boundary else "边界已失效"
        warm_prob = min(warm_prob, 0.05)
        cool_prob = max(cool_prob, 0.90)
        if warm_prob + cool_prob > 1.0:
            cool_prob = max(0.0, 1.0 - warm_prob)
        neutral_prob = max(0.0, 1.0 - warm_prob - cool_prob)
        confidence = max(confidence, 0.93)
    elif bool(target_observed.get("realized")):
        source_hardness = _to_float(target_observed.get("source_hardness")) or 0.75
        soft_display = bool(target_observed.get("is_soft_display_source")) or source_hardness < 0.85
        label = "目标桶已由软展示源触达，等待 AWC 确认" if soft_display else "目标桶已由硬实况触达"
        state = "realized"
        realization_mode = "soft_display_in_target_bucket_waiting_hard_confirmation" if soft_display else "observed_in_target_bucket"
        warming_label = "软展示源已触达目标桶" if soft_display else "硬实况已触达目标桶"
        cooling_label = "等待硬实况确认/突破更高桶风险" if soft_display else "突破更高桶风险"
        reached_score = max(0.78, min(0.96, 0.72 + 0.22 * source_hardness))
        if trend_state == "weakening" or (effective is not None and effective <= 0.20):
            reached_score = min(0.98, reached_score + 0.04)
        warm_prob = reached_score
        cool_prob = min(cool_prob, 0.12 if trend_state == "weakening" else 0.18)
        if warm_prob + cool_prob > 1.0:
            cool_prob = max(0.0, 1.0 - warm_prob)
        neutral_prob = max(0.0, 1.0 - warm_prob - cool_prob)
        confidence = max(confidence, min(0.98, 0.82 + 0.12 * source_hardness))
    elif target_is_lower_boundary and bool(target_observed.get("alive")):
        observed_max = _to_float(target_observed.get("observed_max_c"))
        boundary_temp = _bucket_temp(target_bucket)
        next_break_temp = float(boundary_temp) + 1.0 if boundary_temp is not None else None
        gap_to_break = (
            float(next_break_temp) - float(observed_max)
            if next_break_temp is not None
            and observed_max is not None
            and math.isfinite(float(observed_max))
            else None
        )
        breakthrough_cap = 0.56
        neutral_floor = 0.16
        if gap_to_break is not None:
            if gap_to_break >= 3.0:
                breakthrough_cap = 0.18
                neutral_floor = 0.20
            elif gap_to_break >= 2.0:
                breakthrough_cap = 0.30
                neutral_floor = 0.18
            elif gap_to_break >= 1.0:
                breakthrough_cap = 0.48
                neutral_floor = 0.14
            elif gap_to_break > 0.0:
                breakthrough_cap = 0.68
                neutral_floor = 0.10
        if effective is not None:
            if effective >= 1.25 and (gap_to_break is None or gap_to_break <= 2.0):
                breakthrough_cap += 0.10
            elif effective <= 0.20:
                breakthrough_cap -= 0.08
        if trend_state == "weakening":
            breakthrough_cap -= 0.07
        elif trend_state == "strengthening" and (gap_to_break is None or gap_to_break <= 2.0):
            breakthrough_cap += 0.05
        if precip_cooling:
            breakthrough_cap -= 0.10
        elif strong_no_precip:
            breakthrough_cap += 0.03
        breakthrough_cap = max(0.04, min(0.78, breakthrough_cap))
        warm_prob = max(0.02, min(warm_prob, breakthrough_cap))
        neutral_prob = max(neutral_prob, neutral_floor)
        if warm_prob + neutral_prob > 0.92:
            neutral_prob = max(0.06, 0.92 - warm_prob)
        cool_prob = max(0.02, 1.0 - warm_prob - neutral_prob)
        total = warm_prob + cool_prob + neutral_prob
        if total > 0:
            warm_prob /= total
            cool_prob /= total
            neutral_prob /= total
        confidence = max(confidence, min(0.96, 0.74 + 0.06 * coverage))
        if warm_prob >= cool_prob + 0.08:
            label = "低边界被突破风险上升"
            state = "warming"
        elif cool_prob >= warm_prob + 0.08:
            label = "低边界仍受支持"
            state = "cooling"
        else:
            label = "低边界兑现待确认"
            state = "neutral"
    elif warm_prob > cool_prob and warm_prob >= 0.45:
        label = "低边界被突破风险上升" if target_is_lower_boundary else "目标桶升温兑现较可靠"
        state = "warming"
    elif cool_prob > warm_prob and cool_prob >= 0.45:
        label = "目标边界守住较可靠" if target_is_lower_boundary else "目标桶受阻较可靠"
        state = "cooling"
    else:
        label = "低边界兑现待确认" if target_is_lower_boundary else "目标桶兑现可靠性一般"
        state = "neutral"
    return {
        "version": "target_bucket_realization_reliability_v1",
        "state": state,
        "label": label,
        "title": "目标桶兑现可靠性",
        "target_bucket": target_bucket,
        "realization_mode": realization_mode,
        "target_observation_state": dict(target_observed) if target_observed else None,
        "warming_label": warming_label,
        "cooling_label": cooling_label,
        "warming_realization_prob": _round(warm_prob, 4),
        "cooling_realization_prob": _round(cool_prob, 4),
        "target_bucket_warming_realization_prob": _round(warm_prob, 4),
        "target_bucket_blocked_realization_prob": _round(cool_prob, 4),
        "neutral_or_uncertain_prob": _round(neutral_prob, 4),
        "warming_realization_pct": _pct(warm_prob),
        "cooling_realization_pct": _pct(cool_prob),
        "target_bucket_warming_realization_pct": _pct(warm_prob),
        "target_bucket_blocked_realization_pct": _pct(cool_prob),
        "neutral_or_uncertain_pct": _pct(neutral_prob),
        "neutral_label": "中性/不确定",
        "confidence": _round(confidence, 4),
        "confidence_pct": _pct(confidence),
        "sample_coverage": _round(coverage, 4),
        "sample_count": len(nums),
        "effective_rate_c_per_hour": _round(effective, 2),
        "positive_momentum_ratio": _round(positive_ratio, 4),
        "negative_momentum_ratio": _round(negative_ratio, 4),
        "method": "目标最高温桶兑现可靠性：用 24h/12h/6h/3h/2h/1h/45m/30m/15m/10m/5m/3m/2m/1m 温度动量、小时趋势、硬门控和强降水/无降水证据评估当前轨迹是否支持目标桶兑现",
    }


def _hard_source_direct_bucket(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    rounding_rule: str,
    market_unit: str,
) -> Dict[str, Any]:
    def _build(
        *,
        source_id: str,
        source_label: str,
        value_key: str,
        observed_at_keys: Sequence[str],
        fetched_at_key: str,
        fetch_duration_key: str,
        priority: float,
        fallback: bool = False,
        semantics: str = "hard_observed_lower_bound_constraint_for_ssot_posterior",
    ) -> Dict[str, Any]:
        value = _to_float(features.get(value_key))
        if value is None:
            return {}
        if source_id == "zuuu_metar_raw_awc" and bool(features.get("zuuu_metar_raw_awc_date_mismatch")):
            return {}
        if source_id == "wu_twc_observed_intraday" and bool(features.get("wu_twc_observed_intraday_stale")):
            return {}
        raw_bucket = _bucket_label_for_observed_value(rows, value, rounding_rule, market_unit)
        display_bucket = _display_integer_bucket_label(rows, value, market_unit)
        bucket = display_bucket or raw_bucket
        if not bucket:
            return {}
        observed_at = ""
        for key in observed_at_keys:
            observed_at = str(features.get(key) or "").strip()
            if observed_at:
                break
        return {
            "available": True,
            "source": "hard_source_direct",
            "source_id": source_id,
            "source_label": source_label,
            "bucket": bucket,
            "observed_max_c": _round(value, 2),
            "observed_at": observed_at,
            "observed_at_utc": _fmt_utc_seconds(_parse_datetime(observed_at)),
            "observed_at_local": _fmt_local_seconds(_parse_datetime(observed_at)),
            "fetched_at": str(features.get(fetched_at_key) or ""),
            "fetch_duration_ms": _round(features.get(fetch_duration_key), 0),
            "raw_bucket": raw_bucket,
            "display_bucket": display_bucket,
            "priority": float(priority),
            "fallback": bool(fallback),
            "semantics": semantics,
        }

    settlement_source = str(features.get("settlement_actual_source") or "settlement_actual").strip()
    settlement = _build(
        source_id=settlement_source or "settlement_actual",
        source_label="Final settlement",
        value_key="settlement_actual",
        observed_at_keys=("settlement_actual_at", "settlement_final_at", "settlement_signal_created_at"),
        fetched_at_key="settlement_actual_fetched_at",
        fetch_duration_key="settlement_actual_fetch_duration_ms",
        priority=2.0,
        semantics="final_bucket_lock_for_ssot_posterior",
    )
    if settlement:
        return settlement
    awc = _build(
        source_id="zuuu_metar_raw_awc",
        source_label="AWC Raw METAR",
        value_key="zuuu_metar_raw_awc_today_max_c",
        observed_at_keys=("zuuu_metar_raw_awc_today_max_at", "zuuu_metar_raw_awc_latest_at"),
        fetched_at_key="zuuu_metar_raw_awc_fetched_at",
        fetch_duration_key="zuuu_metar_raw_awc_fetch_duration_ms",
        priority=1.0,
    )
    wu_today_max = _build(
        source_id="wu_twc_observed_intraday",
        source_label="WU/TWC intraday observed",
        value_key="wu_twc_observed_intraday_today_max_c",
        observed_at_keys=("wu_twc_observed_intraday_today_max_at", "wu_twc_observed_intraday_current_at"),
        fetched_at_key="wu_twc_observed_intraday_fetched_at",
        fetch_duration_key="wu_twc_observed_intraday_fetch_duration_ms",
        priority=0.9,
        semantics="intraday_observed_lower_bound_constraint_for_ssot_posterior",
    )
    wu_current = _build(
        source_id="wu_twc_observed_intraday",
        source_label="WU/TWC intraday observed",
        value_key="wu_twc_observed_intraday_current_temp_c",
        observed_at_keys=("wu_twc_observed_intraday_current_at", "wu_twc_observed_intraday_today_max_at"),
        fetched_at_key="wu_twc_observed_intraday_fetched_at",
        fetch_duration_key="wu_twc_observed_intraday_fetch_duration_ms",
        priority=0.8,
        fallback=True,
        semantics="intraday_observed_lower_bound_constraint_for_ssot_posterior",
    )
    candidates = [item for item in (awc, wu_today_max, wu_current) if item]
    if not candidates:
        return {}
    candidates.sort(
        key=lambda item: (
            float(item.get("observed_max_c") or float("-inf")),
            float(item.get("priority") or 0.0),
            (_parse_datetime(item.get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        ),
        reverse=True,
    )
    return candidates[0]


def _target_bucket_outlook(
    rows: Sequence[Mapping[str, Any]],
    target_selection: Mapping[str, Any],
    realization_reliability: Mapping[str, Any],
    target_observation_state: Mapping[str, Any],
    features: Mapping[str, Any],
    rounding_rule: str = "",
    market_unit: str = "C",
) -> Dict[str, Any]:
    input_target_label = str(
        target_selection.get("target_bucket")
        or target_selection.get("target_label")
        or ""
    ).strip()
    if not input_target_label:
        return {"available": False, "reason": "target_bucket_missing"}

    entries: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("outcome") or raw.get("target_bucket") or raw.get("bucket") or "").strip()
        if not label:
            continue
        prob, prob_field = _target_bucket_probability(raw)
        if prob is None:
            continue
        entries.append(
            {
                "outcome": label,
                "probability": float(prob),
                "probability_pct": _pct(prob),
                "probability_field": prob_field,
                "relation_to_target": "unknown",
                "row_observed_constraints_hard_zero": bool(
                    raw.get("q_posterior_observed_constraints_hard_zero")
                    or raw.get("observed_constraints_hard_zero")
                ),
            }
        )
    if not entries:
        return {
            "available": False,
            "reason": "bucket_probability_missing",
            "target_bucket": input_target_label,
        }

    entries.sort(key=lambda item: float(item.get("probability") or 0.0), reverse=True)
    pre_constraint_posterior_final_entry = dict(entries[0])
    pre_constraint_cumulative_posterior_final_entry = dict(entries[0])
    hard_source_direct = _hard_source_direct_bucket(rows, features, rounding_rule, market_unit)
    hard_floor_label = str(hard_source_direct.get("bucket") or "").strip() if hard_source_direct.get("available") else ""
    hard_floor_zeroed_prob = 0.0
    hard_floor_kept_prob = 0.0
    hard_floor_applied = False
    if hard_source_direct.get("available") and hard_source_direct.get("bucket"):
        for entry in entries:
            raw_prob = float(entry.get("probability") or 0.0)
            entry["probability_pre_hard_source_floor"] = raw_prob
            entry["probability_pct_pre_hard_source_floor"] = _pct(raw_prob)
            cmp_to_floor = _compare_bucket_labels(entry.get("outcome"), hard_floor_label)
            if cmp_to_floor is not None and cmp_to_floor < 0:
                entry["probability"] = 0.0
                entry["probability_pct"] = 0.0
                entry["hard_source_floor_zeroed"] = True
                hard_floor_zeroed_prob += raw_prob
                hard_floor_applied = True
            else:
                entry["hard_source_floor_zeroed"] = False
                hard_floor_kept_prob += raw_prob
        if hard_floor_applied and hard_floor_kept_prob > 0:
            for entry in entries:
                if entry.get("hard_source_floor_zeroed"):
                    continue
                raw_prob = float(entry.get("probability_pre_hard_source_floor") or 0.0)
                constrained_prob = raw_prob / float(hard_floor_kept_prob)
                entry["probability"] = constrained_prob
                entry["probability_pct"] = _pct(constrained_prob)
                entry["probability_field"] = "hard_source_constrained_" + str(entry.get("probability_field") or "posterior")
        hard_source_direct["posterior_floor_constraint_applied"] = bool(hard_floor_applied)
        hard_source_direct["floor_bucket"] = hard_floor_label
        hard_source_direct["zeroed_probability_pct"] = _pct(hard_floor_zeroed_prob)
        hard_source_direct["kept_probability_pct"] = _pct(hard_floor_kept_prob)

    entries.sort(key=lambda item: float(item.get("probability") or 0.0), reverse=True)
    posterior_final_entry = dict(entries[0])
    cumulative_posterior_final_entry = dict(entries[0])
    final_entry = dict(posterior_final_entry)
    final_bucket_source = "hard_source_constrained_posterior" if hard_floor_applied else "posterior_distribution"
    entry_labels = {str(entry.get("outcome") or "").strip() for entry in entries}
    target_label = input_target_label
    explicit_target_label_source = str(
        target_selection.get("dynamic_target_source")
        or target_selection.get("target_bucket_source")
        or target_selection.get("source")
        or ""
    ).strip()
    target_label_source = explicit_target_label_source
    if not target_label_source:
        cmp_to_posterior_final = _compare_bucket_labels(target_label, posterior_final_entry.get("outcome"))
        target_label_source = (
            "hard_source_constrained_posterior_highest_bucket"
            if hard_floor_applied and cmp_to_posterior_final == 0
            else "posterior_highest_bucket"
        )
    target_invalid_by_hard_source = False
    if hard_source_direct.get("available") and hard_floor_label and target_label:
        cmp_to_hard_floor = _compare_bucket_labels(target_label, hard_floor_label)
        target_invalid_by_hard_source = bool(cmp_to_hard_floor is not None and cmp_to_hard_floor < 0)
    target_invalid_by_row_constraint = False
    if target_label:
        target_candidate_entry = next(
            (entry for entry in entries if str(entry.get("outcome") or "").strip() == target_label),
            None,
        )
        target_invalid_by_row_constraint = bool(
            target_candidate_entry
            and (
                target_candidate_entry.get("hard_source_floor_zeroed")
                or target_candidate_entry.get("row_observed_constraints_hard_zero")
            )
        )
    if (
        not target_label
        or target_label not in entry_labels
        or target_invalid_by_hard_source
        or target_invalid_by_row_constraint
    ):
        target_label = str(posterior_final_entry.get("outcome") or input_target_label).strip() or input_target_label
        target_label_source = (
            "hard_source_constrained_posterior_highest_bucket"
            if hard_floor_applied or target_invalid_by_hard_source or target_invalid_by_row_constraint
            else "posterior_highest_bucket"
        )
    target_entry: Optional[Dict[str, Any]] = None
    below_prob = 0.0
    at_prob = 0.0
    above_prob = 0.0
    unknown_prob = 0.0
    for entry in entries:
        cmp = _compare_bucket_labels(entry.get("outcome"), target_label)
        if cmp is None:
            unknown_prob += float(entry.get("probability") or 0.0)
            entry["relation_to_target"] = "unknown"
        elif cmp < 0:
            below_prob += float(entry.get("probability") or 0.0)
            entry["relation_to_target"] = "below_target"
        elif cmp > 0:
            above_prob += float(entry.get("probability") or 0.0)
            entry["relation_to_target"] = "above_target"
        else:
            at_prob += float(entry.get("probability") or 0.0)
            entry["relation_to_target"] = "target"
            target_entry = dict(entry)

    total_prob = below_prob + at_prob + above_prob + unknown_prob
    residual_prob = max(0.0, 1.0 - total_prob)
    watch = target_selection.get("watch_buckets")
    second_label = ""

    def _valid_second_label(label: Any) -> bool:
        text = str(label or "").strip()
        if not text or text == target_label:
            return False
        if _is_cumulative_bucket(text):
            return False
        cmp = _compare_bucket_labels(text, target_label)
        return bool(cmp is not None and cmp > 0)

    if isinstance(watch, Sequence) and not isinstance(watch, (str, bytes)):
        for raw in watch:
            if not isinstance(raw, Mapping):
                continue
            label = str(raw.get("outcome") or raw.get("target_bucket") or raw.get("bucket") or "").strip()
            if _valid_second_label(label):
                second_label = label
                break
    if not second_label:
        higher_entries = [
            entry
            for entry in entries
            if _valid_second_label(entry.get("outcome"))
        ]
        higher_entries.sort(
            key=lambda entry: (
                _bucket_rank_score(entry.get("outcome")) if _bucket_rank_score(entry.get("outcome")) is not None else 999.0,
                -float(entry.get("probability") or 0.0),
            )
        )
        if higher_entries:
            second_label = str(higher_entries[0].get("outcome") or "")
    second_entry = next((dict(entry) for entry in entries if str(entry.get("outcome") or "") == second_label), None)
    second_or_higher_prob = 0.0
    if second_label:
        for entry in entries:
            cmp = _compare_bucket_labels(entry.get("outcome"), second_label)
            if cmp is not None and cmp >= 0:
                second_or_higher_prob += float(entry.get("probability") or 0.0)

    observed_max, observed_max_source, _observed_max_na_reason = _hard_observed_today_max_c(features)
    if observed_max is None:
        observed_max = _to_float(target_observation_state.get("observed_max_c"))
        observed_max_source = str(target_observation_state.get("source") or observed_max_source or "")
    observed_temp = _to_float(features.get("observed_temp_c"))
    target_temp = _bucket_temp(target_label)
    second_temp = _bucket_temp(second_label)
    target_is_lower_boundary = _is_lower_boundary_bucket(target_label)
    gap_to_target = (
        (float(observed_max) - float(target_temp) if target_is_lower_boundary else float(target_temp) - float(observed_max))
        if target_temp is not None and observed_max is not None
        else None
    )
    gap_to_second = (
        float(second_temp) - float(observed_max)
        if second_temp is not None and observed_max is not None
        else None
    )

    current_state = "unknown"
    current_state_label = "等待实况"
    if observed_max is not None and target_temp is not None:
        if target_is_lower_boundary and observed_max <= float(target_temp) + 1e-9:
            current_state = "target_reached"
            current_state_label = "仍在低边界内"
        elif target_is_lower_boundary and observed_max > float(target_temp) + 1e-9:
            current_state = "above_target"
            current_state_label = "已突破低边界"
        elif second_temp is not None and observed_max >= float(second_temp) - 1e-9:
            current_state = "above_target"
            current_state_label = "已超过目标桶下沿"
        elif observed_max >= float(target_temp) - 1e-9:
            current_state = "target_reached"
            current_state_label = "已达到目标桶下沿"
        else:
            current_state = "below_target"
            current_state_label = "尚未达到目标桶"

    reach_pct = _to_float(realization_reliability.get("target_bucket_warming_realization_pct"))
    if reach_pct is None:
        reach_pct = _to_float(realization_reliability.get("warming_realization_pct"))
    blocked_pct = _to_float(realization_reliability.get("target_bucket_blocked_realization_pct"))
    if blocked_pct is None:
        blocked_pct = _to_float(realization_reliability.get("cooling_realization_pct"))
    neutral_pct = _to_float(realization_reliability.get("neutral_or_uncertain_pct"))
    peak_mu = _to_float(features.get("peak_estimator_mu_c"))
    has_peak_estimator_mu = peak_mu is not None
    peak_sigma = _to_float(features.get("peak_estimator_sigma_c"))
    rise_rate = _temperature_rise_rate_value(features)

    def _fmt_temp(value: Optional[float]) -> str:
        return f"{float(value):.2f}°C" if value is not None and math.isfinite(float(value)) else "—"

    def _fmt_pct(value: Optional[float]) -> str:
        return f"{float(value):.1f}%" if value is not None and math.isfinite(float(value)) else "—"

    def _relation_text(relation: str) -> str:
        if relation == "below_target":
            return "低于目标"
        if relation == "above_target":
            return "高于目标"
        if relation == "target":
            return "目标桶"
        return "未知"

    final_relation = "unknown"
    final_cmp = _compare_bucket_labels(final_entry.get("outcome"), target_label)
    if final_cmp is not None:
        if final_cmp < 0:
            final_relation = "below_target"
        elif final_cmp > 0:
            final_relation = "above_target"
        else:
            final_relation = "target"
    if target_is_lower_boundary:
        relation_label_below = "低于低边界"
        relation_label_target = "守住低边界"
        relation_label_above = "突破低边界"
        relation_meaning_below = f"最终低于 {target_label}（低温侧）"
        relation_meaning_target = f"最终守在 {target_label}"
        relation_meaning_above = f"最终超过 {target_label}"
    else:
        relation_label_below = "低于目标"
        relation_label_target = "目标桶"
        relation_label_above = "高于目标"
        relation_meaning_below = f"最终达不到 {target_label}"
        relation_meaning_target = f"最终正好落在 {target_label}"
        relation_meaning_above = f"最终超过 {target_label}"
    relation_probs = [
        {
            "key": "below_target",
            "label": relation_label_below,
            "probability_pct": _pct(below_prob),
            "meaning": relation_meaning_below,
        },
        {
            "key": "target",
            "label": relation_label_target,
            "probability_pct": _pct(at_prob),
            "meaning": relation_meaning_target,
        },
        {
            "key": "above_target",
            "label": relation_label_above,
            "probability_pct": _pct(above_prob),
            "meaning": relation_meaning_above,
        },
    ]
    dominant_relation = max(
        relation_probs,
        key=lambda item: _to_float(item.get("probability_pct")) if _to_float(item.get("probability_pct")) is not None else -1.0,
    )
    dominant_key = str(dominant_relation.get("key") or "unknown")
    reach_state = "unknown"
    reach_label = "能否达到目标待确认"
    reach_line = "目标下沿是否能被碰到，等待温度轨迹刷新；该口径只作路径/执行参考，不改写最高温落桶后验。"
    if current_state == "above_target":
        reach_state = "already_exceeded"
        reach_label = "已突破低边界" if target_is_lower_boundary else "已超过目标下沿"
        reach_line = (
            f"实况最高已超过 {target_label} 上界；该低边界桶已失效。"
            if target_is_lower_boundary
            else f"实况最高已达到 {target_label} 并进入更高桶风险区。"
        )
    elif current_state == "target_reached":
        reach_state = "already_reached"
        reach_label = "仍在低边界内" if target_is_lower_boundary else "已达到目标下沿"
        reach_line = (
            f"实况最高仍不高于 {target_label} 上界；最终仍可能落在该低边界桶。"
            if target_is_lower_boundary
            else f"实况最高已经碰到 {target_label} 下沿；最终仍可能落在目标或更高桶。"
        )
    elif reach_pct is not None:
        if float(reach_pct) >= 70.0:
            reach_state = "likely"
            reach_label = "低边界较可能守住" if target_is_lower_boundary else "路径触达较顺"
        elif float(reach_pct) >= 45.0:
            reach_state = "possible"
            reach_label = "低边界仍有机会守住" if target_is_lower_boundary else "路径仍有机会"
        else:
            reach_state = "unlikely"
            reach_label = "低边界突破风险高" if target_is_lower_boundary else "路径触达偏弱"
        reach_line = (
            f"轨迹估计守住 {target_label} 上界概率 {_fmt_pct(reach_pct)}；仅作路径/执行参考，不改写最高温落桶后验。"
            if target_is_lower_boundary
            else f"轨迹估计达到 {target_label} 下沿概率 {_fmt_pct(reach_pct)}；仅作路径/执行参考，不改写最高温落桶后验。"
        )
    if target_is_lower_boundary:
        if dominant_key == "above_target":
            settlement_line = f"低边界突破风险占优：高于 {target_label} {_fmt_pct(_pct(above_prob))}，守住低边界 {_fmt_pct(_pct(at_prob))}，更低侧 {_fmt_pct(_pct(below_prob))}。"
        elif dominant_key == "target":
            settlement_line = f"未结算后验集中在低边界内：守住 {target_label} {_fmt_pct(_pct(at_prob))}，突破低边界 {_fmt_pct(_pct(above_prob))}，更低侧 {_fmt_pct(_pct(below_prob))}。"
        else:
            settlement_line = f"未结算后验偏低：更低侧 {_fmt_pct(_pct(below_prob))}，守住 {target_label} {_fmt_pct(_pct(at_prob))}，突破低边界 {_fmt_pct(_pct(above_prob))}。"
    elif dominant_key == "below_target":
        settlement_line = f"未结算后验偏低：低于目标 {_fmt_pct(_pct(below_prob))}，目标桶 {_fmt_pct(_pct(at_prob))}，高于目标 {_fmt_pct(_pct(above_prob))}。"
    elif dominant_key == "above_target":
        settlement_line = f"未结算后验偏高：高于目标 {_fmt_pct(_pct(above_prob))}，目标桶 {_fmt_pct(_pct(at_prob))}，低于目标 {_fmt_pct(_pct(below_prob))}。"
    else:
        settlement_line = f"未结算后验集中在目标桶：目标桶 {_fmt_pct(_pct(at_prob))}，低于目标 {_fmt_pct(_pct(below_prob))}，高于目标 {_fmt_pct(_pct(above_prob))}。"
    if final_relation == "target":
        primary_target_clause = f"目标桶同为 {target_label}。"
    else:
        primary_target_clause = f"当前目标桶是 {target_label}（当前后验最可能{_relation_text(final_relation)}）。"
    if hard_source_direct.get("available"):
        primary_conclusion = (
            f"硬源已触达 {hard_floor_label or hard_source_direct.get('bucket')} 下沿"
            f"（{hard_source_direct.get('source_label')} {_fmt_temp(_to_float(hard_source_direct.get('observed_max_c')))}），"
            f"约束后当前后验最可能 {final_entry.get('outcome')}；"
            f"{primary_target_clause}"
        )
    else:
        primary_conclusion = (
            f"当前后验最可能落在 {final_entry.get('outcome')}（后验 {_fmt_pct(final_entry.get('probability_pct'))}）；"
            f"{primary_target_clause}"
        )
    target_gap_answer = (
        (
            f"已突破 {float(gap_to_target):.2f}°C"
            if target_is_lower_boundary and gap_to_target is not None and gap_to_target > 0
            else ("低边界仍存活" if target_is_lower_boundary and gap_to_target is not None else None)
        )
        or (
            f"还差 {float(gap_to_target):.2f}°C"
            if gap_to_target is not None and gap_to_target > 0
            else ("已到目标下沿" if gap_to_target is not None else "缺口待确认")
        )
    )
    gap_text = (
        (
            f"已突破低边界 {float(gap_to_target):.2f}°C"
            if target_is_lower_boundary and gap_to_target is not None and gap_to_target > 0
            else ("低边界仍存活" if target_is_lower_boundary and gap_to_target is not None else None)
        )
        or (
            f"距目标下沿还差 {float(gap_to_target):.2f}°C"
            if gap_to_target is not None and gap_to_target > 0
            else ("已达到目标下沿" if gap_to_target is not None else "目标缺口待确认")
        )
    )
    peak_text = (
        f"轨迹估计 {_fmt_temp(peak_mu)} ± {_fmt_temp(peak_sigma)}"
        if peak_mu is not None
        else "轨迹估计待刷新"
    )
    if hard_source_direct.get("available"):
        if target_is_lower_boundary:
            settlement_line = (
                f"硬源下界 {hard_floor_label or hard_source_direct.get('bucket')} 已进入后验约束；约束后分布："
                f"守住低边界 {_fmt_pct(_pct(at_prob))}，突破低边界 {_fmt_pct(_pct(above_prob))}，更低侧 {_fmt_pct(_pct(below_prob))}。"
            )
        else:
            settlement_line = (
                f"硬源下界 {hard_floor_label or hard_source_direct.get('bucket')} 已进入后验约束；约束后分布："
                f"低于目标 {_fmt_pct(_pct(below_prob))}，目标桶 {_fmt_pct(_pct(at_prob))}，高于目标 {_fmt_pct(_pct(above_prob))}。"
            )
        summary = (
            f"当前已观测最高 {_fmt_temp(observed_max)}，目标 {target_label}：{gap_text}；"
            f"{peak_text}。硬源下界 {hard_floor_label or hard_source_direct.get('bucket')} "
            f"（{hard_source_direct.get('source_label')} {_fmt_temp(_to_float(hard_source_direct.get('observed_max_c')))}）；"
            f"约束前后验最高 {pre_constraint_posterior_final_entry.get('outcome')}，"
            f"约束后后验最高 {posterior_final_entry.get('outcome')}（{_fmt_pct(posterior_final_entry.get('probability_pct'))}）。"
        )
    else:
        if target_is_lower_boundary:
            summary = (
                f"当前已观测最高 {_fmt_temp(observed_max)}，目标 {target_label}：{gap_text}；"
                f"{peak_text}。当前后验最可能落在 {final_entry.get('outcome')}（{_fmt_pct(final_entry.get('probability_pct'))}）；"
                f"守住低边界 {_fmt_pct(_pct(at_prob))}，突破低边界 {_fmt_pct(_pct(above_prob))}，更低侧 {_fmt_pct(_pct(below_prob))}。"
            )
        else:
            summary = (
                f"当前已观测最高 {_fmt_temp(observed_max)}，目标 {target_label}：{gap_text}；"
                f"{peak_text}。当前后验最可能落在 {final_entry.get('outcome')}（{_fmt_pct(final_entry.get('probability_pct'))}）；"
                f"低于目标 {_fmt_pct(_pct(below_prob))}，目标桶 {_fmt_pct(_pct(at_prob))}，高于目标 {_fmt_pct(_pct(above_prob))}。"
            )
    if residual_prob > 0.0005:
        summary += f" 另有约 {_fmt_pct(_pct(residual_prob))} 为门控排除/残差质量，未强行归一化。"

    second_outlook: Dict[str, Any]
    if second_label:
        below_second_prob = 0.0
        at_second_prob = 0.0
        above_second_prob = 0.0
        unknown_second_prob = 0.0
        for entry in entries:
            cmp = _compare_bucket_labels(entry.get("outcome"), second_label)
            if cmp is None:
                unknown_second_prob += float(entry.get("probability") or 0.0)
            elif cmp < 0:
                below_second_prob += float(entry.get("probability") or 0.0)
            elif cmp > 0:
                above_second_prob += float(entry.get("probability") or 0.0)
            else:
                at_second_prob += float(entry.get("probability") or 0.0)

        second_cmp_to_target = _compare_bucket_labels(second_label, target_label)
        if second_cmp_to_target is not None and second_cmp_to_target > 0:
            second_relation = "above_target"
            second_relation_label = "高于目标候选"
            breakout_phrase = f"突破到 {second_label} 或更高"
            not_break_phrase = f"最终没有突破到 {second_label}"
        elif second_cmp_to_target is not None and second_cmp_to_target < 0:
            second_relation = "below_target"
            second_relation_label = "低于目标候选"
            breakout_phrase = f"回落到 {second_label} 或更低"
            not_break_phrase = f"最终守在 {target_label} 或更高"
        else:
            second_relation = "unknown"
            second_relation_label = "候选桶"
            breakout_phrase = f"最终进入 {second_label} 相关区间"
            not_break_phrase = f"最终不进入 {second_label}"

        candidate_prob_pct = second_entry.get("probability_pct") if second_entry else _pct(at_second_prob)
        second_or_higher_pct = _pct(second_or_higher_prob)
        if gap_to_second is None:
            second_state = "unknown"
            second_state_label = "等待第二目标缺口"
            second_gap_answer = "缺口待确认"
        elif float(gap_to_second) <= 0:
            second_state = "reached"
            second_state_label = "已触及第二目标下沿"
            second_gap_answer = "已到第二目标下沿"
        else:
            second_state = "unreached"
            second_state_label = "尚未触及第二目标"
            second_gap_answer = f"还差 {float(gap_to_second):.2f}°C"

        if second_relation == "above_target":
            candidate_line = (
                f"若最终超过 {target_label}，第一候选是 {second_label}；"
                f"{not_break_phrase} {_fmt_pct(_pct(below_second_prob))}，"
                f"最终正好 {second_label} {_fmt_pct(_pct(at_second_prob))}，"
                f"高于 {second_label} {_fmt_pct(_pct(above_second_prob))}。"
            )
            probability_detail = f"{breakout_phrase}合计 {_fmt_pct(second_or_higher_pct)}。"
        elif second_relation == "below_target":
            candidate_line = (
                f"若目标 {target_label} 失守，主要候选是 {second_label}；"
                f"低于 {second_label} {_fmt_pct(_pct(below_second_prob))}，"
                f"最终正好 {second_label} {_fmt_pct(_pct(at_second_prob))}，"
                f"{not_break_phrase} {_fmt_pct(_pct(at_prob + above_prob))}。"
            )
            probability_detail = f"{breakout_phrase}合计 {_fmt_pct(_pct(below_second_prob + at_second_prob))}。"
        else:
            candidate_line = (
                f"第二候选 {second_label} 的落桶后验 {_fmt_pct(candidate_prob_pct)}；"
                f"低于候选 {_fmt_pct(_pct(below_second_prob))}，高于候选 {_fmt_pct(_pct(above_second_prob))}。"
            )
            probability_detail = f"候选相关概率 {_fmt_pct(candidate_prob_pct)}。"

        hard_source_hits_second = bool(
            hard_source_direct.get("available")
            and str(hard_source_direct.get("bucket") or "") == str(second_label or "")
        )
        if hard_source_hits_second:
            candidate_line = (
                f"{hard_source_direct.get('source_label')} 今日最高 "
                f"{_fmt_temp(_to_float(hard_source_direct.get('observed_max_c')))} "
                f"已直达 {second_label}；后验分布仅作模型参考，"
                f"最终正好 {second_label} {_fmt_pct(_pct(at_second_prob))}，"
                f"高于 {second_label} {_fmt_pct(_pct(above_second_prob))}。"
            )
            probability_detail = (
                f"硬实况直达 {second_label}；模型后验参考为 "
                f"{_fmt_pct(candidate_prob_pct)}，{breakout_phrase}合计 {_fmt_pct(second_or_higher_pct)}。"
            )

        second_reach_line = (
            f"当前已观测最高 {_fmt_temp(observed_max)}，{second_state_label}：{second_gap_answer}；"
            f"{peak_text}。"
        )
        if hard_source_hits_second:
            second_primary_conclusion = (
                f"第二目标/候选桶 {second_label} 已被硬实况直达；不覆盖当前目标 {target_label}，"
                f"但硬源下单目标指向 {second_label}；{probability_detail}"
            )
        else:
            second_primary_conclusion = (
                f"第二目标/候选桶是 {second_label}，不覆盖当前目标 {target_label}；"
                f"最终落在 {second_label} 的后验 {_fmt_pct(candidate_prob_pct)}，"
                f"{probability_detail}"
            )
        second_outlook = {
            "available": True,
            "version": "second_target_outlook_v1",
            "definition": (
                "第二目标来自同一后验排序/资金计划，只回答目标桶旁边的候选与突破风险，"
                "不覆盖当前落桶后验目标桶。"
            ),
            "primary_conclusion": second_primary_conclusion,
            "candidate_line": candidate_line,
            "reach_line": second_reach_line,
            "target_bucket": target_label,
            "second_target_bucket": second_label,
            "candidate_bucket": second_label,
            "candidate_relation_to_target": second_relation,
            "candidate_relation_label": second_relation_label,
            "state": second_state,
            "state_label": second_state_label,
            "candidate_probability_pct": _round(candidate_prob_pct, 2),
            "second_or_higher_probability_pct": _round(second_or_higher_pct, 2),
            "hard_source_direct_bucket": hard_source_direct.get("bucket") if hard_source_direct.get("available") else None,
            "hard_source_direct_source_id": hard_source_direct.get("source_id") if hard_source_direct.get("available") else None,
            "hard_source_direct_hits_candidate": hard_source_hits_second,
            "below_second_target_probability_pct": _pct(below_second_prob),
            "second_target_final_probability_pct": _pct(at_second_prob),
            "above_second_target_probability_pct": _pct(above_second_prob),
            "unknown_second_target_probability_pct": _pct(unknown_second_prob) if unknown_second_prob > 0 else 0.0,
            "observed_today_max_c": _round(observed_max, 2),
            "observed_temp_c": _round(observed_temp, 2),
            "second_target_lower_edge_c": _round(second_temp, 2),
            "gap_to_second_target_lower_edge_c": _round(gap_to_second, 2),
            "peak_estimator_mu_c": _round(peak_mu, 2),
            "peak_estimator_sigma_c": _round(peak_sigma, 2),
            "temperature_rise_rate_c_per_hour": _round(rise_rate, 2),
            "answer_cards": [
                {
                    "key": "candidate_bucket",
                    "label": "候选是谁",
                    "answer": second_label,
                    "detail": f"{second_relation_label}；不覆盖目标 {target_label}。",
                    "tone": "warn",
                },
                {
                    "key": "breakout_probability",
                    "label": "突破/候选概率",
                    "answer": _fmt_pct(second_or_higher_pct if second_relation == "above_target" else candidate_prob_pct),
                    "detail": probability_detail,
                    "tone": "bad" if second_relation == "above_target" and second_or_higher_pct >= 50.0 else "warn",
                },
                {
                    "key": "candidate_gap",
                    "label": "当前离候选",
                    "answer": second_gap_answer,
                    "detail": f"当前最高 {_fmt_temp(observed_max)}；候选下沿 {_fmt_temp(second_temp)}；{peak_text}。",
                    "tone": "bad" if second_state == "reached" and second_relation == "above_target" else ("ok" if second_state == "unreached" else "warn"),
                },
            ],
            "probability_bands": [
                {
                    "key": "below_second_target",
                    "label": f"低于第二目标",
                    "probability_pct": _pct(below_second_prob),
                    "meaning": not_break_phrase,
                },
                {
                    "key": "second_target",
                    "label": f"第二目标",
                    "probability_pct": _pct(at_second_prob),
                    "meaning": f"最终正好落在 {second_label}",
                },
                {
                    "key": "above_second_target",
                    "label": f"高于第二目标",
                    "probability_pct": _pct(above_second_prob),
                    "meaning": f"最终超过 {second_label}",
                },
            ],
            "second_target_row": second_entry,
        }
    else:
        second_outlook = {
            "available": False,
            "version": "second_target_outlook_v1",
            "reason": "second_target_missing",
            "target_bucket": target_label,
        }

    final_card_detail = (
        (
            f"{hard_source_direct.get('source_label')} 今日最高 "
            f"{_fmt_temp(_to_float(hard_source_direct.get('observed_max_c')))}；"
            f"硬源下界 {hard_floor_label or hard_source_direct.get('bucket')}，"
            f"约束前后验最高 {pre_constraint_posterior_final_entry.get('outcome')}，"
            f"约束后后验最高 {posterior_final_entry.get('outcome')}（后验 {_fmt_pct(posterior_final_entry.get('probability_pct'))}）。"
        )
        if hard_source_direct.get("available")
        else f"后验 {_fmt_pct(final_entry.get('probability_pct'))}；相对目标：{_relation_text(final_relation)}。"
    )
    return {
        "available": True,
        "version": "target_bucket_outlook_v2",
        "definition": (
            "最终展望主口径按所有市场结算桶的后验概率排序；or below/or higher 累计边界桶与单温度桶同权参与最终/目标桶选择。"
            "有 AWC/WU 硬实况可落桶时，硬源只作为已触达下界约束进入后验：低于硬源下界的桶归零，剩余桶重新归一化后选择当前后验最可能结算桶。达到/守住目标边界用温度轨迹兑现可靠性，"
            "它只回答能否碰到目标温度门槛，不等同于最终结算在目标桶。"
        ),
        "primary_conclusion": primary_conclusion,
        "settlement_line": settlement_line,
        "reach_line": reach_line,
        "target_gap_answer": target_gap_answer,
        "final_relation_to_target": final_relation,
        "final_relation_label": _relation_text(final_relation),
        "dominant_relation": dominant_key,
        "dominant_relation_label": str(dominant_relation.get("label") or _relation_text(dominant_key)),
        "target_reach_state": reach_state,
        "target_reach_label": reach_label,
        "answer_cards": [
            {
                "key": "final_bucket",
                "label": "当前后验最可能",
                "answer": str(final_entry.get("outcome") or ""),
                "detail": final_card_detail,
                "tone": "ok" if final_relation == "target" else ("warn" if final_relation == "below_target" else "bad"),
            },
            {
                "key": "target_reach",
                "label": "能否守住低边界" if target_is_lower_boundary else "路径触达参考",
                "answer": reach_label,
                "detail": reach_line,
                "tone": "ok" if reach_state in {"already_reached", "likely"} else ("bad" if reach_state == "unlikely" else "warn"),
            },
            {
                "key": "target_gap",
                "label": "当前离低边界" if target_is_lower_boundary else "当前离目标",
                "answer": target_gap_answer,
                "detail": (
                    f"当前最高 {_fmt_temp(observed_max)}；低边界上界 {_fmt_temp(target_temp)}；{peak_text}。"
                    if target_is_lower_boundary
                    else f"当前最高 {_fmt_temp(observed_max)}；目标下沿 {_fmt_temp(target_temp)}；{peak_text}。"
                ),
                "tone": "ok" if gap_to_target is not None and gap_to_target <= 0 else "warn",
            },
        ],
        "probability_bands": relation_probs,
        "target_bucket": target_label,
        "input_target_bucket": input_target_label,
        "target_bucket_source": target_label_source,
        "second_target_bucket": second_label or None,
        "final_bucket": final_entry.get("outcome"),
        "final_bucket_source": final_bucket_source,
        "final_bucket_probability_pct": final_entry.get("probability_pct"),
        "final_bucket_posterior_probability_pct": final_entry.get("probability_pct"),
        "posterior_final_bucket": posterior_final_entry.get("outcome"),
        "posterior_final_bucket_probability_pct": posterior_final_entry.get("probability_pct"),
        "cumulative_posterior_final_bucket": cumulative_posterior_final_entry.get("outcome"),
        "cumulative_posterior_final_bucket_probability_pct": cumulative_posterior_final_entry.get("probability_pct"),
        "pre_constraint_posterior_final_bucket": pre_constraint_posterior_final_entry.get("outcome"),
        "pre_constraint_posterior_final_bucket_probability_pct": pre_constraint_posterior_final_entry.get("probability_pct"),
        "pre_constraint_cumulative_posterior_final_bucket": pre_constraint_cumulative_posterior_final_entry.get("outcome"),
        "pre_constraint_cumulative_posterior_final_bucket_probability_pct": pre_constraint_cumulative_posterior_final_entry.get("probability_pct"),
        "hard_source_direct": hard_source_direct if hard_source_direct.get("available") else {"available": False},
        "target_final_probability_pct": target_entry.get("probability_pct") if target_entry else _pct(at_prob),
        "below_target_probability_pct": _pct(below_prob),
        "above_target_probability_pct": _pct(above_prob),
        "second_or_higher_probability_pct": _pct(second_or_higher_prob) if second_label else None,
        "unknown_probability_pct": _pct(unknown_prob) if unknown_prob > 0 else 0.0,
        "residual_probability_pct": _pct(residual_prob) if residual_prob > 0 else 0.0,
        "probability_mass_total_pct": _pct(total_prob),
        "target_reach_lower_edge_probability_pct": _round(reach_pct, 2),
        "target_blocked_or_cooling_probability_pct": _round(blocked_pct, 2),
        "target_neutral_or_uncertain_probability_pct": _round(neutral_pct, 2),
        "current_state": current_state,
        "current_state_label": current_state_label,
        "observed_temp_c": _round(observed_temp, 2),
        "observed_today_max_c": _round(observed_max, 2),
        "target_lower_edge_c": _round(target_temp, 2),
        "second_target_lower_edge_c": _round(second_temp, 2),
        "gap_to_target_lower_edge_c": _round(gap_to_target, 2),
        "gap_to_second_target_lower_edge_c": _round(gap_to_second, 2),
        "peak_estimator_mu_c": _round(peak_mu, 2),
        "peak_estimator_sigma_c": _round(peak_sigma, 2),
        "temperature_rise_rate_c_per_hour": _round(rise_rate, 2),
        "reach_probability_semantics": "probability_current_trajectory_reaches_target_lower_edge_not_final_settlement_bucket",
        "final_probability_semantics": "hard_source_constrained_posterior_probability_final_settlement_bucket",
        "summary": summary,
        "rows": entries[:8],
        "second_target_row": second_entry,
        "second_target_outlook": second_outlook,
    }


def _sync_target_bucket_outlook_with_temperature_path_distribution(
    target_bucket_outlook: MutableMapping[str, Any],
    temperature_path_distribution: Mapping[str, Any],
) -> MutableMapping[str, Any]:
    if not isinstance(target_bucket_outlook, MutableMapping):
        return target_bucket_outlook
    if not isinstance(temperature_path_distribution, Mapping) or not temperature_path_distribution.get("available"):
        return target_bucket_outlook
    target_leg = temperature_path_distribution.get("target")
    if not isinstance(target_leg, Mapping):
        target_leg = {}
    target_label = str(
        target_leg.get("bucket")
        or temperature_path_distribution.get("target_bucket")
        or target_bucket_outlook.get("target_bucket")
        or ""
    ).strip()
    path_pct = _to_float(
        target_leg.get("path_probability_pct")
        if target_leg.get("path_probability_pct") is not None
        else temperature_path_distribution.get("target_bucket_path_probability_pct")
    )
    path_lcb_pct = _to_float(
        target_leg.get("path_probability_lcb_pct")
        if target_leg.get("path_probability_lcb_pct") is not None
        else temperature_path_distribution.get("target_bucket_path_probability_lcb_pct")
    )
    path_na_reason = str(
        target_leg.get("path_probability_na_reason")
        or temperature_path_distribution.get("target_bucket_path_probability_na_reason")
        or ""
    ).strip()
    path_source = str(target_leg.get("path_probability_source") or "").strip()
    path_mode = str(target_leg.get("path_probability_mode") or "").strip()
    if path_pct is None and not path_na_reason:
        return target_bucket_outlook

    target_is_lower_boundary = _is_lower_boundary_bucket(target_label)

    def _fmt_pct_short(value: Optional[float]) -> str:
        number = _to_float(value)
        return f"{float(number):.1f}%" if number is not None else "—"

    if path_pct is None:
        reach_state = "unknown"
        reach_label = "低边界守住待确认" if target_is_lower_boundary else "路径触达待确认"
        probability_text = f"缺失（{path_na_reason or 'target_path_probability_missing'}）"
    elif float(path_pct) >= 70.0:
        reach_state = "likely"
        reach_label = "低边界较可能守住" if target_is_lower_boundary else "路径触达较顺"
        probability_text = _fmt_pct_short(path_pct)
    elif float(path_pct) >= 45.0:
        reach_state = "possible"
        reach_label = "低边界仍有机会守住" if target_is_lower_boundary else "路径仍有机会"
        probability_text = _fmt_pct_short(path_pct)
    else:
        reach_state = "unlikely"
        reach_label = "低边界突破风险高" if target_is_lower_boundary else "路径触达偏弱"
        probability_text = _fmt_pct_short(path_pct)

    lcb_text = ""
    if path_lcb_pct is not None:
        lcb_text = f"（LCB {_fmt_pct_short(path_lcb_pct)}）"
    if target_is_lower_boundary:
        reach_line = (
            f"轨迹估计守住 {target_label} 上界概率 {probability_text}{lcb_text}；"
            "仅作路径/执行参考，不改写最高温落桶后验。"
        )
    else:
        reach_line = (
            f"轨迹估计达到 {target_label} 下沿概率 {probability_text}{lcb_text}；"
            "仅作路径/执行参考，不改写最高温落桶后验。"
        )

    target_bucket_outlook["target_reach_state"] = reach_state
    target_bucket_outlook["target_reach_label"] = reach_label
    target_bucket_outlook["reach_line"] = reach_line
    target_bucket_outlook["target_reach_lower_edge_probability_pct"] = _round(path_pct, 2)
    target_bucket_outlook["target_path_probability_lcb_pct"] = _round(path_lcb_pct, 2)
    target_bucket_outlook["target_path_probability_source"] = path_source or None
    target_bucket_outlook["target_path_probability_mode"] = path_mode or None
    target_bucket_outlook["target_path_probability_na_reason"] = path_na_reason or None
    target_bucket_outlook["target_reach_probability_source"] = (
        "temperature_path_distribution.target.path_probability_pct"
    )
    target_bucket_outlook["target_reach_probability_semantics"] = (
        "canonical_temperature_path_probability_not_final_settlement_bucket"
    )
    trade_follow = temperature_path_distribution.get("trade_follow")
    if isinstance(trade_follow, Mapping):
        target_bucket_outlook["temperature_path_trade_follow_allowed"] = bool(trade_follow.get("allowed"))
        target_bucket_outlook["temperature_path_trade_follow_reason"] = str(trade_follow.get("reason") or "")

    cards = target_bucket_outlook.get("answer_cards")
    if isinstance(cards, list):
        next_cards: List[Any] = []
        for card in cards:
            if not isinstance(card, Mapping):
                next_cards.append(card)
                continue
            next_card = dict(card)
            if str(next_card.get("key") or "") == "target_reach":
                next_card["answer"] = reach_label
                next_card["detail"] = reach_line
                next_card["tone"] = (
                    "ok"
                    if reach_state in {"already_reached", "likely"}
                    else ("bad" if reach_state == "unlikely" else "warn")
                )
            next_cards.append(next_card)
        target_bucket_outlook["answer_cards"] = next_cards
    return target_bucket_outlook


def _refresh_cadence(
    payload: Mapping[str, Any],
    features: Mapping[str, Any],
    event_date: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    peak_hour, peak_meta = _expected_peak_hour_from_features(features, event_date=event_date)
    current_dt = now_utc or datetime.now(timezone.utc)
    current_local = current_dt.astimezone(CHENGDU_TZ)
    event_date_key = str(event_date or features.get("metar_target_event_date") or "").strip()
    is_d0 = bool(event_date_key and event_date_key == current_local.date().isoformat())
    cadence_dt = current_dt if is_d0 else _payload_evidence_dt(payload)
    cadence_basis = "current_local_time" if is_d0 else "snapshot_time"
    if cadence_dt is None:
        cadence_dt = _parse_datetime(features.get("zuuu_metar_raw_awc_latest_at")) or _parse_datetime(
            features.get("temperature_momentum_latest_observation_at")
        )
        cadence_basis = "latest_observation_time"
    hour = None
    if cadence_dt is not None:
        hour = _local_hour_fraction(cadence_dt)
    if hour is None:
        hour = _to_float(features.get("observation_hour_local"))
        cadence_basis = "observation_hour_local"
    observation_dt = _parse_datetime(features.get("zuuu_metar_raw_awc_latest_at")) or _parse_datetime(
        features.get("temperature_momentum_latest_observation_at")
    )
    observation_age_minutes = None
    if observation_dt is not None:
        observation_age_minutes = max(0.0, (current_dt - observation_dt).total_seconds() / 60.0)
    source_stale = bool(features.get("metar_stale")) or bool(
        observation_age_minutes is not None and observation_age_minutes > 90.0
    )
    if hour is None:
        return {
            "recommended_interval_sec": 900,
            "label": "15min",
            "reason": "缺少本地观测小时，使用保守刷新频率。",
            "hours_to_peak_window": None,
            "peak_hour_local": _round(peak_hour, 2),
            "peak_hour_source": str(peak_meta.get("expected_peak_hour_source") or ""),
            "peak_at_local": peak_meta.get("expected_peak_at_local"),
            "cadence_basis": cadence_basis,
            "event_date": event_date_key or None,
            "time_precision": "seconds",
            "reference_time_local": _fmt_local_seconds(cadence_dt),
            "reference_time_utc": _fmt_utc_seconds(cadence_dt),
            "latest_observation_time_local": _fmt_local_seconds(observation_dt),
            "latest_observation_time_utc": _fmt_utc_seconds(observation_dt),
            "observation_age_minutes": _round(observation_age_minutes, 1),
            "source_stale": source_stale,
        }
    hours_to_peak = float(peak_hour) - float(hour)
    if hours_to_peak > 4.0:
        interval = 900
        reason = "距离最高温窗口较远，15 分钟刷新足够。"
    elif hours_to_peak > 2.0:
        interval = 600
        reason = "进入午后升温阶段，刷新加密到 10 分钟。"
    elif hours_to_peak > 1.0:
        interval = 300
        reason = "距离最高温窗口小于 2 小时，刷新加密到 5 分钟。"
    elif hours_to_peak > 0.5:
        interval = 120
        reason = "距离最高温窗口小于 1 小时，刷新加密到 2 分钟。"
    elif hours_to_peak > -0.5:
        interval = 1
        reason = "处于最高温窗口核心段，刷新加密到 1 秒级检查。"
    elif hours_to_peak > -1.0:
        interval = 60
        reason = "最高温窗口刚过，刷新加密到 1 分钟确认已观测最高温。"
    else:
        interval = 300
        reason = "最高温窗口后仍需确认已观测最高温，维持 5 分钟刷新。"
    if is_d0 and source_stale:
        stale_cap = 30 if abs(hours_to_peak) <= 0.5 else (120 if hours_to_peak <= 1.5 and hours_to_peak > -1.0 else 300)
        interval = min(interval, stale_cap)
        reason = f"{reason} 硬实况源超过阈值未更新，按上游未更新状态高频复查。"
    return {
        "recommended_interval_sec": interval,
        "label": _format_interval_label_sec(interval),
        "reason": reason,
        "hours_to_peak_window": _round(hours_to_peak, 2),
        "observation_hour_local": _round(hour, 2),
        "cadence_basis": cadence_basis,
        "event_date": event_date_key or None,
        "time_precision": "seconds",
        "reference_time_local": _fmt_local_seconds(current_dt if is_d0 else cadence_dt),
        "reference_time_utc": _fmt_utc_seconds(current_dt if is_d0 else cadence_dt),
        "latest_observation_time_local": _fmt_local_seconds(observation_dt),
        "latest_observation_time_utc": _fmt_utc_seconds(observation_dt),
        "observation_age_minutes": _round(observation_age_minutes, 1),
        "source_stale": source_stale,
        "peak_hour_local": _round(peak_hour, 2),
        "peak_hour_source": str(peak_meta.get("expected_peak_hour_source") or ""),
        "peak_at_local": peak_meta.get("expected_peak_at_local"),
    }


def _top_rows(rows: Sequence[Mapping[str, Any]], single_only: bool = False, limit: int = 3) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for raw in rows:
        label = str(raw.get("outcome") or raw.get("label") or raw.get("target_bucket") or raw.get("bucket") or "")
        if single_only and (raw.get("is_cumulative_bucket") or not _is_single_temperature_bucket(label)):
            continue
        q_target, q_field = _target_bucket_probability(raw)
        if q_target is None:
            continue
        row = dict(raw)
        row["target_bucket_probability"] = q_target
        row["target_bucket_probability_pct"] = _pct(q_target)
        row["target_bucket_probability_field"] = q_field
        candidates.append(row)
    candidates.sort(key=lambda row: float(row.get("target_bucket_probability") or -1.0), reverse=True)
    return candidates[:limit]


def _target_bucket_probability(row: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    q_temperature = _to_float(row.get("q_temperature_bucket_posterior"))
    if q_temperature is not None:
        return q_temperature, "q_temperature_bucket_posterior"
    q_bayes = _to_float(row.get("q_bayes_posterior"))
    if q_bayes is not None:
        return q_bayes, "q_bayes_posterior"
    q_calibrated = _to_float(row.get("q_calibrated"))
    if q_calibrated is not None:
        return q_calibrated, "q_calibrated_legacy_fallback"
    target_probability = _to_float(row.get("target_bucket_probability"))
    if target_probability is not None:
        return target_probability, str(row.get("target_bucket_probability_field") or "target_bucket_probability")
    return None, ""


def _price_probability(value: Any) -> Optional[float]:
    num = _to_float(value)
    if num is None:
        return None
    if 1.0 < num <= 100.0:
        return num / 100.0
    return num


def _first_float(row: Mapping[str, Any], keys: Sequence[str]) -> Optional[float]:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def _market_tradeability(row: Mapping[str, Any]) -> Dict[str, Any]:
    label = str(row.get("outcome") or row.get("label") or "").strip()
    source = str(row.get("display_probability_source") or "").strip()
    dead = _to_bool(row.get("display_probability_market_dead")) or source == "dead_yes_market_zero"
    invalid = _to_bool(row.get("display_probability_market_invalid")) or source == "invalid_yes_market_quote"
    bid = _price_probability(row.get("best_bid"))
    ask = _price_probability(row.get("best_ask"))
    no_bid = _price_probability(row.get("best_bid_no"))
    no_ask = _price_probability(row.get("best_ask_no"))
    spread = ask - bid if bid is not None and ask is not None else None
    outcome_volume = _first_float(row, ("outcome_volume", "volume_24h", "volume_total"))
    liquidity = _to_float(row.get("liquidity"))
    reasons: List[str] = []
    state = "tradable"
    tradable = True
    observable = True

    if invalid:
        state = "invalid_quote"
        tradable = False
        reasons.append("盘口报价无效")
    elif dead:
        state = "market_dead_observe_only"
        tradable = False
        reasons.append("YES盘口死亡/归零")
    elif bid is None and ask is None:
        state = "missing_quote"
        tradable = False
        reasons.append("缺少YES bid/ask")
    elif bid is None or ask is None:
        state = "one_sided_quote"
        tradable = False
        reasons.append("YES盘口单边报价")
    elif spread is not None and spread > 0.20:
        state = "wide_spread"
        tradable = False
        reasons.append(f"价差过宽 {spread * 100:.1f}pp")
    elif outcome_volume is not None and outcome_volume <= 0 and liquidity is not None and liquidity <= 0:
        state = "thin_book"
        tradable = False
        reasons.append("成交量/流动性为0")
    elif outcome_volume is None and liquidity is None:
        state = "tradable_quote_volume_unknown"
        reasons.append("有盘口但成交量/流动性未知")
    else:
        reasons.append("盘口可执行")

    if not label:
        observable = False
        reasons.append("缺少温度桶标签")

    if not tradable and _is_cumulative_bucket(label):
        actionability_state = "risk_monitor_only"
    elif tradable:
        actionability_state = "tradeable"
    else:
        actionability_state = "observe_only"

    return {
        "version": "target_market_tradeability_v1",
        "outcome": label or None,
        "state": state,
        "actionability_state": actionability_state,
        "tradable": tradable,
        "observable": observable,
        "is_dead_market": dead,
        "is_invalid_quote": invalid,
        "best_bid": _round(bid, 4),
        "best_ask": _round(ask, 4),
        "best_bid_pct": _pct(bid),
        "best_ask_pct": _pct(ask),
        "best_bid_no": _round(no_bid, 4),
        "best_ask_no": _round(no_ask, 4),
        "spread": _round(spread, 4),
        "spread_pct": _pct(spread),
        "outcome_volume": _round(outcome_volume, 2),
        "liquidity": _round(liquidity, 2),
        "source": source or None,
        "reasons": reasons,
    }


def _actionable_target_candidate(
    candidates: Sequence[Mapping[str, Any]],
    excluded_label: str,
) -> Optional[Dict[str, Any]]:
    for candidate in candidates:
        label = str(candidate.get("outcome") or "").strip()
        if not label or label == excluded_label:
            continue
        if _is_cumulative_bucket(label):
            continue
        tradeability = _market_tradeability(candidate)
        if tradeability.get("tradable"):
            row = dict(candidate)
            row["market_tradeability"] = tradeability
            return row
    return None


def _d0_intraday_switch_window(
    features: Mapping[str, Any],
    *,
    event_date: Any,
) -> Dict[str, Any]:
    evidence_dt = (
        _parse_datetime(features.get("weather_likelihood_refreshed_at"))
        or _parse_datetime(features.get("zuuu_metar_raw_awc_latest_at"))
        or _parse_datetime(features.get("wu_twc_observed_intraday_today_max_at"))
        or _parse_datetime(features.get("wu_twc_observed_intraday_current_at"))
    )
    event_key = str(event_date or features.get("metar_target_event_date") or "").split("T", 1)[0]
    if evidence_dt is None:
        return {
            "active": False,
            "reason": "missing_reference_time",
            "event_date": event_key or None,
        }
    local_dt = evidence_dt.astimezone(CHENGDU_TZ)
    is_d0 = bool(event_key and event_key == local_dt.date().isoformat())
    peak_hour, peak_meta = _expected_peak_hour_from_features(features, event_date=event_key)
    local_hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
    hours_to_peak = float(peak_hour) - float(local_hour)
    active = bool(is_d0 and hours_to_peak <= 6.5 and hours_to_peak >= -1.5)
    if not is_d0:
        reason = "not_d0_local_event_date"
    elif active:
        reason = "d0_intraday_switch_window"
    else:
        reason = "outside_intraday_switch_window"
    return {
        "active": active,
        "reason": reason,
        "event_date": event_key or None,
        "reference_time_utc": _fmt_utc_seconds(evidence_dt),
        "reference_time_local": _fmt_local_seconds(evidence_dt),
        "local_hour": _round(local_hour, 2),
        "peak_hour_local": _round(peak_hour, 2),
        "peak_hour_source": str(peak_meta.get("expected_peak_hour_source") or ""),
        "peak_at_local": peak_meta.get("expected_peak_at_local"),
        "hours_to_peak_window": _round(hours_to_peak, 2),
    }


def _hard_final_bucket_lock_reason(features: Mapping[str, Any]) -> str:
    demotion_reason = _settlement_actual_intraday_deformation_lock_reason(features)
    if demotion_reason:
        return ""
    if _to_float(features.get("settlement_actual")) is not None or _to_float(features.get("settlement_actual_c")) is not None:
        return "settlement_actual_known"
    for key in (
        "hard_source_final_daily_max",
        "temperature_observation_final_daily_max",
        "observed_today_max_is_final",
        "daily_max_observation_complete",
        "observed_today_max_final",
    ):
        if _to_bool(features.get(key)):
            return key
    return ""


def _nested_float(mapping: Mapping[str, Any], path: Sequence[str]) -> Optional[float]:
    cur: Any = mapping
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return _to_float(cur)


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None:
            return parsed
    return None


def _settlement_bucket_int(value: Any, rounding_rule: Any = "") -> Optional[int]:
    raw = _to_float(value)
    if raw is None:
        return None
    policy = str(rounding_rule or "whole_degrees").strip().lower()
    if "round" in policy and "floor" not in policy:
        return int(round(float(raw)))
    if "whole" in policy and "floor" not in policy:
        return int(math.floor(float(raw) + 0.5))
    return int(math.floor(float(raw)))


def _flatten_tokens(value: Any) -> List[str]:
    if isinstance(value, Mapping):
        out: List[str] = []
        for item in value.values():
            out.extend(_flatten_tokens(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_flatten_tokens(item))
        return out
    text = str(value or "").strip()
    return [text.lower()] if text else []


def _settlement_actual_intraday_deformation_lock_reason(features: Mapping[str, Any]) -> str:
    if not _to_bool(os.environ.get("PM_BAYES_BLOCK_DEFORMED_WU_INTRADAY_SETTLEMENT_LOCK", "1")):
        return ""
    settlement_value = _first_float(features.get("settlement_actual"), features.get("settlement_actual_c"))
    if settlement_value is None:
        return ""
    comparison = features.get("source_observation_comparison")
    if not isinstance(comparison, Mapping):
        return ""
    alert = features.get("weather_source_delay_deformation_alert")
    alert_map = alert if isinstance(alert, Mapping) else {}
    alert_text = " ".join(_flatten_tokens(alert_map))
    alert_on = bool(
        _to_bool(alert_map.get("alert"))
        and (
            "wu" in alert_text
            or "twc" in alert_text
            or "temperature_deformation" in alert_text
            or "temperature_mismatch" in alert_text
            or "delay" in alert_text
        )
    )
    if not alert_on:
        return ""
    rounding_rule = features.get("rounding_rule") or "whole_degrees"
    settlement_bucket = _settlement_bucket_int(settlement_value, rounding_rule)
    awc = comparison.get("awc") if isinstance(comparison.get("awc"), Mapping) else {}
    wu_twc = comparison.get("wu_twc") if isinstance(comparison.get("wu_twc"), Mapping) else {}
    awc_value = _nested_float(awc, ("raw_observations", "today_max_metar", "temperature_raw_value"))
    if awc_value is None:
        awc_value = _first_float(awc.get("today_max_c"), awc.get("bucket_basis_value_c"))
    awc_bucket = _settlement_bucket_int(awc_value, rounding_rule)
    wu_current_value = _nested_float(wu_twc, ("raw_observations", "current_metric", "max_since7am_raw_value"))
    if wu_current_value is None:
        wu_current_value = _first_float(wu_twc.get("today_max_c"), wu_twc.get("bucket_basis_value_c"))
    wu_current_bucket = _settlement_bucket_int(wu_current_value, rounding_rule)
    wu_history_value = _nested_float(wu_twc, ("raw_observations", "history_metric", "raw_value"))
    wu_history_bucket = _settlement_bucket_int(wu_history_value, rounding_rule)
    if (
        settlement_bucket is None
        or awc_bucket is None
        or wu_current_bucket is None
        or int(settlement_bucket) == int(awc_bucket)
    ):
        return ""
    if int(settlement_bucket) != int(wu_current_bucket):
        return ""
    if wu_history_bucket is not None and int(wu_history_bucket) != int(awc_bucket):
        return ""
    return "wu_twc_intraday_deformation_not_final_settlement"


def _switch_pressure_candidate(
    *,
    rows: Sequence[Mapping[str, Any]],
    top_single: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
    target_label: str,
    observed_label: str,
    observed_soft: bool,
    momentum_supports_warming: bool,
    momentum_gate: Optional[Mapping[str, Any]] = None,
    features: Mapping[str, Any],
    event_date: Any,
    rounding_rule: str = "",
    market_unit: str = "C",
) -> Optional[Dict[str, Any]]:
    if not target_label:
        return None
    final_lock_reason = _hard_final_bucket_lock_reason(features)
    if final_lock_reason:
        return None
    window = _d0_intraday_switch_window(features, event_date=event_date)
    if not window.get("active"):
        return None

    target_row = dict(target or _bucket_row_by_label(rows, target_label))
    target_prob, target_prob_field = _target_bucket_probability(target_row)

    candidate_by_label: Dict[str, Dict[str, Any]] = {}
    for raw in [*[dict(item) for item in top_single], *[dict(item) for item in rows]]:
        label = str(raw.get("outcome") or raw.get("label") or "").strip()
        if not label or label == target_label or _is_cumulative_bucket(label):
            continue
        if label in candidate_by_label:
            continue
        if _compare_bucket_labels(label, target_label) != 1:
            continue
        row = dict(raw)
        prob, prob_field = _target_bucket_probability(row)
        row["target_bucket_probability"] = prob
        row["target_bucket_probability_field"] = prob_field
        candidate_by_label[label] = row
    if not candidate_by_label:
        return None

    observed_current = _to_float(
        features.get("observed_temp_c")
        if features.get("observed_temp_c") is not None
        else features.get("metar_temperature_c")
    )
    observed_max, _observed_max_source, _observed_max_na_reason = _hard_observed_today_max_c(features)
    observed_path_temp, observed_path_source = _observed_path_temperature_c(observed_current, observed_max)
    momentum_meta = momentum_gate if isinstance(momentum_gate, Mapping) else {}
    actual_rate = _to_float(momentum_meta.get("effective_rate_c_per_hour"))
    actual_rate_source = "temperature_momentum_effective_c_per_hour"
    if actual_rate is None:
        actual_rate = _temperature_rise_rate_value(features)
        actual_rate_source = "temperature_rise_rate_c_per_hour"
    hours_to_peak = _to_float(window.get("hours_to_peak_window"))
    projected_peak_c = None
    if observed_path_temp is not None and actual_rate is not None and hours_to_peak is not None:
        projected_peak_c = float(observed_path_temp) + max(0.0, float(actual_rate)) * max(0.0, float(hours_to_peak))

    projected_bucket = ""
    if projected_peak_c is not None:
        projected_bucket = _display_integer_bucket_label(rows, projected_peak_c, market_unit)
        if not projected_bucket:
            projected_bucket = _bucket_label_for_observed_value(rows, projected_peak_c, rounding_rule, market_unit)
        if (
            not projected_bucket
            or projected_bucket == target_label
            or _is_cumulative_bucket(projected_bucket)
            or _compare_bucket_labels(projected_bucket, target_label) != 1
        ):
            projected_bucket = ""

    evaluated: List[Dict[str, Any]] = []
    for candidate in candidate_by_label.values():
        candidate_label = str(candidate.get("outcome") or "")
        candidate_prob = _to_float(candidate.get("target_bucket_probability"))
        prob_lead = None
        if candidate_prob is not None and target_prob is not None:
            prob_lead = float(candidate_prob) - float(target_prob)
        posterior_lag = -float(prob_lead) if prob_lead is not None else None
        soft_observed_confirmed = bool(
            observed_soft
            and observed_label
            and candidate_label == observed_label
        )
        trajectory_bucket_match = bool(projected_bucket and candidate_label == projected_bucket)
        path = (
            _temperature_path_probability_for_bucket(
                candidate_label,
                observed_max_c=observed_max,
                peak_mu_c=projected_peak_c,
                peak_sigma_c=0.85,
                peak_mu_source="temperature_momentum_projected_peak",
            )
            if projected_peak_c is not None
            else {"available": False, "reason": "projected_peak_missing"}
        )
        path_probability = _to_float(path.get("probability"))
        trajectory_confirmed = bool(path_probability is not None and float(path_probability) >= 0.55)
        posterior_confirmed = bool(
            posterior_lag is not None
            and posterior_lag <= 0.06
            and candidate_prob is not None
            and candidate_prob >= 0.24
        )
        pressure = bool(
            momentum_supports_warming
            and (soft_observed_confirmed or trajectory_confirmed or trajectory_bucket_match)
            and (
                posterior_confirmed
                or (
                    candidate_prob is not None
                    and candidate_prob >= 0.18
                    and path_probability is not None
                    and float(path_probability) >= 0.55
                )
            )
        )
        evaluated.append(
            {
                "row": dict(candidate),
                "label": candidate_label,
                "candidate_probability": candidate_prob,
                "candidate_probability_field": candidate.get("target_bucket_probability_field"),
                "probability_lead": prob_lead,
                "posterior_lag": posterior_lag,
                "soft_observed_confirmed": soft_observed_confirmed,
                "posterior_confirmed": posterior_confirmed,
                "trajectory_confirmed": trajectory_confirmed,
                "trajectory_bucket_match": trajectory_bucket_match,
                "path": path,
                "path_probability": path_probability,
                "pressure": pressure,
            }
        )
    pressure_candidates = [item for item in evaluated if item.get("pressure")]
    if not pressure_candidates:
        return None
    pressure_candidates.sort(
        key=lambda item: (
            bool(item.get("trajectory_bucket_match")),
            bool(item.get("soft_observed_confirmed")),
            bool(item.get("trajectory_confirmed")),
            float(item.get("path_probability") if item.get("path_probability") is not None else -1.0),
            float(item.get("candidate_probability") if item.get("candidate_probability") is not None else -1.0),
            -abs((_bucket_temp(item.get("label")) or 0.0) - float(projected_peak_c or 0.0)),
        ),
        reverse=True,
    )
    selected = pressure_candidates[0]
    candidate = dict(selected["row"])
    candidate_label = str(selected.get("label") or "")
    candidate_prob = _to_float(selected.get("candidate_probability"))
    prob_lead = _to_float(selected.get("probability_lead"))
    posterior_lag = _to_float(selected.get("posterior_lag"))
    soft_observed_confirmed = bool(selected.get("soft_observed_confirmed"))
    posterior_confirmed = bool(selected.get("posterior_confirmed"))
    trajectory_confirmed = bool(selected.get("trajectory_confirmed"))
    trajectory_bucket_match = bool(selected.get("trajectory_bucket_match"))
    path = selected.get("path") if isinstance(selected.get("path"), Mapping) else {}
    path_probability = _to_float(selected.get("path_probability"))

    if soft_observed_confirmed:
        trigger = "soft_observed_touched"
    elif trajectory_bucket_match:
        trigger = "temperature_momentum_projected_bucket_reaches_candidate"
    elif trajectory_confirmed:
        trigger = "temperature_momentum_projected_peak_reaches_candidate"
    else:
        trigger = "posterior_trajectory_candidate_leads"
    out = dict(candidate)
    out["early_switch_context"] = {
        "trigger": trigger,
        "intraday_window": window,
        "held_target_bucket": target_label,
        "candidate_bucket": candidate_label,
        "candidate_probability": _round(candidate_prob, 4),
        "candidate_probability_pct": _pct(candidate_prob),
        "candidate_probability_field": candidate.get("target_bucket_probability_field"),
        "held_target_probability": _round(target_prob, 4),
        "held_target_probability_pct": _pct(target_prob),
        "held_target_probability_field": target_prob_field or None,
        "probability_lead": _round(prob_lead, 4),
        "probability_lag": _round(posterior_lag, 4),
        "probability_lag_max": 0.06,
        "probability_lead_pct": _pct(prob_lead),
        "soft_observed_confirmed": soft_observed_confirmed,
        "posterior_confirmed": posterior_confirmed,
        "trajectory_confirmed": trajectory_confirmed,
        "trajectory_bucket_match": trajectory_bucket_match,
        "trajectory_projected_bucket": projected_bucket or None,
        "trajectory_path_probability": _round(path_probability, 4),
        "trajectory_path_probability_pct": _pct(path_probability),
        "trajectory_path_probability_source": path.get("source"),
        "trajectory_projected_peak_c": _round(projected_peak_c, 2),
        "trajectory_projected_peak_source": actual_rate_source,
        "trajectory_observed_path_temp_c": _round(observed_path_temp, 2),
        "trajectory_observed_path_temp_source": observed_path_source,
        "trajectory_actual_rate_c_per_hour": _round(actual_rate, 2),
        "trajectory_hours_to_peak_window": _round(hours_to_peak, 2),
        "momentum_supports_warming": momentum_supports_warming,
        "momentum_gate": dict(momentum_meta) if momentum_meta else None,
        "evaluated_candidate_count": len(evaluated),
        "selection_rule": "temperature_trajectory_projected_bucket_first_then_path_probability_then_posterior_floor",
        "decision_inputs": "posterior_probability_and_temperature_trajectory_projected_bucket",
    }
    return out


def _forecast_path_breakout_candidate(
    *,
    rows: Sequence[Mapping[str, Any]],
    top_single: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
    target_label: str,
    features: Mapping[str, Any],
    event_date: Any,
    rounding_rule: str = "",
    market_unit: str = "C",
) -> Optional[Dict[str, Any]]:
    if not target_label or not _is_lower_boundary_bucket(target_label):
        return None
    if _hard_final_bucket_lock_reason(features):
        return None
    window = _d0_intraday_switch_window(features, event_date=event_date)
    hours_to_peak = _to_float(window.get("hours_to_peak_window"))
    if window.get("reason") == "not_d0_local_event_date" or hours_to_peak is None:
        return None
    # This path is for the early D0 forecast/trajectory target. Once the live
    # peak window opens, the stricter multi-window momentum switch owns target
    # changes to avoid short-term noise.
    if bool(window.get("active")) or float(hours_to_peak) <= 0:
        return None
    observed_max, _observed_max_source, _observed_max_na_reason = _hard_observed_today_max_c(features)
    if observed_max is None:
        return None
    peak_mu, peak_source = _soft_forecast_peak(features)
    if peak_mu is None:
        peak_mu = _to_float(features.get("peak_estimator_mu_c"))
        peak_source = "peak_estimator_mu_c" if peak_mu is not None else ""
    if peak_mu is None:
        return None
    peak_sigma = _to_float(features.get("peak_estimator_sigma_c"))
    if peak_sigma is None:
        peak_sigma = 0.85
    forecast_bucket = _display_integer_bucket_label(rows, peak_mu, market_unit)
    if not forecast_bucket:
        forecast_bucket = _bucket_label_for_observed_value(rows, peak_mu, rounding_rule, market_unit)

    target_path = _temperature_path_probability_for_bucket(
        target_label,
        observed_max_c=observed_max,
        peak_mu_c=peak_mu,
        peak_sigma_c=peak_sigma,
        peak_mu_source=peak_source or "soft_forecast_peak_c",
    )
    target_hold_prob = _to_float(target_path.get("probability"))
    if target_hold_prob is None or target_hold_prob > 0.35:
        return None

    target_row = dict(target or _bucket_row_by_label(rows, target_label))
    target_prob, target_prob_field = _target_bucket_probability(target_row)
    candidate_by_label: Dict[str, Dict[str, Any]] = {}
    for raw in [*[dict(item) for item in top_single], *[dict(item) for item in rows]]:
        label = str(raw.get("outcome") or raw.get("label") or "").strip()
        if not label or label == target_label or _is_cumulative_bucket(label):
            continue
        if label in candidate_by_label:
            continue
        if _compare_bucket_labels(label, target_label) != 1:
            continue
        row = dict(raw)
        prob, prob_field = _target_bucket_probability(row)
        if prob is None:
            continue
        path = _temperature_path_probability_for_bucket(
            label,
            observed_max_c=observed_max,
            peak_mu_c=peak_mu,
            peak_sigma_c=peak_sigma,
            peak_mu_source=peak_source or "soft_forecast_peak_c",
        )
        path_prob = _to_float(path.get("probability"))
        posterior_lag = float(target_prob) - float(prob) if target_prob is not None else None
        forecast_bucket_match = bool(forecast_bucket and label == forecast_bucket)
        min_path_probability = 0.40 if forecast_bucket_match else 0.55
        if (
            path_prob is None
            or float(path_prob) < min_path_probability
            or float(prob) < 0.15
            or (posterior_lag is not None and posterior_lag > 0.10)
        ):
            continue
        row["target_bucket_probability"] = prob
        row["target_bucket_probability_field"] = prob_field
        row["forecast_path_breakout_context"] = {
            "trigger": "early_d0_low_boundary_breakout_path",
            "held_target_bucket": target_label,
            "candidate_bucket": label,
            "candidate_probability": _round(prob, 4),
            "candidate_probability_pct": _pct(prob),
            "candidate_probability_field": prob_field,
            "held_target_probability": _round(target_prob, 4),
            "held_target_probability_pct": _pct(target_prob),
            "held_target_probability_field": target_prob_field or None,
            "posterior_lag": _round(posterior_lag, 4),
            "posterior_lag_max": 0.10,
            "target_hold_path_probability": _round(target_hold_prob, 4),
            "target_hold_path_probability_pct": _pct(target_hold_prob),
            "target_hold_path_probability_max": 0.35,
            "candidate_path_probability": _round(path_prob, 4),
            "candidate_path_probability_pct": _pct(path_prob),
            "candidate_path_probability_min": min_path_probability,
            "candidate_path_probability_source": path.get("source"),
            "forecast_peak_bucket": forecast_bucket or None,
            "forecast_peak_bucket_match": forecast_bucket_match,
            "forecast_peak_c": _round(peak_mu, 2),
            "forecast_peak_source": peak_source or "soft_forecast_peak_c",
            "forecast_peak_sigma_c": _round(peak_sigma, 2),
            "observed_today_max_c": _round(observed_max, 2),
            "intraday_window": window,
            "selection_rule": "early_d0_low_boundary_hold_low_and_single_bucket_path_high",
        }
        candidate_by_label[label] = row
    candidates = list(candidate_by_label.values())
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            bool((row.get("forecast_path_breakout_context") or {}).get("forecast_peak_bucket_match")),
            float(row.get("target_bucket_probability") or -1.0),
            float((row.get("forecast_path_breakout_context") or {}).get("candidate_path_probability") or -1.0),
            -abs((_bucket_temp(row.get("outcome")) or 0.0) - float(peak_mu)),
        ),
        reverse=True,
    )
    return candidates[0]


def _temperature_momentum_supports_warming(
    features: Mapping[str, Any],
    temperature_trend: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    trend = temperature_trend if isinstance(temperature_trend, Mapping) else {}
    momentum = (
        trend.get("temperature_momentum")
        if isinstance(trend.get("temperature_momentum"), Mapping)
        else {}
    )
    momentum_state = str(momentum.get("state") or "").strip()
    feature_state = str(features.get("temperature_momentum_realization_state") or "").strip()
    state = momentum_state if momentum_state and momentum_state != "missing" else feature_state
    effective = _to_float(momentum.get("effective_rate_c_per_hour"))
    if effective is None:
        effective = _to_float(features.get("temperature_momentum_effective_c_per_hour"))
    rise = _temperature_rise_rate_value(features)
    sustained = bool(momentum.get("sustained_warming")) or _to_bool(features.get("temperature_momentum_sustained_warming"))
    gate_allowed_raw = momentum.get("posterior_gate_allowed")
    if gate_allowed_raw is None:
        gate_allowed_raw = features.get("temperature_momentum_posterior_gate_allowed")
    gate_allowed = _to_bool(gate_allowed_raw) if gate_allowed_raw is not None else None
    rates = momentum.get("rates_c_per_hour") if isinstance(momentum.get("rates_c_per_hour"), Mapping) else {}
    if not rates:
        rates = {
            label: features.get(f"temperature_momentum_{label}_c_per_hour")
            for label in TEMPERATURE_MOMENTUM_WINDOWS
            if features.get(f"temperature_momentum_{label}_c_per_hour") is not None
        }
    gate = _temperature_momentum_posterior_gate(rates, state=state, effective=effective)
    hard_context_windows = [
        label
        for label in ("2h", "3h", "6h")
        if _to_float(rates.get(label)) is not None and float(rates[label]) >= 0.35
    ]
    if gate_allowed is not None:
        explicit_reason = str(features.get("temperature_momentum_posterior_gate_reason") or gate.get("reason") or "").strip()
        explicit_one_hour_only = explicit_reason in {
            "one_hour_hard_momentum_confirmed",
            "hard_observation_one_hour_momentum_confirmed",
        }
        gate["allowed"] = bool(gate_allowed)
        if gate_allowed and explicit_one_hour_only and not hard_context_windows:
            gate["allowed"] = False
            gate["state"] = "one_hour_watch"
            gate["reason"] = "one_hour_requires_2h_3h_context"
        elif gate_allowed:
            gate["state"] = "confirmed"
            gate["reason"] = explicit_reason or "explicit_gate_allowed"
    hard_one_hour_enabled = _to_bool(os.environ.get("PM_CHENGDU_HARD_OBS_ONE_HOUR_MOMENTUM_GATE_ENABLED", "1"))
    hard_one_hour_threshold = _to_float(
        os.environ.get("PM_CHENGDU_HARD_OBS_ONE_HOUR_MOMENTUM_GATE_MIN_C_PER_HOUR")
    )
    if hard_one_hour_threshold is None or not math.isfinite(float(hard_one_hour_threshold)):
        hard_one_hour_threshold = 0.75
    source_text = " ".join(
        str(features.get(key) or "")
        for key in (
            "observed_today_max_source",
            "observed_temp_source",
            "temperature_rise_rate_source",
            "temperature_trajectory_stable_rate_source",
        )
    ).lower()
    hard_observation_source = any(
        token in source_text
        for token in ("zuuu", "metar", "awc", "airport")
    )
    if (
        not bool(gate.get("allowed"))
        and hard_one_hour_enabled
        and hard_observation_source
        and str(state or "") in {"sustained_warming", "emerging_warming"}
        and effective is not None
        and float(effective) >= float(hard_one_hour_threshold)
        and rise is not None
        and float(rise) >= float(hard_one_hour_threshold)
        and hard_context_windows
    ):
        gate["allowed"] = True
        gate["state"] = "confirmed"
        gate["reason"] = "hard_observation_one_hour_context_confirmed"
        if not gate.get("confirmed_windows"):
            gate["confirmed_windows"] = ["1h"]
    supports = (
        bool(gate.get("allowed"))
        and (
            sustained
            or state in {"sustained_warming", "emerging_warming"}
            or (effective is not None and effective >= 0.50)
            or (rise is not None and rise >= 0.75)
        )
    )
    return supports, {
        "state": state or "missing",
        "supports_warming": supports,
        "effective_rate_c_per_hour": _round(effective, 2),
        "rise_rate_c_per_hour": _round(rise, 2),
        "sustained_warming": sustained,
        "posterior_gate_allowed": bool(gate.get("allowed")),
        "posterior_gate_state": str(gate.get("state") or ""),
        "posterior_gate_reason": str(gate.get("reason") or ""),
        "posterior_confirmed_windows": list(gate.get("confirmed_windows") or []),
    }


def _target_rows(rows: Sequence[Mapping[str, Any]], single_only: bool = False, limit: int = 3) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for raw in rows:
        label = str(raw.get("outcome") or raw.get("label") or raw.get("target_bucket") or raw.get("bucket") or "")
        if single_only and (raw.get("is_cumulative_bucket") or not _is_single_temperature_bucket(label)):
            continue
        q_target, q_field = _target_bucket_probability(raw)
        if q_target is None:
            continue
        row = dict(raw)
        row["target_bucket_probability"] = q_target
        row["target_bucket_probability_pct"] = _pct(q_target)
        row["target_bucket_probability_field"] = q_field
        candidates.append(row)
    candidates.sort(
        key=lambda row: (
            float(row.get("target_bucket_probability") or -1.0),
            -_bucket_sort_key(row)[0],
        ),
        reverse=True,
    )
    return candidates[:limit]


def _bucket_rank(label: Any) -> Optional[Tuple[int, float]]:
    text = str(label or "").strip().lower()
    temp = _bucket_temp(label)
    if temp is None:
        return None
    if "or below" in text or text.startswith("<=") or "below" in text:
        return (0, temp)
    if "or higher" in text or text.startswith(">=") or "higher" in text or "above" in text:
        return (2, temp)
    return (1, temp)


def _compare_bucket_labels(left: Any, right: Any) -> Optional[int]:
    left_rank = _bucket_rank(left)
    right_rank = _bucket_rank(right)
    if left_rank is None or right_rank is None:
        return None
    if left_rank[1] < right_rank[1]:
        return -1
    if left_rank[1] > right_rank[1]:
        return 1
    if left_rank[0] < right_rank[0]:
        return -1
    if left_rank[0] > right_rank[0]:
        return 1
    return 0


def _bucket_row_by_label(rows: Sequence[Mapping[str, Any]], label: Any) -> Dict[str, Any]:
    target = str(label or "").strip()
    if not target:
        return {}
    for row in rows or []:
        if str(row.get("outcome") or row.get("label") or "").strip() == target:
            return dict(row)
    return {}


def _bucket_rank_score(label: Any) -> Optional[float]:
    rank = _bucket_rank(label)
    if rank is None:
        return None
    kind, temp = rank
    if kind == 0:
        return float(temp) - 0.01
    if kind == 2:
        return float(temp) + 0.01
    return float(temp)


def _is_lower_boundary_bucket(label: Any) -> bool:
    text = str(label or "").strip().lower()
    return "or below" in text or text.startswith("<=") or "below" in text


def _is_upper_boundary_bucket(label: Any) -> bool:
    text = str(label or "").strip().lower()
    return "or higher" in text or text.startswith(">=") or "higher" in text or "above" in text


def _observation_bucket_candidates(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    rounding_rule: str,
    market_unit: str,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen: Dict[Tuple[str, str, str], int] = {}

    def add(
        *,
        source: str,
        source_label: str,
        value: Any,
        observed_at: Any,
        hardness: float,
        source_class: str,
    ) -> None:
        num = _to_float(value)
        if num is None:
            return
        raw_bucket = _raw_decimal_floor_bucket_label(rows, num, market_unit)
        display_bucket = _display_integer_bucket_label(rows, num, market_unit)
        bucket = display_bucket or raw_bucket
        record = {
            "source": source,
            "source_label": source_label,
            "source_class": source_class,
            "observed_max_c": _round(num, 4),
            "observed_at": str(observed_at or ""),
            "observed_at_utc": _fmt_utc_seconds(_parse_datetime(observed_at)),
            "observed_at_local": _fmt_local_seconds(_parse_datetime(observed_at)),
            "settlement_proxy_bucket": bucket,
            "raw_bucket": raw_bucket,
            "display_bucket": display_bucket,
            "source_hardness": float(hardness),
            "source_hardness_class": "hard_observed" if hardness >= 0.85 else "soft_display",
            "is_soft_display_source": bool(hardness < 0.85 or "wu_twc" in source),
            "bucket_rank_score": _bucket_rank_score(bucket),
        }
        key = (source, str(_round(num, 4)), str(bucket or ""))
        existing_idx = seen.get(key)
        if existing_idx is not None:
            if float(candidates[existing_idx].get("source_hardness") or 0.0) < float(hardness):
                candidates[existing_idx] = record
            return
        seen[key] = len(candidates)
        candidates.append(record)

    add(
        source="zuuu_metar_raw_awc_today_max",
        source_label="AWC Raw METAR",
        value=features.get("zuuu_metar_raw_awc_today_max_c"),
        observed_at=features.get("zuuu_metar_raw_awc_today_max_at") or features.get("zuuu_metar_raw_awc_latest_at"),
        hardness=0.95,
        source_class="official_airport_metar_hard_observed",
    )
    add(
        source="wu_twc_observed_intraday_today_max",
        source_label="WU/TWC",
        value=features.get("wu_twc_observed_intraday_today_max_c"),
        observed_at=features.get("wu_twc_observed_intraday_today_max_at")
        or features.get("wu_twc_observed_intraday_current_at"),
        hardness=0.72,
        source_class="settlement_ecosystem_display_soft_observed",
    )

    observed_value, observed_source_text, observed_na_reason = _hard_observed_today_max_c(features)
    observed_source = str(observed_source_text or features.get("observed_today_max_source") or "").strip().lower()
    observed_source_id = "observed_today_max"
    observed_label = "Observed today max"
    observed_class = "generic_observed_anchor"
    observed_hardness = 0.82
    if "wu" in observed_source or "twc" in observed_source:
        observed_source_id = "wu_twc_observed_intraday_today_max"
        observed_label = "WU/TWC"
        observed_class = "settlement_ecosystem_display_soft_observed"
        observed_hardness = 0.72
    elif "awc" in observed_source or "metar" in observed_source or "zuuu" in observed_source:
        observed_source_id = "zuuu_metar_raw_awc_today_max"
        observed_label = "AWC Raw METAR"
        observed_class = "official_airport_metar_hard_observed"
        observed_hardness = 0.95
    add(
        source=observed_source_id,
        source_label=observed_label,
        value=observed_value,
        observed_at=features.get("observed_today_max_at")
        or features.get("zuuu_metar_raw_awc_today_max_at")
        or features.get("wu_twc_observed_intraday_today_max_at"),
        hardness=observed_hardness,
        source_class=observed_class if observed_value is not None else (observed_na_reason or observed_class),
    )
    candidates.sort(
        key=lambda item: (
            item.get("bucket_rank_score") is not None,
            float(item.get("bucket_rank_score") or -999.0),
            float(item.get("source_hardness") or 0.0),
            _parse_created_at(item.get("observed_at")),
        ),
        reverse=True,
    )
    return candidates


def _target_bucket_observation_state(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    target_bucket: Optional[str],
    rounding_rule: str,
    market_unit: str,
) -> Dict[str, Any]:
    target_label = str(target_bucket or "").strip()
    if not target_label:
        return {"state": "missing_target", "realized": False, "exceeded": False}
    target_text = target_label.lower()
    target_is_lower_boundary = _is_lower_boundary_bucket(target_text)
    candidates = _observation_bucket_candidates(rows, features, rounding_rule, market_unit)
    best: Dict[str, Any] = {
        "state": "not_observed",
        "realized": False,
        "exceeded": False,
        "target_bucket": target_label,
    }
    for candidate in candidates:
        bucket = str(candidate.get("settlement_proxy_bucket") or "")
        comparison = _compare_bucket_labels(bucket, target_label) if bucket else None
        record = dict(candidate)
        record["observed_max_c"] = _round(candidate.get("observed_max_c"), 2)
        record["target_bucket"] = target_label
        if bucket == target_label:
            if target_is_lower_boundary:
                record.update(
                    {
                        "state": "observed_within_lower_boundary",
                        "realized": False,
                        "exceeded": False,
                        "alive": True,
                    }
                )
            else:
                record.update({"state": "observed_in_target_bucket", "realized": True, "exceeded": False})
            return record
        if comparison is not None and comparison > 0:
            record.update({"state": "observed_above_target_bucket", "realized": False, "exceeded": True})
            return record
        if comparison is not None and comparison < 0:
            record.update({"state": "observed_below_target_bucket", "realized": False, "exceeded": False})
            best = record
    return best


def _daily_target_bucket_selection(
    top_all: Sequence[Mapping[str, Any]],
    top_single: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    cumulative_reference = dict(top_all[0]) if top_all else {}
    target = dict(cumulative_reference)
    single_reference = dict(top_single[0]) if top_single else {}
    target_label = str(target.get("outcome") or "")
    cumulative_reference_label = str(cumulative_reference.get("outcome") or "")
    single_reference_label = str(single_reference.get("outcome") or "")
    target_is_cumulative = bool(target.get("is_cumulative_bucket")) or _is_cumulative_bucket(target_label)
    cumulative_reference_is_cumulative = bool(cumulative_reference.get("is_cumulative_bucket")) or _is_cumulative_bucket(cumulative_reference_label)
    differs = bool(single_reference_label and target_label and single_reference_label != target_label)
    target_prob, target_prob_field = _target_bucket_probability(target)
    cumulative_reference_prob, _cumulative_reference_prob_field = _target_bucket_probability(cumulative_reference)
    single_reference_prob, _single_reference_prob_field = _target_bucket_probability(single_reference)
    return {
        "version": "daily_target_bucket_selection_v3",
        "definition": (
            "每日目标桶用于日内决策、交易建议和兑现可靠性评估；"
            "按所有市场结算桶的后验概率排序，or below、or higher 累计边界桶与单温度桶同权参与目标排序。"
        ),
        "target_bucket": target_label or None,
        "target_bucket_type": "cumulative_boundary" if target_is_cumulative else ("single_temperature" if target_label else None),
        "target_probability_field": target_prob_field or None,
        "target_probability_pct": _pct(target_prob) if target_prob is not None else None,
        "target_row": target or None,
        "cumulative_boundary_reference_bucket": cumulative_reference or None,
        "cumulative_boundary_reference_probability_pct": _pct(cumulative_reference_prob) if cumulative_reference_prob is not None else None,
        "cumulative_boundary_reference_is_cumulative": cumulative_reference_is_cumulative,
        "single_temperature_reference_bucket": single_reference or None,
        "single_temperature_reference_role": "atomic_temperature_target_reference",
        "single_temperature_reference_probability_pct": _pct(single_reference_prob) if single_reference_prob is not None else None,
        "target_is_cumulative_bucket": target_is_cumulative,
        "differs_from_single_temperature_reference": differs,
        "excluded_reference_reason": (
            "single_temperature_reference_not_target"
            if single_reference_label and target_label and single_reference_label != target_label
            else None
        ),
        "selection_rule": (
            "按 D0 硬实况门控后的 q_temperature_bucket_posterior 在所有市场结算桶中选择最高桶；"
            "若该字段缺失，依次回退 q_bayes_posterior；q_calibrated 只作旧事实层兼容兜底；"
            "边界桶不再被单温度桶排除。"
        ),
        "watch_buckets": [dict(row) for row in top_all[:3]],
    }


def _merge_ssot_payload_target_selection(
    payload_selection: Any,
    computed_selection: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
    payload_authoritative: bool = False,
) -> Dict[str, Any]:
    if not isinstance(payload_selection, Mapping):
        return dict(computed_selection or {})
    out = dict(payload_selection if payload_authoritative else computed_selection or {})
    fallback = dict(computed_selection or {}) if payload_authoritative else dict(payload_selection)

    def _merge_value_present(value: Any) -> bool:
        return value not in (None, "", [], {})

    for key, value in fallback.items():
        if _merge_value_present(value) and not _merge_value_present(out.get(key)):
            out[key] = value
    target_label = str(
        out.get("target_bucket")
        or out.get("posterior_highest_bucket_label")
        or ""
    ).strip()
    if target_label and not isinstance(out.get("target_row"), Mapping):
        row = _bucket_row_by_label(rows, target_label)
        if row:
            out["target_row"] = row
    if target_label and not out.get("target_bucket_type"):
        out["target_bucket_type"] = (
            "cumulative_boundary" if _is_cumulative_bucket(target_label) else "single_temperature"
        )
    out["ssot_source"] = source
    out["selection_runtime_recomputed"] = False
    return out


def _dynamic_target_bucket_selection(
    rows: Sequence[Mapping[str, Any]],
    top_all: Sequence[Mapping[str, Any]],
    top_single: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    rounding_rule: str,
    market_unit: str,
    temperature_trend: Optional[Mapping[str, Any]] = None,
    event_date: Any = "",
    market_top_bucket: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    posterior = _daily_target_bucket_selection(top_all, top_single)
    target = dict(posterior.get("target_row") or {})
    target_label = str(posterior.get("target_bucket") or target.get("outcome") or "")
    single_reference = dict(posterior.get("single_temperature_reference_bucket") or {})
    single_reference_label = str(single_reference.get("outcome") or "")
    selected_label = target_label
    selected_row = dict(target) if target else {}
    selected_source = "dynamic_posterior_highest_bucket"
    dynamic_state = "posterior_highest_bucket"
    dynamic_label = "动态后验最高桶"
    reason = "每个快照按事实门控后的全市场后验概率重新排序；最高可结算桶即当前目标桶。"
    observations = _observation_bucket_candidates(rows, features, rounding_rule, market_unit)
    best_observed = observations[0] if observations else {}
    momentum_supports_warming, momentum_gate = _temperature_momentum_supports_warming(features, temperature_trend)
    unactionable_posterior_target = None
    selected_tradeability = _market_tradeability(selected_row)
    if selected_label and not selected_tradeability.get("tradable"):
        unactionable_posterior_target = dict(selected_row)
        unactionable_posterior_target["market_tradeability"] = selected_tradeability
    selected_row = dict(selected_row) if selected_row else {}
    if selected_row:
        selected_row["market_tradeability"] = selected_tradeability

    selected_prob, selected_prob_field = _target_bucket_probability(selected_row)
    selected_is_cumulative = bool(selected_row.get("is_cumulative_bucket")) or _is_cumulative_bucket(selected_label)
    observed_label = str(best_observed.get("settlement_proxy_bucket") or best_observed.get("display_bucket") or "").strip()
    observed_soft = bool(best_observed.get("is_soft_display_source"))
    early_switch = _switch_pressure_candidate(
        rows=rows,
        top_single=top_single,
        target=target,
        target_label=target_label,
        observed_label=observed_label,
        observed_soft=observed_soft,
        momentum_supports_warming=momentum_supports_warming,
        momentum_gate=momentum_gate,
        features=features,
        event_date=event_date,
        rounding_rule=rounding_rule,
        market_unit=market_unit,
    )
    forecast_path_breakout = None
    if not early_switch:
        forecast_path_breakout = _forecast_path_breakout_candidate(
            rows=rows,
            top_single=top_single,
            target=target,
            target_label=target_label,
            features=features,
            event_date=event_date,
            rounding_rule=rounding_rule,
            market_unit=market_unit,
        )
    trajectory_candidate = early_switch or forecast_path_breakout or {}
    trajectory_candidate_source = (
        "temperature_trajectory_switch_pressure"
        if early_switch
        else ("temperature_path_breakout_target" if forecast_path_breakout else "")
    )
    watch: List[Dict[str, Any]] = []
    for raw in [
        selected_row,
        target,
        single_reference,
        early_switch or {},
        forecast_path_breakout or {},
        *[dict(item) for item in top_all[:4]],
    ]:
        label = str(raw.get("outcome") or "")
        if not label or any(str(item.get("outcome") or "") == label for item in watch):
            continue
        row = dict(raw)
        if label == selected_label:
            row["role"] = "dynamic_target_bucket"
        elif label == target_label:
            row["role"] = "posterior_highest_bucket"
        elif label == single_reference_label:
            row["role"] = "single_temperature_reference_bucket"
        else:
            row["role"] = "watch_bucket"
        watch.append(row)
    return {
        **posterior,
        "version": "dynamic_daily_target_bucket_selection_v4",
        "definition": (
            "每日动态目标桶服从强实况/结算与事实门控后的主后验 Top1；温度轨迹、动量、物理路径必须先进入"
            "q_temperature_bucket_posterior/q_bayes_posterior，不能另起口径覆盖目标桶。"
            "or below、or higher 这类累计边界桶与单温度桶同权参与目标排序；盘口可交易性只解释执行状态，不覆盖目标桶。"
        ),
        "target_bucket": selected_label or None,
        "target_bucket_type": "cumulative_boundary" if selected_is_cumulative else ("single_temperature" if selected_label else None),
        "target_probability_field": selected_prob_field or None,
        "target_probability_pct": _pct(selected_prob) if selected_prob is not None else None,
        "target_row": selected_row or None,
        "posterior_highest_bucket": target or None,
        "posterior_highest_bucket_label": target_label or None,
        "posterior_highest_probability_pct": posterior.get("target_probability_pct"),
        "single_temperature_reference_bucket": single_reference or None,
        "single_temperature_reference_probability_pct": posterior.get("single_temperature_reference_probability_pct"),
        "dynamic_target_source": selected_source,
        "dynamic_target_state": dynamic_state,
        "dynamic_target_label": dynamic_label,
        "dynamic_target_reason": reason,
        "target_market_tradeability": selected_tradeability,
        "target_actionability_state": selected_tradeability.get("actionability_state"),
        "target_is_tradeable": bool(selected_tradeability.get("tradable")),
        "target_momentum_gate": momentum_gate,
        "forecast_path_breakout_context": (
            dict(forecast_path_breakout.get("forecast_path_breakout_context") or {})
            if isinstance(forecast_path_breakout, Mapping)
            else None
        ),
        "trajectory_candidate": dict(trajectory_candidate) if isinstance(trajectory_candidate, Mapping) and trajectory_candidate else None,
        "trajectory_candidate_source": trajectory_candidate_source or None,
        "trajectory_candidate_applied_to_target": False,
        "unactionable_posterior_target": unactionable_posterior_target,
        "observed_bucket_candidate": dict(best_observed) if best_observed else None,
        "early_switch_context": early_switch.get("early_switch_context") if early_switch else None,
        "target_is_cumulative_bucket": selected_is_cumulative,
        "differs_from_posterior_highest": bool(selected_label and target_label and selected_label != target_label),
        "differs_from_single_temperature_reference": bool(
            single_reference_label and selected_label and single_reference_label != selected_label
        ),
        "selection_rule": (
            "先用 D0 硬实况/事实门控更新每个可结算桶的 q_temperature_bucket_posterior；"
            "再在所有市场桶中取后验最高者作为唯一目标；D0 多窗口升温动量、路径突破、冷池/盆地机制只作为似然、路径可靠性和交易跟随解释，"
            "不得绕过后验 Top1 覆盖目标桶；边界桶不再被原子单温度桶排除；"
            "盘口不可执行只写入 target_market_tradeability，不触发目标 fallback。"
        ),
        "watch_buckets": watch[:4],
    }


def _probability_value(value: Any) -> Optional[float]:
    num = _to_float(value)
    if num is None or not math.isfinite(float(num)):
        return None
    if 1.0 < float(num) <= 100.0:
        return max(0.0, min(1.0, float(num) / 100.0))
    return max(0.0, min(1.0, float(num)))


def _row_probability_for_fields(row: Mapping[str, Any], fields: Sequence[str]) -> Tuple[Optional[float], str]:
    for field in fields:
        value = _probability_value(row.get(field))
        if value is not None:
            return value, field
    return None, ""


def _source_reference_top(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_id: str,
    source_label: str,
    probability_fields: Sequence[str],
    peak_c: Any = None,
    peak_source: str = "",
    market_unit: str = "C",
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("outcome") or raw.get("label") or "").strip()
        if not label:
            continue
        prob, field = _row_probability_for_fields(raw, probability_fields)
        if prob is None:
            continue
        row = dict(raw)
        row["reference_probability"] = prob
        row["reference_probability_pct"] = _pct(prob)
        row["reference_probability_field"] = field
        candidates.append(row)
    candidates.sort(
        key=lambda item: (
            float(item.get("reference_probability") or -1.0),
            -(_bucket_rank_score(item.get("outcome")) or 999.0),
        ),
        reverse=True,
    )
    peak_value = _to_float(peak_c)
    peak_bucket = ""
    if peak_value is not None:
        peak_bucket = _display_integer_bucket_label(rows, peak_value, market_unit)
        if not peak_bucket:
            peak_bucket = _bucket_label_for_observed_value(rows, peak_value, "", market_unit)
    top = dict(candidates[0]) if candidates else {}
    if not top and peak_bucket:
        top = _bucket_row_by_label(rows, peak_bucket)
        if top:
            prob, field = _target_bucket_probability(top)
            top["reference_probability"] = prob
            top["reference_probability_pct"] = _pct(prob)
            top["reference_probability_field"] = field or None
    label = str(top.get("outcome") or peak_bucket or "").strip()
    return {
        "available": bool(label),
        "source_id": source_id,
        "source_label": source_label,
        "top_bucket": label or None,
        "top_probability_pct": top.get("reference_probability_pct"),
        "top_probability_field": top.get("reference_probability_field"),
        "top_row": top or None,
        "peak_c": _round(peak_value, 2),
        "peak_source": peak_source or None,
    }


def _forecast_reference_bayes(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    *,
    market_unit: str = "C",
) -> Dict[str, Any]:
    model_peak = _to_float(features.get("forecast_peak_temp_c"))
    if model_peak is None:
        model_peak = _to_float(features.get("forecast_peak_temp"))
    if model_peak is None:
        model_peak = _to_float(features.get("model_peak_temp_c"))
    settlement_peak = _to_float(features.get("settlement_source_forecast_peak_c"))
    if settlement_peak is None:
        settlement_peak = _to_float(features.get("wu_forecast_peak_c"))
    if settlement_peak is None:
        settlement_peak = _to_float(features.get("weather_com_forecast_peak_c"))
    if settlement_peak is None:
        settlement_peak = _to_float(features.get("twc_forecast_peak_c"))
    airport_peak = _to_float(features.get("taf_forecast_peak_c"))

    sources = {
        "model_forecast": _source_reference_top(
            rows,
            source_id="model_forecast",
            source_label="三大模型预测最高",
            probability_fields=(
                "q_bayes_model_prob",
                "q_ensemble_qm_bucket",
                "q_model_temperature_bucket_round",
                "q_raw",
            ),
            peak_c=model_peak,
            peak_source="forecast_peak_temp_c/model_peak_temp_c",
            market_unit=market_unit,
        ),
        "settlement_source_forecast": _source_reference_top(
            rows,
            source_id="settlement_source_forecast",
            source_label="WU/TWC 结算源预测最高",
            probability_fields=(
                "q_bayes_wu_forecast_prob",
                "q_wu_forecast_peak",
                "q_weather_com_forecast_peak",
            ),
            peak_c=settlement_peak,
            peak_source="settlement_source_forecast_peak_c/wu_forecast_peak_c",
            market_unit=market_unit,
        ),
        "airport_forecast": _source_reference_top(
            rows,
            source_id="airport_forecast",
            source_label="AWC/TAF 机场预测最高",
            probability_fields=("q_bayes_taf_peak_prob", "q_taf_peak"),
            peak_c=airport_peak,
            peak_source="taf_forecast_peak_c",
            market_unit=market_unit,
        ),
    }
    scores: Dict[str, Dict[str, Any]] = {}
    for source_key, source in sources.items():
        if not source.get("available") or not source.get("top_bucket"):
            continue
        label = str(source.get("top_bucket") or "")
        prob = _probability_value(source.get("top_probability_pct"))
        if prob is not None and source.get("top_probability_pct") is not None and float(source.get("top_probability_pct") or 0.0) > 1.0:
            prob = float(source.get("top_probability_pct") or 0.0) / 100.0
        if prob is None:
            prob = 1.0
        item = scores.setdefault(label, {"bucket": label, "score": 0.0, "weight": 0.0, "sources": []})
        weight = 1.0
        item["score"] += float(prob) * weight
        item["weight"] += weight
        item["sources"].append(source_key)
    combined: List[Dict[str, Any]] = []
    for item in scores.values():
        weight = float(item.get("weight") or 0.0)
        if weight <= 0:
            continue
        score = float(item.get("score") or 0.0) / weight
        row = _bucket_row_by_label(rows, item.get("bucket"))
        combined.append(
            {
                "outcome": item.get("bucket"),
                "reference_probability": score,
                "reference_probability_pct": _pct(score),
                "reference_sources": list(item.get("sources") or []),
                "row": row or None,
            }
        )
    combined.sort(key=lambda item: float(item.get("reference_probability") or -1.0), reverse=True)
    target = combined[0] if combined else {}
    return {
        "available": bool(target),
        "version": "forecast_reference_bayes_v1",
        "role": "weak_likelihood_reference_only",
        "definition": "预测模型/WU-TWC预测/TAF预测只形成 forecast_reference，不允许直接覆盖今日观测目标桶。",
        "target_bucket": target.get("outcome"),
        "target_probability_pct": target.get("reference_probability_pct"),
        "target_probability_field": "forecast_reference_combined_probability",
        "target_row": target.get("row") if isinstance(target.get("row"), Mapping) else None,
        "sources": sources,
        "combined_top": combined[:5],
    }


def _observation_bucket_mass(
    label: Any,
    *,
    peak_mu_c: Optional[float],
    peak_sigma_c: Optional[float],
    observed_floor_bucket: str = "",
) -> Tuple[Optional[float], str]:
    text = str(label or "").strip()
    threshold = _bucket_temp(text)
    if not text or threshold is None:
        return None, "bucket_threshold_missing"
    if observed_floor_bucket and _compare_bucket_labels(text, observed_floor_bucket) == -1:
        return 0.0, "hard_zero_below_observed_floor"
    if peak_mu_c is None or not math.isfinite(float(peak_mu_c)):
        return None, "peak_estimator_mu_missing"
    sigma = peak_sigma_c if peak_sigma_c is not None and math.isfinite(float(peak_sigma_c)) else None
    if sigma is None or float(sigma) <= 0.05:
        sigma = 0.85
    sigma_f = max(float(sigma), 0.05)
    if _is_lower_boundary_bucket(text):
        upper = float(threshold) + 0.5
        return max(0.0, min(1.0, _normal_cdf((upper - float(peak_mu_c)) / sigma_f))), "normal_mass_lower_boundary"
    if _is_upper_boundary_bucket(text):
        lower = float(threshold) - 0.5
        return max(0.0, min(1.0, 1.0 - _normal_cdf((lower - float(peak_mu_c)) / sigma_f))), "normal_mass_upper_boundary"
    low = float(threshold) - 0.5
    high = float(threshold) + 0.5
    mass = _normal_cdf((high - float(peak_mu_c)) / sigma_f) - _normal_cdf((low - float(peak_mu_c)) / sigma_f)
    return max(0.0, min(1.0, mass)), "normal_mass_single_temperature_bucket"


def _observation_led_bayes(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    temperature_trend: Optional[Mapping[str, Any]],
    feature_groups: Optional[Mapping[str, Any]] = None,
    *,
    rounding_rule: str = "",
    market_unit: str = "C",
    event_date: Any = "",
    reference_bucket: str = "",
) -> Dict[str, Any]:
    observations = _observation_bucket_candidates(rows, features, rounding_rule, market_unit)
    best_observed = observations[0] if observations else {}
    hard_observed = next(
        (
            item
            for item in observations
            if not item.get("is_soft_display_source")
            and (_to_float(item.get("source_hardness")) or 0.0) >= 0.85
        ),
        {},
    )
    observed_current = _to_float(features.get("observed_temp_c"))
    observed_max, observed_max_source, observed_max_na_reason = _hard_observed_today_max_c(features)
    observed_path_temp, observed_path_source = _observed_path_temperature_c(observed_current, observed_max)
    observed_floor_bucket = str(
        hard_observed.get("settlement_proxy_bucket")
        or hard_observed.get("display_bucket")
        or ""
    ).strip()
    if not observed_floor_bucket and observed_path_temp is not None:
        observed_floor_bucket = _display_integer_bucket_label(rows, observed_path_temp, market_unit)
        if not observed_floor_bucket:
            observed_floor_bucket = _bucket_label_for_observed_value(rows, observed_path_temp, rounding_rule, market_unit)

    peak_mu = _to_float(features.get("peak_estimator_mu_c"))
    has_peak_estimator_mu = peak_mu is not None
    peak_sigma = _to_float(features.get("peak_estimator_sigma_c"))
    peak_source = str(features.get("peak_estimator_source") or "peak_estimator_mu_c").strip()
    momentum_supports_warming, momentum_gate = _temperature_momentum_supports_warming(features, temperature_trend)
    window = _d0_intraday_switch_window(features, event_date=event_date)
    actual_rate = _to_float(momentum_gate.get("effective_rate_c_per_hour"))
    if actual_rate is None:
        actual_rate = _temperature_rise_rate_value(features)
    hours_to_peak = _to_float(window.get("hours_to_peak_window"))
    projected_peak = None
    if observed_path_temp is not None and actual_rate is not None and hours_to_peak is not None:
        projected_peak = float(observed_path_temp) + max(0.0, float(actual_rate)) * max(0.0, float(hours_to_peak))
    projected_peak_usable = bool(
        projected_peak is not None
        and window.get("active")
        and momentum_supports_warming
    )
    observed_breaks_reference = bool(
        observed_floor_bucket
        and reference_bucket
        and _compare_bucket_labels(observed_floor_bucket, reference_bucket) == 1
    )
    physical_modifier = _physical_weather_modifier(features, feature_groups)
    if not has_peak_estimator_mu and not projected_peak_usable and not observed_breaks_reference:
        return {
            "available": False,
            "version": "observation_led_bayes_v1",
            "reason": "peak_estimator_or_confirmed_breakout_missing",
            "reference_bucket": str(reference_bucket or "") or None,
            "observed_floor_bucket": observed_floor_bucket or None,
            "observed_temp_c": _round(observed_current, 2),
            "observed_today_max_c": _round(observed_max, 2),
            "observed_today_max_source": observed_max_source,
            "observed_today_max_na_reason": observed_max_na_reason,
            "temperature_momentum_projected_peak_c": _round(projected_peak, 2),
            "temperature_momentum_effective_rate_c_per_hour": _round(actual_rate, 2),
            "temperature_momentum_supports_warming": bool(momentum_supports_warming),
            "temperature_momentum_gate": dict(momentum_gate),
            "intraday_window": dict(window),
            "physical_weather_modifier": physical_modifier,
            "physical_weather_state": physical_modifier.get("state"),
            "physical_weather_peak_mu_adjustment_c": physical_modifier.get("peak_mu_adjustment_c"),
        }
    if peak_mu is None and projected_peak_usable:
        peak_mu = projected_peak
        peak_source = "temperature_momentum_projected_peak"
    if peak_mu is None and observed_path_temp is not None:
        peak_mu = observed_path_temp
        peak_source = observed_path_source or "observed_path_temperature"
    if peak_sigma is None:
        peak_sigma = 0.35 if str(peak_source).startswith("locked_trusted_observed") else 0.85
    base_peak_mu = peak_mu
    physical_adjustment = _to_float(physical_modifier.get("peak_mu_adjustment_c"))
    if peak_mu is not None and physical_adjustment is not None and abs(float(physical_adjustment)) > 1e-9:
        adjusted_peak = float(peak_mu) + float(physical_adjustment)
        if observed_path_temp is not None:
            adjusted_peak = max(float(observed_path_temp), adjusted_peak)
        peak_mu = adjusted_peak
        peak_source = f"{peak_source}+physical_weather_modifier"
    sigma_add = _to_float(physical_modifier.get("peak_sigma_add_c"))
    if peak_sigma is not None and sigma_add is not None and sigma_add > 0:
        peak_sigma = max(0.05, float(peak_sigma) + float(sigma_add))
    post_peak_anchor_applied = False
    post_peak_anchor_reason = ""
    post_peak_anchor_after_h = _to_float(os.environ.get("PM_CHENGDU_OBS_LED_POST_PEAK_ANCHOR_AFTER_HOURS"))
    if post_peak_anchor_after_h is None or not math.isfinite(float(post_peak_anchor_after_h)):
        post_peak_anchor_after_h = 0.75
    hard_observed_source_text = str(observed_max_source or features.get("observed_temp_source") or "").lower()
    hard_observed_source = any(token in hard_observed_source_text for token in ("zuuu", "metar", "awc", "airport"))
    if (
        _to_bool(os.environ.get("PM_CHENGDU_OBS_LED_POST_PEAK_HARD_OBS_ANCHOR_ENABLED", "1"))
        and hard_observed_source
        and hours_to_peak is not None
        and float(hours_to_peak) <= -float(post_peak_anchor_after_h)
        and observed_path_temp is not None
        and peak_mu is not None
        and float(peak_mu) > float(observed_path_temp)
    ):
        peak_mu = float(observed_path_temp)
        peak_source = f"{peak_source}+post_peak_hard_observation_anchor"
        post_peak_anchor_applied = True
        post_peak_anchor_reason = "hard_observed_max_after_peak_window"

    bucket_rows: List[Dict[str, Any]] = []
    mass_total = 0.0
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("outcome") or raw.get("label") or "").strip()
        if not label:
            continue
        mass, reason = _observation_bucket_mass(
            label,
            peak_mu_c=peak_mu,
            peak_sigma_c=peak_sigma,
            observed_floor_bucket=observed_floor_bucket,
        )
        if mass is None:
            continue
        row = dict(raw)
        row["observation_led_probability_raw"] = _round(mass, 6)
        row["observation_led_probability_reason"] = reason
        bucket_rows.append(row)
        mass_total += float(mass)
    if not bucket_rows:
        return {
            "available": False,
            "version": "observation_led_bayes_v1",
            "reason": "observation_led_bucket_mass_missing",
            "observed_today_max_c": _round(observed_max, 2),
            "observed_today_max_source": observed_max_source,
            "observed_today_max_na_reason": observed_max_na_reason,
            "observed_temp_c": _round(observed_current, 2),
            "peak_estimator_mu_c": _round(peak_mu, 2),
            "peak_estimator_sigma_c": _round(peak_sigma, 2),
            "physical_weather_modifier": physical_modifier,
            "physical_weather_state": physical_modifier.get("state"),
            "physical_weather_peak_mu_adjustment_c": physical_modifier.get("peak_mu_adjustment_c"),
        }
    if mass_total <= 0 and observed_floor_bucket:
        for row in bucket_rows:
            if str(row.get("outcome") or "") == observed_floor_bucket:
                row["observation_led_probability_raw"] = 1.0
                mass_total = 1.0
                break
    if mass_total <= 0:
        return {
            "available": False,
            "version": "observation_led_bayes_v1",
            "reason": "observation_led_probability_zero_after_observed_floor",
            "observed_floor_bucket": observed_floor_bucket or None,
        }
    for row in bucket_rows:
        prob = float(row.get("observation_led_probability_raw") or 0.0) / float(mass_total)
        row["observation_led_probability"] = _round(prob, 6)
        row["observation_led_probability_pct"] = _pct(prob)
        row["target_bucket_probability"] = prob
        row["target_bucket_probability_pct"] = _pct(prob)
        row["target_bucket_probability_field"] = "observation_led_bayes_probability"
    bucket_rows.sort(
        key=lambda row: (
            float(row.get("observation_led_probability") or -1.0),
            -(_bucket_rank_score(row.get("outcome")) or 999.0),
        ),
        reverse=True,
    )
    target_row = dict(bucket_rows[0])
    target_label = str(target_row.get("outcome") or "").strip()
    reference = str(reference_bucket or "").strip()
    cmp_to_reference = _compare_bucket_labels(target_label, reference) if target_label and reference else None
    if cmp_to_reference is None or not reference:
        switch_direction = "unknown"
    elif cmp_to_reference > 0:
        switch_direction = "up"
    elif cmp_to_reference < 0:
        switch_direction = "down"
    else:
        switch_direction = "hold"
    target_temp = _bucket_temp(target_label)
    delta_to_candidate = (
        float(target_temp) - float(peak_mu)
        if target_temp is not None and peak_mu is not None
        else None
    )
    return {
        "available": True,
        "version": "observation_led_bayes_v1",
        "role": "medium_strong_likelihood_diagnostic",
        "definition": "当前实况、今日已观测最高、温度轨迹、温度动量和峰值估计共同形成 observation-led 似然诊断；它必须进入主后验或显式切桶信号，不能单独覆盖 SSOT 目标桶。",
        "target_bucket": target_label or None,
        "target_probability_pct": target_row.get("observation_led_probability_pct"),
        "target_probability_field": "observation_led_bayes_probability",
        "target_row": target_row,
        "reference_bucket": reference or None,
        "switch_from_bucket": reference or None,
        "switch_candidate_bucket": target_label or None,
        "switch_direction": switch_direction,
        "switch_explanation": (
            f"{reference} -> {target_label}"
            if reference and target_label and reference != target_label
            else (f"维持 {target_label}" if target_label else "目标桶待确认")
        ),
        "switch_support_state": (
            "supports_switch" if switch_direction in {"up", "down"} else ("hold" if switch_direction == "hold" else "unknown")
        ),
        "peak_estimator_delta_to_candidate_c": _round(delta_to_candidate, 2),
        "observed_floor_bucket": observed_floor_bucket or None,
        "observed_bucket_candidate": dict(best_observed) if best_observed else None,
        "observed_temp_c": _round(observed_current, 2),
        "observed_today_max_c": _round(observed_max, 2),
        "observed_today_max_source": observed_max_source,
        "observed_today_max_na_reason": observed_max_na_reason,
        "observed_path_temp_c": _round(observed_path_temp, 2),
        "observed_path_temp_source": observed_path_source,
        "base_peak_estimator_mu_c": _round(base_peak_mu, 2),
        "peak_estimator_mu_c": _round(peak_mu, 2),
        "peak_estimator_sigma_c": _round(peak_sigma, 2),
        "peak_estimator_source": peak_source,
        "post_peak_hard_observation_anchor_applied": post_peak_anchor_applied,
        "post_peak_hard_observation_anchor_reason": post_peak_anchor_reason,
        "physical_weather_modifier": physical_modifier,
        "physical_weather_state": physical_modifier.get("state"),
        "physical_weather_peak_mu_adjustment_c": physical_modifier.get("peak_mu_adjustment_c"),
        "temperature_momentum_projected_peak_c": _round(projected_peak, 2),
        "temperature_momentum_effective_rate_c_per_hour": _round(actual_rate, 2),
        "temperature_momentum_supports_warming": bool(momentum_supports_warming),
        "temperature_momentum_gate": dict(momentum_gate),
        "intraday_window": dict(window),
        "rows": bucket_rows[:8],
        "all_rows": bucket_rows,
    }


def _observation_led_likelihood_weight(
    observation_led_bayes: Mapping[str, Any],
    features: Mapping[str, Any],
) -> float:
    override = _to_float(os.environ.get("PM_OBSERVATION_LED_MAIN_POSTERIOR_WEIGHT"))
    if override is not None:
        return max(0.0, min(0.98, float(override)))

    source = str(
        observation_led_bayes.get("observed_today_max_source")
        or features.get("observed_today_max_source")
        or features.get("observed_temp_source")
        or ""
    ).lower()
    if any(token in source for token in ("settlement", "final_daily_max", "official_daily_max")):
        weight = 0.90
    elif any(token in source for token in ("zuuu", "metar", "awc", "airport")):
        weight = 0.72
    elif any(token in source for token in ("wu", "twc", "weather.com", "wunderground")):
        weight = 0.62
    else:
        weight = 0.55

    physical_state = str(observation_led_bayes.get("physical_weather_state") or "").strip()
    if physical_state in {"cooling", "warming"}:
        weight += 0.06
    if observation_led_bayes.get("temperature_momentum_supports_warming") is True:
        weight += 0.04

    sigma = _to_float(observation_led_bayes.get("peak_estimator_sigma_c"))
    if sigma is not None and sigma > 2.0:
        weight -= min(0.18, max(0.0, float(sigma) - 2.0) * 0.08)
    window = observation_led_bayes.get("intraday_window")
    if isinstance(window, Mapping) and window.get("active") is False:
        weight -= 0.04
    return max(0.35, min(0.88, float(weight)))


def _apply_observation_led_likelihood_to_rows(
    rows: Sequence[Mapping[str, Any]],
    observation_led_bayes: Mapping[str, Any],
    features: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    copied = [dict(row) for row in rows or []]
    if not observation_led_bayes.get("available"):
        return copied, {
            "applied": False,
            "reason": str(observation_led_bayes.get("reason") or "observation_led_bayes_unavailable"),
            "version": "observation_led_main_posterior_likelihood_v1",
        }
    if _hard_final_bucket_lock_reason(features):
        return copied, {
            "applied": False,
            "reason": "final_bucket_lock_has_priority",
            "version": "observation_led_main_posterior_likelihood_v1",
        }
    source_text = str(
        observation_led_bayes.get("observed_today_max_source")
        or features.get("observed_today_max_source")
        or features.get("observed_temp_source")
        or ""
    ).strip()
    source_lower = source_text.lower()
    recognized_observation_source = any(
        token in source_lower
        for token in (
            "zuuu",
            "metar",
            "awc",
            "airport",
            "wu",
            "twc",
            "weather.com",
            "wunderground",
            "settlement",
            "official_daily_max",
            "final_daily_max",
        )
    )
    if not recognized_observation_source and _to_bool(features.get("observed_today_max_trusted")) is not True:
        return copied, {
            "applied": False,
            "reason": "observation_source_not_hard_or_trusted",
            "version": "observation_led_main_posterior_likelihood_v1",
        }

    raw_obs_rows = observation_led_bayes.get("all_rows") or observation_led_bayes.get("rows") or []
    obs_by_label: Dict[str, Mapping[str, Any]] = {}
    for raw in raw_obs_rows:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("outcome") or raw.get("label") or "").strip()
        if label:
            obs_by_label[label] = raw
    if not obs_by_label:
        return copied, {
            "applied": False,
            "reason": "observation_led_probability_rows_missing",
            "version": "observation_led_main_posterior_likelihood_v1",
        }

    weight = _observation_led_likelihood_weight(observation_led_bayes, features)
    epsilon = 1e-9
    scores: List[Optional[float]] = []
    pre_probs: List[Optional[float]] = []
    obs_probs: List[Optional[float]] = []
    hard_zero_reasons: List[str] = []
    for row in copied:
        label = str(row.get("outcome") or row.get("label") or "").strip()
        obs_row = obs_by_label.get(label) or {}
        obs_prob = _probability_value(obs_row.get("observation_led_probability"))
        if obs_prob is None:
            obs_prob = _probability_value(obs_row.get("target_bucket_probability"))
        base_prob = _probability_value(row.get("q_temperature_bucket_posterior_pre_observation_led_likelihood"))
        if base_prob is None:
            base_prob = _probability_value(row.get("q_temperature_bucket_posterior"))
        if base_prob is None:
            base_prob = _probability_value(row.get("q_bayes_posterior"))
        if base_prob is None:
            base_prob = _probability_value(row.get("q_calibrated"))
        pre_probs.append(base_prob)
        obs_probs.append(obs_prob)
        reason = str(obs_row.get("observation_led_probability_reason") or "")
        hard_zero = bool(row.get("q_posterior_observed_constraints_hard_zero")) or reason == "hard_zero_below_observed_floor"
        hard_zero_reasons.append(reason if hard_zero else "")
        if base_prob is None and obs_prob is None:
            scores.append(None)
        elif hard_zero:
            scores.append(0.0)
        elif base_prob is None:
            scores.append(max(0.0, float(obs_prob or 0.0)))
        elif obs_prob is None:
            scores.append(max(0.0, float(base_prob)))
        else:
            score = math.exp(
                (1.0 - weight) * math.log(max(float(base_prob), epsilon))
                + weight * math.log(max(float(obs_prob), epsilon))
            )
            scores.append(max(0.0, score))

    total = sum(float(score) for score in scores if score is not None and math.isfinite(float(score)))
    if total <= 0:
        return copied, {
            "applied": False,
            "reason": "observation_led_reweighted_probability_zero",
            "version": "observation_led_main_posterior_likelihood_v1",
        }

    target_before = ""
    target_after = ""
    before_candidates = []
    after_candidates = []
    for idx, row in enumerate(copied):
        pre_prob = pre_probs[idx]
        obs_prob = obs_probs[idx]
        new_prob = max(0.0, float(scores[idx] or 0.0) / float(total)) if scores[idx] is not None else None
        row["observation_led_likelihood_applied"] = True
        row["observation_led_likelihood_weight"] = _round(weight, 4)
        row["observation_led_likelihood_probability"] = obs_prob
        row["observation_led_likelihood_probability_pct"] = _pct(obs_prob)
        row["observation_led_likelihood_source"] = "observation_led_bayes"
        if hard_zero_reasons[idx]:
            row["observation_led_likelihood_hard_zero"] = True
            row["observation_led_likelihood_hard_zero_reason"] = hard_zero_reasons[idx]
        if "q_temperature_bucket_posterior_pre_observation_led_likelihood" not in row:
            row["q_temperature_bucket_posterior_pre_observation_led_likelihood"] = pre_prob
            row["q_temperature_bucket_posterior_pre_observation_led_likelihood_pct"] = _pct(pre_prob)
        if "q_bayes_posterior_pre_observation_led_likelihood" not in row:
            row["q_bayes_posterior_pre_observation_led_likelihood"] = _probability_value(row.get("q_bayes_posterior"))
            row["q_bayes_posterior_pre_observation_led_likelihood_pct"] = _pct(row.get("q_bayes_posterior_pre_observation_led_likelihood"))
        if new_prob is not None:
            row["q_temperature_bucket_posterior"] = new_prob
            row["q_temperature_bucket_posterior_pct"] = _pct(new_prob)
            row["q_bayes_posterior"] = new_prob
            row["q_bayes_posterior_pct"] = _pct(new_prob)
            row["q_posterior_observation_led_likelihood_applied"] = True
            row["q_posterior_observation_led_likelihood_weight"] = _round(weight, 4)
            row["q_posterior_observation_led_likelihood_source"] = "observation_led_bayes"
        label = str(row.get("outcome") or row.get("label") or "").strip()
        if label and pre_prob is not None:
            before_candidates.append((float(pre_prob), label))
        if label and new_prob is not None:
            after_candidates.append((float(new_prob), label))
    if before_candidates:
        before_candidates.sort(key=lambda item: item[0], reverse=True)
        target_before = before_candidates[0][1]
    if after_candidates:
        after_candidates.sort(key=lambda item: item[0], reverse=True)
        target_after = after_candidates[0][1]

    return copied, {
        "applied": True,
        "version": "observation_led_main_posterior_likelihood_v1",
        "weight": _round(weight, 4),
        "source": "observation_led_bayes",
        "source_class": "strong_observation_likelihood"
        if weight >= 0.70
        else "medium_strong_observation_likelihood",
        "target_bucket_before": target_before or None,
        "target_bucket_after": target_after or None,
        "observation_led_target_bucket": observation_led_bayes.get("target_bucket"),
        "observed_today_max_c": observation_led_bayes.get("observed_today_max_c"),
        "peak_estimator_mu_c": observation_led_bayes.get("peak_estimator_mu_c"),
        "peak_estimator_sigma_c": observation_led_bayes.get("peak_estimator_sigma_c"),
        "physical_weather_state": observation_led_bayes.get("physical_weather_state"),
        "physical_weather_peak_mu_adjustment_c": observation_led_bayes.get("physical_weather_peak_mu_adjustment_c"),
    }


def _settlement_lock_bayes(
    rows: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    *,
    rounding_rule: str = "",
    market_unit: str = "C",
) -> Dict[str, Any]:
    final_reason = _hard_final_bucket_lock_reason(features)
    if not final_reason:
        return {
            "available": False,
            "locked": False,
            "version": "settlement_lock_bayes_v1",
            "reason": "final_settlement_missing",
        }
    settlement_value = _to_float(features.get("settlement_actual"))
    if settlement_value is None:
        settlement_value = _to_float(features.get("settlement_actual_c"))
    if settlement_value is None:
        settlement_value, _settlement_proxy_source, _settlement_proxy_na_reason = _hard_observed_today_max_c(features)
    bucket = _bucket_label_for_observed_value(rows, settlement_value, rounding_rule, market_unit)
    if not bucket:
        bucket = _display_integer_bucket_label(rows, settlement_value, market_unit)
    row = _bucket_row_by_label(rows, bucket)
    if row:
        row["target_bucket_probability"] = 1.0
        row["target_bucket_probability_pct"] = 100.0
        row["target_bucket_probability_field"] = "settlement_lock_bayes_probability"
    return {
        "available": bool(bucket),
        "locked": bool(bucket),
        "version": "settlement_lock_bayes_v1",
        "role": "strongest_likelihood_final_bucket_lock",
        "target_bucket": bucket or None,
        "target_probability_pct": 100.0 if bucket else None,
        "target_probability_field": "settlement_lock_bayes_probability",
        "target_row": row or None,
        "settlement_actual_c": _round(settlement_value, 2),
        "final_lock_reason": final_reason,
        "source": str(features.get("settlement_actual_source") or "settlement_actual"),
    }


def _apply_bayes_target_priority(
    computed_selection: Mapping[str, Any],
    *,
    forecast_reference_bayes: Mapping[str, Any],
    observation_led_bayes: Mapping[str, Any],
    settlement_lock_bayes: Mapping[str, Any],
) -> Dict[str, Any]:
    out = dict(computed_selection or {})
    previous_label = str(out.get("target_bucket") or "").strip()
    observation_priority_sources: set[str] = set()
    computed_dynamic_source = str(out.get("dynamic_target_source") or "").strip()
    computed_observation_bayes: Dict[str, Any] = {}
    if computed_dynamic_source in observation_priority_sources and previous_label:
        computed_row = out.get("target_row") if isinstance(out.get("target_row"), Mapping) else {}
        computed_observation_bayes = dict(observation_led_bayes or {})
        computed_observation_bayes.update(
            {
                "available": True,
                "version": "observation_led_bayes_v1",
                "role": "medium_strong_likelihood_diagnostic",
                "definition": (
                    "当前实况、今日已观测最高、温度轨迹、温度动量和峰值估计共同形成 observation-led 显式切桶信号；"
                    "该信号必须先进入主后验概率，不再绕过 posterior Top1 直接覆盖目标桶；低于终局结算锁。"
                ),
                "target_bucket": previous_label,
                "target_probability_pct": out.get("target_probability_pct"),
                "target_probability_field": out.get("target_probability_field") or "observation_led_dynamic_target_probability",
                "target_row": dict(computed_row) if computed_row else None,
                "reference_bucket": out.get("posterior_highest_bucket_label") or None,
                "selection_underlying_source": computed_dynamic_source,
                "selection_underlying_state": str(out.get("dynamic_target_state") or ""),
                "selection_underlying_label": str(out.get("dynamic_target_label") or ""),
                "selection_underlying_reason": str(out.get("dynamic_target_reason") or ""),
            }
        )
        early_context = out.get("early_switch_context") if isinstance(out.get("early_switch_context"), Mapping) else {}
        breakout_context = (
            out.get("forecast_path_breakout_context")
            if isinstance(out.get("forecast_path_breakout_context"), Mapping)
            else {}
        )
        switch_context = early_context or breakout_context
        from_bucket = (
            switch_context.get("held_target_bucket")
            or out.get("posterior_highest_bucket_label")
            or forecast_reference_bayes.get("target_bucket")
        )
        candidate_bucket = switch_context.get("candidate_bucket") or previous_label
        computed_observation_bayes.update(
            {
                "switch_from_bucket": from_bucket,
                "switch_candidate_bucket": candidate_bucket,
                "switch_explanation": (
                    f"{from_bucket} -> {previous_label}"
                    if from_bucket and previous_label and str(from_bucket) != previous_label
                    else f"维持 {previous_label}"
                ),
                "switch_direction": (
                    "up"
                    if _compare_bucket_labels(previous_label, from_bucket) == 1
                    else (
                        "down"
                        if _compare_bucket_labels(previous_label, from_bucket) == -1
                        else "hold"
                    )
                ) if from_bucket else "unknown",
                "switch_support_state": "supports_switch" if from_bucket and str(from_bucket) != previous_label else "hold",
                "temperature_trajectory_context": dict(switch_context) if switch_context else None,
            }
        )
    observation_priority_bayes = computed_observation_bayes or dict(observation_led_bayes or {})
    chosen = None
    chosen_source = ""
    chosen_state = ""
    chosen_label = ""
    chosen_reason = ""
    if settlement_lock_bayes.get("locked") and settlement_lock_bayes.get("target_bucket"):
        chosen = settlement_lock_bayes
        chosen_source = "settlement_lock_bayes"
        chosen_state = "final_bucket_lock"
        chosen_label = "终局结算锁"
        chosen_reason = "WU/TWC final settlement 或 settlement_actual 已确认，直接 final_bucket_lock。"
    elif (
        not previous_label
        and forecast_reference_bayes.get("available")
        and forecast_reference_bayes.get("target_bucket")
    ):
        chosen = forecast_reference_bayes
        chosen_source = "forecast_reference_bayes"
        chosen_state = "forecast_reference_target_bucket"
        chosen_label = "预测参考目标桶"
        chosen_reason = "缺少可用观测主导目标时，只用预测弱似然作为参考目标。"

    if chosen is not None:
        target_label = str(chosen.get("target_bucket") or "").strip()
        target_row = chosen.get("target_row") if isinstance(chosen.get("target_row"), Mapping) else {}
        observation_confirms_existing_target = bool(
            chosen_source == "observation_led_bayes"
            and previous_label
            and target_label == previous_label
        )
        out["target_bucket"] = target_label or None
        out["target_bucket_type"] = (
            "cumulative_boundary" if _is_cumulative_bucket(target_label) else ("single_temperature" if target_label else None)
        )
        if observation_confirms_existing_target:
            out["target_probability_pct"] = out.get("target_probability_pct", chosen.get("target_probability_pct"))
            out["target_probability_field"] = out.get("target_probability_field", chosen.get("target_probability_field"))
            if not isinstance(out.get("target_row"), Mapping):
                out["target_row"] = dict(target_row) if target_row else _bucket_row_by_label(
                    [*(observation_led_bayes.get("rows") or []), *(forecast_reference_bayes.get("rows") or [])],
                    target_label,
                )
        else:
            out["target_probability_pct"] = chosen.get("target_probability_pct")
            out["target_probability_field"] = chosen.get("target_probability_field")
            out["target_row"] = dict(target_row) if target_row else _bucket_row_by_label(
                [*(observation_led_bayes.get("rows") or []), *(forecast_reference_bayes.get("rows") or [])],
                target_label,
            )
        underlying_source = str(chosen.get("selection_underlying_source") or "").strip()
        out["dynamic_target_source"] = underlying_source or chosen_source
        out["dynamic_target_state"] = str(chosen.get("selection_underlying_state") or "") or chosen_state
        out["dynamic_target_label"] = str(chosen.get("selection_underlying_label") or "") or chosen_label
        out["dynamic_target_reason"] = str(chosen.get("selection_underlying_reason") or "") or chosen_reason
        out["target_selection_source"] = chosen_source
        out["target_selection_state"] = chosen_state
        out["target_selection_reason"] = chosen_reason
        if observation_confirms_existing_target:
            out["observation_led_confirmed_existing_target"] = True
    else:
        out.setdefault("target_selection_source", str(out.get("dynamic_target_source") or "dynamic_posterior_highest_bucket"))
        out.setdefault("target_selection_state", str(out.get("dynamic_target_state") or "posterior_highest_bucket"))
        out.setdefault("target_selection_reason", str(out.get("dynamic_target_reason") or "事实门控后的主后验最高桶作为目标桶。"))
    out["target_selection_priority_version"] = "bayes_target_selection_priority_v1"
    out["selection_priority"] = chosen_source or str(out.get("dynamic_target_source") or "dynamic_posterior_highest_bucket")
    out["definition"] = (
        "Bayes 目标桶优先级：final_bucket_lock > explicit_observation_led_switch > "
        "hard-source-constrained posterior highest > forecast_reference fallback。预测只作弱似然参考；"
        "实况、温度轨迹、动量和峰值估计必须进入主后验或显式切桶信号，不能用第二套 observation-led 分布覆盖 SSOT 目标；终局结算直接锁桶。"
    )
    out["selection_priority_chain"] = [
        "settlement_lock_bayes",
        "explicit_observation_led_switch",
        "hard_source_constrained_posterior_highest",
        "forecast_reference_fallback",
    ]
    out["previous_dynamic_target_bucket"] = previous_label or None
    out["forecast_reference_bayes"] = dict(forecast_reference_bayes)
    out["observation_led_bayes"] = dict(observation_priority_bayes)
    out["settlement_lock_bayes"] = dict(settlement_lock_bayes)
    out["forecast_reference_target_bucket"] = forecast_reference_bayes.get("target_bucket")
    out["observation_led_target_bucket"] = observation_priority_bayes.get("target_bucket")
    out["settlement_lock_target_bucket"] = settlement_lock_bayes.get("target_bucket")
    out["target_switch_context"] = {
        "from_bucket": observation_priority_bayes.get("switch_from_bucket") or previous_label or forecast_reference_bayes.get("target_bucket"),
        "to_bucket": out.get("target_bucket"),
        "candidate_bucket": observation_priority_bayes.get("switch_candidate_bucket") or out.get("target_bucket"),
        "direction": observation_priority_bayes.get("switch_direction"),
        "support_state": observation_priority_bayes.get("switch_support_state"),
        "explanation": observation_priority_bayes.get("switch_explanation"),
        "peak_estimator_mu_c": observation_priority_bayes.get("peak_estimator_mu_c"),
        "peak_estimator_sigma_c": observation_priority_bayes.get("peak_estimator_sigma_c"),
        "peak_estimator_delta_to_candidate_c": observation_priority_bayes.get("peak_estimator_delta_to_candidate_c"),
        "observation_led_candidate_applied": bool(chosen_source == "observation_led_bayes"),
        "observation_led_candidate_not_applied_reason": (
            ""
            if chosen_source == "observation_led_bayes"
            else "diagnostic_only_until_explicit_temperature_path_switch"
        ),
    }
    out["differs_from_posterior_highest"] = bool(
        out.get("posterior_highest_bucket_label")
        and out.get("target_bucket")
        and out.get("posterior_highest_bucket_label") != out.get("target_bucket")
    )
    return out


def _cloud_growth(recent_payloads: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    points: List[Tuple[float, float]] = []
    for payload in recent_payloads:
        features = payload.get("weather_likelihood_features")
        if not isinstance(features, Mapping):
            continue
        cloud = _to_float(features.get("cloud_cover_pct"))
        ts = _parse_created_at(payload.get("_created_at"))
        if cloud is None or ts <= 0:
            continue
        points.append((ts, cloud))
    if len(points) < 2:
        return {
            "available": False,
            "state": "missing",
            "delta_pct": None,
            "conclusion": "云量变化序列不足，不能判断是否快速增加。",
        }
    points.sort(key=lambda item: item[0])
    delta = points[-1][1] - points[0][1]
    if delta >= 15.0:
        state = "cooling"
        conclusion = "云量快速增加，降温概率上调，中低温桶相对受益。"
    elif delta <= -15.0:
        state = "warming"
        conclusion = "云量明显下降，遮蔽减弱，升温概率上调。"
    else:
        state = "neutral"
        conclusion = "云量没有快速增加，暂不作为降温主证据。"
    return {
        "available": True,
        "state": state,
        "delta_pct": round(delta, 1),
        "start_pct": round(points[0][1], 1),
        "latest_pct": round(points[-1][1], 1),
        "conclusion": conclusion,
    }


def _precip_state(groups: Mapping[str, Any], features: Mapping[str, Any]) -> Dict[str, Any]:
    group = groups.get("precipitation_evaporative_cooling")
    profile: Mapping[str, Any] = {}
    if isinstance(group, Mapping):
        raw_profile = group.get("source_profile")
        profile = raw_profile if isinstance(raw_profile, Mapping) else {}
    evidence_state = str(
        (group.get("evidence_state") if isinstance(group, Mapping) else "")
        or profile.get("evidence_state")
        or ""
    ).strip()
    source_class = str(profile.get("source_class") or "").strip()
    source = str(profile.get("source") or "").strip()
    observed_rate = _to_float(features.get("observed_precip_rate_mm_per_h"))
    observed_raining = _truthy(features.get("is_raining_observed"))
    if evidence_state == "strong_no_precip" or _truthy(features.get("awc_metar_no_precip_observed")):
        state = "warming"
        observed_rate = 0.0
        conclusion = "AWC/METAR 明确无降水，雷达/降雨回波不成立，不应上调降温概率。"
    elif evidence_state in {"strong_precip", "precip", "strong_rain"} or observed_raining:
        state = "cooling"
        conclusion = "强降水或可验证实况降水成立，降温概率显著上调，高温桶下调。"
    elif observed_rate is not None and observed_rate > 0:
        state = "neutral"
        conclusion = "仅有代理降水字段，不能当作强雷达/实况降水；先作为弱观察，不单独下调高温桶。"
    else:
        state = "missing"
        conclusion = "强降水证据缺失，不能把代理降雨字段当作雷达回波。"
    return {
        "state": state,
        "evidence_state": evidence_state or ("strong_no_precip" if state == "warming" else "missing"),
        "source": source,
        "source_class": source_class,
        "rate_mm_per_h": observed_rate,
        "is_raining": features.get("is_raining"),
        "is_raining_observed": features.get("is_raining_observed"),
        "conclusion": conclusion,
    }


def _precip_nowcast_risk(features: Mapping[str, Any]) -> Dict[str, Any]:
    max_rate = _to_float(
        features.get("precip_nowcast_1_3h_max_rate_mm_per_h")
        if features.get("precip_nowcast_1_3h_max_rate_mm_per_h") is not None
        else features.get("forecast_precip_nowcast_1_3h_max_rate_mm_per_h")
    )
    total_mm = _to_float(
        features.get("precip_nowcast_1_3h_total_mm")
        if features.get("precip_nowcast_1_3h_total_mm") is not None
        else features.get("forecast_precip_nowcast_1_3h_total_mm")
    )
    max_prob = _to_float(
        features.get("precip_nowcast_1_3h_max_probability_pct")
        if features.get("precip_nowcast_1_3h_max_probability_pct") is not None
        else features.get("forecast_precip_nowcast_1_3h_max_probability_pct")
    )
    source = str(
        features.get("precip_nowcast_1_3h_source")
        or features.get("forecast_precip_nowcast_1_3h_source")
        or ""
    ).strip()
    checked_at = str(
        features.get("precip_nowcast_1_3h_checked_at")
        or features.get("forecast_precip_nowcast_1_3h_checked_at")
        or ""
    ).strip()
    nowcast_reference_time = str(
        features.get("precip_nowcast_1_3h_reference_time")
        or features.get("forecast_precip_nowcast_1_3h_reference_time")
        or ""
    ).strip()
    reference_time = str(
        features.get("weather_likelihood_refreshed_at")
        or features.get("zuuu_metar_raw_awc_latest_at")
        or features.get("wu_twc_observed_intraday_current_at")
        or features.get("_created_at")
        or nowcast_reference_time
        or ""
    ).strip()
    next_at = str(
        features.get("precip_nowcast_1_3h_next_precip_at")
        or features.get("forecast_precip_nowcast_1_3h_next_precip_at")
        or ""
    ).strip()
    window_hours = _to_float(
        features.get("precip_nowcast_1_3h_window_hours")
        if features.get("precip_nowcast_1_3h_window_hours") is not None
        else features.get("forecast_precip_nowcast_1_3h_window_hours")
    )
    max_age_hours = max(4.0, float(window_hours or 3.0) + 1.0)
    ref_dt = _parse_datetime(reference_time)
    checked_dt = _parse_datetime(checked_at)
    next_dt = _parse_datetime(next_at)
    available = (
        _truthy(features.get("precip_nowcast_1_3h_available"))
        or _truthy(features.get("forecast_precip_nowcast_1_3h_available"))
        or max_rate is not None
        or max_prob is not None
    )
    if not available:
        return {
            "state": "missing",
            "risk_state": "missing",
            "source": source,
            "checked_at": checked_at,
            "reference_time": reference_time,
            "nowcast_reference_time": nowcast_reference_time,
            "conclusion": "未来1-3小时降水 nowcast 风险源缺失；只能判断已发生降水/无降水，不能判断即将下雨。",
        }
    timing_state = "current"
    timing_na_reason = ""
    if ref_dt is not None and next_dt is not None and next_dt < ref_dt - timedelta(minutes=15):
        timing_state = "expired"
        timing_na_reason = "next_precip_at_before_reference_time"
    elif ref_dt is not None and checked_dt is not None and ref_dt - checked_dt > timedelta(hours=max_age_hours):
        timing_state = "stale"
        timing_na_reason = "checked_at_older_than_nowcast_window"
    elif ref_dt is not None and next_dt is not None and next_dt > ref_dt + timedelta(hours=max_age_hours):
        timing_state = "outside_window"
        timing_na_reason = "next_precip_at_outside_1_3h_window"
    if timing_state != "current":
        return {
            "state": "neutral",
            "risk_state": timing_state,
            "source": source or "precip_nowcast_1_3h",
            "checked_at": checked_at,
            "reference_time": reference_time,
            "nowcast_reference_time": nowcast_reference_time,
            "next_precip_at": next_at,
            "max_rate_mm_per_h": max_rate,
            "total_mm": total_mm,
            "max_probability_pct": max_prob,
            "timing_na_reason": timing_na_reason,
            "conclusion": "未来1-3小时降水 nowcast 时间窗口已失效或不在当前参考窗口内，不作为压低高温概率的冷却证据。",
        }
    if (max_rate is not None and max_rate >= 0.2) or (max_prob is not None and max_prob >= 50.0):
        state = "cooling"
        risk_state = "elevated"
        conclusion = "未来1-3小时降水 nowcast 风险升高，可能压制后续升温兑现；这是未来风险，不等于已发生降水。"
    elif (max_rate is not None and max_rate > 0.0) or (max_prob is not None and max_prob >= 25.0):
        state = "neutral"
        risk_state = "watch"
        conclusion = "未来1-3小时存在降水预报观察信号，先作为升温兑现风险提示，不当作强降水证据。"
    else:
        state = "neutral"
        risk_state = "none"
        conclusion = "未来1-3小时降水 nowcast 暂未提示明显降水风险；当前高温判断仍以温度轨迹为主。"
    return {
        "state": state,
        "risk_state": risk_state,
        "source": source or "precip_nowcast_1_3h",
        "checked_at": checked_at,
        "reference_time": reference_time,
        "nowcast_reference_time": nowcast_reference_time,
        "next_precip_at": next_at,
        "max_rate_mm_per_h": max_rate,
        "total_mm": total_mm,
        "max_probability_pct": max_prob,
        "timing_na_reason": timing_na_reason,
        "conclusion": conclusion,
    }


def _next_higher_bucket_group_label(target_label: Any, rows: Sequence[Mapping[str, Any]]) -> str:
    target_score = _bucket_rank_score(target_label)
    candidates: List[Tuple[int, float, str]] = []
    for row in rows or []:
        label = str(row.get("outcome") or row.get("label") or "").strip()
        if not label:
            continue
        score = _bucket_rank_score(label)
        temp = _bucket_temp(label)
        if score is None or temp is None:
            continue
        if target_score is not None and score <= target_score:
            continue
        if _is_upper_boundary_bucket(label):
            priority = 1
        elif _is_cumulative_bucket(label):
            continue
        else:
            priority = 0
        candidates.append((priority, float(temp), label))
    if not candidates:
        return "更高温桶"
    _, temp, _label = min(candidates, key=lambda item: (item[0], item[1]))
    text = f"{int(temp)}" if abs(temp - int(temp)) < 1e-9 else f"{temp:g}"
    return f"{text}°C+"


def _next_higher_bucket_temp(target_label: Any, rows: Sequence[Mapping[str, Any]]) -> Optional[float]:
    target_score = _bucket_rank_score(target_label)
    candidates: List[Tuple[int, float]] = []
    for row in rows or []:
        label = str(row.get("outcome") or row.get("label") or "").strip()
        score = _bucket_rank_score(label)
        temp = _bucket_temp(label)
        if score is None or temp is None:
            continue
        if target_score is not None and score <= target_score:
            continue
        if _is_upper_boundary_bucket(label):
            priority = 1
        elif _is_cumulative_bucket(label):
            continue
        else:
            priority = 0
        candidates.append((priority, float(temp)))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[1]


def _fmt_c(value: Any, decimals: int = 2) -> str:
    num = _to_float(value)
    if num is None:
        return "—"
    return f"{num:.{decimals}f}°C"


def _fmt_rate(value: Any) -> str:
    num = _to_float(value)
    if num is None:
        return "—"
    return f"{num:.2f}°C/h"


def _evidence_conclusions(
    payload: Mapping[str, Any],
    recent_payloads: Sequence[Mapping[str, Any]],
    high_temp_guard: Optional[Mapping[str, Any]] = None,
    temperature_trend: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        features = {}
    groups = payload.get("weather_likelihood_feature_groups")
    if not isinstance(groups, Mapping):
        groups = {}
    cloud = _to_float(features.get("cloud_cover_pct"))
    low_cloud = _to_float(features.get("cloud_cover_low_pct"))
    solar = _to_float(features.get("solar_radiation"))
    dew = _to_float(features.get("dewpoint_c"))
    rise = _temperature_rise_rate_value(features)
    observed = _to_float(features.get("observed_temp_c"))
    observed_max, _observed_max_source, _observed_max_na_reason = _hard_observed_today_max_c(features)
    peak_mu = _to_float(features.get("peak_estimator_mu_c"))
    forecast_peak = _to_float(features.get("forecast_peak_temp"))

    clear_warm = (
        (cloud is not None and cloud <= 30.0)
        and (low_cloud is None or low_cloud <= 10.0)
        and (solar is None or solar >= 350.0)
        and (dew is None or dew <= 20.0)
    )
    if clear_warm:
        clear_state = "warming"
        clear_conclusion = "晴空/低云量/低露点条件成立，代表升温物理潜力；是否放大高温桶必须看实况温度轨迹是否兑现。"
    elif cloud is not None and cloud >= 60.0:
        clear_state = "cooling"
        clear_conclusion = "云量偏高，短波入射受压，升温概率下调。"
    else:
        clear_state = "neutral"
        clear_conclusion = "晴空低云证据不够强，暂不单独上调高温桶。"

    cloud_growth = _cloud_growth(recent_payloads)
    precip = _precip_state(groups, features)
    precip_nowcast = _precip_nowcast_risk(features)
    physical_modifier = _physical_weather_modifier(features, groups)

    if dew is not None and dew >= 22.0:
        dew_state = "cooling"
        dew_conclusion = "露点偏高，云雨/对流与湿球约束风险上调，高温桶下调。"
    elif dew is not None and dew <= 20.0:
        dew_state = "warming"
        dew_conclusion = "露点不高，湿球约束不强，不构成高温桶压制。"
    else:
        dew_state = "neutral"
        dew_conclusion = "露点处于中性区间，对桶概率影响有限。"

    trajectory_weak = False
    if rise is not None and rise <= 0.0:
        trajectory_weak = True
    if peak_mu is not None and forecast_peak is not None and forecast_peak - peak_mu >= 2.0:
        trajectory_weak = True
    trend_state = str((temperature_trend or {}).get("latest_trend_state") or "")
    momentum = (
        temperature_trend.get("temperature_momentum")
        if isinstance(temperature_trend, Mapping) and isinstance(temperature_trend.get("temperature_momentum"), Mapping)
        else {}
    )
    momentum_state = str(momentum.get("state") or "missing")
    momentum_rates = momentum.get("rates_c_per_hour") if isinstance(momentum.get("rates_c_per_hour"), Mapping) else {}
    if momentum_state in {"cooling", "not_realized", "flat"}:
        momentum_evidence_state = "cooling"
    elif momentum_state in {"sustained_warming", "emerging_warming", "weak_warming"}:
        momentum_evidence_state = "warming"
    else:
        momentum_evidence_state = "neutral"
    guard_active = bool((high_temp_guard or {}).get("active"))
    current_below_observed_max = (
        observed is not None
        and observed_max is not None
        and math.isfinite(float(observed))
        and math.isfinite(float(observed_max))
        and float(observed_max) - float(observed) >= 0.75
    )
    if guard_active:
        trajectory_state = "cooling"
        trajectory_conclusion = str((high_temp_guard or {}).get("conclusion") or "温度轨迹触发高温桶放大禁止。")
    elif current_below_observed_max:
        trajectory_state = "neutral"
        trajectory_conclusion = (
            "硬实况已记录日内高点：当前包装源 "
            f"{_fmt_c(observed)}，已观测最高 {_fmt_c(observed_max)}，峰值估计 {_fmt_c(peak_mu)}；"
            "已达到的桶按实况下界处理，更高桶才需要后续重新升温兑现。"
        )
    elif momentum_state in {"cooling", "not_realized", "flat"}:
        trajectory_state = "cooling"
        trajectory_conclusion = (
            "温度轨迹已经变弱：Temperature Momentum 未兑现，已观测最高 "
            f"{_fmt_c(observed_max)}，峰值估计 {_fmt_c(peak_mu)}；高温桶不能单靠晴空/无雨放大。"
        )
    elif trajectory_weak:
        trajectory_state = "cooling"
        trajectory_conclusion = "实况温度轨迹偏弱，能量收支峰值低于软预报，高温桶需要后续升温重新确认。"
    elif trend_state == "weakening":
        trajectory_state = "cooling"
        trajectory_conclusion = "小时级升温趋势变弱，高温桶需要下一小时重新确认。"
    elif trend_state == "strengthening":
        trajectory_state = "warming"
        trajectory_conclusion = "小时级升温趋势变强，温度轨迹支持继续上探。"
    elif rise is not None and rise >= 1.0:
        trajectory_state = "warming"
        trajectory_conclusion = "实况升温率较强，温度轨迹支持继续上探。"
    else:
        trajectory_state = "neutral"
        trajectory_conclusion = "温度轨迹暂未给出强方向。"

    return [
        {
            "key": "clear_low_cloud_low_dewpoint",
            "label": "晴空/低云/低露点",
            "state": clear_state,
            "evidence_class": "physical_potential",
            "conclusion": clear_conclusion,
            "metrics": {
                "cloud_cover_pct": _round(cloud, 1),
                "cloud_cover_low_pct": _round(low_cloud, 1),
                "solar_radiation": _round(solar, 1),
                "dewpoint_c": _round(dew, 1),
            },
        },
        {
            "key": "cloud_growth",
            "label": "云量快速变化",
            "state": cloud_growth["state"],
            "evidence_class": "physical_potential",
            "conclusion": str(cloud_growth["conclusion"]),
            "metrics": {k: cloud_growth.get(k) for k in ("start_pct", "latest_pct", "delta_pct") if k in cloud_growth},
        },
        {
            "key": "radar_precip_echo",
            "label": "雷达/降水回波",
            "state": precip["state"],
            "evidence_class": "physical_potential",
            "conclusion": precip["conclusion"],
            "metrics": {
                "evidence_state": precip["evidence_state"],
                "source": precip["source"],
                "source_class": precip["source_class"],
                "rate_mm_per_h": _round(precip["rate_mm_per_h"], 2),
                "is_raining": precip["is_raining"],
                "is_raining_observed": precip["is_raining_observed"],
            },
        },
        {
            "key": "precip_nowcast_1_3h",
            "label": "未来1-3小时降水nowcast",
            "state": precip_nowcast["state"],
            "evidence_class": "nowcast_risk",
            "conclusion": precip_nowcast["conclusion"],
            "metrics": {
                "risk_state": precip_nowcast.get("risk_state"),
                "source": precip_nowcast.get("source"),
                "checked_at": precip_nowcast.get("checked_at"),
                "next_precip_at": precip_nowcast.get("next_precip_at"),
                "max_rate_mm_per_h": _round(precip_nowcast.get("max_rate_mm_per_h"), 2),
                "total_mm": _round(precip_nowcast.get("total_mm"), 2),
                "max_probability_pct": _round(precip_nowcast.get("max_probability_pct"), 1),
            },
        },
        {
            "key": "physical_weather_modifier",
            "label": "云雨/低云物理修正",
            "state": physical_modifier.get("state"),
            "evidence_class": "intraday_likelihood",
            "conclusion": (
                f"{physical_modifier.get('label') or '物理天气'}：峰值路径修正 {_fmt_c(physical_modifier.get('peak_mu_adjustment_c'), 2)}；"
                "来自 AWC/METAR 与 WU/TWC 云、雨、阵雨、低云、辐射和露点，不替代温度硬实况。"
                if physical_modifier.get("available")
                else "云雨/低云物理修正缺少可用输入，暂不参与峰值路径调整。"
            ),
            "metrics": {
                "peak_mu_adjustment_c": physical_modifier.get("peak_mu_adjustment_c"),
                "path_probability_scale": physical_modifier.get("path_probability_scale"),
                "cloud_cover_pct": physical_modifier.get("cloud_cover_pct"),
                "cloud_cover_low_pct": physical_modifier.get("cloud_cover_low_pct"),
                "dewpoint_c": physical_modifier.get("dewpoint_c"),
                "precipitation_state": (
                    physical_modifier.get("precipitation_profile", {}).get("evidence_state")
                    if isinstance(physical_modifier.get("precipitation_profile"), Mapping)
                    else None
                ),
                "component_count": len(physical_modifier.get("components") or []),
            },
        },
        {
            "key": "high_dewpoint",
            "label": "高露点/湿球",
            "state": dew_state,
            "evidence_class": "physical_potential",
            "conclusion": dew_conclusion,
            "metrics": {"dewpoint_c": _round(dew, 1)},
        },
        {
            "key": "temperature_momentum",
            "label": "Temperature Momentum 温度动量",
            "state": momentum_evidence_state,
            "evidence_class": "observed_realization",
            "conclusion": str(momentum.get("conclusion") or "多窗口温度动量用于判断升温潜力是否正在兑现。"),
            "metrics": {
                **(_temperature_momentum_metric_fields(momentum_rates) if isinstance(momentum_rates, Mapping) else {}),
                "momentum_rates_c_per_hour": {
                    key: _round(momentum_rates.get(key), 2)
                    for key in TEMPERATURE_MOMENTUM_WINDOWS
                    if isinstance(momentum_rates, Mapping) and momentum_rates.get(key) is not None
                },
                "effective_rate_c_per_hour": _round(momentum.get("effective_rate_c_per_hour"), 2),
                "realization_state": momentum_state,
                "sustained_warming": bool(momentum.get("sustained_warming")),
                "source": momentum.get("source"),
            },
        },
        {
            "key": "temperature_trajectory",
            "label": "温度轨迹",
            "state": trajectory_state,
            "evidence_class": "observed_realization",
            "conclusion": trajectory_conclusion,
            "metrics": {
                "observed_temp_c": _round(observed, 2),
                "observed_today_max_c": _round(observed_max, 2),
                "rise_rate_c_per_hour": _round(rise, 2),
                "peak_estimator_mu_c": _round(peak_mu, 2),
                "forecast_peak_temp_c": _round(forecast_peak, 2),
                "hourly_trend_state": trend_state or None,
            },
        },
    ]


def _state_counts(evidence: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in evidence:
        state = str(item.get("state") or "neutral")
        counts[state] = counts.get(state, 0) + 1
    return counts


def _bucket_distance(a: Any, b: Any) -> Optional[float]:
    a_score = _bucket_rank_score(a)
    b_score = _bucket_rank_score(b)
    if a_score is None or b_score is None:
        return None
    return abs(float(a_score) - float(b_score))


def _bucket_directional_distance(a: Any, b: Any) -> Optional[float]:
    a_score = _bucket_rank_score(a)
    b_score = _bucket_rank_score(b)
    if a_score is None or b_score is None:
        return None
    return float(a_score) - float(b_score)


def _remote_no_defense_candidate(
    rows: Sequence[Mapping[str, Any]],
    target_label: Any,
    second_target_label: Any = "",
) -> Dict[str, Any]:
    target = str(target_label or "").strip()
    second = str(second_target_label or "").strip()
    min_raw = _to_float(os.environ.get("PM_TARGET_REALIZATION_REMOTE_NO_MIN_BUCKET_DISTANCE"))
    max_raw = _to_float(os.environ.get("PM_TARGET_REALIZATION_REMOTE_NO_MAX_BUCKET_DISTANCE"))
    if min_raw is None or not math.isfinite(float(min_raw)):
        min_raw = 2.0
    remote_min_distance = int(max(1, min(20, round(float(min_raw)))))
    if max_raw is None or not math.isfinite(float(max_raw)):
        max_raw = 3.0
    remote_max_distance = int(max(remote_min_distance, min(20, round(float(max_raw)))))
    candidates: List[Dict[str, Any]] = []
    for raw in rows or []:
        label = str(raw.get("outcome") or raw.get("label") or "").strip()
        if not label or label == target or (second and label == second):
            continue
        q_target, q_field = _target_bucket_probability(raw)
        distance = _bucket_distance(label, target)
        directional_distance = _bucket_directional_distance(label, target)
        second_distance = _bucket_distance(label, second) if second else None
        if distance is None or directional_distance is None:
            continue
        directional_bucket_distance = int(round(float(directional_distance)))
        if not (remote_min_distance <= directional_bucket_distance <= remote_max_distance):
            continue
        no_ask = _price_probability(raw.get("best_ask_no"))
        no_bid = _price_probability(raw.get("best_bid_no"))
        yes_ask = _price_probability(raw.get("best_ask"))
        yes_bid = _price_probability(raw.get("best_bid"))
        if no_ask is None and yes_bid is not None:
            no_ask = max(0.0, min(1.0, 1.0 - yes_bid))
        if no_bid is None and yes_ask is not None:
            no_bid = max(0.0, min(1.0, 1.0 - yes_ask))
        q_value = float(q_target) if q_target is not None else 1.0
        min_distance = min(distance, second_distance) if second_distance is not None else distance
        # Prefer buckets clearly away from both target legs and with low posterior
        # realization probability. Price is only execution context, not target choice.
        distance_bonus = min(4.0, max(0.0, float(min_distance)))
        score = distance_bonus * 0.35 + (1.0 - min(1.0, max(0.0, q_value))) * 0.65
        candidates.append(
            {
                "bucket": label,
                "outcome": label,
                "remote_no_bucket": label,
                "remote_bucket_realization_prob": q_value if q_target is not None else None,
                "remote_bucket_realization_pct": _pct(q_value) if q_target is not None else None,
                "target_probability_field": q_field or None,
                "target_distance": _round(distance, 2),
                "second_target_distance": _round(second_distance, 2),
                "distance_from_active_legs": _round(min_distance, 2),
                "remote_no_directional_bucket_distance": directional_bucket_distance,
                "remote_no_min_bucket_distance": remote_min_distance,
                "remote_no_max_bucket_distance": remote_max_distance,
                "best_ask_no": no_ask,
                "best_bid_no": no_bid,
                "score": _round(score, 4),
                "enabled": bool(q_value <= 0.20),
            }
        )
    candidates.sort(
        key=lambda item: (
            int(bool(item.get("enabled"))),
            float(item.get("score") or -1.0),
            float(item.get("distance_from_active_legs") or 0.0),
        ),
        reverse=True,
    )
    best = candidates[0] if candidates else {}
    if not best:
        return {
            "available": False,
            "remote_no_bucket": "",
            "reason": "no_above_remote_bucket_candidate_in_distance_window",
            "remote_no_min_bucket_distance": remote_min_distance,
            "remote_no_max_bucket_distance": remote_max_distance,
            "version": "remote_no_defense_candidate_v1",
        }
    reason = (
        f"目标桶 {target or '—'} 上方 +{int(best.get('remote_no_directional_bucket_distance') or 0)} 桶"
        f"{('/第二目标 ' + second) if second else ''}，"
        f"该桶后验兑现概率 {_round(best.get('remote_bucket_realization_pct'), 1)}%，"
        "只作为 NO 防守腿；价格只用于限价/预算。"
    )
    return {
        "available": True,
        "remote_no_bucket": best.get("remote_no_bucket"),
        "remote_bucket_realization_prob": best.get("remote_bucket_realization_prob"),
        "remote_bucket_realization_pct": best.get("remote_bucket_realization_pct"),
        "remote_no_reason": reason,
        "enabled": bool(best.get("enabled")),
        "candidate": best,
        "candidates": candidates[:5],
        "remote_no_min_bucket_distance": remote_min_distance,
        "remote_no_max_bucket_distance": remote_max_distance,
        "selection_rule": "target-above +2/+3 buckets only; exclude target/second target; prefer low final-posterior realization probability",
        "version": "remote_no_defense_candidate_v1",
    }


def _sell_old_buy_new_position_legs(
    *,
    target_label: str,
    target_side: str,
    primary_action: str,
    active_position_legs: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    target = str(target_label or "").strip()
    side = str(target_side or "YES").strip().upper()
    if side not in {"YES", "NO"}:
        side = "YES"
    positions: List[Dict[str, Any]] = []
    for raw in active_position_legs or []:
        if not isinstance(raw, Mapping):
            continue
        outcome = str(raw.get("outcome") or raw.get("bucket") or "").strip()
        pos_side = str(raw.get("side") or "").strip().upper()
        size = _to_float(raw.get("position_size") or raw.get("size") or raw.get("current_size"))
        if not outcome or pos_side not in {"YES", "NO"} or size is None or size <= 0:
            continue
        avg = _to_float(raw.get("avg_price") or raw.get("entry_price"))
        positions.append(
            {
                "outcome": outcome,
                "side": pos_side,
                "position_size": float(size),
                "avg_price": avg,
                "current_notional": (
                    float(size) * float(avg)
                    if avg is not None
                    else _to_float(raw.get("current_notional") or raw.get("notional"))
                ),
                "state": str(raw.get("state") or ""),
                "source": str(raw.get("source") or "edge_trades"),
                "updated_at": str(raw.get("updated_at") or ""),
                "entry_at": str(raw.get("entry_at") or ""),
            }
        )

    def leg(
        *,
        group: str,
        role: str,
        execution_action: str,
        outcome: str,
        leg_side: str,
        position_effect: str,
        source_position: Optional[Mapping[str, Any]] = None,
        paired_exit: Optional[Mapping[str, Any]] = None,
        enabled: bool = True,
        reason: str = "",
    ) -> Dict[str, Any]:
        src = source_position if isinstance(source_position, Mapping) else {}
        paired = paired_exit if isinstance(paired_exit, Mapping) else {}
        return {
            "leg_group": group,
            "target_realization_leg_role": role,
            "target_realization_execution_action": execution_action,
            "execution_action": execution_action,
            "outcome": outcome,
            "side": leg_side,
            "target_realization_position_effect": position_effect,
            "position_effect": position_effect,
            "sell_old_buy_new_semantics": {
                "sell_old": "sell_old_position_side",
                "buy_new": "buy_new_bucket_side",
                "buy_old_reverse": "buy_old_bucket_opposite_side",
            }.get(group, group),
            "leg_enabled": bool(enabled),
            "leg_reason": reason,
            "position_size": src.get("position_size"),
            "avg_price": src.get("avg_price"),
            "current_notional": src.get("current_notional"),
            "position_source": src.get("source"),
            "position_state": src.get("state"),
            "position_updated_at": src.get("updated_at"),
            "paired_exit_outcome": paired.get("outcome"),
            "paired_exit_side": paired.get("side"),
            "paired_exit_execution_action": "SELL" if paired else None,
            "held_target_bucket": paired.get("outcome"),
            "held_target_side": paired.get("side"),
        }

    active_same_target = [
        item for item in positions
        if str(item.get("outcome") or "") == target and str(item.get("side") or "").upper() == side
    ]
    old_positions = [
        item for item in positions
        if not (str(item.get("outcome") or "") == target and str(item.get("side") or "").upper() == side)
    ]
    buy_enabled = bool(
        target
        and "撤回/退出" not in primary_action
        and "撤回/降仓" not in primary_action
        and "不追" not in primary_action
    )
    buy_new_leg = (
        leg(
            group="buy_new",
            role="new_bucket_entry",
            execution_action="BUY",
            outcome=target,
            leg_side=side,
            position_effect="open_or_add_new_bucket_side",
            enabled=buy_enabled,
            reason=(
                "target_side_already_held"
                if active_same_target
                else ("primary_action_allows_new_bucket" if buy_enabled else "primary_action_blocks_new_bucket")
            ),
        )
        if target
        else None
    )
    sell_old_legs: List[Dict[str, Any]] = []
    reverse_legs: List[Dict[str, Any]] = []
    seen_buy_keys: Set[Tuple[str, str]] = {(target, side)} if target else set()
    for pos in old_positions:
        old_outcome = str(pos.get("outcome") or "")
        old_side = str(pos.get("side") or "").upper()
        sell_old_legs.append(
            leg(
                group="sell_old",
                role="old_bucket_exit",
                execution_action="SELL",
                outcome=old_outcome,
                leg_side=old_side,
                position_effect="close_old_bucket_side",
                source_position=pos,
                enabled=True,
                reason="active_position_differs_from_target_bucket",
            )
        )
        reverse_side = _opposite_trade_side(old_side)
        if not reverse_side:
            continue
        reverse_key = (old_outcome, reverse_side)
        if reverse_key in seen_buy_keys:
            if buy_new_leg is not None:
                buy_new_leg["also_old_bucket_reverse"] = True
                buy_new_leg["paired_exit_outcome"] = old_outcome
                buy_new_leg["paired_exit_side"] = old_side
                buy_new_leg["paired_exit_execution_action"] = "SELL"
                buy_new_leg["held_target_bucket"] = old_outcome
                buy_new_leg["held_target_side"] = old_side
            continue
        seen_buy_keys.add(reverse_key)
        reverse_legs.append(
            leg(
                group="buy_old_reverse",
                role="old_bucket_reverse",
                execution_action="BUY",
                outcome=old_outcome,
                leg_side=reverse_side,
                position_effect="open_or_add_old_bucket_opposite_side",
                paired_exit=pos,
                enabled=buy_enabled,
                reason=(
                    "old_bucket_opposite_side_after_target_switch"
                    if buy_enabled
                    else "primary_action_blocks_old_bucket_reverse"
                ),
            )
        )
    all_legs: List[Dict[str, Any]] = []
    all_legs.extend(sell_old_legs)
    if buy_new_leg is not None:
        all_legs.append(buy_new_leg)
    all_legs.extend(reverse_legs)
    return {
        "position_legs_resolved": True,
        "sell_old_buy_new_legs_source": "edge_trades.active_positions",
        "active_position_legs": positions,
        "active_old_position_count": len(old_positions),
        "active_target_position_count": len(active_same_target),
        "sell_old_legs": sell_old_legs,
        "sell_old_leg": sell_old_legs[0] if sell_old_legs else None,
        "buy_new_legs": [buy_new_leg] if buy_new_leg is not None else [],
        "buy_new_leg": buy_new_leg,
        "buy_old_reverse_legs": reverse_legs,
        "buy_old_reverse_leg": reverse_legs[0] if reverse_legs else None,
        "sell_old_buy_new_legs": all_legs,
        "sell_old_buy_new_missing_reason": "" if positions else "no_active_position",
        "no_active_old_position_reason": "" if old_positions else "no_active_old_position_to_sell",
    }


def _target_bucket_trade_plan(
    target_candidates: Sequence[Mapping[str, Any]],
    reliability: Optional[Mapping[str, Any]],
    high_temp_guard: Optional[Mapping[str, Any]] = None,
    precip_cooling: bool = False,
    trajectory_suppression: bool = False,
    all_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    active_position_legs: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    rel = reliability if isinstance(reliability, Mapping) else {}
    target = target_candidates[0] if target_candidates else {}
    target_label = str(target.get("outcome") or rel.get("target_bucket") or "—")
    target_text = target_label.lower()
    target_is_lower_boundary = "or below" in target_text or target_text.startswith("<=") or "below" in target_text
    target_temp = _bucket_temp(target_label)
    state = str(rel.get("state") or "neutral")
    warming_label = str(rel.get("warming_label") or "升温支持目标桶兑现")
    cooling_label = str(rel.get("cooling_label") or "受阻/降温压制目标桶")
    realization_mode = str(rel.get("realization_mode") or "")
    warm_pct = _to_float(rel.get("target_bucket_warming_realization_pct"))
    if warm_pct is None:
        warm_pct = _to_float(rel.get("warming_realization_pct"))
    blocked_pct = _to_float(rel.get("target_bucket_blocked_realization_pct"))
    if blocked_pct is None:
        blocked_pct = _to_float(rel.get("cooling_realization_pct"))
    neutral_pct = _to_float(rel.get("neutral_or_uncertain_pct"))
    confidence_pct = _to_float(rel.get("confidence_pct"))
    guard_active = bool((high_temp_guard or {}).get("active"))
    target_side = str(
        target.get("target_entry_side")
        or target.get("new_target_side")
        or target.get("side")
        or "YES"
    ).strip().upper()
    if target_side not in {"YES", "NO"}:
        target_side = "YES"

    if state == "exceeded":
        primary_action = "撤回/退出"
        target_status = "目标桶已被突破"
        summary = f"目标 {target_label} 已被硬实况突破，不再把它作为可兑现目标。"
    elif state == "realized":
        primary_action = "持有/观望"
        if "soft_display" in realization_mode:
            target_status = "目标桶软触达"
            summary = f"目标 {target_label} 已由 WU/TWC 软展示源触达；等待 AWC Raw METAR 同桶确认，避免主动追价。"
        else:
            target_status = "目标桶已触达"
            summary = f"目标 {target_label} 已由硬实况触达；后续重点看是否突破更高桶，不把降温解读为目标受阻。"
    elif target_is_lower_boundary and (guard_active or precip_cooling or trajectory_suppression):
        primary_action = "持有/观望"
        target_status = "低边界仍受支持"
        if trajectory_suppression and not precip_cooling:
            summary = f"{target_label} 暂未被温度轨迹突破；已观测最高和峰值估计仍压在下一桶附近以下，继续观察全天最高温。"
        else:
            summary = f"{target_label} 暂未被突破；更高桶受实况门控或降水压制，继续观察全天最高温是否越过边界。"
    elif target_is_lower_boundary and (state == "cooling" or (blocked_pct or 0.0) >= (warm_pct or 0.0) + 8.0):
        primary_action = "持有/观望"
        target_status = "低边界守住概率上升"
        summary = f"目标 {target_label} 受温度动量偏弱/降温侧支持；不要把降温理解为目标受阻，风险在后续升温突破边界。"
    elif target_is_lower_boundary and state == "warming":
        primary_action = "撤回/降仓"
        target_status = "低边界突破风险上升"
        summary = f"目标 {target_label} 面临升温突破风险，降低追随强度，只保留被动观察。"
    elif guard_active or precip_cooling:
        primary_action = "撤回/降仓"
        target_status = "目标桶兑现受阻"
        summary = f"目标 {target_label} 受实况轨迹或强降水压制，撤回主动追价，只挂被动限价观察。"
    elif state == "warming" and (warm_pct or 0.0) >= 48.0 and (warm_pct or 0.0) >= (blocked_pct or 0.0) + 8.0:
        primary_action = "谨慎加仓"
        target_status = "目标桶兑现中"
        summary = f"目标 {target_label} 的温度轨迹正在兑现，可只用被动限价小幅加仓；动量断档立即撤回。"
    elif state == "cooling" or (blocked_pct or 0.0) >= (warm_pct or 0.0) + 8.0:
        primary_action = "撤回/降仓"
        target_status = "目标桶兑现受阻"
        summary = f"目标 {target_label} 的受阻概率高于升温兑现概率，降低高温桶追随强度。"
    else:
        primary_action = "观望"
        target_status = "目标桶兑现待确认"
        summary = f"目标 {target_label} 暂未形成足够可靠的兑现确认，只看被动价差，不主动追价。"

    buckets: List[Dict[str, Any]] = []
    for index, raw in enumerate(target_candidates[:3]):
        label = str(raw.get("outcome") or "")
        temp = _bucket_temp(label)
        q_target, q_target_field = _target_bucket_probability(raw)
        role = "目标桶" if index == 0 else "观察桶"
        if index == 0:
            action = primary_action
            status = target_status
            reason = summary
        elif target_temp is not None and temp is not None and temp > target_temp:
            if primary_action == "谨慎加仓":
                action = "观望"
                status = "更高桶需继续确认"
                reason = f"{label} 高于当前目标 {target_label}，只有温度动量继续增强才考虑。"
            else:
                action = "撤回/不追"
                status = "更高桶兑现不足"
                reason = f"{label} 需要更强升温兑现；当前目标桶可靠性不足，不追高。"
        elif target_temp is not None and temp is not None and temp < target_temp:
            action = "观望"
            status = "低一档防守桶"
            reason = f"{label} 可作为温度动量转弱时的防守观察，不替代目标桶判断。"
        else:
            action = "观望"
            status = "观察"
            reason = f"{label} 暂按观察桶处理，等待目标桶兑现可靠性变化。"
        buckets.append(
            {
                "outcome": label,
                "role": role,
                "status": status,
                "action": action,
                "reason": reason,
                "target_probability_pct": _pct(q_target) if q_target is not None else None,
                "target_probability_field": q_target_field or None,
                "q_calibrated_pct": _round(raw.get("q_calibrated_pct"), 2),
                "q_bayes_posterior_pct": _round(raw.get("q_bayes_posterior_pct"), 2),
            }
        )

    second_target_label = str(target_candidates[1].get("outcome") or "") if len(target_candidates) > 1 else ""
    remote_no = _remote_no_defense_candidate(all_rows or target_candidates, target_label, second_target_label)
    position_legs = _sell_old_buy_new_position_legs(
        target_label=target_label,
        target_side=target_side,
        primary_action=primary_action,
        active_position_legs=active_position_legs or [],
    )

    return {
        "version": "target_bucket_realization_trade_plan_v2",
        "target_bucket": target_label,
        "target_side": target_side,
        "target_status": target_status,
        "primary_action": primary_action,
        "summary": summary,
        "warming_support_pct": _round(warm_pct, 2),
        "blocked_or_cooling_pct": _round(blocked_pct, 2),
        "neutral_or_uncertain_pct": _round(neutral_pct, 2),
        "neutral_label": str(rel.get("neutral_label") or "中性/不确定"),
        "confidence_pct": _round(confidence_pct, 2),
        "warming_label": warming_label,
        "cooling_label": cooling_label,
        "realization_mode": realization_mode or None,
        "basis": [
            (
                f"目标桶兑现可靠性：{warming_label} {_round(warm_pct, 1)}%，"
                f"{cooling_label} {_round(blocked_pct, 1)}%，"
                f"中性/不确定 {_round(neutral_pct, 1)}%，置信 {_round(confidence_pct, 1)}%。"
            ),
            "兑现判断以实况温度轨迹为锚；云雨、辐射、露点作为辅助因子。",
            "所有加仓建议都限定为被动限价，不代表主动追价。",
        ],
        "buckets": buckets,
        **position_legs,
        "remote_no_bucket": remote_no.get("remote_no_bucket"),
        "remote_bucket_realization_prob": remote_no.get("remote_bucket_realization_prob"),
        "remote_bucket_realization_pct": remote_no.get("remote_bucket_realization_pct"),
        "remote_no_reason": remote_no.get("remote_no_reason") or remote_no.get("reason"),
        "remote_no_candidate": remote_no,
    }


def _target_trade_plan_candidates_from_selection(
    target_selection: Optional[Mapping[str, Any]],
    fallback_candidates: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    selected = target_selection if isinstance(target_selection, Mapping) else {}
    candidates: List[Dict[str, Any]] = []
    seen: set[str] = set()
    selected_target_row = selected.get("target_row")
    primary_label = str(
        (
            selected_target_row.get("outcome")
            if isinstance(selected_target_row, Mapping)
            else selected.get("target_bucket")
        )
        or ""
    ).strip()

    def add(raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        label = str(raw.get("outcome") or raw.get("target_bucket") or raw.get("bucket") or "").strip()
        if not label or label in seen:
            return
        row = dict(raw)
        row.setdefault("outcome", label)
        candidates.append(row)
        seen.add(label)

    add(selected.get("target_row"))
    for raw in selected.get("watch_buckets") if isinstance(selected.get("watch_buckets"), list) else []:
        add(raw)
    for raw in fallback_candidates:
        add(raw)

    if candidates:
        primary = candidates[0]
        primary_temp = _bucket_temp(primary.get("outcome"))

        def _plan_candidate_sort(row: Mapping[str, Any]) -> Tuple[int, float, float]:
            label = str(row.get("outcome") or "")
            if label == str(primary.get("outcome") or ""):
                return (0, 0.0, 0.0)
            temp = _bucket_temp(label)
            if primary_temp is not None and temp is not None and temp > float(primary_temp):
                return (1, float(temp) - float(primary_temp), -float(_target_bucket_probability(row)[0] or 0.0))
            if primary_temp is not None and temp is not None and temp < float(primary_temp):
                return (2, float(primary_temp) - float(temp), -float(_target_bucket_probability(row)[0] or 0.0))
            return (3, 999.0, -float(_target_bucket_probability(row)[0] or 0.0))

        candidates = [primary] + sorted(candidates[1:], key=_plan_candidate_sort)
        return candidates

    label = primary_label or str(selected.get("target_bucket") or "").strip()
    if not label:
        return []
    row: Dict[str, Any] = {"outcome": label}
    prob_pct = _to_float(selected.get("target_probability_pct"))
    if prob_pct is not None:
        row["target_bucket_probability"] = max(0.0, min(1.0, float(prob_pct) / 100.0))
        row["target_bucket_probability_pct"] = _pct(row["target_bucket_probability"])
        row["target_bucket_probability_field"] = str(
            selected.get("target_probability_field") or "target_selection.target_probability_pct"
        )
    return [row]


def _decision_text(
    evidence: Sequence[Mapping[str, Any]],
    top_single: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    high_temp_guard: Optional[Mapping[str, Any]] = None,
    temperature_trend: Optional[Mapping[str, Any]] = None,
    realization_reliability: Optional[Mapping[str, Any]] = None,
    target_selection: Optional[Mapping[str, Any]] = None,
    active_position_legs: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    counts = _state_counts(evidence)
    target_candidates = _target_trade_plan_candidates_from_selection(target_selection, top_single)
    top = target_candidates[0] if target_candidates else {}
    top_label = str(top.get("outcome") or "—")
    warming = counts.get("warming", 0)
    cooling = counts.get("cooling", 0)
    strong_no_precip = any(
        item.get("key") == "radar_precip_echo"
        and item.get("metrics", {}).get("evidence_state") == "strong_no_precip"
        for item in evidence
    )
    trajectory_cooling = any(item.get("key") == "temperature_trajectory" and item.get("state") == "cooling" for item in evidence)
    precip_cooling = any(item.get("key") == "radar_precip_echo" and item.get("state") == "cooling" for item in evidence)
    future_precip_cooling = any(
        item.get("key") == "precip_nowcast_1_3h" and item.get("state") == "cooling"
        for item in evidence
    )
    trajectory_item = next((item for item in evidence if item.get("key") == "temperature_trajectory"), {})
    trajectory_metrics = (
        trajectory_item.get("metrics")
        if isinstance(trajectory_item, Mapping) and isinstance(trajectory_item.get("metrics"), Mapping)
        else {}
    )
    observed_max = _to_float(trajectory_metrics.get("observed_today_max_c"))
    observed_temp = _to_float(trajectory_metrics.get("observed_temp_c"))
    peak_mu = _to_float(trajectory_metrics.get("peak_estimator_mu_c"))
    higher_group = _next_higher_bucket_group_label(top_label, rows)
    higher_temp = _next_higher_bucket_temp(top_label, rows)
    rel = realization_reliability if isinstance(realization_reliability, Mapping) else {}
    rel_state = str(rel.get("state") or "")
    top_is_lower_boundary = _is_lower_boundary_bucket(top_label)

    guard_active = bool((high_temp_guard or {}).get("active"))
    trend_state = str((temperature_trend or {}).get("latest_trend_state") or "")
    momentum = (
        temperature_trend.get("temperature_momentum")
        if isinstance(temperature_trend, Mapping) and isinstance(temperature_trend.get("temperature_momentum"), Mapping)
        else {}
    )
    momentum_state = str(momentum.get("state") or "missing")
    momentum_rates = momentum.get("rates_c_per_hour") if isinstance(momentum.get("rates_c_per_hour"), Mapping) else {}
    momentum_blocking = momentum_state in {"cooling", "not_realized", "flat"} or rel_state == "cooling"
    current_below_observed_max = (
        observed_temp is not None
        and observed_max is not None
        and math.isfinite(float(observed_temp))
        and math.isfinite(float(observed_max))
        and float(observed_max) - float(observed_temp) >= 0.75
    )
    higher_gap_from_observed = (
        float(higher_temp) - float(observed_max)
        if higher_temp is not None and observed_max is not None and math.isfinite(float(observed_max))
        else None
    )
    higher_gap_from_peak = (
        float(higher_temp) - float(peak_mu)
        if higher_temp is not None and peak_mu is not None and math.isfinite(float(peak_mu))
        else None
    )
    lower_boundary_suppressed_by_track = bool(
        top_is_lower_boundary
        and higher_temp is not None
        and (
            current_below_observed_max
            or (higher_gap_from_observed is not None and higher_gap_from_observed >= 2.0)
            or (higher_gap_from_peak is not None and higher_gap_from_peak >= 1.25)
        )
    )
    if guard_active:
        stance = f"{top_label} 优先观察" if top_is_lower_boundary else "实况门控优先"
        action = f"目标桶先看 {top_label}，高温突破只做观察；新增仓位以被动限价为主。"
        summary = str((high_temp_guard or {}).get("conclusion") or "实况升温节奏偏弱，先压住高温桶放大。")
    elif precip_cooling:
        stance = "高温桶下调"
        action = "不追高；优先等待降水证据消退后再评估。"
        summary = "强降水证据成立，蒸发冷却主导，降温概率显著上调。"
    elif momentum_blocking and top_is_lower_boundary:
        stance = f"温度轨迹变弱，压制 {higher_group}"
        action = f"目标桶先看 {top_label}，不追 {higher_group}；等待实况升温重新加速再评估。"
        summary = (
            "主因不是雨：已观测最高 "
            f"{_fmt_c(observed_max)}，短/中窗口动量 {_fmt_rate(momentum_rates.get('15m'))}/{_fmt_rate(momentum_rates.get('1h'))}/{_fmt_rate(momentum_rates.get('3h'))}，"
            f"峰值估计 {_fmt_c(peak_mu)}；{higher_group} 继续受压。"
        )
    elif lower_boundary_suppressed_by_track:
        stance = f"温度轨迹未突破，压制 {higher_group}"
        action = f"目标桶先看 {top_label}，不追 {higher_group}；轻微正动量只作为突破风险观察。"
        gap_text = _fmt_c(higher_gap_from_peak, 2) if higher_gap_from_peak is not None else "—"
        summary = (
            "主因不是雨：当前 "
            f"{_fmt_c(observed_temp)}，已观测最高 {_fmt_c(observed_max)}，峰值估计 {_fmt_c(peak_mu)}，"
            f"距 {higher_group} 仍约 {gap_text}；{higher_group} 继续受压。"
        )
    elif momentum_blocking or trajectory_cooling:
        stance = "温度轨迹变弱，高温桶下调"
        action = f"目标桶先看 {top_label}，只挂被动限价；等待实况升温重新加速再追高。"
        summary = (
            "晴空/低云/无降水只代表升温潜力；当前温度动量未兑现，"
            f"已观测最高 {_fmt_c(observed_max)}，不能仅凭软预报强推高温桶。"
        )
    elif future_precip_cooling and warming <= cooling + 1:
        stance = "未来降水风险压制高温桶"
        action = f"目标桶先看 {top_label}，未来降水风险消退前不追高。"
        summary = "未来1-3小时降水 nowcast 风险升高，可能压制后续升温兑现；这不是已发生降水。"
    elif momentum_state in {"sustained_warming", "emerging_warming"} and warming > cooling:
        stance = "温和上调高温桶"
        action = f"可关注 {top_label}，但只跟随被动成交；动量断档就撤回高温桶放大。"
        summary = str(momentum.get("conclusion") or "Temperature Momentum 显示升温潜力正在兑现。")
    elif warming > cooling:
        stance = "温和上调高温桶"
        action = f"可关注 {top_label}，仍以被动限价为主，避免盘口跳价追单。"
        summary = "升温侧证据多于降温侧，高温桶小幅受益。"
    elif cooling > warming:
        stance = "中低温桶上调"
        action = "降低高温桶追随强度，等待云量/降水/温度轨迹改善。"
        summary = "降温侧证据更强，中低温桶相对受益。"
    else:
        stance = "中性观望"
        action = "暂无强方向，只看被动价差，不做主动追价。"
        summary = "升温和降温证据接近，暂不放大单一桶判断。"

    recovery_rate_threshold = _to_float(os.environ.get("PM_CHENGDU_HIGH_TEMP_RECOVERY_RATE_C_PER_HOUR"))
    if recovery_rate_threshold is None or not math.isfinite(float(recovery_rate_threshold)):
        recovery_rate_threshold = 1.0
    triggers = [
        f"若实况升温率重新 > {_fmt_rate(recovery_rate_threshold)} 或已观测最高温继续突破，才上调 {higher_group}。",
        "若云量快速增加或出现可验证降水，立即下调高温桶。",
        "若 AWC/METAR 持续 CAVOK 但温度不升，维持观望，不追软预报高温。",
    ]
    if guard_active:
        guard_gap = _fmt_c((high_temp_guard or {}).get("forecast_gap_c"), 2)
        guard_rate = _fmt_rate((high_temp_guard or {}).get("warming_rate_c_per_hour"))
        triggers.insert(0, f"实况门控：软预报差 {guard_gap}，升温率 {guard_rate}，高温突破先降级为观察。")
    if trend_state == "weakening":
        triggers.insert(0, "小时级升温趋势正在变弱，等待下一小时是否恢复。")
    elif trend_state == "strengthening":
        triggers.insert(0, "小时级升温趋势变强，可观察目标桶是否获得连续确认。")
    if momentum_state in {"cooling", "not_realized", "flat"}:
        triggers.insert(0, "Temperature Momentum 未兑现：短窗口到小时级未形成持续升温，不因晴空单独追高。")
    elif momentum_state == "sustained_warming" and guard_active:
        triggers.insert(0, "Temperature Momentum 偏升仅作为突破风险观察，仍需实况最高温越过边界确认。")
    elif momentum_state == "sustained_warming":
        triggers.insert(0, "Temperature Momentum 持续兑现：分钟级到小时级同向偏升，可小幅确认目标高温桶。")
    if strong_no_precip:
        triggers.insert(1, "当前强无降水确认，不能把代理 rain 字段当作雷达降温证据。")
    if future_precip_cooling:
        triggers.insert(1, "未来1-3小时降水 nowcast 风险升高：作为升温兑现风险源，不当作已发生降水。")

    trade_plan = _target_bucket_trade_plan(
        target_candidates,
        realization_reliability,
        high_temp_guard,
        precip_cooling,
        trajectory_suppression=lower_boundary_suppressed_by_track,
        all_rows=rows,
        active_position_legs=active_position_legs,
    )
    if (momentum_blocking or lower_boundary_suppressed_by_track) and not precip_cooling:
        action = (
            f"目标桶先看 {top_label}，{higher_group} 暂不追；"
            "主因是温度轨迹偏弱和已观测最高不足，不是已发生降水。"
        )
    else:
        action = f"{trade_plan.get('primary_action')}：{trade_plan.get('summary')}"

    return {
        "stance": stance,
        "summary": summary,
        "action": action,
        "target_realization_trade_plan": trade_plan,
        "target_bucket": top_label,
        "remote_no_bucket": trade_plan.get("remote_no_bucket"),
        "remote_bucket_realization_prob": trade_plan.get("remote_bucket_realization_prob"),
        "remote_bucket_realization_pct": trade_plan.get("remote_bucket_realization_pct"),
        "remote_no_reason": trade_plan.get("remote_no_reason"),
        "suppressed_higher_bucket_group": higher_group if momentum_blocking or trajectory_cooling or lower_boundary_suppressed_by_track else None,
        "main_reason_not_rain": bool((momentum_blocking or lower_boundary_suppressed_by_track) and not precip_cooling),
        "watch_buckets": [str(row.get("outcome") or "") for row in target_candidates[:3] if row.get("outcome")],
        "triggers": triggers,
    }


def _created_at_replay_key_times(event_date: str) -> List[str]:
    if str(event_date or "").split("T", 1)[0] == "2026-06-14":
        return ["12:34", "13:43", "14:48", "15:34", "16:04", "16:12", "16:35"]
    return []


def _select_created_at_replay_rows(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    *,
    max_points: int = 10,
) -> List[sqlite3.Row]:
    key_times = _created_at_replay_key_times(event_date)
    rows = conn.execute(
        """
        SELECT id, created_at
        FROM event_runs
        WHERE slug = ? AND event_date = ?
        ORDER BY created_at ASC, id ASC
        """,
        (slug, event_date),
    ).fetchall()
    if not rows:
        return []
    parsed: List[Tuple[int, datetime]] = []
    for row in rows:
        dt = _parse_datetime(row["created_at"])
        if dt is None:
            continue
        parsed.append((int(row["id"]), dt))
    if not parsed:
        return []
    if not key_times:
        if len(parsed) <= max_points:
            wanted_ids = [row_id for row_id, _dt in parsed]
        else:
            selected_idx = {0, len(parsed) - 1}
            slots = max(2, min(max_points, 8))
            for idx in range(slots):
                selected_idx.add(round(idx * (len(parsed) - 1) / max(1, slots - 1)))
            wanted_ids = [parsed[idx][0] for idx in sorted(selected_idx)[:max_points]]
        placeholders = ",".join("?" for _ in wanted_ids)
        return list(
            conn.execute(
                f"""
                SELECT id, run_date, event_date, slug, title, payload_json, created_at
                FROM event_runs
                WHERE id IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                wanted_ids,
            ).fetchall()
        )
    selected_ids: Dict[int, None] = {}
    for time_text in key_times:
        try:
            hour_text, minute_text = time_text.split(":", 1)
            target_local = datetime.fromisoformat(str(event_date)).replace(
                hour=int(hour_text),
                minute=int(minute_text),
                second=0,
                microsecond=0,
                tzinfo=CHENGDU_TZ,
            )
        except (ValueError, TypeError):
            continue
        target_utc = target_local.astimezone(timezone.utc)
        after = [(dt - target_utc, row_id) for row_id, dt in parsed if dt >= target_utc - timedelta(minutes=2)]
        if after:
            delta, row_id = min(after, key=lambda item: (abs(item[0].total_seconds()), item[1]))
            if abs(delta.total_seconds()) <= 40 * 60:
                selected_ids[row_id] = None
                continue
        before = [(target_utc - dt, row_id) for row_id, dt in parsed if dt <= target_utc]
        if before:
            delta, row_id = min(before, key=lambda item: (abs(item[0].total_seconds()), item[1]))
            if abs(delta.total_seconds()) <= 40 * 60:
                selected_ids[row_id] = None
    wanted_ids = list(selected_ids.keys())[:max_points]
    if not wanted_ids:
        return []
    placeholders = ",".join("?" for _ in wanted_ids)
    full_rows = conn.execute(
        f"""
        SELECT id, run_date, event_date, slug, title, payload_json, created_at
        FROM event_runs
        WHERE id IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        wanted_ids,
    ).fetchall()
    return list(full_rows)


def _created_at_replay_point(row: sqlite3.Row) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    event_date = str(row["event_date"] or "").split("T", 1)[0]
    created_at = str(row["created_at"] or "")
    payload["_event_run_id"] = int(row["id"])
    payload["_created_at"] = created_at
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        features = {}
    rows = _distribution_rows(payload)
    features = dict(features)
    _merge_forecast_peak_timing_from_rows(features, rows)
    feature_groups = (
        payload.get("weather_likelihood_feature_groups")
        if isinstance(payload.get("weather_likelihood_feature_groups"), Mapping)
        else None
    )
    physical_weather_modifier = _physical_weather_modifier(features, feature_groups)
    rows = _apply_observed_max_display_guard(rows, features)
    rows = _apply_d0_observed_feasibility_guard(rows, features, event_date, today_key=event_date)
    top_single = _target_rows(rows, single_only=True)
    target_all = _target_rows(rows, single_only=False)
    rounding_rule = str(payload.get("rounding_rule") or "").strip()
    market_unit = str(payload.get("market_unit") or "C").strip() or "C"
    recent = [payload]
    high_temp_guard = _high_temp_amplification_guard(features)
    temperature_trend = _temperature_stages(recent)
    warming_pace = _warming_pace_assessment(features, temperature_trend, event_date=event_date)
    if isinstance(temperature_trend, dict):
        temperature_trend["warming_pace_assessment"] = warming_pace
    posterior_selection = _daily_target_bucket_selection(target_all, top_single)
    target_selection = _dynamic_target_bucket_selection(
        rows,
        target_all,
        top_single,
        features,
        rounding_rule,
        market_unit,
        temperature_trend,
        event_date=event_date,
    )
    evidence = _evidence_conclusions(payload, recent, high_temp_guard, temperature_trend)
    target_bucket = str(target_selection.get("target_bucket") or "") or None
    target_observation_state = _target_bucket_observation_state(
        rows,
        features,
        target_bucket,
        rounding_rule,
        market_unit,
    )
    reliability = _temperature_realization_reliability(
        temperature_trend,
        evidence,
        high_temp_guard,
        target_bucket=target_bucket,
        target_observation_state=target_observation_state,
    )
    created_dt = _parse_datetime(created_at)
    trend_momentum = (
        temperature_trend.get("temperature_momentum")
        if isinstance(temperature_trend.get("temperature_momentum"), Mapping)
        else {}
    )
    momentum_rates = trend_momentum.get("rates_c_per_hour") if isinstance(trend_momentum.get("rates_c_per_hour"), Mapping) else {}
    target_row = target_selection.get("target_row") if isinstance(target_selection.get("target_row"), Mapping) else {}
    target_row_posterior, target_row_posterior_field = (
        _target_bucket_probability(target_row) if isinstance(target_row, Mapping) else (None, "")
    )
    posterior_row = posterior_selection.get("target_row") if isinstance(posterior_selection.get("target_row"), Mapping) else {}
    single_row = target_selection.get("single_temperature_reference_bucket")
    single_row = single_row if isinstance(single_row, Mapping) else {}
    hard_observed_max, _hard_observed_max_source, _hard_observed_max_na_reason = _hard_observed_today_max_c(features)
    return {
        "event_run_id": int(row["id"]),
        "created_at": created_at,
        "local_time": created_dt.astimezone(CHENGDU_TZ).strftime("%m/%d %H:%M:%S") if created_dt else "",
        "target_bucket": target_bucket,
        "target_probability_pct": target_selection.get("target_probability_pct"),
        "target_bucket_type": target_selection.get("target_bucket_type"),
        "dynamic_target_state": target_selection.get("dynamic_target_state"),
        "dynamic_target_label": target_selection.get("dynamic_target_label"),
        "dynamic_target_reason": target_selection.get("dynamic_target_reason"),
        "posterior_highest_bucket": posterior_selection.get("target_bucket"),
        "posterior_highest_probability_pct": posterior_selection.get("target_probability_pct"),
        "single_temperature_reference_bucket": single_row.get("outcome"),
        "single_temperature_reference_probability_pct": target_selection.get("single_temperature_reference_probability_pct"),
        "target_q_temperature_bucket_posterior_pct": _pct(target_row.get("q_temperature_bucket_posterior")),
        "posterior_q_temperature_bucket_posterior_pct": _pct(posterior_row.get("q_temperature_bucket_posterior")),
        "reliability_label": reliability.get("label"),
        "reliability_state": reliability.get("state"),
        "realization_mode": reliability.get("realization_mode"),
        "warming_label": reliability.get("warming_label"),
        "cooling_label": reliability.get("cooling_label"),
        "warming_realization_pct": reliability.get("warming_realization_pct"),
        "cooling_realization_pct": reliability.get("cooling_realization_pct"),
        "confidence_pct": reliability.get("confidence_pct"),
        "target_observation_state": {
            "state": target_observation_state.get("state"),
            "source": target_observation_state.get("source"),
            "settlement_proxy_bucket": target_observation_state.get("settlement_proxy_bucket"),
            "observed_max_c": target_observation_state.get("observed_max_c"),
            "realized": bool(target_observation_state.get("realized")),
            "exceeded": bool(target_observation_state.get("exceeded")),
            "is_soft_display_source": bool(target_observation_state.get("is_soft_display_source")),
        },
        "observed_temp_c": _round(features.get("observed_temp_c"), 2),
        "observed_today_max_c": _round(hard_observed_max, 2),
        "zuuu_metar_raw_awc_today_max_c": _round(features.get("zuuu_metar_raw_awc_today_max_c"), 2),
        "wu_twc_observed_intraday_today_max_c": _round(features.get("wu_twc_observed_intraday_today_max_c"), 2),
        "temperature_rise_rate_c_per_hour": _round(_temperature_rise_rate_value(features), 2),
        "momentum_state": trend_momentum.get("state"),
        **(_temperature_momentum_metric_fields(momentum_rates) if isinstance(momentum_rates, Mapping) else {}),
        "momentum_rates_c_per_hour": {
            key: _round(momentum_rates.get(key), 2)
            for key in TEMPERATURE_MOMENTUM_WINDOWS
            if isinstance(momentum_rates, Mapping) and momentum_rates.get(key) is not None
        },
        "hourly_trend_state": temperature_trend.get("latest_trend_state"),
        "hourly_trend_label": temperature_trend.get("latest_trend_label"),
        "high_temp_guard_active": bool(high_temp_guard.get("active")),
        "warming_pace_state": warming_pace.get("state"),
        "warming_pace_label": warming_pace.get("label"),
        "warming_pace_required_rate_c_per_hour": warming_pace.get("required_rate_c_per_hour"),
        "warming_pace_actual_rate_c_per_hour": warming_pace.get("actual_rate_c_per_hour"),
    }


def _created_at_replay(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = _select_created_at_replay_rows(conn, slug, event_date)
    if not rows:
        return [], {
            "available": False,
            "reason": "no_configured_replay_key_times" if not _created_at_replay_key_times(event_date) else "no_matching_snapshots",
            "event_date": str(event_date or ""),
            "version": "chengdu_created_at_replay_v1",
        }
    points: List[Dict[str, Any]] = []
    for row in rows:
        point = _created_at_replay_point(row)
        if point:
            points.append(point)
    return points, {
        "available": bool(points),
        "reason": "ok" if points else "no_parseable_snapshots",
        "event_date": str(event_date or ""),
        "point_count": len(points),
        "version": "chengdu_created_at_replay_v1",
        "definition": "按每个历史 created_at 快照当时已可见数据重跑动态目标桶与兑现状态；不使用未来快照，不写库。",
    }


def _features_from_payload_for_intraday_summary(payload: Mapping[str, Any]) -> Dict[str, Any]:
    features: Dict[str, Any] = {}
    full = payload.get("weather_likelihood_features")
    if isinstance(full, Mapping):
        features.update(dict(full))
    compact = payload.get("weather_likelihood_features_compact")
    if isinstance(compact, Mapping):
        for key, value in compact.items():
            if features.get(key) in (None, "") and value not in (None, ""):
                features[key] = value
    for key in (
        "weather_likelihood_refreshed_at",
        "weather_com_forecast_peak_c",
        "twc_forecast_peak_c",
        "weather_com_forecast_peak_at",
        "twc_forecast_peak_at",
        "weather_com_forecast_peak_time_source",
        "twc_forecast_peak_time_source",
    ):
        if features.get(key) in (None, "") and payload.get(key) not in (None, ""):
            features[key] = payload.get(key)
    return features


def build_chengdu_intraday_summary_from_payload(
    payload: Mapping[str, Any],
    *,
    slug: str = "",
    event_date: str = "",
    created_at: str = "",
    snapshot_id: Any = 0,
    snapshot_source: str = "payload",
) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"ok": False, "error": "payload_not_object"}
    payload_work = dict(payload)
    slug_text = str(slug or payload_work.get("slug") or "")
    event_date_text = str(event_date or payload_work.get("event_date") or "")
    created_text = str(
        created_at
        or payload_work.get("weather_likelihood_refreshed_at")
        or payload_work.get("created_at")
        or payload_work.get("fact_as_of")
        or ""
    )
    if created_text and not payload_work.get("_created_at"):
        payload_work["_created_at"] = created_text
    if snapshot_id and not payload_work.get("_event_run_id"):
        payload_work["_event_run_id"] = snapshot_id

    rows = _distribution_rows(payload_work)
    market_tick_snapshot: Dict[str, Any] = {
        "available": False,
        "reason": "payload_only_no_market_tick_lookup",
        "version": "market_tick_snapshot_overlay_v1",
    }
    market_top_bucket = _market_top_bucket_reference(rows, market_tick_snapshot)
    recent = _append_current_payload_snapshot(
        [],
        payload_work,
        snapshot_id=snapshot_id,
        created_at=created_text,
        source=snapshot_source,
    )
    features = _features_from_payload_for_intraday_summary(payload_work)
    payload_work["weather_likelihood_features"] = features
    if features.get("weather_likelihood_refreshed_at") and not payload_work.get("weather_likelihood_refreshed_at"):
        payload_work["weather_likelihood_refreshed_at"] = features.get("weather_likelihood_refreshed_at")
    _merge_forecast_peak_timing_from_rows(features, rows)
    feature_groups = (
        payload_work.get("weather_likelihood_feature_groups")
        if isinstance(payload_work.get("weather_likelihood_feature_groups"), Mapping)
        else None
    )
    physical_weather_modifier = _physical_weather_modifier(features, feature_groups)
    rows = _apply_observed_max_display_guard(rows, features)
    rows = _apply_d0_observed_feasibility_guard(rows, features, event_date_text)
    top_all = _top_rows(rows, single_only=False)
    top_single = _target_rows(rows, single_only=True)
    target_all = _target_rows(rows, single_only=False)
    rounding_rule = str(payload_work.get("rounding_rule") or "").strip()
    market_unit = str(payload_work.get("market_unit") or "C").strip() or "C"
    posterior_target_selection = _daily_target_bucket_selection(target_all, top_single)
    high_temp_guard = _high_temp_amplification_guard(features)
    temperature_trend = _temperature_stages(recent)
    warming_pace = _warming_pace_assessment(features, temperature_trend, event_date=event_date_text)
    if isinstance(temperature_trend, dict):
        temperature_trend["warming_pace_assessment"] = warming_pace
    target_selection = _dynamic_target_bucket_selection(
        rows,
        target_all,
        top_single,
        features,
        rounding_rule,
        market_unit,
        temperature_trend,
        event_date=event_date_text,
        market_top_bucket=market_top_bucket,
    )
    forecast_reference_bayes = _forecast_reference_bayes(rows, features, market_unit=market_unit)
    observation_led_bayes = _observation_led_bayes(
        rows,
        features,
        temperature_trend,
        feature_groups=feature_groups,
        rounding_rule=rounding_rule,
        market_unit=market_unit,
        event_date=event_date_text,
        reference_bucket=str(
            target_selection.get("target_bucket")
            or posterior_target_selection.get("target_bucket")
            or forecast_reference_bayes.get("target_bucket")
            or ""
        ),
    )
    rows, observation_led_likelihood_meta = _apply_observation_led_likelihood_to_rows(
        rows,
        observation_led_bayes,
        features,
    )
    if observation_led_likelihood_meta.get("applied"):
        observation_led_bayes = dict(observation_led_bayes)
        observation_led_bayes["main_posterior_likelihood"] = dict(observation_led_likelihood_meta)
        top_all = _top_rows(rows, single_only=False)
        top_single = _target_rows(rows, single_only=True)
        target_all = _target_rows(rows, single_only=False)
        posterior_target_selection = _daily_target_bucket_selection(target_all, top_single)
        target_selection = _dynamic_target_bucket_selection(
            rows,
            target_all,
            top_single,
            features,
            rounding_rule,
            market_unit,
            temperature_trend,
            event_date=event_date_text,
            market_top_bucket=market_top_bucket,
        )
        forecast_reference_bayes = _forecast_reference_bayes(rows, features, market_unit=market_unit)
    settlement_lock_bayes = _settlement_lock_bayes(
        rows,
        features,
        rounding_rule=rounding_rule,
        market_unit=market_unit,
    )
    target_selection = _apply_bayes_target_priority(
        target_selection,
        forecast_reference_bayes=forecast_reference_bayes,
        observation_led_bayes=observation_led_bayes,
        settlement_lock_bayes=settlement_lock_bayes,
    )
    temperature_trajectory_status = _temperature_trajectory_status(features, temperature_trend, feature_groups)
    temperature_momentum = (
        temperature_trend.get("temperature_momentum")
        if isinstance(temperature_trend, Mapping) and isinstance(temperature_trend.get("temperature_momentum"), Mapping)
        else {}
    )
    momentum_rates = (
        temperature_momentum.get("rates_c_per_hour")
        if isinstance(temperature_momentum.get("rates_c_per_hour"), Mapping)
        else {}
    )
    observed_today_max_hard_c, observed_today_max_hard_source, observed_today_max_na_reason = _hard_observed_today_max_c(features)
    observed_path_temp_c, observed_path_temp_source = _observed_path_temperature_c(
        features.get("observed_temp_c"),
        observed_today_max_hard_c,
    )
    refresh_cadence = _refresh_cadence(payload_work, features, event_date_text)
    evidence = _evidence_conclusions(payload_work, recent, high_temp_guard, temperature_trend)
    target_bucket = str(target_selection.get("target_bucket") or "") or None
    target_observation_state = _target_bucket_observation_state(
        rows,
        features,
        target_bucket,
        rounding_rule,
        market_unit,
    )
    realization_reliability = _temperature_realization_reliability(
        temperature_trend,
        evidence,
        high_temp_guard,
        target_bucket=target_bucket,
        target_observation_state=target_observation_state,
    )
    target_bucket_outlook = _target_bucket_outlook(
        rows,
        target_selection,
        realization_reliability,
        target_observation_state,
        features,
        rounding_rule,
        market_unit,
    )
    temperature_path_distribution = _temperature_path_distribution(
        rows,
        target_bucket_outlook,
        temperature_trajectory_status,
        realization_reliability,
        refresh_cadence,
        features,
        target_observation_state,
        feature_groups,
    )
    if isinstance(target_bucket_outlook, dict):
        _sync_target_bucket_outlook_with_temperature_path_distribution(
            target_bucket_outlook,
            temperature_path_distribution,
        )
        target_bucket_outlook["temperature_path_distribution"] = temperature_path_distribution
        target_bucket_outlook["forecast_reference_bayes"] = forecast_reference_bayes
        target_bucket_outlook["observation_led_bayes"] = observation_led_bayes
        target_bucket_outlook["settlement_lock_bayes"] = settlement_lock_bayes
    target_candidates = target_selection.get("watch_buckets") if isinstance(target_selection.get("watch_buckets"), list) else top_all
    decision = _decision_text(
        evidence,
        target_candidates,
        rows,
        high_temp_guard,
        temperature_trend,
        realization_reliability,
        target_selection=target_selection,
    )
    target_row = target_selection.get("target_row") if isinstance(target_selection.get("target_row"), Mapping) else {}
    target_label = str(target_bucket or decision.get("target_bucket") or "")
    market_top_label = str(market_top_bucket.get("outcome") or "")
    target_market_divergence = bool(target_label and market_top_label and target_label != market_top_label)
    phys_enabled_any = any(bool(item.get("q_physical_enabled")) for item in rows)
    phys_evidence_max = max([float(item.get("q_physical_evidence") or 0.0) for item in rows] or [0.0])
    comparison_reference_dt = (
        _parse_datetime(payload_work.get("weather_likelihood_refreshed_at"))
        or _parse_datetime(created_text)
        or datetime.now(timezone.utc)
    )
    source_observation_comparison = _source_observation_comparison(
        rows,
        features,
        rounding_rule=rounding_rule,
        market_unit=market_unit,
        reference_time=comparison_reference_dt,
        event_date=event_date_text,
    )
    pre_guard_single = [
        dict(item) for item in rows
        if not item.get("is_cumulative_bucket")
        and (
            _to_float(item.get("q_bayes_posterior_raw")) is not None
            or _to_float(item.get("q_calibrated_raw")) is not None
        )
    ]
    pre_guard_single.sort(
        key=lambda item: (
            _to_float(item.get("q_bayes_posterior_raw"))
            if _to_float(item.get("q_bayes_posterior_raw")) is not None
            else (_to_float(item.get("q_calibrated_raw")) or -1.0)
        ),
        reverse=True,
    )
    target_row = target_selection.get("target_row") if isinstance(target_selection.get("target_row"), Mapping) else {}
    target_row_posterior, target_row_posterior_field = (
        _target_bucket_probability(target_row) if isinstance(target_row, Mapping) else (None, "")
    )
    return {
        "ok": True,
        "source": f"{snapshot_source}.payload_json",
        "event_run_id": int(snapshot_id or 0),
        "run_date": str(payload_work.get("run_date") or ""),
        "event_date": event_date_text,
        "slug": slug_text,
        "title": str(payload_work.get("title") or ""),
        "created_at": created_text,
        "rounding_rule": rounding_rule or None,
        "rounding_rule_label": _rounding_rule_label(rounding_rule),
        "market_unit": market_unit,
        "weather_likelihood_refreshed_at": str(payload_work.get("weather_likelihood_refreshed_at") or ""),
        "market_updated_at": str(payload_work.get("market_updated_at") or ""),
        "feature_quality": str(payload_work.get("weather_likelihood_feature_quality") or ""),
        "physical_likelihood": {
            "enabled_any": phys_enabled_any,
            "evidence_max": phys_evidence_max,
            "status": "enabled" if phys_enabled_any and phys_evidence_max > 0 else "observed_only",
            "weather_modifier": physical_weather_modifier,
            "weather_modifier_state": physical_weather_modifier.get("state"),
            "weather_modifier_peak_mu_adjustment_c": physical_weather_modifier.get("peak_mu_adjustment_c"),
        },
        "physical_weather_modifier": physical_weather_modifier,
        "market_tick_coverage": market_tick_snapshot,
        "market_top_bucket": market_top_bucket,
        "market_top_label": market_top_label,
        "target_market_divergence": target_market_divergence,
        "target_market_divergence_reason": (
            "system_target_bucket_differs_from_market_top_bucket"
            if target_market_divergence
            else None
        ),
        "bucket_probabilities": rows,
        "temperature_distribution": {
            "rows": rows,
            "top_calibrated": top_all[0] if top_all else None,
            "top_target_bucket": target_all[0] if target_all else None,
            "top_single_temperature": top_single[0] if top_single else None,
            "top_single_temperature_label": str(top_single[0].get("outcome") or "") if top_single else "",
            "top_single_temperatures": top_single,
            "source": "bucket_probabilities_alias",
            "ssot": "event_fact_history.fact_json.rows",
        },
        "target_bucket": target_label,
        "target_bucket_source": str(target_selection.get("selection_priority") or target_selection.get("dynamic_target_source") or ""),
        "target_bucket_source_semantics": str(
            target_selection.get("target_selection_priority_version") or "dynamic_posterior_highest_settlement_bucket_v4"
        ),
        "target_bucket_definition": target_selection.get("definition"),
        "target_bucket_selection": target_selection,
        "forecast_reference_bayes": forecast_reference_bayes,
        "observation_led_bayes": observation_led_bayes,
        "observation_led_likelihood": observation_led_likelihood_meta,
        "settlement_lock_bayes": settlement_lock_bayes,
        "posterior_target_bucket_selection": posterior_target_selection,
        "posterior_highest_bucket": posterior_target_selection.get("target_row"),
        "posterior_highest_bucket_label": posterior_target_selection.get("target_bucket"),
        "target_bucket_probability_pct": target_selection.get("target_probability_pct"),
        "target_bucket_probability_field": target_selection.get("target_probability_field"),
        "pre_guard_top_single_temperature": pre_guard_single[0] if pre_guard_single else None,
        "top_calibrated": top_all[0] if top_all else None,
        "top_target_bucket": target_all[0] if target_all else None,
        "top_single_temperature": top_single[0] if top_single else None,
        "top_single_temperatures": top_single,
        "evidence_conclusions": evidence,
        "temperature_trend": temperature_trend,
        "temperature_trajectory_status": temperature_trajectory_status,
        "warming_pace_assessment": warming_pace,
        "temperature_path_distribution": temperature_path_distribution,
        "temperature_realization_reliability": realization_reliability,
        "target_bucket_outlook": target_bucket_outlook,
        "target_bucket_observation_state": target_observation_state,
        "high_temp_amplification_guard": high_temp_guard,
        "refresh_cadence": refresh_cadence,
        "source_observation_comparison": source_observation_comparison,
        "created_at_replay": [],
        "created_at_replay_meta": {
            "available": False,
            "reason": "payload_only_fact_write",
            "event_date": event_date_text,
            "point_count": 0,
            "version": "chengdu_created_at_replay_v1",
        },
        "decision": decision,
        "key_metrics": {
            "observed_temp_c": _round(features.get("observed_temp_c"), 2),
            "observed_today_max_c": _round(observed_today_max_hard_c, 2),
            "observed_today_max_source": observed_today_max_hard_source,
            "observed_today_max_na_reason": observed_today_max_na_reason,
            "observed_path_temp_c": _round(observed_path_temp_c, 2),
            "observed_path_temp_source": observed_path_temp_source,
            "temperature_trajectory_available": bool(temperature_trajectory_status.get("available")),
            "temperature_trajectory_state": str(temperature_trajectory_status.get("state") or ""),
            "temperature_trajectory_label": str(temperature_trajectory_status.get("label") or ""),
            "temperature_trajectory_na_reason": str(temperature_trajectory_status.get("na_reason") or ""),
            "temperature_trajectory_missing_fields": temperature_trajectory_status.get("missing_fields") or [],
            "physical_weather_state": physical_weather_modifier.get("state"),
            "physical_weather_peak_mu_adjustment_c": physical_weather_modifier.get("peak_mu_adjustment_c"),
            "physical_weather_path_probability_scale": physical_weather_modifier.get("path_probability_scale"),
            "observed_source_latest_time_local": refresh_cadence.get("latest_observation_time_local"),
            "observed_source_age_minutes": refresh_cadence.get("observation_age_minutes"),
            "observed_source_stale": bool(refresh_cadence.get("source_stale")),
            "metar_temperature_c": _round(features.get("metar_temperature_c"), 2),
            "zuuu_metar_raw_awc_latest_temp_c": _round(features.get("zuuu_metar_raw_awc_latest_temp_c"), 2),
            "zuuu_metar_raw_awc_today_max_c": _round(features.get("zuuu_metar_raw_awc_today_max_c"), 2),
            "zuuu_metar_raw_awc_latest_at": str(features.get("zuuu_metar_raw_awc_latest_at") or ""),
            "zuuu_metar_raw_awc_today_max_at": str(features.get("zuuu_metar_raw_awc_today_max_at") or ""),
            "temperature_rise_rate_c_per_hour": _round(_temperature_rise_rate_value(features), 2),
            "momentum_state": temperature_momentum.get("state") if isinstance(temperature_momentum, Mapping) else None,
            "momentum_rates_c_per_hour": {
                key: _round(momentum_rates.get(key), 2)
                for key in TEMPERATURE_MOMENTUM_WINDOWS
                if isinstance(momentum_rates, Mapping) and momentum_rates.get(key) is not None
            },
            "warming_pace_state": warming_pace.get("state"),
            "warming_pace_label": warming_pace.get("label"),
            "warming_pace_required_rate_c_per_hour": warming_pace.get("required_rate_c_per_hour"),
            "warming_pace_actual_rate_c_per_hour": warming_pace.get("actual_rate_c_per_hour"),
            "target_bucket": target_label,
            "target_bucket_source": str(target_selection.get("selection_priority") or target_selection.get("dynamic_target_source") or ""),
            "target_bucket_source_semantics": str(
                target_selection.get("target_selection_priority_version") or "dynamic_posterior_highest_settlement_bucket_v4"
            ),
            "target_bucket_probability_pct": target_selection.get("target_probability_pct"),
            "target_bucket_probability_field": target_selection.get("target_probability_field"),
            "target_bucket_posterior_pct": target_row_posterior,
            "target_bucket_posterior": target_row_posterior,
            "target_bucket_posterior_field": target_row_posterior_field,
            "peak_estimator_mu_c": _round(features.get("peak_estimator_mu_c"), 2),
            "peak_estimator_sigma_c": _round(features.get("peak_estimator_sigma_c"), 2),
            "peak_estimator_source": str(features.get("peak_estimator_source") or ""),
            "peak_estimator_observed_today_max_source": str(
                features.get("peak_estimator_observed_today_max_source") or ""
            ),
            "peak_estimator_raw_observed_today_max_c": _round(
                features.get("peak_estimator_raw_observed_today_max_c"), 2
            ),
            "peak_estimator_raw_observed_today_max_demoted_reason": str(
                features.get("peak_estimator_raw_observed_today_max_demoted_reason") or ""
            ),
        },
    }


def build_chengdu_intraday_summary(
    db_path: Path | str = DEFAULT_DB_PATH,
    event_date: Optional[str] = None,
) -> Dict[str, Any]:
    db = Path(db_path)
    if not db.exists():
        return {"ok": False, "error": f"db_missing:{db}"}
    try:
        conn = _connect_readonly(db)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"db_open_failed:{exc}"}
    try:
        try:
            row: Any = _latest_fact_snapshot(conn, event_date=event_date) or _latest_event_run(conn, event_date=event_date)
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"db_read_failed:{exc}"}
        if row is None:
            return {"ok": False, "error": "chengdu_event_run_not_found"}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception as exc:
            return {"ok": False, "error": f"payload_json_invalid:{exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload_json_not_object"}
        payload = _hydrate_fact_payload_temperature_features(payload)
        snapshot_source_text = str(row.get("snapshot_source") if isinstance(row, dict) else "event_runs")
        slug = str(row["slug"] or "")
        event_date_text = str(row["event_date"] or "")
        active_position_legs = _active_position_legs(conn, slug, event_date_text)
        rows = _distribution_rows(payload)
        market_tick_snapshot = _latest_market_tick_snapshot(
            conn,
            slug,
            event_date_text.split("T", 1)[0],
            rows,
        )
        if snapshot_source_text == "event_fact_latest":
            market_tick_snapshot = dict(market_tick_snapshot)
            market_tick_snapshot["overlay_applied"] = False
            market_tick_snapshot["overlay_skipped_reason"] = "event_fact_latest_distribution_is_ssot"
        else:
            rows = _apply_market_tick_overlay(rows, market_tick_snapshot)
            market_tick_snapshot = dict(market_tick_snapshot)
            market_tick_snapshot["overlay_applied"] = True
        market_top_bucket = _market_top_bucket_reference(rows, market_tick_snapshot)
        try:
            recent_runs = _recent_payloads(conn, slug, event_date_text, limit=240)
        except sqlite3.Error:
            recent_runs = []
        try:
            recent_facts = _recent_fact_payloads(conn, slug, event_date_text, limit=2000)
        except sqlite3.Error:
            recent_facts = []
        recent = _merge_recent_payloads(recent_runs, recent_facts)
        recent = _append_current_payload_snapshot(
            recent,
            payload,
            snapshot_id=row["id"],
            created_at=str(row["created_at"] or ""),
            source=snapshot_source_text,
        )
        features = payload.get("weather_likelihood_features")
        if not isinstance(features, Mapping):
            features = {}
        features = dict(features)
        _merge_forecast_peak_timing_from_rows(features, rows)
        feature_groups = (
            payload.get("weather_likelihood_feature_groups")
            if isinstance(payload.get("weather_likelihood_feature_groups"), Mapping)
            else None
        )
        physical_weather_modifier = _physical_weather_modifier(features, feature_groups)
        if snapshot_source_text != "event_fact_latest":
            rows = _apply_observed_max_display_guard(rows, features)
            rows = _apply_d0_observed_feasibility_guard(rows, features, str(row["event_date"] or ""))
        top_all = _top_rows(rows, single_only=False)
        top_single = _target_rows(rows, single_only=True)
        target_all = _target_rows(rows, single_only=False)
        rounding_rule = str(payload.get("rounding_rule") or "").strip()
        market_unit = str(payload.get("market_unit") or "C").strip() or "C"
        posterior_target_selection = _daily_target_bucket_selection(target_all, top_single)
        if snapshot_source_text == "event_fact_latest":
            posterior_target_selection = _merge_ssot_payload_target_selection(
                payload.get("posterior_target_bucket_selection"),
                posterior_target_selection,
                rows,
                source="event_fact_latest.posterior_target_bucket_selection",
                payload_authoritative=True,
            )
        high_temp_guard = _high_temp_amplification_guard(features)
        temperature_trend = _temperature_stages(recent)
        warming_pace = _warming_pace_assessment(features, temperature_trend, event_date=str(row["event_date"] or ""))
        if isinstance(temperature_trend, dict):
            temperature_trend["warming_pace_assessment"] = warming_pace
        target_selection = _dynamic_target_bucket_selection(
            rows,
            target_all,
            top_single,
            features,
            rounding_rule,
            market_unit,
            temperature_trend,
            event_date=str(row["event_date"] or ""),
            market_top_bucket=market_top_bucket,
        )
        if snapshot_source_text == "event_fact_latest":
            target_selection = _merge_ssot_payload_target_selection(
                payload.get("target_bucket_selection"),
                target_selection,
                rows,
                source="event_fact_latest.target_bucket_selection",
                payload_authoritative=True,
            )
        forecast_reference_bayes = _forecast_reference_bayes(rows, features, market_unit=market_unit)
        observation_led_bayes = _observation_led_bayes(
            rows,
            features,
            temperature_trend,
            feature_groups=feature_groups,
            rounding_rule=rounding_rule,
            market_unit=market_unit,
            event_date=str(row["event_date"] or ""),
            reference_bucket=str(
                target_selection.get("target_bucket")
                or posterior_target_selection.get("target_bucket")
                or forecast_reference_bayes.get("target_bucket")
                or ""
            ),
        )
        rows, observation_led_likelihood_meta = _apply_observation_led_likelihood_to_rows(
            rows,
            observation_led_bayes,
            features,
        )
        if observation_led_likelihood_meta.get("applied"):
            observation_led_bayes = dict(observation_led_bayes)
            observation_led_bayes["main_posterior_likelihood"] = dict(observation_led_likelihood_meta)
            top_all = _top_rows(rows, single_only=False)
            top_single = _target_rows(rows, single_only=True)
            target_all = _target_rows(rows, single_only=False)
            posterior_target_selection = _daily_target_bucket_selection(target_all, top_single)
            if snapshot_source_text == "event_fact_latest":
                posterior_target_selection = _merge_ssot_payload_target_selection(
                    payload.get("posterior_target_bucket_selection"),
                    posterior_target_selection,
                    rows,
                    source="event_fact_latest.posterior_target_bucket_selection",
                    payload_authoritative=False,
                )
            target_selection = _dynamic_target_bucket_selection(
                rows,
                target_all,
                top_single,
                features,
                rounding_rule,
                market_unit,
                temperature_trend,
                event_date=str(row["event_date"] or ""),
                market_top_bucket=market_top_bucket,
            )
            if snapshot_source_text == "event_fact_latest":
                target_selection = _merge_ssot_payload_target_selection(
                    payload.get("target_bucket_selection"),
                    target_selection,
                    rows,
                    source="event_fact_latest.target_bucket_selection",
                    payload_authoritative=False,
                )
            forecast_reference_bayes = _forecast_reference_bayes(rows, features, market_unit=market_unit)
        settlement_lock_bayes = _settlement_lock_bayes(
            rows,
            features,
            rounding_rule=rounding_rule,
            market_unit=market_unit,
        )
        target_selection = _apply_bayes_target_priority(
            target_selection,
            forecast_reference_bayes=forecast_reference_bayes,
            observation_led_bayes=observation_led_bayes,
            settlement_lock_bayes=settlement_lock_bayes,
        )
        temperature_trajectory_status = _temperature_trajectory_status(features, temperature_trend, feature_groups)
        temperature_momentum = (
            temperature_trend.get("temperature_momentum")
            if isinstance(temperature_trend, Mapping) and isinstance(temperature_trend.get("temperature_momentum"), Mapping)
            else {}
        )
        momentum_rates = (
            temperature_momentum.get("rates_c_per_hour")
            if isinstance(temperature_momentum.get("rates_c_per_hour"), Mapping)
            else {}
        )
        observed_today_max_hard_c, observed_today_max_hard_source, observed_today_max_na_reason = _hard_observed_today_max_c(features)
        observed_path_temp_c, observed_path_temp_source = _observed_path_temperature_c(
            features.get("observed_temp_c"),
            observed_today_max_hard_c,
        )
        refresh_cadence = _refresh_cadence(payload, features, str(row["event_date"] or ""))
        evidence = _evidence_conclusions(payload, recent, high_temp_guard, temperature_trend)
        target_bucket = str(target_selection.get("target_bucket") or "") or None
        target_observation_state = _target_bucket_observation_state(
            rows,
            features,
            target_bucket,
            rounding_rule,
            market_unit,
        )
        realization_reliability = _temperature_realization_reliability(
            temperature_trend,
            evidence,
            high_temp_guard,
            target_bucket=target_bucket,
            target_observation_state=target_observation_state,
        )
        target_bucket_outlook = _target_bucket_outlook(
            rows,
            target_selection,
            realization_reliability,
            target_observation_state,
            features,
            rounding_rule,
            market_unit,
        )
        temperature_path_distribution = _temperature_path_distribution(
            rows,
            target_bucket_outlook,
            temperature_trajectory_status,
            realization_reliability,
            refresh_cadence,
            features,
            target_observation_state,
            feature_groups,
        )
        if isinstance(target_bucket_outlook, dict):
            _sync_target_bucket_outlook_with_temperature_path_distribution(
                target_bucket_outlook,
                temperature_path_distribution,
            )
            target_bucket_outlook["temperature_path_distribution"] = temperature_path_distribution
            target_bucket_outlook["forecast_reference_bayes"] = forecast_reference_bayes
            target_bucket_outlook["observation_led_bayes"] = observation_led_bayes
            target_bucket_outlook["settlement_lock_bayes"] = settlement_lock_bayes
        target_candidates = target_selection.get("watch_buckets") if isinstance(target_selection.get("watch_buckets"), list) else top_all
        decision = _decision_text(
            evidence,
            target_candidates,
            rows,
            high_temp_guard,
            temperature_trend,
            realization_reliability,
            target_selection=target_selection,
            active_position_legs=active_position_legs,
        )
        pre_guard_single = [
            dict(item) for item in rows
            if not item.get("is_cumulative_bucket")
            and (
                _to_float(item.get("q_bayes_posterior_raw")) is not None
                or _to_float(item.get("q_calibrated_raw")) is not None
            )
        ]
        pre_guard_single.sort(
            key=lambda item: (
                _to_float(item.get("q_bayes_posterior_raw"))
                if _to_float(item.get("q_bayes_posterior_raw")) is not None
                else (_to_float(item.get("q_calibrated_raw")) or -1.0)
            ),
            reverse=True,
        )
        target_row = target_selection.get("target_row") if isinstance(target_selection.get("target_row"), Mapping) else {}
        target_row_posterior, target_row_posterior_field = (
            _target_bucket_probability(target_row) if isinstance(target_row, Mapping) else (None, "")
        )
        target_label = str(target_bucket or decision.get("target_bucket") or "")
        market_top_label = str(market_top_bucket.get("outcome") or "")
        target_market_divergence = bool(target_label and market_top_label and target_label != market_top_label)
        phys_enabled_any = any(bool(item.get("q_physical_enabled")) for item in rows)
        phys_evidence_max = max([float(item.get("q_physical_evidence") or 0.0) for item in rows] or [0.0])
        soft_forecast_peak_c, soft_forecast_source = _soft_forecast_peak(features)
        comparison_reference_dt = (
            _parse_datetime(payload.get("weather_likelihood_refreshed_at"))
            or _parse_datetime(row["created_at"])
            or datetime.now(timezone.utc)
        )
        source_observation_comparison = _source_observation_comparison(
            rows,
            features,
            rounding_rule=rounding_rule,
            market_unit=market_unit,
            reference_time=comparison_reference_dt,
            event_date=event_date_text,
        )
        replay_points, replay_meta = _created_at_replay(
            conn,
            slug,
            event_date_text.split("T", 1)[0],
        )
        return {
            "ok": True,
            "source": snapshot_source_text + ".payload_json",
            "event_run_id": int(row["id"]),
            "run_date": str(row["run_date"] or ""),
            "event_date": event_date_text,
            "slug": slug,
            "title": str(row["title"] or ""),
            "created_at": str(row["created_at"] or ""),
            "rounding_rule": rounding_rule or None,
            "rounding_rule_label": _rounding_rule_label(rounding_rule),
            "market_unit": market_unit,
            "weather_likelihood_refreshed_at": str(payload.get("weather_likelihood_refreshed_at") or ""),
            "market_updated_at": str(payload.get("market_updated_at") or ""),
            "feature_quality": str(payload.get("weather_likelihood_feature_quality") or ""),
            "physical_likelihood": {
                "enabled_any": phys_enabled_any,
                "evidence_max": phys_evidence_max,
                "status": "enabled" if phys_enabled_any and phys_evidence_max > 0 else "observed_only",
                "weather_modifier": physical_weather_modifier,
                "weather_modifier_state": physical_weather_modifier.get("state"),
                "weather_modifier_peak_mu_adjustment_c": physical_weather_modifier.get("peak_mu_adjustment_c"),
            },
            "physical_weather_modifier": physical_weather_modifier,
            "market_tick_coverage": market_tick_snapshot,
            "market_top_bucket": market_top_bucket,
            "market_top_label": market_top_label,
            "target_market_divergence": target_market_divergence,
            "target_market_divergence_reason": (
                "system_target_bucket_differs_from_market_top_bucket"
                if target_market_divergence
                else None
            ),
            "bucket_probabilities": rows,
            "temperature_distribution": {
                "rows": rows,
                "top_calibrated": top_all[0] if top_all else None,
                "top_target_bucket": target_all[0] if target_all else None,
                "top_single_temperature": top_single[0] if top_single else None,
                "top_single_temperature_label": (
                    str(top_single[0].get("outcome") or "") if top_single else ""
                ),
                "top_single_temperatures": top_single,
                "source": "bucket_probabilities_alias",
                "ssot": f"{snapshot_source_text}.payload_json.rows",
            },
            "target_bucket": target_label,
            "target_bucket_source": str(target_selection.get("selection_priority") or target_selection.get("dynamic_target_source") or ""),
            "target_bucket_source_semantics": str(
                target_selection.get("target_selection_priority_version") or "dynamic_posterior_highest_settlement_bucket_v4"
            ),
            "target_bucket_definition": target_selection.get("definition"),
            "target_bucket_selection": target_selection,
            "forecast_reference_bayes": forecast_reference_bayes,
            "observation_led_bayes": observation_led_bayes,
            "observation_led_likelihood": observation_led_likelihood_meta,
            "settlement_lock_bayes": settlement_lock_bayes,
            "posterior_target_bucket_selection": posterior_target_selection,
            "posterior_highest_bucket": posterior_target_selection.get("target_row"),
            "posterior_highest_bucket_label": posterior_target_selection.get("target_bucket"),
            "target_bucket_probability_pct": target_selection.get("target_probability_pct"),
            "target_bucket_probability_field": target_selection.get("target_probability_field"),
            "pre_guard_top_single_temperature": pre_guard_single[0] if pre_guard_single else None,
            "top_calibrated": top_all[0] if top_all else None,
            "top_target_bucket": target_all[0] if target_all else None,
            "top_single_temperature": top_single[0] if top_single else None,
            "top_single_temperatures": top_single,
            "evidence_conclusions": evidence,
            "temperature_trend": temperature_trend,
            "temperature_trajectory_status": temperature_trajectory_status,
            "warming_pace_assessment": warming_pace,
            "temperature_path_distribution": temperature_path_distribution,
            "temperature_realization_reliability": realization_reliability,
            "target_bucket_outlook": target_bucket_outlook,
            "target_bucket_observation_state": target_observation_state,
            "high_temp_amplification_guard": high_temp_guard,
            "refresh_cadence": refresh_cadence,
            "source_observation_comparison": source_observation_comparison,
            "created_at_replay": replay_points,
            "created_at_replay_meta": replay_meta,
            "decision": decision,
            "key_metrics": {
                "observed_temp_c": _round(features.get("observed_temp_c"), 2),
                "observed_today_max_c": _round(observed_today_max_hard_c, 2),
                "observed_today_max_source": observed_today_max_hard_source,
                "observed_today_max_na_reason": observed_today_max_na_reason,
                "observed_path_temp_c": _round(observed_path_temp_c, 2),
                "observed_path_temp_source": observed_path_temp_source,
                "temperature_trajectory_available": bool(temperature_trajectory_status.get("available")),
                "temperature_trajectory_state": str(temperature_trajectory_status.get("state") or ""),
                "temperature_trajectory_label": str(temperature_trajectory_status.get("label") or ""),
                "temperature_trajectory_na_reason": str(temperature_trajectory_status.get("na_reason") or ""),
                "temperature_trajectory_missing_fields": temperature_trajectory_status.get("missing_fields") or [],
                "physical_weather_state": physical_weather_modifier.get("state"),
                "physical_weather_peak_mu_adjustment_c": physical_weather_modifier.get("peak_mu_adjustment_c"),
                "physical_weather_path_probability_scale": physical_weather_modifier.get("path_probability_scale"),
                "observed_source_latest_time_local": refresh_cadence.get("latest_observation_time_local"),
                "observed_source_age_minutes": refresh_cadence.get("observation_age_minutes"),
                "observed_source_stale": bool(refresh_cadence.get("source_stale")),
                "observed_source_stale_reason": str(features.get("metar_stale_reason") or ""),
                "metar_temperature_c": _round(features.get("metar_temperature_c"), 2),
                "zuuu_metar_raw_awc_latest_temp_c": _round(features.get("zuuu_metar_raw_awc_latest_temp_c"), 2),
                "zuuu_metar_raw_awc_today_max_c": _round(features.get("zuuu_metar_raw_awc_today_max_c"), 2),
                "zuuu_metar_raw_awc_latest_at": str(features.get("zuuu_metar_raw_awc_latest_at") or ""),
                "zuuu_metar_raw_awc_latest_raw": str(features.get("zuuu_metar_raw_awc_latest_raw") or ""),
                "zuuu_metar_raw_awc_today_max_at": str(features.get("zuuu_metar_raw_awc_today_max_at") or ""),
                "zuuu_metar_raw_awc_today_max_raw": str(features.get("zuuu_metar_raw_awc_today_max_raw") or ""),
                "wu_twc_observed_intraday_current_temp_c": _round(features.get("wu_twc_observed_intraday_current_temp_c"), 2),
                "wu_twc_observed_intraday_today_max_c": _round(features.get("wu_twc_observed_intraday_today_max_c"), 2),
                "wu_twc_observed_intraday_current_at": str(features.get("wu_twc_observed_intraday_current_at") or ""),
                "wu_twc_observed_intraday_today_max_at": str(features.get("wu_twc_observed_intraday_today_max_at") or ""),
                "wu_twc_observed_intraday_history_raw_value": features.get("wu_twc_observed_intraday_history_raw_value"),
                "wu_twc_observed_intraday_history_raw_unit": str(features.get("wu_twc_observed_intraday_history_raw_unit") or ""),
                "wu_twc_observed_intraday_history_raw_imperial_value": features.get("wu_twc_observed_intraday_history_raw_imperial_value"),
                "wu_twc_observed_intraday_history_raw_imperial_unit": str(features.get("wu_twc_observed_intraday_history_raw_imperial_unit") or ""),
                "wu_twc_observed_intraday_current_metric_raw_value": features.get("wu_twc_observed_intraday_current_metric_raw_value"),
                "wu_twc_observed_intraday_current_metric_raw_unit": str(features.get("wu_twc_observed_intraday_current_metric_raw_unit") or ""),
                "wu_twc_observed_intraday_current_imperial_raw_value": features.get("wu_twc_observed_intraday_current_imperial_raw_value"),
                "wu_twc_observed_intraday_current_imperial_raw_unit": str(features.get("wu_twc_observed_intraday_current_imperial_raw_unit") or ""),
                "temperature_rise_rate_c_per_hour": _round(_temperature_rise_rate_value(features), 2),
                **(_temperature_momentum_rate_fields(momentum_rates) if isinstance(momentum_rates, Mapping) else {}),
                "temperature_momentum_rates_c_per_hour": {
                    key: _round(momentum_rates.get(key), 2)
                    for key in TEMPERATURE_MOMENTUM_WINDOWS
                    if isinstance(momentum_rates, Mapping) and momentum_rates.get(key) is not None
                },
                "temperature_momentum_effective_c_per_hour": _round(temperature_momentum.get("effective_rate_c_per_hour"), 2),
                "temperature_momentum_realization_state": str(temperature_momentum.get("state") or "missing"),
                "warming_pace_state": str(warming_pace.get("state") or "unknown"),
                "warming_pace_label": str(warming_pace.get("label") or ""),
                "warming_pace_required_rate_c_per_hour": warming_pace.get("required_rate_c_per_hour"),
                "warming_pace_actual_rate_c_per_hour": warming_pace.get("actual_rate_c_per_hour"),
                "warming_pace_ratio": warming_pace.get("pace_ratio"),
                "target_bucket": target_label,
                "target_bucket_source": str(target_selection.get("selection_priority") or target_selection.get("dynamic_target_source") or ""),
                "target_bucket_source_semantics": str(
                    target_selection.get("target_selection_priority_version") or "dynamic_posterior_highest_settlement_bucket_v4"
                ),
                "target_bucket_probability_pct": target_selection.get("target_probability_pct"),
                "target_bucket_probability_field": target_selection.get("target_probability_field"),
                "target_bucket_posterior_pct": target_row_posterior,
                "target_bucket_posterior": target_row_posterior,
                "target_bucket_posterior_field": target_row_posterior_field,
                "temperature_path_reliability_state": (
                    (temperature_path_distribution.get("reliability") or {}).get("state")
                    if isinstance(temperature_path_distribution.get("reliability"), Mapping)
                    else None
                ),
                "temperature_path_trade_follow_allowed": (
                    (temperature_path_distribution.get("trade_follow") or {}).get("allowed")
                    if isinstance(temperature_path_distribution.get("trade_follow"), Mapping)
                    else None
                ),
                "warming_realization_reliability_pct": realization_reliability.get("warming_realization_pct"),
                "cooling_realization_reliability_pct": realization_reliability.get("cooling_realization_pct"),
                "realization_reliability_confidence_pct": realization_reliability.get("confidence_pct"),
                "realization_reliability_state": realization_reliability.get("state"),
                "peak_estimator_mu_c": _round(features.get("peak_estimator_mu_c"), 2),
                "peak_estimator_sigma_c": _round(features.get("peak_estimator_sigma_c"), 2),
                "peak_estimator_source": str(features.get("peak_estimator_source") or ""),
                "peak_estimator_observed_today_max_source": str(
                    features.get("peak_estimator_observed_today_max_source") or ""
                ),
                "peak_estimator_raw_observed_today_max_c": _round(
                    features.get("peak_estimator_raw_observed_today_max_c"), 2
                ),
                "peak_estimator_raw_observed_today_max_source": str(
                    features.get("peak_estimator_raw_observed_today_max_source") or ""
                ),
                "peak_estimator_raw_observed_today_max_demoted_reason": str(
                    features.get("peak_estimator_raw_observed_today_max_demoted_reason") or ""
                ),
                "forecast_peak_temp_c": _round(
                    _to_float(features.get("forecast_peak_temp"))
                    if _to_float(features.get("forecast_peak_temp")) is not None
                    else (
                        _to_float(features.get("forecast_peak_temp_c"))
                        if _to_float(features.get("forecast_peak_temp_c")) is not None
                        else soft_forecast_peak_c
                    ),
                    2,
                ),
                "soft_forecast_peak_c": _round(soft_forecast_peak_c, 2),
                "soft_forecast_source": soft_forecast_source,
                "soft_forecast_gap_c": _round(
                    soft_forecast_peak_c - observed_path_temp_c
                    if soft_forecast_peak_c is not None and observed_path_temp_c is not None
                    else None,
                    2,
                ),
                "wu_forecast_peak_c": _round(features.get("settlement_source_forecast_peak_c"), 2),
                "weather_com_forecast_peak_c": _round(features.get("weather_com_forecast_peak_c"), 2),
                "twc_forecast_peak_c": _round(features.get("twc_forecast_peak_c"), 2),
                "taf_forecast_peak_c": _round(features.get("taf_forecast_peak_c"), 2),
                "cloud_cover_pct": _round(features.get("cloud_cover_pct"), 1),
                "cloud_cover_low_pct": _round(features.get("cloud_cover_low_pct"), 1),
                "cloud_cover_mid_pct": _round(features.get("cloud_cover_mid_pct"), 1),
                "solar_radiation": _round(features.get("solar_radiation"), 1),
                "dewpoint_c": _round(features.get("dewpoint_c"), 1),
                "pressure_hpa": _round(features.get("pressure_hpa"), 1),
                "awc_metar_no_precip_observed": features.get("awc_metar_no_precip_observed"),
                "metar_cavok_observed": features.get("metar_cavok_observed"),
                "is_raining": features.get("is_raining"),
                "is_raining_observed": features.get("is_raining_observed"),
                "observed_precip_rate_mm_per_h": _round(features.get("observed_precip_rate_mm_per_h"), 2),
                "precip_nowcast_1_3h_available": features.get("precip_nowcast_1_3h_available"),
                "precip_nowcast_1_3h_risk_state": str(features.get("precip_nowcast_1_3h_risk_state") or ""),
                "precip_nowcast_1_3h_max_rate_mm_per_h": _round(features.get("precip_nowcast_1_3h_max_rate_mm_per_h"), 2),
                "precip_nowcast_1_3h_total_mm": _round(features.get("precip_nowcast_1_3h_total_mm"), 2),
                "precip_nowcast_1_3h_max_probability_pct": _round(features.get("precip_nowcast_1_3h_max_probability_pct"), 1),
                "precip_nowcast_1_3h_next_precip_at": str(features.get("precip_nowcast_1_3h_next_precip_at") or ""),
                "precip_nowcast_1_3h_checked_at": str(features.get("precip_nowcast_1_3h_checked_at") or ""),
                "precip_nowcast_1_3h_source": str(features.get("precip_nowcast_1_3h_source") or ""),
            },
        }
    finally:
        conn.close()


def build_log_line(summary: Mapping[str, Any]) -> str:
    if not summary.get("ok"):
        return f"[chengdu-intraday-decision] ok=0 error={summary.get('error')}"
    top = summary.get("top_single_temperature")
    top = top if isinstance(top, Mapping) else {}
    decision = summary.get("decision") if isinstance(summary.get("decision"), Mapping) else {}
    metrics = summary.get("key_metrics") if isinstance(summary.get("key_metrics"), Mapping) else {}
    phys = summary.get("physical_likelihood") if isinstance(summary.get("physical_likelihood"), Mapping) else {}
    guard = summary.get("high_temp_amplification_guard") if isinstance(summary.get("high_temp_amplification_guard"), Mapping) else {}
    trend = summary.get("temperature_trend") if isinstance(summary.get("temperature_trend"), Mapping) else {}
    cadence = summary.get("refresh_cadence") if isinstance(summary.get("refresh_cadence"), Mapping) else {}
    evidence = summary.get("evidence_conclusions") if isinstance(summary.get("evidence_conclusions"), list) else []
    evidence_states = ",".join(
        f"{item.get('key')}:{item.get('state')}"
        for item in evidence
        if isinstance(item, Mapping)
    )
    return (
        "[chengdu-intraday-decision] "
        f"ok=1 event_date={summary.get('event_date')} "
        f"run_id={summary.get('event_run_id')} "
        f"snapshot={summary.get('created_at')} "
        f"weather={summary.get('weather_likelihood_refreshed_at')} "
        f"stance={decision.get('stance')} "
        f"target={decision.get('target_bucket')} "
        f"top_single={top.get('outcome')} "
        f"top_single_q={top.get('target_bucket_probability_pct') or top.get('q_temperature_bucket_posterior_pct') or top.get('q_bayes_posterior_pct')} "
        f"temp={metrics.get('observed_temp_c')} "
        f"max={metrics.get('observed_today_max_c')} "
        f"rise={metrics.get('temperature_rise_rate_c_per_hour')} "
        f"momentum_effective={metrics.get('temperature_momentum_effective_c_per_hour')} "
        f"momentum_15m={metrics.get('temperature_momentum_15m_c_per_hour')} "
        f"momentum_1h={metrics.get('temperature_momentum_1h_c_per_hour')} "
        f"momentum_3h={metrics.get('temperature_momentum_3h_c_per_hour')} "
        f"momentum_state={metrics.get('temperature_momentum_realization_state')} "
        f"trend={trend.get('latest_trend_state')} "
        f"guard_active={int(bool(guard.get('active')))} "
        f"soft_gap={guard.get('forecast_gap_c')} "
        f"refresh={cadence.get('recommended_interval_sec')}s "
        f"refresh_ref={cadence.get('reference_time_local')} "
        f"obs_latest={cadence.get('latest_observation_time_local')} "
        f"time_precision={cadence.get('time_precision')} "
        f"cloud={metrics.get('cloud_cover_pct')} "
        f"low_cloud={metrics.get('cloud_cover_low_pct')} "
        f"dewpoint={metrics.get('dewpoint_c')} "
        f"precip_rate={metrics.get('observed_precip_rate_mm_per_h')} "
        f"phys_status={phys.get('status')} "
        f"evidence={evidence_states} "
        f"action={decision.get('action')}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Chengdu intraday decision summary")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--date", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-line", action="store_true")
    args = parser.parse_args(argv)
    summary = build_chengdu_intraday_summary(Path(args.db), event_date=args.date or None)
    if args.json and not args.log_line:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(build_log_line(summary))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
