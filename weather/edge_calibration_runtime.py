from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_CACHE: Dict[str, Dict[str, Any]] = {}


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _safe_logit(prob: float, eps: float = 1e-6) -> float:
    p = _clamp01(prob)
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _safe_sigmoid(z: float) -> float:
    if z >= 0:
        exp_neg = math.exp(-z)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(z)
    return exp_pos / (1.0 + exp_pos)


def _parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return bool(default)
    return text in {"1", "true", "yes", "on"}


def _default_bundle_path() -> str:
    raw = str(os.environ.get("PM_Q_CALIBRATION_BUNDLE_PATH") or "").strip()
    if raw:
        return raw
    return str(Path(__file__).resolve().parent / "data" / "q_calibration_bundle.json")


def _safe_bin_key(value: Optional[float], bins: Tuple[float, ...], prefix: str, unknown: str) -> str:
    if value is None or not math.isfinite(value):
        return f"{prefix}:{unknown}"
    for idx, upper in enumerate(bins):
        if value <= upper:
            return f"{prefix}:{idx}"
    return f"{prefix}:{len(bins) - 1}"


def _session_bucket(ts_utc: Optional[datetime]) -> str:
    dt = ts_utc if isinstance(ts_utc, datetime) else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hour = int(dt.astimezone(timezone.utc).hour)
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 14:
        return "eu"
    if 14 <= hour < 24:
        return "us"
    return "u"


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bundle_mtime(path: str) -> Optional[float]:
    try:
        return float(os.path.getmtime(path))
    except Exception:
        return None


def load_q_calibration_bundle(bundle_path: Optional[str] = None) -> Dict[str, Any]:
    path = str(bundle_path or _default_bundle_path()).strip()
    if not path:
        return {}
    mtime = _bundle_mtime(path)
    if mtime is None:
        return {}
    cached = _CACHE.get(path)
    if isinstance(cached, dict) and cached.get("mtime") == mtime and isinstance(cached.get("bundle"), dict):
        return dict(cached.get("bundle") or {})
    payload: Dict[str, Any] = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            payload = raw
    except Exception:
        payload = {}
    if payload:
        payload.setdefault("bundle_path", path)
        payload.setdefault("loaded_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _CACHE[path] = {"mtime": mtime, "bundle": dict(payload)}
    return dict(payload)


@dataclass
class SegmentContext:
    regime: str
    days_to_event: Optional[float]
    spread: Optional[float]
    depth_ratio: Optional[float]
    tick_age_sec: Optional[float]
    session: str


def build_segment_key(
    *,
    regime: Any = None,
    days_to_event: Any = None,
    spread: Any = None,
    depth_ratio: Any = None,
    tick_age_sec: Any = None,
    session: Any = None,
    decision_ts: Any = None,
) -> Tuple[str, SegmentContext]:
    regime_text = str(regime or "").strip().lower() or "default"
    dte = _to_float(days_to_event)
    spread_f = _to_float(spread)
    depth_f = _to_float(depth_ratio)
    tick_age_f = _to_float(tick_age_sec)
    ts = _parse_ts(decision_ts)
    session_text = str(session or "").strip().lower()
    if not session_text:
        session_text = _session_bucket(ts)
    d_key = _safe_bin_key(dte, (0.0, 1.0, 2.0, 4.0, 7.0, 14.0, 9999.0), "d", "u")
    s_key = _safe_bin_key(spread_f, (0.01, 0.02, 0.03, 0.05, 0.08, 0.15, 1.0), "s", "u")
    r_key = _safe_bin_key(depth_f, (0.5, 1.0, 2.0, 4.0, 8.0, 20.0, 9999.0), "r", "u")
    t_key = _safe_bin_key(tick_age_f, (15.0, 30.0, 60.0, 120.0, 300.0, 900.0, 999999.0), "t", "u")
    key = f"reg:{regime_text}|{d_key}|{s_key}|{r_key}|{t_key}|sess:{session_text}"
    return key, SegmentContext(
        regime=regime_text,
        days_to_event=dte,
        spread=spread_f,
        depth_ratio=depth_f,
        tick_age_sec=tick_age_f,
        session=session_text,
    )


def _lookup_segment(bundle: Dict[str, Any], segment_key: str) -> Tuple[Optional[Dict[str, Any]], str, int]:
    segments = bundle.get("segments")
    if not isinstance(segments, dict) or not segment_key:
        return None, "global_only", 0
    if isinstance(segments.get(segment_key), dict):
        return dict(segments.get(segment_key) or {}), segment_key, 0

    parts = [p for p in segment_key.split("|") if p]
    if not parts:
        return None, "global_only", 0

    candidates = []
    for i in range(len(parts)):
        if parts[i].startswith(("s:", "r:", "t:")):
            tmp = list(parts)
            tmp[i] = tmp[i].split(":", 1)[0] + ":u"
            candidates.append("|".join(tmp))
    for i in range(len(parts)):
        if parts[i].startswith("sess:"):
            tmp = list(parts)
            tmp[i] = "sess:u"
            candidates.append("|".join(tmp))
    for rank, cand in enumerate(candidates, start=1):
        if isinstance(segments.get(cand), dict):
            return dict(segments.get(cand) or {}), cand, rank
    return None, "global_only", 0


def _resolve_platt(bundle: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], str]:
    glb = bundle.get("global_platt")
    if isinstance(glb, dict):
        a = _to_float(glb.get("a"))
        b = _to_float(glb.get("b"))
        if a is not None and b is not None:
            return a, b, "bundle_global_platt"
    platt = bundle.get("platt")
    if isinstance(platt, dict):
        a = _to_float(platt.get("a"))
        b = _to_float(platt.get("b"))
        if a is not None and b is not None:
            return a, b, "bundle_platt"
    return None, None, "q_raw_passthrough"


