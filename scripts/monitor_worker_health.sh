#!/bin/bash
# 监控worker健康状态

LOCK_DIR="/Users/xiong/weather/data/auto_trade_runtime.lock"
MAX_LOCK_AGE_MIN=5
MAX_WORKER_RUNTIME_MIN=10
ROOT="/Users/xiong/weather"
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$ROOT")" || {
    echo "🔴 ALERT: Missing project python runtime"
    exit 1
}

echo "=== Worker Health Monitor ==="
echo "Time: $(date -u)"
echo ""

# 检查lock年龄
if [ -d "$LOCK_DIR" ]; then
    LOCK_AGE_MIN=$(find "$LOCK_DIR" -type d -mmin +${MAX_LOCK_AGE_MIN} | wc -l | tr -d ' ')
    if [ "$LOCK_AGE_MIN" -gt 0 ]; then
        echo "🔴 ALERT: Lock is stale (>${MAX_LOCK_AGE_MIN} min)"
        echo "   Cleaning up..."
        rm -rf "$LOCK_DIR"
        echo "   ✅ Lock cleaned"
    else
        echo "✅ Lock age OK (<${MAX_LOCK_AGE_MIN} min)"
    fi
else
    echo "✅ No lock exists"
fi

echo ""

# 检查worker进程
STUCK_WORKERS=$(ps aux | grep auto_trade_job_worker | grep -v grep | awk -v max=$MAX_WORKER_RUNTIME_MIN '{
    split($10, time, ":");
    if (length(time) == 2) {
        runtime = time[1] * 60 + time[2];
    } else if (length(time) == 3) {
        runtime = time[1] * 3600 + time[2] * 60 + time[3];
    } else {
        runtime = 0;
    }
    if (runtime > max * 60) {
        print $2, runtime/60 "min";
    }
}')

if [ -n "$STUCK_WORKERS" ]; then
    echo "🔴 ALERT: Stuck workers detected:"
    echo "$STUCK_WORKERS" | while read pid runtime; do
        echo "   PID $pid running for $runtime"
        echo "   Killing..."
        kill -9 $pid 2>/dev/null
        echo "   ✅ Killed"
    done
else
    echo "✅ No stuck workers"
fi

echo ""

