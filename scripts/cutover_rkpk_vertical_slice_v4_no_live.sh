#!/bin/bash
set -euo pipefail

# Narrow RKPK vertical-slice cutover:
# five-model schema -> strict-OOS P0 v4 -> DeltaBasketScore v2 -> one restart.
# market_ws and clob_order_service must remain on their current PIDs.

ROOT="/Users/xiong/weather"
TARGET_DB="$ROOT/data/bayes_basket_target_ssot.db"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
PY="$ROOT/.venv/bin/python"
PACKET="$ROOT/output/audits/bayes_basket_target_rkpk_native_00z_d2_v4_no_live_calibration_packet_complete_29d.json"
DELTA_MANIFEST="$ROOT/config/trading/delta_basket_score_v2_rkpk_no_live_bootstrap_v2.json"
STATION_POLICY_MANIFEST="$ROOT/config/trading/bayes_basket_target_target_v2_station_policy_no_live_v1.json"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
TAF_CROSSFIT="$ROOT/output/audits/bayes_basket_target_rkpk_native_00z_d2_taf_nwp_crossfit_30d_v4.json"
TAF_EXACT_JOIN="$ROOT/output/audits/bayes_basket_target_rkpk_native_00z_d2_taf_exact_join_30d_v4.json"
TAF_ARTIFACT="$ROOT/output/audits/bayes_basket_target_rkpk_native_00z_d2_taf_revision_artifact_v4.json"
TAF_TRANSPORT="$ROOT/output/audits/bayes_basket_target_rkpk_native_00z_d2_taf_transport_oos_v5_rehearsal.json"
STAMP="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/data/bayes_basket_target_ssot.pre_rkpk_v4_${STAMP}.backup.db"
ACTIVATION_BACKUP="$ROOT/data/bayes_basket_target_ssot.pre_target_v2_activation_${STAMP}.backup.db"
MIGRATION_RECEIPT="$ROOT/output/audits/bayes_basket_target_five_model_pool_apply_${STAMP}.json"
INGRESS_RECEIPT="$ROOT/output/audits/bayes_basket_target_rkpk_v4_ingress_${STAMP}.json"
ACTIVATION_RECEIPT="$ROOT/output/audits/bayes_basket_target_target_v2_activation_${STAMP}.json"
STATION_POLICY_RECEIPT="$ROOT/output/audits/bayes_basket_target_target_v2_station_policy_${STAMP}.json"
FINAL_RECEIPT="$ROOT/output/audits/bayes_basket_target_rkpk_vertical_cutover_${STAMP}.json"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-rkpk-vertical-v4-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-rkpk-vertical-v4-production-no-live" >&2
  exit 2
fi
if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0 --confirm-rkpk-vertical-v4-production-no-live" >&2
  exit 1
fi
for required in \
  "$TARGET_DB" "$TARGET_PLIST" "$PY" "$PACKET" "$DELTA_MANIFEST" \
  "$STATION_POLICY_MANIFEST" "$ACTIVATION" \
  "$TAF_CROSSFIT" "$TAF_EXACT_JOIN" "$TAF_ARTIFACT" "$TAF_TRANSPORT"; do
  if [ ! -f "$required" ]; then
    echo "required_path_missing=$required" >&2
    exit 1
  fi
done

"$PY" - \
  "$ROOT" "$PACKET" "$ACTIVATION" "$TAF_CROSSFIT" "$TAF_EXACT_JOIN" \
  "$TAF_ARTIFACT" "$TAF_TRANSPORT" <<'PY'
import sys
from pathlib import Path

root, packet_path, activation_path, crossfit_path, exact_path, artifact_path, transport_path = (
    Path(value) for value in sys.argv[1:]
)
sys.path.insert(0, str(root))

from bayes_basket_target.activation import load_target_activation  # noqa: E402
from bayes_basket_target.contracts import (  # noqa: E402
    FORMAL_ENSEMBLE_METHOD_VERSION,
)
from bayes_basket_target.resource_bounds import load_bounded_json  # noqa: E402
from bayes_basket_target.taf_nwp_transport_oos import (  # noqa: E402
    _validate_content_hash,
    _validate_crossfit,
    _validate_exact_join,
)
from bayes_basket_target.taf_revision import TafRevisionArtifact  # noqa: E402

