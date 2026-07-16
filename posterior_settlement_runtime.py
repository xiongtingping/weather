from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


STRATEGY_FAMILY = "posterior_settlement"
LANE_YES_TARGET = "yes_realized_target"
LANE_OBSERVE = "observe"
ALLOWED_LANES = (LANE_YES_TARGET, LANE_OBSERVE)

DEFAULT_YES_RELIABILITY_MIN = 0.60

TRAJECTORY_SUPPORTED = "supported"
TRAJECTORY_UNSUPPORTED = "unsupported"
TRAJECTORY_NEUTRAL = "neutral"
TRAJECTORY_MISSING = "missing"
TRAJECTORY_STATES = (
    TRAJECTORY_SUPPORTED,
    TRAJECTORY_UNSUPPORTED,
    TRAJECTORY_NEUTRAL,
    TRAJECTORY_MISSING,
)

ACTION_HOLD = "HOLD"
ACTION_HOLD_RECONFIRMED = "HOLD_RECONFIRMED"
ACTION_EXIT_CLEAR = "EXIT_CLEAR"
ACTION_SWITCH_BUY = "SWITCH_BUY"
ACTION_OBSERVE_REPRICE = "OBSERVE_REPRICE"
ADJUSTMENT_ACTIONS = (
    ACTION_HOLD,
    ACTION_HOLD_RECONFIRMED,
    ACTION_EXIT_CLEAR,
    ACTION_SWITCH_BUY,
    ACTION_OBSERVE_REPRICE,
)

DEFAULT_UNREALIZABLE_RELIABILITY_MAX = 0.55
DEFAULT_SWITCH_RELIABILITY_MIN = 0.65
DEFAULT_SWITCH_POSTERIOR_LEAD_MIN = 0.05
DEFAULT_UNREALIZABLE_CONSECUTIVE_SNAPSHOTS = 2
DEFAULT_D0_TRAJECTORY_SWITCH_POSTERIOR_LAG_MAX = 0.06
DEFAULT_D0_TRAJECTORY_SWITCH_POSTERIOR_MIN = 0.24
CHENGDU_TZ = timezone(timedelta(hours=8))


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
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


