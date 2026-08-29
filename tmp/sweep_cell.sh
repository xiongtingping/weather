#!/bin/bash
# One sweep cell: rebuild the ledger under the given knobs and print the book.
set -u
label="$1"; shift
out=$(env "$@" python3 scripts/paper_trading_ledger.py --backfill --rebuild --report 2>/dev/null | tail -1)
python3 - "$label" "$out" <<'PY'
import json, sys
label, raw = sys.argv[1], sys.argv[2]
try:
    d = json.loads(raw)["book"]
except Exception:
    print("%-34s (解析失败)" % label); raise SystemExit
print("%-34s ROI %+7.1f%%  权益 %8.1f  命中 %5.1f%%  结算 %3d  投入 %7.1f"
      % (label, 100*d["return_on_stake"], d["equity_usdc"],
         100*d["hit_rate"], d["settled_chains"], d["staked_usdc"]))
PY
