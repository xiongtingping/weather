from __future__ import annotations

from typing import Optional, Tuple, Dict, Any, Iterable
import math
import json
import os
import time


_EDGE_REALIZABLE_CALIBRATION_CACHE: Dict[str, Any] = {
    "path": "",
    "mtime": None,
    "loaded_at": 0.0,
    "data": None,
}


def _now_ts() -> float:
    return time.time()


def _default_bins() -> Dict[str, Any]:
    return {
        "days_to_settle": [0.0, 1.0, 2.0, 4.0, 8.0, 30.0, 9999.0],
        "spread": [0.0, 0.01, 0.03, 0.06, 0.1, 0.2, 1.0],
        "depth_ratio": [0.0, 0.5, 1.0, 2.0, 4.0, 10.0, 100.0],
        "edge": [0.0, 0.01, 0.03, 0.06, 0.1, 0.2, 1.0],
        "delta": [0.0, 0.01, 0.03, 0.06, 0.1, 0.2, 0.5, 1.0],
    }


def _finite(val: Optional[float]) -> bool:
    return val is not None and math.isfinite(val)


def fee_cost(exec_price: Optional[float], fee_bps: Optional[float]) -> Optional[float]:
    return 0.0


def edge_realizable(
    exec_edge_entry: Optional[float],
    exec_entry_price: Optional[float],
    fee_bps: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    if not _finite(exec_edge_entry):
        return None, None
    return float(exec_edge_entry), 0.0


def edge_realizable_score(
    edge_val: Optional[float],
    time_structure: Optional[float],
    penalty: Optional[float],
) -> Optional[float]:
    if edge_val is None or not math.isfinite(edge_val):
        return None
    ts = float(time_structure) if time_structure is not None and math.isfinite(time_structure) else 1.0
    pen = float(penalty) if penalty is not None and math.isfinite(penalty) else 0.0
    return max(float(edge_val), 0.0) * 100.0 * ts - pen


def _bin_index(val: Optional[float], edges: Iterable[float]) -> str:
    if val is None or not math.isfinite(val):
        return "u"
    v = float(val)
    edge_list = list(edges)
    for idx, upper in enumerate(edge_list):
        if v <= float(upper):
            return str(idx)
    return str(len(edge_list) - 1) if edge_list else "u"


def edge_realizable_bucket_key(
    *,
    days_to_settle: Optional[float],
    spread: Optional[float],
    depth_ratio: Optional[float],
    edge_val: Optional[float],
    delta_exit: Optional[float],
    order_type: Optional[str],
    bins: Optional[Dict[str, Any]] = None,
) -> str:
    b = bins or _default_bins()
    days_idx = _bin_index(days_to_settle, b.get("days_to_settle") or [])
    spread_idx = _bin_index(spread, b.get("spread") or [])
    depth_idx = _bin_index(depth_ratio, b.get("depth_ratio") or [])
    edge_idx = _bin_index(edge_val, b.get("edge") or [])
    delta_idx = _bin_index(delta_exit, b.get("delta") or [])
    ot = str(order_type or "").strip().upper() or "ANY"
    return f"d{days_idx}|s{spread_idx}|r{depth_idx}|e{edge_idx}|x{delta_idx}|ot:{ot}"


def _load_calibration(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return {}
    cache = _EDGE_REALIZABLE_CALIBRATION_CACHE
    if cache.get("path") == path and cache.get("mtime") == mtime and cache.get("data") is not None:
        return dict(cache["data"] or {})
    try:
        raw = json.loads(open(path, "r", encoding="utf-8").read())
    except Exception:
        raw = {}
    cache["path"] = path
    cache["mtime"] = mtime
    cache["loaded_at"] = _now_ts()
    cache["data"] = raw if isinstance(raw, dict) else {}
    return dict(cache["data"] or {})


def _lookup_prob(table: Dict[str, Any], key: str, default: float) -> float:
    if not isinstance(table, dict):
        return float(default)
    entry = table.get(key)
    if isinstance(entry, dict):
        val = entry.get("p")
        try:
            return float(val)
        except Exception:
            return float(default)
    try:
        return float(entry)
    except Exception:
        return float(default)


def _lookup_prob_meta(table: Dict[str, Any], key: str, default: float) -> Tuple[float, Dict[str, Any]]:
    meta = {"found": False, "hit": None, "total": None, "bucket": key, "fallback_default": False}
    if not isinstance(table, dict):
        meta["fallback_default"] = True
        return float(default), meta
    entry = table.get(key)
    if entry is None:
        meta["fallback_default"] = True
        return float(default), meta
    meta["found"] = True
    if isinstance(entry, dict):
        meta["hit"] = entry.get("hit")
        meta["total"] = entry.get("total")
        val = entry.get("p")
        try:
            return float(val), meta
        except Exception:
            meta["fallback_default"] = True
            return float(default), meta
    try:
        return float(entry), meta
    except Exception:
        meta["fallback_default"] = True
        return float(default), meta


def _resolve_hold_cost(
    *,
    hours_to_settle: Optional[float],
    params: Optional[Dict[str, Any]] = None,
) -> float:
    if hours_to_settle is None or not math.isfinite(hours_to_settle) or hours_to_settle <= 0:
        return 0.0
    mode = str((params or {}).get("mode") or os.environ.get("PM_EDGE_REALIZABLE_HOLD_MODE") or "linear").strip().lower()
    if mode not in {"linear", "exp"}:
        mode = "linear"
    if mode == "exp":
        lam_raw = (params or {}).get("lambda")
        if lam_raw is None:
            lam_raw = os.environ.get("PM_EDGE_REALIZABLE_HOLD_LAMBDA")
        try:
            lam = float(lam_raw)
        except Exception:
            lam = 0.0
        if not math.isfinite(lam) or lam <= 0:
            return 0.0
        return float(lam) * float(hours_to_settle)
    k_raw = (params or {}).get("k_per_hour")
    if k_raw is None:
        k_raw = os.environ.get("PM_EDGE_REALIZABLE_HOLD_K_PER_HOUR")
    try:
        k_val = float(k_raw)
    except Exception:
        k_val = 0.0
    if not math.isfinite(k_val) or k_val <= 0:
        return 0.0
    return float(k_val) * float(hours_to_settle)


def edge_realizable_v2(
    exec_edge_entry: Optional[float],
    exec_entry_price: Optional[float],
    fee_bps: Optional[float],
    *,
    target_exit_price: Optional[float] = None,
    base_bid: Optional[float] = None,
    spread: Optional[float] = None,
    depth_ratio: Optional[float] = None,
    days_to_settle: Optional[float] = None,
    hours_to_settle: Optional[float] = None,
    order_type: Optional[str] = None,
    calibration_path: Optional[str] = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    base_edge, _fee = edge_realizable(exec_edge_entry, exec_entry_price, fee_bps)
    if base_edge is None or not math.isfinite(base_edge):
        return None, {}
    calib_path = (
        str(calibration_path or os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH") or "").strip()
    )
    calib = _load_calibration(calib_path) if calib_path else {}
    bins = calib.get("bins") if isinstance(calib.get("bins"), dict) else _default_bins()
    delta_exit = None
    if target_exit_price is not None and math.isfinite(target_exit_price):
        if base_bid is not None and math.isfinite(base_bid):
            delta_exit = float(target_exit_price) - float(base_bid)
    key = edge_realizable_bucket_key(
        days_to_settle=days_to_settle,
        spread=spread,
        depth_ratio=depth_ratio,
        edge_val=base_edge,
        delta_exit=delta_exit,
        order_type=order_type,
        bins=bins,
    )
    key_any = key
    if "|ot:" in key and not key.endswith("ot:ANY"):
        key_any = key.rsplit("|ot:", 1)[0] + "|ot:ANY"
    try:
        min_samples = int(os.environ.get("PM_EDGE_REALIZABLE_MIN_SAMPLES", "20"))
    except Exception:
        min_samples = 20
    if min_samples < 1:
        min_samples = 1

    def _resolve_prob_optional(
        table: Dict[str, Any],
        key_primary: str,
        key_fallback: str,
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        val, meta = _lookup_prob_meta(table, key_primary, 0.0)
        if (not meta.get("found")) and key_fallback != key_primary:
            val_fb, meta_fb = _lookup_prob_meta(table, key_fallback, 0.0)
            if meta_fb.get("found"):
                meta = meta_fb
                meta["fallback_key_any"] = True
                meta["bucket"] = key_fallback
                val = val_fb
        if not meta.get("found"):
            meta["fallback_default"] = True
            return None, meta
        try:
            val_f = float(val)
        except Exception:
            return None, meta
        if not math.isfinite(val_f):
            return None, meta
        return float(val_f), meta

    p_converge, p_converge_meta = _resolve_prob_optional(
        calib.get("p_converge_buckets") or {},
        key,
        key_any,
    )
    key_entry = edge_realizable_bucket_key(
        days_to_settle=days_to_settle,
        spread=spread,
        depth_ratio=depth_ratio,
        edge_val=None,
        delta_exit=None,
        order_type=order_type,
        bins=bins,
    )
    key_entry_any = key_entry
    if "|ot:" in key_entry and not key_entry.endswith("ot:ANY"):
        key_entry_any = key_entry.rsplit("|ot:", 1)[0] + "|ot:ANY"
    p_fill_entry, p_fill_entry_meta = _resolve_prob_optional(
        calib.get("p_fill_entry_buckets") or {},
        key_entry,
        key_entry_any,
    )
    p_fill_exit, p_fill_exit_meta = _resolve_prob_optional(
        calib.get("p_fill_exit_buckets") or {},
        key_entry,
        key_entry_any,
    )
    if p_fill_entry is None or p_converge is None or p_fill_exit is None:
        p_realize = None
    else:
        p_realize = max(0.0, min(1.0, float(p_fill_entry) * float(p_converge) * float(p_fill_exit)))
    def _is_low_sample(meta: Dict[str, Any]) -> bool:
        if not isinstance(meta, dict):
            return False
        total = meta.get("total")
        try:
            total_val = int(total)
        except Exception:
            return False
        return total_val < int(min_samples)
    def _is_missing(meta: Dict[str, Any]) -> bool:
        return not bool(meta.get("found")) if isinstance(meta, dict) else True
    metas = [p_converge_meta, p_fill_entry_meta, p_fill_exit_meta]
    p_realize_missing = any(_is_missing(m) for m in metas)
    p_realize_low_sample = any((_is_low_sample(m) for m in metas if not _is_missing(m)))
    totals = []
    for meta in metas:
        if isinstance(meta, dict) and meta.get("total") is not None:
            try:
                totals.append(int(meta.get("total")))
            except Exception:
                pass
    p_realize_samples_min = min(totals) if totals else None
    if hours_to_settle is None and days_to_settle is not None and math.isfinite(days_to_settle):
        hours_to_settle = float(days_to_settle) * 24.0
    hold_params = calib.get("hold_cost_params") if isinstance(calib.get("hold_cost_params"), dict) else {}
    hold_cost = _resolve_hold_cost(hours_to_settle=hours_to_settle, params=hold_params)
    edge_val = None
    if p_realize is not None:
        edge_val = float(base_edge) * float(p_realize) - float(hold_cost)
    meta = {
        "p_converge": p_converge,
        "p_fill_entry": p_fill_entry,
        "p_fill_exit": p_fill_exit,
        "p_realize": p_realize,
        "p_converge_meta": p_converge_meta,
        "p_fill_entry_meta": p_fill_entry_meta,
        "p_fill_exit_meta": p_fill_exit_meta,
        "p_realize_missing": p_realize_missing,
        "p_realize_low_sample": p_realize_low_sample,
        "p_realize_samples_min": p_realize_samples_min,
        "p_realize_min_samples": int(min_samples),
        "hold_cost": hold_cost,
        "target_exit_price": target_exit_price,
        "delta_exit": delta_exit,
        "bucket_key": key,
        "calibration_path": calib_path,
    }
    return edge_val, meta
