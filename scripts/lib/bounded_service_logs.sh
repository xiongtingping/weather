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

# Halved when the previous run started being kept: two generations at 32MiB
# occupy what one at 64MiB used to, so keeping the evidence costs no disk.
WEATHER_SERVICE_LOG_MAX_BYTES="${WEATHER_SERVICE_LOG_MAX_BYTES:-33554432}"
case "$WEATHER_SERVICE_LOG_MAX_BYTES" in
  ''|*[!0-9]*) WEATHER_SERVICE_LOG_MAX_BYTES=33554432 ;;
esac
if [ "$WEATHER_SERVICE_LOG_MAX_BYTES" -lt 1048576 ] || [ "$WEATHER_SERVICE_LOG_MAX_BYTES" -gt 33554432 ]; then
  WEATHER_SERVICE_LOG_MAX_BYTES=33554432
fi
export WEATHER_SERVICE_LOG_MAX_BYTES

weather_prepare_bounded_service_logs() {
  local log_path
  for log_path in "$@"; do
    # Only touch explicit repository-local service logs.  A caller passing
    # another path fails closed instead of turning this helper into a generic
    # destructive utility.  The .prev sibling below is derived from a path
    # that has already passed this check, so it is inside the same directory.
    case "$log_path" in
      "$ROOT/data/"*.log) ;;
      *) return 1 ;;
    esac
    if [ -f "$log_path" ]; then
      # Keep the previous run's output.  These services run under launchd
      # with KeepAlive, so a crash is followed within seconds by a start that
      # used to wipe the traceback explaining it -- on 2026-08-26 several
      # restarts of the resident could not be attributed from its logs at
      # all, and a genuine crash would have been just as silent.
      #
      # Only when there is something to keep: a service that dies before
      # writing anything must not push the traceback that killed the run
      # before it out of .prev with an empty file.
      #
      # Copy, then truncate -- never rename.  launchd opens StandardOutPath
      # before this launcher runs, so its file descriptor follows the inode:
      # renaming the file would send the whole next run's output into .prev
      # and leave the live log empty forever.  Truncation keeps the inode the
      # descriptor is already pointing at.
      if [ -s "$log_path" ]; then
        cp -f "$log_path" "${log_path}.prev" 2>/dev/null || true
      fi
      : > "$log_path"
    fi
  done
  # Truncation at launch is the boundary.  Steady-state growth is bounded by
  # the resident itself (see weather_basket_yes_daemon.py), which can trim its
  # own log without imposing a limit on the database files it must also write.
}
