#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import uuid
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_BOOK = DATA_DIR / "ab_entry_forward_book.jsonl"
DEFAULT_SUMMARY = DATA_DIR / "ab_entry_forward_summary.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.backtest as backtest


BASELINES: Dict[str, Dict[str, Any]] = {
    "baseline_default": {
        "name": "默认",
        "params": {
            "min_edge": 0.02,
            "max_ask": 0.80,
            "min_q": 0.20,
            "max_q": 0.95,
            "max_spread": 0.08,
            "min_models": 2,
            "top_n": 1,
            "stake": 10.0,
        },
    },
    "baseline_conservative": {
        "name": "保守型",
        "params": {
            "min_edge": 0.03,
            "max_ask": 0.65,
            "min_q": 0.30,
            "max_q": 0.90,
            "max_spread": 0.06,
            "min_models": 3,
            "top_n": 1,
            "stake": 10.0,
        },
    },
    "baseline_aggressive": {
        "name": "进攻型",
        "params": {
            "min_edge": 0.015,
            "max_ask": 0.90,
            "min_q": 0.10,
            "max_q": 0.98,
            "max_spread": 0.12,
            "min_models": 2,
            "top_n": 1,
            "stake": 10.0,
        },
    },
}

EXIT_DEFAULTS: Dict[str, Any] = {
    "exit_stop_loss_pct": 0.2,
    "exit_trail_pct": 0.01,
    "exit_lag_max": 0.8,
    "exit_lag_consecutive": 2,
    "exit_edge_flip_min": 0.005,
    "exit_trend_min_move": 0.005,
    "exit_trend_consist": 0.67,
    "exit_stop_loss_enabled": True,
    "exit_trail_enabled": True,
    "exit_trend_enabled": False,
    "exit_lag_enabled": True,
    "exit_edge_flip_enabled": True,
}


