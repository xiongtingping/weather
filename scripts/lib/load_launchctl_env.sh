#!/bin/bash

# Best-effort hydrate selected env vars from the user's launchd environment.
# This keeps background LaunchAgent jobs usable even when repo-local .env is absent.

_dotenv_trim() {
  local value="${1:-}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

_dotenv_strip_quotes() {
  local value="${1:-}"
  if [[ "$value" == \"*\" && "$value" == *\" && ${#value} -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' && ${#value} -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

_dotenv_key_allowed() {
  local wanted="$1"
  shift || true
  local name=""
  if [ "$#" -eq 0 ]; then
    return 0
  fi
  for name in "$@"; do
    if [ "$wanted" = "$name" ]; then
      return 0
    fi
  done
  return 1
}

_dotenv_private_key_literal() {
  local value="${1:-}"
  [[ "$value" =~ ^0x[0-9A-Fa-f]{64}$ ]]
}

_launchd_proxy_host_port() {
  local proxy_url="${1:-}"
  if [ -z "$proxy_url" ] || ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  python3 - "$proxy_url" <<'PY'
import sys
from urllib.parse import urlparse

raw = str(sys.argv[1] or "").strip()
if not raw:
    raise SystemExit(0)
if "://" not in raw:
    raw = "http://" + raw
try:
    parsed = urlparse(raw)
    port = parsed.port
except Exception:
    raise SystemExit(0)
if parsed.hostname and port:
    print(f"{parsed.hostname} {port}")
PY
}

_launchd_proxy_reachable() {
  local host="${1:-}"
  local port="${2:-}"
  local timeout="${3:-0.4}"
  if [ -z "$host" ] || [ -z "$port" ] || ! [[ "$port" =~ ^[0-9]+$ ]] || ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  python3 - "$host" "$port" "$timeout" <<'PY'
import socket
import sys

host = str(sys.argv[1] or "")
try:
    port = int(sys.argv[2])
    timeout = float(sys.argv[3])
except Exception:
    raise SystemExit(1)
try:
    with socket.create_connection((host, port), timeout=max(0.05, timeout)):
        raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
}

_launchd_proxy_candidate_ports() {
  local raw=""
  local key=""
  local proxy=""
  local host_port=""
  local port=""
  {
    printf '%s\n' "${PM_PROXY_PORTS:-}" "${PM_PROXY_LOCAL_FALLBACK_PORTS:-7890,7897}"
    for key in HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy GLOBAL_AGENT_HTTP_PROXY PM_MARKET_WS_HTTP_PROXY; do
      proxy="${!key:-}"
      if [ -z "$proxy" ]; then
        continue
      fi
      host_port="$(_launchd_proxy_host_port "$proxy" 2>/dev/null || true)"
      port="${host_port##* }"
      if [[ "$port" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$port"
      fi
    done
    if command -v scutil >/dev/null 2>&1; then
      scutil --proxy 2>/dev/null | awk '/HTTPPort|HTTPSPort|SOCKSPort/ {print $3}' || true
    fi
  } | tr ', ' '\n\n' | awk 'NF && $1 ~ /^[0-9]+$/ && $1 > 0 && $1 <= 65535 && !seen[$1]++ {print $1}'
}

_launchd_export_http_proxy() {
  local proxy_url="$1"
  export HTTP_PROXY="$proxy_url"
  export HTTPS_PROXY="$proxy_url"
  export http_proxy="$proxy_url"
  export https_proxy="$proxy_url"
  export GLOBAL_AGENT_HTTP_PROXY="$proxy_url"
}

ensure_launchd_proxy_env_reachable() {
  if [[ "${PM_ENV_PROXY_REACHABILITY_GUARD:-1}" =~ ^(0|false|no|off)$ ]]; then
    return 0
  fi
  if [[ "${PM_CLOB_FORCE_DIRECT:-0}" =~ ^(1|true|yes|on)$ ]]; then
    return 0
  fi

  local current="${HTTPS_PROXY:-${HTTP_PROXY:-${ALL_PROXY:-${https_proxy:-${http_proxy:-${all_proxy:-}}}}}}"
  local host_port=""
  local host=""
  local port=""
  local candidate=""
  local replacement=""
  local proxy_url=""

  if [ -n "$current" ]; then
    host_port="$(_launchd_proxy_host_port "$current" 2>/dev/null || true)"
    host="${host_port% *}"
    port="${host_port##* }"
    if [ -z "$host" ] || [ -z "$port" ] || ! [[ "$port" =~ ^[0-9]+$ ]]; then
      return 0
    fi
    case "$host" in
      127.0.0.1|localhost|::1) ;;
      *) return 0 ;;
    esac
    if _launchd_proxy_reachable "$host" "$port"; then
      _launchd_export_http_proxy "$current"
      return 0
    fi
    while IFS= read -r candidate; do
      [ -z "$candidate" ] && continue
      [ "$candidate" = "$port" ] && continue
      if _launchd_proxy_reachable "$host" "$candidate"; then
        replacement="$candidate"
        break
      fi
    done < <(_launchd_proxy_candidate_ports)
    if [ -n "$replacement" ]; then
      proxy_url="http://$host:$replacement"
      _launchd_export_http_proxy "$proxy_url"
      if [[ "${PM_ENV_PROXY_GUARD_LOG:-1}" =~ ^(1|true|yes|on)$ ]]; then
        echo "[env_loader] repaired local proxy port ${port} -> ${replacement} for launchd env" >&2
      fi
    elif [[ "${PM_ENV_PROXY_GUARD_LOG:-1}" =~ ^(1|true|yes|on)$ ]]; then
      echo "[env_loader] local proxy port ${port} unreachable; no reachable fallback found" >&2
    fi
    return 0
  fi

  if [[ "${PM_CLOB_FORCE_PROXY:-0}" =~ ^(1|true|yes|on)$ ]]; then
    while IFS= read -r candidate; do
      [ -z "$candidate" ] && continue
      if _launchd_proxy_reachable "127.0.0.1" "$candidate"; then
        proxy_url="http://127.0.0.1:$candidate"
        _launchd_export_http_proxy "$proxy_url"
        if [[ "${PM_ENV_PROXY_GUARD_LOG:-1}" =~ ^(1|true|yes|on)$ ]]; then
          echo "[env_loader] selected local proxy port ${candidate} for launchd env" >&2
        fi
        break
      fi
    done < <(_launchd_proxy_candidate_ports)
  fi
}

load_env_file_vars_safe() {
  local file_path="$1"
  local override="${2:-0}"
  shift 2 || true
  if [ ! -f "$file_path" ]; then
    return 0
  fi

  local line=""
  local trimmed=""
  local key=""
  local value=""
  local invalid_count=0
  while IFS= read -r line || [ -n "$line" ]; do
    trimmed="$line"
    trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    if [ -z "$trimmed" ] || [[ "$trimmed" == \#* ]]; then
      continue
    fi
    if [[ "$trimmed" == export\ * ]]; then
      trimmed="${trimmed#export }"
      trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
      trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    fi
    if [[ ! "$trimmed" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      if _dotenv_private_key_literal "$trimmed" && _dotenv_key_allowed "PRIVATE_KEY" "$@"; then
        if [ "$override" = "1" ] || [ -z "${PRIVATE_KEY:-}" ]; then
          export "PRIVATE_KEY=$trimmed"
        fi
        continue
      fi
      invalid_count=$((invalid_count + 1))
      continue
    fi
    key="${trimmed%%=*}"
    value="${trimmed#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == \"*\" && "$value" == *\" && ${#value} -ge 2 ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' && ${#value} -ge 2 ]]; then
      value="${value:1:${#value}-2}"
    fi
    if ! _dotenv_key_allowed "$key" "$@"; then
      continue
    fi
    if [ "$override" = "1" ] || [ -z "${!key:-}" ]; then
      export "$key=$value"
    fi
  done < "$file_path"

  if [ "$invalid_count" -gt 0 ]; then
    echo "[env_loader] ignored ${invalid_count} invalid dotenv lines from ${file_path}" >&2
  fi
}

load_repo_env_file() {
  local file_path="$1"
  load_env_file_vars_safe "$file_path" 1
  ensure_launchd_proxy_env_reachable
}

load_selected_env_file_vars() {
  local file_path="$1"
  shift || true
  load_env_file_vars_safe "$file_path" 0 "$@"
}

load_selected_env_file_vars_override() {
  local file_path="$1"
  shift || true
  load_env_file_vars_safe "$file_path" 1 "$@"
}

load_launchctl_env_vars() {
  if ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi

  local name=""
  local value=""
  for name in "$@"; do
    if [ -n "${!name:-}" ]; then
      continue
    fi
    value="$(launchctl getenv "$name" 2>/dev/null || true)"
    if [ -n "$value" ]; then
      export "$name=$value"
    fi
  done
}

load_polymarket_launchctl_env() {
  local home_dir="${HOME:-}"
  if [ -z "$home_dir" ]; then
    home_dir="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
  fi
  if [ -z "$home_dir" ]; then
    home_dir="/root"
  fi
  local shared_env_path="${PM_SHARED_POLY_ENV_PATH:-$home_dir/poly/.env}"
  export PM_SHARED_POLY_ENV_PATH="$shared_env_path"
  load_selected_env_file_vars_override \
    "$shared_env_path" \
    POLY_API_KEY \
    POLY_API_SECRET \
    POLY_API_PASSPHRASE \
    POLY_PRIVATE_KEY \
    PRIVATE_KEY \
    POLY_RPC_URL \
    POLY_DATA_API_HOST \
    POLY_DATA_API_USER \
    PM_FUNDER_ADDRESS \
    POLY_FUNDER_ADDRESS \
    PM_SIGNATURE_TYPE \
    SIGNATURE_TYPE

  if [[ "${PM_ALLOW_LAUNCHCTL_POLY_CREDENTIALS:-0}" =~ ^(1|true|yes|on)$ ]]; then
    load_launchctl_env_vars \
      POLY_API_KEY \
      POLY_API_SECRET \
      POLY_API_PASSPHRASE \
      PRIVATE_KEY
  fi
  load_launchctl_env_vars \
    POLY_RPC_URL \
    POLY_DATA_API_HOST \
    POLY_DATA_API_USER \
    PM_FUNDER_ADDRESS \
    PM_SIGNATURE_TYPE \
    SIGNATURE_TYPE

  if [ -z "${PRIVATE_KEY:-}" ] && [ -n "${POLY_PRIVATE_KEY:-}" ]; then
    export PRIVATE_KEY="$POLY_PRIVATE_KEY"
  fi
  if [ -z "${PM_FUNDER_ADDRESS:-}" ] && [ -n "${POLY_FUNDER_ADDRESS:-}" ]; then
    export PM_FUNDER_ADDRESS="$POLY_FUNDER_ADDRESS"
  fi
}

polymarket_env_mode() {
  local path="${PM_SHARED_POLY_ENV_PATH:-${HOME:-}/poly/.env}"
  if [ ! -f "$path" ]; then
    echo "missing"
    return 0
  fi
  if stat -f '%Lp' "$path" >/dev/null 2>&1; then
    stat -f '%Lp' "$path"
    return 0
  fi
  stat -c '%a' "$path" 2>/dev/null || echo "unknown"
}

polymarket_credentials_preflight() {
  local path="${PM_SHARED_POLY_ENV_PATH:-${HOME:-}/poly/.env}"
  local mode=""
  local missing=""
  local key=""
  local messages=()
  local should_log="1"
  local cooldown="${PM_POLY_ENV_PREFLIGHT_LOG_COOLDOWN_SEC:-300}"
  local stamp=""
  if ! [[ "$cooldown" =~ ^[0-9]+$ ]]; then
    cooldown="300"
  fi
  if [ "$cooldown" -gt 0 ]; then
    stamp="${TMPDIR:-/tmp}/weather_poly_env_preflight_${UID:-$(id -u)}.stamp"
    if [ -f "$stamp" ]; then
      local now_ts=""
      local stamp_ts=""
      now_ts="$(date +%s 2>/dev/null || echo 0)"
      if stat -f '%m' "$stamp" >/dev/null 2>&1; then
        stamp_ts="$(stat -f '%m' "$stamp" 2>/dev/null || echo 0)"
      else
        stamp_ts="$(stat -c '%Y' "$stamp" 2>/dev/null || echo 0)"
      fi
      if [[ "$now_ts" =~ ^[0-9]+$ ]] && [[ "$stamp_ts" =~ ^[0-9]+$ ]] && [ $((now_ts - stamp_ts)) -lt "$cooldown" ]; then
        should_log="0"
      fi
    fi
  fi
  mode="$(polymarket_env_mode)"
  if [ "$mode" = "missing" ]; then
    messages+=("[poly_env] missing credential file: $path")
  elif [[ "$mode" =~ ^[0-9]+$ ]] && [ $((10#$mode % 100)) -ne 0 ]; then
    messages+=("[poly_env] insecure credential file mode=$mode path=$path; expected 600 or stricter")
  fi
  for key in POLY_API_KEY POLY_API_SECRET POLY_API_PASSPHRASE PRIVATE_KEY; do
    if [ -z "${!key:-}" ]; then
      missing="${missing}${missing:+,}$key"
    fi
  done
  if [ -n "$missing" ]; then
    messages+=("[poly_env] missing required credentials: $missing source=$path")
  fi
  if [ "${#messages[@]}" -gt 0 ] && [ "$should_log" = "1" ]; then
    printf '%s\n' "${messages[@]}" >&2
    if [ -n "$stamp" ]; then
      : > "$stamp" 2>/dev/null || true
    fi
  fi
  if [[ "${PM_AUTO_TRADE_ENABLED:-0}" =~ ^(1|true|yes|on)$ ]] \
    && ! [[ "${PM_AUTO_TRADE_DRY_RUN:-1}" =~ ^(1|true|yes|on)$ ]] \
    && [ -n "$missing" ]; then
    return 1
  fi
  return 0
}

wait_for_polymarket_credentials_if_required() {
  local label="${1:-poly_env}"
  local interval="${2:-60}"
  local wait_log_cooldown="${PM_POLY_ENV_WAIT_LOG_COOLDOWN_SEC:-300}"
  local wait_log_stamp=""
  if ! [[ "$interval" =~ ^[0-9]+$ ]] || [ "$interval" -lt 5 ]; then
    interval="60"
  fi
  if ! [[ "$wait_log_cooldown" =~ ^[0-9]+$ ]]; then
    wait_log_cooldown="300"
  fi
  wait_log_stamp="${TMPDIR:-/tmp}/weather_poly_env_wait_${UID:-$(id -u)}_${label//[^A-Za-z0-9_]/_}.stamp"
  while true; do
    load_polymarket_launchctl_env
    if polymarket_credentials_preflight; then
      return 0
    fi
    local log_wait="1"
    if [ "$wait_log_cooldown" -gt 0 ] && [ -f "$wait_log_stamp" ]; then
      local now_ts=""
      local stamp_ts=""
      now_ts="$(date +%s 2>/dev/null || echo 0)"
      if stat -f '%m' "$wait_log_stamp" >/dev/null 2>&1; then
        stamp_ts="$(stat -f '%m' "$wait_log_stamp" 2>/dev/null || echo 0)"
      else
        stamp_ts="$(stat -c '%Y' "$wait_log_stamp" 2>/dev/null || echo 0)"
      fi
      if [[ "$now_ts" =~ ^[0-9]+$ ]] && [[ "$stamp_ts" =~ ^[0-9]+$ ]] && [ $((now_ts - stamp_ts)) -lt "$wait_log_cooldown" ]; then
        log_wait="0"
      fi
    fi
    if [ "$log_wait" = "1" ]; then
      echo "[$label] live credentials missing; waiting ${interval}s for ${PM_SHARED_POLY_ENV_PATH:-${HOME:-}/poly/.env}" >&2
      : > "$wait_log_stamp" 2>/dev/null || true
    fi
    sleep "$interval"
  done
}
