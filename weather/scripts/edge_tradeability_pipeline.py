#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _parse_iso(value: Any) -> Optional[datetime]:
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


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _tail_lines(text: str, max_lines: int = 20) -> str:
    lines = str(text or "").splitlines()
    if not lines:
        return ""
    return "\n".join(lines[-max(1, int(max_lines)) :])


def _run_step(cmd: Sequence[str], cwd: Path) -> Dict[str, Any]:
    env = os.environ.copy()
    py_path = str(cwd)
    old_py_path = str(env.get("PYTHONPATH") or "").strip()
    if old_py_path:
        env["PYTHONPATH"] = f"{py_path}:{old_py_path}"
    else:
        env["PYTHONPATH"] = py_path
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    return {
        "ok": proc.returncode == 0,
        "code": int(proc.returncode),
        "cmd": " ".join(str(x) for x in cmd),
        "stdout_tail": _tail_lines(proc.stdout, 30),
        "stderr_tail": _tail_lines(proc.stderr, 30),
    }


def _parse_last_json_line(text: str) -> Dict[str, Any]:
    for line in reversed(str(text or "").splitlines()):
        row = str(line or "").strip()
        if not row:
            continue
        if not (row.startswith("{") and row.endswith("}")):
            continue
        try:
            payload = json.loads(row)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _recommend_policy_mode(
    *,
    tradeability_audit: Dict[str, Any],
    executable_audit: Dict[str, Any],
) -> Tuple[str, str]:
    trade_gate = bool(((tradeability_audit.get("gate") or {}).get("pass")))
    counts = tradeability_audit.get("counts") or {}
    exec_summary = tradeability_audit.get("edge_exec_audit") or {}
    rows_with_tradeability = int(counts.get("rows_with_tradeability") or 0)
    pred_n = _to_float(exec_summary.get("pred_n"))
    avg_realized_edge = _to_float(exec_summary.get("avg_realized_edge"))
    exec_gate = bool(((executable_audit.get("gate") or {}).get("pass")))

    if (
        trade_gate
        and exec_gate
        and pred_n is not None
        and pred_n >= 80.0
        and rows_with_tradeability >= 500
        and avg_realized_edge is not None
        and avg_realized_edge >= 0.0
    ):
        return "enforced", "gate_pass + samples_enough + realized_edge_nonneg"
    if (
        trade_gate
        and exec_gate
        and pred_n is not None
        and pred_n >= 20.0
        and rows_with_tradeability >= 120
    ):
        return "canary", "gate_pass_but_samples_limited"
    return "shadow", "gate_not_ready"


def _update_env_file(env_path: Path, updates: Dict[str, str]) -> Dict[str, Any]:
    original_lines: List[str] = []
    if env_path.exists():
        original_lines = env_path.read_text(encoding="utf-8").splitlines()
    lines = list(original_lines)
    key_index: Dict[str, int] = {}
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key and key not in key_index:
            key_index[key] = idx

    changed: Dict[str, Dict[str, Any]] = {}
    for key, value in updates.items():
        target = f"{key}={value}"
        if key in key_index:
            idx = key_index[key]
            old_line = lines[idx]
            if old_line != target:
                lines[idx] = target
                changed[key] = {"old": old_line, "new": target}
        else:
            lines.append(target)
            key_index[key] = len(lines) - 1
            changed[key] = {"old": None, "new": target}

    if changed:
        payload = "\n".join(lines).rstrip("\n") + "\n"
        env_path.write_text(payload, encoding="utf-8")
    return {"changed": changed, "changed_count": len(changed)}


