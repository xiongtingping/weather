#!/bin/bash
set -euo pipefail

# Make recursive invocations robust even when this script is called via a relative path.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# do shell script() uses a minimal PATH (no Homebrew). Add common locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
fi

UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

UI_PORT="${PM_UI_PORT:-3002}"
UI_URL="http://127.0.0.1:${UI_PORT}"

LA_DIR="${HOME}/Library/LaunchAgents"

labels=(
  "com.xiong.weather.ui"
  "com.weather.market_refresh_1m"
  "com.weather.model_scan"
  "com.weather.auto_trade_entry_1m"
  "com.weather.auto_trade_worker"
  "com.weather.auto_trade_watcher"
  "com.weather.trade_sync_loop"
  "com.weather.daily_scan"
  "com.weather.exit_retry_watch"
)

usage() {
  cat <<EOF
Usage: scripts/weatherctl.sh <command>

Commands:
  up                    Start services and wait for UI heartbeat, then open UI
  oneclick              Kill old procs, clean locks, start services, wait for UI, then open UI
  heartbeat             Check UI heartbeat (/api/heartbeat)
  doctor                Show LaunchAgent + DB freshness overview
  start                 Bootstrap/enable/kickstart LaunchAgents (best-effort)
  stop                  Stop/bootout LaunchAgents (best-effort)
  restart               stop + start
  open                  Open UI in browser (${UI_URL})
  scan:all              Trigger full scan via /api/scan
  scan:model            Trigger model scan via /api/scan
  scan:market           Trigger market refresh via /api/scan
  facts                 Materialize facts for today (SSOT)
  audit:edge            Run executable-edge audit script (supports extra args)
  audit:tradeability    Run signal->path->tradeable-edge audit script
  pipeline:tradeability Run full tradeability pipeline (calibrate+audits+dataset+strategy-gate)
  auto:run              Enqueue auto-trade job for today
  auto:smoke-test       Real BUY then SELL (auto-select liquid target)
  auto:restart-watcher  Kickstart auto-trade watcher LaunchAgent
  clean-locks           Remove stale lock directories under data/
  kill-procs            Kill old python/node processes for this repo (best-effort)
  db                    Open sqlite3 shell on data/archive.db
  tail:<name>           Tail logs (name: market|model|entry|worker|watcher|sync|ui|uierr)
EOF
}

die() { echo "error: $*" >&2; exit 2; }

_now() { date "+%Y-%m-%d %H:%M:%S"; }

_curl() {
  /usr/bin/curl -sS --max-time 12 "$@"
}

_curl_long() {
  /usr/bin/curl -sS --max-time 120 "$@"
}

_heartbeat_ok() {
  # curl returns non-zero on connect failure.
  local out
  out="$(_curl -f "${UI_URL}/api/heartbeat" 2>/dev/null || true)"
  if [ -z "$out" ]; then
    return 1
  fi
  # Lightweight check; avoid jq/rg dependency (Dock app environment PATH is minimal).
  if echo "$out" | /usr/bin/grep -Eq "\"ok\"[[:space:]]*:[[:space:]]*true"; then
    return 0
  fi
  return 1
}

