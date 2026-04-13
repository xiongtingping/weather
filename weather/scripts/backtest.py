#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
import random

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scan_weather_market as server


DB_PATH = server.DB_PATH
FOCUS_MODELS = ["gfs_seamless", "icon_seamless", "ecmwf_ifs025", "nws_forecast"]
WEIGHTINGS = ["equal", "auto", "scenario"]


@dataclass
class StrategyConfig:
    min_edge: float = 0.0
    max_ask: float = 0.98
    max_q: float = 1.0
    min_q: float = 0.0
    max_spread: float = 0.10
    min_model_spread: float = 0.0
    max_model_spread: float = 1.0
    min_models: int = 2
    stake: float = 10.0
    top_n: int = 1
    strict_auto: bool = True
    max_lead_days: Optional[int] = None
    temp_offset: float = 0.0
    temp_offset_c: Optional[float] = None
    temp_offset_f: Optional[float] = None
    actual_rounding: str = "none"
    predict_mode: str = "top_q"
    predict_quantile: float = 0.5
    predict_offset: float = 0.0
    predict_offset_c: Optional[float] = None
    predict_offset_f: Optional[float] = None
    auto_days: int = 7
    auto_power: float = 1.0
    auto_mode: str = "global"
    auto_alpha: float = 0.0
    auto_blend: float = 1.0
    auto_min_samples: int = 0
    early_gfs_share: float = 0.5
    mid_ecmwf_share: float = 0.6
    late_nws_share: float = 0.6
    integer_ecmwf_share: float = 0.55


@dataclass
class SearchConfig:
    ranges: Dict[str, List]
    systems: List[str]
    min_trades: int = 5
    max_trials: Optional[int] = None
    seed: int = 7
    require_all_systems: bool = True
    target_hit: Optional[float] = None
    force_predict: bool = False


def top_model_bucket(rows: list) -> str:
    best_label = ""
    best_q = None
    best_key = (float("inf"), float("inf"), "")
    for r in rows:
        label = r.get("outcome") or ""
        if not label:
            continue
        q = r.get("q_round")
        if q is None:
            continue
        try:
            q_val = float(q)
        except (TypeError, ValueError):
            continue
        key = server._bucket_sort_key(label)
        if best_q is None or q_val > best_q or (q_val == best_q and key < best_key):
            best_q = q_val
            best_label = label
            best_key = key
    return best_label


