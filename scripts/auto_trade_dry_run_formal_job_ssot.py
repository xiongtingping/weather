#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "archive.db"
REPORT_DIR = ROOT / "data" / "reports"
UI_URL = "http://127.0.0.1:3002/api/ui_snapshot?limit=40"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402
from scripts.check_auto_trade_ssot_capital_plan import compare_run_to_snapshot  # noqa: E402


def _fetch_ui_snapshot(url: str) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=12) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _save_snapshot(snapshot: Mapping[str, Any]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"auto_trade_ssot_dry_run_snapshot_{now_tag}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _select_event_keys(
    snapshot: Mapping[str, Any],
    *,
    max_events: int,
    require_positive_notional: bool,
) -> List[Dict[str, str]]:
    facts = snapshot.get("facts")
    items = facts.get("items") if isinstance(facts, Mapping) else None
    if not isinstance(items, list):
        return []
    positive: List[Tuple[int, str, str]] = []
    fallback: List[Tuple[int, str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        slug = str(item.get("slug") or "").strip()
        event_date = str(item.get("event_date") or "").strip().split("T", 1)[0]
        if not slug or not event_date:
            continue
        plan = item.get("target_realization_capital_plan")
        if not isinstance(plan, Mapping) or plan.get("ok") is False:
            continue
        try:
            offset = int(plan.get("event_offset_days"))
        except Exception:
            offset = 999
        if offset not in {0, 1, 2}:
            continue
        key = (slug, event_date)
        if key in seen:
            continue
        seen.add(key)
        legs = plan.get("legs")
        legs_list = legs if isinstance(legs, list) else []
        has_positive_notional = False
        for leg in legs_list:
            if not isinstance(leg, Mapping):
                continue
            try:
                if float(leg.get("target_notional") or 0.0) > 0:
                    has_positive_notional = True
                    break
            except Exception:
                continue
        row = (offset, slug, event_date)
        fallback.append(row)
        if has_positive_notional:
            positive.append(row)
    picked = positive if (require_positive_notional and positive) else fallback
    picked.sort(key=lambda x: (x[0], x[2], x[1]))
    out: List[Dict[str, str]] = []
    for _, slug, event_date in picked[: max(1, int(max_events or 1))]:
        out.append({"slug": slug, "event_date": event_date})
    return out


def _load_job_by_id(job_id: int) -> Dict[str, Any]:
    if job_id <= 0 or not DB_PATH.exists():
        return {}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM auto_trade_jobs WHERE id = ?", (int(job_id),)).fetchone()
    return swm._serialize_job_row(row) if row else {}


def _load_run_decision_stats(run_id: int) -> Dict[str, Any]:
    if run_id <= 0 or not DB_PATH.exists():
        return {}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        summary_row = conn.execute(
            """
            SELECT
              COUNT(*) AS total_count,
              SUM(CASE WHEN action = 'entry' THEN 1 ELSE 0 END) AS entry_count,
              SUM(CASE WHEN action = 'exit' THEN 1 ELSE 0 END) AS exit_count,
              SUM(CASE WHEN action = 'entry' AND will_order = 1 THEN 1 ELSE 0 END) AS entry_will_order_count
            FROM auto_trade_decisions
            WHERE run_id = ?
            """,
            (int(run_id),),
        ).fetchone()
        sample_rows = conn.execute(
            """
            SELECT id, slug, event_date, outcome, side, action, decided_state, will_order, details_json
            FROM auto_trade_decisions
            WHERE run_id = ?
              AND action = 'entry'
            ORDER BY id ASC
            LIMIT 20
            """,
            (int(run_id),),
        ).fetchall()
    summary = {
        "total_count": int((summary_row["total_count"] if summary_row else 0) or 0),
        "entry_count": int((summary_row["entry_count"] if summary_row else 0) or 0),
        "exit_count": int((summary_row["exit_count"] if summary_row else 0) or 0),
        "entry_will_order_count": int((summary_row["entry_will_order_count"] if summary_row else 0) or 0),
        "entry_samples": [],
    }
    for row in sample_rows or []:
        details: Dict[str, Any] = {}
        try:
            details = json.loads(row["details_json"] or "{}")
        except Exception:
            details = {}
        summary["entry_samples"].append(
            {
                "id": int(row["id"] or 0),
                "slug": str(row["slug"] or ""),
                "event_date": str(row["event_date"] or ""),
                "outcome": str(row["outcome"] or ""),
                "side": str(row["side"] or ""),
                "decided_state": str(row["decided_state"] or ""),
                "will_order": int(row["will_order"] or 0),
                "target_realization_plan_notional": details.get("target_realization_plan_notional"),
                "kelly_target_notional": details.get("kelly_target_notional"),
                "kelly_sizing_source": details.get("kelly_sizing_source"),
                "order_size_source": details.get("order_size_source"),
            }
        )
    return summary


def _wait_for_job_terminal(
    *,
    job_id: int,
    max_cycles: int,
    poll_sec: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    history: List[Dict[str, Any]] = []
    for _ in range(max(1, int(max_cycles or 1))):
        job = _load_job_by_id(job_id)
        state = str(job.get("state") or "")
        if state in {"success", "failed", "stuck", "superseded"}:
            return job, history
        worker_result = swm.run_auto_trade_job_worker_once()
        history.append(worker_result if isinstance(worker_result, dict) else {"raw": worker_result})
        job = _load_job_by_id(job_id)
        state = str(job.get("state") or "")
        if state in {"success", "failed", "stuck", "superseded"}:
            return job, history
        next_retry_at = str(job.get("next_retry_at") or "").strip()
        if next_retry_at:
            wait_for = max(float(poll_sec), 1.0)
        else:
            wait_for = max(0.1, float(poll_sec))
        time.sleep(wait_for)
    return _load_job_by_id(job_id), history


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Enqueue one formal auto-trade dry-run job, execute it through the real worker wrapper, and reconcile SSOT capital plan against run-scoped decisions."
    )
    ap.add_argument("--run-date", default="", help="ET run_date; default today")
    ap.add_argument("--ui-url", default=UI_URL)
    ap.add_argument("--max-events", type=int, default=3)
    ap.add_argument("--priority", type=int, default=1000000)
    ap.add_argument("--source", default="manual_dryrun_ssot_reconcile")
    ap.add_argument("--max-worker-cycles", type=int, default=8)
    ap.add_argument("--poll-sec", type=float, default=1.0)
    ap.add_argument("--notional-tolerance", type=float, default=0.10)
    ap.add_argument("--allow-zero-notional-events", action="store_true")
    args = ap.parse_args(argv)

    os.environ["PM_AUTO_TRADE_DRY_RUN"] = "1"
    os.environ.setdefault("PM_AUTO_TRADE_ENABLED", "0")

    run_date = str(args.run_date or "").strip() or swm.today_date_str("America/New_York")
    snapshot = _fetch_ui_snapshot(args.ui_url)
    snapshot_file = _save_snapshot(snapshot)
    include_event_keys = _select_event_keys(
        snapshot,
        max_events=int(args.max_events or 3),
        require_positive_notional=not bool(args.allow_zero_notional_events),
    )
    if not include_event_keys:
        payload = {
            "ok": False,
            "error": "no_ssot_events_selected",
            "snapshot_file": snapshot_file,
            "run_date": run_date,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    enqueue_payload = {
        "requested_from": "cli_formal_dry_run_ssot_reconcile",
        "include_event_keys": include_event_keys,
        "snapshot_file": snapshot_file,
    }
    enqueue_res = swm.enqueue_auto_trade_job(
        run_date,
        source=str(args.source or "").strip() or "manual_dryrun_ssot_reconcile",
        priority=int(args.priority or 0),
        payload=enqueue_payload,
        force_enqueue=True,
    )
    job = enqueue_res.get("job") if isinstance(enqueue_res, Mapping) else {}
    job_id = int((job or {}).get("id") or 0)
    if not enqueue_res.get("ok") or job_id <= 0:
        payload = {
            "ok": False,
            "error": "enqueue_failed",
            "enqueue": enqueue_res,
            "snapshot_file": snapshot_file,
            "run_date": run_date,
            "include_event_keys": include_event_keys,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    final_job, worker_history = _wait_for_job_terminal(
        job_id=job_id,
        max_cycles=int(args.max_worker_cycles or 1),
        poll_sec=float(args.poll_sec or 1.0),
    )
    run_id = int((final_job.get("run_id") or 0) if isinstance(final_job, Mapping) else 0)
    decision_stats = _load_run_decision_stats(run_id)
    reconcile_result: Dict[str, Any] = {}
    reconcile_ok = False
    if run_id > 0:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            reconcile_result = compare_run_to_snapshot(
                conn,
                run_id=run_id,
                snapshot=snapshot,
                notional_tolerance=float(args.notional_tolerance),
                include_event_keys=[
                    (str(item.get("slug") or ""), str(item.get("event_date") or ""))
                    for item in include_event_keys
                ],
            )
        reconcile_ok = bool(reconcile_result.get("ok"))

    entry_count = int(decision_stats.get("entry_count") or 0)
    ok = bool(
        str(final_job.get("state") or "") == "success"
        and run_id > 0
        and entry_count > 0
        and reconcile_ok
    )
    out = {
        "ok": ok,
        "run_date": run_date,
        "snapshot_file": snapshot_file,
        "include_event_keys": include_event_keys,
        "enqueue": enqueue_res,
        "job_final": final_job,
        "worker_history": worker_history,
        "run_id": run_id,
        "decision_stats": decision_stats,
        "reconcile": reconcile_result,
    }
    if str(final_job.get("state") or "") != "success":
        out["error"] = f"job_not_success:{final_job.get('state') or 'unknown'}"
    elif run_id <= 0:
        out["error"] = "run_id_missing"
    elif entry_count <= 0:
        out["error"] = "entry_sample_missing"
    elif not reconcile_ok:
        out["error"] = "ssot_reconcile_mismatch"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
