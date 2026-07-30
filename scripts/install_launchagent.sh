#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT/launchd"
DST_DIR="${HOME}/Library/LaunchAgents"
USER_NAME="${PM_LAUNCHD_USER:-${USER}}"
GROUP_NAME="${PM_LAUNCHD_GROUP:-$(id -gn "${USER_NAME}")}" || GROUP_NAME="staff"
BOOTSTRAP="${PM_LAUNCHD_BOOTSTRAP:-1}"
UID_VALUE="$(id -u "${USER_NAME}")"

if [ ! -d "$SRC_DIR" ]; then
  echo "Missing launchd directory: $SRC_DIR" >&2
  exit 1
fi

install -d "$DST_DIR" "$ROOT/data" "$ROOT/data/logs"

ALL_PLISTS=(
  com.weather.market_ws.plist
  com.weather.auto_trade_entry_1m.plist
  com.weather.daily_trade_sms_summary.plist
  com.weather.archive_db_governance_offpeak.plist
  com.weather.archive_db_auto_guard.plist
)
PLISTS=("${ALL_PLISTS[@]}")
if [ -n "${PM_LAUNCHD_ONLY:-}" ]; then
  IFS=',' read -r -a requested_plists <<< "${PM_LAUNCHD_ONLY}"
  PLISTS=()
  for requested in "${requested_plists[@]}"; do
    requested="$(printf '%s' "$requested" | xargs)"
    [ -n "$requested" ] || continue
    if [[ "$requested" != *.plist ]]; then
      requested="${requested}.plist"
    fi
    for allowed in "${ALL_PLISTS[@]}"; do
      if [ "$requested" = "$allowed" ]; then
        PLISTS+=("$allowed")
        break
      fi
    done
  done
  if [ "${#PLISTS[@]}" -eq 0 ]; then
    echo "PM_LAUNCHD_ONLY did not name a supported LaunchAgent." >&2
    exit 2
  fi
fi
LEGACY_PLISTS=(
  com.weather.auto_trade_worker.plist
  com.weather.trade_sync_loop.plist
  com.weather.auto_trade_watcher.plist
  com.weather.daily_scan.plist
  com.weather.dynamic_three_leg_execution.plist
  com.weather.feedback_loop_hourly.plist
  com.weather.market_refresh_1m.plist
  com.weather.model_scan.plist
  com.weather.q_calibrated_writeback_hourly.plist
  com.weather.target_realization_worker.plist
  com.weather.task_queue_worker.plist
  com.weather.trade_engine.plist
  com.xiong.weather.ui.plist
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
    # Make LaunchAgent definitions relocatable: rewrite any embedded absolute paths to this repo root.
    old_root = infer_old_root(data)
    if old_root and old_root != str(root):
        data = _rewrite_strings(data, old_root, str(root))
    # LaunchAgents run as the current user; remove UserName/GroupName.
    data.pop("UserName", None)
    data.pop("GroupName", None)
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
  launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
  launchctl disable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
  rm -f "$dest"
done

while IFS=$'\t' read -r label dest; do
  [ -z "$label" ] && continue
  [ -z "$dest" ] && continue
  chmod 644 "$dest"
  if [ "$BOOTSTRAP" = "1" ]; then
    launchctl enable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    if ! launchctl bootstrap "gui/${UID_VALUE}" "$dest"; then
      echo "[launchagent] bootstrap failed: ${label} (${dest})" >&2
      launchctl print "gui/${UID_VALUE}/${label}" 2>/dev/null | head -n 20 >&2 || true
      continue
    fi
    launchctl enable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    if [ "${label}" = "com.weather.daily_scan" ] && [ "${PM_LAUNCHD_KICKSTART_DAILY:-0}" != "1" ]; then
      :
    else
      launchctl kickstart -k "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    fi
  fi
done < "$ENTRIES_FILE"
rm -f "$ENTRIES_FILE"

echo "Installed LaunchAgents for ${USER_NAME}."
if [ "$BOOTSTRAP" != "1" ]; then
  echo "Set PM_LAUNCHD_BOOTSTRAP=1 to bootstrap immediately."
fi
