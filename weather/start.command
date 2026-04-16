#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
fi

PORT=3002
LOG="$ROOT/data/server.log"
PIDFILE="$ROOT/data/server.pid"
HEALTH_URL="http://127.0.0.1:${PORT}/api/latest"
MAX_RETRIES=40
SLEEP_SEC=0.5
AUTO_SCAN_BLOCKING="${AUTO_SCAN_BLOCKING:-1}"
LOCK_DIR="$ROOT/data/start.lock"
SCAN_STATE_PATH="$ROOT/data/scan_state.json"
PM_START_VALIDATE_MIN_RUNS="${PM_START_VALIDATE_MIN_RUNS:-1}"
PM_START_VALIDATE_INCLUDE_HISTORY="${PM_START_VALIDATE_INCLUDE_HISTORY:-0}"
PM_START_VALIDATE_TIMEOUT_SEC="${PM_START_VALIDATE_TIMEOUT_SEC:-6}"
PM_START_OPEN_BROWSER="${PM_START_OPEN_BROWSER:-1}"
PM_START_CLEAR_CACHE="${PM_START_CLEAR_CACHE:-1}"
PM_SCAN_HEARTBEAT_SEC="${PM_SCAN_HEARTBEAT_SEC:-10}"
PM_SCAN_HEARTBEAT_SHOW_LOG="${PM_SCAN_HEARTBEAT_SHOW_LOG:-1}"
PM_SCAN_HEARTBEAT_LINES="${PM_SCAN_HEARTBEAT_LINES:-1}"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="python3"
fi
SCAN_DAYS_ARG="--days-ahead -1"