def _clamp01(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return max(0.0, min(1.0, float(value)))


def _prob(value: Any) -> Optional[float]:
    val = _to_float(value)
    if val is None:
        return None
    if val > 1.0 and val <= 100.0:
        val = val / 100.0
    return _clamp01(val)


def _first_float(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> Optional[float]:
    for key in keys:
        val = _to_float(candidate.get(key))
        if val is not None:
            return val
        if isinstance(context, Mapping):
            val = _to_float(context.get(key))
            if val is not None:
                return val
    return None


def _observed_path_temperature(
    observed_max: Optional[float],
    current_temp: Optional[float],
) -> Optional[float]:
    if observed_max is not None and current_temp is not None:
        return max(float(observed_max), float(current_temp))
    if observed_max is not None:
        return float(observed_max)
    if current_temp is not None:
        return float(current_temp)
    return None


def _first_prob(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> Optional[float]:
    for key in keys:
        val = _prob(candidate.get(key))
        if val is not None:
            return val
        if isinstance(context, Mapping):
            val = _prob(context.get(key))
            if val is not None:
                return val
    return None


def _first_str(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> str:
    for key in keys:
        val = candidate.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
        if isinstance(context, Mapping):
            val = context.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def _first_bool(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> Optional[bool]:
    for key in keys:
        val = _to_bool(candidate.get(key))
        if val is not None:
            return val
        if isinstance(context, Mapping):
            val = _to_bool(context.get(key))
            if val is not None:
                return val
    return None


def normalize_bucket_label(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("℃", "C").replace("° C", "°C")
    text = re.sub(r"\s+", " ", text)
    return text


def bucket_anchor(label: Any) -> Optional[float]:
    text = normalize_bucket_label(label)
    if not text:
        return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    if not nums:
        return None
    vals = []
    for item in nums[:2]:
        val = _to_float(item)
        if val is not None:
            vals.append(val)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return sum(vals) / len(vals)


def bucket_distance(left: Any, right: Any) -> Optional[int]:
    left_anchor = bucket_anchor(left)
    right_anchor = bucket_anchor(right)
    if left_anchor is None or right_anchor is None:
        return None
    return int(abs(round(float(left_anchor) - float(right_anchor))))


def bucket_directional_distance(left: Any, right: Any) -> Optional[int]:
    left_anchor = bucket_anchor(left)
    right_anchor = bucket_anchor(right)
    if left_anchor is None or right_anchor is None:
        return None
    return int(round(float(left_anchor) - float(right_anchor)))


def _bucket_upper(label: Any) -> Optional[float]:
    text = normalize_bucket_label(label).lower()
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    vals = [_to_float(item) for item in nums[:2]]
    vals = [val for val in vals if val is not None]
    if not vals:
        return None
    if "or below" in text or "below" in text or text.startswith("<="):
        return float(vals[0])
    return max(float(val) for val in vals)


def _same_bucket(left: Any, right: Any) -> bool:
    return normalize_bucket_label(left).lower() == normalize_bucket_label(right).lower()


def _is_lower_boundary_bucket(label: Any) -> bool:
    text = normalize_bucket_label(label).lower()
    return bool(text and ("or below" in text or "below" in text or text.startswith("<=")))


def _is_cumulative_bucket(label: Any) -> bool:
    text = normalize_bucket_label(label).lower()
    return bool(
        text
        and (
            "or below" in text
            or "below" in text
            or text.startswith("<=")
            or "or higher" in text
            or "higher" in text
            or "above" in text
            or text.startswith(">=")
        )
    )


def _is_single_temperature_bucket(label: Any) -> bool:
    text = normalize_bucket_label(label).lower()
    if not text or _is_cumulative_bucket(text):
        return False
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    if len(nums) != 1:
        return False
    if " to " in text or "-" in text.replace("°c", "").replace(" c", ""):
        return False
    return True


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _d0_intraday_switch_window(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    reference_time = _parse_datetime(
        _first_str(
            candidate,
            context,
            "weather_likelihood_refreshed_at",
            "zuuu_metar_raw_awc_latest_at",
            "wu_twc_observed_intraday_today_max_at",
            "wu_twc_observed_intraday_current_at",
            "created_at",
            "snapshot_id",
        )
    )
    event_date = _first_str(candidate, context, "event_date", "market_event_date", "metar_target_event_date")
    event_key = event_date.split("T", 1)[0] if event_date else ""
    if reference_time is None:
        return {"active": False, "reason": "missing_reference_time", "event_date": event_key or None}
    local_dt = reference_time.astimezone(CHENGDU_TZ)
    is_d0 = bool(event_key and event_key == local_dt.date().isoformat())
    peak_hour = _first_float(candidate, context, "local_peak_hour", "expected_peak_hour_local")
    if peak_hour is None:
        peak_hour = 15.0
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
        "reference_time_utc": reference_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "reference_time_local": local_dt.isoformat(timespec="seconds"),
        "local_hour": round(local_hour, 2),
        "peak_hour_local": round(float(peak_hour), 2),
        "hours_to_peak_window": round(hours_to_peak, 2),
    }


def _bucket_prob_from_context(context: Optional[Mapping[str, Any]], bucket_label: Any) -> Optional[float]:
    label = normalize_bucket_label(bucket_label)
    if not label or not isinstance(context, Mapping):
        return None
    by_label = context.get("posterior_settlement_bucket_prob_by_label")
    if not isinstance(by_label, Mapping):
        return None
    meta = by_label.get(label.lower())
    if not isinstance(meta, Mapping):
        return None
    return _first_prob(meta, None, "posterior_settlement_candidate_bucket_prob", "prob", "p")


def _normalize_trajectory_support_state(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace("-", "_").replace(" ", "_")
    supported_values = {
        "supported",
        "support",
        "supports",
        "realized",
        "realizing",
        "target_realizing",
        "target_supported",
        "sustained_warming",
        "warming_realized",
        "warming_supported",
        "warming_support",
        "low_boundary_holding",
        "boundary_holding",
    }
    unsupported_values = {
        "unsupported",
        "not_supported",
        "not_realized",
        "target_unsupported",
        "target_unrealizable",
        "unrealizable",
        "weakening",
        "cooling",
        "stalled",
        "warming_momentum_not_realized",
        "current_temp_far_below_target_without_warming",
        "lower_boundary_breakout_risk",
        "breakout_risk",
    }
    neutral_values = {"neutral", "mixed", "uncertain", "flat", "stable", "unknown"}
    missing_values = {"missing", "na", "n/a", "none", "null"}
    if text in supported_values:
        return TRAJECTORY_SUPPORTED
    if text in unsupported_values:
        return TRAJECTORY_UNSUPPORTED
    if text in neutral_values:
        return TRAJECTORY_NEUTRAL
    if text in missing_values:
        return TRAJECTORY_MISSING
    return ""


def infer_trajectory_support_state(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    target_bucket: Any,
) -> str:
    explicit = _first_str(
        candidate,
        context,
        "trajectory_support_state",
        "target_trajectory_support_state",
        "temperature_trajectory_support_state",
        "target_realization_trajectory_support_state",
    )
    normalized = _normalize_trajectory_support_state(explicit)
    if normalized:
        return normalized

    target_label = normalize_bucket_label(target_bucket)
    if not target_label:
        return TRAJECTORY_MISSING
    lower_boundary = "below" in target_label.lower() or target_label.startswith("<=")
    target_upper = _bucket_upper(target_label)
    target_anchor = bucket_anchor(target_label)
    observed_max = _first_float(
        candidate,
        context,
        "observed_today_max_c",
        "observed_max_c",
        "zuuu_metar_raw_awc_today_max_c",
        "wu_twc_observed_intraday_today_max_c",
    )
    current_temp = _observed_path_temperature(
        observed_max,
        _first_float(candidate, context, "observed_path_temp_c", "observed_temp_c", "current_temp_c", "metar_temperature_c"),
    )
    effective_rate = _first_float(
        candidate,
        context,
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
        "warming_rate_c_per_hour",
    )
    state_text = _normalize_trajectory_support_state(
        _first_str(candidate, context, "temperature_momentum_realization_state")
    )

    if observed_max is not None and target_upper is not None:
        if float(observed_max) > float(target_upper) + 0.05:
            return TRAJECTORY_UNSUPPORTED
        if lower_boundary:
            gap_to_break = float(target_upper) + 1.0 - float(observed_max)
            if effective_rate is not None and effective_rate >= 0.45 and gap_to_break <= 1.25:
                return TRAJECTORY_UNSUPPORTED
            if gap_to_break >= 1.5 and (effective_rate is None or effective_rate <= 0.25):
                return TRAJECTORY_SUPPORTED
        elif target_anchor is not None:
            gap_to_target = float(target_anchor) - float(observed_max)
            if abs(gap_to_target) <= 0.65:
                return TRAJECTORY_SUPPORTED
            if gap_to_target > 0.65:
                if effective_rate is not None and effective_rate >= 0.45:
                    return TRAJECTORY_SUPPORTED
                if effective_rate is not None and effective_rate <= 0.05:
                    return TRAJECTORY_UNSUPPORTED

    if current_temp is not None and target_anchor is not None and effective_rate is not None:
        if not lower_boundary and float(current_temp) < float(target_anchor) - 2.0 and effective_rate <= 0.0:
            return TRAJECTORY_UNSUPPORTED

    if state_text in {TRAJECTORY_SUPPORTED, TRAJECTORY_UNSUPPORTED}:
        return state_text
    if state_text:
        return state_text
    return TRAJECTORY_MISSING


def _int_value(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], *keys: str) -> int:
    for key in keys:
        raw = candidate.get(key)
        if raw is None and isinstance(context, Mapping):
            raw = context.get(key)
        val = _to_float(raw)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                return 0
    return 0


def _append_reason(reasons: list[str], reason: str) -> None:
    text = str(reason or "").strip()
    if text and text not in reasons:
        reasons.append(text)


def _trajectory_state_from_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace("-", "_").replace(" ", "_")
    if text in {"supported", "support", "realized", "realizing", "target_realizing", "target_supported"}:
        return TRAJECTORY_SUPPORTED
    if text in {
        "sustained_warming",
        "warming_realized",
        "warming_supported",
        "warming_realizing",
        "realized_warming",
        "positive_momentum",
    }:
        return TRAJECTORY_SUPPORTED
    if text in {
        "unsupported",
        "not_supported",
        "not_realized",
        "not_realizing",
        "target_unrealizable",
        "unrealizable",
        "weakening",
        "cooling",
        "stalled",
        "failed",
        "blocked",
        "negative_momentum",
    }:
        return TRAJECTORY_UNSUPPORTED
    if text in {"neutral", "mixed", "flat", "uncertain", "basic_stable", "stable", "unknown"}:
        return TRAJECTORY_NEUTRAL
    if text in {"missing", "na", "n/a", "none", "no_data"}:
        return TRAJECTORY_MISSING
    if "unsupported" in text or "unrealizable" in text or "not_realiz" in text:
        return TRAJECTORY_UNSUPPORTED
    if "weakening" in text or "cooling" in text or "stalled" in text:
        return TRAJECTORY_UNSUPPORTED
    if "supported" in text or "realiz" in text or "warming" in text:
        return TRAJECTORY_SUPPORTED
    return ""


def _explicit_trajectory_state(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> str:
    for key in (
        "trajectory_support_state",
        "target_trajectory_support_state",
        "temperature_trajectory_support_state",
        "posterior_settlement_trajectory_support_state",
        "temperature_momentum_realization_state",
        "temperature_momentum_state",
        "target_realization_state",
        "realization_state",
        "momentum_realization_state",
    ):
        state = _trajectory_state_from_text(candidate.get(key))
        if state:
            return state
        if isinstance(context, Mapping):
            state = _trajectory_state_from_text(context.get(key))
            if state:
                return state
    return ""


def infer_trajectory_support_state(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    *,
    target_bucket: Any = None,
) -> str:
    explicit = _explicit_trajectory_state(candidate, context)
    if explicit:
        return explicit

    bucket = normalize_bucket_label(target_bucket or _first_str(candidate, context, "target_bucket", "posterior_settlement_target_bucket"))
    observed_max = _first_float(
        candidate,
        context,
        "observed_today_max_c",
        "observed_max_c",
        "zuuu_metar_raw_awc_today_max_c",
        "wu_twc_observed_intraday_today_max_c",
    )
    current_temp = _observed_path_temperature(
        observed_max,
        _first_float(candidate, context, "observed_path_temp_c", "observed_temp_c", "current_temp_c", "metar_temperature_c"),
    )
    effective_rate = _first_float(
        candidate,
        context,
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
        "warming_rate_c_per_hour",
    )
    target_upper = _bucket_upper(bucket)
    target_anchor = bucket_anchor(bucket)
    lower_boundary = "below" in bucket.lower() or bucket.startswith("<=")

    if bucket and observed_max is not None and target_upper is not None:
        if lower_boundary:
            if float(observed_max) > float(target_upper) + 0.05:
                return TRAJECTORY_UNSUPPORTED
            if effective_rate is not None:
                if float(observed_max) >= float(target_upper) - 1.0 and effective_rate >= 0.35:
                    return TRAJECTORY_UNSUPPORTED
                if float(observed_max) <= float(target_upper) - 0.75 and effective_rate <= 0.15:
                    return TRAJECTORY_SUPPORTED
        elif target_anchor is not None:
            if float(observed_max) > float(target_upper) + 0.05:
                return TRAJECTORY_UNSUPPORTED
            if abs(float(target_anchor) - float(observed_max)) <= 0.55:
                return TRAJECTORY_SUPPORTED
            if effective_rate is not None:
                if float(observed_max) < float(target_anchor) - 1.8 and effective_rate <= 0.05:
                    return TRAJECTORY_UNSUPPORTED
                if float(observed_max) < float(target_anchor) - 0.6 and effective_rate >= 0.35:
                    return TRAJECTORY_SUPPORTED

    if bucket and current_temp is not None and target_anchor is not None and effective_rate is not None:
        if not lower_boundary and float(current_temp) < float(target_anchor) - 2.0 and effective_rate <= 0.0:
            return TRAJECTORY_UNSUPPORTED

    if effective_rate is not None:
        if effective_rate >= 0.45:
            return TRAJECTORY_SUPPORTED
        if effective_rate <= -0.05:
            return TRAJECTORY_UNSUPPORTED
        return TRAJECTORY_NEUTRAL
    return TRAJECTORY_MISSING


def _candidate_bucket(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> str:
    return normalize_bucket_label(
        _first_str(
            candidate,
            context,
            "outcome",
            "q_temperature_bucket_label",
            "bucket_label",
            "label",
        )
    )


def _bucket_probability(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> Optional[float]:
    return _first_prob(
        candidate,
        context,
        "candidate_bucket_realization_prob",
        "posterior_settlement_candidate_bucket_prob",
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
    )


def _target_context_hard_zero(row: Mapping[str, Any]) -> bool:
    if not _observed_constraint_signal_allowed(row):
        return False
    if _to_bool(row.get("q_bayes_physical_likelihood_hard_zero")) is True:
        return True
    if _to_bool(row.get("impossible_by_observed_max")) is True:
        return True
    if _to_bool(row.get("suppressed_by_observed_feasibility")) is True:
        factor = _to_float(row.get("observed_feasibility_factor"))
        if factor is None or factor <= 1e-6:
            return True
    return False


def _target_context_hard_zero_reasons(row: Mapping[str, Any]) -> list[str]:
    if not _observed_constraint_signal_allowed(row):
        return []
    reasons: list[str] = []
    raw = row.get("q_bayes_physical_likelihood_hard_reasons")
    if isinstance(raw, (list, tuple)):
        reasons.extend(str(item).strip() for item in raw if str(item).strip())
    elif raw is not None and str(raw).strip():
        reasons.append(str(raw).strip())
    if _to_bool(row.get("impossible_by_observed_max")) is True:
        reason = str(row.get("impossible_reason") or "observed_today_max_above_bucket_upper_bound").strip()
        if reason:
            reasons.append(reason)
    if _to_bool(row.get("suppressed_by_observed_feasibility")) is True:
        reason = str(row.get("suppressed_reason") or "observed_trajectory_near_zero_bucket_feasibility").strip()
        if reason:
            reasons.append(reason)
    return list(dict.fromkeys(reasons))


def _observed_constraint_signal_allowed(row: Mapping[str, Any]) -> bool:
    """Return true when observed/physical hard constraints are valid for this event."""
    if _to_bool(row.get("market_closed")) is True:
        return True
    for key in (
        "temperature_observation_final_daily_max",
        "observed_today_max_is_final",
        "daily_max_observation_complete",
        "observed_today_max_final",
    ):
        if _to_bool(row.get(key)) is True:
            return True

    d0_explanation = row.get("q_bayes_physical_d0_explanation")
    if isinstance(d0_explanation, Mapping) and _to_bool(d0_explanation.get("d0")) is True:
        return True

    obs_source = str(row.get("observed_nowcast_source") or "").strip()
    if obs_source:
        return True
    if _first_prob(row, None, "observed_nowcast_value", "observed_today_max_c") is not None:
        return True

    target_date = str(
        row.get("zuuu_metar_raw_awc_target_event_date")
        or row.get("event_date")
        or ""
    ).strip()
    observation_date = str(
        row.get("zuuu_metar_raw_awc_observation_local_date")
        or row.get("observation_local_date")
        or ""
    ).strip()
    if target_date and observation_date:
        return target_date == observation_date

    if _to_bool(row.get("market_closed")) is False:
        return False

    # Historical/test call sites without market state are treated as final-like.
    return True


def _observed_feasibility_likelihood_allowed(row: Mapping[str, Any]) -> bool:
    """Intraday observed maxima can eliminate buckets, but cannot prove final high."""
    if _to_bool(row.get("market_closed")) is True:
        return True
    final_keys = (
        "temperature_observation_final_daily_max",
        "observed_today_max_is_final",
        "daily_max_observation_complete",
        "observed_today_max_final",
    )
    for key in final_keys:
        if _to_bool(row.get(key)) is True:
            return True
    if _to_bool(row.get("market_closed")) is False:
        return False
    for key in final_keys:
        if key in row and _to_bool(row.get(key)) is False:
            return False
    if any(
        key in row
        for key in (
            "observed_nowcast_source",
            "observed_nowcast_source_kind",
            "observed_nowcast_value",
        )
    ):
        return False
    # Historical/test call sites that do not carry market state are treated as
    # final-like; live intraday rows must carry market_closed=false or nowcast.
    return True


def _observed_feasibility_likelihood_skipped(row: Mapping[str, Any]) -> bool:
    if _observed_feasibility_likelihood_allowed(row):
        return False
    return (
        _first_prob(
            row,
            None,
            "q_bayes_physical_likelihood_observed_prob",
            "q_bayes_independent_physics_residual_observed_prob",
        )
        is not None
    )


def _clear_observed_feasibility_constraint_meta(row: Dict[str, Any]) -> None:
    for key in (
        "q_posterior_observed_constraints_observed_feasibility_factor",
        "q_posterior_observed_feasibility_applied",
        "q_posterior_observed_feasibility_factor",
    ):
        row.pop(key, None)
    row["q_posterior_observed_feasibility_applied"] = False
    row["q_posterior_observed_feasibility_skipped_reason"] = (
        "intraday_observed_max_not_final"
    )


def _restore_pre_observed_constraints(rows: Sequence[Mapping[str, Any]]) -> bool:
    touched = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_touched = False
        canonical_pre_val = _prob(row.get("q_temperature_bucket_posterior_pre_observed_constraints"))
        for key in (
            "q_temperature_bucket_posterior",
            "q_bayes_posterior",
            "q_contract_payout_posterior",
        ):
            pre_key = f"{key}_pre_observed_constraints"
            pre_val = _prob(row.get(pre_key))
            if key != "q_temperature_bucket_posterior" and canonical_pre_val is not None:
                pre_val = canonical_pre_val
            if pre_val is None:
                continue
            current = _prob(row.get(key))
            if current is None or abs(float(current) - float(pre_val)) > 1e-12:
                row[key] = float(pre_val)
                row_touched = True
        no_pre = _prob(row.get("q_bayes_posterior_no_pre_observed_constraints"))
        if no_pre is not None and "q_bayes_posterior_no" in row:
            current_no = _prob(row.get("q_bayes_posterior_no"))
            if current_no is None or abs(float(current_no) - float(no_pre)) > 1e-12:
                row["q_bayes_posterior_no"] = float(no_pre)
                row_touched = True
        if row_touched:
            _mark_observed_constraints_not_applied(
                row,
                "observed_constraints_not_applicable",
            )
            touched = True
    return touched


def _mark_observed_constraints_not_applied(row: Dict[str, Any], reason: str) -> bool:
    touched = False
    if row.get("q_posterior_observed_constraints_applied") is not False:
        row["q_posterior_observed_constraints_applied"] = False
        touched = True
    for key in (
        "q_posterior_observed_constraints_source",
        "q_posterior_observed_constraints_pre_prob",
        "q_posterior_observed_constraints_post_prob",
        "q_posterior_observed_constraints_reason_codes",
        "q_posterior_observed_constraints_hard_zero_reasons",
        "q_posterior_observed_constraints_observed_feasibility_factor",
        "q_temperature_bucket_posterior_pre_observed_constraints",
        "q_bayes_posterior_pre_observed_constraints",
        "q_contract_payout_posterior_pre_observed_constraints",
        "q_bayes_posterior_no_pre_observed_constraints",
    ):
        if key in row:
            row.pop(key, None)
            touched = True
    if row.get("q_posterior_observed_constraints_hard_zero") is not False:
        row["q_posterior_observed_constraints_hard_zero"] = False
        touched = True
    if row.get("q_posterior_observed_constraints_skipped_reason") != reason:
        row["q_posterior_observed_constraints_skipped_reason"] = reason
        touched = True
    if row.get("q_posterior_observed_feasibility_applied") is True:
        row["q_posterior_observed_feasibility_applied"] = False
        touched = True
    return touched


def _has_hard_source_posterior_override(rows: Sequence[Mapping[str, Any]]) -> bool:
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if _to_bool(row.get("hard_source_posterior_override_applied")) is True:
            return True
        if _to_bool(row.get("hard_source_posterior_override_final_bucket_lock")) is True:
            return True
        if str(row.get("q_posterior_observed_constraints_source") or "").strip() == "hard_source_posterior_override":
            return True
    return False


def _target_reliability(candidate: Mapping[str, Any], context: Optional[Mapping[str, Any]], target_bucket: str, target_prob: Optional[float]) -> Tuple[float, str]:
    explicit = _first_prob(
        candidate,
        context,
        "target_realization_path_p_reach_yes",
        "target_realization_signal_confidence_yes",
        "target_realization_reliability",
        "posterior_settlement_target_realization_reliability",
        "temperature_realization_reliability",
        "realization_reliability",
        "target_bucket_realization_reliability",
        "warming_realization_reliability_pct",
    )
    if explicit is not None:
        return float(explicit), "explicit_target_realization_reliability"

    base = float(target_prob or 0.0)
    phys = _first_prob(
        candidate,
        context,
        "q_bayes_physical_likelihood_observed_prob",
        "q_bayes_independent_physics_residual_observed_prob",
        "q_bayes_physical_likelihood_prob",
        "q_bayes_independent_physics_residual_prob",
    )
    if phys is not None:
        base = max(base, 0.45 * base + 0.55 * float(phys))

    observed_max = _first_float(
        candidate,
        context,
        "observed_today_max_c",
        "observed_max_c",
        "zuuu_metar_raw_awc_today_max_c",
        "wu_twc_observed_intraday_today_max_c",
    )
    current_temp = _observed_path_temperature(
        observed_max,
        _first_float(candidate, context, "observed_path_temp_c", "observed_temp_c", "current_temp_c", "metar_temperature_c"),
    )
    effective_rate = _first_float(
        candidate,
        context,
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
        "warming_rate_c_per_hour",
    )
    target_upper = _bucket_upper(target_bucket)
    target_anchor = bucket_anchor(target_bucket)
    lower_boundary = "below" in normalize_bucket_label(target_bucket).lower() or normalize_bucket_label(target_bucket).startswith("<=")

    reason_bits = ["derived_from_posterior"]
    if observed_max is not None and target_upper is not None:
        if lower_boundary:
            if float(observed_max) > float(target_upper) + 0.05:
                return 0.03, "hard_observed_max_broke_lower_boundary"
            gap = float(target_upper) + 1.0 - float(observed_max)
            if gap >= 2.0 and (effective_rate is None or effective_rate <= 0.35):
                base = max(base, 0.72)
                reason_bits.append("lower_boundary_holding")
            elif gap <= 0.8 and effective_rate is not None and effective_rate >= 0.45:
                base = min(base, 0.45)
                reason_bits.append("lower_boundary_breakout_risk")
        elif target_anchor is not None:
            if float(observed_max) > float(target_upper) + 0.05:
                return 0.05, "hard_observed_max_above_target_bucket"
            gap_to_target = float(target_anchor) - float(observed_max)
            if abs(gap_to_target) <= 0.55:
                base = max(base, 0.82)
                reason_bits.append("observed_max_near_target_bucket")
            elif gap_to_target > 0.55 and effective_rate is not None:
                if effective_rate >= 0.45:
                    base = max(base, min(0.82, base + 0.22))
                    reason_bits.append("warming_momentum_supports_target")
                elif effective_rate <= 0.05:
                    base = min(base, 0.45)
                    reason_bits.append("warming_momentum_not_realized")

    if current_temp is not None and target_anchor is not None and effective_rate is not None:
        if not lower_boundary and float(current_temp) < float(target_anchor) - 2.0 and effective_rate <= 0.0:
            base = min(base, 0.35)
            reason_bits.append("current_temp_far_below_target_without_warming")

    return max(0.0, min(0.98, float(base))), ",".join(reason_bits)


def _bucket_meta_from_context(context: Optional[Mapping[str, Any]], bucket: str) -> Optional[Mapping[str, Any]]:
    if not isinstance(context, Mapping) or not bucket:
        return None
    by_label = context.get("posterior_settlement_bucket_prob_by_label")
    if not isinstance(by_label, Mapping):
        return None
    meta = by_label.get(bucket.lower())
    if isinstance(meta, Mapping):
        return meta
    return None


def _bucket_prob_from_context(context: Optional[Mapping[str, Any]], bucket: str) -> Optional[float]:
    if not isinstance(context, Mapping) or not bucket:
        return None
    meta = _bucket_meta_from_context(context, bucket)
    if isinstance(meta, Mapping):
        return _prob(
            meta.get("posterior_settlement_candidate_bucket_prob")
            if "posterior_settlement_candidate_bucket_prob" in meta
            else meta.get("prob")
        )
    by_label = context.get("posterior_settlement_bucket_prob_by_label")
    if not isinstance(by_label, Mapping):
        return None
    meta = by_label.get(bucket.lower())
    return _prob(meta)


def _bucket_hard_zero_from_context(context: Optional[Mapping[str, Any]], bucket: str) -> bool:
    meta = _bucket_meta_from_context(context, normalize_bucket_label(bucket))
    if not isinstance(meta, Mapping):
        return False
    if _to_bool(meta.get("posterior_settlement_candidate_bucket_hard_zero")) is True:
        return True
    prob = _prob(meta.get("posterior_settlement_candidate_bucket_prob"))
    raw_prob = _prob(meta.get("posterior_settlement_candidate_bucket_prob_raw"))
    reasons = meta.get("posterior_settlement_candidate_bucket_hard_zero_reasons")
    has_reason = bool(reasons) if isinstance(reasons, (list, tuple)) else bool(str(reasons or "").strip())
    return bool(prob is not None and prob <= 1e-9 and raw_prob is not None and raw_prob > 1e-9 and has_reason)


def _posterior_for_bucket(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    bucket: str,
    *,
    target_bucket: str = "",
    current_top_bucket: str = "",
) -> Optional[float]:
    if not bucket:
        return None
    by_label_prob = _bucket_prob_from_context(context, bucket)
    if by_label_prob is not None:
        return by_label_prob
    candidate_bucket = _candidate_bucket(candidate, context)
    if candidate_bucket and _same_bucket(candidate_bucket, bucket):
        prob = _bucket_probability(candidate, context)
        if prob is not None:
            return prob
    if target_bucket and _same_bucket(target_bucket, bucket):
        prob = _first_prob(candidate, context, "target_posterior", "posterior_settlement_target_posterior")
        if prob is not None:
            return prob
    if current_top_bucket and _same_bucket(current_top_bucket, bucket):
        prob = _first_prob(candidate, context, "current_top_posterior", "posterior_settlement_current_top_posterior")
        if prob is not None:
            return prob
    return None


def _soft_observed_confirms_bucket(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    bucket: str,
) -> bool:
    explicit_bucket = normalize_bucket_label(
        _first_str(
            candidate,
            context,
            "soft_observed_bucket",
            "observed_bucket",
            "settlement_proxy_bucket",
            "target_observation_bucket",
            "dynamic_target_bucket",
        )
    )
    if explicit_bucket and _same_bucket(explicit_bucket, bucket):
        return True
    anchor = bucket_anchor(bucket)
    upper = _bucket_upper(bucket)
    if anchor is None or upper is None:
        return False
    observed_soft = _first_float(
        candidate,
        context,
        "wu_twc_observed_intraday_today_max_c",
        "wu_twc_observed_intraday_current_c",
        "weather_com_observed_today_max_c",
    )
    if observed_soft is None:
        return False
    return bool(float(observed_soft) >= float(anchor) - 0.55 and float(observed_soft) <= float(upper) + 0.95)


def _d0_posterior_trajectory_switch_pressure(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    *,
    held_bucket: str,
    candidate_bucket: str,
    candidate_posterior: Optional[float],
    held_posterior: Optional[float],
    candidate_trajectory_state: str,
) -> Tuple[bool, Dict[str, Any]]:
    held_label = normalize_bucket_label(held_bucket)
    switch_label = normalize_bucket_label(candidate_bucket)
    if not held_label or not switch_label or _same_bucket(held_label, switch_label):
        return False, {}
    if not _is_lower_boundary_bucket(held_label) or not _is_single_temperature_bucket(switch_label):
        return False, {}
    held_upper = _bucket_upper(held_label)
    switch_anchor = bucket_anchor(switch_label)
    if held_upper is not None and switch_anchor is not None and float(switch_anchor) <= float(held_upper):
        return False, {}

    window = _d0_intraday_switch_window(candidate, context)
    held_hard_zero = _bucket_hard_zero_from_context(context, held_label)
    hard_zero_window_override = bool(
        held_hard_zero
        and str(window.get("reason") or "") == "outside_intraday_switch_window"
        and str(window.get("event_date") or "").strip()
    )
    if not window.get("active") and not hard_zero_window_override:
        return False, {"intraday_window": window}

    posterior_lead = None
    if candidate_posterior is not None and held_posterior is not None:
        posterior_lead = float(candidate_posterior) - float(held_posterior)

    soft_observed_confirmed = _soft_observed_confirms_bucket(candidate, context, switch_label)
    candidate_meta = _bucket_meta_from_context(context, switch_label)
    candidate_observed_feasibility_factor = (
        _prob(candidate_meta.get("posterior_settlement_candidate_observed_feasibility_factor"))
        if isinstance(candidate_meta, Mapping)
        else None
    )
    observed_feasibility_supports_candidate = bool(
        candidate_observed_feasibility_factor is not None
        and float(candidate_observed_feasibility_factor) >= 0.50
        and candidate_posterior is not None
        and float(candidate_posterior) >= DEFAULT_D0_TRAJECTORY_SWITCH_POSTERIOR_MIN
    )
    momentum_supports_warming = bool(
        candidate_trajectory_state == TRAJECTORY_SUPPORTED
        or observed_feasibility_supports_candidate
        or (
            (_first_float(
                candidate,
                context,
                "temperature_momentum_effective_c_per_hour",
                "temperature_momentum_1h_c_per_hour",
                "warming_rate_c_per_hour",
            )
            or -999.0)
            >= 0.35
        )
    )
    posterior_lag = -float(posterior_lead) if posterior_lead is not None else None
    posterior_confirmed = bool(
        posterior_lead is not None
        and posterior_lag is not None
        and float(posterior_lag) <= DEFAULT_D0_TRAJECTORY_SWITCH_POSTERIOR_LAG_MAX
        and candidate_posterior is not None
        and float(candidate_posterior) >= DEFAULT_D0_TRAJECTORY_SWITCH_POSTERIOR_MIN
    )
    held_trajectory_state = infer_trajectory_support_state(candidate, context, target_bucket=held_label)
    held_trajectory_no_longer_supported = bool(
        held_hard_zero
        or held_trajectory_state in {TRAJECTORY_UNSUPPORTED, TRAJECTORY_NEUTRAL, TRAJECTORY_MISSING}
    )
    allowed = bool(
        held_trajectory_no_longer_supported
        and momentum_supports_warming
        and (soft_observed_confirmed or posterior_confirmed)
    )
    if soft_observed_confirmed:
        trigger = "soft_observed_touched"
    elif posterior_confirmed:
        trigger = "posterior_trajectory_candidate_leads"
    else:
        trigger = "conditions_not_met"
    return allowed, {
        "trigger": trigger,
        "intraday_window": window,
        "held_target_bucket": held_label,
        "candidate_bucket": switch_label,
        "held_target_posterior": float(held_posterior) if held_posterior is not None else None,
        "candidate_posterior": float(candidate_posterior) if candidate_posterior is not None else None,
        "posterior_lead": float(posterior_lead) if posterior_lead is not None else None,
        "posterior_lag": float(posterior_lag) if posterior_lag is not None else None,
        "posterior_lag_max": float(DEFAULT_D0_TRAJECTORY_SWITCH_POSTERIOR_LAG_MAX),
        "candidate_posterior_min": float(DEFAULT_D0_TRAJECTORY_SWITCH_POSTERIOR_MIN),
        "soft_observed_confirmed": bool(soft_observed_confirmed),
        "posterior_confirmed": bool(posterior_confirmed),
        "candidate_observed_feasibility_factor": (
            float(candidate_observed_feasibility_factor)
            if candidate_observed_feasibility_factor is not None
            else None
        ),
        "observed_feasibility_supports_candidate": bool(observed_feasibility_supports_candidate),
        "momentum_supports_warming": bool(momentum_supports_warming),
        "held_bucket_hard_zero": bool(held_hard_zero),
        "held_trajectory_state": held_trajectory_state,
        "held_trajectory_no_longer_supported": bool(held_trajectory_no_longer_supported),
        "hard_zero_window_override": bool(hard_zero_window_override),
        "decision_inputs": "posterior_probability_and_temperature_trajectory_only",
    }


def _unsupported_snapshot_count(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    trajectory_state: str = "",
) -> int:
    raw = _first_float(
        candidate,
        context,
        "target_unrealizable_consecutive_snapshots",
        "trajectory_unsupported_snapshot_count",
        "target_unsupported_consecutive_snapshots",
        "unsupported_snapshot_count",
        "posterior_settlement_unsupported_snapshot_count",
    )
    if raw is not None:
        return max(0, int(raw))
    previous = _first_float(
        candidate,
        context,
        "target_unrealizable_previous_consecutive_snapshots",
        "previous_target_unrealizable_consecutive_snapshots",
        "posterior_settlement_previous_target_unrealizable_consecutive_snapshots",
    )
    if trajectory_state == TRAJECTORY_UNSUPPORTED:
        return max(1, int(previous or 0) + 1)
    second_consecutive = _first_bool(
        candidate,
        context,
        "target_unrealizable_second_consecutive",
        "trajectory_unsupported_second_consecutive",
    )
    return 2 if second_consecutive else 0


def evaluate_realtime_target_adjustment(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    *,
    previous_top_bucket: Any = None,
    current_top_bucket: Any = None,
    held_target_bucket: Any = None,
    snapshot_id: Any = None,
    unrealizable_reliability_max: float = DEFAULT_UNREALIZABLE_RELIABILITY_MAX,
    switch_reliability_min: float = DEFAULT_SWITCH_RELIABILITY_MIN,
    switch_posterior_lead_min: float = DEFAULT_SWITCH_POSTERIOR_LEAD_MIN,
    unrealizable_consecutive_snapshots: int = DEFAULT_UNREALIZABLE_CONSECUTIVE_SNAPSHOTS,
) -> Dict[str, Any]:
    prev_top = normalize_bucket_label(
        previous_top_bucket
        or _first_str(candidate, context, "previous_top_bucket", "posterior_settlement_previous_top_bucket")
    )
    cur_top = normalize_bucket_label(
        current_top_bucket
        or _first_str(
            candidate,
            context,
            "current_top_bucket",
            "posterior_settlement_current_top_bucket",
            "target_bucket",
            "posterior_settlement_target_bucket",
        )
    )
    held_bucket = normalize_bucket_label(
        held_target_bucket
        or _first_str(
            candidate,
            context,
            "held_target_bucket",
            "posterior_settlement_held_target_bucket",
            "current_position_target_bucket",
            "current_target_bucket",
        )
    )
    has_active_position = _first_bool(
        candidate,
        context,
        "has_active_position",
        "posterior_settlement_has_active_position",
    )
    has_active_yes_position = _first_bool(
        candidate,
        context,
        "has_active_yes_position",
        "posterior_settlement_has_active_yes_position",
    )
    if has_active_position is None:
        has_active_position = bool(has_active_yes_position) or bool(held_bucket)
    if has_active_yes_position is None:
        has_active_yes_position = False
    target_bucket = normalize_bucket_label(
        held_bucket
        or _first_str(
            candidate,
            context,
            "target_bucket",
            "posterior_settlement_target_bucket",
        )
    )
    if not target_bucket:
        target_bucket = cur_top
    second_bucket = normalize_bucket_label(
        _first_str(
            candidate,
            context,
            "second_target_bucket",
            "posterior_settlement_second_target_bucket",
        )
    )
    current_top_prob = _posterior_for_bucket(
        candidate,
        context,
        cur_top,
        target_bucket=cur_top,
        current_top_bucket=cur_top,
    )
    target_prob = _posterior_for_bucket(
        candidate,
        context,
        target_bucket,
        target_bucket=target_bucket,
        current_top_bucket=cur_top,
    )
    switch_reference_bucket = held_bucket or (
        prev_top if prev_top and cur_top and not _same_bucket(prev_top, cur_top) else target_bucket
    )
    switch_reference_prob = (
        _posterior_for_bucket(
            candidate,
            context,
            switch_reference_bucket,
            target_bucket=switch_reference_bucket,
            current_top_bucket=cur_top,
        )
        if switch_reference_bucket
        else target_prob
    )
    if current_top_prob is None:
        current_top_prob = _first_prob(candidate, context, "current_top_posterior", "posterior_settlement_current_top_posterior")
    if target_prob is None:
        target_prob = _first_prob(
            candidate,
            context,
            "held_target_posterior",
            "current_target_posterior",
            "target_posterior",
            "posterior_settlement_target_posterior",
        )

    target_rel = _first_prob(
        candidate,
        context,
        "held_target_realization_reliability",
        "current_target_realization_reliability",
    )
    target_rel_source = "explicit_current_target_realization_reliability"
    if target_rel is None:
        target_rel, target_rel_source = _target_reliability(candidate, context, target_bucket, target_prob)
    new_rel = _first_prob(
        candidate,
        context,
        "new_target_realization_reliability",
        "current_top_target_realization_reliability",
    )
    new_rel_source = "explicit_new_target_realization_reliability"
    if new_rel is None:
        new_rel, new_rel_source = _target_reliability({}, context, cur_top, current_top_prob)

    trajectory_state = infer_trajectory_support_state(candidate, context, target_bucket=target_bucket)
    new_trajectory_state = _trajectory_state_from_text(
        _first_str(candidate, context, "new_target_trajectory_support_state", "current_top_trajectory_support_state")
    )
    if not new_trajectory_state:
        new_trajectory_state = infer_trajectory_support_state({}, context, target_bucket=cur_top)

    top_bucket_changed = bool(prev_top and cur_top and not _same_bucket(prev_top, cur_top))
    unsupported_count = _unsupported_snapshot_count(candidate, context, trajectory_state)
    unrealizable_triggered = bool(
        trajectory_state == TRAJECTORY_UNSUPPORTED
        and float(target_rel) < float(unrealizable_reliability_max)
        and int(unsupported_count) >= int(unrealizable_consecutive_snapshots)
    )
    regular_posterior_lead = None
    if current_top_prob is not None and switch_reference_prob is not None:
        regular_posterior_lead = float(current_top_prob) - float(switch_reference_prob)

    regular_switch_allowed = bool(
        top_bucket_changed
        and cur_top
        and switch_reference_bucket
        and not _same_bucket(cur_top, switch_reference_bucket)
        and float(new_rel) + 1e-12 >= float(switch_reliability_min)
        and regular_posterior_lead is not None
        and regular_posterior_lead + 1e-12 >= float(switch_posterior_lead_min)
        and new_trajectory_state == TRAJECTORY_SUPPORTED
    )
    explicit_d0_switch_candidate_bucket = normalize_bucket_label(
        _first_str(
            candidate,
            context,
            "d0_switch_candidate_bucket",
            "posterior_settlement_d0_switch_candidate_bucket",
            "switch_candidate_bucket",
            "posterior_settlement_switch_candidate_bucket",
            "candidate_switch_bucket",
        )
    )
    d0_switch_candidate_bucket = explicit_d0_switch_candidate_bucket
    if (
        not d0_switch_candidate_bucket
        and _is_lower_boundary_bucket(switch_reference_bucket)
        and _is_single_temperature_bucket(cur_top)
    ):
        held_upper = _bucket_upper(switch_reference_bucket)
        cur_anchor = bucket_anchor(cur_top)
        if held_upper is None or cur_anchor is None or float(cur_anchor) > float(held_upper):
            d0_switch_candidate_bucket = cur_top
    if not d0_switch_candidate_bucket:
        d0_switch_candidate_bucket = normalize_bucket_label(
            _first_str(
                candidate,
                context,
                "second_target_bucket",
                "posterior_settlement_second_target_bucket",
                "outcome",
                "bucket_label",
            )
        )
    if not d0_switch_candidate_bucket:
        d0_switch_candidate_bucket = cur_top
    d0_switch_candidate_prob = _posterior_for_bucket(
        candidate,
        context,
        d0_switch_candidate_bucket,
        target_bucket=d0_switch_candidate_bucket,
        current_top_bucket=cur_top,
    )
    d0_switch_candidate_trajectory_state = (
        infer_trajectory_support_state(candidate, context, target_bucket=d0_switch_candidate_bucket)
        if d0_switch_candidate_bucket
        else TRAJECTORY_MISSING
    )
    trajectory_pressure_allowed, trajectory_pressure_context = _d0_posterior_trajectory_switch_pressure(
        candidate,
        context,
        held_bucket=switch_reference_bucket,
        candidate_bucket=d0_switch_candidate_bucket,
        candidate_posterior=d0_switch_candidate_prob,
        held_posterior=switch_reference_prob,
        candidate_trajectory_state=d0_switch_candidate_trajectory_state,
    )
    switch_target_bucket = d0_switch_candidate_bucket if trajectory_pressure_allowed else cur_top
    switch_target_prob = d0_switch_candidate_prob if trajectory_pressure_allowed else current_top_prob
    switch_target_trajectory_state = d0_switch_candidate_trajectory_state if trajectory_pressure_allowed else new_trajectory_state
    switch_target_rel = float(new_rel)
    switch_target_rel_source = new_rel_source
    if trajectory_pressure_allowed:
        switch_target_rel = max(float(new_rel), float(switch_reliability_min) + 0.05)
        switch_target_rel_source = "d0_posterior_trajectory_switch_pressure"
    posterior_lead = None
    if switch_target_prob is not None and switch_reference_prob is not None:
        posterior_lead = float(switch_target_prob) - float(switch_reference_prob)
    switch_allowed = bool(regular_switch_allowed or trajectory_pressure_allowed)

    reasons: list[str] = []
    if top_bucket_changed:
        _append_reason(reasons, "top_bucket_changed")
    elif prev_top and cur_top:
        _append_reason(reasons, "top_bucket_unchanged")
    else:
        _append_reason(reasons, "previous_or_current_top_missing")
    if unrealizable_triggered:
        _append_reason(reasons, "target_unrealizable")
    elif trajectory_state == TRAJECTORY_UNSUPPORTED:
        _append_reason(reasons, "target_trajectory_unsupported_not_confirmed")
    elif trajectory_state == TRAJECTORY_SUPPORTED:
        _append_reason(reasons, "target_trajectory_supported")
    elif trajectory_state == TRAJECTORY_NEUTRAL:
        _append_reason(reasons, "target_trajectory_neutral")
    else:
        _append_reason(reasons, "target_trajectory_missing")
    if trajectory_pressure_allowed:
        _append_reason(reasons, "d0_posterior_trajectory_switch")
        pressure_trigger = str(trajectory_pressure_context.get("trigger") or "").strip()
        if pressure_trigger:
            _append_reason(reasons, f"d0_trajectory_switch_{pressure_trigger}")
    if switch_allowed:
        _append_reason(reasons, "new_target_switch_conditions_pass")
    elif top_bucket_changed and cur_top and switch_reference_bucket and not _same_bucket(cur_top, switch_reference_bucket):
        if float(new_rel) < float(switch_reliability_min):
            _append_reason(reasons, "new_target_reliability_below_min")
        if regular_posterior_lead is None:
            _append_reason(reasons, "new_target_posterior_lead_missing")
        elif regular_posterior_lead + 1e-12 < float(switch_posterior_lead_min):
            _append_reason(reasons, "new_target_posterior_lead_below_min")
        if new_trajectory_state != TRAJECTORY_SUPPORTED:
            _append_reason(reasons, "new_target_trajectory_not_supported")

    if trajectory_pressure_allowed and top_bucket_changed and unrealizable_triggered:
        trigger_type = "top_bucket_changed+target_unrealizable+d0_posterior_trajectory_switch"
    elif trajectory_pressure_allowed and top_bucket_changed:
        trigger_type = "top_bucket_changed+d0_posterior_trajectory_switch"
    elif trajectory_pressure_allowed and unrealizable_triggered:
        trigger_type = "target_unrealizable+d0_posterior_trajectory_switch"
    elif trajectory_pressure_allowed:
        trigger_type = "d0_posterior_trajectory_switch"
    elif top_bucket_changed and unrealizable_triggered:
        trigger_type = "top_bucket_changed+target_unrealizable"
    elif top_bucket_changed:
        trigger_type = "top_bucket_changed"
    elif unrealizable_triggered:
        trigger_type = "target_unrealizable"
    else:
        trigger_type = "none"

    if trigger_type == "none":
        action = ACTION_HOLD
    elif switch_allowed:
        action = ACTION_SWITCH_BUY
    elif unrealizable_triggered:
        action = ACTION_EXIT_CLEAR
    elif top_bucket_changed and cur_top and switch_reference_bucket and not _same_bucket(cur_top, switch_reference_bucket):
        action = ACTION_OBSERVE_REPRICE
    elif trajectory_state == TRAJECTORY_SUPPORTED and target_rel + 1e-12 >= float(unrealizable_reliability_max):
        action = ACTION_HOLD_RECONFIRMED
    else:
        action = ACTION_OBSERVE_REPRICE

    snap = snapshot_id or _first_str(
        candidate,
        context,
        "snapshot_id",
        "event_run_id",
        "run_id",
        "fact_as_of",
        "created_at",
    )
    dedupe_key = "|".join(
        part
        for part in (
            str(_first_str(candidate, context, "slug") or ""),
            str(_first_str(candidate, context, "event_date") or ""),
            str(snap or ""),
            str(action),
            str(cur_top or ""),
        )
        if part
    )

    action_target_bucket = switch_target_bucket if action == ACTION_SWITCH_BUY else target_bucket
    action_target_prob = switch_target_prob if action == ACTION_SWITCH_BUY else target_prob
    action_target_rel = switch_target_rel if action == ACTION_SWITCH_BUY else float(target_rel)
    action_target_rel_source = switch_target_rel_source if action == ACTION_SWITCH_BUY else target_rel_source
    out: Dict[str, Any] = {
        "previous_top_bucket": prev_top or None,
        "current_top_bucket": cur_top or None,
        "top_bucket_changed": bool(top_bucket_changed),
        "held_target_bucket": held_bucket or None,
        "current_position_target_bucket": held_bucket or None,
        "has_active_position": bool(has_active_position),
        "has_active_yes_position": bool(has_active_yes_position),
        "target_bucket": action_target_bucket or None,
        "target_posterior": float(action_target_prob) if action_target_prob is not None else None,
        "second_target_bucket": second_bucket or None,
        "target_realization_reliability": float(action_target_rel),
        "target_realization_reliability_source": action_target_rel_source,
        "held_target_realization_reliability": float(target_rel),
        "held_target_realization_reliability_source": target_rel_source,
        "trajectory_support_state": trajectory_state,
        "unrealizable_triggered": bool(unrealizable_triggered),
        "trigger_type": trigger_type,
        "action": action,
        "reason_codes": list(reasons),
        "target_adjustment_decision_version": "target_realization_posterior_trajectory_switch_v2",
        "target_adjustment_decision_budget_sec": 3.0,
        "target_adjustment_action": action,
        "target_adjustment_triggered": bool(trigger_type != "none"),
        "target_adjustment_reason_codes": list(reasons),
        "target_unrealizable_consecutive_snapshots": int(unsupported_count),
        "target_unrealizable_reliability_max": float(unrealizable_reliability_max),
        "switch_buy_reliability_min": float(switch_reliability_min),
        "switch_buy_posterior_lead_min": float(switch_posterior_lead_min),
        "new_target_bucket": switch_target_bucket or None,
        "new_target_posterior": float(switch_target_prob) if switch_target_prob is not None else None,
        "new_target_realization_reliability": float(switch_target_rel),
        "new_target_realization_reliability_source": switch_target_rel_source,
        "new_target_trajectory_support_state": switch_target_trajectory_state,
        "held_target_posterior": float(target_prob) if target_prob is not None else None,
        "current_target_posterior": float(target_prob) if target_prob is not None else None,
        "switch_reference_bucket": switch_reference_bucket or None,
        "switch_reference_posterior": float(switch_reference_prob) if switch_reference_prob is not None else None,
        "new_target_posterior_lead": float(posterior_lead) if posterior_lead is not None else None,
        "switch_buy_allowed": bool(switch_allowed),
        "new_buy_allowed_this_snapshot": 1 if action == ACTION_SWITCH_BUY else 0,
        "target_adjustment_dedupe_key": dedupe_key or None,
        "d0_posterior_trajectory_switch_allowed": bool(trajectory_pressure_allowed),
        "d0_posterior_trajectory_switch_context": dict(trajectory_pressure_context) if trajectory_pressure_context else None,
    }
    for key, value in list(out.items()):
        if key.startswith("posterior_settlement_"):
            continue
        out[f"posterior_settlement_{key}"] = value
    return out


def classify_posterior_settlement_candidate(
    candidate: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    *,
    yes_reliability_min: float = DEFAULT_YES_RELIABILITY_MIN,
) -> Dict[str, Any]:
    side = str(candidate.get("side") or "").strip().upper()
    candidate_bucket = _candidate_bucket(candidate, context)
    bucket_prob = _bucket_probability(candidate, context)
    target_bucket = normalize_bucket_label(
        _first_str(
            candidate,
            context,
            "target_bucket",
            "posterior_settlement_target_bucket",
            "dynamic_target_bucket",
            "q_bayes_target_bucket",
        )
    )
    if not target_bucket and side == "YES":
        target_bucket = candidate_bucket
    target_prob = _first_prob(
        candidate,
        context,
        "target_posterior",
        "posterior_settlement_target_posterior",
        "target_bucket_probability",
        "target_bucket_probability_pct",
    )
    if target_prob is None and _same_bucket(candidate_bucket, target_bucket):
        target_prob = bucket_prob

    second_bucket = normalize_bucket_label(
        _first_str(
            candidate,
            context,
            "second_target_bucket",
            "posterior_settlement_second_target_bucket",
            "breakout_bucket",
        )
    )
    second_prob = _first_prob(
        candidate,
        context,
        "second_target_posterior",
        "posterior_settlement_second_target_posterior",
    )
    plan_action = _first_str(
        candidate,
        context,
        "target_realization_plan_action",
        "target_realization_action",
    ).lower()
    leg_role = _first_str(
        candidate,
        context,
        "target_realization_leg_role",
        "target_realization_plan_role",
        "target_realization_role",
    ).lower()
    old_bucket_reverse_leg = bool(leg_role == "old_bucket_reverse")
    ssot_second_target_yes = bool(
        side == "YES"
        and plan_action == "add"
        and leg_role == "second_target_yes"
        and second_bucket
        and _same_bucket(candidate_bucket, second_bucket)
    )
    reliability, reliability_source = _target_reliability(
        candidate,
        context,
        candidate_bucket if old_bucket_reverse_leg else second_bucket if ssot_second_target_yes else target_bucket,
        bucket_prob if old_bucket_reverse_leg else second_prob if ssot_second_target_yes else target_prob,
    )
    directional_dist = bucket_directional_distance(candidate_bucket, target_bucket)
    dist = abs(int(directional_dist)) if directional_dist is not None else bucket_distance(candidate_bucket, target_bucket)
    if dist is None:
        dist = int(_first_float(candidate, context, "posterior_settlement_bucket_distance") or 0)

    lane = LANE_OBSERVE
    allowed = False
    block_reason = "not_a_posterior_settlement_entry"
    switch_reason = ""
    if old_bucket_reverse_leg and side in {"YES", "NO"}:
        block_reason = "retired_old_bucket_reverse_buy_use_sell_old_transition"
    elif side == "YES":
        if not target_bucket:
            block_reason = "target_bucket_missing"
        elif ssot_second_target_yes:
            if second_prob is None:
                block_reason = "second_target_posterior_missing"
            elif reliability + 1e-9 < float(yes_reliability_min):
                block_reason = "second_target_realization_reliability_below_min"
            else:
                lane = LANE_YES_TARGET
                allowed = True
                block_reason = ""
        elif not _same_bucket(candidate_bucket, target_bucket):
            block_reason = "yes_candidate_not_target_bucket"
        elif target_prob is None:
            block_reason = "target_posterior_missing"
        elif reliability + 1e-9 < float(yes_reliability_min):
            block_reason = "target_realization_reliability_below_min"
        else:
            lane = LANE_YES_TARGET
            allowed = True
            block_reason = ""
    elif side == "NO":
        block_reason = "retired_posterior_settlement_no_use_dynamic_three_leg"
    else:
        block_reason = "side_not_tradeable_for_posterior_settlement"

    explicit_switch_reason = _first_str(
        candidate,
        context,
        "bucket_switch_reason",
        "posterior_settlement_bucket_switch_reason",
    )
    if explicit_switch_reason:
        switch_reason = explicit_switch_reason
    previous_target = normalize_bucket_label(
        _first_str(candidate, context, "previous_target_bucket", "posterior_settlement_previous_target_bucket")
    )
    if not switch_reason and previous_target and target_bucket and not _same_bucket(previous_target, target_bucket):
        switch_reason = "target_bucket_changed_by_facts"

    out: Dict[str, Any] = {
        "strategy_family": STRATEGY_FAMILY,
        "entry_strategy_family": STRATEGY_FAMILY,
        "strategy_lane": lane,
        "entry_alpha_gate_source": STRATEGY_FAMILY,
        "take_profit_enabled": False,
        "stop_loss_enabled": False,
        "settlement_exit_only": True,
        "target_bucket": target_bucket or None,
        "target_posterior": float(target_prob) if target_prob is not None else None,
        "target_realization_leg_role": leg_role or None,
        "target_realization_reliability": float(reliability),
        "target_realization_reliability_source": reliability_source,
        "second_target_bucket": second_bucket or None,
        "second_target_posterior": float(second_prob) if second_prob is not None else None,
        "posterior_settlement_candidate_bucket": candidate_bucket or None,
        "posterior_settlement_candidate_bucket_prob": float(bucket_prob) if bucket_prob is not None else None,
        "posterior_settlement_bucket_distance": int(dist),
        "posterior_settlement_yes_reliability_min": float(yes_reliability_min),
        "posterior_settlement_entry_allowed": bool(allowed),
        "posterior_settlement_entry_block_reason": block_reason,
        "bucket_switch_reason": switch_reason,
    }
    if lane == LANE_YES_TARGET:
        score_prob = bucket_prob if old_bucket_reverse_leg else target_prob
        out["posterior_settlement_sort_score"] = float(reliability) * 100.0 + float(score_prob or 0.0) * 10.0
        out["posterior_settlement_size_scale"] = min(1.0, max(0.20, float(reliability)))
    else:
        out["posterior_settlement_sort_score"] = 0.0
        out["posterior_settlement_size_scale"] = 0.0
    return out


def event_target_context(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    buckets: list[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = normalize_bucket_label(row.get("outcome") or row.get("q_temperature_bucket_label"))
        if not label:
            continue
        prob = _first_prob(
            row,
            None,
            "q_temperature_bucket_posterior",
            "q_bayes_posterior",
            "target_bucket_probability",
            "target_posterior",
        )
        if prob is None:
            continue
        raw_prob = float(prob)
        hard_zero = _target_context_hard_zero(row)
        hard_zero_reasons = _target_context_hard_zero_reasons(row) if hard_zero else []
        if hard_zero:
            prob = 0.0
        observed_feasibility_factor = None
        if _observed_feasibility_likelihood_allowed(row):
            observed_feasibility_factor = _first_prob(
                row,
                None,
                "q_bayes_physical_likelihood_observed_prob",
                "q_bayes_independent_physics_residual_observed_prob",
            )
        anchor = bucket_anchor(label)
        is_single = _is_single_temperature_bucket(label)
        buckets.append(
            {
                "prob": float(prob),
                "prob_pre_observed_feasibility": float(prob),
                "anchor": float(anchor) if anchor is not None else 1e9,
                "label": label,
                "is_single": is_single,
                "raw_prob": raw_prob,
                "hard_zero": hard_zero,
                "hard_zero_reasons": hard_zero_reasons,
                "observed_feasibility_factor": observed_feasibility_factor,
                "observed_feasibility_applied": False,
            }
        )
    if not buckets:
        return {}

    hard_zero_present = any(bool(bucket.get("hard_zero")) for bucket in buckets)
    observed_factor_present = any(bucket.get("observed_feasibility_factor") is not None for bucket in buckets)
    if hard_zero_present or observed_factor_present:
        adjusted_probs = []
        changed = bool(hard_zero_present)
        for bucket in buckets:
            prob_pre = float(bucket.get("prob") or 0.0)
            factor = bucket.get("observed_feasibility_factor")
            adjusted = prob_pre
            if factor is not None:
                adjusted = prob_pre * float(factor)
                if abs(adjusted - prob_pre) > 1e-12:
                    changed = True
            adjusted_probs.append(max(0.0, float(adjusted)))
        adjusted_sum = sum(adjusted_probs)
        if changed and adjusted_sum > 0.0:
            for bucket, adjusted in zip(buckets, adjusted_probs):
                bucket["prob_pre_constraints"] = float(bucket.get("raw_prob") or 0.0)
                bucket["prob"] = max(0.0, float(adjusted) / float(adjusted_sum))
                bucket["posterior_constraints_applied"] = True
                bucket["observed_feasibility_applied"] = bool(bucket.get("observed_feasibility_factor") is not None)

    buckets.sort(key=lambda item: (-float(item["prob"]), float(item["anchor"]), str(item["label"])))
    target_bucket = buckets[0]
    target_prob = float(target_bucket["prob"])
    target = str(target_bucket["label"])
    second = buckets[1] if len(buckets) > 1 else None
    by_label = {}
    for idx, bucket in enumerate(buckets):
        prob = float(bucket["prob"])
        label = str(bucket["label"])
        meta = {
            "posterior_settlement_candidate_bucket_prob": prob,
            "posterior_settlement_bucket_rank": idx + 1,
        }
        if bool(bucket.get("hard_zero")):
            meta["posterior_settlement_candidate_bucket_prob_raw"] = float(bucket["raw_prob"])
            meta["posterior_settlement_candidate_bucket_hard_zero"] = True
            meta["posterior_settlement_candidate_bucket_hard_zero_reasons"] = list(
                bucket.get("hard_zero_reasons") or []
            )
        if bool(bucket.get("observed_feasibility_applied")):
            meta["posterior_settlement_candidate_bucket_prob_pre_observed_feasibility"] = float(
                bucket.get("prob_pre_observed_feasibility") or 0.0
            )
            factor = bucket.get("observed_feasibility_factor")
            if factor is not None:
                meta["posterior_settlement_candidate_observed_feasibility_factor"] = float(factor)
        if bool(bucket.get("posterior_constraints_applied")):
            meta["posterior_settlement_candidate_bucket_constraints_applied"] = True
            meta["posterior_settlement_candidate_bucket_prob_pre_constraints"] = float(
                bucket.get("prob_pre_constraints") or 0.0
            )
            meta["posterior_settlement_candidate_bucket_constraints_source"] = (
                "physical_hard_zero_and_observed_feasibility"
            )
        by_label[label.lower()] = meta
    out: Dict[str, Any] = {
        "posterior_settlement_target_bucket": target,
        "target_bucket": target,
        "posterior_settlement_current_top_bucket": target,
        "current_top_bucket": target,
        "posterior_settlement_target_posterior": target_prob,
        "target_posterior": target_prob,
        "posterior_settlement_current_top_posterior": target_prob,
        "current_top_posterior": target_prob,
        "posterior_settlement_bucket_prob_by_label": by_label,
    }
    if second is not None:
        second_prob = float(second["prob"])
        second_label = str(second["label"])
        out["posterior_settlement_second_target_bucket"] = second_label
        out["second_target_bucket"] = second_label
        out["posterior_settlement_second_target_posterior"] = second_prob
        out["second_target_posterior"] = second_prob
    if _is_lower_boundary_bucket(target):
        single_candidate = next((item for item in buckets[1:] if bool(item.get("is_single"))), None)
        if single_candidate is not None:
            cand_prob = float(single_candidate["prob"])
            cand_label = str(single_candidate["label"])
            out["posterior_settlement_d0_switch_candidate_bucket"] = cand_label
            out["d0_switch_candidate_bucket"] = cand_label
            out["posterior_settlement_d0_switch_candidate_posterior"] = cand_prob
            out["d0_switch_candidate_posterior"] = cand_prob
    return out


def _constraint_base_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    base = dict(row)
    for key in (
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
        "q_contract_payout_posterior",
    ):
        pre_key = f"{key}_pre_observed_constraints"
        pre_val = _prob(row.get(pre_key))
        if pre_val is not None:
            base[key] = pre_val
    no_pre = _prob(row.get("q_bayes_posterior_no_pre_observed_constraints"))
    if no_pre is not None:
        base["q_bayes_posterior_no"] = no_pre
    return base


def apply_posterior_observed_constraints_to_distribution(
    rows: Sequence[Mapping[str, Any]],
) -> bool:
    """
    Mutate distribution rows so final posterior fields obey observed D0 constraints.

    Raw/model/QM fields are intentionally left untouched. The original posterior
    values are preserved in *_pre_observed_constraints fields, making this helper
    idempotent for UI refreshes and fact re-reads.
    """
    if not isinstance(rows, list) or not rows:
        return False
    if _has_hard_source_posterior_override(rows):
        return False
    context_rows: List[Dict[str, Any]] = [
        _constraint_base_row(row) for row in rows if isinstance(row, Mapping)
    ]
    observed_feasibility_skipped = any(
        _observed_feasibility_likelihood_skipped(row) for row in context_rows
    )
    context = event_target_context(context_rows)
    by_label = context.get("posterior_settlement_bucket_prob_by_label")
    if not isinstance(by_label, Mapping) or not by_label:
        return False

    constraints_present = False
    for meta in by_label.values():
        if not isinstance(meta, Mapping):
            continue
        if (
            _to_bool(meta.get("posterior_settlement_candidate_bucket_hard_zero")) is True
            or _to_bool(meta.get("posterior_settlement_candidate_bucket_constraints_applied")) is True
            or meta.get("posterior_settlement_candidate_observed_feasibility_factor") is not None
        ):
            constraints_present = True
            break
    if not constraints_present:
        restored = _restore_pre_observed_constraints(rows)
        cleared = False
        if observed_feasibility_skipped:
            for row in rows:
                if isinstance(row, dict):
                    _clear_observed_feasibility_constraint_meta(row)
                    cleared = _mark_observed_constraints_not_applied(
                        row,
                        "observed_constraints_not_applicable",
                    ) or cleared
        else:
            for row in rows:
                if isinstance(row, dict) and row.get("q_posterior_observed_constraints_applied") is True:
                    cleared = _mark_observed_constraints_not_applied(
                        row,
                        "observed_constraints_not_applicable",
                    ) or cleared
        return bool(restored or cleared)

    touched = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = normalize_bucket_label(row.get("outcome") or row.get("q_temperature_bucket_label"))
        if not label:
            continue
        meta = by_label.get(label.lower())
        if not isinstance(meta, Mapping):
            continue
        adjusted = _prob(meta.get("posterior_settlement_candidate_bucket_prob"))
        if adjusted is None:
            continue
        hard_zero = _to_bool(meta.get("posterior_settlement_candidate_bucket_hard_zero")) is True
        reasons_raw = meta.get("posterior_settlement_candidate_bucket_hard_zero_reasons")
        if isinstance(reasons_raw, (list, tuple)):
            reasons = [str(reason).strip() for reason in reasons_raw if str(reason).strip()]
        elif reasons_raw is not None and str(reasons_raw).strip():
            reasons = [str(reasons_raw).strip()]
        else:
            reasons = []

        base = _constraint_base_row(row)
        base_prob = _first_prob(
            base,
            None,
            "q_temperature_bucket_posterior",
            "q_bayes_posterior",
            "q_contract_payout_posterior",
            "q_round",
            "q_raw",
        )
        if base_prob is None:
            base_prob = adjusted

        for key in (
            "q_temperature_bucket_posterior",
            "q_bayes_posterior",
            "q_contract_payout_posterior",
        ):
            old = _prob(base.get(key))
            if old is not None and f"{key}_pre_observed_constraints" not in row:
                row[f"{key}_pre_observed_constraints"] = float(old)
            current = _prob(row.get(key))
            if current is None or abs(float(current) - float(adjusted)) > 1e-12:
                row[key] = float(adjusted)
                touched = True

        no_adjusted = max(0.0, min(1.0, 1.0 - float(adjusted)))
        no_old = _prob(base.get("q_bayes_posterior_no"))
        if no_old is not None and "q_bayes_posterior_no_pre_observed_constraints" not in row:
            row["q_bayes_posterior_no_pre_observed_constraints"] = float(no_old)
        if "q_bayes_posterior_no" in row:
            current_no = _prob(row.get("q_bayes_posterior_no"))
            if current_no is None or abs(float(current_no) - no_adjusted) > 1e-12:
                row["q_bayes_posterior_no"] = float(no_adjusted)
                touched = True

        row["q_posterior_observed_constraints_applied"] = True
        row["q_posterior_observed_constraints_source"] = "posterior_settlement_event_target_context"
        row["q_posterior_observed_constraints_pre_prob"] = float(base_prob)
        row["q_posterior_observed_constraints_post_prob"] = float(adjusted)
        row["q_posterior_observed_constraints_hard_zero"] = bool(hard_zero)
        if reasons:
            row["q_posterior_observed_constraints_reason_codes"] = reasons
        factor = _prob(meta.get("posterior_settlement_candidate_observed_feasibility_factor"))
        if factor is not None:
            row["q_posterior_observed_constraints_observed_feasibility_factor"] = float(factor)
            row["q_posterior_observed_feasibility_applied"] = True
            row.pop("q_posterior_observed_feasibility_skipped_reason", None)
        elif observed_feasibility_skipped:
            _clear_observed_feasibility_constraint_meta(row)
        if meta.get("posterior_settlement_bucket_rank") is not None:
            row["posterior_settlement_bucket_rank"] = meta.get("posterior_settlement_bucket_rank")
        if hard_zero:
            row["posterior_settlement_candidate_bucket_hard_zero"] = True
            row["posterior_settlement_candidate_bucket_hard_zero_reasons"] = reasons
            raw_prob = _prob(meta.get("posterior_settlement_candidate_bucket_prob_raw"))
            if raw_prob is not None:
                row["posterior_settlement_candidate_bucket_prob_raw"] = float(raw_prob)
    return bool(touched)