def expected_value_from_rows(
    rows: list,
    rounding_rule: Optional[str] = None,
    lat: Optional[float] = None,
    event_date: Optional[str] = None,
) -> Optional[float]:
    total = 0.0
    acc = 0.0
    for r in rows:
        label = r.get("outcome") or ""
        rep = server._bucket_expected_value_from_label(label, rounding_rule, lat, event_date)
        if rep is None:
            continue
        try:
            q = float(r.get("q_round") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(q):
            continue
        total += q
        acc += rep * q
    if total <= 0:
        return None
    return acc / total


def recompute_rows_and_suggestions(
    rows: list,
    models: list,
    weighting: str,
    weights_override: Optional[dict] = None,
    weight_date: Optional[str] = None,
) -> Tuple[List[dict], List[dict]]:
    weights = server.resolve_weights(
        models,
        weighting,
        weights_override=weights_override,
        weight_date=weight_date,
    )
    rows_out = []
    suggestions = []
    for r in rows:
        label = r.get("outcome") or ""
        if not label:
            continue
        per_model_q = r.get("per_model_q") or {}
        candidate_models = [
            m
            for m in models
            if isinstance(per_model_q.get(m), dict) and float(weights.get(m, 0.0) or 0.0) > 0
        ]
        if not candidate_models:
            candidate_models = [m for m in models if isinstance(per_model_q.get(m), dict)]
        usable = []
        for model in candidate_models:
            qpm = per_model_q.get(model) or {}
            if not isinstance(qpm, dict):
                continue
            try:
                q_round_val = float(qpm.get("q_round", 0.0))
                q_floor_val = float(qpm.get("q_floor", qpm.get("q_round", 0.0)))
                q_ceil_val = float(qpm.get("q_ceil", qpm.get("q_round", 0.0)))
                q_min_val = float(qpm.get("q_min", qpm.get("q_round", 0.0)))
                q_max_val = float(qpm.get("q_max", qpm.get("q_round", 0.0)))
            except (TypeError, ValueError):
                continue
            usable.append((model, q_round_val, q_floor_val, q_ceil_val, q_min_val, q_max_val))
        if not usable:
            continue
        q_round_models = [item[1] for item in usable]
        q_min_models = [item[4] for item in usable]
        q_max_models = [item[5] for item in usable]
        sum_w = sum(float(weights.get(model, 0.0) or 0.0) for model, *_ in usable)
        use_equal = sum_w <= 0
        denom = float(len(usable)) if use_equal else sum_w
        q_round = 0.0
        q_floor = 0.0
        q_ceil = 0.0
        for model, q_round_val, q_floor_val, q_ceil_val, _q_min_val, _q_max_val in usable:
            w = 1.0 if use_equal else float(weights.get(model, 0.0) or 0.0)
            q_round += w * q_round_val
            q_floor += w * q_floor_val
            q_ceil += w * q_ceil_val
        q_round /= denom
        q_floor /= denom
        q_ceil /= denom
        q_min = min(q_min_models)
        q_max = max(q_max_models)
        q_round_no = 1.0 - q_round
        q_min_no = 1.0 - q_max
        best_bid = r.get("best_bid")
        best_ask = r.get("best_ask")
        best_bid_no = r.get("best_bid_no")
        best_ask_no = r.get("best_ask_no")
        spread = r.get("spread")
        spread_no = r.get("spread_no")
        signal_edge_round = (q_round - best_ask) if (best_ask is not None) else None
        signal_edge_round_no = (q_round_no - best_ask_no) if (best_ask_no is not None) else None
        model_spread = r.get("q_round_model_spread")
        if model_spread is None:
            model_spread = max(q_round_models) - min(q_round_models)
        row_out = dict(r)
        row_out.update(
            {
                "q_round": q_round,
                "q_floor": q_floor,
                "q_ceil": q_ceil,
                "q_min": q_min,
                "q_max": q_max,
                "q_round_no": q_round_no,
                "q_min_no": q_min_no,
                "signal_edge_round": signal_edge_round,
                "signal_edge_round_no": signal_edge_round_no,
                "q_round_model_spread": model_spread,
            }
        )
        rows_out.append(row_out)
        if signal_edge_round is not None:
            suggestions.append(
                {
                    "side": "YES",
                    "outcome": label,
                    "edge": signal_edge_round,
                    "q": q_round,
                    "best_ask": best_ask,
                    "best_bid": best_bid,
                    "spread": spread,
                    "model_spread": model_spread,
                }
            )
        if signal_edge_round_no is not None:
            suggestions.append(
                {
                    "side": "NO",
                    "outcome": label,
                    "edge": signal_edge_round_no,
                    "q": q_round_no,
                    "best_ask": best_ask_no,
                    "best_bid": best_bid_no,
                    "spread": spread_no,
                    "model_spread": model_spread,
                }
            )
    return rows_out, suggestions


def _weighted_bias(weights: Dict[str, float], bias_map: Dict[str, Dict[str, float]]) -> Optional[float]:
    if not weights:
        return None
    total = 0.0
    acc = 0.0
    for model, weight in weights.items():
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        entry = bias_map.get(model) or {}
        bias = entry.get("mean")
        try:
            b = float(bias) if bias is not None else 0.0
        except (TypeError, ValueError):
            b = 0.0
        total += w
        acc += w * b
    if total <= 0:
        return None
    return acc / total


def _apply_place_bias(
    base_bias: Optional[float],
    field: str,
    place_entry: Optional[dict],
    market_unit: str,
) -> Optional[float]:
    if base_bias is None or not place_entry:
        return base_bias
    count = place_entry.get(f"{field}_count") or 0.0
    if count < server.PLACE_BIAS_MIN_COUNT:
        return base_bias
    raw = place_entry.get(field)
    place_val = server._bias_to_unit(raw, market_unit) if raw is not None else None
    if place_val is None:
        return base_bias
    alpha = float(count) / (float(count) + server.PLACE_BIAS_SHRINK_K)
    mixed = alpha * float(place_val) + (1.0 - alpha) * float(base_bias)
    clamp = server.PLACE_BIAS_CLAMP_C
    if mixed > clamp:
        mixed = clamp
    elif mixed < -clamp:
        mixed = -clamp
    return mixed


def corrected_top_bucket_from_event(ev: dict, weighting: str, cfg: StrategyConfig) -> str:
    if weighting not in WEIGHTINGS:
        return ""
    market_var = ev.get("market_var") or ""
    if not market_var.startswith("temperature"):
        return ""
    model_members = ev.get("model_members") or {}
    if not model_members:
        return ""
    rows_all = ev.get("rows_all") or []
    models = ev.get("models") or []
    if not rows_all or not models:
        return ""
    run_date = ev.get("run_date") or ""
    rounding_rule = ev.get("rounding_rule") or ""
    market_unit = ev.get("market_unit") or ""

    weights_override, _weight_date = resolve_weighting_inputs(
        rows_all,
        models,
        weighting,
        ev.get("run_date") or "",
        ev.get("event_date") or "",
        ev.get("rounding_rule") or "",
        ev.get("market_unit") or "",
        cfg.strict_auto,
        cfg,
    )
    if weighting == "equal":
        weights = equal_weights(models)
    elif weighting == "scenario":
        weights = weights_override or equal_weights(models)
    else:
        weights = weights_override or equal_weights(models)
    weights = normalize_weights(weights, models)
    if not weights:
        return ""

    apply_model_bias = weighting in {"equal", "scenario"}
    residual = 0.0
    bias_map = server.get_recent_model_bias(7, end_date=run_date) if apply_model_bias else {}
    if apply_model_bias:
        base_bias_c = _weighted_bias(weights, bias_map)
        base_bias = server._bias_to_unit(base_bias_c, market_unit) if base_bias_c is not None else None
        place_bias_map = server.get_recent_place_bias(7, end_date=run_date)
        place_entry = None
        station_id = (ev.get("station_id") or "").strip()
        city = (ev.get("city") or "").strip()
        if station_id:
            place_entry = (place_bias_map.get("station") or {}).get(station_id)
        if place_entry is None and city:
            place_entry = (place_bias_map.get("city") or {}).get(city)
        target_bias = _apply_place_bias(base_bias, weighting, place_entry, market_unit)
        if target_bias is not None and base_bias is not None:
            residual = float(target_bias) - float(base_bias)

    members_by_model: Dict[str, server.np.ndarray] = {}
    for model in models:
        values = model_members.get(model)
        if values is None:
            continue
        try:
            arr = server.np.array(values, dtype=float)
        except Exception:
            continue
        if arr.size == 0:
            continue
        shift = residual
        if apply_model_bias:
            entry = bias_map.get(model) or {}
            bias_unit = server._bias_to_unit(entry.get("mean"), market_unit)
            try:
                shift += float(bias_unit or 0.0)
            except (TypeError, ValueError):
                pass
        if shift:
            arr = arr - shift
        members_by_model[model] = arr
    if not members_by_model:
        return ""

    best_label = ""
    best_q = -1.0
    best_key = (float("inf"), float("inf"), "")
    for row in rows_all:
        label = row.get("outcome") or ""
        if not label:
            continue
        try:
            spec = server.parse_outcome_label(label)
        except ValueError:
            continue
        total_w = 0.0
        acc = 0.0
        for model in models:
            w = float(weights.get(model, 0.0))
            if w <= 0:
                continue
            values = members_by_model.get(model)
            if values is None or values.size == 0:
                continue
            q_band = server.compute_q_band(values, spec, rounding_rule)
            acc += w * float(q_band.get("q_round") or 0.0)
            total_w += w
        if total_w <= 0:
            continue
        q_val = acc / total_w
        key = server._bucket_sort_key(label)
        if q_val > best_q or (q_val == best_q and key < best_key):
            best_q = q_val
            best_label = label
            best_key = key
    return best_label


server.top_model_bucket = top_model_bucket
server.expected_value_from_rows = expected_value_from_rows
server.recompute_rows_and_suggestions = recompute_rows_and_suggestions

def parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def date_range(days: int) -> Tuple[str, str]:
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    return start.isoformat(), today.isoformat()


def load_runs(run_start: str, run_end: str) -> List[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM event_runs
        WHERE run_date >= ? AND run_date <= ?
          AND settlement_actual IS NOT NULL
        ORDER BY created_at ASC
        """,
        (run_start, run_end),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def event_key(row: dict) -> str:
    slug = row.get("slug") or ""
    title = row.get("title") or ""
    event_date = row.get("event_date") or ""
    return slug if slug else f"{title}|{event_date}"


def pick_latest(existing: Optional[dict], row: dict) -> dict:
    if not existing:
        return row
    return row if (row.get("created_at") or "") > (existing.get("created_at") or "") else existing


def group_by_run_date(rows: Iterable[dict]) -> Dict[str, Dict[str, dict]]:
    grouped: Dict[str, Dict[str, dict]] = {}
    for r in rows:
        run_date = r.get("run_date") or ""
        if not run_date:
            continue
        key = event_key(r)
        grouped.setdefault(run_date, {})
        grouped[run_date][key] = pick_latest(grouped[run_date].get(key), r)
    return grouped


def lead_bucket(run_date: str, event_date: str) -> str:
    rd = parse_date(run_date)
    ed = parse_date(event_date)
    if not rd or not ed:
        return "unknown"
    delta = (ed - rd).days
    if delta < 0:
        return "post"
    if delta == 0:
        return "D0"
    if delta == 1:
        return "D1"
    if 2 <= delta <= 3:
        return "D2-3"
    if 4 <= delta <= 7:
        return "D4-7"
    return "D8+"


def lead_days(run_date: str, event_date: str) -> Optional[int]:
    rd = parse_date(run_date)
    ed = parse_date(event_date)
    if not rd or not ed:
        return None
    return (ed - rd).days


def resolve_basis(row: dict) -> str:
    basis = (row.get("location_basis") or "").strip()
    if basis:
        return basis
    if row.get("station_id"):
        return "station"
    if row.get("city"):
        return "city"
    return "unknown"


def adjust_actual(actual: float, unit: str, rounding_rule: str, cfg: StrategyConfig) -> float:
    val = actual
    mode = (cfg.actual_rounding or "none").lower()
    rule = (rounding_rule or "").lower()
    if mode == "by_rule":
        if rule == "whole_degrees":
            return math.floor(val)
        return val
    if mode == "floor":
        return math.floor(val)
    if mode == "ceil":
        return math.ceil(val)
    if mode == "round":
        return round(val)
    if mode == "half_up":
        return round(val * 2.0) / 2.0
    return val


def adjust_prediction_value(value: float, unit: str, cfg: StrategyConfig) -> float:
    return value


def compute_model_spread(models: list, row: dict) -> float:
    per_model = row.get("per_model_q") or {}
    values = []
    for model in models:
        data = per_model.get(model) or {}
        q = data.get("q_round")
        if q is None:
            continue
        try:
            values.append(float(q))
        except (TypeError, ValueError):
            continue
    if not values:
        return 0.0
    return max(values) - min(values)


def build_model_suggestions(rows: list, model: str, models: list) -> List[dict]:
    suggestions = []
    for r in rows:
        per_model = r.get("per_model_q") or {}
        data = per_model.get(model) or {}
        q = data.get("q_round")
        if q is None:
            continue
        try:
            q = float(q)
        except (TypeError, ValueError):
            continue
        q_min = data.get("q_min", q)
        q_max = data.get("q_max", q)
        q_no = 1.0 - q
        q_min_no = 1.0 - q_max
        best_ask = r.get("best_ask")
        best_ask_no = r.get("best_ask_no")
        best_bid = r.get("best_bid")
        best_bid_no = r.get("best_bid_no")
        model_spread = compute_model_spread(models, r)
        limit_rng = server.suggest_limit_price(best_bid, best_ask, q_min, 0.90)
        limit_rng_no = server.suggest_limit_price(best_bid_no, best_ask_no, q_min_no, 0.90)
        if best_ask is not None and limit_rng is not None:
            edge = q - best_ask
            suggestions.append(
                {
                    "side": "YES",
                    "outcome": r.get("outcome"),
                    "edge": edge,
                    "q": q,
                    "best_ask": best_ask,
                    "best_bid": best_bid,
                    "spread": r.get("spread"),
                    "model_spread": model_spread,
                    "limit_low": limit_rng[0],
                    "limit_high": limit_rng[1],
                }
            )
        if best_ask_no is not None and limit_rng_no is not None:
            edge_no = q_no - best_ask_no
            suggestions.append(
                {
                    "side": "NO",
                    "outcome": r.get("outcome"),
                    "edge": edge_no,
                    "q": q_no,
                    "best_ask": best_ask_no,
                    "best_bid": best_bid_no,
                    "spread": r.get("spread_no"),
                    "model_spread": model_spread,
                    "limit_low": limit_rng_no[0],
                    "limit_high": limit_rng_no[1],
                }
            )
    suggestions.sort(key=lambda s: s.get("edge") or 0.0, reverse=True)
    return suggestions


def build_model_bucket_rows(rows: list, model: str) -> List[dict]:
    buckets = []
    for r in rows:
        per_model = r.get("per_model_q") or {}
        data = per_model.get(model) or {}
        q = data.get("q_round")
        if q is None:
            continue
        try:
            q = float(q)
        except (TypeError, ValueError):
            continue
        buckets.append({"outcome": r.get("outcome"), "q_round": q})
    return buckets


def filter_suggestions(suggestions: List[dict], cfg: StrategyConfig) -> List[dict]:
    filtered = []
    for s in suggestions:
        edge = s.get("edge")
        ask = s.get("best_ask")
        if edge is None or ask is None:
            continue
        if edge <= 0 or edge < cfg.min_edge:
            continue
        if ask > cfg.max_ask:
            continue
        q = s.get("q")
        if q is not None and q < cfg.min_q:
            continue
        if q is not None and q > cfg.max_q:
            continue
        spread = s.get("spread")
        if spread is not None and spread > cfg.max_spread:
            continue
        model_spread = s.get("model_spread")
        if model_spread is not None and model_spread < cfg.min_model_spread:
            continue
        if model_spread is not None and model_spread > cfg.max_model_spread:
            continue
        filtered.append(s)
    return filtered[: cfg.top_n]


def evaluate_trade(
    actual: float,
    outcome: str,
    side: str,
    ask: float,
    stake: float,
    unit: str,
    rounding_rule: str,
    cfg: StrategyConfig,
) -> Tuple[int, float]:
    adjusted = adjust_actual(actual, unit, rounding_rule, cfg)
    gap = server.settlement_gap(adjusted, outcome)
    if gap is None:
        return 0, 0.0
    hit = 1 if abs(gap) < 1e-9 else 0
    if side.upper() == "NO":
        hit = 1 - hit
    pnl = (stake * (1.0 - ask)) if hit else (-stake * ask)
    return hit, pnl


def evaluate_prediction(
    actual: float,
    outcome: str,
    unit: str,
    rounding_rule: str,
    cfg: StrategyConfig,
) -> int:
    adjusted = adjust_actual(actual, unit, rounding_rule, cfg)
    gap = server.settlement_gap(adjusted, outcome)
    if gap is None:
        return 0
    return 1 if abs(gap) < 1e-9 else 0


def scenario_weights_for_config(cfg: StrategyConfig, stage: str) -> dict:
    if stage == "early":
        gfs = cfg.early_gfs_share
        icon = 1.0 - gfs
        return {
            "gfs_seamless": gfs,
            "icon_seamless": icon,
            "ecmwf_ifs025": 0.0,
            "nws_forecast": 0.0,
        }
    if stage == "mid":
        ecmwf = cfg.mid_ecmwf_share
        rest = max(0.0, 1.0 - ecmwf)
        return {
            "ecmwf_ifs025": ecmwf,
            "gfs_seamless": rest / 2.0,
            "icon_seamless": rest / 2.0,
            "nws_forecast": 0.0,
        }
    if stage == "late":
        nws = cfg.late_nws_share
        return {
            "nws_forecast": nws,
            "ecmwf_ifs025": max(0.0, 1.0 - nws),
            "gfs_seamless": 0.0,
            "icon_seamless": 0.0,
        }
    if stage == "integer":
        ecmwf = cfg.integer_ecmwf_share
        icon = 1.0 - ecmwf
        return {
            "ecmwf_ifs025": ecmwf,
            "icon_seamless": icon,
            "gfs_seamless": 0.0,
            "nws_forecast": 0.0,
        }
    return {}


def adjust_weights(weights: Dict[str, float], power: float) -> Dict[str, float]:
    if not weights:
        return {}
    scaled = {k: float(v) ** power for k, v in weights.items() if v is not None}
    total = sum(scaled.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in scaled.items()}


def equal_weights(models: Optional[List[str]]) -> Dict[str, float]:
    if not models:
        return {}
    w = 1.0 / len(models)
    return {m: w for m in models}


def normalize_weights(weights: Dict[str, float], models: Optional[List[str]]) -> Dict[str, float]:
    if not weights:
        return {}
    if not models:
        total = sum(weights.values())
        if total <= 0:
            return {}
        return {m: v / total for m, v in weights.items()}
    total = sum(weights.get(m, 0.0) for m in models)
    if total <= 0:
        return {}
    return {m: weights.get(m, 0.0) / total for m in models}


def blend_with_equal(
    weights: Dict[str, float],
    models: Optional[List[str]],
    blend: float,
) -> Dict[str, float]:
    if not models:
        return weights
    blend = max(0.0, min(1.0, blend))
    if not weights:
        return equal_weights(models)
    eq = equal_weights(models)
    return {m: blend * weights.get(m, 0.0) + (1.0 - blend) * eq[m] for m in models}


AUTO_WEIGHT_CACHE: Dict[Tuple[str, int, float], Dict[str, float]] = {}
AUTO_STAGE_WEIGHT_CACHE: Dict[Tuple[str, int, float, str], Dict[str, float]] = {}
AUTO_STAGE_STATS_CACHE: Dict[Tuple[str, int], Dict[str, Dict[str, Dict[str, float]]]] = {}
AUTO_WEIGHT_EVENTS: Optional[List[dict]] = None
AUTO_WEIGHT_EVENT_CACHE: Dict[Tuple[int, str, int, float], Dict[str, float]] = {}
AUTO_STAGE_EVENT_CACHE: Dict[Tuple[int, str, int], Dict[str, Dict[str, Dict[str, float]]]] = {}
AUTO_STAGE_WEIGHT_EVENT_CACHE: Dict[Tuple[int, str, int, float, str], Dict[str, float]] = {}


def compute_auto_weights(
    weight_date: str,
    days: int,
    power: float,
    models: Optional[List[str]] = None,
    alpha: float = 0.0,
    blend: float = 1.0,
    min_samples: int = 0,
) -> Dict[str, float]:
    key = (weight_date, days, power)
    if AUTO_WEIGHT_EVENTS is not None:
        events_key = (id(AUTO_WEIGHT_EVENTS), weight_date, days, power)
        cached = AUTO_WEIGHT_EVENT_CACHE.get(events_key)
        if cached is not None:
            return cached
        weights = compute_auto_weights_from_events(
            weight_date,
            days,
            power,
            AUTO_WEIGHT_EVENTS,
            models=models,
            alpha=alpha,
            blend=blend,
            min_samples=min_samples,
        )
        AUTO_WEIGHT_EVENT_CACHE[events_key] = weights
        return weights
    cached = AUTO_WEIGHT_CACHE.get(key)
    if cached is not None:
        return cached
    weights = server.compute_model_hit_weights(days, end_date=weight_date)
    weights = normalize_weights(weights, models)
    weights = adjust_weights(weights, power)
    weights = blend_with_equal(weights, models, blend)
    AUTO_WEIGHT_CACHE[key] = weights
    return weights


def compute_auto_weights_from_events(
    weight_date: str,
    days: int,
    power: float,
    events: List[dict],
    models: Optional[List[str]] = None,
    alpha: float = 0.0,
    blend: float = 1.0,
    min_samples: int = 0,
) -> Dict[str, float]:
    end_dt = parse_date(weight_date) or dt.date.today()
    start_dt = end_dt - dt.timedelta(days=days)
    latest: Dict[str, dict] = {}
    for ev in events:
        ev_date = parse_date(ev.get("event_date"))
        run_dt = parse_date(ev.get("run_date"))
        if not ev_date or not run_dt:
            continue
        if ev_date < start_dt or ev_date > end_dt:
            continue
        if run_dt > end_dt:
            continue
        key_event = event_key(ev)
        existing = latest.get(key_event)
        if not existing:
            latest[key_event] = ev
        else:
            cur_key = existing.get("created_at") or existing.get("run_date") or ""
            new_key = ev.get("created_at") or ev.get("run_date") or ""
            if new_key > cur_key:
                latest[key_event] = ev
    counts: Dict[str, float] = {}
    hits: Dict[str, float] = {}
    for ev in latest.values():
        rows_all = ev.get("rows_all") or []
        models = ev.get("models") or []
        if not rows_all or not models:
            continue
        for model in models:
            per_rows = build_model_bucket_rows(rows_all, model)
            top_label = server.top_model_bucket(per_rows)
            if not top_label:
                continue
            gap = server.settlement_gap(ev.get("actual"), top_label, ev.get("rounding_rule"))
            if gap is None:
                continue
            counts[model] = counts.get(model, 0.0) + 1.0
            if abs(gap) < 1e-9:
                hits[model] = hits.get(model, 0.0) + 1.0
    total_counts = sum(counts.values())
    if models is None:
        models = list(counts.keys())
    if min_samples and total_counts < min_samples:
        return equal_weights(models)
    raw = {m: float(hits.get(m, 0.0)) + alpha for m in models}
    raw = normalize_weights(raw, models)
    weights = adjust_weights(raw, power)
    weights = blend_with_equal(weights, models, blend)
    return weights


def compute_auto_weights_by_stage(
    weight_date: str,
    days: int,
    power: float,
    stage: str,
    models: Optional[List[str]] = None,
    alpha: float = 0.0,
    blend: float = 1.0,
    min_samples: int = 0,
) -> Dict[str, float]:
    if AUTO_WEIGHT_EVENTS is not None:
        key = (id(AUTO_WEIGHT_EVENTS), weight_date, days, power, stage)
        cached = AUTO_STAGE_WEIGHT_EVENT_CACHE.get(key)
        if cached is not None:
            return cached
        if stage not in {"early", "mid", "late", "integer"}:
            weights = compute_auto_weights(
                weight_date,
                days,
                power,
                models=models,
                alpha=alpha,
                blend=blend,
                min_samples=min_samples,
            )
            AUTO_STAGE_WEIGHT_EVENT_CACHE[key] = weights
            return weights
        events_key = (id(AUTO_WEIGHT_EVENTS), weight_date, days)
        stats = AUTO_STAGE_EVENT_CACHE.get(events_key)
        if stats is None:
            stats = compute_auto_stage_stats_from_events(weight_date, days, AUTO_WEIGHT_EVENTS)
            AUTO_STAGE_EVENT_CACHE[events_key] = stats
        stage_stats = stats.get(stage) or {}
        counts = {model: entry.get("count", 0.0) for model, entry in stage_stats.items()}
        hits = {model: float(entry.get("hit", 0.0)) for model, entry in stage_stats.items()}
        if models is None:
            models = list(counts.keys())
        total_counts = sum(counts.values())
        if min_samples and total_counts < min_samples:
            weights = equal_weights(models)
        else:
            raw = {m: hits.get(m, 0.0) + alpha for m in models}
            raw = normalize_weights(raw, models)
            weights = adjust_weights(raw, power)
            weights = blend_with_equal(weights, models, blend)
        if not weights:
            weights = compute_auto_weights(
                weight_date,
                days,
                power,
                models=models,
                alpha=alpha,
                blend=blend,
                min_samples=min_samples,
            )
        AUTO_STAGE_WEIGHT_EVENT_CACHE[key] = weights
        return weights
    key = (weight_date, days, power, stage)
    cached = AUTO_STAGE_WEIGHT_CACHE.get(key)
    if cached is not None:
        return cached
    if stage not in {"early", "mid", "late", "integer"}:
        weights = compute_auto_weights(
            weight_date,
            days,
            power,
            models=models,
            alpha=alpha,
            blend=blend,
            min_samples=min_samples,
        )
        AUTO_STAGE_WEIGHT_CACHE[key] = weights
        return weights
    stats_key = (weight_date, days)
    stats = AUTO_STAGE_STATS_CACHE.get(stats_key)
    if stats is None:
        end_dt = parse_date(weight_date) or dt.date.today()
        start = (end_dt - dt.timedelta(days=days)).isoformat()
        end = end_dt.isoformat()
        if not DB_PATH.exists():
            return {}
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT *
            FROM event_runs
            WHERE event_date >= ? AND event_date <= ?
              AND run_date <= ?
              AND settlement_actual IS NOT NULL
            ORDER BY created_at ASC
            """,
            (start, end, end),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        latest_by_event: Dict[str, dict] = {}
        for r in rows:
            key_event = event_key(r)
            latest_by_event[key_event] = pick_latest(latest_by_event.get(key_event), r)
        stats = {}
        for r in latest_by_event.values():
            market_var = r.get("market_var") or ""
            if not market_var.startswith("temperature"):
                continue
            payload = json.loads(r.get("payload_json") or "{}")
            if payload.get("index_source"):
                continue
            rows_all = payload.get("rows") or []
            models = payload.get("models") or []
            if not rows_all or not models:
                continue
            labels = [bucket.get("outcome") for bucket in rows_all]
            stage_now = server.scenario_stage(
                r.get("run_date"),
                r.get("event_date"),
                r.get("rounding_rule"),
                r.get("market_unit"),
                labels,
            )
            stage_stats = stats.setdefault(stage_now, {})
            for model in models:
                per_rows = build_model_bucket_rows(rows_all, model)
                top_label = server.top_model_bucket(per_rows)
                if not top_label:
                    continue
                gap = server.settlement_gap(r.get("settlement_actual"), top_label, r.get("rounding_rule"))
                if gap is None:
                    continue
                entry = stage_stats.setdefault(model, {"count": 0.0, "hit": 0.0})
                entry["count"] += 1.0
                if abs(gap) < 1e-9:
                    entry["hit"] += 1.0
        AUTO_STAGE_STATS_CACHE[stats_key] = stats
    stage_stats = stats.get(stage) or {}
    counts = {model: entry.get("count", 0.0) for model, entry in stage_stats.items()}
    hits = {model: float(entry.get("hit", 0.0)) for model, entry in stage_stats.items()}
    if models is None:
        models = list(counts.keys())
    total_counts = sum(counts.values())
    if min_samples and total_counts < min_samples:
        weights = equal_weights(models)
    else:
        raw = {m: hits.get(m, 0.0) + alpha for m in models}
        raw = normalize_weights(raw, models)
        weights = adjust_weights(raw, power)
        weights = blend_with_equal(weights, models, blend)
    if not weights:
        weights = compute_auto_weights(
            weight_date,
            days,
            power,
            models=models,
            alpha=alpha,
            blend=blend,
            min_samples=min_samples,
        )
    AUTO_STAGE_WEIGHT_CACHE[key] = weights
    return weights


def compute_auto_stage_stats_from_events(
    weight_date: str,
    days: int,
    events: List[dict],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    end_dt = parse_date(weight_date) or dt.date.today()
    start_dt = end_dt - dt.timedelta(days=days)
    latest: Dict[str, dict] = {}
    for ev in events:
        ev_date = parse_date(ev.get("event_date"))
        run_dt = parse_date(ev.get("run_date"))
        if not ev_date or not run_dt:
            continue
        if ev_date < start_dt or ev_date > end_dt:
            continue
        if run_dt > end_dt:
            continue
        key_event = event_key(ev)
        existing = latest.get(key_event)
        if not existing:
            latest[key_event] = ev
        else:
            cur_key = existing.get("created_at") or existing.get("run_date") or ""
            new_key = ev.get("created_at") or ev.get("run_date") or ""
            if new_key > cur_key:
                latest[key_event] = ev
    stats: Dict[str, Dict[str, Dict[str, float]]] = {}
    for ev in latest.values():
        rows_all = ev.get("rows_all") or []
        models = ev.get("models") or []
        if not rows_all or not models:
            continue
        labels = [bucket.get("outcome") for bucket in rows_all]
        stage_now = server.scenario_stage(
            ev.get("run_date"),
            ev.get("event_date"),
            ev.get("rounding_rule"),
            ev.get("market_unit"),
            labels,
        )
        stage_stats = stats.setdefault(stage_now, {})
        for model in models:
            per_rows = build_model_bucket_rows(rows_all, model)
            top_label = server.top_model_bucket(per_rows)
            if not top_label:
                continue
            gap = server.settlement_gap(ev.get("actual"), top_label, ev.get("rounding_rule"))
            if gap is None:
                continue
            entry = stage_stats.setdefault(model, {"count": 0.0, "hit": 0.0})
            entry["count"] += 1.0
            if abs(gap) < 1e-9:
                entry["hit"] += 1.0
    return stats


def resolve_weighting_inputs(
    rows: list,
    models: list,
    weighting: str,
    run_date: str,
    event_date: str,
    rounding_rule: str,
    market_unit: str,
    strict_auto: bool,
    cfg: StrategyConfig,
) -> Tuple[Optional[dict], str]:
    weights_override = None
    weight_date = run_date
    if strict_auto:
        rd = parse_date(run_date)
        if rd:
            weight_date = (rd - dt.timedelta(days=1)).isoformat()
    if weighting == "scenario":
        labels = [bucket.get("outcome") for bucket in rows]
        stage = server.scenario_stage(run_date, event_date, rounding_rule, market_unit, labels)
        weights_override = scenario_weights_for_config(cfg, stage)
    if weighting == "auto":
        if (cfg.auto_mode or "global").lower() == "stage":
            labels = [bucket.get("outcome") for bucket in rows]
            stage = server.scenario_stage(run_date, event_date, rounding_rule, market_unit, labels)
            weights_override = compute_auto_weights_by_stage(
                weight_date,
                cfg.auto_days,
                cfg.auto_power,
                stage,
                models=models,
                alpha=cfg.auto_alpha,
                blend=cfg.auto_blend,
                min_samples=cfg.auto_min_samples,
            )
        else:
            weights_override = compute_auto_weights(
                weight_date,
                cfg.auto_days,
                cfg.auto_power,
                models=models,
                alpha=cfg.auto_alpha,
                blend=cfg.auto_blend,
                min_samples=cfg.auto_min_samples,
            )
    return weights_override, weight_date


def recompute_weighting_suggestions(
    rows: list,
    models: list,
    weighting: str,
    run_date: str,
    event_date: str,
    rounding_rule: str,
    market_unit: str,
    strict_auto: bool,
    cfg: StrategyConfig,
) -> List[dict]:
    weights_override, weight_date = resolve_weighting_inputs(
        rows, models, weighting, run_date, event_date, rounding_rule, market_unit, strict_auto, cfg
    )
    _, suggestions = server.recompute_rows_and_suggestions(
        rows,
        models,
        weighting,
        weights_override=weights_override,
        weight_date=weight_date,
    )
    return suggestions


def recompute_weighting_rows(
    rows: list,
    models: list,
    weighting: str,
    run_date: str,
    event_date: str,
    rounding_rule: str,
    market_unit: str,
    strict_auto: bool,
    cfg: StrategyConfig,
) -> List[dict]:
    weights_override, weight_date = resolve_weighting_inputs(
        rows, models, weighting, run_date, event_date, rounding_rule, market_unit, strict_auto, cfg
    )
    rows_all, _ = server.recompute_rows_and_suggestions(
        rows,
        models,
        weighting,
        weights_override=weights_override,
        weight_date=weight_date,
    )
    return rows_all


def choose_label_by_value(rows: list, value: float) -> str:
    best_label = ""
    best_gap = None
    best_key = (float("inf"), float("inf"), "")
    for r in rows:
        label = r.get("outcome") or ""
        if not label:
            continue
        gap = server.settlement_gap(value, label)
        if gap is None:
            continue
        abs_gap = abs(gap)
        key = server._bucket_sort_key(label)
        if best_gap is None or abs_gap < best_gap or (abs_gap == best_gap and key < best_key):
            best_gap = abs_gap
            best_label = label
            best_key = key
    return best_label


def choose_label_by_quantile(rows: list, quantile: float) -> str:
    buckets = []
    total = 0.0
    for r in rows:
        label = r.get("outcome") or ""
        if not label:
            continue
        try:
            q = float(r.get("q_round") or 0.0)
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        total += q
        buckets.append((server._bucket_sort_key(label), label, q))
    if total <= 0 or not buckets:
        return server.top_model_bucket(rows)
    buckets.sort(key=lambda b: b[0])
    target = min(max(quantile, 0.0), 1.0) * total
    acc = 0.0
    for _, label, q in buckets:
        acc += q
        if acc >= target:
            return label
    return buckets[-1][1]


def predict_label_from_rows(rows: list, unit: str, cfg: StrategyConfig) -> str:
    mode = (cfg.predict_mode or "top_q").lower()
    if mode == "mean":
        expected = server.expected_value_from_rows(rows)
        if expected is None:
            return server.top_model_bucket(rows)
        expected = adjust_prediction_value(expected, unit, cfg)
        label = choose_label_by_value(rows, expected)
        return label or server.top_model_bucket(rows)
    if mode == "quantile":
        return choose_label_by_quantile(rows, cfg.predict_quantile)
    return server.top_model_bucket(rows)


def force_predict_label(ev: dict, name: str, cfg: StrategyConfig) -> str:
    if name in WEIGHTINGS and (cfg.predict_mode or "top_q").lower() == "top_q":
        corrected = corrected_top_bucket_from_event(ev, name, cfg)
        if corrected:
            return corrected
    if name in WEIGHTINGS:
        rows_all = recompute_weighting_rows(
            ev["rows_all"],
            ev["models"],
            name,
            ev["run_date"],
            ev["event_date"],
            ev["rounding_rule"],
            ev["market_unit"],
            cfg.strict_auto,
            cfg,
        )
        label = predict_label_from_rows(rows_all, ev["market_unit"], cfg)
    else:
        per_rows = build_model_bucket_rows(ev["rows_all"], name)
        label = predict_label_from_rows(per_rows, ev["market_unit"], cfg)
        if not label:
            rows_all = recompute_weighting_rows(
                ev["rows_all"],
                ev["models"],
                "equal",
                ev["run_date"],
                ev["event_date"],
                ev["rounding_rule"],
                ev["market_unit"],
                cfg.strict_auto,
                cfg,
            )
            label = predict_label_from_rows(rows_all, ev["market_unit"], cfg)
    if not label:
        for r in ev["rows_all"]:
            label = r.get("outcome") or ""
            if label:
                break
    return label


def summarize_stats(stats: dict) -> str:
    trades = stats["trades"]
    wins = stats["wins"]
    pnl = stats["pnl"]
    stake = stats["stake"]
    win_rate = (wins / trades) if trades else 0.0
    roi = (pnl / stake) if stake else 0.0
    avg_edge = (stats["edge_sum"] / trades) if trades else 0.0
    avg_ask = (stats["ask_sum"] / trades) if trades else 0.0
    return f"trades={trades} win={win_rate:.1%} pnl={pnl:.2f} roi={roi:.1%} avg_edge={avg_edge:.3f} avg_ask={avg_ask:.3f}"


def prepare_events(rows: List[dict]) -> List[dict]:
    grouped = group_by_run_date(rows)
    systems = WEIGHTINGS + FOCUS_MODELS
    events = []
    csv_rows = []
    for run_date, daily_events in sorted(grouped.items()):
        for r in daily_events.values():
            market_var = r.get("market_var") or ""
            if not market_var.startswith("temperature"):
                continue
            payload = json.loads(r.get("payload_json") or "{}")
            if payload.get("index_source"):
                continue
            rows_all = payload.get("rows") or []
            models = payload.get("models") or []
            if not rows_all or not models:
                continue
            actual = r.get("settlement_actual")
            if actual is None:
                continue
            try:
                actual_val = float(actual)
            except (TypeError, ValueError):
                continue
            lead = lead_bucket(run_date, r.get("event_date") or "")
            lead_value = lead_days(run_date, r.get("event_date") or "")
            basis = resolve_basis(r)
            rounding_rule = r.get("rounding_rule") or ""
            unit = r.get("market_unit") or ""

            model_suggestions = {}
            for model in FOCUS_MODELS:
                model_suggestions[model] = build_model_suggestions(rows_all, model, models)
            events.append(
                {
                    "run_date": run_date,
                    "event_date": r.get("event_date") or "",
                    "slug": r.get("slug"),
                    "title": r.get("title"),
                    "created_at": r.get("created_at") or "",
                    "lead": lead,
                    "lead_days": lead_value,
                    "basis": basis,
                    "model_count": len(models),
                    "actual": actual_val,
                    "rows_all": rows_all,
                    "models": models,
                    "rounding_rule": rounding_rule,
                    "market_unit": unit,
                    "market_var": market_var,
                    "model_members": payload.get("model_members") or {},
                    "station_id": r.get("station_id") or "",
                    "city": r.get("city") or "",
                    "model_suggestions": model_suggestions,
                }
            )
    return events


def evaluate_events(
    events: List[dict],
    cfg: StrategyConfig,
    systems: List[str],
    min_trades: int,
    csv_path: Optional[str] = None,
    force_predict: bool = False,
    weight_events: Optional[List[dict]] = None,
) -> Dict[str, dict]:
    global AUTO_WEIGHT_EVENTS
    prev_events = AUTO_WEIGHT_EVENTS
    AUTO_WEIGHT_EVENTS = weight_events if weight_events is not None else events
    try:
        stats = {
            name: {
                "trades": 0,
                "wins": 0,
                "pnl": 0.0,
                "stake": 0.0,
                "edge_sum": 0.0,
                "ask_sum": 0.0,
                "lead": {},
                "basis": {},
            }
            for name in systems
        }
        csv_rows = []
        for ev in events:
            if not force_predict:
                if ev["model_count"] < cfg.min_models:
                    continue
                if cfg.max_lead_days is not None:
                    ld = ev.get("lead_days")
                    if ld is None or ld > cfg.max_lead_days:
                        continue
            for name in systems:
                if force_predict:
                    label = force_predict_label(ev, name, cfg)
                    if not label:
                        continue
                    hit = evaluate_prediction(
                        ev["actual"],
                        label,
                        ev["market_unit"],
                        ev["rounding_rule"],
                        cfg,
                    )
                    agg = stats[name]
                    agg["trades"] += 1
                    agg["wins"] += hit
                    agg["lead"].setdefault(ev["lead"], {"trades": 0, "wins": 0})
                    agg["lead"][ev["lead"]]["trades"] += 1
                    agg["lead"][ev["lead"]]["wins"] += hit
                    agg["basis"].setdefault(ev["basis"], {"trades": 0, "wins": 0})
                    agg["basis"][ev["basis"]]["trades"] += 1
                    agg["basis"][ev["basis"]]["wins"] += hit
                    if csv_path:
                        csv_rows.append(
                            {
                                "system": name,
                                "run_date": ev["run_date"],
                                "event_date": ev["event_date"],
                                "slug": ev["slug"],
                                "title": ev["title"],
                                "side": "PRED",
                                "outcome": label,
                                "ask": None,
                                "edge": None,
                                "lead": ev["lead"],
                                "basis": ev["basis"],
                                "hit": hit,
                                "pnl": 0.0,
                            }
                        )
                    continue
                if name in WEIGHTINGS:
                    suggestions = recompute_weighting_suggestions(
                        ev["rows_all"],
                        ev["models"],
                        name,
                        ev["run_date"],
                        ev["event_date"],
                        ev["rounding_rule"],
                        ev["market_unit"],
                        cfg.strict_auto,
                        cfg,
                    )
                else:
                    suggestions = ev["model_suggestions"].get(name) or []
                filtered = filter_suggestions(suggestions, cfg)
                if not filtered:
                    continue
                for s in filtered:
                    hit, pnl = evaluate_trade(
                        ev["actual"],
                        s.get("outcome") or "",
                        s.get("side") or "",
                        float(s.get("best_ask")),
                        cfg.stake,
                        ev["market_unit"],
                        ev["rounding_rule"],
                        cfg,
                    )
                    agg = stats[name]
                    agg["trades"] += 1
                    agg["wins"] += hit
                    agg["pnl"] += pnl
                    agg["stake"] += cfg.stake
                    agg["edge_sum"] += float(s.get("edge") or 0.0)
                    agg["ask_sum"] += float(s.get("best_ask") or 0.0)
                    agg["lead"].setdefault(ev["lead"], {"trades": 0, "wins": 0})
                    agg["lead"][ev["lead"]]["trades"] += 1
                    agg["lead"][ev["lead"]]["wins"] += hit
                    agg["basis"].setdefault(ev["basis"], {"trades": 0, "wins": 0})
                    agg["basis"][ev["basis"]]["trades"] += 1
                    agg["basis"][ev["basis"]]["wins"] += hit
                    if csv_path:
                        csv_rows.append(
                            {
                                "system": name,
                                "run_date": ev["run_date"],
                                "event_date": ev["event_date"],
                                "slug": ev["slug"],
                                "title": ev["title"],
                                "side": s.get("side"),
                                "outcome": s.get("outcome"),
                                "ask": s.get("best_ask"),
                                "edge": s.get("edge"),
                                "lead": ev["lead"],
                                "basis": ev["basis"],
                                "hit": hit,
                                "pnl": pnl,
                            }
                        )

        if csv_path:
            import csv

            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "system",
                        "run_date",
                        "event_date",
                        "slug",
                        "title",
                        "side",
                        "outcome",
                        "ask",
                        "edge",
                        "lead",
                        "basis",
                        "hit",
                        "pnl",
                    ],
                )
                writer.writeheader()
                writer.writerows(csv_rows)
            print(f"\nSaved trades to {csv_path}")
        return stats
    finally:
        AUTO_WEIGHT_EVENTS = prev_events


def iter_param_grid(ranges: Dict[str, List], max_trials: Optional[int], seed: int) -> List[dict]:
    keys = list(ranges.keys())
    values = [ranges[k] for k in keys]
    total = 1
    for group in values:
        total *= max(1, len(group))
    if max_trials is None or total <= max_trials:
        combos = []
        for vals in _product(values):
            combos.append(dict(zip(keys, vals)))
        return combos
    rng = random.Random(seed)
    combos = []
    seen = set()
    while len(combos) < max_trials:
        pick = tuple(rng.choice(group) for group in values)
        if pick in seen:
            continue
        seen.add(pick)
        combos.append(dict(zip(keys, pick)))
    return combos


def _product(values: List[List]) -> Iterable[Tuple]:
    if not values:
        return [tuple()]
    out = [()]
    for group in values:
        out = [prefix + (item,) for prefix in out for item in group]
    return out


def load_search_config(path: str) -> SearchConfig:
    with open(path, "r") as f:
        data = json.load(f)
    ranges = data.get("ranges") or {}
    systems = data.get("systems") or (WEIGHTINGS + FOCUS_MODELS)
    return SearchConfig(
        ranges=ranges,
        systems=systems,
        min_trades=int(data.get("min_trades", 5)),
        max_trials=data.get("max_trials"),
        seed=int(data.get("seed", 7)),
        require_all_systems=bool(data.get("require_all_systems", True)),
        target_hit=float(data["target_hit"]) if data.get("target_hit") is not None else None,
        force_predict=bool(data.get("force_predict", False)),
    )


def strategy_from_params(params: dict, base: Optional[StrategyConfig] = None) -> StrategyConfig:
    base = base or StrategyConfig()
    max_lead_days = params.get("max_lead_days", base.max_lead_days)
    return StrategyConfig(
        min_edge=float(params.get("min_edge", base.min_edge)),
        max_ask=float(params.get("max_ask", base.max_ask)),
        max_q=float(params.get("max_q", base.max_q)),
        min_q=float(params.get("min_q", base.min_q)),
        max_spread=float(params.get("max_spread", base.max_spread)),
        min_model_spread=float(params.get("min_model_spread", base.min_model_spread)),
        max_model_spread=float(params.get("max_model_spread", base.max_model_spread)),
        min_models=int(params.get("min_models", base.min_models)),
        stake=float(params.get("stake", base.stake)),
        top_n=int(params.get("top_n", base.top_n)),
        strict_auto=bool(params.get("strict_auto", base.strict_auto)),
        max_lead_days=int(max_lead_days) if max_lead_days is not None else None,
        temp_offset=float(params.get("temp_offset", base.temp_offset)),
        temp_offset_c=(
            float(params["temp_offset_c"])
            if params.get("temp_offset_c") is not None
            else base.temp_offset_c
        ),
        temp_offset_f=(
            float(params["temp_offset_f"])
            if params.get("temp_offset_f") is not None
            else base.temp_offset_f
        ),
        actual_rounding=str(params.get("actual_rounding", base.actual_rounding)),
        predict_mode=str(params.get("predict_mode", base.predict_mode)),
        predict_quantile=float(params.get("predict_quantile", base.predict_quantile)),
        predict_offset=float(params.get("predict_offset", base.predict_offset)),
        predict_offset_c=(
            float(params["predict_offset_c"])
            if params.get("predict_offset_c") is not None
            else base.predict_offset_c
        ),
        predict_offset_f=(
            float(params["predict_offset_f"])
            if params.get("predict_offset_f") is not None
            else base.predict_offset_f
        ),
        auto_days=int(params.get("auto_days", base.auto_days)),
        auto_power=float(params.get("auto_power", base.auto_power)),
        auto_mode=str(params.get("auto_mode", base.auto_mode)),
        auto_alpha=float(params.get("auto_alpha", base.auto_alpha)),
        auto_blend=float(params.get("auto_blend", base.auto_blend)),
        auto_min_samples=int(params.get("auto_min_samples", base.auto_min_samples)),
        early_gfs_share=float(params.get("early_gfs_share", base.early_gfs_share)),
        mid_ecmwf_share=float(params.get("mid_ecmwf_share", base.mid_ecmwf_share)),
        late_nws_share=float(params.get("late_nws_share", base.late_nws_share)),
        integer_ecmwf_share=float(params.get("integer_ecmwf_share", base.integer_ecmwf_share)),
    )


def find_best_config(events: List[dict], search: SearchConfig) -> Optional[dict]:
    combos = iter_param_grid(search.ranges, search.max_trials, search.seed)
    systems = search.systems
    best_overall = {"rate": -1.0, "trades": 0, "cfg": None, "per_system": None}
    for params in combos:
        cfg = strategy_from_params(params)
        stats = evaluate_events(
            events,
            cfg,
            systems,
            search.min_trades,
            force_predict=search.force_predict,
        )
        rates = []
        trades_sum = 0
        per_system = {}
        valid = True
        for name in systems:
            d = stats[name]
            trades = d["trades"]
            if trades < search.min_trades:
                if search.require_all_systems:
                    valid = False
                    break
                continue
            rate = d["wins"] / trades if trades else 0.0
            trades_sum += trades
            rates.append(rate)
            per_system[name] = {"rate": rate, "trades": trades}
        if not valid or not rates:
            continue
        overall_rate = (
            sum(rates) / len(systems) if search.require_all_systems else (sum(rates) / len(rates))
        )
        if overall_rate > best_overall["rate"] or (
            overall_rate == best_overall["rate"] and trades_sum > best_overall["trades"]
        ):
            best_overall = {
                "rate": overall_rate,
                "trades": trades_sum,
                "cfg": params,
                "per_system": per_system,
            }
            if search.target_hit is not None and overall_rate >= search.target_hit:
                break
    return best_overall if best_overall["cfg"] is not None else None


def load_run_config(path: str) -> dict:
    with open(path, "r") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def optimize(events: List[dict], search: SearchConfig) -> None:
    systems = search.systems
    best_overall = find_best_config(events, search)
    print("\n= Optimization result (hit rate only) =")
    if not best_overall:
        print("No config meets min_trades for required systems.")
        return
    print(
        f"Best unified config avg hit rate: {best_overall['rate']:.1%} "
        f"(total trades={best_overall['trades']}) cfg={best_overall['cfg']}"
    )
    if best_overall["per_system"]:
        print("\nPer-system hit rate:")
        for name in systems:
            d = best_overall["per_system"].get(name)
            if not d:
                print(f"{name:14s} n/a")
                continue
            print(f"{name:14s} hit={d['rate']:.1%} trades={d['trades']}")


def roll_validate(
    events: List[dict],
    search: SearchConfig,
    train_days: int,
    test_days: int,
    step_days: int,
    max_splits: Optional[int],
    csv_path: Optional[str],
) -> None:
    dates = sorted({ev.get("run_date") for ev in events if ev.get("run_date")})
    if len(dates) < train_days + test_days:
        print("Not enough run_date windows for rolling validation.")
        return
    systems = search.systems
    aggregated = {name: {"trades": 0, "wins": 0} for name in systems}
    rows_out = []
    split = 0
    for start_idx in range(0, len(dates) - train_days - test_days + 1, step_days):
        if max_splits and split >= max_splits:
            break
        train_dates = set(dates[start_idx : start_idx + train_days])
        test_dates = set(dates[start_idx + train_days : start_idx + train_days + test_days])
        train_events = [ev for ev in events if ev.get("run_date") in train_dates]
        test_events = [ev for ev in events if ev.get("run_date") in test_dates]
        if not train_events or not test_events:
            continue
        split += 1
        split_search = replace(search, seed=search.seed + split)
        best = find_best_config(train_events, split_search)
        if not best:
            continue
        cfg = strategy_from_params(best["cfg"])
        train_stats = evaluate_events(
            train_events,
            cfg,
            systems,
            0,
            force_predict=search.force_predict,
            weight_events=train_events,
        )
        test_stats = evaluate_events(
            test_events,
            cfg,
            systems,
            0,
            force_predict=search.force_predict,
            weight_events=train_events,
        )
        for name in systems:
            t = test_stats[name]
            aggregated[name]["trades"] += t["trades"]
            aggregated[name]["wins"] += t["wins"]
            if csv_path:
                tr = train_stats[name]
                train_rate = (tr["wins"] / tr["trades"]) if tr["trades"] else 0.0
                test_rate = (t["wins"] / t["trades"]) if t["trades"] else 0.0
                rows_out.append(
                    {
                        "split": split,
                        "train_start": min(train_dates),
                        "train_end": max(train_dates),
                        "test_start": min(test_dates),
                        "test_end": max(test_dates),
                        "system": name,
                        "train_trades": tr["trades"],
                        "train_hit": tr["wins"],
                        "train_rate": f"{train_rate:.1%}",
                        "test_trades": t["trades"],
                        "test_hit": t["wins"],
                        "test_rate": f"{test_rate:.1%}",
                        "params": json.dumps(best["cfg"], ensure_ascii=True),
                    }
                )
    print("\n= Rolling out-of-sample summary (test windows) =")
    for name in systems:
        d = aggregated[name]
        rate = (d["wins"] / d["trades"]) if d["trades"] else 0.0
        print(f"{name:14s} hit={rate:.1%} trades={d['trades']}")
    if csv_path and rows_out:
        import csv

        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "split",
                    "train_start",
                    "train_end",
                    "test_start",
                    "test_end",
                    "system",
                    "train_trades",
                    "train_hit",
                    "train_rate",
                    "test_trades",
                    "test_hit",
                    "test_rate",
                    "params",
                ],
            )
            writer.writeheader()
            writer.writerows(rows_out)
        print(f"\nSaved rolling report to {csv_path}")


