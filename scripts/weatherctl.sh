#!/bin/bash
set -euo pipefail

# Make recursive invocations robust even when this script is called via a relative path.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# do shell script() uses a minimal PATH (no Homebrew). Add common locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi
. "$ROOT/scripts/lib/resolve_project_python.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/weather_refresh_coordinator.sh"
weather_refresh_configure_lane "${PM_WEATHERCTL_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}"

UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

UI_PORT="${PM_UI_PORT:-3002}"
UI_URL="http://127.0.0.1:${UI_PORT}"

LA_DIR="${HOME}/Library/LaunchAgents"

labels=(
  "com.xiong.weather.ui"
  "com.weather.market_ws"
  "com.weather.market_refresh_1m"
  "com.weather.model_scan"
  "com.weather.model_scan_hourly"
  "com.weather.task_queue_worker"
  "com.weather.q_calibrated_writeback_hourly"
  "com.weather.feedback_loop_hourly"
  "com.weather.daily_scan"
  "com.weather.archive_db_auto_guard"
  "com.weather.archive_db_governance_offpeak"
)

legacy_trade_labels=(
  "com.weather.auto_trade_entry_1m"
  "com.weather.trade_engine"
  "com.weather.auto_trade_worker"
  "com.weather.trade_sync_loop"
  "com.weather.target_realization_worker"
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
  audit:ui-probability  Run UI page probability consistency audit (supports extra args)
  pipeline:tradeability Run full tradeability pipeline (calibrate+audits+dataset+strategy-gate)
  auto:run              Enqueue auto-trade job for today
  auto:smoke-test       Smoke-test preflight only; real BUY/SELL needs manual confirm phrase
  auto:restart-watcher  Kickstart auto-trade watcher
  clean-locks           Remove stale lock directories under data/
  kill-procs            Kill old python/node processes for this repo (best-effort)
  db                    Open sqlite3 shell on data/archive.db
  tail:<name>           Tail logs (name: ws|market|model|entry|engine|worker|watcher|sync|ui|uierr)
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

_bounded_kickstart() {
  local domain_label="$1"
  local child_pid=""
  launchctl kickstart -k "$domain_label" >/dev/null 2>&1 &
  child_pid=$!
  for _ in $(seq 1 50); do
    if ! kill -0 "$child_pid" 2>/dev/null; then
      wait "$child_pid" 2>/dev/null || true
      return 0
    fi
    sleep 0.1
  done
  # Terminating the launchctl client does not terminate the service it has
  # already asked launchd to start. It only bounds maintenance recovery.
  kill -TERM "$child_pid" 2>/dev/null || true
  wait "$child_pid" 2>/dev/null || true
  return 0
}

_bootstrap_label() {
  local label="$1"
  local plist="$(_plist_path_for_label "$label")"
  if [ ! -f "$plist" ]; then
    return 0
  fi
  # `stop` disables before bootout to prevent KeepAlive respawn. Re-enable
  # before bootstrap; bootstrapping a disabled label is rejected by launchd.
  launchctl enable "${GUI_DOMAIN}/${label}" >/dev/null 2>&1 || true
  launchctl bootstrap "${GUI_DOMAIN}" "$plist" >/dev/null 2>&1 || true
  case "$label" in
    com.weather.daily_scan|com.weather.q_calibrated_writeback_hourly|com.weather.archive_db_auto_guard|com.weather.archive_db_governance_offpeak)
      return 0
      ;;
  esac
  _bounded_kickstart "${GUI_DOMAIN}/${label}"
}

_bounded_launchctl_client() {
  local child_pid=""
  launchctl "$@" >/dev/null 2>&1 &
  child_pid=$!
  for _ in $(seq 1 30); do
    if ! kill -0 "$child_pid" 2>/dev/null; then
      wait "$child_pid" 2>/dev/null || true
      child_pid=""
      break
    fi
    sleep 0.1
  done
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
}