packet = load_bounded_json(packet_path)
activation = load_target_activation(activation_path)
crossfit = load_bounded_json(crossfit_path)
exact_join = load_bounded_json(exact_path)
artifact_raw = load_bounded_json(artifact_path)
transport = load_bounded_json(transport_path)
if not all(
    isinstance(value, dict)
    for value in (packet, crossfit, exact_join, artifact_raw, transport)
):
    raise SystemExit("rkpk_v4_cutover_taf_preflight_json_invalid")
if (
    activation.get("execution_mode") != "production_no_live"
    or (activation.get("target_scope") or {}).get("strategy_version")
    != "target-v2"
    or activation.get("live_submit_authorized") is not False
    or activation.get("calls_order_gateway") is not False
):
    raise SystemExit("rkpk_v4_cutover_target_v2_activation_invalid")
ensembles = packet.get("ensemble_artifacts")
if not isinstance(ensembles, list) or len(ensembles) != 1:
    raise SystemExit("rkpk_v4_cutover_taf_preflight_ensemble_invalid")
ensemble = ensembles[0]
if (
    not isinstance(ensemble, dict)
    or ensemble.get("weight_method") != FORMAL_ENSEMBLE_METHOD_VERSION
):
    raise SystemExit("rkpk_v4_cutover_taf_preflight_method_invalid")
crossfit_sha, _rows = _validate_crossfit(crossfit)
samples = _validate_exact_join(
    exact_join,
    crossfit_sha256=crossfit_sha,
)
artifact = TafRevisionArtifact.from_mapping(artifact_raw)
_validate_content_hash(transport, name="rkpk_v4_cutover_taf_transport")
if (
    crossfit.get("method_version") != FORMAL_ENSEMBLE_METHOD_VERSION
    or crossfit.get("ensemble_artifact_content_sha256")
    != ensemble.get("content_sha256")
    or artifact.nwp_combination_artifact_sha256 != crossfit_sha
    or artifact.sample_n != len(samples)
    or transport.get("state") != "PASS"
    or transport.get("validated") is not True
    or transport.get("gaps") != []
    or transport.get("crossfit_prediction_artifact_sha256") != crossfit_sha
    or transport.get("taf_artifact_content_sha256") != artifact.content_sha256
    or (transport.get("live_fingerprint") or {}).get(
        "ensemble_artifact_content_sha256"
    ) != ensemble.get("content_sha256")
):
    raise SystemExit("rkpk_v4_cutover_taf_preflight_lineage_invalid")
if (
    int(transport.get("sample_n") or 0) < 24
    or transport.get("nll_not_worse") is not True
    or transport.get("crps_not_worse") is not True
):
    raise SystemExit("rkpk_v4_cutover_taf_preflight_oos_invalid")
PY

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

market_pid_before="$(service_pid "$MARKET_LABEL")"
order_pid_before="$(service_pid "$ORDER_LABEL")"
target_pid_before="$(service_pid "$TARGET_LABEL")"
if [ -z "$market_pid_before" ] || [ -z "$order_pid_before" ] \
  || [ -z "$target_pid_before" ]; then
  echo "rkpk_v4_cutover_requires_three_running_system_services" >&2
  exit 1
fi

/usr/bin/touch "$LIVE_PAUSE"
target_stopped=0

restore_target_on_error() {
  /usr/bin/touch "$LIVE_PAUSE"
  if [ "$target_stopped" = "1" ] \
    && ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$TARGET_PLIST" >/dev/null 2>&1 || true
  fi
}
trap restore_target_on_error ERR

/bin/launchctl bootout "system/$TARGET_LABEL"
target_stopped=1
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 0.25
done
if /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
  echo "rkpk_v4_cutover_target_did_not_stop" >&2
  exit 1
fi

"$PY" "$ROOT/scripts/migrate_bayes_basket_target_five_model_pool_schema.py" \
  --db "$TARGET_DB" \
  --output "$MIGRATION_RECEIPT" \
  --backup "$BACKUP" \
  --apply-production-no-live \
  --confirmation confirm-five-model-pool-production-no-live

"$PY" - "$TARGET_DB" <<'PY'
import sqlite3
import sys

