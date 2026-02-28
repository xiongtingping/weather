#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import scan_weather_market as s


def _latest_run_date() -> Optional[str]:
    try:
        with s.open_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(run_date) FROM event_runs")
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None
    except Exception:
        return None


def _set_env(key: str, value: Optional[str]) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = str(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strong backfill for edge_trade_fills using time windows."
    )
    parser.add_argument("--days", type=int, default=60, help="Lookback days (default: 60)")
    parser.add_argument("--window-days", type=int, default=7, help="Window size in days (default: 7)")
    parser.add_argument("--run-date", type=str, default="latest", help="Run date for token map")
    parser.add_argument("--max-pages", type=int, default=200, help="Max pages per window (default: 200)")
    parser.add_argument("--budget-ms", type=int, default=600000, help="Budget ms per window")
    parser.add_argument("--hard-timeout-ms", type=int, default=620000, help="Hard timeout ms per window")
    parser.add_argument("--no-infer-buy", action="store_true", help="Disable inferred buy fills")
    args = parser.parse_args()

    run_date = args.run_date
    if run_date in ("", "latest", "auto"):
        run_date = _latest_run_date() or ""
    if not run_date:
        raise SystemExit("No run_date found in event_runs.")

    with s.open_db() as conn:
        trade_token_map, trade_mark_map, event_count = s.build_trade_sync_maps(conn, run_date)
    if not trade_token_map:
        raise SystemExit(f"No trade tokens found for run_date={run_date}")

    # Configure trade fetch env for the node script.
    _set_env("PM_CLOB_TRADES_MAX_PAGES", str(args.max_pages))
    _set_env("PM_CLOB_TRADES_BUDGET_MS", str(args.budget_ms))
    _set_env("PM_CLOB_TRADES_HARD_TIMEOUT_MS", str(args.hard_timeout_ms))
    _set_env("PM_CLOB_RETRY_MAX", os.environ.get("PM_CLOB_RETRY_MAX", "4"))
    _set_env("PM_CLOB_DIRECT_RETRIES", os.environ.get("PM_CLOB_DIRECT_RETRIES", "4"))
    if args.no_infer_buy:
        _set_env("PM_TRADE_SYNC_INFER_BUY", "0")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, args.days))
    window_days = max(1, args.window_days)
    window = timedelta(days=window_days)

    print(
        f"Backfill run_date={run_date} events={event_count} "
        f"range={start.date()}..{now.date()} window_days={window_days}"
    )

    # Walk from newest to oldest.
    window_end = now
    while window_end > start:
        window_start = max(start, window_end - window)
        after = int(window_start.timestamp())
        before = int(window_end.timestamp())
        _set_env("PM_CLOB_TRADES_AFTER", str(after))
        _set_env("PM_CLOB_TRADES_BEFORE", str(before))
        label = f"{window_start.isoformat(timespec='seconds')}..{window_end.isoformat(timespec='seconds')}"
        print(f"[backfill] window {label} after={after} before={before}")
        ok = s.sync_trade_fills(trade_token_map, trade_mark_map)
        print(f"[backfill] ok={ok} window={label}")
        # Advance to next window
        window_end = window_start - timedelta(seconds=1)
        # Small pause to avoid hammering APIs
        time.sleep(0.5)

    # Cleanup window params
    _set_env("PM_CLOB_TRADES_AFTER", None)
    _set_env("PM_CLOB_TRADES_BEFORE", None)


if __name__ == "__main__":
    main()