_bootout_label() {
  local label="$1"
  # launchctl clients can hang while a KeepAlive job is stopping.  A
  # maintenance window must have a finite pause deadline; terminating the
  # client does not undo a stop/bootout request already delivered to launchd.
  # Disable first so KeepAlive cannot race the bounded bootout and respawn a
  # writer while quiescence is being proven. `start` re-enables every label.
  _bounded_launchctl_client disable "${GUI_DOMAIN}/${label}"
  _bounded_launchctl_client stop "${GUI_DOMAIN}/${label}"
  _bounded_launchctl_client bootout "${GUI_DOMAIN}/${label}"
}

_retire_target_realization_worker_children() {
  local pattern="$ROOT/scan_weather_market.py.*--auto-trade-job-worker"
  local pids=""
  local pid=""

  # The LaunchAgent is a supervising shell.  A child created before TERM
  # forwarding was deployed can outlive launchctl bootout and keep archive.db
  # write-capable.  `weatherctl stop` is the maintenance boundary for this
  # repository, so it must not report success while that governed child lives.
  for _ in $(seq 1 20); do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] && return 0
    sleep 0.1
  done
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    kill -TERM $pids 2>/dev/null || true
  fi
  for _ in $(seq 1 50); do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] && return 0
    sleep 0.1
  done
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    for pid in $pids; do
      case "$(ps -p "$pid" -o command= 2>/dev/null || true)" in
        *"$ROOT/scan_weather_market.py"*"--auto-trade-job-worker"*)
          kill -KILL "$pid" 2>/dev/null || true
          ;;
      esac
    done
  fi
  sleep 0.1
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "error: governed target-realization worker survived weatherctl stop" >&2
    return 1
  fi
  return 0
}

_governed_writer_child_pids() {
  local pattern=""
  local pid=""
  local cwd=""
  local -a patterns=(
    "$ROOT/scripts/market_ws_listener.py"
    "$ROOT/scripts/market_ws_listener.sh"
    "$ROOT/scripts/market_refresh_1m.sh"
    "$ROOT/scripts/market_refresh_frequent.sh"
    "$ROOT/scripts/market_refresh_urgent.sh"
    "$ROOT/scripts/model_scan.sh"
    "$ROOT/scripts/model_scan_hourly.sh"
    "$ROOT/scripts/task_queue_worker.py"
    "$ROOT/scripts/weather_strategy_batch.py"
    "$ROOT/scripts/discover_weather_markets_fast.py"
    "$ROOT/scripts/q_calibrated_writeback_hourly.sh"
    "$ROOT/scripts/daily_scan_and_settlement.sh"
    "$ROOT/scripts/archive_db_auto_guard.py"
    "$ROOT/scripts/archive_db_rebuild_build.py"
    "$ROOT/scripts/db_maintenance.sh"
    "sqlite3 (data/archive[.]db|$ROOT/data/archive[.]db).*[.]backup"
    "$ROOT/scripts/trade_engine_loop.sh"
    "$ROOT/scan_weather_market.py"
    "$ROOT/ui/server.py"
  )
  for pattern in "${patterns[@]}"; do
    pgrep -f "$pattern" 2>/dev/null || true
  done
  # The module-form UI command has no repo path in argv.  Only claim it when
  # its current working directory is this repository.
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    if [ "$cwd" = "$ROOT" ] || [[ "$cwd" == "$ROOT/"* ]]; then
      printf '%s\n' "$pid"
    fi
  done < <(pgrep -f '[Pp]ython.*-m ui[.]server' 2>/dev/null || true)
}

