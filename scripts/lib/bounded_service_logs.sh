#!/bin/bash

# Source this file from a service launcher.  It bounds only the launcher's own
# local log files.
#
# It must do so by truncating those exact paths.  It must NOT use `ulimit -f`:
# RLIMIT_FSIZE is a process-wide limit that the exec'd Python/Node child
# inherits and that applies to *every* file the service writes, not just its
# log.  A previous version set `ulimit -f`, and because bash on macOS counts
# that argument in 1024-byte blocks, the resident ran with RLIMIT_FSIZE =
# 134217728.  SQLite's WAL for data/archive.db grew to exactly that size and
# then every write failed with `disk I/O error`, stalling AWC canonical ingress
# and -- before the lane was partitioned -- the ASOS argmax trading lane with
# it, for a whole event day.  Bound logs by size here; never by rlimit.

WEATHER_SERVICE_LOG_MAX_BYTES="${WEATHER_SERVICE_LOG_MAX_BYTES:-67108864}"
case "$WEATHER_SERVICE_LOG_MAX_BYTES" in
  ''|*[!0-9]*) WEATHER_SERVICE_LOG_MAX_BYTES=67108864 ;;
esac
if [ "$WEATHER_SERVICE_LOG_MAX_BYTES" -lt 1048576 ] || [ "$WEATHER_SERVICE_LOG_MAX_BYTES" -gt 67108864 ]; then
  WEATHER_SERVICE_LOG_MAX_BYTES=67108864
fi
export WEATHER_SERVICE_LOG_MAX_BYTES

weather_prepare_bounded_service_logs() {
  local log_path
  for log_path in "$@"; do
    # Only truncate explicit repository-local service logs.  A caller passing
    # another path fails closed instead of turning this helper into a generic
    # destructive utility.
    case "$log_path" in
      "$ROOT/data/"*.log) ;;
      *) return 1 ;;
    esac
    if [ -f "$log_path" ]; then
      : > "$log_path"
    fi
  done
  # Truncation at launch is the boundary.  Steady-state growth is bounded by
  # the resident itself (see weather_basket_yes_daemon.py), which can trim its
  # own log without imposing a limit on the database files it must also write.
}
