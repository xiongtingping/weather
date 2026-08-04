#!/usr/bin/env python3
"""Materialize strict no-live activation from retained deterministic OOS.

The Open-Meteo AIFS/ICON historical reports are already closed-day, exact
20-train/8-holdout evidence.  They are deterministic reports rather than the
member-run shape consumed by the native ensemble persistence writer.  This
adapter converts that *existing* evidence into the same append-only
``PRODUCTION_NO_LIVE_ONLY`` system/cross activation tables.  It never fetches
data, changes native arrival/P1/formal authority, or calls the order gateway.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    HISTORICAL_OOS_TEMPORAL_SCOPE,
    HISTORICAL_REPUBLISHER_RECEIPT,
    P0_PROBABILITY_PATHWAY,
    persist_native_ensemble_oos,
    _validate_native_ensemble_oos_report,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_AIFS = ROOT / "output/audits/open_meteo_aifs_deterministic_historical_oos_20260802.json"
DEFAULT_ICON = ROOT / "output/audits/open_meteo_icon_global_deterministic_historical_oos_20260802.json"
DEFAULT_OUTPUT = ROOT / "output/audits/republisher_deterministic_activation_20260803.json"
AIFS_ID = "ECMWF_AIFS_ENS_GLOBAL"
ICON_ID = "DWD_ICON_GLOBAL_EPS"
RUN_CYCLE = "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
THRESHOLD_PROFILE = "FORMAL_PROMOTION_20_TRAIN_8_HOLDOUT"
MIN_TRAIN = 20
MIN_HOLDOUT = 8


def _load(path: Path) -> dict[str, Any]:
    raw = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(raw, Mapping):
        raise RuntimeError(f"json_object_required:{path}")
    supplied = str(raw.get("content_sha256") or "")
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise RuntimeError(f"content_sha256_mismatch:{path}")
    return dict(raw)


def _bucket(value: float) -> str:
    return str(math.floor(float(value) + 0.5))


def _metrics(probability: Mapping[str, float], actual: float) -> dict[str, float]:
    actual_bucket = _bucket(actual)
    keys = sorted({int(key) for key in probability} | {int(actual_bucket)})
    brier = sum(
        (float(probability.get(str(key), 0.0)) - (1.0 if str(key) == actual_bucket else 0.0)) ** 2
        for key in keys
    )
    nll = -math.log(max(float(probability.get(actual_bucket, 0.0)), 1e-12))
    crps = 0.0
    for key in range(min(keys), max(keys) + 1):
        forecast_cdf = sum(float(value) for bucket, value in probability.items() if int(bucket) <= key)
        observed_cdf = 1.0 if int(actual_bucket) <= key else 0.0
        crps += (forecast_cdf - observed_cdf) ** 2
    return {"brier": brier, "nll": nll, "crps": crps}


def _mean_metrics(rows: list[Mapping[str, float]]) -> dict[str, float]:
    return {
        key: sum(float(row[key]) for row in rows) / len(rows)
        for key in ("brier", "nll", "crps")
    }


def _correlation(left: list[float], right: list[float]) -> float:
    if len(left) < 2 or len(left) != len(right):
        return 0.0
    lm = sum(left) / len(left)
    rm = sum(right) / len(right)
    numerator = sum((a - lm) * (b - rm) for a, b in zip(left, right))
    lscale = math.sqrt(sum((a - lm) ** 2 for a in left))
    rscale = math.sqrt(sum((b - rm) ** 2 for b in right))
    return 0.0 if lscale == 0.0 or rscale == 0.0 else max(-1.0, min(1.0, numerator / (lscale * rscale)))


def _timezone(conn: sqlite3.Connection, station: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT settlement_definition FROM calibration_station_registry WHERE station_id=?",
        (station,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"station_unregistered:{station}")
    rows = conn.execute(
        "SELECT DISTINCT settlement_timezone FROM basket_target_market_registry "
        "WHERE station_id=? AND settlement_timezone<>''",
        (station,),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"station_timezone_not_unique:{station}")
    return str(row[0]), str(rows[0][0])


def _candidate_index(report: Mapping[str, Any], model: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for raw in report.get("candidates") or []:
        if not isinstance(raw, Mapping):
            continue
        station = str(raw.get("station_id") or "").strip().upper()
        if not station or station in result:
            raise RuntimeError(f"duplicate_candidate:{model}:{station}")
        if (
            raw.get("state") != "PRODUCTION_NO_LIVE_OOS_PASS"
            or int(raw.get("train_n") or 0) < MIN_TRAIN
            or int(raw.get("holdout_n") or 0) < MIN_HOLDOUT
            or int(raw.get("future_event_count") or 0) != 0
            or raw.get("oos_event_temporal_scope") != HISTORICAL_OOS_TEMPORAL_SCOPE
            or raw.get("source_role") != "aggregator_republisher"
            or raw.get("transport_provider_id") != "OPEN_METEO"
        ):
            continue
        result[station] = raw
    return result


def _system_evaluation(
    candidate: Mapping[str, Any],
    *,
    system_id: str,
    timezone_name: str,
) -> dict[str, Any]:
    prediction_rows = list(candidate.get("predictions") or [])
    parameters = {
        "bias_c": float(candidate.get("train_bias_c") or 0.0),
        # The deterministic report supplies the strict bias/OOS contract;
        # member spread is measured from the current packet at compile time.
        "spread_factor": 1.0,
        "reliability_weight": 2.0 / 3.0,
        "climate_samples_c": [float(candidate.get("train_climatology_tmax_c") or 0.0)],
        "train_rmse_c": max(float((candidate.get("model_metrics") or {}).get("rmse_c") or 1.0), 0.1),
        "train_mean_spread_c": max(float((candidate.get("model_metrics") or {}).get("rmse_c") or 1.0), 0.1),
    }
    predictions: list[dict[str, Any]] = []
    metrics: list[dict[str, float]] = []
    for raw in prediction_rows:
        calibrated = float(raw.get("calibrated_daily_max_c"))
        actual = float(raw.get("actual_tmax_c"))
        probability = {_bucket(calibrated): 1.0}
        metrics.append(_metrics(probability, actual))
        contract_id = str(raw.get("historical_oos_contract_id") or "")
        member_shas = [str(value) for value in raw.get("member_run_content_shas") or []]
        if not contract_id or not member_shas:
            raise RuntimeError(f"deterministic_prediction_lineage_missing:{system_id}")
        predictions.append({
            "event_date": str(raw["event_date"]),
            "decision_at_utc": str(raw["decision_at_utc"]),
            "trained_through_event_date": str(candidate["train_end_date"]),
            "settlement_timezone": timezone_name,
            "probability": probability,
            "system_member_set_shas": {system_id: member_shas[0]},
            "historical_oos_contract_ids": [contract_id],
            "prediction_oos": 1,
        })
    return {
        "transport_provider_id": "OPEN_METEO",
        "holdout_source_class": HISTORICAL_REPUBLISHER_RECEIPT,
        "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
        "future_event_count": 0,
        "train_source_counts": {
            "FORWARD_LOCAL_RECEIPT": 0,
            "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE": 0,
            "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY": 0,
            HISTORICAL_REPUBLISHER_RECEIPT: int(candidate["train_n"]),
        },
        "train_start_date": str(candidate["train_start_date"]),
        "train_end_date": str(candidate["train_end_date"]),
        "holdout_start_date": str(candidate["holdout_start_date"]),
        "holdout_end_date": str(candidate["holdout_end_date"]),
        "train_n": int(candidate["train_n"]),
        "holdout_n": int(candidate["holdout_n"]),
        "parameters": parameters,
        "oos_metrics": _mean_metrics(metrics),
        "predictions": predictions,
    }


def _build_report(
    conn: sqlite3.Connection,
    *,
    aifs: Mapping[str, Any],
    icon: Mapping[str, Any],
    station: str,
    generated_at_utc: str,
) -> dict[str, Any]:
    aifs_candidate = _candidate_index(aifs, "AIFS")[station]
    icon_candidate = _candidate_index(icon, "ICON")[station]
    if any(
        aifs_candidate[key] != icon_candidate[key]
        for key in (
            "season_bin", "lead_bucket", "run_cycle", "train_start_date",
            "train_end_date", "holdout_start_date", "holdout_end_date",
            "train_n", "holdout_n",
        )
    ):
        raise RuntimeError(f"cohort_mismatch:{station}")
    definition, timezone_name = _timezone(conn, station)
    aifs_predictions = list(aifs_candidate["predictions"])
    icon_predictions = list(icon_candidate["predictions"])
    if len(aifs_predictions) != MIN_HOLDOUT or len(icon_predictions) != MIN_HOLDOUT:
        raise RuntimeError(f"holdout_count_invalid:{station}")
    cross_predictions: list[dict[str, Any]] = []
    cross_metrics: list[dict[str, float]] = []
    aifs_errors: list[float] = []
    icon_errors: list[float] = []
    for aifs_row, icon_row in zip(aifs_predictions, icon_predictions):
        if str(aifs_row["event_date"]) != str(icon_row["event_date"]):
            raise RuntimeError(f"holdout_dates_mismatch:{station}")
        if float(aifs_row["actual_tmax_c"]) != float(icon_row["actual_tmax_c"]):
            raise RuntimeError(f"truth_mismatch:{station}:{aifs_row['event_date']}")
        aifs_value = float(aifs_row["calibrated_daily_max_c"])
        icon_value = float(icon_row["calibrated_daily_max_c"])
        actual = float(aifs_row["actual_tmax_c"])
        probability: dict[str, float] = {}
        for key, value in ({_bucket(aifs_value): 0.5, _bucket(icon_value): 0.5}).items():
            probability[key] = probability.get(key, 0.0) + value
        cross_metrics.append(_metrics(probability, actual))
        aifs_errors.append(aifs_value - actual)
        icon_errors.append(icon_value - actual)
        aifs_contract = str(aifs_row["historical_oos_contract_id"])
        icon_contract = str(icon_row["historical_oos_contract_id"])
        cross_predictions.append({
            "event_date": str(aifs_row["event_date"]),
            "decision_at_utc": max(str(aifs_row["decision_at_utc"]), str(icon_row["decision_at_utc"])),
            "trained_through_event_date": str(aifs_candidate["train_end_date"]),
            "settlement_timezone": timezone_name,
            "probability": probability,
            "system_member_set_shas": {
                AIFS_ID: str((aifs_row.get("member_run_content_shas") or [""])[0]),
                ICON_ID: str((icon_row.get("member_run_content_shas") or [""])[0]),
            },
            "historical_oos_contract_ids": [aifs_contract, icon_contract],
            "prediction_oos": 1,
        })
    selected_ids = sorted([AIFS_ID, ICON_ID])
    cycle_mapping = {AIFS_ID: RUN_CYCLE, ICON_ID: RUN_CYCLE}
    family_ids = {AIFS_ID: "ECMWF", ICON_ID: "DWD_ICON_EPS"}
    family_weights = {"DWD_ICON_EPS": 0.5, "ECMWF": 0.5}
    system_weights = {AIFS_ID: 0.5, ICON_ID: 0.5}
    candidate_identity = {
        "schema": "native_ensemble_strict_oos_candidate_v1",
        "state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "station_id": station,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "season_bin": str(aifs_candidate["season_bin"]),
        "lead_bucket": str(aifs_candidate["lead_bucket"]),
        "run_cycle": RUN_CYCLE,
        "run_cycle_scope": "SAME_NATIVE_CYCLE",
        "run_cycle_by_system": cycle_mapping,
        "run_cycle_by_system_sha256": content_sha256(cycle_mapping),
        "settlement_definition": definition,
        "settlement_timezone": timezone_name,
        "selected_system_ids": selected_ids,
        "selected_system_count": 2,
        "available_system_set_hash": content_sha256(selected_ids),
        "independence_family_ids": sorted(set(family_ids.values())),
        "independent_family_count": 2,
        "system_family_ids": family_ids,
        "system_transport_provider_ids": {AIFS_ID: "OPEN_METEO", ICON_ID: "OPEN_METEO"},
        "system_weights": system_weights,
        "family_weights": family_weights,
        "family_weight_caps": {"DWD_ICON_EPS": 0.75, "ECMWF": 0.75},
        "threshold_profile": THRESHOLD_PROFILE,
        "min_train_days": MIN_TRAIN,
        "min_holdout_days": MIN_HOLDOUT,
        "formal_promotion_min_train_days": MIN_TRAIN,
        "formal_promotion_min_holdout_days": MIN_HOLDOUT,
        "train_start_date": str(aifs_candidate["train_start_date"]),
        "train_end_date": str(aifs_candidate["train_end_date"]),
        "holdout_start_date": str(aifs_candidate["holdout_start_date"]),
        "holdout_end_date": str(aifs_candidate["holdout_end_date"]),
        "rolling_origin_fold_count": MIN_HOLDOUT,
        "effective_sample_n": float(MIN_TRAIN),
        "oos_metrics": _mean_metrics(cross_metrics),
        "climatology_baseline_metrics": dict(aifs_candidate["climatology_baseline_metrics"]),
        "cross_system_residual_correlation": {
            AIFS_ID: {AIFS_ID: 1.0, ICON_ID: _correlation(aifs_errors, icon_errors)},
            ICON_ID: {AIFS_ID: _correlation(aifs_errors, icon_errors), ICON_ID: 1.0},
        },
        "predictions": cross_predictions,
        "system_evaluations": {
            AIFS_ID: _system_evaluation(aifs_candidate, system_id=AIFS_ID, timezone_name=timezone_name),
            ICON_ID: _system_evaluation(icon_candidate, system_id=ICON_ID, timezone_name=timezone_name),
        },
        "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
        "future_event_count": 0,
        "formal_p0_eligible": False,
        "source_role": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
    }
    candidate = {**candidate_identity, "content_sha256": content_sha256(candidate_identity)}
    report_identity = {
        "schema": "native_ensemble_strict_forward_oos_v1",
        "station_id": station,
        "as_of_utc": generated_at_utc,
        "min_train_days": MIN_TRAIN,
        "min_holdout_days": MIN_HOLDOUT,
        "threshold_profile": THRESHOLD_PROFILE,
        "historical_event_date_cutoff": str(aifs_candidate["holdout_end_date"]),
        "formal_promotion_threshold_profile": THRESHOLD_PROFILE,
        "formal_promotion_min_train_days": MIN_TRAIN,
        "formal_promotion_min_holdout_days": MIN_HOLDOUT,
        "registered_system_count": 2,
        "readiness": [],
        "cross_candidates": [candidate],
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "formal_p0_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**report_identity, "content_sha256": content_sha256(report_identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--aifs", type=Path, default=DEFAULT_AIFS)
    parser.add_argument("--icon", type=Path, default=DEFAULT_ICON)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    db = args.db.expanduser().resolve(strict=True)
    aifs = _load(args.aifs)
    icon = _load(args.icon)
    aifs_by_station = _candidate_index(aifs, "AIFS")
    icon_by_station = _candidate_index(icon, "ICON")
    requested = {str(value).strip().upper() for value in args.station_id if str(value).strip()}
    stations = sorted((set(aifs_by_station) & set(icon_by_station)) & (requested or (set(aifs_by_station) & set(icon_by_station))))
    generated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    mode = "rwc" if args.commit else "ro"
    reports: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    with sqlite3.connect(f"file:{db}?mode={mode}", uri=True, timeout=60.0) as conn:
        if not args.commit:
            conn.execute("PRAGMA query_only=ON")
        for station in stations:
            try:
                report = _build_report(conn, aifs=aifs, icon=icon, station=station, generated_at_utc=generated_at)
                _validate_native_ensemble_oos_report(conn, report)
                reports.append(report)
            except Exception as exc:
                blocked.append({"station_id": station, "reason": f"{type(exc).__name__}:{exc}"})
        persist_receipts: list[dict[str, Any]] = []
        if args.commit and reports:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for report in reports:
                    persist_receipts.append(persist_native_ensemble_oos(conn, report, activated_at_utc=generated_at))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    identity = {
        "schema": "republisher_deterministic_activation_materialization_v1",
        "generated_at_utc": generated_at,
        "database": str(db),
        "source_reports": [
            {"path": str(args.aifs.expanduser().resolve()), "content_sha256": aifs["content_sha256"]},
            {"path": str(args.icon.expanduser().resolve()), "content_sha256": icon["content_sha256"]},
        ],
        "station_count": len(stations),
        "report_count": len(reports),
        "blocked": blocked,
        "commit": bool(args.commit),
        "persist_receipts": persist_receipts,
        "formal_p0_eligible": False,
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "policy": {
            "official_native_first": True,
            "future_data_is_not_oos": True,
            "historical_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
            "minimum_train_days": MIN_TRAIN,
            "minimum_holdout_days": MIN_HOLDOUT,
            "source_role": "aggregator_republisher",
            "transport_provider_id": "OPEN_METEO",
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json_if_changed(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "state": "COMMITTED" if args.commit else "READY_READ_ONLY",
        "station_count": len(stations),
        "report_count": len(reports),
        "blocked_count": len(blocked),
        "activations": sum(int(r.get("counts", {}).get("activations", 0)) for r in persist_receipts),
        "system_artifacts": sum(int(r.get("counts", {}).get("system_artifacts", 0)) for r in persist_receipts),
        "cross_artifacts": sum(int(r.get("counts", {}).get("cross_artifacts", 0)) for r in persist_receipts),
        "output": str(args.output.expanduser().resolve()),
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
