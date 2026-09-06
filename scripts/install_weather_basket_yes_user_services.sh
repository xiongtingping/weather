#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
UID_VALUE="$(id -u)"
DOMAIN="gui/${UID_VALUE}"
PAUSE="$ROOT/data/live_paused.flag"
RECEIPT="$ROOT/output/audits/weather_basket_yes_user_domain_install_latest.json"
LEGACY_LABEL="com.weather.reported_trajectory_no"
LABELS=(
  com.weather.weather_basket_yes
  com.weather.market_ws
  com.weather.clob_order_service
)
RETIRED_SYSTEM_LABELS=(
  com.weather.monitor.daemon
  com.weather.model_release_watcher
)

if [ "$(id -u)" -eq 0 ]; then
  echo "user-domain installer must run as the login user, not root" >&2
  exit 2
fi

mkdir -p "$ROOT/data" "$ROOT/data/logs" "$ROOT/output/audits"
# This used to be ``touch "$PAUSE"``: installing the services also stopped
# live submission until someone armed it again on the board.  A safe default,
# and wrong in practice for the reason the day below shows -- an install is
# not a decision to stop trading, and the switch it threw was invisible until
# an order was refused by it.
#
# Measured 2026-09-06.  A targetctl restart failed at its stop step (times out
# non-zero, and set -e then skips the install that follows), which is exactly
# the failure whose remedy is to run THIS script by hand -- so the recovery
# path armed the trap.  It ran at 12:07:51Z.  At 13:36:22Z EGLC produced the
# day's first complete 2026-09-08 basket: three legs, 5.202 USDC, the 0.031
# leg lifted to 33 shares by charter 98, live_submit_authorized on every
# intent -- and all three came back SUBMIT_REFUSED with
# clob_order_service_live_paused.  Every gate had opened; the counter was shut,
# and nothing said so.
#
# User ruling 2026-09-06 ("取消暂停旗标,我自己开关"): no install or restart
# path sets the flag.  The board raises and lowers it.  The runtime boundary
# is untouched -- the order service still reads the file per order and still
# fails closed on an unreadable one
# (tests/test_clob_gateway_live_pause_boundary.py).  What replaces the touch
# is the report at the end of this script: the state is always said out loud,
# which the silent touch never was.

# The renamed service is a replacement, never a fourth production owner.
# Make the user-domain old label absent before the new plist is installed.
/bin/launchctl disable "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1 || true
/bin/launchctl bootout "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1 || true
/bin/rm -f "$HOME/Library/LaunchAgents/${LEGACY_LABEL}.plist"
legacy_deadline=$((SECONDS + 10))
while /bin/launchctl print "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$legacy_deadline" ]; then
    echo "legacy user-domain owner remained loaded: ${LEGACY_LABEL}" >&2
    exit 3
  fi
  sleep 0.25
done
if /bin/launchctl print "${DOMAIN}/${LEGACY_LABEL}" >/dev/null 2>&1; then
  echo "legacy user-domain owner remained loaded: ${LEGACY_LABEL}" >&2
  exit 3
fi

for label in "${LABELS[@]}" "${RETIRED_SYSTEM_LABELS[@]}"; do
  if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
    echo "system owner still loaded: ${label}; refusing dual-active" >&2
    exit 4
  fi
  if [ -e "/Library/LaunchDaemons/${label}.plist" ]; then
    echo "retired system plist still present: /Library/LaunchDaemons/${label}.plist; run one-time migration first" >&2
    exit 4
  fi
done

PM_LAUNCHD_ONLY="$(
  IFS=,
  printf '%s' "${LABELS[*]}"
)" PM_LAUNCHD_BOOTSTRAP=1 \
  /bin/bash "$ROOT/scripts/install_launchagent.sh"

