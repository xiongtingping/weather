#!/bin/bash
# A/B the next-day fleet regime shift through the real paper ledger.
#
# Holds the ledger sweep's own lock for the whole comparison.  The resident
# hosts a --backfill every fifteen minutes; landing between the two arms it
# would delete the experimental rows (different fingerprint, inside the
# recent window) and re-derive live ones, which would silently corrupt the
# comparison rather than fail it.
#
# Ends by restoring the live book, because both arms rebuild in place.

set -uo pipefail
cd /Users/xiong/weather
. scripts/lib/single_instance.sh
single_instance paper_trading_ledger_sweep

run_arm() {
  local label="$1"; shift
  echo "──── $label"
  local started=$SECONDS
  env "$@" python3 scripts/paper_trading_ledger.py \
      --backfill --rebuild --report 2>/dev/null | tail -1 \
    > "tmp/regime_ab_${label}.json"
  echo "     rebuilt in $((SECONDS - started))s"
  python3 tmp/regime_ab_readout.py "$label" "tmp/regime_ab_${label}.json"
}

run_arm baseline PAPER_LEDGER_FLEET_REGIME=0
run_arm regime   PAPER_LEDGER_FLEET_REGIME=P0,P0-M

echo "──── restoring the live book"
python3 scripts/paper_trading_ledger.py --backfill --rebuild >/dev/null 2>&1
python3 - <<'PY'
import sqlite3, sys
sys.path.insert(0, "scripts")
import paper_trading_ledger as L
conn = sqlite3.connect(
    "file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30.0
)
rows = conn.execute(
    "SELECT DISTINCT selection_rule_sha256 FROM paper_trade_position"
).fetchall()
live = L.SELECTION_RULE_SHA256
print("     rule fingerprints now in the book:",
      [r[0][:12] for r in rows])
print("     live fingerprint:", live[:12],
      "| clean:", all(r[0] == live for r in rows))
PY
echo "──── done"
