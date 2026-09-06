#!/bin/bash
# The one way to restart the basket resident, and the reason there is only one.
#
# Restarting resets the official-capture rotation cursor.  The catalog is
# ordered (not-ready, event day, station, lead) and advances 20 scopes every
# ten minutes, so a D+2 scope sits around position 192 and a restart costs
# roughly eight to sixteen minutes before that event day is served again.
# One restart is a cost worth paying for a change that needs it; four in
# forty-five minutes is a lane that never gets served.
#
# Measured 2026-09-06: the resident was restarted at 12:07, 12:30, 12:44 and
# 12:51Z by three different sessions, each making its own change live.  The
# 2026-09-08 lane was judged once in that whole window, and the pass it did
# get drew six stations none of which exercised the change that had just
# landed.  Nobody was wrong; there was just no place for them to meet.
#
# So: every session restarts through here.  Inside the cooldown the request is
# recorded rather than performed, and the next restart -- whoever triggers it
# -- serves every request queued behind it.  That is the batching; the file is
# the meeting place.
#
# Usage:
#   scripts/restart_resident.sh "why" [--who NAME]   request a restart
#   scripts/restart_resident.sh "why" --queue-only   record it, never restart
#   scripts/restart_resident.sh --pending            what is queued, when due
#   scripts/restart_resident.sh "why" --force        restart now, records why
#
# ``--queue-only`` exists because the cooldown cannot express "this is not
# urgent".  The clock starts empty -- no restart has ever been taken through
# this script -- so the first request finds the cooldown elapsed and restarts
# at once, which is the opposite of what a session with an audit-only change
# wants when another session is mid-measurement.  Measured 2026-09-06 22:15Z:
# an audit-only field committed at 22:13 could not be registered as pending
# without costing the 09-08 lane another cursor reset.  With this flag the
# journal takes the intent and the next restart anyone makes serves it.
#
# Exit 0 when the restart happened AND when it was queued: a queued change is
# not an error, it is a change that lands on the next restart.  Exit non-zero
# only when the restart was attempted and did not take.
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"
LABEL="com.weather.weather_basket_yes"
DOMAIN="gui/$(id -u)"
STATE_DIR="$ROOT/data/runtime/resident_restart"
JOURNAL="$STATE_DIR/requests.jsonl"
LAST="$STATE_DIR/last_restart_utc"
LOCK="$STATE_DIR/.lock"
STATUS="$ROOT/data/weather_basket_yes_status.json"
COOLDOWN_SEC="${PM_RESIDENT_RESTART_COOLDOWN_SEC:-600}"

mkdir -p "$STATE_DIR"
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
epoch()   { date -u +%s; }

REASON=""; WHO="${PM_SESSION_NAME:-unknown}"; FORCE=0; PENDING_ONLY=0; QUEUE_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pending)    PENDING_ONLY=1; shift ;;
    --force)      FORCE=1; shift ;;
    --queue-only) QUEUE_ONLY=1; shift ;;
    --who)        WHO="${2:-unknown}"; shift 2 ;;
    *)            REASON="$1"; shift ;;
  esac
done
if [ "$FORCE" = "1" ] && [ "$QUEUE_ONLY" = "1" ]; then
  echo "restart_resident: --force and --queue-only contradict each other" >&2
  exit 2
fi

last_epoch() { [ -f "$LAST" ] && cat "$LAST" || echo 0; }
pending_rows() {
  [ -f "$JOURNAL" ] || { echo "  (none)"; return 0; }
  python3 - "$JOURNAL" <<'PYPEND'
import json,sys
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
open_rows=[r for r in rows if r.get("served_at_utc") is None]
if not open_rows: print("  (none)")
for r in open_rows:
    print("  %s  %-14s %s" % (r["requested_at_utc"], r.get("who","?"), r.get("reason","")))
PYPEND
}

