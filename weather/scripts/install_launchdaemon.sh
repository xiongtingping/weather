#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT/launchd"
DST_DIR="/Library/LaunchDaemons"
USER_NAME="${PM_LAUNCHD_USER:-${SUDO_USER:-$USER}}"
GROUP_NAME="${PM_LAUNCHD_GROUP:-$(id -gn "${USER_NAME}")}" || GROUP_NAME="staff"
BOOTSTRAP="${PM_LAUNCHD_BOOTSTRAP:-1}"
DISABLE_AGENTS="${PM_LAUNCHD_DISABLE_AGENTS:-1}"
UID_VALUE="$(id -u "${USER_NAME}")"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi

if [ ! -d "$SRC_DIR" ]; then
  echo "Missing launchd directory: $SRC_DIR" >&2
  exit 1
fi

install -d -o "$USER_NAME" -g "$GROUP_NAME" "$ROOT/data" "$ROOT/data/logs"

PLISTS=(
  com.xiong.weather.ui.plist
  com.weather.model_scan.plist
  com.weather.daily_scan.plist
  com.weather.auto_trade_watcher.plist
  com.weather.auto_trade_entry_1m.plist
  com.weather.trade_engine.plist
)
LEGACY_PLISTS=(
  com.weather.auto_trade_worker.plist
  com.weather.trade_sync_loop.plist
)

/usr/bin/python3 - "$USER_NAME" "$GROUP_NAME" "$ROOT" "$DST_DIR" "${PLISTS[@]}" >"$ROOT/data/.launchd_entries.tmp" <<'PY'
import plistlib
import sys
from pathlib import Path
from typing import Optional

user, group, root, dst_dir, *plist_names = sys.argv[1:]
root = Path(root)
dst_dir = Path(dst_dir)

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
        for x in obj.values():
            yield from _walk_strings(x)

def _rewrite_strings(obj, old_root: str, new_root: str):
    if isinstance(obj, str):
        return obj.replace(old_root, new_root)
    if isinstance(obj, list):
        return [_rewrite_strings(x, old_root, new_root) for x in obj]
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

for name in plist_names:
    src = root / "launchd" / name
    if not src.exists():
        continue
    with src.open("rb") as f:
        data = plistlib.load(f)
    old_root = infer_old_root(data)
    if old_root and old_root != str(root):
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
    dst = dst_dir / name
    with dst.open("wb") as f:
        plistlib.dump(data, f, fmt=plistlib.FMT_XML, sort_keys=False)
    label = data.get("Label", "")
    if label:
        print(f"{label}\t{dst}")
PY
ENTRIES_FILE="$ROOT/data/.launchd_entries.tmp"
if [ ! -s "$ENTRIES_FILE" ]; then
  echo "No plist files installed." >&2
  exit 1
fi

for name in "${LEGACY_PLISTS[@]}"; do
  dest="${DST_DIR}/${name}"
  label="${name%.plist}"
  launchctl bootout "system/${label}" >/dev/null 2>&1 || true
  rm -f "$dest"
  if [ "$DISABLE_AGENTS" = "1" ]; then
    launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    launchctl disable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
  fi
done

while IFS=$'\t' read -r label dest; do
  [ -z "$label" ] && continue
  [ -z "$dest" ] && continue
  chown root:wheel "$dest"
  chmod 644 "$dest"
  if [ "$BOOTSTRAP" = "1" ]; then
    launchctl bootout system "$dest" >/dev/null 2>&1 || true
    launchctl bootstrap system "$dest"
    launchctl enable "system/${label}" || true
    launchctl kickstart -k "system/${label}" || true
  fi
  if [ "$DISABLE_AGENTS" = "1" ]; then
    launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    launchctl disable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
  fi
done < "$ENTRIES_FILE"
rm -f "$ENTRIES_FILE"

echo "Installed LaunchDaemons for ${USER_NAME}."
if [ "$DISABLE_AGENTS" = "1" ]; then
  echo "Disabled matching LaunchAgents in gui/${UID_VALUE}."
fi
if [ "$BOOTSTRAP" != "1" ]; then
  echo "Set PM_LAUNCHD_BOOTSTRAP=1 to bootstrap immediately."
fi
