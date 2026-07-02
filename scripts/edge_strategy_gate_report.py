#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _to_int(value: Any) -> int:
    val = _to_float(value)
    if val is None:
        return 0
    try:
        return int(val)
    except Exception:
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    script_path = Path(__file__).resolve()
    root = script_path.parent.parent
    data_dir = root / "data"

    ap = argparse.ArgumentParser(description="Build strategy-level realized-edge gate report from fills SSOT.")
    ap.add_argument("--db", default=str(data_dir / "archive.db"))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--out", default=str(data_dir / "reports" / "edge_strategy_gate_live.json"))
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--min-realized-edge", type=float, default=0.0)
    args = ap.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)
    days = max(1, int(args.days))
    min_samples = max(1, int(args.min_samples))
    min_realized_edge = float(args.min_realized_edge)

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from ui.server import compute_strategy_stats

    if not db_path.exists():
        payload = {
            "ok": False,
            "generated_at": _now_iso(),
            "error": f"db_missing:{db_path}",
            "db": str(db_path),
            "days": days,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": False, "error": payload["error"], "out": str(out_path)}, ensure_ascii=False))
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        raw_stats = compute_strategy_stats(conn, days=days)
    finally:
        conn.close()

    by_strategy: List[Dict[str, Any]] = []
    by_strategy_map: Dict[str, Dict[str, Any]] = {}
    for strategy, raw in (raw_stats or {}).items():
        name = str(strategy or "").strip().lower() or "main"
        if name in {"exit", "smoke_test"}:
            continue
        if not isinstance(raw, dict):
            continue
        sample_n = _to_int(raw.get("closed_trade_count"))
        trade_count = _to_int(raw.get("trade_count"))
        total_cost = _to_float(raw.get("total_cost"))
        total_pnl = _to_float(raw.get("total_pnl"))
        realized_edge = _to_float(raw.get("roi"))
        win_rate = _to_float(raw.get("win_rate"))
        order_count = _to_int(raw.get("order_count"))
        filled_order_count = _to_int(raw.get("filled_order_count"))
        has_edge = realized_edge is not None
        eligible = has_edge and sample_n >= min_samples
        pass_gate = bool(eligible and realized_edge is not None and realized_edge >= min_realized_edge)
        row = {
            "strategy": name,
            "sample_n": sample_n,
            "closed_trade_count": sample_n,
            "trade_count": trade_count,
            "order_count": order_count,
            "filled_order_count": filled_order_count,
            "realized_edge": realized_edge,
            "roi": realized_edge,
            "total_cost": total_cost,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "eligible": eligible,
            "pass_gate": pass_gate,
        }
        by_strategy.append(row)
        by_strategy_map[name] = row

    by_strategy.sort(
        key=lambda row: (
            _to_float(row.get("total_cost")) or 0.0,
            _to_int(row.get("sample_n")),
            str(row.get("strategy") or ""),
        ),
        reverse=True,
    )

    eligible_rows = [row for row in by_strategy if bool(row.get("eligible"))]
    pass_rows = [row for row in eligible_rows if bool(row.get("pass_gate"))]
    blocked_rows = [row for row in eligible_rows if not bool(row.get("pass_gate"))]
    low_sample_rows = [row for row in by_strategy if not bool(row.get("eligible"))]

    payload = {
        "ok": True,
        "generated_at": _now_iso(),
        "db": str(db_path),
        "days": days,
        "gate": {
            "min_samples": min_samples,
            "min_realized_edge": min_realized_edge,
            "eligible_strategies": len(eligible_rows),
            "pass_strategies": len(pass_rows),
            "blocked_strategies": len(blocked_rows),
        },
        "summary": {
            "strategy_count": len(by_strategy),
            "eligible_count": len(eligible_rows),
            "pass_count": len(pass_rows),
            "blocked_count": len(blocked_rows),
            "low_sample_count": len(low_sample_rows),
            "blocked": [str(row.get("strategy") or "") for row in blocked_rows],
            "low_sample": [str(row.get("strategy") or "") for row in low_sample_rows],
        },
        "by_strategy": by_strategy,
        "by_strategy_map": by_strategy_map,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(out_path),
                "strategy_count": len(by_strategy),
                "eligible_count": len(eligible_rows),
                "blocked_count": len(blocked_rows),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