_retire_governed_writer_children() {
  local pids=""
  local pid=""
  # launchctl bootout is asynchronous and several historical wrappers did not
  # forward TERM.  Give them a short natural exit window, then retire only
  # processes whose argv/cwd proves ownership by this repository.
  for _ in $(seq 1 20); do
    pids="$(_governed_writer_child_pids | sort -u | xargs 2>/dev/null || true)"
    [ -z "$pids" ] && return 0
    sleep 0.1
  done
  pids="$(_governed_writer_child_pids | sort -u | xargs 2>/dev/null || true)"
  [ -z "$pids" ] || kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 1 50); do
    pids="$(_governed_writer_child_pids | sort -u | xargs 2>/dev/null || true)"
    [ -z "$pids" ] && return 0
    sleep 0.1
  done
  pids="$(_governed_writer_child_pids | sort -u | xargs 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    for pid in $pids; do
      kill -KILL "$pid" 2>/dev/null || true
    done
  fi
  sleep 0.1
  pids="$(_governed_writer_child_pids | sort -u | xargs 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "error: governed archive writer children survived weatherctl stop: $pids" >&2
    return 1
  fi
  return 0
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
    echo "[weather processes]"
    ps -axo pid,ppid,ni,pcpu,pmem,etime,stat,command \
      | /usr/bin/grep -F "$ROOT/" \
      | /usr/bin/grep -E "scan_weather_market.py|ui/server.py|feedback_loop.py|feedback_loop_hourly.sh|auto_trade_worker_frequent.sh|market_ws_listener.py|market_ws_listener.sh|market_refresh_frequent.sh|market_refresh_urgent.sh|market_refresh_1m.sh|model_scan.sh|q_calibrated_writeback_hourly.sh|trade_engine_loop.sh" \
      | /usr/bin/grep -v "/usr/bin/grep" || true
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
      "$ROOT/data/logs/trade_engine.log" \
      "$ROOT/data/logs/trade_engine.out.log" \
      "$ROOT/data/logs/trade_engine.err.log" \
      "$ROOT/data/logs/auto_trade_watcher.out.log" \
      "$ROOT/data/logs/trade_sync_loop.out.log" \
      "$ROOT/data/ui_server.out.log" \
      "$ROOT/data/ui_server.err.log" \
      ; do
	      if [ -f "$p" ]; then
	        echo "$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$p")  $p"
	      fi
	    done
	    if [ "${PM_WEATHERCTL_DOCTOR_UI_PROB_AUDIT:-1}" != "0" ]; then
	      echo ""
	      echo "[ui probability consistency]"
	      RUN_DATE="${PM_UI_PROB_AUDIT_RUN_DATE:-$(TZ=America/New_York date '+%Y-%m-%d')}"
	      "$SELF" audit:ui-probability \
	        --runtime \
	        --run-date "$RUN_DATE" \
	        --limit "${PM_UI_PROB_AUDIT_LIMIT:-24}" \
	        --no-refresh \
	        --min-checked-items "${PM_UI_PROB_AUDIT_MIN_CHECKED_ITEMS:-1}" \
	        --retries "${PM_UI_PROB_AUDIT_DOCTOR_RETRIES:-1}" \
	        --retry-delay-sec "${PM_UI_PROB_AUDIT_DOCTOR_RETRY_DELAY_SEC:-1}" \
	        --timeout "${PM_UI_PROB_AUDIT_DOCTOR_TIMEOUT_SEC:-20}" \
	        --summary \
	        || true
	    fi
	    ;;
  start)
    echo "---- $(_now) start ----"
    for label in "${legacy_trade_labels[@]}"; do
      _bootout_label "$label"
    done
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
    for label in "${legacy_trade_labels[@]}"; do
      _bootout_label "$label"
    done
    _retire_target_realization_worker_children
    _retire_governed_writer_children
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
    mkdir -p "$ROOT/data"
    if ! weather_refresh_register_entrypoint "weatherctl_facts" "data_source" "${PM_WEATHERCTL_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}" "$ROOT/data/weatherctl.log" "weatherctl"; then
      echo "---- $(_now) facts_materialize skipped: coordinator role mismatch ----"
      exit 0
    fi
    if weather_refresh_skip_if_live_ssot "weatherctl_facts" "$ROOT/data/weatherctl.log" "weatherctl"; then
      echo "---- $(_now) facts_materialize skipped: live SSOT/AWC owns writes ----"
      exit 0
    fi
    weatherctl_facts_cleanup() {
      weather_refresh_db_writer_release 2>/dev/null || true
      weather_scan_lane_release 2>/dev/null || true
    }
    trap weatherctl_facts_cleanup EXIT
    if ! weather_scan_lane_acquire "weatherctl_facts" "${PM_WEATHERCTL_FACTS_LANE_WAIT_SEC:-0}" "$ROOT/data/weatherctl.log" "weatherctl"; then
      echo "---- $(_now) facts_materialize skipped: refresh lane busy ----"
      exit 0
    fi
    if ! weather_refresh_db_writer_acquire "weatherctl_facts" "${PM_WEATHERCTL_FACTS_DB_LOCK_WAIT_SEC:-0}" "$ROOT/data/weatherctl.log" "weatherctl"; then
      echo "---- $(_now) facts_materialize skipped: db_writer busy ----"
      exit 0
    fi
    echo "---- $(_now) facts_materialize (run_date=${RUN_DATE}) ----"
    "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$RUN_DATE" --write-scope "${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
    ;;
  audit:edge)
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
      PY="$(resolve_project_python "$ROOT" || true)"
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
	      PY="$(resolve_project_python "$ROOT" || true)"
	    fi
    if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
      die "missing python runtime"
    fi
	    "$PY" "$ROOT/scripts/edge_tradeability_audit.py" \
	      --db "$ROOT/data/archive.db" \
	      "$@"
	    ;;
	  audit:ui-probability)
	    PY="$ROOT/.venv/bin/python"
	    if [ ! -x "$PY" ]; then
	      PY="$(resolve_project_python "$ROOT" || true)"
	    fi
	    if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
	      die "missing python runtime"
	    fi
	    if [ "$#" -gt 0 ]; then
	      "$PY" "$ROOT/scripts/check_ui_page_probability_consistency.py" "$@"
	      exit $?
	    fi
	    RUN_DATE="${PM_UI_PROB_AUDIT_RUN_DATE:-$(TZ=America/New_York date '+%Y-%m-%d')}"
	    LIMIT="${PM_UI_PROB_AUDIT_LIMIT:-24}"
	    MIN_CHECKED="${PM_UI_PROB_AUDIT_MIN_CHECKED_ITEMS:-1}"
	    RETRIES="${PM_UI_PROB_AUDIT_RETRIES:-3}"
	    DELAY="${PM_UI_PROB_AUDIT_RETRY_DELAY_SEC:-2}"
	    TIMEOUT="${PM_UI_PROB_AUDIT_TIMEOUT_SEC:-30}"
	    status=0
	    echo "---- $(_now) ui_probability_source_force_rebuild (run_date=${RUN_DATE}, limit=${LIMIT}) ----"
	    "$PY" "$ROOT/scripts/check_ui_page_probability_consistency.py" \
	      --run-date "$RUN_DATE" \
	      --limit "$LIMIT" \
	      --force-rebuild \
	      --summary \
	      || status=$?
	    echo "---- $(_now) ui_probability_runtime_cached (run_date=${RUN_DATE}, limit=${LIMIT}) ----"
	    "$PY" "$ROOT/scripts/check_ui_page_probability_consistency.py" \
	      --runtime \
	      --run-date "$RUN_DATE" \
	      --limit "$LIMIT" \
	      --no-refresh \
	      --min-checked-items "$MIN_CHECKED" \
	      --retries "$RETRIES" \
	      --retry-delay-sec "$DELAY" \
	      --timeout "$TIMEOUT" \
	      --summary \
	      || status=$?
	    exit "$status"
	    ;;
	  pipeline:tradeability)
    PY="$ROOT/.venv/bin/python"
    if [ ! -x "$PY" ]; then
      PY="$(resolve_project_python "$ROOT" || true)"
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
      PY="$(resolve_project_python "$ROOT" || true)"
    fi
    if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
      die "missing python runtime"
    fi
    STRATEGY_BATCH_DAYS_AHEAD="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_DAYS_AHEAD:-2}"
    if ! [[ "$STRATEGY_BATCH_DAYS_AHEAD" =~ ^[0-9]+$ ]]; then
      STRATEGY_BATCH_DAYS_AHEAD="2"
    fi
    STRATEGY_BATCH_EVENT_DATES="$("$PY" - "$RUN_DATE" "$STRATEGY_BATCH_DAYS_AHEAD" <<'PY'
