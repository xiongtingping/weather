#!/bin/bash
# Intentionally do not modify the caller's shell options (this file is sourced).

# run_with_timeout <timeout_sec> <log_path> <command...>
#
# - Runs the command in background.
# - If it is still running after timeout_sec, send TERM then KILL.
# - Always appends a timeout line to log_path (if provided) when timing out.
# - Returns:
#     - command exit code on normal completion
#     - 124 on timeout
_run_with_timeout_kill_descendants() {
  local parent_pid="${1:-}"
  local signal_name="${2:-TERM}"
  local child_pids=""
  local child_pid=""

  if [ -z "${parent_pid}" ]; then
    return 0
  fi
  child_pids="$(pgrep -P "${parent_pid}" 2>/dev/null || true)"
  if [ -z "${child_pids}" ]; then
    return 0
  fi
  for child_pid in ${child_pids}; do
    _run_with_timeout_kill_descendants "${child_pid}" "${signal_name}"
    kill -"${signal_name}" "${child_pid}" 2>/dev/null || true
  done
}

run_with_timeout() {
  local timeout_sec="${1:-}"
  local log_path="${2:-}"
  shift 2 || true

  # Capture any bash "Terminated" job messages (and our own stderr) into the log file
  # when a log path is provided. This keeps LaunchAgent .err.log clean.
  local _stderr_saved=0
  if [ -n "${log_path}" ]; then
    exec 3>&2
    exec 2>>"${log_path}"
    _stderr_saved=1
  fi

  if [ -z "${timeout_sec}" ]; then
    timeout_sec="0"
  fi

  # If no timeout requested, run directly.
  if [ "${timeout_sec}" = "0" ] || [ "${timeout_sec}" = "0.0" ]; then
    if [ -n "${log_path}" ]; then
      "$@" >>"${log_path}" 2>&1
    else
      "$@"
    fi
    local rc=$?
    if [ "${_stderr_saved}" = "1" ]; then
      exec 2>&3
      exec 3>&-
    fi
    return "${rc}"
  fi

  local start
  start="$(date +%s)"

  if [ -n "${log_path}" ]; then
    "$@" >>"${log_path}" 2>&1 &
  else
    "$@" &
  fi
  local pid=$!

  while kill -0 "${pid}" 2>/dev/null; do
    sleep 1
    local now
    now="$(date +%s)"
    if [ $((now - start)) -ge "${timeout_sec}" ]; then
      if [ -n "${log_path}" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [timeout] kill pid=${pid} after ${timeout_sec}s: $*" >>"${log_path}"
      fi
      _run_with_timeout_kill_descendants "${pid}" TERM
      kill -TERM "${pid}" 2>/dev/null || true
      sleep 2
      _run_with_timeout_kill_descendants "${pid}" KILL
      kill -KILL "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
      if [ "${_stderr_saved}" = "1" ]; then
        exec 2>&3
        exec 3>&-
      fi
      return 124
    fi
  done

  wait "${pid}"
  local rc=$?
  if [ "${_stderr_saved}" = "1" ]; then
    exec 2>&3
    exec 3>&-
  fi
  return "${rc}"
}