# 检查任务失败率
SINCE_ISO=$("$PY" - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"))
PY
)
FAILED_RATE=$(sqlite3 /Users/xiong/weather/data/archive.db "
SELECT COALESCE(ROUND(
    SUM(CASE WHEN state IN ('failed', 'retrying') THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0),
    1
), 0)
FROM auto_trade_jobs
WHERE created_at >= '$SINCE_ISO'
")

echo "Task failure rate (last hour): ${FAILED_RATE}%"
if [ $(echo "$FAILED_RATE > 10" | bc) -eq 1 ]; then
    echo "🔴 ALERT: High failure rate (>${FAILED_RATE}%)"
else
    echo "✅ Failure rate OK"
fi

echo ""
echo "Runtime Python:"
if "$PY" "$ROOT/scripts/check_runtime_python.py"; then
    echo "✅ Runtime Python OK"
else
    echo "🔴 ALERT: Runtime Python has blockers"
fi

echo ""
echo "Trade readiness:"
if "$PY" "$ROOT/scripts/check_trade_readiness.py"; then
    echo "✅ Trade readiness OK"
else
    echo "🔴 ALERT: Trade readiness has blockers"
fi

echo ""
echo "Bayesian readiness:"
BAYES_CITY="${PM_BAYES_READINESS_CITY:-}"
BAYES_SCOPE="${PM_BAYES_READINESS_MARKET_SCOPE:-highest_temperature}"
BAYES_SINCE_DAYS="${PM_BAYES_READINESS_SINCE_DAYS:-14}"
BAYES_REPORT_SUFFIX="${BAYES_CITY:-$BAYES_SCOPE}"
BAYES_REPORT="${PM_BAYES_READINESS_REPORT:-$ROOT/data/reports/bayesian_readiness_audit_${BAYES_REPORT_SUFFIX}.json}"
if "$PY" "$ROOT/scripts/bayesian_readiness_audit.py" \
    --since-days "$BAYES_SINCE_DAYS" \
    --city "$BAYES_CITY" \
    --market-scope "$BAYES_SCOPE" \
    --out "$BAYES_REPORT" \
    --json; then
    echo "✅ Bayesian readiness OK"
else
    echo "🔴 ALERT: Bayesian readiness has blockers"
    echo "   report: $BAYES_REPORT"
fi

echo ""
echo "Paper trading validation:"
PAPER_CITY="${PM_PAPER_VALIDATION_CITY:-${BAYES_CITY}}"
PAPER_SCOPE="${PM_PAPER_VALIDATION_MARKET_SCOPE:-${BAYES_SCOPE}}"
PAPER_SINCE_DAYS="${PM_PAPER_VALIDATION_SINCE_DAYS:-28}"
PAPER_REPORT="${PM_PAPER_VALIDATION_REPORT:-$ROOT/data/reports/paper_trading_validation_latest.json}"
if "$PY" "$ROOT/scripts/paper_trading_validation.py" \
    --since-days "$PAPER_SINCE_DAYS" \
    --city "$PAPER_CITY" \
    --market-scope "$PAPER_SCOPE" \
    --out "$PAPER_REPORT" \
    --json; then
    echo "✅ Paper trading validation OK"
else
    echo "🔴 ALERT: Paper trading validation has blockers"
    echo "   report: $PAPER_REPORT"
fi

echo ""
echo "Small-capital validation review:"
SMALL_CAP_PILOT_CITIES="${PM_SMALL_CAPITAL_PILOT_CITIES:-${PM_PAPER_VALIDATION_CITY:-}}"
SMALL_CAP_SINCE_DAYS="${PM_SMALL_CAPITAL_VALIDATION_SINCE_DAYS:-42}"
SMALL_CAP_SCOPE="${PM_SMALL_CAPITAL_MARKET_SCOPE:-${PAPER_SCOPE}}"
SMALL_CAP_REPORT="${PM_SMALL_CAPITAL_VALIDATION_REPORT:-$ROOT/data/reports/small_capital_validation_review_latest.json}"
SMALL_CAP_PILOT_CITIES_EFFECTIVE="$SMALL_CAP_PILOT_CITIES"
SMALL_CAP_PILOT_ARGS=()
if [ -n "${SMALL_CAP_PILOT_CITIES_EFFECTIVE// /}" ]; then
    SMALL_CAP_PILOT_ARGS=(--pilot-city "$SMALL_CAP_PILOT_CITIES_EFFECTIVE")
else
    echo "ℹ️ Small-cap pilot cities: using auto defaults from objective/matrix reports"
fi
if "$PY" "$ROOT/scripts/small_capital_validation_review.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        "${SMALL_CAP_PILOT_ARGS[@]}" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out "$SMALL_CAP_REPORT" \
        --json; then
        echo "✅ Small-capital validation review OK"
    else
        echo "🔴 ALERT: Small-capital validation review has blockers"
        echo "   report: $SMALL_CAP_REPORT"
    fi
echo ""
echo "Pilot observation review:"
PILOT_OBSERVATION_REPORT="${PM_PILOT_OBSERVATION_REPORT:-$ROOT/data/reports/pilot_observation_review_latest.md}"
if "$PY" "$ROOT/scripts/pilot_observation_review.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        "${SMALL_CAP_PILOT_ARGS[@]}" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --format markdown \
        --out "$PILOT_OBSERVATION_REPORT" >/dev/null; then
        echo "✅ Pilot observation review generated"
        echo "   report: $PILOT_OBSERVATION_REPORT"
    else
        echo "🔴 ALERT: Pilot observation review failed"
        echo "   report: $PILOT_OBSERVATION_REPORT"
    fi
echo ""
echo "Paper observation tracking:"
PAPER_OBSERVATION_TRACKING_REPORT="${PM_PAPER_OBSERVATION_TRACKING_REPORT:-$ROOT/data/reports/paper_observation_tracking_latest.json}"
if "$PY" "$ROOT/scripts/paper_observation_tracking_report.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        "${SMALL_CAP_PILOT_ARGS[@]}" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out "$PAPER_OBSERVATION_TRACKING_REPORT" \
        --json >/dev/null; then
        echo "✅ Paper observation tracking generated"
        echo "   report: $PAPER_OBSERVATION_TRACKING_REPORT"
    else
        echo "🔴 ALERT: Paper observation tracking failed"
        echo "   report: $PAPER_OBSERVATION_TRACKING_REPORT"
    fi
echo ""
echo "Paper observation rolling validation:"
PAPER_OBSERVATION_ROLLING_REPORT="${PM_PAPER_OBSERVATION_ROLLING_REPORT:-$ROOT/data/reports/paper_observation_rolling_validation_latest.json}"
if "$PY" "$ROOT/scripts/paper_observation_rolling_validation.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        "${SMALL_CAP_PILOT_ARGS[@]}" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out "$PAPER_OBSERVATION_ROLLING_REPORT" \
        --json >/dev/null; then
        echo "✅ Paper observation rolling validation OK"
    else
        echo "🔴 ALERT: Paper observation rolling validation has blockers"
        echo "   report: $PAPER_OBSERVATION_ROLLING_REPORT"
    fi
echo ""
echo "Paper observation target package:"
PAPER_OBSERVATION_TARGET_JSON="${PM_PAPER_OBSERVATION_TARGET_JSON:-$ROOT/data/reports/paper_observation_target_package_latest.json}"
PAPER_OBSERVATION_TARGET_MD="${PM_PAPER_OBSERVATION_TARGET_MD:-$ROOT/data/reports/paper_observation_target_package_latest.md}"
if "$PY" "$ROOT/scripts/paper_observation_target_package.py" \
        --event-date "$(TZ=America/New_York date '+%Y-%m-%d')" \
        --event-date "$(TZ=America/New_York date -v+1d '+%Y-%m-%d' 2>/dev/null || TZ=America/New_York date -d '+1 day' '+%Y-%m-%d')" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out-json "$PAPER_OBSERVATION_TARGET_JSON" \
        --out-md "$PAPER_OBSERVATION_TARGET_MD" \
        --json >/dev/null; then
        echo "✅ Paper observation target package generated"
        echo "   json: $PAPER_OBSERVATION_TARGET_JSON"
        echo "   md: $PAPER_OBSERVATION_TARGET_MD"
    else
        echo "🔴 ALERT: Paper observation target package failed"
        echo "   json: $PAPER_OBSERVATION_TARGET_JSON"
    fi
echo ""
echo "Strategy objective completion audit:"
STRATEGY_OBJECTIVE_AUDIT_REPORT="${PM_STRATEGY_OBJECTIVE_AUDIT_REPORT:-$ROOT/data/reports/strategy_objective_completion_audit_latest.json}"
STRATEGY_OBJECTIVE_AUDIT_REFRESH="${PM_STRATEGY_OBJECTIVE_AUDIT_REFRESH:-0}"
if [ "$STRATEGY_OBJECTIVE_AUDIT_REFRESH" = "1" ] || [ ! -s "$STRATEGY_OBJECTIVE_AUDIT_REPORT" ]; then
    if "$PY" "$ROOT/scripts/strategy_objective_completion_audit.py" \
            --event-date "$(TZ=America/New_York date '+%Y-%m-%d')" \
            "${SMALL_CAP_PILOT_ARGS[@]}" \
            --market-scope "$SMALL_CAP_SCOPE" \
            --since-days "$SMALL_CAP_SINCE_DAYS" \
            --out "$STRATEGY_OBJECTIVE_AUDIT_REPORT" \
            --json >/dev/null; then
            echo "✅ Strategy objective completion audit OK"
        else
            echo "ℹ️ Strategy objective completion audit not complete"
            echo "   report: $STRATEGY_OBJECTIVE_AUDIT_REPORT"
        fi
    else
        "$PY" - "$STRATEGY_OBJECTIVE_AUDIT_REPORT" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
dependency_paths = [
    path.parent / "paper_observation_target_package_latest.json",
    path.parent / "paper_observation_rolling_validation_latest.json",
]
report_mtime = path.stat().st_mtime
newest_dependency_mtime = max(
    [item.stat().st_mtime for item in dependency_paths if item.exists()] or [0.0]
)
cache_stale = newest_dependency_mtime > report_mtime + 1.0
payload = json.loads(path.read_text(encoding="utf-8"))
summary = payload.get("current_strategy_batch_summary") or {}
target = summary.get("targeted_candidate_status") or {}
blockers = list(payload.get("blockers") or [])
print(
    "ℹ️ Strategy objective audit cached "
    f"ok={bool(payload.get('ok'))} "
    f"stale={cache_stale} "
    f"generated_at={payload.get('generated_at') or ''} "
    f"pilot_cities={','.join(str(x) for x in payload.get('pilot_cities') or []) or '-'}"
)
if blockers:
    print("   blockers: " + ",".join(str(x) for x in blockers[:3]))
if target:
    print(
        "   target: "
        f"{target.get('status') or '-'} "
        f"rows={int(target.get('candidate_target_rows') or 0)} "
        f"next={target.get('next_action') or '-'}"
    )
    hunt_items = []
    for item in list(target.get("target_hunt_plan") or [])[:3]:
        if not isinstance(item, dict):
            continue
        city = str(item.get("city") or "").strip()
        strategy = str(item.get("target_strategy") or "").strip()
        directive = str(item.get("scan_directive") or "").strip()
        hunt_status = str(item.get("hunt_status") or "").strip()
        needed_rows = int(item.get("needed_resolved_rows") or 0)
        needed_wins = int(item.get("needed_additional_wins_for_hit_rate") or 0)
        if not city or not strategy:
            continue
        suffix = ""
        if needed_rows > 0:
            suffix += f"+{needed_rows}resolved"
        if needed_wins > 0:
            suffix += f"+{needed_wins}wins"
        status_suffix = f"[{hunt_status}]" if hunt_status else ""
        hunt_items.append(f"{city}:{strategy}{suffix}{status_suffix}->{directive or '-'}")
    if hunt_items:
        print("   hunt: " + ";".join(hunt_items))
    expansion_items = []
    for item in list(target.get("expansion_hunt_plan") or [])[:3]:
        if not isinstance(item, dict):
            continue
        city = str(item.get("city") or "").strip()
        status = str(item.get("hunt_status") or "").strip()
        rows = int(item.get("paper_observation_rows") or 0)
        if city and rows > 0:
            expansion_items.append(f"{city}:paper{rows}[{status}]")
    if expansion_items:
        print("   expansion: " + ";".join(expansion_items))
    target_tracking = target.get("target_observation_tracking_summary") or {}
    expansion_tracking = target.get("expansion_observation_tracking_summary") or {}
    if isinstance(target_tracking, dict) or isinstance(expansion_tracking, dict):
        print(
            "   tracking: "
            f"target resolved={int(target_tracking.get('resolved_rows') or 0)} "
            f"pending={int(target_tracking.get('pending_rows') or 0)}; "
            f"expansion resolved={int(expansion_tracking.get('resolved_rows') or 0)} "
            f"pending={int(expansion_tracking.get('pending_rows') or 0)}"
        )
    pilot_reco = target.get("pilot_city_recommendation") or {}
    if isinstance(pilot_reco, dict):
        reco_action = str(pilot_reco.get("action") or "").strip()
        reco_cities = [
            str(city)
            for city in list(pilot_reco.get("recommended_pilot_cities") or [])[:2]
            if str(city).strip()
        ]
        if reco_action and reco_action != "keep_current_pilot" and reco_cities:
            print("   pilot_recommendation: " + reco_action + " -> " + ",".join(reco_cities))
    reco_diag = target.get("recommended_pilot_city_diagnostics") or {}
    if isinstance(reco_diag, dict):
        diag_items = []
        for city, item in list(reco_diag.items())[:2]:
            if not isinstance(item, dict):
                continue
            skipped = item.get("skipped_diagnostics") or {}
            excluded = item.get("excluded_entry_reasons") or {}
            top_reason = str(item.get("primary_action") or "").strip() or "-"
            if isinstance(skipped, dict) and skipped:
                top_reason = top_reason if top_reason != "-" else sorted(skipped.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[0][0]
            elif isinstance(excluded, dict) and excluded:
                top_reason = top_reason if top_reason != "-" else sorted(excluded.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[0][0]
            diag_items.append(
                f"{city}:pre{int(item.get('scoped_plan_rows') or 0)}/"
                f"post{int(item.get('candidate_rows') or 0)}/{top_reason}"
            )
        if diag_items:
            print("   pilot_diagnostics: " + ";".join(diag_items))
    settle_ready = target.get("recommended_pilot_settlement_readiness") or {}
    if isinstance(settle_ready, dict):
        scanned = int(settle_ready.get("total_decisions_scanned") or 0)
        planned = int(settle_ready.get("total_planned_labels") or 0)
        if scanned or planned:
            print(
                "   pilot_settlement_labels: "
                f"planned={planned} scanned={scanned} "
                f"next={settle_ready.get('next_action') or '-'}"
            )
            by_city_ready = settle_ready.get("by_city") or {}
            if isinstance(by_city_ready, dict):
                items = []
                for city, item in list(by_city_ready.items())[:2]:
                    if not isinstance(item, dict):
                        continue
                    counts = item.get("eligibility_status_counts") or {}
                    if isinstance(counts, dict) and counts:
                        top = sorted(counts.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[0]
                        items.append(f"{city}:{top[0]}={int(top[1] or 0)}")
            if items:
                print("   pilot_settlement_status: " + ";".join(items))
print(f"   report: {path}")
PY
    echo ""
    echo "Recommended pilot settlement wakeup:"
    WAKEUP_REPORT="${PM_RECOMMENDED_PILOT_SETTLEMENT_WAKEUP_REPORT:-$ROOT/data/reports/recommended_pilot_settlement_wakeup_latest.json}"
if "$PY" "$ROOT/scripts/recommended_pilot_settlement_wakeup.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out "$WAKEUP_REPORT" \
        --json >/dev/null; then
        "$PY" - "$WAKEUP_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    "   wakeup: "
    f"watched={int(payload.get('watched_market_count') or 0)} "
    f"triggered={int(payload.get('triggered_market_count') or 0)} "
    f"next={payload.get('next_action') or '-'}"
)
for item in list(payload.get("triggered_markets") or [])[:3]:
    if isinstance(item, dict):
        print(
            "   triggered: "
            f"{item.get('city') or '-'} {item.get('event_date') or '-'} "
            f"{item.get('slug') or '-'} -> {item.get('current_status') or '-'}"
        )
validation = payload.get("recommended_pilot_observation_validation") or {}
if isinstance(validation, dict):
    thresholds = validation.get("thresholds") or {}
    min_rows = int(thresholds.get("min_resolved_rows") or 0)
    for item in list(validation.get("by_city") or [])[:2]:
        if not isinstance(item, dict):
            continue
        hit = item.get("hit_rate")
        try:
            hit_text = f"{float(hit) * 100:.0f}%" if hit is not None else "-"
        except Exception:
            hit_text = "-"
        print(
            "   pilot_validation: "
            f"{item.get('city') or '-'} "
            f"resolved={int(item.get('resolved_rows') or 0)}/{min_rows} "
            f"pending={int(item.get('pending_rows') or 0)} "
            f"wins={int(item.get('wins') or 0)} "
            f"hr={hit_text} "
            f"ok={bool(item.get('ok'))}"
        )
PY
    else
        echo "🔴 ALERT: Recommended pilot settlement wakeup check failed"
        echo "   report: $WAKEUP_REPORT"
    fi
    echo ""
    echo "Settlement actual repair plan:"
    SETTLEMENT_REPAIR_REPORT="${PM_SETTLEMENT_ACTUAL_REPAIR_PLAN_REPORT:-$ROOT/data/reports/settlement_actual_repair_plan_latest.json}"
    if "$PY" "$ROOT/scripts/settlement_actual_repair_plan.py" \
        --wakeup "$WAKEUP_REPORT" \
        --out "$SETTLEMENT_REPAIR_REPORT" \
        --fetch-preview \
        --json >/dev/null; then
        "$PY" - "$SETTLEMENT_REPAIR_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    "   settlement_repair: "
    f"missing={int(payload.get('missing_settlement_actual_count') or 0)} "
    f"next={payload.get('next_action') or '-'}"
)
for item in list(payload.get("rows") or [])[:3]:
    if not isinstance(item, dict):
        continue
    print(
        "   repair_target: "
        f"{item.get('city') or '-'} {item.get('event_date') or '-'} "
        f"{item.get('slug') or '-'} "
        f"station={item.get('station_id') or '-'} "
        f"source_station={item.get('source_station_id') or '-'} "
        f"metadata_ok={bool(item.get('metadata_ok_for_settlement_fetch'))}"
    )
PY
    else
        echo "🔴 ALERT: Settlement actual repair plan failed"
        echo "   report: $SETTLEMENT_REPAIR_REPORT"
    fi
    echo ""
    echo "Settlement actual apply dry-run:"
    SETTLEMENT_APPLY_REPORT="${PM_SETTLEMENT_ACTUAL_APPLY_PLAN_REPORT:-$ROOT/data/reports/settlement_actual_apply_plan_latest.json}"
    if "$PY" "$ROOT/scripts/apply_settlement_actual_repair_plan.py" \
        --repair-plan "$SETTLEMENT_REPAIR_REPORT" \
        --out "$SETTLEMENT_APPLY_REPORT" \
        --json >/dev/null; then
        "$PY" - "$SETTLEMENT_APPLY_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    "   settlement_apply_dry_run: "
    f"candidates={int(payload.get('candidate_count') or 0)} "
    f"ready={int(payload.get('ready_count') or 0)} "
    f"read_only={bool(payload.get('read_only'))} "
    f"next={payload.get('next_action') or '-'}"
)
for item in list(payload.get("results") or [])[:3]:
    if not isinstance(item, dict):
        continue
    print(
        "   apply_candidate: "
        f"{item.get('city') or '-'} {item.get('event_date') or '-'} "
        f"{item.get('status') or '-'} "
        f"actual={item.get('actual') if item.get('actual') is not None else '-'} "
        f"slug={item.get('slug') or '-'}"
    )
PY
    else
        echo "🔴 ALERT: Settlement actual apply dry-run failed"
        echo "   report: $SETTLEMENT_APPLY_REPORT"
    fi
    echo ""
    echo "Settlement calibration label dry-run:"
    SETTLEMENT_LABEL_REPORT="${PM_SETTLEMENT_LABEL_DRY_RUN_REPORT:-$ROOT/data/reports/settlement_label_dry_run_latest.json}"
    if "$PY" "$ROOT/scripts/build_settlement_calibration_labels.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --json > "$SETTLEMENT_LABEL_REPORT"; then
        "$PY" - "$SETTLEMENT_LABEL_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
skipped = payload.get("skipped") or {}
if not isinstance(skipped, dict):
    skipped = {}
top_skipped = sorted(
    skipped.items(),
    key=lambda kv: (-int(kv[1] or 0), str(kv[0])),
)[:2]
skip_text = ",".join(f"{key}:{int(value or 0)}" for key, value in top_skipped) or "-"
print(
    "   settlement_label_dry_run: "
    f"planned={int(payload.get('planned_labels') or 0)} "
    f"scanned={int(payload.get('decisions_scanned') or 0)} "
    f"mode={payload.get('mode') or '-'} "
    f"skipped={skip_text}"
)
apply_command = str(payload.get("apply_command") or "").strip()
if apply_command:
    print(f"   settlement_label_apply_after_review: {apply_command}")
PY
    else
        echo "🔴 ALERT: Settlement calibration label dry-run failed"
        echo "   report: $SETTLEMENT_LABEL_REPORT"
    fi
    echo ""
    echo "Settlement validation pipeline:"
    SETTLEMENT_PIPELINE_REPORT="${PM_SETTLEMENT_VALIDATION_PIPELINE_REPORT:-$ROOT/data/reports/settlement_validation_pipeline_latest.json}"
    if "$PY" "$ROOT/scripts/settlement_validation_pipeline.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --no-fetch-preview \
        --simulate-after-actual-apply \
        --out "$SETTLEMENT_PIPELINE_REPORT" \
        --json >/dev/null; then
        "$PY" - "$SETTLEMENT_PIPELINE_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repair = payload.get("settlement_actual_repair") or {}
apply = payload.get("settlement_actual_apply_dry_run") or {}
labels = payload.get("settlement_label_dry_run") or {}
simulation = payload.get("after_actual_apply_simulation") or {}
if not isinstance(simulation, dict):
    simulation = {}
print(
    "   settlement_pipeline: "
    f"ok={bool(payload.get('ok'))} "
    f"actual_missing={int(repair.get('missing_count') or 0)} "
    f"actual_ready={int(apply.get('ready_count') or 0)} "
    f"labels_planned={int(labels.get('planned_labels') or 0)} "
    f"sim_labels={int(simulation.get('label_planned_after_actual_apply') or 0)} "
    f"next={payload.get('next_action') or '-'}"
)
confirm = str(apply.get("confirm_command") or "").strip()
if confirm:
    print(f"   settlement_actual_apply_after_review: {confirm}")
PY
    else
        echo "🔴 ALERT: Settlement validation pipeline failed"
        echo "   report: $SETTLEMENT_PIPELINE_REPORT"
    fi
    echo ""
    echo "Settlement confirmation packet:"
    SETTLEMENT_APPLY_FLOW_REPORT="${PM_SETTLEMENT_APPLY_FLOW_REPORT:-$ROOT/data/reports/settlement_validation_apply_flow_latest.json}"
    "$PY" "$ROOT/scripts/apply_settlement_validation_flow.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out "$SETTLEMENT_APPLY_FLOW_REPORT" \
        --json >/dev/null || true
    SETTLEMENT_CONFIRMATION_PACKET_JSON="${PM_SETTLEMENT_CONFIRMATION_PACKET_JSON:-$ROOT/data/reports/settlement_confirmation_packet_latest.json}"
    SETTLEMENT_CONFIRMATION_PACKET_MD="${PM_SETTLEMENT_CONFIRMATION_PACKET_MD:-$ROOT/data/reports/settlement_confirmation_packet_latest.md}"
    if "$PY" "$ROOT/scripts/settlement_confirmation_packet.py" \
        --flow "$SETTLEMENT_APPLY_FLOW_REPORT" \
        --out-json "$SETTLEMENT_CONFIRMATION_PACKET_JSON" \
        --out-md "$SETTLEMENT_CONFIRMATION_PACKET_MD" \
        --json >/dev/null; then
        "$PY" - "$SETTLEMENT_CONFIRMATION_PACKET_JSON" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sim = payload.get("after_actual_apply_simulation") or {}
print(
    "   settlement_confirm: "
    f"ok={bool(payload.get('ok'))} "
    f"actual_ready={int(payload.get('actual_ready') or 0)} "
    f"would_insert={int(payload.get('would_insert_event_run_rows') or 0)} "
    f"sim_labels={int(sim.get('label_planned_after_actual_apply') or 0)} "
    f"sim_insert={int(sim.get('label_apply_inserted_count') or 0)} "
    f"closed={bool(sim.get('label_apply_closed_loop_ok'))}"
)
for item in list(payload.get("actual_items") or [])[:3]:
    if not isinstance(item, dict):
        continue
    print(
        "   settlement_confirm_actual: "
        f"{item.get('city') or '-'} {item.get('event_date') or '-'} "
        f"actual={item.get('actual')}{item.get('market_unit') or ''} "
        f"station={item.get('station_id') or '-'} "
        f"source_station={item.get('source_station_id') or '-'} "
        f"match={bool(item.get('station_source_match'))} "
        f"checksum={item.get('review_checksum') or '-'}"
    )
confirmation = payload.get("confirmation") or {}
cmd = str(confirmation.get("actuals_labels_validate_command") or "").strip()
if cmd:
    print(f"   settlement_confirm_after_review: {cmd}")
PY
    else
        echo "🔴 ALERT: Settlement confirmation packet failed"
        echo "   report: $SETTLEMENT_CONFIRMATION_PACKET_JSON"
    fi
    echo ""
    echo "Settlement closed-loop audit:"
    SETTLEMENT_CLOSED_LOOP_REPORT="${PM_SETTLEMENT_CLOSED_LOOP_AUDIT_REPORT:-$ROOT/data/reports/settlement_closed_loop_audit_latest.json}"
    if "$PY" "$ROOT/scripts/settlement_closed_loop_audit.py" \
        --since-days "$SMALL_CAP_SINCE_DAYS" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out "$SETTLEMENT_CLOSED_LOOP_REPORT" \
        --json >/dev/null; then
        "$PY" - "$SETTLEMENT_CLOSED_LOOP_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    "   settlement_closed_loop: "
    f"ok={bool(payload.get('ok'))} "
    f"phase={payload.get('phase') or '-'} "
    f"actual_ready={int(payload.get('actual_ready') or 0)} "
    f"labels_planned={int(payload.get('labels_planned') or 0)} "
    f"sim_labels={int(payload.get('sim_labels_after_actual_apply') or 0)}"
)
commands = payload.get("commands") or {}
if isinstance(commands, dict):
    apply_actuals = str(commands.get("apply_actuals") or "").strip()
    rolling = str(commands.get("rolling_validation") or "").strip()
    if apply_actuals:
        print(f"   settlement_closed_loop_apply_actuals_after_review: {apply_actuals}")
    if rolling:
        print(f"   settlement_closed_loop_after_labels: {rolling} + objective_audit")
