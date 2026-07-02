#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="${PYTHON:-python3}"
fi

OUT_DIR="${PM_CHENGDU_ANOMALY_OUT_DIR:-$ROOT/data/reports/weather_anomaly}"
LIMIT_TRADES="${PM_CHENGDU_ANOMALY_LIMIT_TRADES:-150}"
PAGE_LIMIT="${PM_CHENGDU_ANOMALY_PAGE_LIMIT:-100}"
REQUEST_SLEEP="${PM_CHENGDU_ANOMALY_REQUEST_SLEEP:-0.03}"
ALERT_RISK_THRESHOLD="${PM_CHENGDU_ANOMALY_ALERT_RISK_THRESHOLD:-40}"
ALERT_COOLDOWN_SEC="${PM_CHENGDU_ANOMALY_ALERT_COOLDOWN_SEC:-900}"
ALERT_RISK_DELTA="${PM_CHENGDU_ANOMALY_ALERT_RISK_DELTA:-5}"

mkdir -p "$OUT_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_PATH="$OUT_DIR/chengdu_weather_market_anomaly_${TS}.json"
ALERTS_PATH="$OUT_DIR/chengdu_weather_market_alerts_${TS}.json"
STATE_PATH="${PM_CHENGDU_ANOMALY_STATE_PATH:-$OUT_DIR/chengdu_weather_market_alert_state.json}"

"$PY" "$ROOT/scripts/weather_market_anomaly_monitor.py" \
  --city chengdu \
  --limit-trades "$LIMIT_TRADES" \
  --data-api-page-limit "$PAGE_LIMIT" \
  --request-sleep "$REQUEST_SLEEP" \
  --alert-risk-threshold "$ALERT_RISK_THRESHOLD" \
  --output-json "$REPORT_PATH" \
  --output-alerts-json "$ALERTS_PATH" \
  --quiet \
  >/dev/null

"$PY" - "$REPORT_PATH" "$ALERTS_PATH" "$STATE_PATH" "$ALERT_COOLDOWN_SEC" "$ALERT_RISK_DELTA" "$ROOT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

report_path = Path(sys.argv[1])
alerts_path = Path(sys.argv[2])
state_path = Path(sys.argv[3])
cooldown_sec = float(sys.argv[4])
risk_delta_threshold = float(sys.argv[5])
root_path = Path(sys.argv[6])
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))
report = json.loads(report_path.read_text())
alerts = json.loads(alerts_path.read_text())
summary = report["summary"]


def parse_dt(value):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def load_state(path):
    if not path.exists():
        return {"alerts": {}}
    try:
        state = json.loads(path.read_text())
    except Exception:
        return {"alerts": {}}
    if not isinstance(state, dict):
        return {"alerts": {}}
    if not isinstance(state.get("alerts"), dict):
        state["alerts"] = {}
    return state


def alert_key(alert):
    return f"{alert.get('slug')}|{alert.get('label')}"


def should_emit(alert, prior, now):
    if not prior:
        return True, "new"
    prior_sent = parse_dt(prior.get("last_sent_at"))
    if prior_sent is None:
        return True, "state_missing_sent_at"
    age = (now - prior_sent).total_seconds()
    if age >= cooldown_sec:
        return True, "cooldown_elapsed"
    if alert.get("severity") != prior.get("last_severity"):
        return True, "severity_changed"
    risk = float(alert.get("risk_score") or 0.0)
    prior_risk = float(prior.get("last_risk_score") or 0.0)
    if abs(risk - prior_risk) >= risk_delta_threshold:
        return True, "risk_changed"
    return False, "cooldown_suppressed"


state = load_state(state_path)
state_alerts = state.setdefault("alerts", {})
now = datetime.now(timezone.utc)
emitted = []
suppressed = []
for alert in alerts.get("alerts", []):
    key = alert_key(alert)
    prior = state_alerts.get(key)
    emit, reason = should_emit(alert, prior, now)
    snapshot = {
        "last_seen_at": now.isoformat(),
        "last_seen_risk_score": alert.get("risk_score"),
        "last_seen_severity": alert.get("severity"),
        "last_seen_mid": alert.get("market_mid"),
        "last_seen_gap": alert.get("market_model_gap"),
    }
    if emit:
        state_alerts[key] = {
            **snapshot,
            "last_sent_at": now.isoformat(),
            "last_sent_reason": reason,
            "last_risk_score": alert.get("risk_score"),
            "last_severity": alert.get("severity"),
            "last_mid": alert.get("market_mid"),
            "last_gap": alert.get("market_model_gap"),
        }
        emitted.append((alert, reason))
    else:
        if prior:
            prior.update(snapshot)
        suppressed.append((alert, reason))