def _load_env_file_kv(env_path: Path) -> Dict[str, str]:
    if not env_path.exists():
        return {}
    out: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = str(raw or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip()
    return out


def _parse_bool_text(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _resolve_bool_setting(
    key: str,
    *,
    default: bool,
    env_file_kv: Dict[str, str],
) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        raw = env_file_kv.get(key)
    parsed = _parse_bool_text(raw)
    if parsed is None:
        return bool(default)
    return bool(parsed)


def _export_decision_dataset(
    *,
    db_path: Path,
    since_days: int,
    out_csv: Path,
) -> Dict[str, Any]:
    if not db_path.exists():
        return {"ok": False, "error": f"db_missing:{db_path}"}

    since_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, int(since_days)))).isoformat(timespec="seconds")
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          id,
          run_id,
          run_date,
          slug,
          event_date,
          outcome,
          side,
          action,
          will_order,
          decided_state,
          reason_codes_json,
          created_at,
          json_extract(details_json,'$.strategy') AS strategy,
          json_extract(details_json,'$.edge_realizable_v2') AS edge_realizable_v2,
          json_extract(details_json,'$.edge_realizable_p_realize') AS edge_realizable_p_realize,
          json_extract(details_json,'$.edge_realizable_p_converge') AS edge_realizable_p_converge,
          json_extract(details_json,'$.edge_signal_lcb') AS edge_signal_lcb,
          json_extract(details_json,'$.edge_path_p_reach_lcb') AS edge_path_p_reach_lcb,
          COALESCE(
            json_extract(details_json,'$.edge_tradeability_profit_value'),
            json_extract(details_json,'$.edge_tradeability_edge_lcb')
          ) AS edge_tradeability_profit_value,
          json_extract(details_json,'$.edge_tradeability_mode') AS edge_tradeability_mode,
          json_extract(details_json,'$.edge_tradeability_blocked') AS edge_tradeability_blocked,
          json_extract(details_json,'$.edge_realizable_exec_audit_blocked') AS edge_realizable_exec_audit_blocked,
          json_extract(details_json,'$.edge_trade_realized_edge') AS edge_trade_realized_edge,
          json_extract(details_json,'$.edge_trade_closed') AS edge_trade_closed,
          json_extract(details_json,'$.edge_trade_state') AS edge_trade_state,
          json_extract(details_json,'$.edge_trade_position_size') AS edge_trade_position_size
        FROM auto_trade_decisions
        WHERE action='entry' AND datetime(created_at) >= datetime(?)
        ORDER BY id ASC
        """,
        (since_iso,),
    )
    rows = cur.fetchall()

    cur.execute(
        """
        SELECT
          slug,
          event_date,
          outcome,
          side,
          state,
          entry_size,
          realized_pnl,
          updated_at
        FROM edge_trades
        """
    )
    trade_rows = cur.fetchall()
    conn.close()

    trade_map: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    trade_map_side: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in trade_rows:
        slug = str(row["slug"] or "")
        event_date = str(row["event_date"] or "")
        outcome = str(row["outcome"] or "")
        side = str(row["side"] or "").upper()
        payload = {
            "state": str(row["state"] or ""),
            "entry_size": _to_float(row["entry_size"]),
            "realized_pnl": _to_float(row["realized_pnl"]),
            "updated_at": str(row["updated_at"] or ""),
        }
        key = (slug, event_date, outcome, side)
        trade_map.setdefault(key, []).append(payload)
        side_key = (slug, event_date, side)
        trade_map_side.setdefault(side_key, []).append(payload)
    for key in list(trade_map.keys()):
        trade_map[key].sort(key=lambda item: _parse_iso(item.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc))
    for key in list(trade_map_side.keys()):
        trade_map_side[key].sort(key=lambda item: _parse_iso(item.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc))

    headers = [
        "id",
        "run_id",
        "run_date",
        "slug",
        "event_date",
        "outcome",
        "side",
        "action",
        "will_order",
        "decided_state",
        "reason_codes_json",
        "created_at",
        "strategy",
        "edge_realizable_v2",
        "edge_realizable_p_realize",
        "edge_realizable_p_converge",
        "edge_signal_lcb",
        "edge_path_p_reach_lcb",
        "edge_tradeability_profit_value",
        "edge_tradeability_mode",
        "edge_tradeability_blocked",
        "edge_realizable_exec_audit_blocked",
        "edge_trade_realized_edge",
        "edge_trade_closed",
        "edge_trade_state",
        "edge_trade_position_size",
        "realized_edge_source",
        "label_realized_edge_nonneg",
        "label_realized_edge_pos",
    ]

    with out_csv.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            realized = _to_float(row["edge_trade_realized_edge"])
            realized_source = "details_json" if realized is not None else ""
            if realized is None:
                key = (
                    str(row["slug"] or ""),
                    str(row["event_date"] or ""),
                    str(row["outcome"] or ""),
                    str(row["side"] or "").upper(),
                )
                side_key = (key[0], key[1], key[3])
                candidates = trade_map.get(key) or trade_map_side.get(side_key) or []
                for item in candidates:
                    entry_size = _to_float(item.get("entry_size"))
                    realized_pnl = _to_float(item.get("realized_pnl"))
                    if (
                        entry_size is None
                        or realized_pnl is None
                        or not math.isfinite(entry_size)
                        or not math.isfinite(realized_pnl)
                        or abs(entry_size) <= 1e-9
                    ):
                        continue
                    realized = float(realized_pnl) / float(entry_size)
                    realized_source = "edge_trades"
                    break
            label_nonneg = None if realized is None else (1 if realized >= 0.0 else 0)
            label_pos = None if realized is None else (1 if realized > 0.0 else 0)
            payload = {k: row[k] for k in headers if k in row.keys()}
            payload["edge_realizable_v2"] = _to_float(row["edge_realizable_v2"])
            payload["edge_realizable_p_realize"] = _to_float(row["edge_realizable_p_realize"])
            payload["edge_realizable_p_converge"] = _to_float(row["edge_realizable_p_converge"])
            payload["edge_signal_lcb"] = _to_float(row["edge_signal_lcb"])
            payload["edge_path_p_reach_lcb"] = _to_float(row["edge_path_p_reach_lcb"])
            payload["edge_tradeability_profit_value"] = _to_float(row["edge_tradeability_profit_value"])
            payload["edge_trade_realized_edge"] = realized
            payload["realized_edge_source"] = realized_source
            payload["label_realized_edge_nonneg"] = label_nonneg
            payload["label_realized_edge_pos"] = label_pos
            writer.writerow(payload)

    realized_vals = [_to_float(r["edge_trade_realized_edge"]) for r in rows]
    realized_vals = [x for x in realized_vals if x is not None]
    return {
        "ok": True,
        "out": str(out_csv),
        "rows": len(rows),
        "rows_with_realized_edge": len(realized_vals),
        "avg_realized_edge": (sum(realized_vals) / len(realized_vals)) if realized_vals else None,
    }


def _export_trade_labels(
    *,
    db_path: Path,
    since_days: int,
    out_csv: Path,
) -> Dict[str, Any]:
    if not db_path.exists():
        return {"ok": False, "error": f"db_missing:{db_path}"}
    since_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, int(since_days)))).isoformat(timespec="seconds")
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          slug,
          event_date,
          outcome,
          side,
          state,
          entry_at,
          updated_at,
          entry_price,
          entry_size,
          realized_pnl,
          unrealized_pnl,
          last_action,
          last_action_price,
          last_action_at
        FROM edge_trades
        WHERE datetime(updated_at) >= datetime(?)
          AND entry_size IS NOT NULL
          AND ABS(entry_size) > 1e-9
        ORDER BY datetime(updated_at) ASC
        """,
        (since_iso,),
    )
    rows = cur.fetchall()
    conn.close()

    headers = [
        "slug",
        "event_date",
        "outcome",
        "side",
        "state",
        "entry_at",
        "updated_at",
        "entry_price",
        "entry_size",
        "realized_pnl",
        "unrealized_pnl",
        "last_action",
        "last_action_price",
        "last_action_at",
        "realized_edge",
        "label_realized_edge_nonneg",
        "label_realized_edge_pos",
    ]

    realized_vals: List[float] = []
    with out_csv.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            entry_size = _to_float(row["entry_size"])
            realized_pnl = _to_float(row["realized_pnl"])
            realized_edge = None
            if (
                entry_size is not None
                and realized_pnl is not None
                and math.isfinite(entry_size)
                and math.isfinite(realized_pnl)
                and abs(entry_size) > 1e-9
            ):
                realized_edge = float(realized_pnl) / float(entry_size)
                realized_vals.append(realized_edge)
            payload = {k: row[k] for k in headers if k in row.keys()}
            payload["realized_edge"] = realized_edge
            payload["label_realized_edge_nonneg"] = None if realized_edge is None else (1 if realized_edge >= 0.0 else 0)
            payload["label_realized_edge_pos"] = None if realized_edge is None else (1 if realized_edge > 0.0 else 0)
            writer.writerow(payload)

    return {
        "ok": True,
        "out": str(out_csv),
        "rows": len(rows),
        "rows_with_realized_edge": len(realized_vals),
        "avg_realized_edge": (sum(realized_vals) / len(realized_vals)) if realized_vals else None,
    }