PY
    else
        echo "🔴 ALERT: Settlement closed-loop audit failed"
        echo "   report: $SETTLEMENT_CLOSED_LOOP_REPORT"
    fi
    echo ""
    echo "Strategy goal completion matrix:"
    STRATEGY_GOAL_MATRIX_REPORT="${PM_STRATEGY_GOAL_COMPLETION_MATRIX_REPORT:-$ROOT/data/reports/strategy_goal_completion_matrix_latest.json}"
    if "$PY" "$ROOT/scripts/strategy_goal_completion_matrix.py" \
        --out "$STRATEGY_GOAL_MATRIX_REPORT" \
        --json >/dev/null; then
        echo "✅ Strategy goal completion matrix OK"
    else
        echo "ℹ️ Strategy goal completion matrix not complete"
    fi
    "$PY" - "$STRATEGY_GOAL_MATRIX_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows = payload.get("rows") or []
blocked = [
    row for row in rows
    if isinstance(row, dict) and row.get("status") not in {"proved"}
]
print(
    "   strategy_goal_matrix: "
    f"ok={bool(payload.get('ok'))} "
    f"blocked={len(blocked)} "
    f"next={payload.get('next_mainline_task') or '-'}"
)
for row in blocked[:4]:
    blockers = ",".join(str(x) for x in row.get("blockers") or []) or "-"
    print(f"   goal_blocker: {row.get('name') or '-'} status={row.get('status') or '-'} {blockers}")
