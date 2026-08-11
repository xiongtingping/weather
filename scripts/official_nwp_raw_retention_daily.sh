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
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$ROOT")" || {
  echo "missing project python runtime"
  exit 1
}

AUDIT_DIR="$ROOT/output/audits"
PLAN="$AUDIT_DIR/official_nwp_native_raw_retention_plan_latest.json"
RESULT="$AUDIT_DIR/official_nwp_native_raw_retention_apply_latest.json"
MIN_AGE_DAYS="${PM_OFFICIAL_NWP_NATIVE_RETENTION_MIN_AGE_DAYS:-2}"

COMMON=(
  "$ROOT/scripts/official_nwp_raw_retention.py"
  --lane native
  --min-age-days "$MIN_AGE_DAYS"
)
# Twelve root-owned 0600 leftovers under data/research make the fail-closed
# reference scan refuse the whole pass.  Until their ownership is repaired
# (`sudo chown xiong:staff ...`), the opt-out keeps disk hygiene running and
# records every skipped document in the plan.
if [ "${PM_OFFICIAL_NWP_NATIVE_RETENTION_ALLOW_UNREADABLE_REFS:-0}" = "1" ]; then
  COMMON+=(--allow-unreadable-reference-documents)
fi

echo "=== official NWP native raw retention ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')  min_age_days=${MIN_AGE_DAYS}"

echo "【dry-run 计划】"
"$PY" "${COMMON[@]}" --audit-output "$PLAN" >/dev/null
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