if [ "$PENDING_ONLY" = "1" ]; then
  LE="$(last_epoch)"; NOW="$(epoch)"; DUE=$(( LE + COOLDOWN_SEC - NOW ))
  echo "last_restart_utc: $([ -f "$LAST.iso" ] && cat "$LAST.iso" || echo none)"
  if [ "$DUE" -gt 0 ]; then echo "cooldown: ${DUE}s remaining"; else echo "cooldown: elapsed, a restart may be taken now"; fi
  echo "pending requests:"; pending_rows
  exit 0
fi

[ -n "$REASON" ] || { echo "restart_resident: a reason is required" >&2; exit 2; }

# Serialise: two sessions must not restart at the same instant, and the
# journal append must not interleave.  Same noclobber-plus-kill-0 shape as
# scripts/lib/single_instance.sh, for the same reason it gives.
if ! ( set -o noclobber; echo $$ > "$LOCK" ) 2>/dev/null; then
  HOLDER="$(cat "$LOCK" 2>/dev/null || echo 0)"
  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then
    echo "restart_resident: another session holds the restart lock (pid $HOLDER); try again in a moment" >&2
    exit 3
  fi
  echo $$ > "$LOCK"
fi
trap 'rm -f "$LOCK"' EXIT

REQ_AT="$(now_utc)"
printf '{"requested_at_utc": "%s", "who": "%s", "reason": %s, "served_at_utc": null}\n' \
  "$REQ_AT" "$WHO" "$(printf '%s' "$REASON" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
  >> "$JOURNAL"

if [ "$QUEUE_ONLY" = "1" ]; then
  echo "queued (not urgent): recorded without restarting."
  echo "Whoever takes the next restart serves this request too."
  echo "  scripts/restart_resident.sh --pending      # see the queue"
  exit 0
fi

LE="$(last_epoch)"; NOW="$(epoch)"; ELAPSED=$(( NOW - LE ))
if [ "$FORCE" != "1" ] && [ "$ELAPSED" -lt "$COOLDOWN_SEC" ]; then
  echo "queued: $(( COOLDOWN_SEC - ELAPSED ))s of cooldown remain."
  echo "Your change lands on the next restart; whoever takes it serves this request too."
  echo "  scripts/restart_resident.sh --pending      # see the queue"
  echo "  scripts/restart_resident.sh \"$REASON\" --force   # only if it cannot wait"
  exit 0
fi

BEFORE="$(python3 - "$STATUS" <<'PY' 2>/dev/null || echo 0
import json,sys
try: print(json.load(open(sys.argv[1])).get("pid") or 0)
except Exception: print(0)
PY
)"
echo "restarting $LABEL (was pid $BEFORE) -- serving every queued request"
# kickstart -k is the primitive that works; targetctl.sh restart is not used
# here because its stop step times out non-zero under set -e and leaves the
# other two services down (measured twice, 2026-09-06).
launchctl kickstart -k "$DOMAIN/$LABEL"

python3 - "$STATUS" "$BEFORE" <<'PY'
import json,sys,time
status,before=sys.argv[1],int(sys.argv[2])
for _ in range(60):
    time.sleep(2)
    try: d=json.load(open(status))
    except Exception: continue
    if int(d.get("pid") or 0) not in (0,before):
        print("up: pid %s started %s" % (d.get("pid"), d.get("started_at")))
        raise SystemExit(0)
print("restart did not report a new pid within 120s", file=sys.stderr)
raise SystemExit(1)
PY

epoch > "$LAST"; now_utc > "$LAST.iso"
python3 - "$JOURNAL" "$(now_utc)" <<'PY'
import json,sys
path,served=sys.argv[1],sys.argv[2]
rows=[json.loads(l) for l in open(path) if l.strip()]
n=0
for r in rows:
    if r.get("served_at_utc") is None: r["served_at_utc"]=served; n+=1
open(path,"w").write("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows))
print("served %d queued request(s)" % n)
PY
