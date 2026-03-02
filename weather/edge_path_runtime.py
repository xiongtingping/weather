from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import json
import math
import os
from pathlib import Path


_CACHE: Dict[str, Dict[str, Any]] = {}


def _to_float(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _clamp01(val: float) -> float:
    return max(0.0, min(1.0, float(val)))


def _safe_key(x: Optional[float], bins: Tuple[float, ...]) -> str:
    if x is None or not math.isfinite(x):
        return "u"
    for idx, upper in enumerate(bins):
        if x <= upper:
            return str(idx)
    return str(len(bins) - 1)


def _build_bucket_key(
    *,
    signal_alpha: Optional[float],
    spread: Optional[float],
    depth_ratio: Optional[float],
    hours_to_settle: Optional[float],
) -> str:
    a = _safe_key(signal_alpha, (-0.10, -0.03, 0.0, 0.02, 0.05, 0.10, 1.0))
    s = _safe_key(spread, (0.01, 0.02, 0.03, 0.05, 0.08, 0.15, 1.0))
    d = _safe_key(depth_ratio, (0.5, 1.0, 2.0, 4.0, 8.0, 20.0, 999.0))
    h = _safe_key(hours_to_settle, (4.0, 8.0, 16.0, 24.0, 48.0, 96.0, 9999.0))
    return f"a{a}|s{s}|d{d}|h{h}"


def _load_calibration(path: str) -> Dict[str, Any]:
    text = str(path or "").strip()
    if not text:
        return {}
    try:
        mtime = os.path.getmtime(text)
    except Exception:
        return {}
    cached = _CACHE.get(text)
    if isinstance(cached, dict) and cached.get("mtime") == mtime and isinstance(cached.get("data"), dict):
        return dict(cached.get("data") or {})
    try:
        payload = json.loads(Path(text).read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    data = payload if isinstance(payload, dict) else {}
    _CACHE[text] = {"mtime": mtime, "data": data}
    return dict(data or {})


def _wilson_bounds(p: float, n: int, z: float) -> Tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = _clamp01(p)
    z2 = float(z) * float(z)
    denom = 1.0 + z2 / float(n)
    center = p + z2 / (2.0 * float(n))
    pm = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * float(n))) / float(n))
    lo = (center - pm) / denom
    hi = (center + pm) / denom
    return _clamp01(lo), _clamp01(hi)


def estimate_path_probability(
    *,
    signal_alpha: Any,
    spread: Any,
    depth_ratio: Any,
    hours_to_settle: Any,
    calibration_bucket_key: Optional[str] = None,
    calibration_path: Optional[str] = None,
    signal_z: float = 1.64,
) -> Dict[str, Any]:
    signal_alpha_f = _to_float(signal_alpha)
    spread_f = _to_float(spread)
    depth_ratio_f = _to_float(depth_ratio)
    hours_to_settle_f = _to_float(hours_to_settle)
    bucket_key = (
        str(calibration_bucket_key or "").strip()
        or _build_bucket_key(
            signal_alpha=signal_alpha_f,
            spread=spread_f,
            depth_ratio=depth_ratio_f,
            hours_to_settle=hours_to_settle_f,
        )
    )
    cal_path = str(calibration_path or "").strip()
    if not cal_path:
        cal_path = str(
            os.environ.get("PM_EDGE_PATH_CALIBRATION_PATH")
            or os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH_ORDERS")
            or os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH")
            or ""
        ).strip()
    cal = _load_calibration(cal_path) if cal_path else {}

    p = None
    n = None
    source = "heuristic"
    generated_at = str(cal.get("generated_at") or "").strip() or None

    table = cal.get("p_converge_buckets")
    if isinstance(table, dict) and bucket_key:
        entry = table.get(bucket_key)
        if isinstance(entry, dict):
            p = _to_float(entry.get("p"))
            total = entry.get("total")
            try:
                n = int(total) if total is not None else None
            except Exception:
                n = None
            if p is not None:
                source = "calibration_p_converge"
        elif entry is not None:
            p = _to_float(entry)
            if p is not None:
                source = "calibration_p_converge"

    if p is None:
        alpha = signal_alpha_f if signal_alpha_f is not None else 0.0
        spr = spread_f if spread_f is not None else 0.05
        dep = depth_ratio_f if depth_ratio_f is not None else 1.0
        hts = hours_to_settle_f if hours_to_settle_f is not None else 24.0
        score = alpha * 8.0 - spr * 4.0 + math.log1p(max(dep, 0.0)) * 0.25 - hts * 0.01
        p = 1.0 / (1.0 + math.exp(-score))

    p = _clamp01(float(p))
    if n is not None and n > 0:
        lo, hi = _wilson_bounds(p, int(n), float(signal_z))
    else:
        sigma = 0.18
        lo = _clamp01(p - float(signal_z) * sigma)
        hi = _clamp01(p + float(signal_z) * sigma)

    return {
        "p_reach": float(p),
        "p_reach_lcb": float(lo),
        "p_reach_ucb": float(hi),
        "path_bucket_key": bucket_key,
        "path_sample_n": int(n) if n is not None and n >= 0 else None,
        "path_source": source,
        "path_calibration_path": cal_path or None,
        "path_calibration_generated_at": generated_at,
    }