database = sys.argv[1]
with sqlite3.connect(database, timeout=30, isolation_level=None) as conn:
    conn.execute("PRAGMA busy_timeout=30000")
    mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] or "")
    if mode.lower() != "wal":
        raise SystemExit(f"rkpk_v4_cutover_wal_enable_failed:{mode}")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("rkpk_v4_cutover_wal_quick_check_failed")
PY

"$PY" "$ROOT/scripts/ingest_bayes_basket_target_artifacts.py" \
  --packet "$PACKET" \
  --db "$TARGET_DB" \
  --apply \
  --confirm INGEST_BAYES_BASKET_TARGET_ARTIFACTS \
  --receipt "$INGRESS_RECEIPT"

"$PY" "$ROOT/scripts/apply_bayes_basket_target_delta_v2_schema.py" \
  --db "$TARGET_DB" \
  --apply \
  --confirm-reported-trajectory-no-stopped

"$PY" "$ROOT/scripts/apply_delta_basket_score_v2_no_live_bootstrap.py" \
  --db "$TARGET_DB" \
  --manifest "$DELTA_MANIFEST" \
  --apply \
  --confirm-reported-trajectory-no-stopped

"$PY" "$ROOT/scripts/apply_bayes_basket_target_target_v2_station_policy_no_live.py" \
  --db "$TARGET_DB" \
  --manifest "$STATION_POLICY_MANIFEST" \
  --receipt "$STATION_POLICY_RECEIPT" \
  --apply \
  --confirm-reported-trajectory-no-stopped TARGET_V2_STATION_POLICY_NO_LIVE

"$PY" "$ROOT/scripts/bayes_basket_target_no_live_activation_apply.py" \
  --db "$TARGET_DB" \
  --activation "$ACTIVATION" \
  --backup-db "$ACTIVATION_BACKUP" \
  --receipt "$ACTIVATION_RECEIPT"

"$PY" - \
  "$ROOT" "$TARGET_DB" "$DELTA_MANIFEST" "$STATION_POLICY_MANIFEST" \
  "$STATION_POLICY_RECEIPT" "$ACTIVATION" "$ACTIVATION_RECEIPT" <<'PY'
import json
import sqlite3
import sys

sys.path.insert(0, sys.argv[1])
from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.ssot import inspect_calibration_model_pool_schema

