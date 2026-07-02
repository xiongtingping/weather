#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT/launchd"
DST_DIR="/Library/LaunchDaemons"
USER_NAME="${PM_LAUNCHD_USER:-${SUDO_USER:-$USER}}"
GROUP_NAME="${PM_LAUNCHD_GROUP:-$(id -gn "${USER_NAME}")}" || GROUP_NAME="staff"
UID_VALUE="$(id -u "${USER_NAME}")"
PLIST_NAME="com.weather.monitor.daemon.plist"
MONITOR_LABEL="com.weather.monitor.daemon"
LEGACY_MONITOR_SYSTEM_LABELS=(
  "system/com.weather.auto_trade_watcher"
  "system/com.weather.monitor_daemon"
)
LEGACY_MONITOR_GUI_LABELS=(
  "gui/${UID_VALUE}/com.weather.auto_trade_watcher"
  "gui/${UID_VALUE}/com.weather.monitor_daemon"
)
LEGACY_MONITOR_PLISTS=(
  "/Library/LaunchDaemons/com.weather.auto_trade_watcher.plist"
  "/Library/LaunchDaemons/com.weather.monitor_daemon.plist"
)
LEGACY_MONITOR_AGENT_PLISTS=(
  "/Users/${USER_NAME}/Library/LaunchAgents/com.weather.auto_trade_watcher.plist"
  "/Users/${USER_NAME}/Library/LaunchAgents/com.weather.monitor_daemon.plist"
)

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi

if [ ! -d "$SRC_DIR" ]; then
  echo "Missing launchd directory: $SRC_DIR" >&2
  exit 1
fi

src="$SRC_DIR/$PLIST_NAME"
dst="$DST_DIR/$PLIST_NAME"
if [ ! -f "$src" ]; then
  echo "Missing plist: $src" >&2
  exit 1
fi

install -d -o "$USER_NAME" -g "$GROUP_NAME" "$ROOT/data" "$ROOT/data/logs"

/usr/bin/python3 - "$USER_NAME" "$GROUP_NAME" "$ROOT" "$src" "$dst" <<'PY'
import plistlib
import sys
from pathlib import Path
from typing import Optional

user, group, root, source, destination = sys.argv[1:]
root = Path(root)
source = Path(source)
destination = Path(destination)

def default_home(u: str) -> str:
    if u == "root":
        return "/var/root"
    return f"/Users/{u}"

def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk_strings(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)


def _rewrite_strings(obj, old_root: str, new_root: str):
    if isinstance(obj, str):
        return obj.replace(old_root, new_root)
    if isinstance(obj, list):
        return [_rewrite_strings(v, old_root, new_root) for v in obj]
    if isinstance(obj, dict):
        return {k: _rewrite_strings(v, old_root, new_root) for k, v in obj.items()}
    return obj


def infer_old_root(data: dict) -> Optional[str]:
    wd = data.get("WorkingDirectory")
    if isinstance(wd, str) and wd.startswith("/"):
        return wd
    for s in _walk_strings(data):
        if not isinstance(s, str) or not s.startswith("/"):
            continue
        for marker in ("/scripts/", "/data/", "/.venv/", "/.env", "/scan_weather_market.py", "/ui/server.py"):
            if marker in s:
                return s.split(marker, 1)[0]
    return None

with source.open("rb") as f:
    data = plistlib.load(f)

old_root = infer_old_root(data) or str(root)
if old_root != str(root):
    data = _rewrite_strings(data, old_root, str(root))

data["UserName"] = user
data["GroupName"] = group
env = data.get("EnvironmentVariables") or {}
env.setdefault("HOME", default_home(user))
env.setdefault("USER", user)
env.setdefault("LOGNAME", user)
env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
data["EnvironmentVariables"] = env
data["WorkingDirectory"] = str(root)

destination.parent.mkdir(parents=True, exist_ok=True)
with destination.open("wb") as f:
    plistlib.dump(data, f, fmt=plistlib.FMT_XML, sort_keys=False)
print(f"{data.get('Label','')}\t{destination}")
PY

# Remove legacy watcher agents so monitor domain is single source.
for legacy_gui_label in "${LEGACY_MONITOR_GUI_LABELS[@]}"; do
  launchctl bootout "$legacy_gui_label" >/dev/null 2>&1 || true
  launchctl disable "$legacy_gui_label" >/dev/null 2>&1 || true
done
for legacy_system_label in "${LEGACY_MONITOR_SYSTEM_LABELS[@]}"; do
  launchctl bootout "$legacy_system_label" >/dev/null 2>&1 || true
done
for legacy_plist in "${LEGACY_MONITOR_PLISTS[@]}"; do
  launchctl bootout "$legacy_plist" >/dev/null 2>&1 || true
  rm -f "$legacy_plist"
done
for legacy_plist in "${LEGACY_MONITOR_AGENT_PLISTS[@]}"; do
  launchctl bootout "gui/${UID_VALUE}" "$legacy_plist" >/dev/null 2>&1 || true
  rm -f "$legacy_plist"
done

if [ ! -f "$dst" ]; then
  echo "Generate plist failed: $dst" >&2
  exit 1
fi

chown root:wheel "$dst"
chmod 644 "$dst"
launchctl bootout system "$dst" >/dev/null 2>&1 || true
# A previously disabled system label can make bootstrap fail with a vague
# "Bootstrap failed: 5: Input/output error", so clear the disabled bit first.
launchctl enable "system/${MONITOR_LABEL}" || true
if ! launchctl bootstrap system "$dst"; then
  echo "Bootstrap failed for ${MONITOR_LABEL}. Diagnostics:" >&2
  plutil -lint "$dst" >&2 || true
  launchctl print-disabled system 2>/dev/null | grep -E "${MONITOR_LABEL}|com.weather.auto_trade_watcher|com.weather.monitor_daemon" >&2 || true
  log show --style compact --last 5m --predicate 'process == "launchd" AND (eventMessage CONTAINS[c] "polymarket" OR eventMessage CONTAINS[c] "auto_trade_watcher")' 2>/dev/null | tail -n 80 >&2 || true
  exit 1
fi
launchctl enable "system/${MONITOR_LABEL}" || true
launchctl kickstart -k "system/${MONITOR_LABEL}" || true

echo "Installed and started ${MONITOR_LABEL} from ${dst}."