PY
    echo ""
    echo "Recommended pilot gap scan plan:"
    GAP_SCAN_PLAN_REPORT="${PM_RECOMMENDED_PILOT_GAP_SCAN_PLAN_REPORT:-$ROOT/data/reports/recommended_pilot_gap_scan_plan_latest.json}"
    if "$PY" "$ROOT/scripts/recommended_pilot_gap_scan_plan.py" \
        --market-scope "$SMALL_CAP_SCOPE" \
        --out "$GAP_SCAN_PLAN_REPORT" \
        --json >/dev/null; then
        "$PY" - "$GAP_SCAN_PLAN_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    "   gap_scan: "
    f"queue={int(payload.get('queue_count') or 0)} "
    f"cities={int(payload.get('city_count') or 0)} "
    f"strategy_gaps={int(payload.get('strategy_gap_count') or 0)} "
    f"next={payload.get('next_action') or '-'}"
)
for item in list(payload.get("plan") or [])[:3]:
    if not isinstance(item, dict):
        continue
    strategy_bits = []
    for gap in list(item.get("strategy_gaps") or [])[:2]:
        if not isinstance(gap, dict):
            continue
        strategy = str(gap.get("target_strategy") or "-")
        needed = int(gap.get("needed_resolved_rows") or 0)
        directive = str(gap.get("scan_directive") or "-")
        strategy_bits.append(f"{strategy}+{needed}->{directive}")
    strategy_text = ",".join(strategy_bits) if strategy_bits else ",".join(
        str(x) for x in item.get("scan_directives") or []
    )
    print(
        "   gap_target: "
        f"{item.get('city') or '-'} "
        f"resolved={int(item.get('resolved_rows') or 0)} "
        f"need={int(item.get('needed_resolved_rows') or 0)} "
        f"{strategy_text or '-'}"
    )