(
    database,
    manifest_path,
    station_manifest_path,
    station_receipt_path,
    activation_path,
    activation_receipt_path,
) = sys.argv[2:]
manifest = json.loads(open(manifest_path, encoding="utf-8").read())
station_manifest = json.loads(
    open(station_manifest_path, encoding="utf-8").read()
)
station_receipt = json.loads(
    open(station_receipt_path, encoding="utf-8").read()
)
activation = load_target_activation(activation_path)
activation_receipt = json.loads(
    open(activation_receipt_path, encoding="utf-8").read()
)
with sqlite3.connect(database, timeout=30) as conn:
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("rkpk_v4_cutover_quick_check_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise SystemExit("rkpk_v4_cutover_foreign_key_check_failed")
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
        raise SystemExit("rkpk_v4_cutover_wal_mode_missing")
    if inspect_calibration_model_pool_schema(conn).get("ok") is not True:
        raise SystemExit("rkpk_v4_cutover_five_model_schema_missing")

    activations = conn.execute(
        "SELECT artifact_kind,generation,authority_scope,count(*) "
        "FROM calibration_activation "
        "WHERE station_id='RKPK' AND ("
        "(artifact_kind='MODEL_CALIBRATION' AND generation=4) OR "
        "(artifact_kind='MODEL_ENSEMBLE' AND generation=5)) "
        "GROUP BY artifact_kind,generation,authority_scope ORDER BY 1"
    ).fetchall()
    if activations != [
        ("MODEL_CALIBRATION", 4, "PRODUCTION_NO_LIVE_ONLY", 2),
        ("MODEL_ENSEMBLE", 5, "PRODUCTION_NO_LIVE_ONLY", 1),
    ]:
        raise SystemExit(f"rkpk_v4_cutover_activation_invalid:{activations!r}")

    ensemble = conn.execute(
        "SELECT candidate_models_json,weight_method,oos_metrics_json "
        "FROM calibration_ensemble_artifact e "
        "JOIN calibration_activation a ON a.artifact_id=e.ensemble_artifact_id "
        "WHERE a.station_id='RKPK' AND a.artifact_kind='MODEL_ENSEMBLE' "
        "AND a.generation=5 AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY'"
    ).fetchone()
    if ensemble is None:
        raise SystemExit("rkpk_v4_cutover_ensemble_missing")
    if json.loads(ensemble[0]) != ["ECMWF", "GFS"]:
        raise SystemExit("rkpk_v4_cutover_ensemble_model_set_invalid")
    if ensemble[1] != "rolling_origin_probability_stack_v4_five_model_pool_min2":
        raise SystemExit("rkpk_v4_cutover_weight_method_invalid")
    if int(json.loads(ensemble[2]).get("n") or 0) != 6:
        raise SystemExit("rkpk_v4_cutover_ensemble_holdout_invalid")

    policies = conn.execute(
        "SELECT a.maturity_bin,p.strategy_version,p.oos_gate_passed "
        "FROM basket_delta_v2_policy_activation a "
        "JOIN basket_delta_v2_policy_artifact p ON p.artifact_id=a.artifact_id "
        "WHERE a.station_id='RKPK' AND a.authority_scope='PRODUCTION_NO_LIVE' "
        "AND a.strategy_version='target-v2' "
        "ORDER BY a.maturity_bin"
    ).fetchall()
    if policies != [
        ("d1", "target-v2", 0),
        ("d2", "target-v2", 0),
        ("d2_fallback", "target-v2", 0),
    ]:
        raise SystemExit(f"rkpk_v4_cutover_delta_policy_invalid:{policies!r}")
    legacy = conn.execute(
        "SELECT a.artifact_id,("
        "SELECT p.decision FROM basket_delta_v2_policy_promotion p "
        "WHERE p.artifact_id=a.artifact_id "
        "ORDER BY p.decision_seq DESC LIMIT 1) "
        "FROM basket_delta_v2_policy_activation a "
        "WHERE a.station_id='RKPK' "
        "AND a.authority_scope='PRODUCTION_NO_LIVE' "
        "AND a.strategy_version='target-v1' "
        "ORDER BY a.artifact_id"
    ).fetchall()
    if len(legacy) != 2 or any(row[1] != "SUPERSEDED" for row in legacy):
        raise SystemExit(
            f"rkpk_v4_cutover_legacy_delta_not_superseded:{legacy!r}"
        )
    station_priors = conn.execute(
        "SELECT global_prior_id,count(*) "
        "FROM basket_global_prior_artifact "
        "WHERE strategy_version='target-v2' GROUP BY global_prior_id"
    ).fetchall()
    if station_priors != [(station_manifest["global_prior_id"], 1)]:
        raise SystemExit(
            f"rkpk_v4_cutover_target_v2_global_prior_invalid:{station_priors!r}"
        )
    station_scopes = conn.execute(
        "SELECT stage,maturity_bin,count(*) "
        "FROM basket_policy_activation "
        "WHERE station_id='RKPK' AND strategy_version='target-v2' "
        "AND season_bin='summer' AND liquidity_bin='liquid' "
        "GROUP BY stage,maturity_bin ORDER BY stage,maturity_bin"
    ).fetchall()
    if station_scopes != [
        ("P0", "d1", 1),
        ("P0", "d2", 1),
        ("P0", "d2_fallback", 1),
        ("P1", "d1", 1),
        ("P1", "d2", 1),
        ("P1", "d2_fallback", 1),
    ]:
        raise SystemExit(
            f"rkpk_v4_cutover_target_v2_station_policy_invalid:{station_scopes!r}"
        )
    if (
        station_receipt.get("state")
        not in {"COMMITTED", "UNCHANGED_ZERO_WRITE"}
        or int(station_receipt.get("resolved_scope_count") or 0) != 6
        or station_receipt.get("strategy_version") != "target-v2"
        or station_receipt.get("live_submit_authorized") is not False
        or station_receipt.get("calls_order_gateway") is not False
        or int(station_receipt.get("orders_submitted", -1)) != 0
    ):
        raise SystemExit("rkpk_v4_cutover_station_policy_receipt_invalid")
    activation_row = conn.execute(
        "SELECT generation,state,activation_sha256,activation_json "
        "FROM basket_strategy_activation_state WHERE singleton=1"
    ).fetchone()
    if (
        activation_row is None
        or activation_row[1] != "enabled"
        or activation_row[2] != activation["activation_contract_sha256"]
        or json.loads(activation_row[3]).get("target_scope", {}).get(
            "strategy_version"
        ) != "target-v2"
        or int(activation_row[0])
        != int(activation_receipt.get("generation") or -1)
    ):
        raise SystemExit("rkpk_v4_cutover_target_v2_activation_drift")
    expected_fill = len(manifest["maturity_bins"]) * len(manifest["price_grid"])
    fill_count = conn.execute(
        "SELECT count(*) FROM basket_delta_v2_fill_artifact "
        "WHERE station_id='RKPK' AND authority_scope='PRODUCTION_NO_LIVE'"
    ).fetchone()[0]
    if int(fill_count) < expected_fill:
        raise SystemExit("rkpk_v4_cutover_delta_fill_grid_incomplete")
PY

/bin/launchctl bootstrap system "$TARGET_PLIST"
target_stopped=0
deadline=$((SECONDS + 30))
target_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_pid_after="$(service_pid "$TARGET_LABEL")"
  if [ -n "$target_pid_after" ] \
    && [ "$target_pid_after" != "$target_pid_before" ]; then
    break
  fi
  /bin/sleep 0.25
done
if [ -z "$target_pid_after" ] \
  || [ "$target_pid_after" = "$target_pid_before" ]; then
  echo "rkpk_v4_cutover_target_pid_did_not_advance" >&2
  exit 1
fi

market_pid_after="$(service_pid "$MARKET_LABEL")"
order_pid_after="$(service_pid "$ORDER_LABEL")"
if [ "$market_pid_after" != "$market_pid_before" ] \
  || [ "$order_pid_after" != "$order_pid_before" ]; then
  echo "rkpk_v4_cutover_non_target_pid_changed" >&2
  exit 1
fi
if [ ! -f "$LIVE_PAUSE" ]; then
  echo "rkpk_v4_cutover_live_pause_missing" >&2
  exit 1
fi

"$PY" - \
  "$FINAL_RECEIPT" "$BACKUP" "$MIGRATION_RECEIPT" "$INGRESS_RECEIPT" \
  "$ACTIVATION_BACKUP" "$ACTIVATION_RECEIPT" "$STATION_POLICY_RECEIPT" \
  "$target_pid_before" "$target_pid_after" "$market_pid_after" \
  "$order_pid_after" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    out,
    backup,
    migration,
    ingress,
    activation_backup,
    activation_receipt,
    station_policy_receipt,
) = map(
    Path, sys.argv[1:8]
)
identity = {
    "schema": "rkpk_vertical_slice_v4_no_live_cutover_receipt_v1",
    "state": "COMMITTED",
    "created_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z"),
    "backup": str(backup),
    "migration_receipt": str(migration),
    "ingress_receipt": str(ingress),
    "activation_backup": str(activation_backup),
    "activation_receipt": str(activation_receipt),
    "station_policy_receipt": str(station_policy_receipt),
    "strategy_version": "target-v2",
    "reported_trajectory_no_pid_before": int(sys.argv[8]),
    "reported_trajectory_no_pid_after": int(sys.argv[9]),
    "market_ws_pid": int(sys.argv[10]),
    "clob_order_service_pid": int(sys.argv[11]),
    "execution_mode": "production_no_live",
    "live_submit_authorized": False,
    "authorizes_order": False,
    "calls_order_gateway": False,
    "orders_submitted": 0,
}
out.write_text(
    json.dumps(identity, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps(identity, ensure_ascii=False, sort_keys=True))
PY

trap - ERR
echo "rkpk_vertical_slice_v4_no_live_cutover_ok"
echo "reported_trajectory_no_pid=$target_pid_before->$target_pid_after"
echo "market_ws_pid=$market_pid_after"
echo "clob_order_service_pid=$order_pid_after"
echo "live_pause_retained=true"
