#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "archive.db"
UI_URL = "http://127.0.0.1:3002/api/ui_snapshot?limit=40"


def _load_json(text: str) -> Dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in {float("inf"), float("-inf")}:
        return None
    return out


def _latest_run_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(run_id) FROM auto_trade_decisions WHERE run_id IS NOT NULL"
    ).fetchone()
    return int(row[0] or 0)


def _json_list(text: Any) -> list[Any]:
    try:
        value = json.loads(str(text or "[]"))
    except Exception:
        return []
    return value if isinstance(value, list) else []


def _load_decisions(conn: sqlite3.Connection, run_id: int) -> Dict[Tuple[str, str, str, str, str], Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, slug, event_date, outcome, side, action, decided_state, will_order,
               reason_codes_json, reasons_json, details_json, created_at
        FROM auto_trade_decisions
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (int(run_id),),
    ).fetchall()
    out: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for row in rows:
        details = _load_json(row["details_json"])
        rec = {
            "id": row["id"],
            "slug": row["slug"],
            "event_date": row["event_date"],
            "outcome": row["outcome"],
            "side": row["side"],
            "action": row["action"],
            "decided_state": row["decided_state"],
            "will_order": int(row["will_order"] or 0),
            "reason_codes": _json_list(row["reason_codes_json"]),
            "reasons": _json_list(row["reasons_json"]),
            "details": details,
            "created_at": row["created_at"],
        }
        key = (
            str(row["slug"] or ""),
            str(row["event_date"] or ""),
            str(row["outcome"] or ""),
            str(row["side"] or "").upper(),
            str(row["action"] or "").lower(),
        )
        out[key] = rec
    return out


