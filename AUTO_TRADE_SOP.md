# Auto Trade SOP

## Scope
This SOP covers the operational flow for automated trading: preflight checks, running scans, monitoring entries/exits, handling exit skips, and post-run review.

## Preflight (before enabling auto trade)
- Verify environment flags in `.env`:
  - `PM_AUTO_TRADE_ENABLED=1`
  - `PM_AUTO_TRADE_DRY_RUN=0`
  - `PM_AUTO_TRADE_SELL_ON_EXIT=1`
  - `PM_AUTO_TRADE_EXIT_FORCE_AFTER_MIN=<minutes>` (optional safety fallback)
- Optional but recommended sync flags:
  - `PM_TRADE_SYNC_AFTER_SCAN=1` (force sync after each scan)
  - `PM_TRADE_SYNC_LOOP_INTERVAL=120` (seconds between sync cycles)
  - `PM_TRADE_SYNC_RETRIES=3` (transient error retries for trade sync)
  - `PM_TRADE_SYNC_BACKOFF=2.0` (seconds backoff per retry)
  - `PM_TRADE_SYNC_ASSUME_ZERO=1` (treat trade history as source of truth)
  - `PM_CLOB_TRADES_MAX_PAGES=10` (trade history pages per sync)
- Verify required secrets exist (do not print values):
  - `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `PRIVATE_KEY`
- Optional positions API (authoritative positions source):
  - `PM_POSITIONS_SYNC=1`
  - `PM_POSITIONS_API_URL=<endpoint>`
  - `PM_POSITIONS_API_KEY` or `PM_POSITIONS_API_AUTH`
- Confirm network/proxy health for Gamma and CLOB.
- Optional: check balance
  - `node scripts/polymarket_order.mjs balance`

## Run (scan + auto trade)
- Standard run uses the model scan job:
  - `scripts/model_scan.sh`
- If running in the background, keep `data/model_scan.log` as the single source of truth.

## Trade sync loop (recommended)
- Long-running guardian loop (sync + exit-only recalculation):
  - `scripts/trade_sync_loop.sh`
- If using launchd, enable `launchd/com.weather.trade_sync_loop.plist`.

## Monitor (live)
- Tail core log:
  - `tail -f data/model_scan.log`
- Optional watcher:
  - `python3 scripts/auto_trade_watcher.py --log data/model_scan.log`
- Watch for:
  - `[auto-trade] entry ...`
  - `[auto-trade] exit ...`
  - `[auto-trade] exit skip ...`

## Entry flow (what should happen)
1. Scan evaluates candidates.
2. Entry order is placed.
3. Order is confirmed (status in logs).
4. `edge_trades` updated with entry state.

## Exit flow (what should happen)
1. Exit triggers fire (lag, trend, trail stop, hold, etc.).
2. Exit order is placed.
3. Order is confirmed.
4. `edge_trades` updated with exit state.

If exit is skipped, the system logs:
- `[auto-trade] exit skip ... reason=... detail=...`
- `edge_trades.exit_skip_reason`, `edge_trades.exit_skip_detail`, `edge_trades.exit_skip_at`

## Exit skip triage
If a position should exit but did not:
1. Check the log for `exit skip`.
2. Query DB for latest exit skip:
   - `sqlite3 data/archive.db "select exit_skip_reason, exit_skip_detail, exit_skip_at from edge_trades where slug='<slug>' and event_date='<YYYY-MM-DD>' and outcome='<outcome>' and side='<YES|NO>';"`
3. Common causes:
   - `exit_holdoff` (min hold not satisfied)
   - `depth_missing` / `depth_block`
   - `exit_price_missing` / `exit_price_invalid`
   - `exit_retry_blocked`
4. If needed, manually place an exit order.

## Manual order checks (spot verify)
- Open orders:
  - `node scripts/polymarket_order.mjs fetch-open-orders`
- Single order status:
  - `node scripts/polymarket_order.mjs get-order <order_id>`

## Incident checklist (no order placed)
- Confirm scan process is running (and not terminated).
- Confirm auto trade enabled flags.
- Confirm exit trigger in `edge_trades.exit_triggered_at`.
- Confirm no `exit skip` with blocking reason.

## Shutdown / pause
- Set `PM_AUTO_TRADE_ENABLED=0` and restart scans.
- Verify no new auto trade logs appear.
