#!/usr/bin/env python3
"""
Non-mirror strategy performance + entry-quality report (SSOT from archive.db).

This script is intentionally read-only and uses backend SSOT functions:
- compute_strategy_stats(): realized performance from fills (FIFO attribution to entry strategy)
- compute_strategy_entry_quality(): entry-time expected-edge diagnostics aligned to facts snapshots
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any, Dict, List, Tuple


def _fmt_usd(v: Any) -> str:
    try:
        x = float(v)
    except Exception:
        return "—"
    return f"{x:+.2f}"


def _fmt_pct(v: Any) -> str:
    try:
        x = float(v)
    except Exception:
        return "—"
    return f"{x*100:+.1f}%"


def _fmt_num(v: Any, decimals: int = 3) -> str:
    try:
        x = float(v)
    except Exception:
        return "—"
    return f"{x:.{decimals}f}"


def _fmt_int(v: Any) -> str:
    try:
        return str(int(v))
    except Exception:
        return "0"


def _sorted_keys(stats: Dict[str, Dict[str, Any]]) -> List[str]:
    keys = [str(k or "").strip().lower() for k in (stats or {}).keys()]
    keys = [k for k in keys if k]
    keys.sort(key=lambda k: float((stats.get(k) or {}).get("entry_order_notional") or 0.0), reverse=True)
    return keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    # Allow running as a standalone script (ui/ isn't an installed package).
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Import here so running this script doesn't slow down module import for others.
    from ui.server import compute_strategy_entry_quality, compute_strategy_stats, resolve_stats_range

    start_dt, end_dt = resolve_stats_range(conn, int(args.days), require_settlement=False)
    start_text, end_text = start_dt.isoformat(), end_dt.isoformat()

    stats = compute_strategy_stats(conn, days=int(args.days))
    quality = compute_strategy_entry_quality(conn, days=int(args.days))
    for k, q in (quality or {}).items():
        if k in stats and isinstance(stats.get(k), dict) and isinstance(q, dict):
            stats[k].update(q)

    exclude = {"mirror_follow", "mirror_follow_improve"}
    hidden = {"exit", "smoke_test"}

    print(f"Stats window (event_date): {start_text} -> {end_text}  (days={args.days})")
    print("")
    print("Columns:")
    print("  strategy | placed/filled | pnl | roi | win_rate | pnl(NO/YES) | roi(NO/YES) | exp_edge(c) | pos_exp% | model_spread")
    print("")

    for key in _sorted_keys(stats):
        if key in hidden or key in exclude:
            continue
        s = stats.get(key) or {}
        if not isinstance(s, dict):
            continue
        has_any = (
            float(s.get("order_count") or 0) > 0
            or float(s.get("buy_fill_count") or 0) > 0
            or abs(float(s.get("total_pnl") or 0.0)) > 1e-9
        )
        if not has_any:
            continue
        exp_edge = s.get("entry_fact_avg_exec_edge_entry")
        exp_edge_c = None
        try:
            exp_edge_c = float(exp_edge) * 100.0
        except Exception:
            exp_edge_c = None
        exp_edge_text = f"{exp_edge_c:+.1f}c" if exp_edge_c is not None else "—"
        line = " | ".join(
            [
                f"{key:12s}",
                f"{_fmt_int(s.get('order_count'))}/{_fmt_int(s.get('filled_order_count'))}",
                _fmt_usd(s.get("total_pnl")),
                _fmt_pct(s.get("roi")),
                _fmt_pct(s.get("win_rate")),
                f"{_fmt_usd(s.get('total_pnl_no'))}/{_fmt_usd(s.get('total_pnl_yes'))}",
                f"{_fmt_pct(s.get('roi_no'))}/{_fmt_pct(s.get('roi_yes'))}",
                exp_edge_text,
                _fmt_pct(s.get("entry_fact_pos_exec_edge_ratio")),
                _fmt_num(s.get("entry_fact_avg_model_spread"), 3),
            ]
        )
        print(line)

    print("")
    print("Notes:")
    print("- pnl/roi/win_rate: realized, from fills FIFO attributed to *entry* strategy.")
    print("- exp_edge / pos_exp% / model_spread: entry-time diagnostics from facts snapshot at/before first fill.")
    print("- pos_exp% is per filled entry order sample (not size-weighted).")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
