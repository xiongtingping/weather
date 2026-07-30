#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.weather.reported_trajectory_no"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
PAUSE="$ROOT/data/live_paused.flag"
if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0" >&2
  exit 1
fi

touch "$PAUSE"

current_config_sha="$(/usr/bin/python3 - "$ROOT" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
config = json.loads((root / "config/trading/reported_trajectory_no_v1.json").read_text())
encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(encoded).hexdigest())
PY
)"

# Make the database activation state agree with the filesystem pause before
# touching launchd. If the old service is still alive, this closes the safety
# window immediately; if SQLite cannot commit, remain paused and do not start
# a new resident process.
if ! /usr/bin/python3 - "$ROOT" <<'PY'
import sqlite3
import sys
from pathlib import Path
root = Path(sys.argv[1])
conn = sqlite3.connect(root / "data/archive.db", timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "UPDATE reported_trajectory_activation_state "
    "SET state='paused',updated_at=datetime('now') WHERE singleton=1"
)
conn.commit()
row = conn.execute(
    "SELECT state FROM reported_trajectory_activation_state WHERE singleton=1"
).fetchone()
conn.close()
raise SystemExit(0 if row and row[0] == "paused" else 1)
PY
then
  echo "reported_trajectory_no_system_resume_failed; could_not_persist_paused_activation; live pause retained" >&2
  exit 1
fi

# A pause/resume is only valid for the exact activation that was cut over.
# If the config changed while the service was down, do not bootstrap an owner
# that can only fail closed on every loop.  Hand the paused database to the
# controlled generation cutover, which validates the current AMSC/AWC policy,
# advances generation, and installs all three system services atomically.
stored_config_sha="$(/usr/bin/python3 - "$ROOT" <<'PY'
import sqlite3
import sys
from pathlib import Path
root = Path(sys.argv[1])
with sqlite3.connect(root / "data/archive.db", timeout=60) as conn:
    conn.execute("PRAGMA busy_timeout=60000")
    row = conn.execute(
        "SELECT config_sha256 FROM reported_trajectory_activation_state WHERE singleton=1"
    ).fetchone()
print(str(row[0]) if row and row[0] else "")
PY
)"
if [ "$stored_config_sha" != "$current_config_sha" ]; then
  echo "reported_trajectory_no_config_drift_detected; stored=$stored_config_sha; current=$current_config_sha; entering_controlled_generation_cutover" >&2
  exec bash "$ROOT/scripts/cutover_reported_trajectory_no_system_generation.sh"
fi

launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
launchctl enable "system/${LABEL}"

if ! /usr/bin/plutil -lint "$PLIST" >/dev/null 2>&1; then
  echo "reported_trajectory_no_system_resume_failed; invalid_plist=$PLIST; live pause retained" >&2
  /usr/bin/plutil -lint "$PLIST" >&2 || true
  exit 1
fi

# launchd can briefly retain the old job after bootout. Retry only the
# bootstrap operation; never remove the live pause until the service and the
# paused activation row have both been observed.
bootstrap_error=""
bootstrapped=0
for attempt in 1 2 3 4; do
  if bootstrap_output="$(launchctl bootstrap system "$PLIST" 2>&1)"; then
    bootstrapped=1
    break
  fi
  bootstrap_error="$bootstrap_output"
  if [ "$attempt" -lt 4 ]; then
    sleep "$attempt"
  fi
done
if [ "$bootstrapped" -ne 1 ]; then
  echo "reported_trajectory_no_system_resume_failed; bootstrap_error=${bootstrap_error:-unknown}; live pause retained" >&2
  echo "--- launchctl print ---" >&2
  launchctl print "system/${LABEL}" >&2 || true
  echo "--- launchctl list ---" >&2
  launchctl list 2>&1 | /usr/bin/grep -i "reported_trajectory_no" >&2 || true
  echo "--- daemon stderr tail ---" >&2
  /usr/bin/tail -100 "$ROOT/data/logs/reported_trajectory_no.err.log" >&2 || true
  exit 1
fi
launchctl kickstart -k "system/${LABEL}" >/dev/null 2>&1 || true

deadline=$((SECONDS + 20))
while [ "$SECONDS" -lt "$deadline" ]; do
  if launchctl print "system/${LABEL}" >/dev/null 2>&1 \
    && /usr/bin/python3 - "$ROOT" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
root = Path(sys.argv[1])
cfg = json.loads((root / "config/trading/reported_trajectory_no_v1.json").read_text())
conn = sqlite3.connect(root / "data/archive.db", timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
row = conn.execute("SELECT state,config_sha256 FROM reported_trajectory_activation_state WHERE singleton=1").fetchone()
sha = hashlib.sha256(json.dumps(cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
raise SystemExit(0 if row and row[0] == "paused" and row[1] == sha else 1)
PY
  then
    if /usr/bin/python3 - "$ROOT" <<'PY'
import sqlite3
import sys
from pathlib import Path
root = Path(sys.argv[1])
conn = sqlite3.connect(root / "data/archive.db", timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
conn.execute("UPDATE reported_trajectory_activation_state SET state='enabled',updated_at=datetime('now') WHERE singleton=1")
conn.commit()
row = conn.execute("SELECT state FROM reported_trajectory_activation_state WHERE singleton=1").fetchone()
raise SystemExit(0 if row and row[0] == "enabled" else 1)
PY
    then
      rm -f "$PAUSE"
      echo "reported_trajectory_no_system_resume_ok"
      exit 0
    fi
  fi
  sleep 0.25
done

echo "reported_trajectory_no_system_resume_failed; live pause retained" >&2
exit 1
