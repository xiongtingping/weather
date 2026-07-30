#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/launchd/com.weather.monitor.daemon.plist"
UID_VALUE="$(id -u)"
USER_NAME="$(id -un)"
GUI_ROOT="gui/${UID_VALUE}"
LABEL="com.weather.monitor.daemon"
DOMAIN="${GUI_ROOT}/${LABEL}"
DEST_DIR="$HOME/Library/LaunchAgents"
DEST="$DEST_DIR/${LABEL}.plist"
SENTINEL="$ROOT/data/monitor_user_domain_migration.enabled"

if [ "$UID_VALUE" -eq 0 ]; then
  echo "Refusing root install: the monitor is a per-user LaunchAgent." >&2
  exit 1
fi
test -f "$SOURCE"
mkdir -p "$DEST_DIR" "$ROOT/data" "$ROOT/data/logs"

/usr/bin/python3 - "$SOURCE" "$DEST" "$ROOT" "$HOME" "$USER_NAME" "$UID_VALUE" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

source, destination, root, home, user, uid = sys.argv[1:]
with open(source, "rb") as handle:
    data = plistlib.load(handle)
data.pop("UserName", None)
data.pop("GroupName", None)
data["WorkingDirectory"] = root
data["ProgramArguments"] = ["/bin/bash", f"{root}/scripts/weather_monitor_launchagent_entrypoint.sh"]
env = dict(data.get("EnvironmentVariables") or {})
env.update({
    "HOME": home, "USER": user, "LOGNAME": user,
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "PM_MONITOR_LAUNCH_DOMAIN": f"gui/{uid}/com.weather.monitor.daemon",
})
data["EnvironmentVariables"] = env
target = Path(destination)
temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
with temporary.open("wb") as handle:
    plistlib.dump(data, handle, fmt=plistlib.FMT_XML, sort_keys=False)
os.chmod(temporary, 0o644)
os.replace(temporary, target)
PY

printf 'gui/%s/com.weather.monitor.daemon\n' "$UID_VALUE" > "$SENTINEL"
plutil -lint "$DEST"
launchctl bootout "$GUI_ROOT" "$DEST" >/dev/null 2>&1 || true
launchctl enable "$DOMAIN"
launchctl bootstrap "$GUI_ROOT" "$DEST"

if launchctl print system/com.weather.monitor.daemon 2>/dev/null | grep -Eq '^[[:space:]]+pid = [0-9]+$'; then
  echo "installed_domain=$DOMAIN"
  echo "state=waiting_for_legacy_system_exit_or_reboot"
else
  echo "installed_domain=$DOMAIN"
  echo "state=user_domain_active"
fi
echo "sudo_required=false"
