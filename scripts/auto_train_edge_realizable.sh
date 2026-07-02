#!/bin/bash
#
# edge_realizable bucket-evidence calibration helper.
#
# Default behavior is deliberately safe: generate and validate a candidate file
# only. Production replacement requires explicit --apply, and service restart
# requires explicit --restart.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DB_PATH="$PROJECT_DIR/data/archive.db"
PRODUCTION_PATH="$PROJECT_DIR/data/edge_realizable_calibration.json"
OUTPUT_PATH="$PROJECT_DIR/data/edge_realizable_calibration.candidate.json"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/edge_realizable_training_$(date +%Y%m%d_%H%M%S).log"
REPORT_PATH="$PROJECT_DIR/data/reports/edge_realizable_training_$(date +%Y%m%d).md"

. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime"
  exit 1
}

TRAINING_DAYS=14
MAX_FACTS=1000
FILL_MODE="ticks"
FORCE=false
APPLY=false
RESTART=false

usage() {
  cat <<EOF
Usage: $0 [--force] [--apply] [--restart] [--since-days N] [--max-facts N] [--fill-mode ticks|orders] [--output PATH]

Default: generate a validated candidate calibration file only.
  --apply      replace data/edge_realizable_calibration.json after validation
  --restart    restart com.weather.trade_engine after --apply
  --force      allow validation to pass with empty bucket tables
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=true
      shift
      ;;
    --apply)
      APPLY=true
      shift
      ;;
    --restart)
      RESTART=true
      shift
      ;;
    --since-days)
      TRAINING_DAYS="${2:-}"
      shift 2
      ;;
    --max-facts)
      MAX_FACTS="${2:-}"
      shift 2
      ;;
    --fill-mode)
      FILL_MODE="${2:-}"
      shift 2
      ;;
    --output)
      OUTPUT_PATH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ "$FILL_MODE" != "ticks" && "$FILL_MODE" != "orders" ]]; then
  echo "invalid --fill-mode: $FILL_MODE"
  exit 1
fi

mkdir -p "$LOG_DIR" "$(dirname "$OUTPUT_PATH")" "$(dirname "$REPORT_PATH")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "========================================="
log "edge_realizable bucket calibration"
log "========================================="
log "db=$DB_PATH"
log "output=$OUTPUT_PATH"
log "production=$PRODUCTION_PATH"
log "fill_mode=$FILL_MODE since_days=$TRAINING_DAYS max_facts=$MAX_FACTS apply=$APPLY restart=$RESTART force=$FORCE"

if [[ ! -f "$DB_PATH" ]]; then
  log "Error: db missing: $DB_PATH"
  exit 1
fi

log "Generating calibration candidate..."
"$PY" "$SCRIPT_DIR/edge_realizable_calibrate.py" \
  --db "$DB_PATH" \
  --since-days "$TRAINING_DAYS" \
  --fill-mode "$FILL_MODE" \
  --max-facts "$MAX_FACTS" \
  --out "$OUTPUT_PATH" \
  2>&1 | tee -a "$LOG_FILE"

log "Validating candidate bucket evidence..."
VALIDATION_OUTPUT=$("$PY" - "$OUTPUT_PATH" "$FORCE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
force = str(sys.argv[2]).lower() == "true"
payload = json.loads(path.read_text())

def table_count(name):
    value = payload.get(name)
    return len(value) if isinstance(value, dict) else 0

counts = {
    "p_converge_buckets": table_count("p_converge_buckets"),
    "p_fill_entry_buckets": table_count("p_fill_entry_buckets"),
    "p_fill_exit_buckets": table_count("p_fill_exit_buckets"),
}
stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
p_fill_entry = payload.get("p_fill_entry_default")
p_converge = payload.get("p_converge_default")
p_fill_exit = payload.get("p_fill_exit_default")
try:
    p_realize = float(p_fill_entry) * float(p_converge) * float(p_fill_exit)
except Exception:
    p_realize = None

print("summary:")
print(f"  generated_at={payload.get('generated_at')}")
print(f"  fill_mode={payload.get('fill_mode')}")
for key, count in counts.items():
    print(f"  {key}={count}")
for key in ("converge_samples", "entry_samples", "exit_samples", "exit_attempts"):
    if key in stats:
        print(f"  {key}={stats.get(key)}")
print(f"  p_fill_entry_default={p_fill_entry}")
print(f"  p_converge_default={p_converge}")
print(f"  p_fill_exit_default={p_fill_exit}")
if p_realize is not None:
    print(f"  p_realize_default_product={p_realize:.6f}")

missing = [key for key, count in counts.items() if count <= 0]
if missing and not force:
    print("validation_error=empty_bucket_tables:" + ",".join(missing))
    sys.exit(2)
PY
)
log "$VALIDATION_OUTPUT"

if [[ "$APPLY" != true ]]; then
  log "Candidate validated; production file was not changed. Use --apply only after review."
else
  if [[ "$OUTPUT_PATH" == "$PRODUCTION_PATH" ]]; then
    log "Error: --apply requires --output to differ from production path"
    exit 1
  fi
  BACKUP_PATH="$PROJECT_DIR/data/edge_realizable_calibration.$(date +%Y%m%d_%H%M%S).bak"
  if [[ -f "$PRODUCTION_PATH" ]]; then
    cp "$PRODUCTION_PATH" "$BACKUP_PATH"
    log "Backed up production calibration to $BACKUP_PATH"
  fi
  cp "$OUTPUT_PATH" "$PRODUCTION_PATH"
  log "Applied candidate to production calibration: $PRODUCTION_PATH"
  if [[ "$RESTART" == true ]]; then
    log "Restart requested; restarting com.weather.trade_engine if loaded..."
    if launchctl list | /usr/bin/grep -q "com.weather.trade_engine"; then
      launchctl stop com.weather.trade_engine || true
      sleep 2
      launchctl start com.weather.trade_engine || true
      log "Restart command sent"
    else
      log "com.weather.trade_engine not loaded; restart skipped"
    fi
  else
    log "Restart was not requested; running services were left untouched."
  fi
fi

cat > "$REPORT_PATH" <<EOF
# edge_realizable 校准候选报告

生成时间：$(date '+%Y-%m-%d %H:%M:%S')

## 输出

- 候选文件：$OUTPUT_PATH
- 生产文件：$PRODUCTION_PATH
- 已应用生产：$APPLY
- 已请求重启：$RESTART

## 校验摘要

\`\`\`text
$VALIDATION_OUTPUT
\`\`\`

## 备注

默认调度只生成候选文件，不替换生产校准文件。生产替换需显式传入 \`--apply\`。

日志：$LOG_FILE
EOF

log "Report written: $REPORT_PATH"
log "========================================="
log "Done"
log "========================================="
