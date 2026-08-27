#!/bin/bash
# Run one A/B arm of the fleet regime shift through the real paper ledger.
#
# Takes the ledger sweep's own lock, so the resident's hosted --backfill
# cannot land in the middle of a rebuild.  It can still land between arms,
# which is why the readout checks fingerprint purity rather than trusting
# that it did not.
#
# Usage: tmp/regime_arm.sh <label> [ENV=VALUE ...]

set -uo pipefail
cd /Users/xiong/weather
. scripts/lib/single_instance.sh
single_instance paper_trading_ledger_sweep

label="$1"; shift
started=$SECONDS
env "$@" python3 scripts/paper_trading_ledger.py \
    --backfill --rebuild --report 2>/dev/null | tail -1 \
  > "tmp/regime_ab_${label}.json"
echo "rebuilt in $((SECONDS - started))s"

python3 - "$label" <<'PY'
import sqlite3, sys, os
sys.path.insert(0, "scripts")
conn = sqlite3.connect(
    "file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30.0
)
shas = [r[0] for r in conn.execute(
    "SELECT DISTINCT selection_rule_sha256 FROM paper_trade_position"
)]
print("fingerprints in the book:", [s[:12] for s in shas],
      "| pure:", len(shas) == 1)
PY

python3 tmp/regime_ab_readout.py "$label" "tmp/regime_ab_${label}.json"
