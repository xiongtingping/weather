#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.weather.clob_order_service"

if [ "$(id -u)" -eq 0 ]; then
  echo "CLOB installer is user-domain only; run without root/sudo" >&2
  exit 2
fi
if /bin/launchctl print "system/${LABEL}" >/dev/null 2>&1; then
  echo "system owner still loaded: ${LABEL}; refusing dual-active" >&2
  exit 3
fi

mkdir -p "$ROOT/data"
# The installer used to ``touch data/live_paused.flag`` here, so a freshly
# installed order service came up refusing every submission until someone
# armed it on the board.  A safe default, and wrong in practice: a reinstall
# is not a decision to stop trading, and the flag it set was invisible until
# an order was refused by it.
#
# Measured 2026-09-06: the flag was set at 12:07:51Z by a service reinstall
# and nobody cleared it.  At 13:36:22Z EGLC produced the day's first complete
# 2026-09-08 basket -- three legs, 5.202 USDC, the 0.031 leg lifted to 33
# shares by charter 98 -- and all three intents came back SUBMIT_REFUSED with
# ``clob_order_service_live_paused``.  The whole chain worked and the counter
# was shut.
#
# User ruling 2026-09-06 ("取消暂停旗标，我自己开关"): the pause flag is the
# operator's switch alone.  Nothing in the install or restart path sets it;
# the board raises and lowers it, and scripts/restart_resident.sh reports its
# state after every restart so a flag that is set is seen rather than
# discovered by a refused order.  The runtime boundary is untouched -- the
# service still reads the file per order and still fails closed on an
# unreadable one (tests/test_clob_gateway_live_pause_boundary.py).
PM_LAUNCHD_ONLY="$LABEL" PM_LAUNCHD_BOOTSTRAP=1 \
  exec /bin/bash "$ROOT/scripts/install_launchagent.sh"
