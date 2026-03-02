from __future__ import annotations

from typing import Any, Dict, Optional
import math


def _to_float(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(val)))


def compute_signal_advantage(
    *,
    q_raw: Any = None,
    q_calibrated: Any = None,
    q_edge: Any = None,
    trend_pct: Any = None,
    trend_consistency_up: Any = None,
    model_spread: Any = None,
    freshness_sec: Any = None,
    signal_z: float = 1.64,
) -> Dict[str, Any]:
    q_raw_f = _to_float(q_raw)
    q_cal_f = _to_float(q_calibrated)
    q_edge_f = _to_float(q_edge)
    trend_f = _to_float(trend_pct)
    trend_cons_f = _to_float(trend_consistency_up)
    model_spread_f = _to_float(model_spread)
    freshness_f = _to_float(freshness_sec)

    if q_edge_f is None:
        q_ref = q_cal_f if q_cal_f is not None else q_raw_f
        if q_ref is not None:
            q_edge_f = q_ref - 0.5
    if q_edge_f is None:
        return {}
    if trend_f is None:
        trend_f = 0.0
    if trend_cons_f is None:
        trend_cons_f = 0.5
    trend_cons_f = _clamp(trend_cons_f, 0.0, 1.0)

    model_penalty = 0.0
    if model_spread_f is not None:
        model_penalty = max(0.0, model_spread_f - 0.08) * 0.60
    freshness_penalty = 0.0
    if freshness_f is not None and freshness_f > 0:
        freshness_penalty = min(0.10, freshness_f / 14400.0 * 0.05)

    signal_alpha = (
        float(q_edge_f)
        + float(trend_f) * 0.35
        + (float(trend_cons_f) - 0.5) * 0.05
        - model_penalty
        - freshness_penalty
    )
    signal_sigma = 0.03 + model_penalty * 0.5 + freshness_penalty
    if model_spread_f is not None:
        signal_sigma += max(0.0, model_spread_f) * 0.25
    signal_sigma = _clamp(signal_sigma, 0.01, 0.35)
    signal_lcb = float(signal_alpha) - float(signal_z) * float(signal_sigma)
    signal_confidence = _clamp(1.0 - signal_sigma / 0.35, 0.0, 1.0)

    return {
        "signal_alpha": float(signal_alpha),
        "signal_lcb": float(signal_lcb),
        "signal_sigma": float(signal_sigma),
        "signal_confidence": float(signal_confidence),
        "signal_components_q_edge": float(q_edge_f),
        "signal_components_trend": float(trend_f),
        "signal_components_trend_consistency": float(trend_cons_f),
        "signal_components_model_spread": model_spread_f,
        "signal_components_freshness_sec": freshness_f,
    }