PY
    else
        echo "🔴 ALERT: Recommended pilot gap scan plan failed"
        echo "   report: $GAP_SCAN_PLAN_REPORT"
    fi
    echo ""
    echo "Recommended pilot gap refresh health:"
    GAP_REFRESH_HEALTH_REPORT="${PM_RECOMMENDED_PILOT_GAP_REFRESH_HEALTH_REPORT:-$ROOT/data/reports/recommended_pilot_gap_refresh_health_latest.json}"
    if "$PY" "$ROOT/scripts/recommended_pilot_gap_refresh_health.py" \
        --topn "${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-4}" \
        --out "$GAP_REFRESH_HEALTH_REPORT" \
        --json >/dev/null; then
        echo "✅ Recommended pilot gap refresh OK"
    else
        echo "🔴 ALERT: Recommended pilot gap refresh has blockers"
        echo "   report: $GAP_REFRESH_HEALTH_REPORT"
        "$PY" - "$GAP_REFRESH_HEALTH_REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    "   gap_refresh: "
    f"slugs={int(payload.get('priority_slug_count') or 0)} "
    f"next={payload.get('next_action') or '-'}"
)
for row in list(payload.get("rows") or [])[:3]:
    if not isinstance(row, dict):
        continue
    blockers = ",".join(str(x) for x in row.get("blockers") or []) or "-"
    print(
        "   gap_refresh_target: "
        f"{row.get('slug') or '-'} "
        f"tick_age={row.get('tick_age_sec')} "
        f"model_age={row.get('model_age_sec')} "
        f"fact_age={row.get('fact_age_sec')} "
        f"blockers={blockers}"
    )
