#!/bin/bash

# Source this file from a service launcher.  It deliberately limits only the
# launcher's own local log files; it does not touch SSOT, database, raw source,
# or order evidence.

WEATHER_SERVICE_LOG_MAX_BYTES="${WEATHER_SERVICE_LOG_MAX_BYTES:-67108864}"
case "$WEATHER_SERVICE_LOG_MAX_BYTES" in
  ''|*[!0-9]*) WEATHER_SERVICE_LOG_MAX_BYTES=67108864 ;;
esac
if [ "$WEATHER_SERVICE_LOG_MAX_BYTES" -lt 1048576 ] || [ "$WEATHER_SERVICE_LOG_MAX_BYTES" -gt 67108864 ]; then
  WEATHER_SERVICE_LOG_MAX_BYTES=67108864
fi
WEATHER_SERVICE_LOG_MAX_BLOCKS=$((WEATHER_SERVICE_LOG_MAX_BYTES / 512))

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
  # macOS ulimit -f is expressed in 512-byte blocks and is inherited by the
  # Python/Node child.  launchd then restarts the bounded service after a
  # SIGXFSZ rather than allowing an unbounded append.
  ulimit -f "$WEATHER_SERVICE_LOG_MAX_BLOCKS"
}
