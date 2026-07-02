#!/bin/zsh
set -euo pipefail

ROOT_DIR="/Users/xiong/weather"
LOG_DIR="${ROOT_DIR}/data/logs"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

if [ -f "${ROOT_DIR}/scripts/lib/load_launchctl_env.sh" ]; then
  . "${ROOT_DIR}/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "${ROOT_DIR}/.env"
fi

PY="${ROOT_DIR}/.venv/bin/python"
if [ ! -x "${PY}" ]; then
  . "${ROOT_DIR}/scripts/lib/resolve_project_python.sh"
  PY="$(resolve_project_python "$ROOT_DIR" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "${PY}" ]; then
  echo "missing project python runtime" >&2
  exit 1
fi

echo "=== AB forward start: $(date -Iseconds) ==="
"${PY}" scripts/ab_entry_forward.py
status=$?
echo "=== AB forward end: $(date -Iseconds) status=${status} ==="
exit $status
