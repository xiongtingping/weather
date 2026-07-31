#!/bin/bash
set -euo pipefail

echo "retired: target production services now run only in gui/\$(id -u)" >&2
echo "use: bash scripts/install_reported_trajectory_no_user_services.sh" >&2
exit 78
