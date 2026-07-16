#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/launchd/com.weather.forecast_source_forward_capture.plist"
DST="/Library/LaunchDaemons/com.weather.forecast_source_forward_capture.plist"
LABEL="com.weather.forecast_source_forward_capture"
USER_NAME="${PM_LAUNCHD_USER:-${SUDO_USER:-xiong}}"
GROUP_NAME="${PM_LAUNCHD_GROUP:-$(id -gn "$USER_NAME")}" || GROUP_NAME="staff"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo bash $0" >&2
  exit 1
fi

install -d -o "$USER_NAME" -g "$GROUP_NAME" "$ROOT/data" "$ROOT/data/logs" "$ROOT/data/research/forecast_source_skill/forward"
/usr/bin/python3 - "$SRC" "$DST" "$ROOT" "$USER_NAME" "$GROUP_NAME" <<'PY'
import plistlib
import sys
from pathlib import Path

source, destination, root, user, group = sys.argv[1:]
with Path(source).open("rb") as handle:
    payload = plistlib.load(handle)

def rewrite(value):
    if isinstance(value, str):
        return value.replace("/Users/xiong/weather", root).replace("/Users/xiong", str(Path(root).parent))
    if isinstance(value, list):
        return [rewrite(item) for item in value]
    if isinstance(value, dict):
        return {key: rewrite(item) for key, item in value.items()}
    return value

payload = rewrite(payload)
payload["UserName"] = user
payload["GroupName"] = group
payload["WorkingDirectory"] = root
payload["EnvironmentVariables"]["HOME"] = str(Path(root).parent)
with Path(destination).open("wb") as handle:
    plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=False)
PY
chown root:wheel "$DST"
chmod 644 "$DST"
/usr/bin/plutil -lint "$DST"
/bin/launchctl bootout system "$DST" >/dev/null 2>&1 || true
/bin/launchctl enable "system/$LABEL" || true
/bin/launchctl bootstrap system "$DST"
/bin/launchctl print "system/$LABEL"
