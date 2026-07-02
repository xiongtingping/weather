#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
. "$ROOT/scripts/lib/load_launchctl_env.sh"
export PM_POLY_ENV_PREFLIGHT_LOG_COOLDOWN_SEC=0
load_repo_env_file "$ROOT/.env"
load_polymarket_launchctl_env

path="${PM_SHARED_POLY_ENV_PATH:-${HOME:-}/poly/.env}"
mode="$(polymarket_env_mode)"
missing=""
for key in POLY_API_KEY POLY_API_SECRET POLY_API_PASSPHRASE PRIVATE_KEY; do
  if [ -z "${!key:-}" ]; then
    missing="${missing}${missing:+,}${key}"
  fi
done

printf 'poly_env_path=%s\n' "$path"
printf 'poly_env_mode=%s\n' "$mode"
if [ -n "$missing" ]; then
  printf 'credentials=missing:%s\n' "$missing"
else
  printf 'credentials=ok\n'
fi

if polymarket_credentials_preflight; then
  printf 'preflight=ok\n'
  exit 0
fi
printf 'preflight=blocked\n'
exit 2
