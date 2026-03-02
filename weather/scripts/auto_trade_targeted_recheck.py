#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import signal
from collections import Counter
from datetime import datetime
from multiprocessing import get_context
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(REPO_ROOT, "data", "archive.db")

if REPO_ROOT not in os.sys.path:
    os.sys.path.insert(0, REPO_ROOT)


def _load_stale_events(conn: sqlite3.Connection, run_date: str) -> List[Tuple[str, str, int]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.slug, d.event_date, COUNT(*) AS cnt
        FROM auto_trade_decisions d
        JOIN auto_trade_jobs j ON j.run_id = d.run_id
        WHERE j.run_date = ?
          AND j.state = 'success'
          AND COALESCE(j.source, '') != 'manual_dryrun'
          AND d.action = 'entry'
          AND d.reason_codes_json LIKE '%market_tick_stale%'
        GROUP BY d.slug, d.event_date
        ORDER BY cnt DESC
        """,
        (run_date,),
    )
    return [
        (
            str(r[0] or "").strip(),
            str(r[1] or "").split("T", 1)[0],
            int(r[2] or 0),
        )
        for r in cur.fetchall()
        if str(r[0] or "").strip()
    ]


def _load_fact_payload(
    conn: sqlite3.Connection,
    run_date: str,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.run_date = ? AND h.slug = ? AND h.event_date = ?
        LIMIT 1
        """,
        (run_date, slug, event_date),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return {}
    try:
        fact = json.loads(row[0])
    except Exception:
        return {}
    if not isinstance(fact, dict):
        return {}
    rows_payload = fact.get("distribution") or fact.get("rows") or []
    if not isinstance(rows_payload, list) or not rows_payload:
        return {}
    payload = dict(fact)
    payload["rows"] = rows_payload
    payload.setdefault("slug", slug)
    payload.setdefault("event_url", f"https://polymarket.com/event/{slug}")
    return payload


def _event_worker(
    run_date: str,
    event_date: str,
    payload: Dict[str, Any],
    run_id: int,
    queue,
) -> None:
    import scan_weather_market as swm

    swm.AUTO_TRADE_DRY_RUN = True
    try:
        swm._attach_entry_expected_targets_from_ssot(payload)
        swm.execute_auto_trades(run_date, event_date, payload, exit_only=False, run_id=run_id)
        queue.put({"ok": True})
    except Exception as exc:
        queue.put({"ok": False, "error": str(exc)})


def _run_with_timeout(
    run_date: str,
    event_date: str,
    payload: Dict[str, Any],
    run_id: int,
    timeout_sec: float,
    *,
    inline: bool = False,
) -> Tuple[bool, str]:
    if inline:
        import scan_weather_market as swm

        swm.AUTO_TRADE_DRY_RUN = True
        swm._attach_entry_expected_targets_from_ssot(payload)
        timeout_val = float(timeout_sec or 0)
        if timeout_val < 0:
            timeout_val = 0.0
        old_handler = None

        def _timeout_handler(_signum, _frame):
            raise TimeoutError("timeout")

        try:
            if timeout_val > 0:
                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.setitimer(signal.ITIMER_REAL, timeout_val)
            swm.execute_auto_trades(run_date, event_date, payload, exit_only=False, run_id=run_id)
            return True, ""
        except TimeoutError:
            return False, "timeout"
        except Exception as exc:
            return False, str(exc)
        finally:
            if timeout_val > 0:
                try:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                except Exception:
                    pass
                if old_handler is not None:
                    try:
                        signal.signal(signal.SIGALRM, old_handler)
                    except Exception:
                        pass

    ctx = get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_event_worker,
        args=(run_date, event_date, payload, run_id, queue),
    )
    proc.start()
    proc.join(timeout_sec)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        return False, "timeout"
    try:
        res = queue.get_nowait()
    except Exception:
        return False, "no_result"
    if res.get("ok"):
        return True, ""
    return False, str(res.get("error") or "worker_failed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-date", default="")
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--inline", action="store_true", help="Run inline (no multiprocessing)")
    ap.add_argument(
        "--skip-refresh-check",
        action="store_true",
        help="Skip orderbook refresh precheck before executing auto-trade",
    )
    args = ap.parse_args()

    os.environ["PM_AUTO_TRADE_DRY_RUN"] = "1"
    os.environ["PM_AUTO_TRADE_ENABLED"] = "0"

    if args.run_date:
        run_date = args.run_date
    else:
        run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        stale_events = _load_stale_events(conn, run_date)

    if not stale_events:
        print("no_stale_events")
        return 0

    import scan_weather_market as swm

    run_id = swm._auto_trade_job_insert_run_row(run_date, swm.utc_now_iso())

    ok_count = 0
    fail_count = 0
    fallback_count = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for slug, event_date, cnt in stale_events:
            payload = _load_fact_payload(conn, run_date, slug, event_date)
            if not payload:
                fail_count += 1
                continue
            if not args.skip_refresh_check:
                refresh_args = argparse.Namespace(
                    market_refresh_only=True,
                    market_refresh_max_outcomes=8,
                    market_refresh_time_budget_sec=8.0,
                    market_refresh_clob_timeout_sec=2.0,
                    market_refresh_clob_retries=1,
                    depth_levels=3,
                    tz="America/New_York",
                )
                try:
                    event_detail = swm.fetch_gamma_event_by_slug(slug)
                except Exception:
                    event_detail = None
                ok_refresh, err_refresh, _count, _rows, created_at = swm.refresh_market_ticks_for_event(
                    slug,
                    event_date,
                    refresh_args,
                    event_detail=event_detail,
                )
                if not ok_refresh and str(err_refresh or "").startswith("no_orderbook_quotes"):
                    targets, targets_source = swm._resolve_fallback_decision_targets(
                        payload, slug, event_date
                    )
                    wrote = swm._record_auto_trade_fallback_decisions(
                        run_id=run_id,
                        run_date=run_date,
                        slug=slug,
                        event_date=event_date,
                        title=str(payload.get("title") or ""),
                        targets=targets,
                        reason_code="orderbook_missing",
                        reason_text="订单簿不可用（orderbook_missing）",
                        details={
                            "refresh_error": err_refresh,
                            "refresh_created_at": created_at,
                            "targets_source": targets_source,
                            "source": "targeted_recheck",
                        },
                        decided_state="blocked",
                        will_order=0,
                    )
                    fallback_count += int(wrote > 0)
                    print(
                        f"event_orderbook_missing slug={slug} date={event_date} wrote={wrote} err={err_refresh}"
                    )
                    continue
            ok, err = _run_with_timeout(
                run_date,
                event_date,
                payload,
                int(run_id),
                float(args.timeout_sec),
                inline=bool(args.inline),
            )
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                print(f"event_fail slug={slug} date={event_date} err={err}")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT reason_codes_json, will_order
            FROM auto_trade_decisions
            WHERE run_id = ? AND action = 'entry'
            """,
            (run_id,),
        )
        rows = cur.fetchall()
        total = len(rows)
        will_order = sum(1 for r in rows if int(r["will_order"] or 0) == 1)
        reasons = Counter()
        for r in rows:
            rc_raw = r["reason_codes_json"] or ""
            try:
                rc_list = json.loads(rc_raw) if rc_raw else []
            except Exception:
                rc_list = [rc_raw] if rc_raw else []
            if not isinstance(rc_list, list):
                rc_list = [str(rc_list)]
            key = ",".join([str(x) for x in rc_list if str(x)]) or "none"
            reasons[key] += 1

    print("dryrun_run_id", run_id)
    print("events_ok", ok_count, "events_fail", fail_count, "fallback_written", fallback_count)
    print("total_decisions", total, "will_order", will_order)
    print("market_tick_stale", reasons.get("market_tick_stale", 0))
    print("top_reasons", reasons.most_common(6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
