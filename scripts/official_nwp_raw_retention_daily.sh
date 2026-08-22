#!/bin/bash
# Daily retention pass over the native content-addressed NWP object stores.
#
# The stores under data/runtime/official_nwp_sources and
# data/research/bayes_basket_target/model_arrivals have no eviction of their
# own; ecmwf_pf50 alone writes gigabytes per day.  This pass always writes a
# dry-run plan first, and only then -- and only when explicitly enabled --
# discards bodies that no reference document names any more.
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"
. "$ROOT/scripts/lib/single_instance.sh"
single_instance official_nwp_raw_retention_daily
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$ROOT")" || {
  echo "missing project python runtime"
  exit 1
}

AUDIT_DIR="$ROOT/output/audits"
PLAN="$AUDIT_DIR/official_nwp_native_raw_retention_plan_latest.json"
RESULT="$AUDIT_DIR/official_nwp_native_raw_retention_apply_latest.json"
MIN_AGE_DAYS="${PM_OFFICIAL_NWP_NATIVE_RETENTION_MIN_AGE_DAYS:-2}"
AUDITS_RETENTION_DAYS="${PM_OUTPUT_AUDITS_RETENTION_DAYS:-14}"

# Runs first, not after: a raw body only becomes reclaimable below once no
# packet under output/audits names its hash any more, and a packet that is
# never pruned keeps it referenced forever.  Pruning stale packets in the
# same pass lets that day's native sweep see the reference drop immediately
# instead of waiting a day behind.
echo "=== output/audits retention ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')  retention_days=${AUDITS_RETENTION_DAYS}"
AUDITS_PLAN="$AUDIT_DIR/output_audits_retention_plan_latest.json"
AUDITS_RESULT="$AUDIT_DIR/output_audits_retention_apply_latest.json"
AUDITS_APPLY_FLAG=()
if [ "${PM_OUTPUT_AUDITS_RETENTION_APPLY:-0}" = "1" ]; then
  AUDITS_APPLY_FLAG=(--apply --result-output "$AUDITS_RESULT")
fi
"$PY" "$ROOT/scripts/output_audits_retention.py" \
  --audits-root "$AUDIT_DIR" \
  --retention-days "$AUDITS_RETENTION_DAYS" \
  --plan-output "$AUDITS_PLAN" \
  "${AUDITS_APPLY_FLAG[@]+"${AUDITS_APPLY_FLAG[@]}"}" >/tmp/output_audits_retention_stdout.json
"$PY" - /tmp/output_audits_retention_stdout.json <<'PYAUDITS'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
plan = payload["plan"]
mib = lambda value: f"{int(value) / 1024 / 1024:.1f} MiB"
print(f"  state={plan['state']} stale={plan.get('stale_count', 0)} ({mib(plan.get('stale_bytes', 0))})")
apply = payload.get("apply") or {}
if apply:
    print(f"  deleted={apply['deleted_count']} ({mib(apply['deleted_bytes'])})")
PYAUDITS
rm -f /tmp/output_audits_retention_stdout.json
if [ "${PM_OUTPUT_AUDITS_RETENTION_APPLY:-0}" != "1" ]; then
  echo "dry-run：PM_OUTPUT_AUDITS_RETENTION_APPLY!=1，本次只出计划不删除。"
fi

COMMON=(
  "$ROOT/scripts/official_nwp_raw_retention.py"
  --lane native
  --min-age-days "$MIN_AGE_DAYS"
)
# A reference document this user cannot read cannot prove a body is garbage,
# so the scan refuses the whole pass by default.  The opt-out exists only for
# a deliberate operator decision and records every skipped document.
if [ "${PM_OFFICIAL_NWP_NATIVE_RETENTION_ALLOW_UNREADABLE_REFS:-0}" = "1" ]; then
  COMMON+=(--allow-unreadable-reference-documents)
fi

echo "=== official NWP native raw retention ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')  min_age_days=${MIN_AGE_DAYS}"

echo "【dry-run 计划】"
if ! "$PY" "${COMMON[@]}" --audit-output "$PLAN" >/dev/null 2>"$PLAN.err"; then
  if grep -q "reference_unreadable" "$PLAN.err"; then
    echo "阻断：引用扫描 fail-closed，有读不到的引用文档，本次不删除任何东西。"
    echo "读不到的文档（修好属主后本任务自动恢复）："
    "$PY" - <<'PYBLOCK'
import os
import sys

ROOT = "/Users/xiong/weather"
blocked = []
for reference_root in ("output/audits", "data/research", "data/runtime"):
    for current, _directories, files in os.walk(os.path.join(ROOT, reference_root)):
        for name in files:
            if not name.lower().endswith((".json", ".jsonl")):
                continue
            path = os.path.join(current, name)
            try:
                with open(path, "rb") as handle:
                    handle.read(1)
            except OSError as exc:
                blocked.append(f"  {path}  ({exc.strerror})")
print("\n".join(blocked) if blocked else "  （已无阻断文档，请重跑）")
print(f"  共 {len(blocked)} 个。修复：sudo chown {os.environ.get('USER', 'xiong')}:staff <路径>")
PYBLOCK
    rm -f "$PLAN.err"
    exit 1
  fi
  cat "$PLAN.err" >&2
  rm -f "$PLAN.err"
  exit 1
fi
rm -f "$PLAN.err"
"$PY" - "$PLAN" <<'PYSUMMARY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
mib = lambda value: f"{int(value) / 1024 / 1024:.1f} MiB"
print(f"  state={plan['state']} apply_allowed={plan['apply_allowed']}")
print(f"  bodies={plan['body_hash_count']} referenced={plan['referenced_hash_count']}")
print(f"  unreferenced={plan['unreferenced_count']} ({mib(plan['unreferenced_bytes'])})")
print(f"  withheld_young={plan['withheld_young_count']} ({mib(plan['withheld_young_bytes'])})")
print(f"  unreadable_reference_documents={plan['unreadable_reference_document_count']}")
for row in plan["store_rows"][:5]:
    if row["unreferenced_bytes"]:
        print(f"    {row['provider']}: {row['unreferenced_count']} / {mib(row['unreferenced_bytes'])}")
PYSUMMARY

if [ "${PM_OFFICIAL_NWP_NATIVE_RETENTION_APPLY:-0}" != "1" ]; then
  echo "dry-run：PM_OFFICIAL_NWP_NATIVE_RETENTION_APPLY!=1，本次只出计划不删除。"
  exit 0
fi

echo "【apply】只删除无引用且早于 ${MIN_AGE_DAYS} 天的 body；删前写 cold archive manifest。"
"$PY" "${COMMON[@]}" --apply --purge-unreferenced --audit-output "$RESULT" >/dev/null
"$PY" - "$RESULT" <<'PYAPPLY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"  state={plan['state']}")
print(
    f"  deleted={plan.get('deleted_count', 0)}"
    f" ({int(plan.get('deleted_bytes', 0)) / 1024 / 1024:.1f} MiB)"
)
for path in plan.get("archive_manifest_paths") or []:
    print(f"  manifest: {path}")
PYAPPLY

echo "【剩余】"
du -sh data/runtime/official_nwp_sources data/research/bayes_basket_target/model_arrivals
echo "=== 完成 ==="