def predict_q_calibrated(
    *,
    q_raw: Any,
    bundle: Optional[Dict[str, Any]] = None,
    bundle_path: Optional[str] = None,
    regime: Any = None,
    days_to_event: Any = None,
    spread: Any = None,
    depth_ratio: Any = None,
    tick_age_sec: Any = None,
    session: Any = None,
    decision_ts: Any = None,
    z_value: Any = None,
) -> Dict[str, Any]:
    q_raw_f = _to_float(q_raw)
    if q_raw_f is None:
        return {}
    q_raw_f = _clamp01(q_raw_f)
    data = dict(bundle) if isinstance(bundle, dict) else load_q_calibration_bundle(bundle_path)
    enabled = _parse_bool(data.get("enabled"), True) if data else False
    segment_key, seg_ctx = build_segment_key(
        regime=regime,
        days_to_event=days_to_event,
        spread=spread,
        depth_ratio=depth_ratio,
        tick_age_sec=tick_age_sec,
        session=session,
        decision_ts=decision_ts,
    )
    version = str((data or {}).get("version") or "platt_fallback")
    generated_at = str((data or {}).get("generated_at") or "")
    sample_n = _to_int((data or {}).get("sample_n"))
    z = _to_float(z_value)
    if z is None:
        z = _to_float((data or {}).get("z"))
    if z is None or z <= 0 or not math.isfinite(z):
        z = 1.64

    residual_global = _to_float((data or {}).get("global_residual_mean"))
    if residual_global is None:
        residual_global = 0.0
    sigma_global = _to_float((data or {}).get("global_sigma"))
    if sigma_global is None or sigma_global <= 0:
        sigma_global = 0.08
    min_segment_n = _to_int((data or {}).get("min_segment_n"))
    if min_segment_n is None or min_segment_n < 1:
        min_segment_n = 20

    platt_a, platt_b, source = _resolve_platt(data if enabled else {})
    if platt_a is not None and platt_b is not None:
        base = _safe_sigmoid(platt_a * _safe_logit(q_raw_f) + platt_b)
    else:
        base = q_raw_f

    residual = residual_global
    sigma = sigma_global
    segment_used = "global"
    segment_n = None
    fallback_rank = 0
    if enabled and isinstance(data, dict):
        seg_payload, seg_key_used, fallback_rank = _lookup_segment(data, segment_key)
        if isinstance(seg_payload, dict):
            cand_n = _to_int(seg_payload.get("sample_n"))
            cand_res = _to_float(seg_payload.get("residual_mean"))
            cand_sigma = _to_float(seg_payload.get("residual_sigma"))
            if cand_n is not None and cand_n >= int(min_segment_n):
                if cand_res is not None:
                    residual = cand_res
                if cand_sigma is not None and cand_sigma > 0:
                    sigma = cand_sigma
                segment_used = seg_key_used
                segment_n = cand_n
                source = "bundle_segmented_platt"
            else:
                segment_used = "global"
                segment_n = cand_n
                if source == "bundle_global_platt":
                    source = "bundle_global_platt_low_segment_n"
    sigma = max(0.005, min(float(sigma), 0.45))
    mean = _clamp01(float(base) + float(residual))
    lcb = _clamp01(mean - float(z) * sigma)
    ucb = _clamp01(mean + float(z) * sigma)
    return {
        "q_calibrated_mean": float(mean),
        "q_calibrated_lcb": float(lcb),
        "q_calibrated_ucb": float(ucb),
        "q_calibrated_sigma": float(sigma),
        "q_calibrated_source": source,
        "q_calibration_version": version,
        "q_calibration_generated_at": generated_at,
        "q_calibration_sample_n": int(sample_n) if sample_n is not None else None,
        "q_calibration_segment_key": segment_key,
        "q_calibration_segment_used": segment_used,
        "q_calibration_segment_n": int(segment_n) if segment_n is not None else None,
        "q_calibration_fallback_rank": int(fallback_rank),
        "q_calibration_z": float(z),
        "q_calibration_context": {
            "regime": seg_ctx.regime,
            "days_to_event": seg_ctx.days_to_event,
            "spread": seg_ctx.spread,
            "depth_ratio": seg_ctx.depth_ratio,
            "tick_age_sec": seg_ctx.tick_age_sec,
            "session": seg_ctx.session,
        },
    }