state["updated_at"] = now.isoformat()
state["cooldown_sec"] = cooldown_sec
state["risk_delta_threshold"] = risk_delta_threshold
state_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
tmp_path.replace(state_path)

print(
    "[chengdu-weather-anomaly] "
    f"generated_at={summary.get('generated_at')} "
    f"markets={summary.get('events_analyzed')} "
    f"buckets={summary.get('core_buckets_analyzed')} "
    f"trades={summary.get('trades_analyzed')} "
    f"wallets={summary.get('unique_wallets_analyzed')} "
    f"alerts={alerts.get('alert_count')} "
    f"emitted={len(emitted)} "
    f"suppressed={len(suppressed)} "
    f"report={report_path} alerts_json={alerts_path}"
)

try:
    from chengdu_intraday_summary import build_chengdu_intraday_summary, build_log_line

    intraday_summary = build_chengdu_intraday_summary(root_path / "data" / "archive.db")
    print(build_log_line(intraday_summary))
except Exception as exc:
    print(f"[chengdu-intraday-decision] ok=0 error={type(exc).__name__}:{exc}")

TYPE_LABELS = {
    "book_inducement": "盘口诱导",
    "wallet_concentration": "钱包集中成交",
    "pressure_accumulation": "压价吸筹",
    "pump_or_inducement": "诱多拉价",
    "large_money_trade_flow": "大资金流向",
    "model_market_or_book_anomaly": "模型/盘口偏离",
}
TYPE_ORDER = [
    "book_inducement",
    "wallet_concentration",
    "pressure_accumulation",
    "pump_or_inducement",
    "large_money_trade_flow",
    "model_market_or_book_anomaly",
]
all_alerts = [a for a in alerts.get("alerts", []) if isinstance(a, dict)]
emitted_keys = {alert_key(a) for a, _reason in emitted}
groups = {}
for alert in all_alerts:
    t = str(alert.get("alert_type") or "model_market_or_book_anomaly").strip() or "model_market_or_book_anomaly"
    groups.setdefault(t, {"alerts": [], "emitted": 0})["alerts"].append(alert)
    if alert_key(alert) in emitted_keys:
        groups[t]["emitted"] += 1

for t in sorted(groups, key=lambda key: TYPE_ORDER.index(key) if key in TYPE_ORDER else 999):
    group_alerts = groups[t]["alerts"]
    buckets = ",".join(str(a.get("label") or "") for a in group_alerts[:6])
    max_risk = max((float(a.get("risk_score") or 0.0) for a in group_alerts), default=0.0)
    high_count = sum(1 for a in group_alerts if str(a.get("severity") or "").lower() == "high")
    print(
        "[chengdu-weather-alert-group] "
        f"type={t} "
        f"label={TYPE_LABELS.get(t, t)} "
        f"count={len(group_alerts)} "
        f"emitted={groups[t]['emitted']} "
        f"high={high_count} "
        f"max_risk={max_risk:.1f} "
        f"buckets={buckets}"
    )

for alert, emit_reason in emitted[:12]:
    actions = ",".join(alert.get("canonical_actions") or [])
    triggers = ",".join(alert.get("triggered_metrics") or [])
    secondary = ",".join(alert.get("secondary_evidence_types") or [])
    print(
        "[chengdu-weather-alert] "
        f"reason={emit_reason} "
        f"type={alert.get('alert_type')} "
        f"secondary={secondary} "
        f"severity={alert.get('severity')} "
        f"slug={alert.get('slug')} "
        f"bucket={alert.get('label')} "
        f"risk={float(alert.get('risk_score') or 0):.1f} "
        f"mid={alert.get('market_mid')} "
        f"gap={alert.get('market_model_gap')} "
        f"large_money={alert.get('large_money_direction')} "
        f"actions={actions} "
        f"triggers={triggers}"
    )
PY
