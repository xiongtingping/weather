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

DEFAULT_PLISTS=(
  com.weather.weather_basket_yes.plist
  com.weather.market_ws.plist
  com.weather.clob_order_service.plist
)
# Installable but not installed by default.  The live control server was run
# by hand (as a session preview) and died with the session, which left the
# board's LIVE switch unclickable from 2026-09-09.  Install it once:
#
#   PM_LAUNCHD_ONLY=com.weather.live_control scripts/install_launchagent.sh
#
# The paper ledger sweep is deliberately NOT offered here.  The resident has
# hosted it since 2026-08-24, inside the SSOT writer turn.  This list used to
# offer its launchd plist on the belief that the book otherwise moved only by
# hand; installed on 2026-09-11 it ran every fifteen minutes OUTSIDE that
# turn, and was the only process writing the SSOT that the turn did not
# serialize.  Measured 2026-09-13: capture_noaa_conus_forward held the turn
# for 1140s while production waited 1132s behind it, then failed with
# "database is locked" -- the ledger had taken the real lock at 02:52Z,
# squarely inside that window.  A lane has one scheduler.
OPTIONAL_PLISTS=(
  com.weather.live_control.plist
  # The board's static server on 8791; same history as the live control
  # server (a session preview that died with its session, 2026-09-21).
  com.weather.dashboard_static.plist
  # Charter 207: our settled buckets against the market's, every 3 hours.
  # A reader outside the writer turn; it writes only two files under data/.
  com.weather.settlement_truth_reconciliation.plist
  # The board's research render, one heavy section an hour (2026-09-24,
  # 「单开研究节渲染」).  A reader; it writes one section-cache file.
  com.weather.dashboard_research_render.plist
  # Settled truth against IEM's copy of the same reports, daily at 19:30
  # local (2026-09-25, 「好」 to the IEM truth audit).  A reader outside the
  # writer turn; it writes only two files under data/.
  com.weather.iem_truth_audit.plist
)
SUPPORTED_PLISTS=("${DEFAULT_PLISTS[@]}" "${OPTIONAL_PLISTS[@]}")
PLISTS=("${DEFAULT_PLISTS[@]}")
if [ -n "${PM_LAUNCHD_ONLY:-}" ]; then
  IFS=',' read -r -a requested_plists <<< "${PM_LAUNCHD_ONLY}"
  PLISTS=()
  for requested in "${requested_plists[@]}"; do
    requested="$(printf '%s' "$requested" | xargs)"
    [ -n "$requested" ] || continue
    if [[ "$requested" != *.plist ]]; then
      requested="${requested}.plist"
    fi
    for allowed in "${SUPPORTED_PLISTS[@]}"; do
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
    label = data.get("Label", "")
    env = data.get("EnvironmentVariables") or {}
    env.setdefault("HOME", default_home(user))
    env.setdefault("USER", user)
    env.setdefault("LOGNAME", user)
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    if label in {
        "com.weather.weather_basket_yes",
        "com.weather.market_ws",
        "com.weather.clob_order_service",
    }:
        env["WEATHER_SERVICE_DOMAIN"] = "user"
        env["PM_TARGET_EXECUTION_MODE"] = "production_no_live"
    if label == "com.weather.weather_basket_yes":
        env["PM_TARGET_PRODUCTION_WRITER"] = "1"
    if label == "com.weather.clob_order_service":
        env["PM_CLOB_ORDER_SERVICE_READ_ONLY"] = "1"
        env["PM_LIVE_PAUSE_FILE"] = str(root / "data/live_paused.flag")
    data["EnvironmentVariables"] = env
    data["WorkingDirectory"] = str(root)
    dst = dst_dir / name
    with dst.open("wb") as f:
        plistlib.dump(data, f, fmt=plistlib.FMT_XML, sort_keys=False)
    if label:
        print(f"{label}\t{dst}")
PY

ENTRIES_FILE="$ROOT/data/.launchd_entries.tmp"
if [ ! -s "$ENTRIES_FILE" ]; then
  echo "No plist files installed." >&2
  exit 1
fi

# The three production owners must never be loaded in user and system domains
# at the same time. Initial migration retires the root-owned jobs first; every
# subsequent user-domain install remains passwordless.
if [ "$BOOTSTRAP" = "1" ]; then
  if launchctl print "system/com.weather.reported_trajectory_no" >/dev/null 2>&1; then
    echo "[launchagent] retired system owner still loaded: com.weather.reported_trajectory_no" >&2
    echo "[launchagent] run the one-time user-domain migration before installing" >&2
    rm -f "$ENTRIES_FILE"
    exit 3
  fi
  while IFS=$'\t' read -r label _dest; do
    case "$label" in
      com.weather.weather_basket_yes|com.weather.market_ws|com.weather.clob_order_service)
        if launchctl print "system/${label}" >/dev/null 2>&1; then
          echo "[launchagent] system owner still loaded: ${label}" >&2
          echo "[launchagent] user-domain bootstrap refused to prevent dual-active" >&2
          rm -f "$ENTRIES_FILE"
          exit 3
        fi
        ;;
    esac
  done < "$ENTRIES_FILE"
fi

for name in "${LEGACY_PLISTS[@]}"; do
  dest="${DST_DIR}/${name}"
  label="${name%.plist}"
  launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
  launchctl disable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
  rm -f "$dest"
done

# The old no-strategy LaunchAgent must never coexist with the pure-YES owner.
# This is intentionally repeated here (not only in the migration wrapper) so
# every normal install/restart has the same single-owner invariant.
retired_label="com.weather.reported_trajectory_no"
launchctl disable "gui/${UID_VALUE}/${retired_label}" >/dev/null 2>&1 || true
launchctl bootout "gui/${UID_VALUE}/${retired_label}" >/dev/null 2>&1 || true
rm -f "${DST_DIR}/${retired_label}.plist"
if launchctl print "gui/${UID_VALUE}/${retired_label}" >/dev/null 2>&1; then
  echo "[launchagent] retired user-domain owner remained loaded: ${retired_label}" >&2
  rm -f "$ENTRIES_FILE"
  exit 4
fi

bootstrap_failed=0
while IFS=$'\t' read -r label dest; do
  [ -z "$label" ] && continue
  [ -z "$dest" ] && continue
  chmod 644 "$dest"
  if [ "$BOOTSTRAP" = "1" ]; then
    launchctl enable "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    launchctl bootout "gui/${UID_VALUE}/${label}" >/dev/null 2>&1 || true
    teardown_deadline=$((SECONDS + 10))
    while launchctl print "gui/${UID_VALUE}/${label}" >/dev/null 2>&1; do
      if [ "$SECONDS" -ge "$teardown_deadline" ]; then
        echo "[launchagent] previous instance did not leave launchd: ${label}" >&2
        bootstrap_failed=1
        break
      fi
      sleep 0.25
    done
    if [ "$bootstrap_failed" -ne 0 ]; then
      continue
    fi
    if ! launchctl bootstrap "gui/${UID_VALUE}" "$dest"; then
      echo "[launchagent] bootstrap failed: ${label} (${dest})" >&2
      launchctl print "gui/${UID_VALUE}/${label}" 2>/dev/null | head -n 20 >&2 || true
      bootstrap_failed=1
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

if [ "$bootstrap_failed" -ne 0 ]; then
  exit 5
fi

echo "Installed LaunchAgents for ${USER_NAME}."
if [ "$BOOTSTRAP" != "1" ]; then
  echo "Set PM_LAUNCHD_BOOTSTRAP=1 to bootstrap immediately."
fi