deadline=$((SECONDS + 45))
while [ "$SECONDS" -lt "$deadline" ]; do
  ready=1
  for label in "${LABELS[@]}"; do
    if ! /bin/launchctl print "${DOMAIN}/${label}" 2>/dev/null \
      | /usr/bin/grep -q '^[[:space:]]*state = running$'; then
      ready=0
      break
    fi
  done
  if [ "$ready" -eq 1 ]; then
    health="$(/usr/bin/curl -fsS --max-time 2 \
      http://127.0.0.1:8765/health 2>/dev/null || true)"
    if [ -n "$health" ] && printf '%s' "$health" \
      | /usr/bin/python3 -c '
import json,sys
x=json.load(sys.stdin)
# Confirm the service is UP.  This used to also require read_only and
# live_pause_present -- it asserted the install had left submission shut.
# With the touch gone (user ruling 2026-09-06) that can never hold, and the
# loop would burn its whole 45s deadline and fall through silently every
# time, which is what it already did whenever PM_CLOB_ORDER_SERVICE_READ_ONLY
# was 0.  The pause state is reported at the end of this script instead of
# being asserted here.
raise SystemExit(0 if x.get("ready") is True else 1)
'; then
      break
    fi
  fi
  sleep 0.25
done

for label in "${LABELS[@]}"; do
  if /bin/launchctl print "system/${label}" >/dev/null 2>&1; then
    echo "dual-active system owner detected after install: ${label}" >&2
    exit 5
  fi
  if ! /bin/launchctl print "${DOMAIN}/${label}" 2>/dev/null \
    | /usr/bin/grep -q '^[[:space:]]*state = running$'; then
    echo "user-domain service not running: ${label}" >&2
    exit 6
  fi
done

/usr/bin/python3 - "$RECEIPT" "$DOMAIN" "${LABELS[@]}" <<'PY'
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Loaded by file, not through the package: ``bayes_basket_target/__init__``
# pulls the whole production tree, whose capture lanes import third-party
# modules the system python3 does not have.  The receipt needs only the
# stdlib-only contracts module; importing the package made this step fail
# after every install since 2026-08-08.  The installer already cd'd to ROOT.
_spec = importlib.util.spec_from_file_location(
    "_receipt_contracts",
    Path.cwd() / "bayes_basket_target" / "contracts.py",
)
_module = importlib.util.module_from_spec(_spec)
# Registered before exec: the module defines dataclasses, and dataclass
# field resolution looks its defining module up in ``sys.modules``.
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
content_sha256 = _module.content_sha256

output = Path(sys.argv[1]).resolve()
domain = sys.argv[2]
labels = sys.argv[3:]
identity = {
    "schema": "weather_basket_yes_user_domain_install_v1",
    "state": "PASS",
    "installed_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z"),
    "domain": domain,
    "service_labels": labels,
    "system_domain_absent": True,
    "single_domain_owner": True,
    "passwordless_future_control": True,
    "production_no_live": True,
    # Measured, not asserted: the installer no longer sets the flag, so this
    # records the operator's standing state at install time.
    "live_pause_present": (Path.cwd() / "data" / "live_paused.flag").is_file(),
    "calls_order_gateway": False,
    "live_submit_authorized": False,
    "orders_submitted": 0,
}
payload = {**identity, "content_sha256": content_sha256(identity)}
encoded = (
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
).encode("utf-8")
if len(encoded) > 64 * 1024:
    raise SystemExit("user_domain_install_receipt_too_large")
output.parent.mkdir(parents=True, exist_ok=True)
temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
temporary.write_bytes(encoded)
os.replace(temporary, output)
print(json.dumps({
    "state": payload["state"],
    "domain": domain,
    "service_count": len(labels),
    "live_pause_present": identity["live_pause_present"],
    "calls_order_gateway": False,
}, sort_keys=True))
PY

# Say the pause state rather than set it.  A flag that is on after an install
# is now the operator's standing choice, and it is read here instead of being
# discovered by a refused order.
if [ -f "$PAUSE" ]; then
  echo "!! live submission is PAUSED -- $PAUSE present since $(stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%SZ' "$PAUSE" 2>/dev/null || echo unknown)"
  echo "   every order returns clob_order_service_live_paused until the board lowers it"
else
  echo "live submission: not paused (no $PAUSE)"
fi

echo "weather_basket_yes_user_domain_install_ok"
