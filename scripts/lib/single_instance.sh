# Refuse to start a second copy of a timed sweep.
#
# launchd's StartInterval fires on the clock, never on the previous run.  A
# sweep written when it took half a minute keeps that contract only while it
# stays under its own interval; once it does not, launchd stacks copies and
# every copy competes for the same single write lock.
#
# Measured 2026-08-22: paper_trading_ledger_sweep, scheduled every fifteen
# minutes, had four copies running at once -- 15m33s, 7m53s, 5m04s and 3m55s
# in -- three holding the target SSOT read-write.  The resident's BEGIN
# IMMEDIATE was blocked 22.2 seconds behind them, which fail-closes P0 and P1
# for that whole cycle.  Nothing in the sweep noticed, because nothing was
# watching.
#
# ``shlock`` ships with macOS and documents exactly this, but it refuses a
# lock whose recorded process is already gone -- verified against a pid that
# had exited -- so a killed sweep would block its lane forever.  The check is
# written out instead: create the lock file with noclobber, which is atomic,
# and on contention ask ``kill -0`` whether the recorded holder still exists.
#
# Two starts that both find a stale lock could both reclaim it.  The window is
# the few instructions between the read and the create, and the outcome is two
# copies -- exactly today's behaviour, not worse -- so it is not worth a
# second lock to close.
#
# Usage: ``single_instance <name>`` near the top of a sweep, once the project
# directory is known.  A refused start exits 0: the next interval is the
# retry, and a stacked copy is not a fault to alert on.
single_instance() {
  local name="$1"
  local lock_dir="${TMPDIR:-/tmp}/weather-sweep-locks"
  local lock holder attempt
  mkdir -p "$lock_dir"
  lock="$lock_dir/$name.lock"
  for attempt in 1 2; do
    if ( set -o noclobber; printf '%s\n' "$$" > "$lock" ) 2>/dev/null; then
      trap 'rm -f "'"$lock"'"' EXIT
      return 0
    fi
    holder="$(tr -dc '0-9' < "$lock" 2>/dev/null)"
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name already running (pid $holder); this start is skipped" >&2
      exit 0
    fi
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name reclaiming a lock left by pid ${holder:-unknown}" >&2
    rm -f "$lock"
  done
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $name could not take its lock; this start is skipped" >&2
  exit 0
}
