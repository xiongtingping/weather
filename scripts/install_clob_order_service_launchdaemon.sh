#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/launchd/com.weather.clob_order_service.plist"
DST="/Library/LaunchDaemons/com.weather.clob_order_service.plist"
LABEL="com.weather.clob_order_service"
USER_NAME="${PM_LAUNCHD_USER:-${SUDO_USER:-$USER}}"
GROUP_NAME="${PM_LAUNCHD_GROUP:-$(id -gn "${USER_NAME}")}" || GROUP_NAME="staff"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi
if [ ! -f "$SRC" ]; then
  echo "Missing plist: $SRC" >&2
  exit 1
fi

install -d -o "$USER_NAME" -g "$GROUP_NAME" "$ROOT/data" "$ROOT/data/logs"

/usr/bin/python3 - "$USER_NAME" "$GROUP_NAME" "$ROOT" "$SRC" "$DST" <<'PY'
import plistlib
import sys
from pathlib import Path

user, group, root, source, destination = sys.argv[1:]
root = Path(root)
with Path(source).open("rb") as f:
    data = plistlib.load(f)
data["UserName"] = user
data["GroupName"] = group
env = data.get("EnvironmentVariables") or {}
env.setdefault("HOME", "/Users/" + user if user != "root" else "/var/root")
env.setdefault("USER", user)
env.setdefault("LOGNAME", user)
env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
data["EnvironmentVariables"] = env
data["WorkingDirectory"] = str(root)

def rewrite(obj):
    if isinstance(obj, str):
        return obj.replace("/Users/xiong/weather", str(root))
    if isinstance(obj, list):
        return [rewrite(v) for v in obj]
    if isinstance(obj, dict):
        return {k: rewrite(v) for k, v in obj.items()}
    return obj

data = rewrite(data)
Path(destination).parent.mkdir(parents=True, exist_ok=True)
with Path(destination).open("wb") as f:
    plistlib.dump(data, f, fmt=plistlib.FMT_XML, sort_keys=False)
PY

chown root:wheel "$DST"
chmod 644 "$DST"
launchctl bootout system "$DST" >/dev/null 2>&1 || true
if [ -n "${SUDO_UID:-}" ] && [ "${SUDO_UID:-0}" != "0" ]; then
  USER_AGENT="/Users/${USER_NAME}/Library/LaunchAgents/${LABEL}.plist"
  launchctl bootout "gui/${SUDO_UID}/${LABEL}" >/dev/null 2>&1 || true
  launchctl bootout "gui/${SUDO_UID}" "$USER_AGENT" >/dev/null 2>&1 || true
fi
launchctl enable "system/${LABEL}" || true
launchctl bootstrap system "$DST"
launchctl kickstart -k "system/${LABEL}" || true
echo "Installed and started ${LABEL} from ${DST}."
