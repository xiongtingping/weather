#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

. "$ROOT/scripts/lib/bounded_service_logs.sh"
weather_prepare_bounded_service_logs \
  "$ROOT/data/logs/clob_order_service.out.log" \
  "$ROOT/data/logs/clob_order_service.err.log"

if [ "${WEATHER_SERVICE_DOMAIN:-}" != "user" ]; then
  echo "[clob_order_service] user_domain_required" >&2
  exit 78
fi

if [ "${PM_TARGET_EXECUTION_MODE:-}" != "production_no_live" ]; then
  echo "[clob_order_service] production_no_live_required" >&2
  exit 78
fi

# The gateway must stay available during a managed maintenance window so its
# capabilities and durable idempotency state can be audited before exposure.
# `clob_order_service.mjs` checks this same pause file at every place-order
# admission and rejects before any idempotency write, signing, or exchange POST.
export PM_LIVE_PAUSE_FILE="${PM_LIVE_PAUSE_FILE:-$ROOT/data/live_paused.flag}"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
fi

# Preserve the launchd authority after optional credential loading.  This
# resident supplies evidence and health only; order submission remains closed.
export WEATHER_SERVICE_DOMAIN=user
export PM_LIVE_PAUSE_FILE="$ROOT/data/live_paused.flag"
export PM_TARGET_EXECUTION_MODE=production_no_live
export PM_CLOB_ORDER_SERVICE_READ_ONLY=0
# The region pre-rejection reads polymarket.com/api/geoblock -- the website's
# restriction -- and refuses before signing, so the exchange's own answer was
# never obtained.  The attempt record separates the two: the three responses
# that actually came back "Trading restricted in your region" are all
# 2026-08-07 from the US egress, and every rejection since the gate landed on
# 2026-08-09 is our own clob_region_blocked:JP with exchange_post_started
# false.  User ruling 2026-08-30, with the consequence stated first: the ASOS
# lane was exempt from the pause flag and the read-only flag above, so this
# gate was its last one and lifting it let that lane reach the exchange.
#
# That exemption is gone as of the same day, by the follow-up ruling: the
# pause flag and the read-only flag now stop every lane without exception, so
# lifting this gate no longer leaves anything ungated -- it only lets the
# exchange answer for itself once the operator lifts the pause flag.
export PM_CLOB_IGNORE_REGION_BLOCK=1
# Keep exact-token conditional balances fresh for held-YES safety checks. This
# is read-only evidence and remains bounded by the service concurrency/queue
# limits; it never authorizes an order.
export PM_CLOB_BALANCE_PREFETCH_ENABLED=1

NODE_BIN="${NODE_BIN:-/usr/local/bin/node}"
if [ ! -x "$NODE_BIN" ]; then
  NODE_BIN="$(command -v node || true)"
fi
if [ -z "${NODE_BIN:-}" ]; then
  echo "[clob_order_service] missing node runtime" >&2
  exit 1
fi

mkdir -p "$ROOT/data" "$ROOT/data/logs"
exec "$NODE_BIN" "$ROOT/scripts/clob_order_service.mjs"