_facts_materialize_window_from_scan() {
  local run_date="${1:-}"
  local days_arg="${2:-}"
  local include_yesterday="${3:-0}"
  "$PY" - "$ROOT/data/archive.db" "$run_date" "$days_arg" "$include_yesterday" <<'PY'
import sqlite3
import sys
from datetime import date, timedelta

db_path, run_date_raw, days_arg_raw, include_yesterday_raw = sys.argv[1:5]
run_date_text = str(run_date_raw or "").strip().split("T", 1)[0]
try:
    run_day = date.fromisoformat(run_date_text)
except Exception:
    print("")
    print("1")
    raise SystemExit(0)

include_yesterday = str(include_yesterday_raw or "").strip().lower() in {"1", "true", "yes", "on"}
days_ahead = 0
parts = str(days_arg_raw or "").strip().split()
if len(parts) >= 2:
    try:
        days_ahead = int(parts[1])
    except Exception:
        days_ahead = 0

end_day = run_day + timedelta(days=max(0, days_ahead))
if days_ahead < 0:
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT MAX(event_date)
            FROM (
              SELECT event_date FROM model_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM market_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM event_runs WHERE run_date = ?
            )
            """,
            (run_date_text, run_date_text, run_date_text),
        ).fetchone()
        candidate_text = str((row[0] if row else "") or "").strip().split("T", 1)[0]
        if candidate_text:
            candidate_day = date.fromisoformat(candidate_text)
            if candidate_day > end_day:
                end_day = candidate_day
    except Exception:
        pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

start_day = run_day - timedelta(days=1) if include_yesterday else run_day
if end_day < start_day:
    end_day = start_day
days = max(1, (end_day - start_day).days + 1)
print(end_day.isoformat())
print(days)
PY
}

is_launchd() {
  local ppid
  ppid="$(ps -o ppid= -p $$ 2>/dev/null | tr -d ' ')"
  [ "${ppid:-}" = "1" ]
}

port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:$PORT >/dev/null 2>&1
    return $?
  fi
  return 1
}

now_text() {
  date +"%Y-%m-%d %H:%M:%S"
}

format_elapsed() {
  local seconds="$1"
  local minutes=$((seconds / 60))
  local remain=$((seconds % 60))
  printf "%02d:%02d" "$minutes" "$remain"
}

write_scan_state() {
  "$PY" - "$SCAN_STATE_PATH" "$1" "$2" "$3" "$4" "$5" <<'PY'
import json, sys
path, running, started_at, ended_at, exit_code, pid = sys.argv[1:]
state = {
    "running": running == "1",
    "started_at": started_at,
    "ended_at": ended_at,
    "exit_code": None if exit_code == "" else int(exit_code),
    "pid": None if pid == "" else int(pid),
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(state, f)
PY
}

monitor_scan() {
  local pid="$1"
  local log_path="$2"
  local start_ts
  start_ts="$(date +%s)"
  local last_line=""
  echo "Scan heartbeat: every ${PM_SCAN_HEARTBEAT_SEC}s (pid=$pid)"
  while kill -0 "$pid" 2>/dev/null; do
    sleep "$PM_SCAN_HEARTBEAT_SEC"
    local now
    now="$(date +%s)"
    local elapsed
    elapsed="$((now - start_ts))"
    local elapsed_text
    elapsed_text="$(format_elapsed "$elapsed")"
    if [ "$PM_SCAN_HEARTBEAT_SHOW_LOG" != "0" ] && [ -s "$log_path" ]; then
      local line
      line="$(tail -n "$PM_SCAN_HEARTBEAT_LINES" "$log_path" | tr -d '\r')"
      if [ "$line" != "$last_line" ]; then
        echo "Scan running ${elapsed_text} · ${line}"
        last_line="$line"
      else
        echo "Scan running ${elapsed_text} · no new log"
      fi
    else
      echo "Scan running ${elapsed_text}"
    fi
  done
}

clear_cache() {
  if [ "$PM_START_CLEAR_CACHE" = "0" ]; then
    return
  fi
  echo "Clearing runtime cache..."
  rm -f "$ROOT/data/scan.pid" "$ROOT/data/scan_state.json" || true
  rm -rf "$ROOT/data/model_scan.lock" || true
  : > "$ROOT/data/scan.log"
  : > "$ROOT/data/model_scan.log"
}

# Pick up macOS system proxy settings only when env proxy is not provided.
HAS_PROXY_ENV=0
if [ -n "${HTTP_PROXY:-}" ] || [ -n "${HTTPS_PROXY:-}" ] || [ -n "${ALL_PROXY:-}" ]; then
  HAS_PROXY_ENV=1
fi
if [ "$HAS_PROXY_ENV" = "0" ] && [ -n "${PM_PROXY_PORTS:-}" ]; then
  FIRST_PORT="$(echo "$PM_PROXY_PORTS" | tr ', ' '\n' | awk 'NF{print $1; exit}')"
  if [ -n "$FIRST_PORT" ]; then
    export HTTP_PROXY="http://127.0.0.1:$FIRST_PORT"
    export HTTPS_PROXY="http://127.0.0.1:$FIRST_PORT"
    HAS_PROXY_ENV=1
  fi
fi
if [ "$HAS_PROXY_ENV" = "0" ] && command -v scutil >/dev/null 2>&1; then
  PROXY_INFO="$(scutil --proxy)"
  HTTP_ENABLE="$(echo "$PROXY_INFO" | awk '/HTTPEnable/{print $3}')"
  HTTPS_ENABLE="$(echo "$PROXY_INFO" | awk '/HTTPSEnable/{print $3}')"
  SOCKS_ENABLE="$(echo "$PROXY_INFO" | awk '/SOCKSEnable/{print $3}')"
  HTTP_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/HTTPProxy/{print $3}')"
  HTTP_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/HTTPPort/{print $3}')"
  HTTPS_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/HTTPSProxy/{print $3}')"
  HTTPS_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/HTTPSPort/{print $3}')"
  SOCKS_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/SOCKSProxy/{print $3}')"
  SOCKS_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/SOCKSPort/{print $3}')"
  if [ "$HTTP_ENABLE" = "1" ] && [ -n "$HTTP_PROXY_HOST" ] && [ -n "$HTTP_PROXY_PORT" ]; then
    export HTTP_PROXY="http://$HTTP_PROXY_HOST:$HTTP_PROXY_PORT"
  fi
  if [ "$HTTPS_ENABLE" = "1" ] && [ -n "$HTTPS_PROXY_HOST" ] && [ -n "$HTTPS_PROXY_PORT" ]; then
    export HTTPS_PROXY="http://$HTTPS_PROXY_HOST:$HTTPS_PROXY_PORT"
  fi
  if [ "$SOCKS_ENABLE" = "1" ] && [ -n "$SOCKS_PROXY_HOST" ] && [ -n "$SOCKS_PROXY_PORT" ]; then
    export ALL_PROXY="socks5://$SOCKS_PROXY_HOST:$SOCKS_PROXY_PORT"
  fi
fi

mkdir -p "$ROOT/data"
: "${PM_SCAN_ALLOW_INSECURE_SSL:=1}"
# Gamma request tuning (timeout/retries/backoff) to reduce flaky network failures.
: "${PM_SCAN_GAMMA_TIMEOUT:=30}"
: "${PM_SCAN_GAMMA_RETRIES:=8}"
: "${PM_SCAN_GAMMA_BACKOFF:=1.5}"
: "${PM_SCAN_GAMMA_MAX_PAGES:=200}"
export PM_SCAN_GAMMA_TIMEOUT PM_SCAN_GAMMA_RETRIES PM_SCAN_GAMMA_BACKOFF PM_SCAN_GAMMA_MAX_PAGES

# Force proxy for Polymarket/Gamma by default.
FORCE_PROXY_HOSTS_DEFAULT="polymarket.com,gamma-api.polymarket.com,clob.polymarket.com"
if [ -z "${PM_SCAN_FORCE_PROXY_HOSTS:-}" ]; then
  export PM_SCAN_FORCE_PROXY_HOSTS="$FORCE_PROXY_HOSTS_DEFAULT"
else
  export PM_SCAN_FORCE_PROXY_HOSTS="${PM_SCAN_FORCE_PROXY_HOSTS},${FORCE_PROXY_HOSTS_DEFAULT}"
fi

stop_old() {
  if [ -f "$PIDFILE" ]; then
    OLD_PID="$(cat "$PIDFILE" || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "Stopping previous server PID $OLD_PID"
      kill "$OLD_PID" || true
      sleep 0.5
    fi
    rm -f "$PIDFILE"
  fi
  if [ -f "$ROOT/data/scan.pid" ]; then
    OLD_SCAN_PID="$(cat "$ROOT/data/scan.pid" || true)"
    if [ -n "${OLD_SCAN_PID:-}" ] && kill -0 "$OLD_SCAN_PID" 2>/dev/null; then
      echo "Stopping previous scan PID $OLD_SCAN_PID"
      kill "$OLD_SCAN_PID" || true
      sleep 0.5
    fi
    rm -f "$ROOT/data/scan.pid"
    write_scan_state "0" "" "$(now_text)" "-1" ""
  fi
  if command -v lsof >/dev/null 2>&1; then
    PIDS="$(lsof -ti tcp:$PORT || true)"
    if [ -n "$PIDS" ]; then
      echo "Port $PORT is in use. Stopping: $PIDS"
      kill $PIDS || true
    fi
  elif command -v pgrep >/dev/null 2>&1; then
    PIDS="$(pgrep -f "$ROOT/ui/server.py" || true)"
    if [ -n "$PIDS" ]; then
      echo "Stopping server.py process: $PIDS"
      kill $PIDS || true
    fi
  fi
}

wait_port_free() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  for _ in $(seq 1 10); do
    if ! lsof -ti tcp:$PORT >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

health_check() {
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    return 1
  fi
  "$PY" - "$HEALTH_URL" <<'PY' || return 1
import json, sys, urllib.request
url = sys.argv[1]
with urllib.request.urlopen(url, timeout=2) as resp:
    json.load(resp)
PY
  return 0
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Another start.command is already running. Exit."
  exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

if is_launchd && port_in_use; then
  echo "Server already running. Launchd mode will stay alive without restarting."
  while true; do sleep 60; done
fi

stop_old
clear_cache
if ! wait_port_free; then
  echo "Port $PORT is still in use after stop attempt."
fi

mkdir -p "$ROOT/data"
SCAN_LOG="$ROOT/data/scan.log"
touch "$SCAN_LOG"
INSECURE_ARG=""
if [ "${PM_SCAN_INSECURE:-0}" = "1" ]; then
  INSECURE_ARG="--insecure"
fi
if [ "$AUTO_SCAN_BLOCKING" != "0" ]; then
  echo "Running scan before starting server... (log: $SCAN_LOG)"
  started_at="$(now_text)"
  PYTHONUNBUFFERED=1 "$PY" "$ROOT/scan_weather_market.py" --scan-weather-today $SCAN_DAYS_ARG --no-update-models --archive $INSECURE_ARG >> "$SCAN_LOG" 2>&1 &
  SCAN_PID=$!
  echo "$SCAN_PID" > "$ROOT/data/scan.pid"
  write_scan_state "1" "$started_at" "" "" "$SCAN_PID"
  monitor_scan "$SCAN_PID" "$SCAN_LOG"
  set +e
  wait "$SCAN_PID"
  SCAN_STATUS=$?
  set -e
  ended_at="$(now_text)"
  write_scan_state "0" "$started_at" "$ended_at" "$SCAN_STATUS" ""
  rm -f "$ROOT/data/scan.pid"
  echo "Scan exit code: $SCAN_STATUS"
  if [ "$SCAN_STATUS" -ne 0 ]; then
    echo "Scan failed; aborting server startup."
    exit "$SCAN_STATUS"
  fi
  echo "Materializing facts from snapshots..."
  RUN_DATE="$(date '+%Y-%m-%d')"
  facts_window="$(_facts_materialize_window_from_scan "$RUN_DATE" "$SCAN_DAYS_ARG" "${PM_SCAN_INCLUDE_YESTERDAY:-0}")"
  FACTS_END_DATE="$(printf '%s\n' "$facts_window" | sed -n '1p')"
  FACTS_DAYS="$(printf '%s\n' "$facts_window" | sed -n '2p')"
  if [ -z "${FACTS_END_DATE:-}" ] || ! [[ "${FACTS_DAYS:-}" =~ ^[0-9]+$ ]]; then
    FACTS_END_DATE="$RUN_DATE"
    FACTS_DAYS=1
  fi
  "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$FACTS_END_DATE" --facts-days "$FACTS_DAYS" >> "$SCAN_LOG" 2>&1 || true
else
  echo "Starting scan in background... (log: $SCAN_LOG)"
  (
    started_at="$(now_text)"
    PYTHONUNBUFFERED=1 "$PY" "$ROOT/scan_weather_market.py" --scan-weather-today $SCAN_DAYS_ARG --no-update-models --archive $INSECURE_ARG >> "$SCAN_LOG" 2>&1 &
    scan_pid=$!
    echo "$scan_pid" > "$ROOT/data/scan.pid"
    write_scan_state "1" "$started_at" "" "" "$scan_pid"
    set +e
    wait "$scan_pid"
    scan_status=$?
    set -e
    ended_at="$(now_text)"
    write_scan_state "0" "$started_at" "$ended_at" "$scan_status" ""
    rm -f "$ROOT/data/scan.pid"
    if [ "$scan_status" -eq 0 ]; then
      echo "Materializing facts from snapshots..." >> "$SCAN_LOG"
      RUN_DATE="$(date '+%Y-%m-%d')"
      facts_window="$(_facts_materialize_window_from_scan "$RUN_DATE" "$SCAN_DAYS_ARG" "${PM_SCAN_INCLUDE_YESTERDAY:-0}")"
      FACTS_END_DATE="$(printf '%s\n' "$facts_window" | sed -n '1p')"
      FACTS_DAYS="$(printf '%s\n' "$facts_window" | sed -n '2p')"
      if [ -z "${FACTS_END_DATE:-}" ] || ! [[ "${FACTS_DAYS:-}" =~ ^[0-9]+$ ]]; then
        FACTS_END_DATE="$RUN_DATE"
        FACTS_DAYS=1
      fi
      "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$FACTS_END_DATE" --facts-days "$FACTS_DAYS" >> "$SCAN_LOG" 2>&1 || true
    fi
  ) &
fi

echo "Starting UI server..."
nohup "$PY" "$ROOT/ui/server.py" >"$LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PIDFILE"

echo "Waiting for health check..."
READY=0
for _ in $(seq 1 "$MAX_RETRIES"); do
  if health_check; then
    READY=1
    break
  fi
  sleep "$SLEEP_SEC"
done

if [ "$READY" -ne 1 ]; then
  echo "Health check failed. Recent log:"
  tail -n 80 "$LOG" || true
  exit 1
fi

echo "Server is healthy on http://localhost:$PORT"

echo "Running startup validation..."
"$PY" - "$HEALTH_URL" "$PORT" "$PM_START_VALIDATE_MIN_RUNS" "$PM_START_VALIDATE_INCLUDE_HISTORY" "$PM_START_VALIDATE_TIMEOUT_SEC" <<'PY'
import json
import sys
import urllib.parse
import urllib.request

health_url = sys.argv[1]
port = sys.argv[2]
min_runs = int(sys.argv[3] or "0")
include_history = str(sys.argv[4] or "1").lower() not in ("0", "false", "no")
timeout = float(sys.argv[5] or "2")

def fetch_json(url):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)

try:
    meta = fetch_json(health_url)
except Exception as exc:
    print(f"Startup validation failed: {exc}")
    sys.exit(2)

latest = meta.get("date") or ""
if not latest:
    print("Latest date: — (empty)")
    if min_runs > 0:
        sys.exit(3)
    sys.exit(0)

params = {"date": latest}
if include_history:
    params["include_history"] = "1"
query = urllib.parse.urlencode(params)
facts_url = f"http://127.0.0.1:{port}/api/facts?{query}"

try:
    runs = fetch_json(facts_url)
except Exception as exc:
    if include_history:
        print(f"Startup validation warning: {exc}")
        include_history = False
        params = {"date": latest}
        query = urllib.parse.urlencode(params)
        facts_url = f"http://127.0.0.1:{port}/api/facts?{query}"
        try:
            runs = fetch_json(facts_url)
        except Exception as exc2:
            print(f"Startup validation failed: {exc2}")
            sys.exit(4)
    else:
        print(f"Startup validation failed: {exc}")
        sys.exit(4)

count = len(runs) if isinstance(runs, list) else 0
print(f"Latest date: {latest} (runs: {count}, include_history={include_history})")
if min_runs > 0 and count < min_runs:
    print(f"Startup validation failed: runs<{min_runs}")
    sys.exit(5)
PY

if [ "$PM_START_OPEN_BROWSER" != "0" ] && ! is_launchd; then
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT"
  else
    echo "Open http://127.0.0.1:$PORT in your browser"
  fi
fi

echo "Server PID: $SERVER_PID"
echo "Log: $LOG"
echo "Open http://localhost:$PORT in your browser"
echo "Open http://127.0.0.1:$PORT in your browser"

if is_launchd; then
  echo "Launchd mode: waiting on server PID $SERVER_PID"
  wait "$SERVER_PID"
fi