def run_backtest(
    rows: List[dict],
    cfg: StrategyConfig,
    csv_path: Optional[str],
    force_predict: bool,
) -> None:
    systems = WEIGHTINGS + FOCUS_MODELS
    events = prepare_events(rows)
    stats = evaluate_events(events, cfg, systems, 0, csv_path=csv_path, force_predict=force_predict)
    print("\n= Backtest summary =")
    for name in systems:
        print(f"{name:14s} {summarize_stats(stats[name])}")

    print("\n= Lead time breakdown =")
    for name in systems:
        rows = stats[name]["lead"]
        if not rows:
            continue
        parts = []
        for bucket in ["D0", "D1", "D2-3", "D4-7", "D8+", "post", "unknown"]:
            if bucket not in rows:
                continue
            d = rows[bucket]
            rate = (d["wins"] / d["trades"]) if d["trades"] else 0.0
            parts.append(f"{bucket}:{rate:.0%}({d['wins']}/{d['trades']})")
        if parts:
            print(f"{name:14s} " + " ".join(parts))

    print("\n= Basis breakdown =")
    for name in systems:
        rows = stats[name]["basis"]
        if not rows:
            continue
        parts = []
        for basis, d in sorted(rows.items()):
            rate = (d["wins"] / d["trades"]) if d["trades"] else 0.0
            parts.append(f"{basis}:{rate:.0%}({d['wins']}/{d['trades']})")
        if parts:
            print(f"{name:14s} " + " ".join(parts))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-start", type=str, default="", help="YYYY-MM-DD (run_date start)")
    ap.add_argument("--run-end", type=str, default="", help="YYYY-MM-DD (run_date end)")
    ap.add_argument("--days", type=int, default=30, help="Fallback run_date window")
    ap.add_argument("--min-edge", type=float, default=0.0, help="Minimum edge (0.02 = 2%)")
    ap.add_argument("--max-ask", type=float, default=0.98, help="Max ask price")
    ap.add_argument("--max-q", type=float, default=1.0, help="Max model prob")
    ap.add_argument("--max-spread", type=float, default=0.10, help="Max market spread")
    ap.add_argument("--min-model-spread", type=float, default=0.0, help="Min model spread")
    ap.add_argument("--min-models", type=int, default=2, help="Min model count")
    ap.add_argument("--stake", type=float, default=10.0, help="Stake per trade")
    ap.add_argument("--top-n", type=int, default=1, help="Top N trades per event")
    ap.add_argument(
        "--strict-auto",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use run_date-1 for auto weights",
    )
    ap.add_argument("--csv", type=str, default="", help="Write trades to CSV")
    ap.add_argument("--optimize", action="store_true", help="Run parameter search")
    ap.add_argument("--config", type=str, default="configs/backtest_optimize.json")
    ap.add_argument("--run-config", type=str, default="", help="Load unified config JSON")
    ap.add_argument("--roll-validate", action="store_true", help="Run rolling out-of-sample validation")
    ap.add_argument("--roll-train-days", type=int, default=3, help="Train window length (run_date days)")
    ap.add_argument("--roll-test-days", type=int, default=1, help="Test window length (run_date days)")
    ap.add_argument("--roll-step-days", type=int, default=1, help="Step between splits (run_date days)")
    ap.add_argument("--roll-max-splits", type=int, default=0, help="Limit number of splits (0=all)")
    ap.add_argument("--roll-max-trials", type=int, default=0, help="Override trials per split (0=use config)")
    ap.add_argument("--roll-csv", type=str, default="", help="Write rolling report CSV")
    ap.add_argument(
        "--force-predict",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Force a prediction per event (ignore filters)",
    )
    args = ap.parse_args()

    run_start = args.run_start
    run_end = args.run_end
    if not run_start or not run_end:
        run_start, run_end = date_range(args.days)

    cfg = StrategyConfig(
        min_edge=args.min_edge,
        max_ask=args.max_ask,
        max_q=args.max_q,
        min_q=0.0,
        max_spread=args.max_spread,
        min_model_spread=args.min_model_spread,
        max_model_spread=1.0,
        min_models=args.min_models,
        stake=args.stake,
        top_n=args.top_n,
        strict_auto=args.strict_auto,
        max_lead_days=None,
        temp_offset=0.0,
        temp_offset_c=None,
        temp_offset_f=None,
        actual_rounding="none",
        predict_mode="top_q",
        predict_quantile=0.5,
        predict_offset=0.0,
        predict_offset_c=None,
        predict_offset_f=None,
        auto_days=7,
        auto_power=1.0,
        auto_mode="global",
        auto_alpha=0.0,
        auto_blend=1.0,
        auto_min_samples=0,
        early_gfs_share=0.5,
        mid_ecmwf_share=0.6,
        late_nws_share=0.6,
        integer_ecmwf_share=0.55,
    )

    force_predict = args.force_predict
    if args.run_config:
        run_cfg = load_run_config(args.run_config)
        params = run_cfg.get("params") if isinstance(run_cfg.get("params"), dict) else run_cfg
        if isinstance(params, dict):
            cfg = strategy_from_params(params, base=cfg)
        if "force_predict" in run_cfg:
            force_predict = bool(run_cfg.get("force_predict"))

    rows = load_runs(run_start, run_end)
    if not rows:
        print("No runs found for the given run_date window.")
        return
    print(f"Run window: {run_start} -> {run_end} | rows={len(rows)}")
    if args.roll_validate:
        search = load_search_config(args.config)
        if args.roll_max_trials:
            search = replace(search, max_trials=args.roll_max_trials)
        events = prepare_events(rows)
        roll_validate(
            events,
            search,
            args.roll_train_days,
            args.roll_test_days,
            args.roll_step_days,
            args.roll_max_splits or None,
            args.roll_csv or None,
        )
    elif args.optimize:
        search = load_search_config(args.config)
        events = prepare_events(rows)
        optimize(events, search)
    else:
        run_backtest(rows, cfg, args.csv or None, force_predict)


if __name__ == "__main__":
    main()
