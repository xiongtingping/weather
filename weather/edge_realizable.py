from __future__ import annotations

from typing import Optional, Tuple, Dict, Any, Iterable, List, Set
import math
import json
import os
import time


_EDGE_REALIZABLE_CALIBRATION_CACHE: Dict[str, Dict[str, Any]] = {}


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


def _as_prob(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    if num < 0.0:
        return 0.0
    if num > 1.0:
        return 1.0
    return float(num)


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
    cache_entry = _EDGE_REALIZABLE_CALIBRATION_CACHE.get(path)
    if (
        isinstance(cache_entry, dict)
        and cache_entry.get("mtime") == mtime
        and cache_entry.get("data") is not None
    ):
        return dict(cache_entry.get("data") or {})
    try:
        raw = json.loads(open(path, "r", encoding="utf-8").read())
    except Exception:
        raw = {}
    data = raw if isinstance(raw, dict) else {}
    _EDGE_REALIZABLE_CALIBRATION_CACHE[path] = {
        "mtime": mtime,
        "loaded_at": _now_ts(),
        "data": data,
    }
    return dict(data or {})


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

    def _normalize_path(path: Any) -> str:
        return str(path or "").strip()

    def _append_unique_path(paths: List[str], path: Any) -> None:
        text = _normalize_path(path)
        if not text:
            return
        if text in paths:
            return
        paths.append(text)

    calibration_paths: List[str] = []
    if calibration_path is not None and str(calibration_path).strip():
        _append_unique_path(calibration_paths, calibration_path)
    else:
        calib_orders_path = _normalize_path(os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH_ORDERS"))
        calib_default_path = _normalize_path(os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH"))
        calib_fallback_path = _normalize_path(os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH_FALLBACK"))
        calib_ticks_path = _normalize_path(os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH_TICKS"))
        if (not calib_orders_path) and calib_default_path.lower().endswith(".json"):
            auto_orders_path = calib_default_path[:-5] + ".orders.json"
            if auto_orders_path and auto_orders_path != calib_default_path and os.path.exists(auto_orders_path):
                calib_orders_path = auto_orders_path
        _append_unique_path(calibration_paths, calib_orders_path or calib_default_path)
        _append_unique_path(calibration_paths, calib_fallback_path)
        _append_unique_path(calibration_paths, calib_ticks_path)
        if calib_orders_path and calib_default_path and calib_orders_path != calib_default_path:
            _append_unique_path(calibration_paths, calib_default_path)
    if not calibration_paths:
        _append_unique_path(calibration_paths, os.environ.get("PM_EDGE_REALIZABLE_CALIBRATION_PATH"))

    source_calibs: List[Dict[str, Any]] = []
    for idx, path in enumerate(calibration_paths):
        calib_raw = _load_calibration(path) if path else {}
        calib_dict = calib_raw if isinstance(calib_raw, dict) else {}
        bins = calib_dict.get("bins") if isinstance(calib_dict.get("bins"), dict) else _default_bins()
        source_calibs.append(
            {
                "name": "primary" if idx == 0 else f"fallback_{idx}",
                "path": path,
                "fill_mode": str(calib_dict.get("fill_mode") or "").strip() or None,
                "generated_at": str(calib_dict.get("generated_at") or "").strip() or None,
                "bins": bins,
                "calib": calib_dict,
            }
        )

    base_bins = source_calibs[0].get("bins") if source_calibs else _default_bins()
    if not isinstance(base_bins, dict):
        base_bins = _default_bins()
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
        bins=base_bins,
    )

    try:
        min_samples = int(os.environ.get("PM_EDGE_REALIZABLE_MIN_SAMPLES", "20"))
    except Exception:
        min_samples = 20
    if min_samples < 1:
        min_samples = 1
    allow_global_defaults = str(os.environ.get("PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS", "1")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    def _key_any(key_text: str) -> str:
        if "|ot:" in key_text and not key_text.endswith("ot:ANY"):
            return key_text.rsplit("|ot:", 1)[0] + "|ot:ANY"
        return key_text

    def _resolve_prob_optional(
        table: Dict[str, Any],
        key_primary: str,
        key_fallbacks: List[str],
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        keys = [key_primary] + list(key_fallbacks or [])
        seen: Set[str] = set()
        first_meta: Optional[Dict[str, Any]] = None
        for idx, bucket_key in enumerate(keys):
            if not bucket_key or bucket_key in seen:
                continue
            seen.add(bucket_key)
            val, meta = _lookup_prob_meta(table, bucket_key, 0.0)
            if first_meta is None:
                first_meta = dict(meta) if isinstance(meta, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            if not meta.get("found"):
                continue
            if idx > 0:
                meta["fallback_bucket_rank"] = idx
                meta["fallback_bucket"] = bucket_key
                if bucket_key.endswith("ot:ANY"):
                    meta["fallback_key_any"] = True
            try:
                val_f = float(val)
            except Exception:
                return None, meta
            if not math.isfinite(val_f):
                return None, meta
            return float(val_f), meta
        meta_missing = first_meta if isinstance(first_meta, dict) else {"bucket": key_primary}
        meta_missing["fallback_default"] = True
        return None, meta_missing

    def _to_int(val: Any) -> Optional[int]:
        try:
            parsed = int(val)
        except Exception:
            return None
        return parsed

    def _entry_key_builder(bins: Dict[str, Any]) -> Tuple[str, List[str], Dict[str, Any]]:
        key_entry = edge_realizable_bucket_key(
            days_to_settle=days_to_settle,
            spread=spread,
            depth_ratio=depth_ratio,
            edge_val=None,
            delta_exit=None,
            order_type=order_type,
            bins=bins,
        )
        key_entry_any = _key_any(key_entry)
        key_entry_depth_unknown = edge_realizable_bucket_key(
            days_to_settle=days_to_settle,
            spread=spread,
            depth_ratio=None,
            edge_val=None,
            delta_exit=None,
            order_type=order_type,
            bins=bins,
        )
        key_entry_depth_unknown_any = _key_any(key_entry_depth_unknown)
        fallback_keys = [key_entry_any, key_entry_depth_unknown, key_entry_depth_unknown_any]
        return key_entry, fallback_keys, {}

    def _converge_key_builder(bins: Dict[str, Any]) -> Tuple[str, List[str], Dict[str, Any]]:
        spread_bins = bins.get("spread") if isinstance(bins, dict) else None
        delta_bins = bins.get("delta") if isinstance(bins, dict) else None
        spread_fallback = None
        delta_fallback = None
        spread_for_converge = spread if _finite(spread) else None
        if spread_for_converge is None:
            if isinstance(spread_bins, list) and spread_bins:
                spread_for_converge = float(spread_bins[-1])
                spread_fallback = "max"
        else:
            spread_val = float(spread_for_converge)
            if spread_val <= 0 and isinstance(spread_bins, list) and len(spread_bins) > 1:
                spread_for_converge = float(spread_bins[1])
                spread_fallback = "min_nonzero"
        delta_for_converge = delta_exit if _finite(delta_exit) else None
        if delta_for_converge is None:
            if isinstance(delta_bins, list) and delta_bins:
                delta_for_converge = float(delta_bins[-1])
                delta_fallback = "max"
        key_converge = edge_realizable_bucket_key(
            days_to_settle=days_to_settle,
            spread=spread_for_converge,
            depth_ratio=None,
            edge_val=None,
            delta_exit=delta_for_converge,
            order_type=order_type,
            bins=bins,
        )
        extra = {"depth_ratio_fallback": "ru"}
        if spread_fallback:
            extra["spread_fallback"] = spread_fallback
        if delta_fallback:
            extra["delta_fallback"] = delta_fallback
        return key_converge, [_key_any(key_converge)], extra

    def _resolve_prob_from_sources(
        table_name: str,
        key_builder: Any,
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        low_sample_choice: Optional[Tuple[float, Dict[str, Any], int]] = None
        first_meta: Optional[Dict[str, Any]] = None
        for src in source_calibs:
            bins = src.get("bins")
            if not isinstance(bins, dict):
                bins = _default_bins()
            key_primary, key_fallbacks, extra_meta = key_builder(bins)
            table = src.get("calib", {}).get(table_name) if isinstance(src.get("calib"), dict) else {}
            if not isinstance(table, dict):
                table = {}
            val, meta = _resolve_prob_optional(table, key_primary, key_fallbacks)
            if not isinstance(meta, dict):
                meta = {}
            meta.setdefault("bucket", key_primary)
            meta["source_name"] = src.get("name")
            meta["source_path"] = src.get("path")
            meta["source_fill_mode"] = src.get("fill_mode")
            meta["source_generated_at"] = src.get("generated_at")
            meta["source_table"] = table_name
            if isinstance(extra_meta, dict) and extra_meta:
                meta.update(extra_meta)
            total_val = _to_int(meta.get("total"))
            if total_val is not None:
                meta["total"] = total_val
            if val is None:
                if first_meta is None:
                    first_meta = dict(meta)
                continue
            low_sample = bool(total_val is not None and total_val < int(min_samples))
            meta["low_sample"] = low_sample
            if not low_sample:
                return float(val), meta
            score = total_val if total_val is not None else -1
            if low_sample_choice is None or score > low_sample_choice[2]:
                low_sample_choice = (float(val), meta, score)
        if low_sample_choice is not None:
            val, meta, _score = low_sample_choice
            meta["fallback_low_sample"] = True
            return val, meta
        if first_meta is not None:
            first_meta["fallback_default"] = True
            return None, first_meta
        return None, {"found": False, "fallback_default": True}

    def _table_default_prob(
        calib_obj: Dict[str, Any],
        table_name: str,
        explicit_key: str,
    ) -> Tuple[Optional[float], Optional[str]]:
        explicit = _as_prob(calib_obj.get(explicit_key))
        if explicit is not None:
            return explicit, "explicit_default"
        table = calib_obj.get(table_name)
        if not isinstance(table, dict) or not table:
            return None, None
        weighted_num = 0.0
        weighted_den = 0.0
        simple_vals: List[float] = []
        for entry in table.values():
            p_val = None
            total_val = None
            if isinstance(entry, dict):
                p_val = _as_prob(entry.get("p"))
                total_val = _to_int(entry.get("total"))
            else:
                p_val = _as_prob(entry)
            if p_val is None:
                continue
            if total_val is not None and total_val > 0:
                weighted_num += float(p_val) * float(total_val)
                weighted_den += float(total_val)
            else:
                simple_vals.append(float(p_val))
        if weighted_den > 0:
            return _as_prob(weighted_num / weighted_den), "weighted_total"
        if simple_vals:
            return _as_prob(sum(simple_vals) / float(len(simple_vals))), "simple_mean"
        return None, None

    def _apply_default_prob(
        *,
        current_val: Optional[float],
        current_meta: Dict[str, Any],
        table_name: str,
        explicit_key: str,
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        if current_val is not None or not allow_global_defaults:
            return current_val, current_meta
        for src in source_calibs:
            calib_obj = src.get("calib")
            if not isinstance(calib_obj, dict):
                continue
            default_val, method = _table_default_prob(calib_obj, table_name, explicit_key)
            if default_val is None:
                continue
            if not isinstance(current_meta, dict):
                current_meta = {}
            current_meta["fallback_default"] = True
            current_meta["fallback_default_value"] = default_val
            if method:
                current_meta["fallback_default_method"] = method
            current_meta["source_name"] = src.get("name")
            current_meta["source_path"] = src.get("path")
            current_meta["source_fill_mode"] = src.get("fill_mode")
            current_meta["source_generated_at"] = src.get("generated_at")
            return default_val, current_meta
        return current_val, current_meta

    p_converge, p_converge_meta = _resolve_prob_from_sources(
        "p_converge_buckets",
        _converge_key_builder,
    )
    p_fill_entry, p_fill_entry_meta = _resolve_prob_from_sources(
        "p_fill_entry_buckets",
        _entry_key_builder,
    )
    p_fill_exit, p_fill_exit_meta = _resolve_prob_from_sources(
        "p_fill_exit_buckets",
        _entry_key_builder,
    )
    p_converge, p_converge_meta = _apply_default_prob(
        current_val=p_converge,
        current_meta=p_converge_meta,
        table_name="p_converge_buckets",
        explicit_key="p_converge_default",
    )
    p_fill_entry, p_fill_entry_meta = _apply_default_prob(
        current_val=p_fill_entry,
        current_meta=p_fill_entry_meta,
        table_name="p_fill_entry_buckets",
        explicit_key="p_fill_entry_default",
    )
    p_fill_exit, p_fill_exit_meta = _apply_default_prob(
        current_val=p_fill_exit,
        current_meta=p_fill_exit_meta,
        table_name="p_fill_exit_buckets",
        explicit_key="p_fill_exit_default",
    )
    if p_fill_entry is None or p_converge is None or p_fill_exit is None:
        p_realize = None
    else:
        p_realize = max(0.0, min(1.0, float(p_fill_entry) * float(p_converge) * float(p_fill_exit)))

    def _is_low_sample(meta: Dict[str, Any]) -> bool:
        if not isinstance(meta, dict):
            return False
        total_val = _to_int(meta.get("total"))
        if total_val is None:
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
            total_val = _to_int(meta.get("total"))
            if total_val is not None:
                totals.append(total_val)
    p_realize_samples_min = min(totals) if totals else None
    if hours_to_settle is None and days_to_settle is not None and math.isfinite(days_to_settle):
        hours_to_settle = float(days_to_settle) * 24.0
    hold_params: Dict[str, Any] = {}
    for src in source_calibs:
        calib_obj = src.get("calib")
        if not isinstance(calib_obj, dict):
            continue
        raw_params = calib_obj.get("hold_cost_params")
        if isinstance(raw_params, dict):
            hold_params = raw_params
            break
    hold_cost = _resolve_hold_cost(hours_to_settle=hours_to_settle, params=hold_params)
    edge_val = None
    if p_realize is not None:
        edge_val = float(base_edge) * float(p_realize) - float(hold_cost)
    primary_calib_path = calibration_paths[0] if calibration_paths else ""
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
        "calibration_path": primary_calib_path,
        "calibration_paths": calibration_paths,
        "calibration_sources": [
            {
                "name": src.get("name"),
                "path": src.get("path"),
                "fill_mode": src.get("fill_mode"),
                "generated_at": src.get("generated_at"),
            }
            for src in source_calibs
        ],
        "p_converge_source": p_converge_meta.get("source_name") if isinstance(p_converge_meta, dict) else None,
        "p_fill_entry_source": p_fill_entry_meta.get("source_name") if isinstance(p_fill_entry_meta, dict) else None,
        "p_fill_exit_source": p_fill_exit_meta.get("source_name") if isinstance(p_fill_exit_meta, dict) else None,
    }
    return edge_val, meta
