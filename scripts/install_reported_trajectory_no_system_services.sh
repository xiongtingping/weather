#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${WEATHER_SYSTEM_USER:-${SUDO_USER:-xiong}}"
GROUP_NAME="${WEATHER_SYSTEM_GROUP:-$(id -gn "$USER_NAME")}" || GROUP_NAME="staff"
UID_VALUE="$(id -u "$USER_NAME")"
SYSTEM_DIR="/Library/LaunchDaemons"
ACTIVATION_MODE="$(/usr/bin/python3 - "$ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
value = json.loads((root / "config/trading/basket_plus_hard_no_v1.json").read_text(encoding="utf-8"))
mode = str(value.get("execution_mode") or "").strip()
if mode not in {"production_no_live", "production_live"}:
    raise SystemExit(f"invalid_basket_activation_mode:{mode}")
print(mode)
PY
)"
if [ "$ACTIVATION_MODE" = "production_live" ]; then
  EXPECTED_READ_ONLY="false"
else
  EXPECTED_READ_ONLY="true"
fi
LABELS=(
  com.weather.reported_trajectory_no
  com.weather.market_ws
  com.weather.clob_order_service
)

if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0" >&2
  exit 1
fi

mkdir -p "$ROOT/data" "$ROOT/data/logs"
touch "$ROOT/data/live_paused.flag"

wait_for_system_service_absent() {
  local label="$1"
  local deadline=$((SECONDS + 60))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if ! launchctl print "system/$label" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

all_system_services_loaded() {
  local label
  for label in "${LABELS[@]}"; do
    if ! launchctl print "system/$label" 2>/dev/null \
      | /usr/bin/grep -q '^[[:space:]]*state = running$'; then
      return 1
    fi
  done
  return 0
}

fail_closed() {
  touch "$ROOT/data/live_paused.flag"
  local label
  for label in "${LABELS[@]}"; do
    launchctl bootout "system/$label" >/dev/null 2>&1 || true
  done
  echo "system_domain_cutover_failed; live pause retained; all system owners stopped" >&2
  exit 1
}

# Stop the user-domain owners first. The pause remains until every system
# service is loaded and the gateway is ready.
for label in "${LABELS[@]}"; do
  launchctl disable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
  launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
done
# Do not leave the retired model-release watcher loaded beside the target
# system services.  This only changes launchd state when this root-controlled
# installer is explicitly run during the maintenance window.
launchctl disable system/com.weather.model_release_watcher >/dev/null 2>&1 || true
launchctl bootout system/com.weather.model_release_watcher >/dev/null 2>&1 || true

python3 - "$ROOT" "$USER_NAME" "$GROUP_NAME" "$SYSTEM_DIR" "$ACTIVATION_MODE" <<'PY'
import plistlib
import sys
from pathlib import Path

root, user, group, destination_dir = map(Path, sys.argv[1:5])
activation_mode = str(sys.argv[5])
destination_dir.mkdir(parents=True, exist_ok=True)
specs = {
    "com.weather.reported_trajectory_no": root / "launchd/com.weather.reported_trajectory_no.plist",
    "com.weather.market_ws": root / "launchd/com.weather.market_ws.plist",
    "com.weather.clob_order_service": root / "launchd/com.weather.clob_order_service.plist",
}
for label, source in specs.items():
    with source.open("rb") as handle:
        data = plistlib.load(handle)
    data["UserName"] = str(user)
    data["GroupName"] = str(group)
    env = dict(data.get("EnvironmentVariables") or {})
    env.update({
        "HOME": f"/Users/{user}",
        "USER": str(user),
        "LOGNAME": str(user),
        "WEATHER_SERVICE_DOMAIN": "system",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    })
    if label == "com.weather.reported_trajectory_no":
        # The target writer remains owned by the single system-domain resident.
        env["PM_TARGET_PRODUCTION_WRITER"] = "1"
        # The target writer is never promoted by the protected basket
        # activation. Keep this lane explicitly production_no_live.
        env["PM_TARGET_EXECUTION_MODE"] = "production_no_live"
    if label == "com.weather.clob_order_service":
        env["PM_LIVE_PAUSE_FILE"] = str(root / "data/live_paused.flag")
        if activation_mode == "production_live":
            env.pop("PM_CLOB_ORDER_SERVICE_READ_ONLY", None)
        else:
            env["PM_CLOB_ORDER_SERVICE_READ_ONLY"] = "1"
    data["EnvironmentVariables"] = env
    data["WorkingDirectory"] = str(root)
    destination = destination_dir / f"{label}.plist"
    temporary = destination.with_name(f".{destination.name}.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(data, handle, fmt=plistlib.FMT_XML, sort_keys=False)
    temporary.replace(destination)
PY

for label in "${LABELS[@]}"; do
  plist="$SYSTEM_DIR/${label}.plist"
  chown root:wheel "$plist"
  chmod 644 "$plist"
  launchctl bootout "system/${label}" >/dev/null 2>&1 || true
done

# bootout is asynchronous for the Python resident.  Do not race a still
# exiting owner with bootstrap; launchd reports that race as error 5.
for label in "${LABELS[@]}"; do
  if ! wait_for_system_service_absent "$label"; then
    echo "system_service_did_not_exit=${label}" >&2
    fail_closed
  fi
done

for label in "${LABELS[@]}"; do
  plist="$SYSTEM_DIR/${label}.plist"
  launchctl enable "system/${label}"
  bootstrap_error=""
  bootstrapped=0
  for attempt in 1 2 3 4 5; do
    if bootstrap_output="$(launchctl bootstrap system "$plist" 2>&1)"; then
      bootstrapped=1
      break
    fi
    bootstrap_error="$bootstrap_output"
    if [ "$attempt" -lt 5 ]; then
      sleep "$attempt"
    fi
  done
  if [ "$bootstrapped" -ne 1 ]; then
    echo "system_service_bootstrap_failed=${label}; error=${bootstrap_error:-unknown}" >&2
    fail_closed
  fi
  if ! launchctl kickstart -k "system/${label}" >/dev/null 2>&1; then
    echo "system_service_kickstart_failed=${label}" >&2
    fail_closed
  fi
done

deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if all_system_services_loaded; then
    health_payload="$(/usr/bin/curl -fsS --max-time 2 http://127.0.0.1:8765/health 2>/dev/null || true)"
    if [ -n "$health_payload" ] \
      && printf '%s' "$health_payload" \
      | EXPECTED_READ_ONLY="$EXPECTED_READ_ONLY" /usr/bin/python3 -c 'import json,os,sys; x=json.load(sys.stdin); expected=os.environ.get("EXPECTED_READ_ONLY") == "true"; raise SystemExit(0 if x.get("ready") is True and x.get("read_only") is expected and x.get("live_pause_present") is True and x.get("order_submission_enabled") is False else 1)'; then
      if [ "$ACTIVATION_MODE" = "production_no_live" ]; then
        echo "system_domain_cutover_ok_no_live_pause_retained"
      else
        echo "system_domain_cutover_ok_pause_retained mode=$ACTIVATION_MODE"
      fi
      exit 0
    fi
  fi
  sleep 0.25
done

echo "system_domain_cutover_not_ready; required system services or gateway health missing" >&2
fail_closed
