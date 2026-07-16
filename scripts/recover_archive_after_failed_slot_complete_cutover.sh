#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SOURCE="${1:-/private/tmp/archive.pre-amsc-slot-complete-v6.20260716T052957Z.db}"
ACTIVE="$ROOT/data/archive.db"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FAILED="/private/tmp/archive.malformed-after-slot-cutover.$STAMP.db"
CONSOLE_USER="$(stat -f%Su /dev/console)"

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 77; }
[ -s "$SOURCE" ] || { echo "verified recovery source missing: $SOURCE" >&2; exit 2; }
[ "$(stat -f%d "$SOURCE")" = "$(stat -f%d "$ROOT/data")" ] || {
  echo "recovery source must be on same filesystem for atomic rename" >&2
  exit 2
}

launchctl disable system/com.weather.monitor.daemon || true
launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
sudo -u "$CONSOLE_USER" bash "$ROOT/scripts/weatherctl.sh" stop || true

patterns=(
  "$ROOT/scripts/polymarket_monitor_daemon.sh"
  "$ROOT/scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh"
  "$ROOT/data/runtime_snapshots/amsc_zuuu_poll/"
  "$ROOT/scripts/hard_source_direct_poller.py"
  "$ROOT/scan_weather_market.py --auto-trade-job-worker"
)
for pattern in "${patterns[@]}"; do
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  [ -z "$pids" ] || kill -TERM $pids 2>/dev/null || true
done
sleep 2

if lsof "$ACTIVE" "$ROOT/data/archive.db-wal" "$ROOT/data/archive.db-shm" 2>/dev/null | tail -n +2 | grep -q .; then
  echo "archive.db still has open handles; recovery remains fail closed" >&2
  exit 3
fi

sqlite3 "$SOURCE" 'PRAGMA quick_check;' | grep -qx ok
rm -rf "$ROOT/data/db_writer.lock"
rm -f "$ROOT/data/archive.db-wal" "$ROOT/data/archive.db-shm"
mv "$ACTIVE" "$FAILED"
mv "$SOURCE" "$ACTIVE"
chown "$CONSOLE_USER":staff "$ACTIVE"
chmod 644 "$ACTIVE"

if ! sqlite3 "$ACTIVE" 'PRAGMA quick_check;' | grep -qx ok; then
  mv "$ACTIVE" "$SOURCE"
  mv "$FAILED" "$ACTIVE"
  echo "restored snapshot failed quick_check; original malformed file reinstated fail closed" >&2
  exit 4
fi
rm -f "$FAILED"

python3 scripts/production_probability_v6_readiness.py --db "$ACTIVE" --summary
sudo -u "$CONSOLE_USER" bash "$ROOT/scripts/weatherctl.sh" start
install -o root -g wheel -m 644 "$ROOT/launchd/com.weather.monitor.daemon.plist" /Library/LaunchDaemons/com.weather.monitor.daemon.plist
launchctl enable system/com.weather.monitor.daemon
launchctl bootstrap system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
launchctl kickstart -k system/com.weather.monitor.daemon

echo "recovery_state=pre_cutover_archive_restored_and_services_resumed"
echo "archive=$ACTIVE"