PY
    fi
fi

echo ""
echo "Env consistency:"
if "$PY" "$ROOT/scripts/check_env_consistency.py"; then
    echo "✅ Env consistency check completed"
else
    echo "🔴 ALERT: Env consistency has blockers"
fi

echo ""
echo "Runtime env health:"
if "$PY" "$ROOT/scripts/check_runtime_env_health.py"; then
    echo "✅ Runtime env health OK"
else
    echo "🔴 ALERT: Runtime env health has blockers"
fi

echo ""
echo "Chengdu D0 physical posterior alignment:"
D0_PHYS_ALIGNMENT_REPORT="${PM_D0_PHYS_ALIGNMENT_REPORT:-$ROOT/data/reports/d0_physical_posterior_alignment_chengdu_latest_d0.json}"
if [ "${PM_D0_PHYS_ALIGNMENT_AUDIT:-1}" = "1" ]; then
    D0_PHYS_ALIGNMENT_SHELL_FLAGS="$-"
    set +e
    "$PY" "$ROOT/scripts/audit_d0_physical_posterior_alignment.py" \
        --latest-chengdu-d0 \
        --limit "${PM_D0_PHYS_ALIGNMENT_LIMIT:-500}" \
        --require-enabled \
        --fail-on-divergence \
        --output "$D0_PHYS_ALIGNMENT_REPORT" \
        --json >/dev/null
    D0_PHYS_ALIGNMENT_CODE=$?
    case "$D0_PHYS_ALIGNMENT_SHELL_FLAGS" in
        *e*) set -e ;;
        *) set +e ;;
    esac
    "$PY" - "$D0_PHYS_ALIGNMENT_REPORT" "$D0_PHYS_ALIGNMENT_CODE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
