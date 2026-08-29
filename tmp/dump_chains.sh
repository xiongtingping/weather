#!/bin/bash
set -u
label="$1"; out="$2"; shift 2
env "$@" python3 scripts/paper_trading_ledger.py --backfill --rebuild >/dev/null 2>&1
sqlite3 -readonly data/bayes_basket_target_ssot.db \
 "SELECT station_id||'|'||event_date||'|'||printf('%.4f',stake_usdc)||'|'||printf('%.4f',pnl_usdc)||'|'||hit FROM paper_trade_settlement ORDER BY 1;" > "$out"
echo "$label -> $(wc -l < "$out") 条链"
