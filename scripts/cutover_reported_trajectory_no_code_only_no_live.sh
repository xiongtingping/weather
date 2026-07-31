#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_DOMAIN_ENTRY="$ROOT/scripts/install_reported_trajectory_no_user_services.sh"

echo "已退役：请以当前登录用户运行 bash \"$USER_DOMAIN_ENTRY\"" >&2
exit 78
