# One background SSOT reader at a time, and only once the WAL is down.
#
# 2026-10-17 (user 「看板研究渲染长期让路，多节数据超期」): the research render
# and the board readings both waited for the page render to finish and then
# started in the same minute (00:17:11 / 00:17:23Z, 02:01:52 / 02:02:02Z).
# Two long readers held snapshots side by side, the WAL could not be
# truncated behind either, it passed the 384 MiB yield line, and every
# section after that yielded at once; the next page render then found the
# WAL over 256 MiB and skipped (04:19, 05:40, 06:56Z).  Precipitation,
# refusal history, SSOT capacity and truth landing went 60-100 hours stale.
#
# ``ssot_reader_turn <name> <wait_sec>`` takes the shared reader turn (a
# noclobber lock file holding the pid, reclaimed when its holder is gone, as
# single_instance does) and then waits for the WAL file to come back under
# the start line -- the resident truncates it within a minute once no reader
# holds it -- all within ``wait_sec``.  It returns 0 holding the turn, which
# is released when the script exits; otherwise it prints why and returns 1.
#
# ``ssot_wal_under <wait_sec>`` is the WAL wait alone (the page render takes
# no turn: the other two already wait for it and stop when it starts).
SSOT_WAL_FILE="data/bayes_basket_target_ssot.db-wal"
SSOT_WAL_START_LINE="${PM_DASHBOARD_RENDER_MAX_WAL_BYTES:-268435456}"
SSOT_READER_POLL_SEC="${PM_SSOT_READER_POLL_SEC:-15}"

ssot_wal_bytes() {
  if [ -f "$SSOT_WAL_FILE" ]; then stat -f '%z' "$SSOT_WAL_FILE"; else echo 0; fi
}

ssot_wal_under() {
  local wait_sec="$1" waited=0 bytes
  bytes="$(ssot_wal_bytes)"
  while [ "$bytes" -gt "$SSOT_WAL_START_LINE" ]; do
    if [ "$waited" -ge "$wait_sec" ]; then
      SSOT_READER_REASON="SSOT WAL ${bytes} bytes > ${SSOT_WAL_START_LINE} after waiting ${waited}s for it to be truncated"
      return 1
    fi
    sleep "$SSOT_READER_POLL_SEC"
    waited=$((waited + SSOT_READER_POLL_SEC))
    bytes="$(ssot_wal_bytes)"
  done
  if [ "$waited" -gt 0 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] waited ${waited}s for the SSOT WAL to come under ${SSOT_WAL_START_LINE} (now ${bytes})"
  fi
  return 0
}

ssot_reader_turn() {
  local name="$1" wait_sec="$2" waited=0 holder
  local lock_dir="${TMPDIR:-/tmp}/weather-sweep-locks"
  local lock="$lock_dir/ssot-background-reader.lock"
  mkdir -p "$lock_dir"
  while ! ( set -o noclobber; printf '%s %s\n' "$$" "$name" > "$lock" ) 2>/dev/null; do
    holder="$(awk '{print $1}' "$lock" 2>/dev/null | tr -dc '0-9')"
    if [ -z "$holder" ] || ! kill -0 "$holder" 2>/dev/null; then
      rm -f "$lock"
      continue
    fi
    if [ "$waited" -ge "$wait_sec" ]; then
      SSOT_READER_REASON="the SSOT reader turn is held by $(cat "$lock" 2>/dev/null) (waited ${waited}s)"
      return 1
    fi
    sleep "$SSOT_READER_POLL_SEC"
    waited=$((waited + SSOT_READER_POLL_SEC))
  done
  # Released at exit, beside whatever exit trap the script already set
  # (single_instance's lock removal).
  local previous
  previous="$(trap -p EXIT | sed -e "s/^trap -- '//" -e "s/' EXIT\$//")"
  trap "${previous:+$previous; }rm -f '$lock'" EXIT
  if [ "$waited" -gt 0 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name waited ${waited}s for the SSOT reader turn"
  fi
  local left=$((wait_sec - waited))
  [ "$left" -lt 600 ] && left=600
  ssot_wal_under "$left"
}
