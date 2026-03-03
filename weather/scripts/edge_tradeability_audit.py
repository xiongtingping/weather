#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _to_float(val: Any) -> Optional[float]:
    try:
        num = float(val)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _q(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    xs = sorted(vals)
    idx = int(round((len(xs) - 1) * max(0.0, min(1.0, q))))
    return float(xs[idx])


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit tradeability chain (signal -> path -> edge_lcb).")
    ap.add_argument("--db", default="data/archive.db")
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--out", default="data/reports/edge_tradeability_audit_live.json")
    ap.add_argument("--edge-exec-audit", default="data/reports/edge_executable_audit_live.json")
    args = ap.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        payload = {
            "ok": False,
            "error": f"db_missing:{db_path}",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return 2

    since_days = max(1, int(args.since_days))
    since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
    since_iso = since_dt.isoformat(timespec="seconds")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          run_id,
          created_at,
          will_order,
          reason_codes_json,
          json_extract(details_json,'$.strategy') AS strategy,
          json_extract(details_json,'$.edge_realizable_v2') AS v2,
          json_extract(details_json,'$.edge_signal_lcb') AS signal_lcb,
          json_extract(details_json,'$.edge_path_p_reach_lcb') AS path_lcb,
          json_extract(details_json,'$.edge_tradeability_edge_lcb') AS edge_lcb,
          json_extract(details_json,'$.edge_tradeability_mode') AS mode,
          json_extract(details_json,'$.edge_tradeability_blocked') AS blocked,
          json_extract(details_json,'$.edge_tradeability_shadow_block') AS shadow_block,
          json_extract(details_json,'$.edge_tradeability_pass') AS tradeability_pass
        FROM auto_trade_decisions
        WHERE action='entry' AND datetime(created_at) >= datetime(?)
        ORDER BY id DESC
        """,
        (since_iso,),
    )
    rows = cur.fetchall()
    conn.close()

    signal_lcb_vals: List[float] = []
    path_lcb_vals: List[float] = []
    edge_lcb_vals: List[float] = []
    by_strategy: Dict[str, Counter] = defaultdict(Counter)
    reason_counter: Counter = Counter()

    total = len(rows)
    with_tradeability = 0
    blocked_n = 0
    shadow_n = 0
    pass_n = 0
    will_order_n = 0
    v2_n = 0
    edge_lcb_pos_n = 0
    path_lcb_ge_02_n = 0
    signal_lcb_pos_n = 0

    for r in rows:
        strategy = str(r["strategy"] or "").strip().lower() or "unknown"
        mode = str(r["mode"] or "").strip().lower() or "unknown"
        blocked = bool(r["blocked"])
        shadow_block = bool(r["shadow_block"])
        tradeability_pass = bool(r["tradeability_pass"])
        will_order = bool(r["will_order"])
        if will_order:
            will_order_n += 1
        v2 = _to_float(r["v2"])
        signal_lcb = _to_float(r["signal_lcb"])
        path_lcb = _to_float(r["path_lcb"])
        edge_lcb = _to_float(r["edge_lcb"])
        if v2 is not None:
            v2_n += 1
        has_tradeability = (
            signal_lcb is not None
            or path_lcb is not None
            or edge_lcb is not None
            or mode != "unknown"
        )
        if has_tradeability:
            with_tradeability += 1
        if blocked:
            blocked_n += 1
        if shadow_block:
            shadow_n += 1
        if tradeability_pass:
            pass_n += 1
        if signal_lcb is not None:
            signal_lcb_vals.append(signal_lcb)
            if signal_lcb >= 0:
                signal_lcb_pos_n += 1
        if path_lcb is not None:
            path_lcb_vals.append(path_lcb)
            if path_lcb >= 0.20:
                path_lcb_ge_02_n += 1
        if edge_lcb is not None:
            edge_lcb_vals.append(edge_lcb)
            if edge_lcb > 0:
                edge_lcb_pos_n += 1

        by_strategy[strategy]["n"] += 1
        by_strategy[strategy]["will_order_n"] += 1 if will_order else 0
        by_strategy[strategy]["blocked_n"] += 1 if blocked else 0
        by_strategy[strategy]["shadow_n"] += 1 if shadow_block else 0
        by_strategy[strategy]["pass_n"] += 1 if tradeability_pass else 0
        by_strategy[strategy][f"mode:{mode}"] += 1

        reason_codes_blob = str(r["reason_codes_json"] or "").strip()
        if reason_codes_blob:
            try:
                reason_codes = json.loads(reason_codes_blob)
            except Exception:
                reason_codes = []
            if isinstance(reason_codes, list):
                for code in reason_codes:
                    text = str(code or "").strip()
                    if text:
                        reason_counter[text] += 1

    strategy_payload = []
    for strategy, ctr in sorted(by_strategy.items(), key=lambda kv: kv[0]):
        n = int(ctr.get("n", 0))
        strategy_payload.append(
            {
                "strategy": strategy,
                "n": n,
                "will_order_n": int(ctr.get("will_order_n", 0)),
                "blocked_n": int(ctr.get("blocked_n", 0)),
                "shadow_n": int(ctr.get("shadow_n", 0)),
                "pass_n": int(ctr.get("pass_n", 0)),
                "pass_rate": (float(ctr.get("pass_n", 0)) / float(n)) if n > 0 else None,
            }
        )

    edge_exec_audit = _load_json(Path(args.edge_exec_audit))
    edge_exec_replay = (edge_exec_audit.get("replay") or {}) if isinstance(edge_exec_audit, dict) else {}
    edge_exec_pred = (edge_exec_replay.get("prediction_vs_realized_closed") or {})
    edge_exec_effective = (edge_exec_replay.get("prediction_vs_effective_matured") or {})
    edge_exec_avg_real = _to_float(edge_exec_pred.get("avg_realized_edge"))
    edge_exec_pred_n = _to_float(edge_exec_pred.get("n"))
    edge_exec_effective_avg = _to_float(edge_exec_effective.get("avg_effective_edge"))
    edge_exec_effective_n = _to_float(edge_exec_effective.get("n"))
    edge_exec_gate_pass = (
        (edge_exec_audit.get("gate") or {}).get("pass")
        if isinstance(edge_exec_audit.get("gate"), dict)
        else None
    )
    try:
        exec_min_rows = float(os.environ.get("PM_EDGE_TRADEABILITY_EXEC_MIN_ROWS", "10") or 10)
    except Exception:
        exec_min_rows = 10.0
    if not math.isfinite(exec_min_rows) or exec_min_rows < 1:
        exec_min_rows = 10.0
    try:
        exec_min_closed_rows_for_avg = float(
            os.environ.get("PM_EDGE_TRADEABILITY_EXEC_MIN_CLOSED_ROWS_FOR_AVG", "3") or 3
        )
    except Exception:
        exec_min_closed_rows_for_avg = 3.0
    if (
        not math.isfinite(exec_min_closed_rows_for_avg)
        or exec_min_closed_rows_for_avg < 1
    ):
        exec_min_closed_rows_for_avg = 3.0

    edge_exec_rows_for_gate = edge_exec_pred_n
    edge_exec_rows_source = "prediction_vs_realized_closed"
    if (
        edge_exec_rows_for_gate is None
        or edge_exec_rows_for_gate + 1e-9 < exec_min_rows
    ):
        if edge_exec_effective_n is not None and math.isfinite(edge_exec_effective_n):
            edge_exec_rows_for_gate = float(edge_exec_effective_n)
            edge_exec_rows_source = "prediction_vs_effective_matured"

    edge_exec_avg_for_gate = edge_exec_avg_real
    edge_exec_avg_source = "prediction_vs_realized_closed"
    if (
        edge_exec_pred_n is None
        or edge_exec_pred_n + 1e-9 < exec_min_closed_rows_for_avg
    ):
        if edge_exec_effective_avg is not None and math.isfinite(edge_exec_effective_avg):
            edge_exec_avg_for_gate = float(edge_exec_effective_avg)
            edge_exec_avg_source = "prediction_vs_effective_matured"

    gate_checks = [
        {
            "name": "tradeability_rows_min",
            "ok": with_tradeability >= 200,
            "value": float(with_tradeability),
            "threshold": 200.0,
            "comparator": ">=",
        },
        {
            "name": "edge_lcb_avg_pos",
            "ok": (sum(edge_lcb_vals) / len(edge_lcb_vals)) > 0 if edge_lcb_vals else False,
            "value": (sum(edge_lcb_vals) / len(edge_lcb_vals)) if edge_lcb_vals else None,
            "threshold": 0.0,
            "comparator": ">",
        },
        {
            "name": "edge_exec_avg_realized_edge_nonneg",
            "ok": edge_exec_avg_for_gate is not None and edge_exec_avg_for_gate >= 0.0,
            "value": edge_exec_avg_for_gate,
            "threshold": 0.0,
            "comparator": ">=",
            "source": edge_exec_avg_source,
        },
        {
            "name": "edge_exec_pred_rows_min",
            "ok": edge_exec_rows_for_gate is not None and edge_exec_rows_for_gate >= exec_min_rows,
            "value": edge_exec_rows_for_gate,
            "threshold": exec_min_rows,
            "comparator": ">=",
            "source": edge_exec_rows_source,
        },
        {
            "name": "edge_exec_gate_pass",
            "ok": bool(edge_exec_gate_pass),
            "value": bool(edge_exec_gate_pass) if edge_exec_gate_pass is not None else None,
            "threshold": True,
            "comparator": "==",
        },
    ]
    gate_pass = all(bool(x.get("ok")) for x in gate_checks)
    failed_names = [str(x.get("name")) for x in gate_checks if not bool(x.get("ok"))]

    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since_dt.isoformat(timespec="seconds"),
        "counts": {
            "rows_total": total,
            "rows_with_tradeability": with_tradeability,
            "blocked_n": blocked_n,
            "shadow_n": shadow_n,
            "pass_n": pass_n,
            "will_order_n": will_order_n,
            "with_v2_n": v2_n,
        },
        "metrics": {
            "signal_lcb_avg": (sum(signal_lcb_vals) / len(signal_lcb_vals)) if signal_lcb_vals else None,
            "path_lcb_avg": (sum(path_lcb_vals) / len(path_lcb_vals)) if path_lcb_vals else None,
            "edge_lcb_avg": (sum(edge_lcb_vals) / len(edge_lcb_vals)) if edge_lcb_vals else None,
            "edge_lcb_p50": _q(edge_lcb_vals, 0.50),
            "edge_lcb_p90": _q(edge_lcb_vals, 0.90),
            "signal_lcb_pos_share": (float(signal_lcb_pos_n) / float(len(signal_lcb_vals))) if signal_lcb_vals else None,
            "path_lcb_ge_0_20_share": (float(path_lcb_ge_02_n) / float(len(path_lcb_vals))) if path_lcb_vals else None,
            "edge_lcb_pos_share": (float(edge_lcb_pos_n) / float(len(edge_lcb_vals))) if edge_lcb_vals else None,
        },
        "by_strategy": strategy_payload,
        "top_reason_codes": [
            {"reason": str(reason), "count": int(cnt)}
            for reason, cnt in reason_counter.most_common(12)
        ],
        "edge_exec_audit": {
            "generated_at": edge_exec_audit.get("generated_at") if isinstance(edge_exec_audit, dict) else None,
            "gate_pass": edge_exec_gate_pass,
            "pred_n": edge_exec_pred_n,
            "avg_realized_edge": edge_exec_avg_real,
            "pred_n_closed": edge_exec_pred_n,
            "avg_realized_edge_closed": edge_exec_avg_real,
            "pred_n_effective": edge_exec_effective_n,
            "avg_effective_edge": edge_exec_effective_avg,
            "pred_rows_for_gate": edge_exec_rows_for_gate,
            "pred_rows_for_gate_source": edge_exec_rows_source,
            "avg_edge_for_gate": edge_exec_avg_for_gate,
            "avg_edge_for_gate_source": edge_exec_avg_source,
            "exec_min_rows": exec_min_rows,
            "exec_min_closed_rows_for_avg": exec_min_closed_rows_for_avg,
        },
        "gate": {
            "pass": gate_pass,
            "failed_names": failed_names,
            "checks": gate_checks,
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(out_path),
                "rows_total": total,
                "rows_with_tradeability": with_tradeability,
                "gate_pass": gate_pass,
                "failed_names": failed_names,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