code = int(sys.argv[2])
payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
status = str(summary.get("status") or "-")
enabled = int(summary.get("enabled_count") or 0)
aligned = int(summary.get("aligned_count") or 0)
diverged = int(summary.get("diverged_count") or 0)
sample_count = int(summary.get("sample_count") or 0)
slug = str(payload.get("slug") or "-")
event_date = str(payload.get("event_date") or "-")
if code == 0:
    prefix = "✅"
    label = "OK"
elif code == 2:
    prefix = "ℹ️"
    label = "waiting"
elif code == 3:
    prefix = "🔴"
    label = "diverged"
else:
    prefix = "🔴"
    label = f"exit_{code}"
print(
    f"{prefix} D0 physical alignment {label}: "
    f"{event_date} {slug} status={status} "
    f"samples={sample_count} enabled={enabled} aligned={aligned} diverged={diverged}"
)
print(f"   report: {path}")
PY
else
    echo "ℹ️ D0 physical alignment audit disabled"
fi

echo ""
echo "SSOT alignment:"
if "$PY" "$ROOT/scripts/check_ssot_alignment.py"; then
    echo "✅ SSOT alignment OK"
else
    echo "🔴 ALERT: SSOT alignment has blockers"
fi

echo ""
echo "Launchd weather:"
if "$PY" "$ROOT/scripts/check_launchd_weather.py"; then
    echo "✅ Launchd weather OK"
else
    echo "🔴 ALERT: Launchd weather has blockers"
fi

echo ""
echo "Market data health:"
if "$PY" "$ROOT/scripts/check_market_data_health.py"; then
    echo "✅ Market data health OK"
else
    echo "🔴 ALERT: Market data health has blockers"
fi

echo ""
echo "Storage/DB health:"
if "$PY" "$ROOT/scripts/check_storage_db_health.py"; then
    echo "✅ Storage/DB health OK"
else
    echo "🔴 ALERT: Storage/DB health has blockers"
fi

echo ""
echo "Recent log health:"
if "$PY" "$ROOT/scripts/check_recent_log_health.py"; then
    echo "✅ Recent log health OK"
else
    echo "🔴 ALERT: Recent log health has blockers"
fi

echo ""
echo "=== End of Health Check ==="