def parse_date(value: str) -> Optional[dt.date]:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def parse_dt(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        val = float(value)
        if not (val == val):
            return None
        return val
    except (TypeError, ValueError):
        return None


def load_search_config(path: Path) -> backtest.SearchConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return backtest.SearchConfig(
        ranges=raw.get("ranges") or {},
        systems=raw.get("systems") or list(backtest.WEIGHTINGS),
        min_trades=int(raw.get("min_trades", 10)),
        max_trials=raw.get("max_trials"),
        seed=int(raw.get("seed", 17)),
        require_all_systems=bool(raw.get("require_all_systems", True)),
        target_hit=float(raw["target_hit"]) if raw.get("target_hit") is not None else None,
        force_predict=bool(raw.get("force_predict", False)),
    )


def load_run_rows(run_date: str) -> List[dict]:
    if not backtest.DB_PATH.exists():
        return []
    conn = sqlite3.connect(backtest.DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM event_runs
        WHERE run_date = ?
        ORDER BY created_at ASC
        """,
        (run_date,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def pick_latest(existing: Optional[dict], row: dict) -> dict:
    return backtest.pick_latest(existing, row)


def group_latest_rows(rows: Iterable[dict]) -> List[dict]:
    grouped: Dict[str, dict] = {}
    for r in rows:
        key = backtest.event_key(r)
        if not key:
            continue
        grouped[key] = pick_latest(grouped.get(key), r)
    return list(grouped.values())


def build_model_suggestions(rows_all: list, models: list) -> Dict[str, List[dict]]:
    suggestions = {}
    for model in models:
        suggestions[model] = backtest.build_model_suggestions(rows_all, model, models)
    return suggestions


def prepare_live_events(rows: List[dict]) -> List[dict]:
    events = []
    for r in rows:
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
        model_suggestions = build_model_suggestions(rows_all, models)
        events.append(
            {
                "run_date": r.get("run_date") or "",
                "event_date": r.get("event_date") or "",
                "slug": r.get("slug"),
                "title": r.get("title"),
                "created_at": r.get("created_at") or "",
                "lead": backtest.lead_bucket(r.get("run_date") or "", r.get("event_date") or ""),
                "lead_days": backtest.lead_days(r.get("run_date") or "", r.get("event_date") or ""),
                "basis": backtest.resolve_basis(r),
                "model_count": len(models),
                "rows_all": rows_all,
                "models": models,
                "rounding_rule": r.get("rounding_rule") or "",
                "market_unit": r.get("market_unit") or "",
                "market_var": market_var,
                "model_members": payload.get("model_members") or {},
                "station_id": r.get("station_id") or "",
                "city": r.get("city") or "",
                "model_suggestions": model_suggestions,
            }
        )
    return events


def normalize_exit_params(params: Dict[str, Any]) -> Dict[str, Any]:
    stop_loss = to_float(params.get("exit_stop_loss_pct", 0.2))
    if stop_loss is not None and stop_loss > 1:
        stop_loss = stop_loss / 100.0
    trail = to_float(params.get("exit_trail_pct", 0.01))
    if trail is not None and trail > 1:
        trail = trail / 100.0
    lag_max = to_float(params.get("exit_lag_max", 0.8))
    lag_consec = params.get("exit_lag_consecutive", 2)
    try:
        lag_consec = max(1, int(lag_consec))
    except (TypeError, ValueError):
        lag_consec = 2
    edge_flip_min = to_float(params.get("exit_edge_flip_min", 0.005))
    trend_min = to_float(params.get("exit_trend_min_move", 0.005))
    trend_consist = to_float(params.get("exit_trend_consist", 0.67))
    out = {
        "exit_stop_loss_pct": stop_loss,
        "exit_trail_pct": trail,
        "exit_lag_max": lag_max,
        "exit_lag_consecutive": lag_consec,
        "exit_edge_flip_min": edge_flip_min,
        "exit_trend_min_move": trend_min,
        "exit_trend_consist": trend_consist,
        "exit_stop_loss_enabled": bool(params.get("exit_stop_loss_enabled", True)),
        "exit_trail_enabled": bool(params.get("exit_trail_enabled", True)),
        "exit_trend_enabled": bool(params.get("exit_trend_enabled", False)),
        "exit_lag_enabled": bool(params.get("exit_lag_enabled", True)),
        "exit_edge_flip_enabled": bool(params.get("exit_edge_flip_enabled", True)),
    }
    return out


def merge_params(params: Dict[str, Any]) -> Dict[str, Any]:
    merged = {**EXIT_DEFAULTS}
    if params:
        merged.update(params)
    return merged


def load_event_snapshots(
    conn: sqlite3.Connection, slug: str, event_date: str, start_at: Optional[str] = None
) -> List[Tuple[dt.datetime, Dict[str, dict]]]:
    cur = conn.cursor()
    if start_at:
        cur.execute(
            """
            SELECT created_at, payload_json
            FROM event_runs
            WHERE slug = ? AND event_date = ? AND created_at >= ?
            ORDER BY created_at ASC
            """,
            (slug, event_date, start_at),
        )
    else:
        cur.execute(
            """
            SELECT created_at, payload_json
            FROM event_runs
            WHERE slug = ? AND event_date = ?
            ORDER BY created_at ASC
            """,
            (slug, event_date),
        )
    snapshots: List[Tuple[dt.datetime, Dict[str, dict]]] = []
    for row in cur.fetchall():
        created_at = row[0]
        ts = parse_dt(created_at)
        if ts is None:
            continue
        payload = json.loads(row[1] or "{}")
        rows = payload.get("rows") or []
        rows_by_outcome = {r.get("outcome"): r for r in rows if r.get("outcome")}
        snapshots.append((ts, rows_by_outcome))
    return snapshots


def exit_price_from_row(row: dict, side: str) -> Optional[float]:
    if side.upper() == "YES":
        return to_float(row.get("best_bid")) or to_float(row.get("best_ask"))
    return to_float(row.get("best_bid_no")) or to_float(row.get("best_ask_no"))


def simulate_exit(
    trade: dict,
    snapshots: List[Tuple[dt.datetime, Dict[str, dict]]],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = normalize_exit_params(params)
    side = str(trade.get("side") or "").upper()
    outcome = trade.get("outcome") or ""
    entry_price = to_float(trade.get("entry_price")) or to_float(trade.get("ask"))
    if entry_price is None or entry_price <= 0:
        return {}
    entry_at = parse_dt(trade.get("entry_at") or trade.get("created_at") or "")
    peak_price = to_float(trade.get("peak_price")) or entry_price
    lag_count = int(trade.get("lag_count") or 0)
    stop_loss_pct = cfg["exit_stop_loss_pct"]
    trail_pct = cfg["exit_trail_pct"]
    lag_max = cfg["exit_lag_max"]
    lag_consec = cfg["exit_lag_consecutive"]
    edge_flip_min = cfg["exit_edge_flip_min"]
    trend_min = cfg["exit_trend_min_move"]
    trend_consist = cfg["exit_trend_consist"]
    for ts, rows_by_outcome in snapshots:
        if entry_at and ts < entry_at:
            continue
        row = rows_by_outcome.get(outcome)
        if not row:
            continue
        exit_price = exit_price_from_row(row, side)
        if exit_price is None:
            continue
        if exit_price > peak_price:
            peak_price = exit_price
        triggers: List[str] = []
        if cfg["exit_stop_loss_enabled"] and stop_loss_pct is not None and stop_loss_pct > 0:
            if exit_price <= entry_price * (1.0 - stop_loss_pct):
                triggers.append("stop_loss")
        if cfg["exit_trail_enabled"] and trail_pct is not None and trail_pct > 0:
            if peak_price >= entry_price * (1.0 + trail_pct) and exit_price <= peak_price * (1.0 - trail_pct):
                triggers.append("trail_stop")
        if cfg["exit_trend_enabled"] and trend_min is not None and trend_consist is not None:
            if side == "YES":
                trend_pct = to_float(row.get("trend_exit_pct_yes"))
                trend_cons = to_float(row.get("trend_exit_consistency_yes"))
            else:
                trend_pct = to_float(row.get("trend_exit_pct_no"))
                trend_cons = to_float(row.get("trend_exit_consistency_no"))
            if trend_pct is not None and trend_cons is not None:
                if trend_pct >= trend_min and trend_cons >= trend_consist:
                    triggers.append("trend_down")
        if cfg["exit_lag_enabled"] and lag_max is not None:
            lag_val = to_float(row.get("lag_yes") if side == "YES" else row.get("lag_no"))
            if lag_val is not None and lag_val >= lag_max:
                lag_count += 1
            else:
                lag_count = 0
            if lag_count >= lag_consec:
                triggers.append("lag")
        if cfg["exit_edge_flip_enabled"] and edge_flip_min is not None:
            if side == "YES":
                prev_edge_min = to_float(row.get("prev_edge_min_yes"))
                signal_edge_min = to_float(row.get("signal_edge_min"))
            else:
                prev_edge_min = to_float(row.get("prev_edge_min_no"))
                signal_edge_min = to_float(row.get("signal_edge_min_no"))
            if (
                prev_edge_min is not None
                and signal_edge_min is not None
                and prev_edge_min > edge_flip_min
                and signal_edge_min < -edge_flip_min
            ):
                triggers.append("edge_flip")
        if triggers:
            return {
                "exit_at": ts.isoformat(timespec="seconds"),
                "exit_price": exit_price,
                "exit_reasons": triggers,
                "peak_price": peak_price,
                "lag_count": lag_count,
            }
    return {"peak_price": peak_price, "lag_count": lag_count}


def find_best_config_by_roi(
    events: List[dict],
    search: backtest.SearchConfig,
    require_positive: bool = True,
) -> Optional[dict]:
    combos = backtest.iter_param_grid(search.ranges, search.max_trials, search.seed)
    systems = search.systems
    best = {"roi": -1e9, "trades": 0, "cfg": None}
    snapshot_cache = build_snapshot_cache(events)
    for params in combos:
        cfg = backtest.strategy_from_params(params)
        stats = evaluate_events_with_exit(
            events,
            cfg,
            systems,
            search.min_trades,
            params,
            snapshot_cache,
        )
        total_trades = 0
        total_pnl = 0.0
        total_stake = 0.0
        valid = False
        for name in systems:
            d = stats[name]
            trades = int(d.get("trades") or 0)
            if trades < search.min_trades:
                if search.require_all_systems:
                    valid = False
                    break
                continue
            valid = True
            total_trades += trades
            total_pnl += float(d.get("pnl") or 0.0)
            total_stake += float(d.get("stake") or 0.0)
        if not valid or total_trades <= 0 or total_stake <= 0:
            continue
        roi = total_pnl / total_stake
        if require_positive and roi <= 0:
            continue
        if roi > best["roi"] or (roi == best["roi"] and total_trades > best["trades"]):
            best = {"roi": roi, "trades": total_trades, "cfg": params}
    return best if best["cfg"] is not None else None


def location_key(ev: dict) -> str:
    station = str(ev.get("station_id") or "").strip()
    if station:
        return f"station:{station}"
    city = str(ev.get("city") or "").strip().lower()
    if city:
        return f"city:{city}"
    slug = str(ev.get("slug") or ev.get("title") or "").strip()
    return f"slug:{slug}"


def summarize_stats(stats: Dict[str, dict], systems: List[str]) -> Dict[str, Any]:
    totals = {
        "trades": 0,
        "wins": 0,
        "pnl": 0.0,
        "stake": 0.0,
        "edge_sum": 0.0,
        "ask_sum": 0.0,
    }
    per_system = {}
    for name in systems:
        s = stats.get(name) or {}
        trades = int(s.get("trades") or 0)
        wins = int(s.get("wins") or 0)
        pnl = float(s.get("pnl") or 0.0)
        stake = float(s.get("stake") or 0.0)
        edge_sum = float(s.get("edge_sum") or 0.0)
        ask_sum = float(s.get("ask_sum") or 0.0)
        rate = (wins / trades) if trades else 0.0
        per_system[name] = {
            "trades": trades,
            "wins": wins,
            "win_rate": rate,
            "pnl": pnl,
            "roi": (pnl / stake) if stake else 0.0,
            "avg_edge": (edge_sum / trades) if trades else 0.0,
            "avg_ask": (ask_sum / trades) if trades else 0.0,
        }
        totals["trades"] += trades
        totals["wins"] += wins
        totals["pnl"] += pnl
        totals["stake"] += stake
        totals["edge_sum"] += edge_sum
        totals["ask_sum"] += ask_sum
    trades = totals["trades"]
    summary = {
        "trades": trades,
        "wins": totals["wins"],
        "win_rate": (totals["wins"] / trades) if trades else 0.0,
        "pnl": totals["pnl"],
        "roi": (totals["pnl"] / totals["stake"]) if totals["stake"] else 0.0,
        "avg_edge": (totals["edge_sum"] / trades) if trades else 0.0,
        "avg_ask": (totals["ask_sum"] / trades) if trades else 0.0,
    }
    return {"summary": summary, "per_system": per_system}


def load_book(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def write_book(path: Path, records: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=True) for item in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def fetch_settlement_actual(conn: sqlite3.Connection, slug: str, event_date: str) -> Optional[float]:
    if not slug or not event_date:
        return None
    cur = conn.cursor()
    cur.execute(
        """
        SELECT settlement_actual
        FROM event_daily_temps
        WHERE slug = ? AND event_date = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (slug, event_date),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None
    cur.execute(
        """
        SELECT settlement_actual
        FROM event_runs
        WHERE slug = ? AND event_date = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (slug, event_date),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None
    return None


def update_settlements(records: List[dict]) -> None:
    if not backtest.DB_PATH.exists():
        return
    conn = sqlite3.connect(backtest.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pending = [r for r in records if r.get("hit") is None or r.get("exit_at")]
        keys = {(r.get("slug") or "", r.get("event_date") or "") for r in pending}
        snapshot_cache: Dict[Tuple[str, str], List[Tuple[dt.datetime, Dict[str, dict]]]] = {}
        for slug, event_date in keys:
            if not slug or not event_date:
                continue
            earliest = None
            for r in pending:
                if r.get("slug") == slug and r.get("event_date") == event_date:
                    ts = r.get("entry_at") or r.get("created_at") or ""
                    if ts and (earliest is None or ts < earliest):
                        earliest = ts
            snapshot_cache[(slug, event_date)] = load_event_snapshots(conn, slug, event_date, earliest)

        for item in records:
            if item.get("hit") is not None:
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            cfg = backtest.strategy_from_params(params)
            slug = item.get("slug") or ""
            event_date = item.get("event_date") or ""
            snapshots = snapshot_cache.get((slug, event_date), [])
            if item.get("exit_at") is None:
                exit_info = simulate_exit(item, snapshots, params)
                if exit_info.get("peak_price") is not None:
                    item["peak_price"] = exit_info.get("peak_price")
                if exit_info.get("lag_count") is not None:
                    item["lag_count"] = exit_info.get("lag_count")
                exit_price = to_float(exit_info.get("exit_price")) if exit_info else None
                if exit_price is not None:
                    entry_price = to_float(item.get("entry_price")) or to_float(item.get("ask"))
                    if entry_price and entry_price > 0:
                        pnl = cfg.stake * (exit_price / entry_price - 1.0)
                        item["exit_price"] = exit_price
                        item["exit_at"] = exit_info.get("exit_at")
                        item["exit_reasons"] = exit_info.get("exit_reasons") or []
                        item["pnl"] = pnl
                        item["hit"] = 1 if pnl > 0 else 0
                        item["settled_at"] = dt.datetime.now().isoformat(timespec="seconds")
                        continue
            actual = fetch_settlement_actual(conn, slug, event_date)
            if actual is None:
                continue
            side = item.get("side") or ""
            outcome = item.get("outcome") or ""
            ask = item.get("ask")
            if ask is None:
                continue
            ask_val = to_float(ask)
            if ask_val is None:
                continue
            hit, pnl = backtest.evaluate_trade(
                actual,
                outcome,
                side,
                ask_val,
                cfg.stake,
                item.get("market_unit") or "",
                item.get("rounding_rule") or "",
                cfg,
            )
            item["settlement_actual"] = actual
            item["hit"] = hit
            item["pnl"] = pnl
            item["settled_at"] = dt.datetime.now().isoformat(timespec="seconds")
    finally:
        conn.close()


def compute_scheme_stats(records: List[dict], systems: List[str]) -> Dict[str, dict]:
    stats = {
        name: {"trades": 0, "wins": 0, "pnl": 0.0, "stake": 0.0, "edge_sum": 0.0, "ask_sum": 0.0}
        for name in systems
    }
    for item in records:
        if item.get("hit") is None:
            continue
        system = item.get("system") or ""
        if system not in stats:
            continue
        edge = item.get("edge") or 0.0
        ask = item.get("ask") or 0.0
        try:
            hit = int(item.get("hit"))
        except (TypeError, ValueError):
            hit = 0
        try:
            pnl = float(item.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        stake = float(item.get("stake") or 0.0)
        entry = stats[system]
        entry["trades"] += 1
        entry["wins"] += hit
        entry["pnl"] += pnl
        entry["stake"] += stake
        entry["edge_sum"] += float(edge or 0.0)
        entry["ask_sum"] += float(ask or 0.0)
    return stats


def build_snapshot_cache(events: List[dict]) -> Dict[Tuple[str, str], List[Tuple[dt.datetime, Dict[str, dict]]]]:
    cache: Dict[Tuple[str, str], List[Tuple[dt.datetime, Dict[str, dict]]]] = {}
    if not backtest.DB_PATH.exists():
        return cache
    conn = sqlite3.connect(backtest.DB_PATH)
    try:
        grouped: Dict[Tuple[str, str], List[str]] = {}
        for ev in events:
            slug = ev.get("slug") or ""
            event_date = ev.get("event_date") or ""
            if not slug or not event_date:
                continue
            grouped.setdefault((slug, event_date), [])
            created_at = ev.get("created_at") or ""
            if created_at:
                grouped[(slug, event_date)].append(created_at)
        for (slug, event_date), starts in grouped.items():
            start_at = min(starts) if starts else None
            cache[(slug, event_date)] = load_event_snapshots(conn, slug, event_date, start_at)
    finally:
        conn.close()
    return cache


def evaluate_events_with_exit(
    events: List[dict],
    cfg: backtest.StrategyConfig,
    systems: List[str],
    min_trades: int,
    params: Dict[str, Any],
    snapshot_cache: Dict[Tuple[str, str], List[Tuple[dt.datetime, Dict[str, dict]]]],
) -> Dict[str, dict]:
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
    for ev in events:
        actual = ev.get("actual")
        if actual is None:
            continue
        entry_at = ev.get("created_at") or ""
        slug = ev.get("slug") or ""
        event_date = ev.get("event_date") or ""
        snapshots = snapshot_cache.get((slug, event_date), [])
        for name in systems:
            if name in backtest.WEIGHTINGS:
                suggestions = backtest.recompute_weighting_suggestions(
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
                suggestions = ev.get("model_suggestions", {}).get(name) or []
            filtered = backtest.filter_suggestions(suggestions, cfg)
            if not filtered:
                continue
            for s in filtered:
                entry_price = to_float(s.get("best_ask"))
                if entry_price is None or entry_price <= 0:
                    continue
                trade = {
                    "side": s.get("side"),
                    "outcome": s.get("outcome"),
                    "entry_price": entry_price,
                    "entry_at": entry_at,
                    "ask": entry_price,
                    "created_at": entry_at,
                }
                exit_info = simulate_exit(trade, snapshots, params)
                exit_price = to_float(exit_info.get("exit_price")) if exit_info else None
                if exit_price is not None:
                    pnl = cfg.stake * (exit_price / entry_price - 1.0)
                    hit = 1 if pnl > 0 else 0
                else:
                    hit, pnl = backtest.evaluate_trade(
                        float(actual),
                        s.get("outcome") or "",
                        s.get("side") or "",
                        entry_price,
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
                agg["ask_sum"] += entry_price
                agg["lead"].setdefault(ev["lead"], {"trades": 0, "wins": 0})
                agg["lead"][ev["lead"]]["trades"] += 1
                agg["lead"][ev["lead"]]["wins"] += hit
                agg["basis"].setdefault(ev["basis"], {"trades": 0, "wins": 0})
                agg["basis"][ev["basis"]]["trades"] += 1
                agg["basis"][ev["basis"]]["wins"] += hit
    return stats


def generate_trades(
    events: List[dict],
    cfg: backtest.StrategyConfig,
    systems: List[str],
    scheme_id: str,
    scheme_name: str,
    scheme_type: str,
    params: Dict[str, Any],
) -> List[dict]:
    per_system: Dict[str, List[dict]] = {name: [] for name in systems}
    for ev in events:
        for name in systems:
            if name in backtest.WEIGHTINGS:
                suggestions = backtest.recompute_weighting_suggestions(
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
                suggestions = ev.get("model_suggestions", {}).get(name) or []
            filtered = backtest.filter_suggestions(suggestions, cfg)
            if not filtered:
                continue
            for s in filtered:
                per_system[name].append(
                    {
                        "scheme_id": scheme_id,
                        "scheme_name": scheme_name,
                        "scheme_type": scheme_type,
                        "system": name,
                        "run_date": ev.get("run_date") or "",
                        "event_date": ev.get("event_date") or "",
                        "slug": ev.get("slug") or "",
                        "title": ev.get("title") or "",
                        "side": s.get("side"),
                        "outcome": s.get("outcome"),
                        "ask": s.get("best_ask"),
                        "entry_price": s.get("best_ask"),
                        "edge": s.get("edge"),
                        "q": s.get("q"),
                        "spread": s.get("spread"),
                        "model_spread": s.get("model_spread"),
                        "stake": cfg.stake,
                        "params": params,
                        "rounding_rule": ev.get("rounding_rule") or "",
                        "market_unit": ev.get("market_unit") or "",
                        "station_id": ev.get("station_id") or "",
                        "city": ev.get("city") or "",
                        "location_key": location_key(ev),
                        "entry_at": ev.get("created_at") or "",
                    }
                )
    selected: List[dict] = []
    for name, items in per_system.items():
        best_by_location: Dict[str, dict] = {}
        for item in items:
            loc = item.get("location_key") or ""
            edge = item.get("edge")
            try:
                edge_val = float(edge) if edge is not None else float("-inf")
            except (TypeError, ValueError):
                edge_val = float("-inf")
            existing = best_by_location.get(loc)
            if existing is None:
                best_by_location[loc] = item
                continue
            existing_edge = existing.get("edge")
            try:
                existing_val = float(existing_edge) if existing_edge is not None else float("-inf")
            except (TypeError, ValueError):
                existing_val = float("-inf")
            if edge_val > existing_val:
                best_by_location[loc] = item
        selected.extend(best_by_location.values())
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward (future) paper trading with rolling tuning.")
    parser.add_argument("--run-date", default="", help="Run date (YYYY-MM-DD). Default: today.")
    parser.add_argument("--lookback-days", type=int, default=7, help="Lookback days for training data.")
    parser.add_argument("--config", default=str(ROOT / "configs" / "ab_entry_search.json"))
    parser.add_argument("--book", default=str(DEFAULT_BOOK))
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY))
    args = parser.parse_args()

    today = dt.date.today()
    run_date = parse_date(args.run_date) or today
    run_date_str = run_date.isoformat()
    train_end = run_date - dt.timedelta(days=1)
    lookback_days = max(1, int(args.lookback_days))
    train_start = train_end - dt.timedelta(days=lookback_days - 1)

    search_cfg = load_search_config(Path(args.config))
    settled_rows = backtest.load_runs(train_start.isoformat(), train_end.isoformat())
    train_events = backtest.prepare_events(settled_rows)
    tuned_params: Dict[str, Any] = {}
    tuned_active = False
    tuned_note = ""
    if train_events:
        best = find_best_config_by_roi(train_events, search_cfg, require_positive=True)
        if best and best.get("cfg"):
            tuned_params = best["cfg"]
            tuned_active = True
        else:
            tuned_note = "no_positive_roi"
    else:
        tuned_note = "no_training_data"

    live_rows = group_latest_rows(load_run_rows(run_date_str))
    live_events = prepare_live_events(live_rows)

    systems = list(search_cfg.systems)
    schemes: List[Dict[str, Any]] = []
    for key, entry in BASELINES.items():
        base_params = merge_params(entry["params"] if isinstance(entry.get("params"), dict) else {})
        schemes.append(
            {
                "id": key,
                "name": entry["name"],
                "type": "baseline",
                "params": base_params,
            }
        )
    schemes.append(
        {
            "id": "tuned_roll",
            "name": "滚动调参",
            "type": "rolling",
            "params": merge_params(tuned_params),
            "inactive": not tuned_active,
            "note": tuned_note,
        }
    )

    book_path = Path(args.book)
    existing = load_book(book_path)
    existing_keys = {str(item.get("key")) for item in existing if item.get("key")}

    new_records: List[dict] = []
    for scheme in schemes:
        if scheme.get("inactive"):
            continue
        params = scheme.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        cfg = backtest.strategy_from_params(params)
        trades = generate_trades(
            live_events,
            cfg,
            systems,
            scheme.get("id") or "",
            scheme.get("name") or "",
            scheme.get("type") or "",
            params,
        )
        for trade in trades:
            key = "|".join(
                [
                    str(trade.get("scheme_id") or ""),
                    str(trade.get("system") or ""),
                    str(trade.get("run_date") or ""),
                    str(trade.get("slug") or trade.get("title") or ""),
                    str(trade.get("event_date") or ""),
                    str(trade.get("side") or ""),
                    str(trade.get("outcome") or ""),
                ]
            )
            if key in existing_keys:
                continue
            trade["key"] = key
            trade["id"] = uuid.uuid4().hex
            trade["created_at"] = dt.datetime.now().isoformat(timespec="seconds")
            trade["settlement_actual"] = None
            trade["hit"] = None
            trade["pnl"] = None
            trade["settled_at"] = None
            existing_keys.add(key)
            new_records.append(trade)

    all_records = existing + new_records
    update_settlements(all_records)
    write_book(book_path, all_records)

    summary_schemes: List[Dict[str, Any]] = []
    total_pending = 0
    for scheme in schemes:
        sid = scheme.get("id")
        scheme_records = [r for r in all_records if r.get("scheme_id") == sid]
        pending = sum(1 for r in scheme_records if r.get("hit") is None)
        total_pending += pending
        stats = compute_scheme_stats(scheme_records, systems)
        summary = summarize_stats(stats, systems)
        summary_schemes.append(
            {
                "id": sid,
                "name": scheme.get("name"),
                "type": scheme.get("type"),
                "params": scheme.get("params"),
                "inactive": scheme.get("inactive") is True,
                "note": scheme.get("note") or "",
                "pending": pending,
                **summary,
            }
        )

    summary_payload = {
        "ok": True,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "run_date": run_date_str,
        "train_window": {
            "start": train_start.isoformat(),
            "end": train_end.isoformat(),
            "lookback_days": lookback_days,
        },
        "systems": systems,
        "min_trades": search_cfg.min_trades,
        "require_positive_roi": True,
        "ab_pair": {"A": "baseline_default", "B": "tuned_roll"},
        "schemes": summary_schemes,
        "pending": total_pending,
    }
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary_payload, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"Wrote {summary_path}")
    if new_records:
        print(f"Added {len(new_records)} new paper trades for {run_date_str}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