def main() -> int:
    script_path = Path(__file__).resolve()
    root = script_path.parent.parent
    data_dir = root / "data"

    ap = argparse.ArgumentParser(description="Run full tradeability pipeline (calibrate + audits + dataset + policy recommendation).")
    ap.add_argument("--db", default=str(data_dir / "archive.db"))
    ap.add_argument("--since-days", type=int, default=60)
    ap.add_argument("--out", default=str(data_dir / "reports" / "edge_tradeability_pipeline_live.json"))
    ap.add_argument("--calib-orders-out", default=str(data_dir / "edge_realizable_calibration.orders.json"))
    ap.add_argument("--calib-ticks-out", default=str(data_dir / "edge_realizable_calibration.json"))
    ap.add_argument("--realizable-report-out", default=str(data_dir / "edge_realizable_report.json"))
    ap.add_argument("--exec-audit-out", default=str(data_dir / "reports" / "edge_executable_audit_live.json"))
    ap.add_argument("--tradeability-audit-out", default=str(data_dir / "reports" / "edge_tradeability_audit_live.json"))
    ap.add_argument("--strategy-gate-out", default=str(data_dir / "reports" / "edge_strategy_gate_live.json"))
    ap.add_argument("--dataset-out", default=str(data_dir / "reports" / "edge_tradeability_dataset_60d.csv"))
    ap.add_argument("--trade-labels-out", default=str(data_dir / "reports" / "edge_trade_labels_60d.csv"))
    ap.add_argument("--apply-env-gates", action="store_true")
    ap.add_argument("--entry-v2-min", type=float, default=0.05)
    ap.add_argument("--entry-p-realize-min", type=float, default=0.60)
    ap.add_argument("--entry-p-converge-min", type=float, default=0.20)
    ap.add_argument("--strategy-realized-edge-min", type=float, default=0.0)
    ap.add_argument("--strategy-realized-edge-min-samples", type=int, default=20)
    ap.add_argument("--apply-policy-mode", choices=["none", "shadow", "canary", "enforced", "recommended"], default="none")
    args = ap.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)
    orders_out = Path(args.calib_orders_out)
    ticks_out = Path(args.calib_ticks_out)
    report_out = Path(args.realizable_report_out)
    exec_audit_out = Path(args.exec_audit_out)
    tradeability_audit_out = Path(args.tradeability_audit_out)
    strategy_gate_out = Path(args.strategy_gate_out)
    dataset_out = Path(args.dataset_out)
    trade_labels_out = Path(args.trade_labels_out)
    env_path = root / ".env"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    orders_out.parent.mkdir(parents=True, exist_ok=True)
    ticks_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    exec_audit_out.parent.mkdir(parents=True, exist_ok=True)
    tradeability_audit_out.parent.mkdir(parents=True, exist_ok=True)
    strategy_gate_out.parent.mkdir(parents=True, exist_ok=True)
    trade_labels_out.parent.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    steps: List[Dict[str, Any]] = []

    commands = [
        (
            "calibrate_orders",
            [
                py,
                str(script_path.parent / "edge_realizable_calibrate.py"),
                "--db",
                str(db_path),
                "--since-days",
                str(max(1, int(args.since_days))),
                "--fill-mode",
                "orders",
                "--out",
                str(orders_out),
            ],
        ),
        (
            "calibrate_ticks",
            [
                py,
                str(script_path.parent / "edge_realizable_calibrate.py"),
                "--db",
                str(db_path),
                "--since-days",
                str(max(1, int(args.since_days))),
                "--fill-mode",
                "ticks",
                "--out",
                str(ticks_out),
            ],
        ),
        (
            "realizable_report",
            [
                py,
                str(script_path.parent / "edge_realizable_report.py"),
                "--db",
                str(db_path),
                "--since-days",
                str(max(1, int(args.since_days))),
                "--out",
                str(report_out),
            ],
        ),
        (
            "audit_executable",
            [
                py,
                str(script_path.parent / "edge_executable_audit.py"),
                "--db",
                str(db_path),
                "--since-days",
                str(max(1, int(args.since_days))),
                "--out",
                str(exec_audit_out),
            ],
        ),
        (
            "audit_tradeability",
            [
                py,
                str(script_path.parent / "edge_tradeability_audit.py"),
                "--db",
                str(db_path),
                "--since-days",
                str(max(1, int(args.since_days))),
                "--edge-exec-audit",
                str(exec_audit_out),
                "--out",
                str(tradeability_audit_out),
            ],
        ),
        (
            "strategy_gate_report",
            [
                py,
                str(script_path.parent / "edge_strategy_gate_report.py"),
                "--db",
                str(db_path),
                "--days",
                str(max(1, int(args.since_days))),
                "--min-samples",
                str(max(1, int(args.strategy_realized_edge_min_samples))),
                "--min-realized-edge",
                str(float(args.strategy_realized_edge_min)),
                "--out",
                str(strategy_gate_out),
            ],
        ),
    ]

    for name, cmd in commands:
        step = {"name": name, "started_at": _now_iso()}
        result = _run_step(cmd, root)
        step.update(result)
        stdout_json = _parse_last_json_line(result.get("stdout_tail") or "")
        if stdout_json:
            step["stdout_json"] = stdout_json
        step["ended_at"] = _now_iso()
        steps.append(step)

    dataset_result = _export_decision_dataset(
        db_path=db_path,
        since_days=max(1, int(args.since_days)),
        out_csv=dataset_out,
    )
    trade_labels_result = _export_trade_labels(
        db_path=db_path,
        since_days=max(1, int(args.since_days)),
        out_csv=trade_labels_out,
    )

    executable_audit = _load_json(exec_audit_out)
    tradeability_audit = _load_json(tradeability_audit_out)
    strategy_gate_report = _load_json(strategy_gate_out)
    recommended_mode, recommended_reason = _recommend_policy_mode(
        tradeability_audit=tradeability_audit,
        executable_audit=executable_audit,
    )

    strategy_gate_env_path = str(strategy_gate_out)
    try:
        strategy_gate_env_path = str(strategy_gate_out.resolve().relative_to(data_dir.resolve()))
    except Exception:
        strategy_gate_env_path = str(strategy_gate_out)
    env_file_kv = _load_env_file_kv(env_path)
    strategy_enabled = {
        "main": _resolve_bool_setting(
            "PM_AUTO_TRADE_ENTRY_MAIN_ENABLED",
            default=True,
            env_file_kv=env_file_kv,
        ),
        "yes_top": _resolve_bool_setting(
            "PM_AUTO_TRADE_ENTRY_YES_TOP_ENABLED",
            default=True,
            env_file_kv=env_file_kv,
        ),
        "adj_pair": _resolve_bool_setting(
            "PM_AUTO_TRADE_ENTRY_ADJ_PAIR_ENABLED",
            default=True,
            env_file_kv=env_file_kv,
        ),
        "follow_copy": _resolve_bool_setting(
            "PM_AUTO_TRADE_ENTRY_FOLLOW_ENABLED",
            default=True,
            env_file_kv=env_file_kv,
        ),
        "hard_bound_no": _resolve_bool_setting(
            "PM_AUTO_TRADE_ENTRY_HARD_BOUND_NO_ENABLED",
            default=True,
            env_file_kv=env_file_kv,
        ),
    }
    raw_blocked_strategies = list((strategy_gate_report.get("summary") or {}).get("blocked") or [])
    blocked_active_strategies = [
        str(name)
        for name in raw_blocked_strategies
        if strategy_enabled.get(str(name).strip().lower(), True)
    ]
    active_strategies = [name for name, enabled in strategy_enabled.items() if enabled]

    env_updates: Dict[str, str] = {}
    if args.apply_env_gates:
        env_updates.update(
            {
                "PM_AUTO_TRADE_ENTRY_EDGE_REALIZABLE_MIN": f"{max(0.0, float(args.entry_v2_min)):.4f}",
                "PM_AUTO_TRADE_ENTRY_EDGE_REALIZABLE_P_REALIZE_MIN": f"{max(0.0, min(1.0, float(args.entry_p_realize_min))):.4f}",
                "PM_AUTO_TRADE_ENTRY_EDGE_REALIZABLE_P_CONVERGE_MIN": f"{max(0.0, min(1.0, float(args.entry_p_converge_min))):.4f}",
                "PM_AUTO_TRADE_STRATEGY_REALIZED_EDGE_GATE_ENABLED": "1",
                "PM_AUTO_TRADE_STRATEGY_REALIZED_EDGE_MIN": f"{float(args.strategy_realized_edge_min):.4f}",
                "PM_AUTO_TRADE_STRATEGY_REALIZED_EDGE_MIN_SAMPLES": str(max(1, int(args.strategy_realized_edge_min_samples))),
                "PM_AUTO_TRADE_STRATEGY_REALIZED_EDGE_REPORT_PATH": strategy_gate_env_path,
            }
        )

    apply_mode = str(args.apply_policy_mode or "none").strip().lower()
    if args.apply_env_gates and apply_mode == "none":
        apply_mode = "recommended"
    if apply_mode == "recommended":
        apply_mode = recommended_mode
    if apply_mode in {"shadow", "canary", "enforced"}:
        env_updates["PM_EDGE_POLICY_MODE"] = apply_mode

    env_update_result: Dict[str, Any] = {"changed": {}, "changed_count": 0}
    if env_updates:
        env_update_result = _update_env_file(env_path, env_updates)

    failed_steps = [s.get("name") for s in steps if not bool(s.get("ok"))]
    payload = {
        "ok": len(failed_steps) == 0 and bool(dataset_result.get("ok")),
        "generated_at": _now_iso(),
        "since_days": int(args.since_days),
        "db": str(db_path),
        "steps": steps,
        "dataset": dataset_result,
        "trade_labels": trade_labels_result,
        "strategy_gate": {
            "out": str(strategy_gate_out),
            "generated_at": strategy_gate_report.get("generated_at"),
            "gate": strategy_gate_report.get("gate"),
            "summary": strategy_gate_report.get("summary"),
            "active_strategies": active_strategies,
            "strategy_enabled": strategy_enabled,
            "blocked_active_strategies": blocked_active_strategies,
        },
        "artifacts": {
            "calib_orders_out": str(orders_out),
            "calib_ticks_out": str(ticks_out),
            "realizable_report_out": str(report_out),
            "exec_audit_out": str(exec_audit_out),
            "tradeability_audit_out": str(tradeability_audit_out),
            "strategy_gate_out": str(strategy_gate_out),
            "dataset_out": str(dataset_out),
            "trade_labels_out": str(trade_labels_out),
        },
        "policy": {
            "recommended_mode": recommended_mode,
            "recommended_reason": recommended_reason,
            "apply_policy_mode": apply_mode if apply_mode in {"shadow", "canary", "enforced"} else None,
        },
        "env_updates": {
            "requested": env_updates,
            "applied": env_update_result,
            "env_path": str(env_path),
        },
        "summary": {
            "failed_steps": failed_steps,
            "tradeability_gate_pass": bool(((tradeability_audit.get("gate") or {}).get("pass"))),
            "tradeability_failed_names": list((tradeability_audit.get("gate") or {}).get("failed_names") or []),
            "rows_with_tradeability": int(((tradeability_audit.get("counts") or {}).get("rows_with_tradeability") or 0)),
            "exec_gate_pass": bool(((executable_audit.get("gate") or {}).get("pass"))),
            "exec_pred_n": _to_float(
                (tradeability_audit.get("edge_exec_audit") or {}).get("pred_rows_for_gate")
            )
            or _to_float((tradeability_audit.get("edge_exec_audit") or {}).get("pred_n"))
            or _to_float((tradeability_audit.get("edge_exec_audit") or {}).get("pred_n_closed")),
            "exec_pred_n_closed": _to_float(
                (tradeability_audit.get("edge_exec_audit") or {}).get("pred_n_closed")
            )
            or _to_float((tradeability_audit.get("edge_exec_audit") or {}).get("pred_n")),
            "exec_avg_realized_edge": _to_float(
                (tradeability_audit.get("edge_exec_audit") or {}).get("avg_edge_for_gate")
            )
            or _to_float((tradeability_audit.get("edge_exec_audit") or {}).get("avg_realized_edge"))
            or _to_float((tradeability_audit.get("edge_exec_audit") or {}).get("avg_realized_edge_closed")),
            "exec_avg_realized_edge_closed": _to_float(
                (tradeability_audit.get("edge_exec_audit") or {}).get("avg_realized_edge_closed")
            )
            or _to_float((tradeability_audit.get("edge_exec_audit") or {}).get("avg_realized_edge")),
            "strategy_gate_eligible_count": int(((strategy_gate_report.get("summary") or {}).get("eligible_count") or 0)),
            "strategy_gate_blocked_count": int(((strategy_gate_report.get("summary") or {}).get("blocked_count") or 0)),
            "strategy_gate_blocked": raw_blocked_strategies,
            "strategy_gate_active_count": len(active_strategies),
            "strategy_gate_blocked_active_count": len(blocked_active_strategies),
            "strategy_gate_blocked_active": blocked_active_strategies,
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": bool(payload.get("ok")),
                "out": str(out_path),
                "failed_steps": failed_steps,
                "recommended_mode": recommended_mode,
                "tradeability_gate_pass": payload["summary"]["tradeability_gate_pass"],
                "rows_with_tradeability": payload["summary"]["rows_with_tradeability"],
                "exec_pred_n": payload["summary"]["exec_pred_n"],
                "exec_pred_n_closed": payload["summary"]["exec_pred_n_closed"],
                "exec_avg_realized_edge": payload["summary"]["exec_avg_realized_edge"],
                "exec_avg_realized_edge_closed": payload["summary"]["exec_avg_realized_edge_closed"],
                "strategy_gate_eligible_count": payload["summary"]["strategy_gate_eligible_count"],
                "strategy_gate_blocked_count": payload["summary"]["strategy_gate_blocked_count"],
                "strategy_gate_blocked_active_count": payload["summary"]["strategy_gate_blocked_active_count"],
                "env_changed_count": int(env_update_result.get("changed_count") or 0),
            },
            ensure_ascii=False,
        )
    )

    return 0 if bool(payload.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