def _load_latest_trade_states(conn: sqlite3.Connection) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, slug, event_date, outcome, side, state, position_size,
               last_action, last_status, exit_reason_codes, updated_at
        FROM edge_trades
        ORDER BY id ASC
        """
    ).fetchall()
    out: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row["slug"] or ""),
            str(row["event_date"] or ""),
            str(row["outcome"] or ""),
            str(row["side"] or "").upper(),
        )
        out[key] = {
            "id": row["id"],
            "slug": row["slug"],
            "event_date": row["event_date"],
            "outcome": row["outcome"],
            "side": row["side"],
            "state": row["state"],
            "position_size": _num(row["position_size"]),
            "last_action": row["last_action"],
            "last_status": row["last_status"],
            "exit_reason_codes": _json_list(row["exit_reason_codes"]),
            "updated_at": row["updated_at"],
        }
    return out


def _will_order(decision: Any) -> bool:
    if not isinstance(decision, Mapping):
        return False
    try:
        return int(decision.get("will_order") or 0) == 1
    except Exception:
        return False


def _details(decision: Any) -> Mapping[str, Any]:
    if not isinstance(decision, Mapping):
        return {}
    details = decision.get("details")
    return details if isinstance(details, Mapping) else {}


def _actual_action_for_leg(
    *,
    entry: Optional[Mapping[str, Any]],
    exit_decision: Optional[Mapping[str, Any]],
    latest_trade: Optional[Mapping[str, Any]] = None,
) -> str:
    entry_will = _will_order(entry)
    exit_will = _will_order(exit_decision)
    if entry_will and exit_will:
        return "conflict_entry_and_exit"
    if exit_will:
        return "over_target"
    if isinstance(entry, Mapping):
        entry_codes = {
            str(code or "").strip()
            for code in (entry.get("reason_codes") or [])
            if str(code or "").strip()
        }
        if entry_codes.intersection(
            {
                "duplicate_open_order",
                "open_order_wait",
                "open_order_recheck_pending",
                "open_order_pending",
                "same_outcome_open_order",
            }
        ):
            return "add"
    if isinstance(latest_trade, Mapping):
        trade_state = str(latest_trade.get("state") or "").strip().lower()
        trade_last_action = str(latest_trade.get("last_action") or "").strip().lower()
        trade_codes = {
            str(code or "").strip()
            for code in (latest_trade.get("exit_reason_codes") or [])
            if str(code or "").strip()
        }
        if (
            "target_realization_rebalance_over_target" in trade_codes
            and (trade_state == "exit" or trade_last_action.startswith("exit"))
        ):
            return "over_target"
        if trade_state == "entry_pending" or trade_last_action in {
            "confirm_no_fill",
            "open_order_sync",
            "open_order_recheck",
        }:
            return "add"
    if entry_will:
        return "add"
    return "hold"


def _expected_action(ui_action: str) -> str:
    action = str(ui_action or "").strip()
    if action == "outside_three_day_window":
        return "hold"
    if action in {"add", "hold", "over_target"}:
        return action
    return action or "hold"


def _action_ok(
    *,
    expected_action: str,
    actual_action: str,
    entry: Optional[Mapping[str, Any]],
    exit_decision: Optional[Mapping[str, Any]],
) -> bool:
    if actual_action != expected_action:
        return False
    if expected_action in {"hold", "add"} and _will_order(exit_decision):
        return False
    if expected_action == "over_target" and _will_order(entry):
        return False
    return True


def _fetch_ui_snapshot(url: str) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_snapshot_from_file(path: str) -> Dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text or "{}")
    return payload if isinstance(payload, dict) else {}


def _iter_plan_legs(snapshot: Mapping[str, Any]) -> Iterable[Tuple[Mapping[str, Any], Mapping[str, Any]]]:
    facts = snapshot.get("facts")
    if not isinstance(facts, Mapping):
        return
    items = facts.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, Mapping):
            continue
        plan = item.get("target_realization_capital_plan")
        if not isinstance(plan, Mapping) or not plan.get("ok"):
            continue
        legs = plan.get("legs")
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if isinstance(leg, Mapping):
                yield item, leg


def compare_run_to_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    snapshot: Mapping[str, Any],
    notional_tolerance: float,
    include_event_keys: Optional[Iterable[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    decisions = _load_decisions(conn, run_id)
    latest_trades = _load_latest_trade_states(conn)
    allowed_keys = {
        (str(slug or "").strip(), str(event_date or "").strip().split("T", 1)[0])
        for slug, event_date in (include_event_keys or [])
        if str(slug or "").strip() and str(event_date or "").strip()
    }
    rows = []
    mismatches = []
    action_mismatches = []
    for item, leg in _iter_plan_legs(snapshot):
        slug = str(item.get("slug") or "").strip()
        event_date = str(item.get("event_date") or "")
        event_date_key = event_date.split("T", 1)[0]
        if allowed_keys and (slug, event_date_key) not in allowed_keys:
            continue
        try:
            offset = int((item.get("target_realization_capital_plan") or {}).get("event_offset_days"))
        except Exception:
            offset = 999
        if offset not in {0, 1, 2}:
            continue
        bucket = str(leg.get("bucket") or "")
        side = str(leg.get("side") or "").upper()
        role = str(leg.get("role") or "")
        ui_action = str(leg.get("action") or "")
        ui_notional = _num(leg.get("target_notional"))
        entry = decisions.get((slug, event_date, bucket, side, "entry"))
        exit_decision = decisions.get((slug, event_date, bucket, side, "exit"))
        latest_trade = latest_trades.get((slug, event_date, bucket, side))
        entry_details = _details(entry)
        exit_details = _details(exit_decision)
        entry_notional = _num((entry_details or {}).get("target_realization_plan_notional"))
        entry_exec_notional = _num((entry_details or {}).get("kelly_target_notional"))
        entry_plan_version = str((entry_details or {}).get("target_realization_capital_plan_version") or "")
        exit_plan_action = str((exit_details or {}).get("target_realization_plan_action") or "")
        exit_execution_action = str((exit_details or {}).get("target_realization_execution_action") or "")
        expected = _expected_action(ui_action)
        actual = _actual_action_for_leg(
            entry=entry,
            exit_decision=exit_decision,
            latest_trade=latest_trade,
        )
        diff = None
        if ui_notional is not None and entry_notional is not None:
            diff = abs(float(ui_notional) - float(entry_notional))
        ok = bool(entry and entry_notional is not None and entry_plan_version)
        if ok and diff is not None:
            ok = diff <= float(notional_tolerance)
        if not ok:
            mismatches.append(
                {
                    "event_date": event_date,
                    "slug": slug,
                    "role": role,
                    "bucket": bucket,
                    "side": side,
                    "ui_notional": ui_notional,
                    "decision_notional": entry_notional,
                    "decision_execution_notional": entry_exec_notional,
                    "entry_id": entry.get("id") if isinstance(entry, Mapping) else None,
                    "entry_plan_version": entry_plan_version,
                    "entry_reason_codes": entry.get("reason_codes") if isinstance(entry, Mapping) else None,
                }
            )
        if not _action_ok(
            expected_action=expected,
            actual_action=actual,
            entry=entry,
            exit_decision=exit_decision,
        ):
            action_mismatches.append(
                {
                    "event_date": event_date,
                    "slug": slug,
                    "role": role,
                    "bucket": bucket,
                    "side": side,
                    "ui_action": ui_action,
                    "expected_action": expected,
                    "actual_action": actual,
                    "entry_id": entry.get("id") if isinstance(entry, Mapping) else None,
                    "entry_will_order": entry.get("will_order") if isinstance(entry, Mapping) else None,
                    "entry_state": entry.get("decided_state") if isinstance(entry, Mapping) else None,
                    "entry_reason_codes": entry.get("reason_codes") if isinstance(entry, Mapping) else None,
                    "exit_id": exit_decision.get("id") if isinstance(exit_decision, Mapping) else None,
                    "exit_will_order": exit_decision.get("will_order") if isinstance(exit_decision, Mapping) else None,
                    "exit_state": exit_decision.get("decided_state") if isinstance(exit_decision, Mapping) else None,
                    "exit_reason_codes": exit_decision.get("reason_codes") if isinstance(exit_decision, Mapping) else None,
                    "exit_plan_action": exit_plan_action,
                    "exit_execution_action": exit_execution_action,
                    "latest_trade_id": latest_trade.get("id") if isinstance(latest_trade, Mapping) else None,
                    "latest_trade_state": latest_trade.get("state") if isinstance(latest_trade, Mapping) else None,
                    "latest_trade_last_action": latest_trade.get("last_action") if isinstance(latest_trade, Mapping) else None,
                    "latest_trade_exit_reason_codes": latest_trade.get("exit_reason_codes") if isinstance(latest_trade, Mapping) else None,
                }
            )
        rows.append(
            {
                "event_date": event_date,
                "slug": slug,
                "role": role,
                "bucket": bucket,
                "side": side,
                "ui_action": ui_action,
                "expected_action": expected,
                "actual_action": actual,
                "ui_target_notional": ui_notional,
                "decision_target_notional": entry_notional,
                "decision_execution_notional": entry_exec_notional,
                "diff": diff,
                "entry_id": entry.get("id") if isinstance(entry, Mapping) else None,
                "entry_state": entry.get("decided_state") if isinstance(entry, Mapping) else None,
                "entry_will_order": entry.get("will_order") if isinstance(entry, Mapping) else None,
                "entry_reason_codes": entry.get("reason_codes") if isinstance(entry, Mapping) else None,
                "exit_id": exit_decision.get("id") if isinstance(exit_decision, Mapping) else None,
                "exit_state": exit_decision.get("decided_state") if isinstance(exit_decision, Mapping) else None,
                "exit_will_order": exit_decision.get("will_order") if isinstance(exit_decision, Mapping) else None,
                "exit_reason_codes": exit_decision.get("reason_codes") if isinstance(exit_decision, Mapping) else None,
                "exit_plan_action": exit_plan_action,
                "exit_execution_action": exit_execution_action,
                "latest_trade_id": latest_trade.get("id") if isinstance(latest_trade, Mapping) else None,
                "latest_trade_state": latest_trade.get("state") if isinstance(latest_trade, Mapping) else None,
                "latest_trade_last_action": latest_trade.get("last_action") if isinstance(latest_trade, Mapping) else None,
                "latest_trade_exit_reason_codes": latest_trade.get("exit_reason_codes") if isinstance(latest_trade, Mapping) else None,
            }
        )
    return {
        "ok": not mismatches and not action_mismatches,
        "run_id": run_id,
        "checked_legs": len(rows),
        "mismatch_count": len(mismatches),
        "action_mismatch_count": len(action_mismatches),
        "mismatches": mismatches,
        "action_mismatches": action_mismatches,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare UI target-realization capital plan with auto_trade_decisions."
    )
    parser.add_argument("--run-id", type=int, default=0, help="auto_trade_decisions.run_id; default latest")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--ui-url", default=UI_URL)
    parser.add_argument("--snapshot-file", default="", help="Frozen ui_snapshot JSON file for exact reconciliation")
    parser.add_argument("--notional-tolerance", type=float, default=0.10)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    run_id = int(args.run_id or _latest_run_id(conn))
    if run_id <= 0:
        print("FAIL no auto_trade_decisions run_id found")
        return 2
    snapshot = (
        _load_snapshot_from_file(args.snapshot_file)
        if str(args.snapshot_file or "").strip()
        else _fetch_ui_snapshot(args.ui_url)
    )
    result = compare_run_to_snapshot(
        conn,
        run_id=run_id,
        snapshot=snapshot,
        notional_tolerance=float(args.notional_tolerance),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("mismatch_count") or result.get("action_mismatch_count") else 0


if __name__ == "__main__":
    sys.exit(main())
