#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "root helper retired; installing the user-domain monitor without sudo"
exec bash "$ROOT/scripts/install_monitor_launchdaemon.sh"
