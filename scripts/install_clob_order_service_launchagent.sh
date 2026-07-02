#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/launchd/com.weather.clob_order_service.plist"
DST="$HOME/Library/LaunchAgents/com.weather.clob_order_service.plist"
LABEL="com.weather.clob_order_service"
UID_NUM="$(id -u)"

if [ ! -f "$SRC" ]; then
  echo "Missing plist: $SRC" >&2
  exit 1
fi

install -d "$HOME/Library/LaunchAgents" "$ROOT/data" "$ROOT/data/logs"

/usr/bin/python3 - "$USER" "$HOME" "$ROOT" "$SRC" "$DST" <<'PY'
import plistlib
import sys
from pathlib import Path

user, home, root, source, destination = sys.argv[1:]
root = Path(root)
with Path(source).open("rb") as f:
    data = plistlib.load(f)
data.pop("UserName", None)
data.pop("GroupName", None)
env = data.get("EnvironmentVariables") or {}
env.setdefault("HOME", home)
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

chmod 644 "$DST"
launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true
launchctl bootout "gui/${UID_NUM}" "$DST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${UID_NUM}" "$DST"
launchctl enable "gui/${UID_NUM}/${LABEL}" || true
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" || true
echo "Installed and started ${LABEL} from ${DST}."
