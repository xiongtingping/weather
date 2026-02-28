#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_OUTPUT = DATA_DIR / "ab_entry_results.json"

import scripts.backtest as backtest


def parse_date(value: str) -> Optional[dt.date]:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


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


def build_baselines() -> Dict[str, Dict[str, Any]]:
    return {
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


def load_search_config(path: Path) -> backtest.SearchConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return backtest.SearchConfig(
        ranges=raw.get("ranges") or {},
        systems=raw.get("systems") or (backtest.WEIGHTINGS),
        min_trades=int(raw.get("min_trades", 10)),
        max_trials=raw.get("max_trials"),
        seed=int(raw.get("seed", 17)),
        require_all_systems=bool(raw.get("require_all_systems", True)),
        target_hit=float(raw["target_hit"]) if raw.get("target_hit") is not None else None,
        force_predict=bool(raw.get("force_predict", False)),
    )


def find_best_config_by_roi(
    events: List[dict],
    search: backtest.SearchConfig,
    require_positive: bool = True,
) -> Optional[dict]:
    combos = backtest.iter_param_grid(search.ranges, search.max_trials, search.seed)
    systems = search.systems
    best = {"roi": -1e9, "trades": 0, "cfg": None}
    for params in combos:
        cfg = backtest.strategy_from_params(params)
        stats = backtest.evaluate_events(
            events,
            cfg,
            systems,
            search.min_trades,
            force_predict=search.force_predict,
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


def roll_tune(
    events: List[dict],
    search: backtest.SearchConfig,
    train_days: int,
    test_days: int,
    step_days: int,
    max_splits: Optional[int],
) -> Tuple[Dict[str, dict], List[Dict[str, Any]], Optional[dict]]:
    dates = sorted({ev.get("run_date") for ev in events if ev.get("run_date")})
    systems = search.systems
    totals = {name: {"trades": 0, "wins": 0, "pnl": 0.0, "stake": 0.0, "edge_sum": 0.0, "ask_sum": 0.0} for name in systems}
    splits: List[Dict[str, Any]] = []
    last_params = None
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
        split_search = backtest.replace(search, seed=search.seed + split)
        best = find_best_config_by_roi(train_events, split_search, require_positive=True)
        if not best:
            continue
        last_params = best.get("cfg")
        cfg = backtest.strategy_from_params(best["cfg"])
        test_stats = backtest.evaluate_events(
            test_events,
            cfg,
            systems,
            0,
            force_predict=search.force_predict,
            weight_events=train_events,
        )
        split_summary = summarize_stats(test_stats, systems)
        splits.append(
            {
                "split": split,
                "train_start": min(train_dates),
                "train_end": max(train_dates),
                "test_start": min(test_dates),
                "test_end": max(test_dates),
                "params": best.get("cfg"),
                "summary": split_summary["summary"],
            }
        )
        for name in systems:
            t = test_stats[name]
            totals[name]["trades"] += int(t.get("trades") or 0)
            totals[name]["wins"] += int(t.get("wins") or 0)
            totals[name]["pnl"] += float(t.get("pnl") or 0.0)
            totals[name]["stake"] += float(t.get("stake") or 0.0)
            totals[name]["edge_sum"] += float(t.get("edge_sum") or 0.0)
            totals[name]["ask_sum"] += float(t.get("ask_sum") or 0.0)
    return totals, splits, last_params


def main() -> int:
    parser = argparse.ArgumentParser(description="AB test entry/exit parameter sets.")
    parser.add_argument("--days", type=int, default=120, help="Lookback days.")
    parser.add_argument("--train-days", type=int, default=30, help="Train window (days).")
    parser.add_argument("--test-days", type=int, default=7, help="Test window (days).")
    parser.add_argument("--step-days", type=int, default=7, help="Step window (days).")
    parser.add_argument("--max-splits", type=int, default=12, help="Max rolling splits.")
    parser.add_argument("--config", default=str(ROOT / "configs" / "ab_entry_search.json"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    today = dt.date.today()
    start = today - dt.timedelta(days=max(args.days, 1))
    rows = backtest.load_runs(start.isoformat(), today.isoformat())
    events = backtest.prepare_events(rows)
    systems = backtest.WEIGHTINGS

    schemes_out: List[Dict[str, Any]] = []
    baselines = build_baselines()
    for key, entry in baselines.items():
        cfg = backtest.strategy_from_params(entry["params"])
        stats = backtest.evaluate_events(events, cfg, systems, 0)
        summary = summarize_stats(stats, systems)
        schemes_out.append(
            {
                "id": key,
                "name": entry["name"],
                "type": "baseline",
                "params": entry["params"],
                **summary,
            }
        )

    search_cfg = load_search_config(Path(args.config))
    roll_stats, splits, last_params = roll_tune(
        events,
        search_cfg,
        args.train_days,
        args.test_days,
        args.step_days,
        args.max_splits,
    )
    roll_summary = summarize_stats(roll_stats, systems)
    schemes_out.append(
        {
            "id": "tuned_roll",
            "name": "滚动调参",
            "type": "rolling",
            "params": last_params or {},
            "splits": splits,
            **roll_summary,
        }
    )

    payload = {
        "ok": True,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "window": {
            "days": args.days,
            "train_days": args.train_days,
            "test_days": args.test_days,
            "step_days": args.step_days,
            "max_splits": args.max_splits,
        },
        "systems": systems,
        "min_trades": search_cfg.min_trades,
        "require_positive_roi": True,
        "schemes": schemes_out,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
