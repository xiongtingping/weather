#!/bin/bash
set -euo pipefail

# A long-lived Bash process may lazily read later parts of its source file.
# Never execute the mutable repository poller directly: freeze one complete,
# syntax-checked copy before the release-window delay starts.
ROOT="${AMSC_POLL_PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SOURCE="${AMSC_POLL_SOURCE_PATH:-$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.sh}"
SNAPSHOT_DIR="${AMSC_POLL_SNAPSHOT_DIR:-$ROOT/data/runtime_snapshots/amsc_zuuu_poll}"

mkdir -p "$SNAPSHOT_DIR"
SNAPSHOT="$(mktemp "$SNAPSHOT_DIR/poll.XXXXXXXX.sh")"
cleanup() {
  rm -f "$SNAPSHOT"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

snapshot_ready=0
for snapshot_attempt in 1 2 3 4 5; do
  source_sha_before="$(shasum -a 256 "$SOURCE" | awk '{print $1}')"
  cp "$SOURCE" "$SNAPSHOT"
  snapshot_sha256="$(shasum -a 256 "$SNAPSHOT" | awk '{print $1}')"
  source_sha_after="$(shasum -a 256 "$SOURCE" | awk '{print $1}')"
  if [ "$source_sha_before" = "$snapshot_sha256" ] && \
     [ "$snapshot_sha256" = "$source_sha_after" ] && \
     /bin/bash -n "$SNAPSHOT"; then
    snapshot_ready=1
    break
  fi
  sleep 0.05
done
if [ "$snapshot_ready" != "1" ]; then
  echo "[amsc_zuuu] immutable poll snapshot failed: source changed or syntax invalid source=$SOURCE" >&2
  exit 2
fi
chmod 500 "$SNAPSHOT"

echo "[amsc_zuuu] immutable poll snapshot sha256=$snapshot_sha256 source=$SOURCE"

export AMSC_POLL_PROJECT_ROOT="$ROOT"
/bin/bash "$SNAPSHOT" "$@"
