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

    # 非对称趋势权重：正趋势0.40，负趋势0.25
    trend_weight = 0.40 if trend_f > 0 else 0.25

    # 一致性方向调整：低分歧时，高概率一致给轻微bonus（只正向）
    consensus_adjustment = 0.0
    if model_spread_f is not None and model_spread_f < 0.10:
        # 只在q_edge > 0.5时加分，避免过度惩罚低概率一致
        consensus_adjustment = max(q_edge_f - 0.5, 0) * 0.04

    signal_alpha = (
        float(q_edge_f)
        + float(trend_f) * trend_weight
        + (float(trend_cons_f) - 0.5) * 0.10  # 从0.05提高到0.10
        + consensus_adjustment  # 一致性方向bonus
        - model_penalty
        - freshness_penalty
    )
    signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty
    if model_spread_f is not None:
        signal_sigma += max(0.0, model_spread_f) * 0.18  # 降低系数，放出更多"分歧但可兑现"的mispricing
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
        "signal_components_consensus_adjustment": float(consensus_adjustment),
    }


def enrich_signal_observability(
    details: Any,
    *,
    signal_z: float = 1.64,
) -> Dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    out = dict(details)
    spread_val = _to_float(out.get("q_round_model_spread"))
    if spread_val is None:
        spread_val = _to_float(out.get("model_spread"))
    if spread_val is not None:
        out["q_round_model_spread"] = float(spread_val)
    need_backfill = False
    for key in (
        "edge_signal_alpha",
        "edge_signal_sigma",
        "edge_signal_lcb",
        "edge_signal_confidence",
        "edge_signal_components_q_edge",
        "edge_signal_components_trend",
        "edge_signal_components_trend_consistency",
        "edge_signal_components_model_spread",
        "edge_signal_components_freshness_sec",
        "edge_signal_components_consensus_adjustment",
    ):
        if _to_float(out.get(key)) is None:
            need_backfill = True
            break
    if not need_backfill:
        return out
    signal_meta = compute_signal_advantage(
        q_raw=out.get("q_raw", out.get("q_round")),
        q_calibrated=out.get("q_calibrated"),
        q_edge=out.get("q_edge"),
        trend_pct=out.get("trend_pct"),
        trend_consistency_up=out.get("trend_consistency_up"),
        model_spread=spread_val,
        signal_z=float(signal_z),
    )
    if not isinstance(signal_meta, dict) or not signal_meta:
        return out
    for detail_key, meta_key in (
        ("edge_signal_alpha", "signal_alpha"),
        ("edge_signal_sigma", "signal_sigma"),
        ("edge_signal_lcb", "signal_lcb"),
        ("edge_signal_confidence", "signal_confidence"),
        ("edge_signal_components_q_edge", "signal_components_q_edge"),
        ("edge_signal_components_trend", "signal_components_trend"),
        ("edge_signal_components_trend_consistency", "signal_components_trend_consistency"),
        ("edge_signal_components_model_spread", "signal_components_model_spread"),
        ("edge_signal_components_freshness_sec", "signal_components_freshness_sec"),
        ("edge_signal_components_consensus_adjustment", "signal_components_consensus_adjustment"),
    ):
        if _to_float(out.get(detail_key)) is not None:
            continue
        val = _to_float(signal_meta.get(meta_key))
        if val is not None:
            out[detail_key] = float(val)
    return out
