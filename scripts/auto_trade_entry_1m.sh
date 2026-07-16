#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
  if ! polymarket_credentials_preflight; then
    echo "[auto_trade_entry] credential preflight failed; skip entry runner"
    exit 0
  fi
fi

# Entry authority comes from posterior/target realization contracts only.
while IFS='=' read -r legacy_runtime_key _; do
  case "$legacy_runtime_key" in
    PM_EDGE_REALIZABLE_*|PM_EDGE_TRADEABILITY_*|PM_LCB_GLOBAL_*|PM_ENTRY_USABLE_EDGE_*)
      unset "$legacy_runtime_key"
      ;;
  esac
done < <(env)

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi
. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"

mkdir -p "$ROOT/data" "$ROOT/data/logs"
LOG="$ROOT/data/logs/auto_trade_entry_1m.log"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
export PM_SMALL_CAPITAL_PILOT_CITIES="${PM_SMALL_CAPITAL_PILOT_CITIES:-chengdu}"

now_text() {
  date '+%Y-%m-%d %H:%M:%S'
}

# Avoid overlapping entry attempts.
LOCK_DIR="$ROOT/data/auto_trade_entry.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -n "${lock_pid:-}" ] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "$(now_text) [auto_entry] already running, skip (pid=$lock_pid)" >> "$LOG"
    exit 0
  fi
  echo "$(now_text) [auto_entry] stale lock cleared" >> "$LOG"
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "$(now_text) [auto_entry] unable to acquire lock, skip" >> "$LOG"
    exit 0
  }
fi
echo "$$" > "$LOCK_PID_FILE"
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(now_text) [auto_entry] python_missing" >> "$LOG"
  exit 0
fi

