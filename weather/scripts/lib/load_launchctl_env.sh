#!/bin/bash

# Best-effort hydrate selected env vars from the user's launchd environment.
# This keeps background LaunchAgent jobs usable even when repo-local .env is absent.

load_selected_env_file_vars() {
  local file_path="$1"
  shift || true
  if [ ! -f "$file_path" ] || [ "$#" -eq 0 ]; then
    return 0
  fi

  local name=""
  local value=""
  while IFS= read -r -d '' name && IFS= read -r -d '' value; do
    if [ -z "${!name:-}" ] && [ -n "$value" ]; then
      export "$name=$value"
    fi
  done < <(
    bash -c '
set -euo pipefail
file_path="$1"
shift
set -a
. "$file_path"
set +a
for name in "$@"; do
  eval "value=\${$name-}"
  if [ -n "${value:-}" ]; then
    printf "%s\0%s\0" "$name" "$value"
  fi
done
' _ "$file_path" "$@"
  )
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
  local shared_env_path="${PM_SHARED_POLY_ENV_PATH:-$HOME/poly/.env}"
  load_selected_env_file_vars \
    "$shared_env_path" \
    POLY_API_KEY \
    POLY_API_SECRET \
    POLY_API_PASSPHRASE \
    POLY_PRIVATE_KEY \
    POLY_RPC_URL \
    POLY_DATA_API_HOST \
    POLY_DATA_API_USER \
    PM_FUNDER_ADDRESS \
    POLY_FUNDER_ADDRESS

  load_launchctl_env_vars \
    POLY_API_KEY \
    POLY_API_SECRET \
    POLY_API_PASSPHRASE \
    PRIVATE_KEY \
    POLY_RPC_URL \
    POLY_DATA_API_HOST \
    POLY_DATA_API_USER \
    PM_FUNDER_ADDRESS

  if [ -z "${PRIVATE_KEY:-}" ] && [ -n "${POLY_PRIVATE_KEY:-}" ]; then
    export PRIVATE_KEY="$POLY_PRIVATE_KEY"
  fi
  if [ -z "${PM_FUNDER_ADDRESS:-}" ] && [ -n "${POLY_FUNDER_ADDRESS:-}" ]; then
    export PM_FUNDER_ADDRESS="$POLY_FUNDER_ADDRESS"
  fi
}
