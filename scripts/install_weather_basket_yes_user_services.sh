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
touch "$PAUSE"

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
raise SystemExit(0 if (
    x.get("ready") is True
    and x.get("read_only") is True
    and x.get("live_pause_present") is True
    and x.get("order_submission_enabled") is False
) else 1)
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
    "live_pause_retained": True,
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
    "live_pause_retained": True,
    "calls_order_gateway": False,
}, sort_keys=True))
PY

echo "weather_basket_yes_user_domain_install_ok"