_wait_for_heartbeat() {
  local wait_sec="${1:-60}"
  local deadline
  deadline=$(( $(date +%s) + wait_sec ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if _heartbeat_ok; then
      return 0
    fi
    sleep 1
  done
  return 1
}

_plist_path_for_label() {
  local label="$1"
  echo "${LA_DIR}/${label}.plist"
}

_bootstrap_label() {
  local label="$1"
  local plist="$(_plist_path_for_label "$label")"
  if [ ! -f "$plist" ]; then
    return 0
  fi
  launchctl bootstrap "${GUI_DOMAIN}" "$plist" >/dev/null 2>&1 || true
  launchctl enable "${GUI_DOMAIN}/${label}" >/dev/null 2>&1 || true
  case "$label" in
    com.weather.daily_scan|com.weather.q_calibrated_writeback_hourly)
      return 0
      ;;
  esac
  launchctl kickstart -k "${GUI_DOMAIN}/${label}" >/dev/null 2>&1 || true
}

_bootout_label() {
  local label="$1"
  local plist="$(_plist_path_for_label "$label")"
  if [ ! -f "$plist" ]; then
    return 0
  fi
  launchctl stop "${GUI_DOMAIN}/${label}" >/dev/null 2>&1 || true
  launchctl bootout "${GUI_DOMAIN}" "$plist" >/dev/null 2>&1 || true
}

cmd="${1:-}"
if [ -z "$cmd" ]; then
  usage
  exit 0
fi
shift || true

case "$cmd" in
  -h|--help|help)
    usage
    exit 0
    ;;
  up)
    "$SELF" start >/dev/null 2>&1 || true
    # If UI isn't up yet, keep trying for a short window.
    if _wait_for_heartbeat 60; then
      open "$UI_URL" >/dev/null 2>&1 || true
      echo "ok"
      exit 0
    fi
    # One more attempt: kickstart UI specifically, then wait a bit more.
    launchctl kickstart -k "${GUI_DOMAIN}/com.xiong.weather.ui" >/dev/null 2>&1 || true
    if _wait_for_heartbeat 30; then
      open "$UI_URL" >/dev/null 2>&1 || true
      echo "ok"
      exit 0
    fi
    echo "error: ui heartbeat failed: ${UI_URL}/api/heartbeat" >&2
    exit 1
    ;;
  oneclick)
    # Dock-friendly: do the common recovery/bring-up steps, then open the UI.
    "$SELF" kill-procs >/dev/null 2>&1 || true
    "$SELF" clean-locks >/dev/null 2>&1 || true
    "$SELF" start >/dev/null 2>&1 || true
    if _wait_for_heartbeat 90; then
      open "$UI_URL" >/dev/null 2>&1 || true
      echo "ok"
      exit 0
    fi
    echo "error: ui heartbeat failed: ${UI_URL}/api/heartbeat" >&2
    "$SELF" doctor >/dev/null 2>&1 || true
    exit 1
    ;;
  heartbeat)
    if _heartbeat_ok; then
      echo "ok"
      exit 0
    fi
    echo "down"
    exit 1
    ;;
  doctor)
    echo "---- $(_now) doctor ----"
    echo "[launchctl]"
    launchctl list | /usr/bin/grep -E "weather|xiong" || true
    echo ""
    echo "[net]"
    if [ -n "${PM_PROXY_PORTS:-}" ]; then
      ok_any=0
      IFS=',' read -r -a ports <<<"${PM_PROXY_PORTS}"
      for p in "${ports[@]}"; do
        p="$(echo "${p:-}" | /usr/bin/tr -d '[:space:]')"
        [ -z "$p" ] && continue
        if command -v nc >/dev/null 2>&1 && nc -z -w 1 127.0.0.1 "$p" >/dev/null 2>&1; then
          echo "proxy_port_127.0.0.1:${p}  ok"
          ok_any=1
        else
          echo "proxy_port_127.0.0.1:${p}  down"
        fi
      done
      if [ "${PM_CLOB_FORCE_PROXY:-0}" = "1" ] && [ "$ok_any" != "1" ]; then
        echo "warn: PM_CLOB_FORCE_PROXY=1 but no proxy port is reachable; CLOB calls may fail."
      fi
    else
      echo "proxy_ports: (unset)"
    fi
    echo ""
    echo "[db] last ticks / facts"
    if [ -f "$ROOT/data/archive.db" ]; then
      sqlite3 -header -column "$ROOT/data/archive.db" "select max(created_at) as last_market_tick_at, count(*) as ticks from event_market_ticks;" || true
      sqlite3 -header -column "$ROOT/data/archive.db" "select max(fact_as_of) as last_fact_as_of, count(*) as facts from event_fact_history;" || true
      sqlite3 -header -column "$ROOT/data/archive.db" "select max(created_at) as last_event_run_at, count(*) as runs from event_runs;" || true
      sqlite3 -header -column "$ROOT/data/archive.db" "select max(created_at) as last_auto_order_at, count(*) as auto_orders from auto_orders;" 2>/dev/null || true
    else
      echo "db_missing: $ROOT/data/archive.db"
    fi
    echo ""
    echo "[locks]"
    for d in "$ROOT/data/"*.lock; do
      if [ -d "$d" ]; then
        pid_file="$d/pid"
        pid=""
        if [ -f "$pid_file" ]; then
          pid="$(cat "$pid_file" 2>/dev/null || true)"
        fi
        echo "$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$d")  $d  pid=${pid:-}"
      fi
    done
    echo ""
    echo "[logs] mtime"
    for p in \
      "$ROOT/data/market_refresh.log" \
      "$ROOT/data/model_scan.log" \
      "$ROOT/data/logs/auto_trade_entry_1m.log" \
      "$ROOT/data/logs/auto_trade_watcher.out.log" \
      "$ROOT/data/logs/trade_sync_loop.out.log" \
      "$ROOT/data/ui_server.out.log" \
      "$ROOT/data/ui_server.err.log" \
      ; do
      if [ -f "$p" ]; then
        echo "$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$p")  $p"
      fi
    done
    ;;
  start)
    echo "---- $(_now) start ----"
    for label in "${labels[@]}"; do
      _bootstrap_label "$label"
    done
    echo "ok"
    ;;
  stop)
    echo "---- $(_now) stop ----"
    for label in "${labels[@]}"; do
      _bootout_label "$label"
    done
    echo "ok"
    ;;
  restart)
    "$SELF" stop
    "$SELF" start
    ;;
  open)
    open "$UI_URL" >/dev/null 2>&1 || true
    echo "opened: $UI_URL"
    ;;
  scan:all)
    "$SELF" facts >/dev/null 2>&1 || true
    _curl "${UI_URL}/api/scan?start=1&mode=all&refresh_models=1" || true
    echo ""
    ;;
  scan:model)
    _curl "${UI_URL}/api/scan?start=1&mode=model&refresh_models=1" || true
    echo ""
    ;;
  scan:market)
    _curl "${UI_URL}/api/scan?start=1&mode=market" || true
    echo ""
    ;;
  facts)
    RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
      die "missing venv python: $PY"
    fi
    echo "---- $(_now) facts_materialize (run_date=${RUN_DATE}) ----"
    "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$RUN_DATE"
    ;;
  audit:edge)
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
      PY="$(command -v python3 || true)"
    fi
    if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
      die "missing python runtime"
    fi
    "$PY" "$ROOT/scripts/edge_executable_audit.py" \
      --db "$ROOT/data/archive.db" \
      --gate-min-link-ratio "${PM_EDGE_AUDIT_GATE_MIN_LINK_RATIO:-0.65}" \
      --gate-min-proxy-days "${PM_EDGE_AUDIT_GATE_MIN_PROXY_DAYS:-1}" \
      --gate-max-tick-age-p90-sec "${PM_EDGE_AUDIT_GATE_MAX_TICK_AGE_P90_SEC:-14400}" \
      "$@"
    ;;
  audit:tradeability)
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
      PY="$(command -v python3 || true)"
    fi
    if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
      die "missing python runtime"
    fi
    "$PY" "$ROOT/scripts/edge_tradeability_audit.py" \
      --db "$ROOT/data/archive.db" \
      "$@"
    ;;
  pipeline:tradeability)
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
      PY="$(command -v python3 || true)"
    fi
    if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
      die "missing python runtime"
    fi
    "$PY" "$ROOT/scripts/edge_tradeability_pipeline.py" \
      --db "$ROOT/data/archive.db" \
      "$@"
    ;;
  auto:run)
    RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
      PY="$(command -v python3 || true)"
    fi
    if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
      die "missing python runtime"
    fi
    "$PY" "$ROOT/scan_weather_market.py" --auto-trade-enqueue --date "${RUN_DATE}" --auto-trade-source weatherctl --auto-trade-priority "${PM_AUTO_TRADE_JOB_PRIORITY_ENTRY:-120}" || true
    echo ""
    ;;
  auto:smoke-test)
    _curl_long "${UI_URL}/api/ops?start=1&cmd=auto_trade_smoke_test&enable=1&confirm=1" || true
    echo ""
    ;;
  auto:restart-watcher)
    _curl "${UI_URL}/api/auto-trade-restart?start=1" || true
    echo ""
    ;;
  clean-locks)
    echo "---- $(_now) clean-locks ----"
    rm -rf "$ROOT/data/"*.lock "$ROOT/data/"*.lock/ 2>/dev/null || true
    rm -rf "$ROOT/data/market_refresh.lock" "$ROOT/data/model_scan.lock" "$ROOT/data/auto_trade_entry.lock" "$ROOT/data/auto_trade_worker.lock" "$ROOT/data/facts_materialize.lock" 2>/dev/null || true
    echo "ok"
    ;;
  kill-procs)
    echo "---- $(_now) kill-procs ----"
    # Best-effort: only kill processes under this repo path.
    for pat in \
      "$ROOT/ui/server.py" \
      "$ROOT/scan_weather_market.py" \
      "$ROOT/scripts/auto_trade_watcher.py" \
      "$ROOT/scripts/auto_trade_worker.sh" \
      "$ROOT/scripts/trade_sync_loop.sh" \
      "$ROOT/scripts/daily_scan_and_settlement.sh" \
      ; do
      pgrep -f "$pat" 2>/dev/null | while read -r pid; do
        if [ -n "${pid:-}" ]; then
          kill -9 "$pid" 2>/dev/null || true
        fi
      done || true
    done
    echo "ok"
    ;;
  db)
    sqlite3 "$ROOT/data/archive.db"
    ;;
  tail:market)
    tail -n 200 -f "$ROOT/data/market_refresh.log"
    ;;
  tail:model)
    tail -n 200 -f "$ROOT/data/model_scan.log"
    ;;
  tail:entry)
    tail -n 200 -f "$ROOT/data/logs/auto_trade_entry_1m.log"
    ;;
  tail:watcher)
    tail -n 200 -f "$ROOT/data/logs/auto_trade_watcher.out.log"
    ;;
  tail:worker)
    tail -n 200 -f "$ROOT/data/logs/auto_trade_worker.log"
    ;;
  tail:sync)
    tail -n 200 -f "$ROOT/data/logs/trade_sync_loop.out.log"
    ;;
  tail:ui)
    tail -n 200 -f "$ROOT/data/ui_server.out.log"
    ;;
  tail:uierr)
    tail -n 200 -f "$ROOT/data/ui_server.err.log"
    ;;
  *)
    die "unknown command: $cmd"
    ;;
esac