from datetime import date, timedelta
import sys

start = date.fromisoformat(sys.argv[1])
days_ahead = max(0, int(sys.argv[2]))
print(",".join((start + timedelta(days=i)).isoformat() for i in range(days_ahead + 1)))
PY
)"
    if [ "${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_ENABLED:-1}" = "1" ]; then
      "$PY" "$ROOT/scripts/weather_strategy_batch.py" \
        --event-date "${STRATEGY_BATCH_EVENT_DATES}" \
        --max-events "${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_MAX_EVENTS:-3}" \
        --recommendation "${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_RECOMMENDATIONS:-target_realization_order_intent,dry_run_order_intent,priority_tail_no_reverse_edge,paper_trade_candidate,strict_edge_candidate_review,probe_candidate,settlement_hold_review}" \
        --focus-lane "${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_FOCUS_LANES:-tail_no_reverse_edge,secondary_edge,d0_physical_or_settlement,major_settlement}" \
        --resource-focus-policy "${PM_RESOURCE_FOCUS_POLICY:-warn}" \
        --min-entry-edge "${PM_WEATHER_STRATEGY_BATCH_MIN_ENTRY_EDGE:-0.075}" \
        --source weatherctl_strategy_batch \
        --priority "${PM_AUTO_TRADE_JOB_PRIORITY_ENTRY:-120}" \
        --enqueue \
        --compact || true
    else
      "$PY" "$ROOT/scan_weather_market.py" --auto-trade-enqueue --date "${RUN_DATE}" --auto-trade-source weatherctl --auto-trade-priority "${PM_AUTO_TRADE_JOB_PRIORITY_ENTRY:-120}" || true
    fi
    echo ""
    ;;
  auto:smoke-test)
    _curl_long "${UI_URL}/api/ops?start=1&cmd=auto_trade_smoke_test&enable=1&intent=manual_smoke_test" || true
    echo ""
    echo "real smoke test requires adding confirm=<PM_AUTO_TRADE_SMOKE_TEST_CONFIRM_PHRASE> manually"
    echo ""
    ;;
  auto:restart-watcher)
    _curl "${UI_URL}/api/auto-trade-restart?start=1" || true
    echo ""
    ;;
  clean-locks)
    echo "---- $(_now) clean-locks ----"
    rm -rf "$ROOT/data/"*.lock "$ROOT/data/"*.lock/ 2>/dev/null || true
    rm -rf "$ROOT/data/market_refresh.lock" "$ROOT/data/model_scan.lock" "$ROOT/data/auto_trade_entry.lock" "$ROOT/data/auto_trade_worker.lock" "$ROOT/data/trade_sync_loop.lock" "$ROOT/data/trade_engine.lock" "$ROOT/data/facts_materialize.lock" 2>/dev/null || true
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
      "$ROOT/scripts/auto_trade_worker_frequent.sh" \
      "$ROOT/scripts/trade_sync_loop.sh" \
      "$ROOT/scripts/trade_engine_loop.sh" \
      "$ROOT/scripts/market_refresh_1m.sh" \
      "$ROOT/scripts/market_refresh_frequent.sh" \
      "$ROOT/scripts/market_refresh_urgent.sh" \
      "$ROOT/scripts/model_scan.sh" \
      "$ROOT/scripts/q_calibrated_writeback_hourly.sh" \
      "$ROOT/scripts/feedback_loop_hourly.sh" \
      "$ROOT/scripts/feedback_loop.py" \
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
  tail:ws)
    tail -n 200 -f "$ROOT/data/market_ws.log"
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
  tail:engine)
    tail -n 200 -f "$ROOT/data/logs/trade_engine.log"
    ;;
  tail:worker)
    tail -n 200 -f "$ROOT/data/logs/trade_engine.log"
    ;;
  tail:sync)
    tail -n 200 -f "$ROOT/data/logs/trade_engine.log"
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
