#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec /bin/bash "$ROOT/scripts/install_clob_order_service_launchdaemon.sh"
