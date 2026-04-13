#!/bin/zsh
set -euo pipefail

ROOT_DIR="/Users/xiong/weather"
LOG_DIR="${ROOT_DIR}/data/logs"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  . "${ROOT_DIR}/.env"
  set +a
fi

PY="${ROOT_DIR}/.venv/bin/python"
if [ ! -x "${PY}" ]; then
  PY="python3"
fi

echo "=== AB forward start: $(date -Iseconds) ==="
"${PY}" scripts/ab_entry_forward.py
status=$?
echo "=== AB forward end: $(date -Iseconds) status=${status} ==="
exit $status