auto_trade_enabled="$(printf '%s' "${PM_AUTO_TRADE_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')"
auto_trade_dry_run="$(printf '%s' "${PM_AUTO_TRADE_DRY_RUN:-1}" | tr '[:upper:]' '[:lower:]')"
if [[ ! "$auto_trade_enabled" =~ ^(1|true|yes|on)$ ]] && [[ ! "$auto_trade_dry_run" =~ ^(1|true|yes|on)$ ]]; then
  echo "$(now_text) [auto_entry] disabled enabled=${PM_AUTO_TRADE_ENABLED:-0} dry_run=${PM_AUTO_TRADE_DRY_RUN:-1}; skip enqueue" >> "$LOG"
  exit 0
fi

POSTERIOR_DIRECT_PAUSE="${PM_AUTO_TRADE_ENTRY_PAUSE_FOR_POSTERIOR_TARGET_DIRECT:-1}"
if [[ "$(printf '%s' "$POSTERIOR_DIRECT_PAUSE" | tr '[:upper:]' '[:lower:]')" =~ ^(1|true|yes|on)$ ]]; then
  POSTERIOR_DIRECT_PAUSE_MIN_PROB="${PM_AUTO_TRADE_ENTRY_POSTERIOR_DIRECT_PAUSE_MIN_PROB:-0.50}"
  posterior_direct_pause_reason="$("$PY" - "$ROOT/data/archive.db" "$RUN_DATE" "$POSTERIOR_DIRECT_PAUSE_MIN_PROB" <<'PY'
import json
import sqlite3
import sys

db_path, event_date, min_prob_text = sys.argv[1:4]
try:
    min_prob = float(min_prob_text)
except Exception:
    min_prob = 0.50

try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.slug, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.event_date = ?
          AND h.slug LIKE '%chengdu%'
        ORDER BY h.fact_as_of DESC, h.id DESC
        LIMIT 1
        """,
        (event_date,),
    ).fetchone()
    if row is None:
        sys.exit(0)
    payload = json.loads(row["fact_json"] or "{}")
    selection = payload.get("posterior_target_bucket_selection") or {}
    plan = payload.get("target_realization_capital_plan") or {}
    target = str(
        selection.get("target_bucket")
        or plan.get("target_bucket")
        or payload.get("target_bucket")
        or ""
    ).strip()
    posterior = selection.get("target_probability")
    if posterior is None:
        posterior = plan.get("target_posterior")
    try:
        posterior = float(posterior)
    except Exception:
        posterior = None
    if not target or posterior is None or posterior < min_prob:
        sys.exit(0)
    active_trade = conn.execute(
        """
        SELECT 1
        FROM edge_trades
        WHERE slug = ?
          AND event_date = ?
          AND outcome = ?
          AND UPPER(COALESCE(side, '')) = 'YES'
          AND LOWER(COALESCE(state, '')) IN ('entry', 'entry_pending', 'exit_pending')
          AND COALESCE(position_size, entry_size, 0) > 0
        LIMIT 1
        """,
        (row["slug"], event_date, target),
    ).fetchone()
    active_order = conn.execute(
        """
        SELECT 1
        FROM auto_orders
        WHERE slug = ?
          AND event_date = ?
          AND outcome = ?
          AND UPPER(COALESCE(side, '')) = 'YES'
          AND LOWER(COALESCE(action, '')) = 'entry'
          AND LOWER(COALESCE(order_status, '')) IN ('live', 'open', 'pending', 'unknown', 'matched', 'filled')
        LIMIT 1
        """,
        (row["slug"], event_date, target),
    ).fetchone()
    if active_trade is None and active_order is None:
        print(f"posterior_target_direct_primary_missing target={target} posterior={posterior:.6f}")
except Exception as exc:
    print(f"posterior_target_direct_guard_error:{exc.__class__.__name__}")
finally:
    try:
        conn.close()
    except Exception:
        pass
PY
)"
  if [ -n "${posterior_direct_pause_reason:-}" ]; then
    echo "$(now_text) [auto_entry] skip broad enqueue: ${posterior_direct_pause_reason}" >> "$LOG"
    exit 0
  fi
fi

PRIORITY="${PM_AUTO_TRADE_JOB_PRIORITY_ENTRY:-120}"
ENTRY_ENQUEUE_TIMEOUT_SEC="${PM_AUTO_TRADE_ENTRY_ENQUEUE_TIMEOUT_SEC:-60}"
if ! [[ "$ENTRY_ENQUEUE_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  ENTRY_ENQUEUE_TIMEOUT_SEC="60"
fi
ENTRY_ENQUEUE_NICE="$(normalize_nice_level "${PM_AUTO_TRADE_ENTRY_ENQUEUE_NICE:-10}" 10)"
STRATEGY_BATCH_ENABLED="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_ENABLED:-1}"
STRATEGY_BATCH_MAX_EVENTS="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_MAX_EVENTS:-3}"
STRATEGY_BATCH_DAYS_AHEAD="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_DAYS_AHEAD:-2}"
if [[ "$auto_trade_enabled" =~ ^(1|true|yes|on)$ ]] && [[ ! "$auto_trade_dry_run" =~ ^(1|true|yes|on)$ ]]; then
  # Live mode must not promote dry-run-only candidates into executable jobs.
  STRATEGY_BATCH_RECOMMENDATIONS="${PM_AUTO_TRADE_ENTRY_LIVE_STRATEGY_BATCH_RECOMMENDATIONS:-target_realization_order_intent,priority_tail_no_reverse_edge,paper_trade_candidate,strict_edge_candidate_review,probe_candidate,settlement_hold_review}"
else
  STRATEGY_BATCH_RECOMMENDATIONS="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_RECOMMENDATIONS:-dry_run_order_intent,priority_tail_no_reverse_edge,paper_trade_candidate,strict_edge_candidate_review,probe_candidate,settlement_hold_review}"
fi
STRATEGY_BATCH_FOCUS_LANES="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_FOCUS_LANES:-tail_no_reverse_edge,secondary_edge,d0_physical_or_settlement,major_settlement}"
STRATEGY_BATCH_RESOURCE_FOCUS_POLICY="${PM_RESOURCE_FOCUS_POLICY:-warn}"
STRATEGY_BATCH_MIN_ENTRY_EDGE="${PM_WEATHER_STRATEGY_BATCH_MIN_ENTRY_EDGE:-0.10}"
STRATEGY_BATCH_OBSERVATION_TARGET_FOCUS="${PM_AUTO_TRADE_ENTRY_OBSERVATION_TARGET_FOCUS:-1}"
STRATEGY_BATCH_OBSERVATION_TARGET_PACKAGE="${PM_PAPER_OBSERVATION_TARGET_JSON:-$ROOT/data/reports/paper_observation_target_package_latest.json}"
STRATEGY_BATCH_FALLBACK_FULL="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_FALLBACK_FULL:-0}"
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

echo "$(now_text) [auto_entry] enqueue run_date=${RUN_DATE} dates=${STRATEGY_BATCH_EVENT_DATES} priority=${PRIORITY} timeout=${ENTRY_ENQUEUE_TIMEOUT_SEC}s nice=${ENTRY_ENQUEUE_NICE} strategy_batch=${STRATEGY_BATCH_ENABLED}" >> "$LOG"
set +e
if [ "$STRATEGY_BATCH_ENABLED" = "1" ]; then
  run_with_timeout "$ENTRY_ENQUEUE_TIMEOUT_SEC" "$LOG" nice -n "$ENTRY_ENQUEUE_NICE" "$PY" "$ROOT/scripts/weather_strategy_batch.py" \
    --event-date "$STRATEGY_BATCH_EVENT_DATES" \
    --max-events "$STRATEGY_BATCH_MAX_EVENTS" \
    --recommendation "$STRATEGY_BATCH_RECOMMENDATIONS" \
    --focus-lane "$STRATEGY_BATCH_FOCUS_LANES" \
    --resource-focus-policy "$STRATEGY_BATCH_RESOURCE_FOCUS_POLICY" \
    --min-entry-edge "$STRATEGY_BATCH_MIN_ENTRY_EDGE" \
    --pilot-city "${PM_SMALL_CAPITAL_PILOT_CITIES}" \
    --observation-target-package "$STRATEGY_BATCH_OBSERVATION_TARGET_PACKAGE" \
    $([ "$STRATEGY_BATCH_OBSERVATION_TARGET_FOCUS" = "1" ] && printf '%s\n' "--observation-target-focus") \
    --source "entry_1m_strategy_batch" \
    --priority "$PRIORITY" \
    --enqueue \
    --compact
  enqueue_rc=$?
  if [ "$enqueue_rc" -ne 0 ] && [ "$STRATEGY_BATCH_FALLBACK_FULL" = "1" ]; then
    echo "$(now_text) [auto_entry] strategy_batch_failed fallback_full=1" >> "$LOG"
    run_with_timeout "$ENTRY_ENQUEUE_TIMEOUT_SEC" "$LOG" nice -n "$ENTRY_ENQUEUE_NICE" "$PY" "$ROOT/scan_weather_market.py" \
      --auto-trade-enqueue \
      --date "$RUN_DATE" \
      --auto-trade-source "entry_1m" \
      --auto-trade-priority "$PRIORITY" \
      --write-scope "${PM_ARCHIVE_WRITE_SCOPE}"
    enqueue_rc=$?
  fi
else
  run_with_timeout "$ENTRY_ENQUEUE_TIMEOUT_SEC" "$LOG" nice -n "$ENTRY_ENQUEUE_NICE" "$PY" "$ROOT/scan_weather_market.py" \
    --auto-trade-enqueue \
    --date "$RUN_DATE" \
    --auto-trade-source "entry_1m" \
    --auto-trade-priority "$PRIORITY" \
    --write-scope "${PM_ARCHIVE_WRITE_SCOPE}"
  enqueue_rc=$?
fi
set -e
if [ "$enqueue_rc" -ne 0 ]; then
  echo "$(now_text) [auto_entry] enqueue_failed" >> "$LOG"
  exit 0
fi
