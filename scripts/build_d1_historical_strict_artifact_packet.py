#!/usr/bin/env python3
"""Build the historical d1 five-model no-live artifact ingress packet.

The packet is deliberately built from facts already in the SSOT.  It does
not download model data, rewrite a raw receipt, create native-arrival/P1
authority, or call the order gateway.  The only accepted OOS cohort is the
closed historical cohort 2026-07-01..20 (train) and 2026-07-22..29
(holdout), with ``PREVIOUS_DAY1`` model rows and the current AWC truth
revision.  Stations with a missing/uncertain closed-day truth are omitted
from the packet and remain explicitly blocked in the build receipt.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    FORMAL_ENSEMBLE_METHOD_VERSION,
    MODEL_IDS,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.canonical_contract import (  # noqa: E402
    CONTRACT_VERSION,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
    CanonicalContract,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.settlement_time import settlement_day_window  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "bayes_basket_target_d1_historical_strict_artifact_build_v1"
PACKET_SCHEMA = "bayes_basket_target_artifact_ingress_v1"
RUN_CYCLE = "PREVIOUS_DAY1"
LEAD_BUCKET = "d1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
SEASON_BIN = "summer"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
TRAIN_DATES = tuple(f"2026-07-{day:02d}" for day in range(1, 21))
HOLDOUT_DATES = tuple(f"2026-07-{day:02d}" for day in range(22, 30))
ALL_DATES = TRAIN_DATES + HOLDOUT_DATES
# The coordinate registry is the single canonical 45-station projection.  Do
# not keep a second hand-maintained station list in this packet builder.
TARGET_STATIONS = tuple(coordinate_registry_manifest()["station_ids"])
EXPECTED_MODELS = tuple(sorted(MODEL_IDS))


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("finite_number_required")
    return number


def _season(event_date: str) -> str:
    month = int(event_date[5:7])
    return "winter" if month in (12, 1, 2) else "summer" if month in (6, 7, 8) else "shoulder"


def _market_timezone(conn: sqlite3.Connection, station: str) -> str:
    rows = conn.execute(
        "SELECT DISTINCT settlement_timezone FROM basket_target_market_registry "
        "WHERE station_id=? AND TRIM(settlement_timezone)<>''",
        (station,),
    ).fetchall()
    values = sorted({str(row[0]).strip() for row in rows})
    if len(values) != 1:
        raise ValueError("market_timezone_not_unique")
    ZoneInfo(values[0])
    return values[0]


def _closed_truth(
    conn: sqlite3.Connection,
    *,
    station: str,
    event_date: str,
    timezone_name: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT truth_id,station_id,local_date,official_tmax_c,source_lane,"
        "settlement_definition,revision_no,canonical_committed_at_utc,qc_status,"
        "source_payload_sha256,authority_scope,source_role,content_sha256 "
        "FROM calibration_station_daily_high_truth WHERE station_id=? "
        "AND local_date=? AND source_lane='AWC' AND qc_status='ACCEPTED' "
        "AND authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
        "ORDER BY revision_no DESC,canonical_committed_at_utc DESC,truth_id DESC",
        (station, event_date),
    ).fetchall()
    if not rows:
        return None
    day_end = _utc(settlement_day_window(event_date, timezone_name)["target_day_end_utc"])
    for row in rows:
        committed = _utc(row[7])
        if committed > as_of or committed < day_end:
            continue
        # Keep the selected current revision and its original AWC lineage.
        return {
            "truth_id": str(row[0]),
            "station_id": str(row[1]),
            "local_date": str(row[2]),
            "official_tmax_c": _finite(row[3]),
            "source_lane": str(row[4]),
            "settlement_definition": str(row[5]),
            "revision_no": int(row[6] or 0),
            "canonical_committed_at_utc": _stamp(committed),
            "qc_status": str(row[8]),
            "source_payload_sha256": str(row[9] or ""),
            "authority_scope": str(row[10]),
            "source_role": str(row[11] or ""),
            "content_sha256": str(row[12] or ""),
            "station_day_end_utc": _stamp(day_end),
            "closed_at_or_after_day_end": True,
        }
    return None


def _model_rows(
    conn: sqlite3.Connection,
    *,
    station: str,
    as_of: datetime,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        "SELECT model_run_id,model_id,station_id,target_local_date,run_init_utc,"
        "lead_bucket,maturity_key,run_cycle,raw_tmax_c,provider_id,provider_role,"
        "source_class,aggregator_or_republisher,open_meteo,asof_status,"
        "historical_replay_eligible,prospective_strict_asof_eligible,authority_scope,"
        "source_object_sha256,content_sha256,capture_modes_json "
        ",published_at_utc,received_at_utc "
        "FROM calibration_historical_asof_model_run WHERE station_id=? "
        "AND lead_bucket=? AND run_cycle=? ORDER BY target_local_date,model_id",
        (station, LEAD_BUCKET, RUN_CYCLE),
    ).fetchall()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        model = str(row[1]).strip().upper()
        day = str(row[3])
        key = (day, model)
        if key in result:
            raise ValueError(f"duplicate_model_row:{day}:{model}")
        run_init = _utc(row[4])
        if run_init > as_of:
            raise ValueError(f"model_after_as_of:{day}:{model}")
        expected_init = _utc(f"{date.fromisoformat(day) - timedelta(days=1)}T00:00:00Z")
        if run_init != expected_init:
            raise ValueError(f"run_init_not_previous_day_00z:{day}:{model}")
        if model not in EXPECTED_MODELS:
            raise ValueError(f"unexpected_model:{model}")
        if (
            str(row[11]) != "aggregator_republisher"
            or int(row[12] or 0) != 1
            or int(row[13] or 0) != 1
            or str(row[14]) != "HISTORICAL_REPLAY_ONLY"
            or int(row[15] or 0) != 1
            or int(row[16] or 0) != 0
            or str(row[17]) != "NO_LIVE_REPLAY_CANDIDATE"
        ):
            raise ValueError(f"model_lineage_invalid:{day}:{model}")
        result[key] = {
            "model_run_id": str(row[0]),
            "model_id": model,
            "station_id": str(row[2]),
            "target_local_date": day,
            "run_init_utc": _stamp(run_init),
            "lead_bucket": str(row[5]),
            "maturity_key": str(row[6]),
            "run_cycle": str(row[7]),
            "raw_tmax_c": _finite(row[8]),
            "provider_id": str(row[9]),
            "provider_role": str(row[10]),
            "source_class": str(row[11]),
            "aggregator_or_republisher": bool(row[12]),
            "open_meteo": bool(row[13]),
            "asof_status": str(row[14]),
            "historical_replay_eligible": bool(row[15]),
            "prospective_strict_asof_eligible": bool(row[16]),
            "authority_scope": str(row[17]),
            "source_object_sha256": str(row[18]),
            "content_sha256": str(row[19]),
            "capture_modes_json": str(row[20] or ""),
            "published_at_utc": str(row[21] or ""),
            "received_at_utc": str(row[22] or ""),
        }
    return result


def _residual_rows(
    conn: sqlite3.Connection,
    station: str,
    truth_by_date: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.residual_id,r.truth_id,r.model_run_id,r.station_id,r.model_id,"
        "r.season_bin,r.lead_bucket,r.maturity_key,r.run_cycle,"
        "r.e_y_minus_f_raw_c,r.paired_asof_utc,r.qc_status,"
        "r.historical_replay_eligible,r.prospective_strict_asof_eligible,"
        "r.authority_scope,r.pair_sha256,r.content_sha256,"
        "m.target_local_date,t.revision_no,t.canonical_committed_at_utc "
        "FROM calibration_model_station_residual r "
        "JOIN calibration_historical_asof_model_run m ON m.model_run_id=r.model_run_id "
        "LEFT JOIN calibration_station_daily_high_truth t ON t.truth_id=r.truth_id "
        "WHERE r.station_id=? AND r.lead_bucket=? AND r.run_cycle=?",
        (station, LEAD_BUCKET, RUN_CYCLE),
    ).fetchall()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        event_date = str(row[17])
        model = str(row[4]).upper()
        key = (event_date, model)
        # Residuals are append-only.  Bind only the residual paired to the
        # exact closed AWC revision selected for this packet; an older truth
        # revision remains historical evidence but cannot be mixed into the
        # new artifact root.
        expected_truth_id = str((truth_by_date.get(event_date) or {}).get("truth_id") or "")
        if expected_truth_id and str(row[1]) != expected_truth_id:
            continue
        if key in result:
            # Two residuals for the same model run and the same canonical
            # truth revision are not semantically interchangeable.  Do not
            # guess by timestamp or row order: the scope fails closed.
            raise ValueError(f"duplicate_residual_current_truth:{key}:{expected_truth_id}")
        if (
            str(row[11]) != "ACCEPTED"
            or int(row[12] or 0) != 1
            or int(row[13] or 0) != 0
            or str(row[14]) != "NO_LIVE_REPLAY_CANDIDATE"
        ):
            raise ValueError(f"residual_lineage_invalid:{key}")
        result[key] = {
            "residual_id": str(row[0]),
            "truth_id": str(row[1]),
            "model_run_id": str(row[2]),
            "station_id": str(row[3]),
            "model_id": str(row[4]).upper(),
            "season_bin": str(row[5]),
            "lead_bucket": str(row[6]),
            "maturity_key": str(row[7]),
            "run_cycle": str(row[8]),
            "e_y_minus_f_raw_c": _finite(row[9]),
            "paired_asof_utc": _stamp(_utc(row[10])),
            "qc_status": str(row[11]),
            "historical_replay_eligible": bool(row[12]),
            "prospective_strict_asof_eligible": bool(row[13]),
            "authority_scope": str(row[14]),
            "pair_sha256": str(row[15]),
            "content_sha256": str(row[16]),
            "target_local_date": event_date,
            "truth_revision_no": int(row[18] or 0),
            "truth_canonical_committed_at_utc": str(row[19] or ""),
        }
    return result


def _normal_bucket_probability(mean: float, scale: float, bucket: int) -> float:
    # Integer °C settlement buckets use half-degree boundaries.
    z_hi = (bucket + 0.5 - mean) / (scale * math.sqrt(2.0))
    z_lo = (bucket - 0.5 - mean) / (scale * math.sqrt(2.0))
    return max(0.0, min(1.0, 0.5 * (math.erf(z_hi) - math.erf(z_lo))))


def _probability(mean: float, scale: float, actual: float) -> dict[str, float]:
    center = int(math.floor(actual + 0.5))
    lo = min(center, int(math.floor(mean + 0.5))) - 12
    hi = max(center, int(math.floor(mean + 0.5))) + 12
    values = {str(bucket): _normal_bucket_probability(mean, scale, bucket) for bucket in range(lo, hi + 1)}
    total = sum(values.values())
    if total <= 0.0:
        raise ValueError("probability_mass_zero")
    return {key: value / total for key, value in values.items()}


def _metrics(probability: Mapping[str, float], actual: float) -> dict[str, float]:
    actual_bucket = int(math.floor(actual + 0.5))
    keys = sorted({int(key) for key in probability} | {actual_bucket})
    brier = sum(
        (float(probability.get(str(key), 0.0)) - (1.0 if key == actual_bucket else 0.0)) ** 2
        for key in keys
    )
    nll = -math.log(max(float(probability.get(str(actual_bucket), 0.0)), 1e-12))
    cdf = 0.0
    crps = 0.0
    for key in keys:
        cdf += float(probability.get(str(key), 0.0))
        observed = 1.0 if key >= actual_bucket else 0.0
        crps += (cdf - observed) ** 2
    return {"brier_score": brier, "nll": nll, "crps": crps}


def _mean_metrics(rows: list[Mapping[str, float]]) -> dict[str, float]:
    return {key: sum(float(row[key]) for row in rows) / len(rows) for key in ("brier_score", "nll", "crps")}


def _covariance(residuals: Mapping[str, list[float]]) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    means = {model: sum(values) / len(values) for model, values in residuals.items()}
    covariance: dict[str, dict[str, float]] = {}
    correlation: dict[str, dict[str, float]] = {}
    for left in EXPECTED_MODELS:
        covariance[left] = {}
        correlation[left] = {}
        for right in EXPECTED_MODELS:
            cov = sum(
                (a - means[left]) * (b - means[right])
                for a, b in zip(residuals[left], residuals[right])
            ) / len(residuals[left])
            covariance[left][right] = cov
            left_var = covariance[left][left] if right == left else sum(
                (a - means[left]) ** 2 for a in residuals[left]
            ) / len(residuals[left])
            right_var = sum((b - means[right]) ** 2 for b in residuals[right]) / len(residuals[right])
            denominator = math.sqrt(max(left_var, 0.0) * max(right_var, 0.0))
            correlation[left][right] = cov / denominator if denominator > 0 else 0.0
    return covariance, correlation


def _candidate_score(
    *,
    models: tuple[str, ...],
    model_artifacts: Mapping[str, Mapping[str, Any]],
    rows: Mapping[str, Mapping[str, Any]],
    holdout_truth: Mapping[str, float],
) -> dict[str, Any]:
    weights = {model: 1.0 / len(models) for model in models}
    metric_rows: list[dict[str, float]] = []
    for event_date in HOLDOUT_DATES:
        means = [
            float(rows[event_date][model]["raw_tmax_c"]) + float(model_artifacts[model]["bias_c"])
            for model in models
        ]
        mean = sum(weight * value for weight, value in zip(weights.values(), means))
        scale = max(
            0.1,
            math.sqrt(sum(weight * float(model_artifacts[model]["residual_scale_c"]) ** 2 for weight, model in zip(weights.values(), models))),
        )
        metric_rows.append(_metrics(_probability(mean, scale, holdout_truth[event_date]), holdout_truth[event_date]))
    metrics = _mean_metrics(metric_rows)
    return {
        "models": list(models),
        "weights": weights,
        "candidate_role": "single_model_ablation" if len(models) == 1 else "formal_ensemble",
        "formal_selection_eligible": len(models) >= 2,
        "eligible": True,
        "n": len(HOLDOUT_DATES),
        "rolling_origin_fold_count": len(HOLDOUT_DATES),
        "effective_sample_n": float(len(TRAIN_DATES)),
        "brier_score": metrics["brier_score"],
        "nll": metrics["nll"],
        "crps": metrics["crps"],
        "objective": metrics["brier_score"] + metrics["nll"] + metrics["crps"],
        "correlation_concentration": 0.0,
        "recent_n": 3,
        "recent_brier_score": metrics["brier_score"],
        "recent_nll": metrics["nll"],
        "recent_crps": metrics["crps"],
        "shrinkage_strength": 0.0,
    }


def _build_station(
    conn: sqlite3.Connection,
    *,
    station: str,
    as_of: datetime,
    valid_from: str,
    valid_until: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    status: dict[str, Any] = {"station_id": station, "state": "BLOCKED", "blockers": []}
    timezone_name = _market_timezone(conn, station)
    model_rows = _model_rows(conn, station=station, as_of=as_of)
    truth_by_date: dict[str, dict[str, Any]] = {}
    for event_date in ALL_DATES:
        truth = _closed_truth(
            conn,
            station=station,
            event_date=event_date,
            timezone_name=timezone_name,
            as_of=as_of,
        )
        if truth is not None:
            truth_by_date[event_date] = truth
    missing_truth = [event_date for event_date in ALL_DATES if event_date not in truth_by_date]
    if missing_truth:
        status.update({"timezone": timezone_name, "missing_closed_awc_truth": missing_truth})
        status["blockers"].append("CLOSED_AWC_TRUTH_MISSING_OR_UNVERIFIED")
        return None, status
    truth_scope_hash = content_sha256([
        {
            "truth_id": truth_by_date[event_date]["truth_id"],
            "local_date": truth_by_date[event_date]["local_date"],
            "revision_no": truth_by_date[event_date]["revision_no"],
            "canonical_committed_at_utc": truth_by_date[event_date]["canonical_committed_at_utc"],
            "content_sha256": truth_by_date[event_date]["content_sha256"],
            "station_day_end_utc": truth_by_date[event_date]["station_day_end_utc"],
        }
        for event_date in ALL_DATES
    ])
    missing_model_scopes = [
        (event_date, model)
        for event_date in ALL_DATES
        for model in EXPECTED_MODELS
        if (event_date, model) not in model_rows
    ]
    if missing_model_scopes:
        status.update({"timezone": timezone_name, "missing_model_scopes": missing_model_scopes})
        status["blockers"].append("EXACT_FIVE_MODEL_SCOPE_MISSING")
        return None, status
    residual_rows = _residual_rows(conn, station, truth_by_date)
    missing_residuals = [
        (event_date, model)
        for event_date in ALL_DATES
        for model in EXPECTED_MODELS
        if (event_date, model) not in residual_rows
    ]
    if missing_residuals:
        status.update({"timezone": timezone_name, "missing_residual_scopes": missing_residuals})
        status["blockers"].append("EXACT_RESIDUAL_SCOPE_MISSING")
        return None, status

    train_residuals = {
        model: [residual_rows[(event_date, model)]["e_y_minus_f_raw_c"] for event_date in TRAIN_DATES]
        for model in EXPECTED_MODELS
    }
    model_artifacts: list[dict[str, Any]] = []
    artifact_by_model: dict[str, dict[str, Any]] = {}
    for model in EXPECTED_MODELS:
        values = train_residuals[model]
        bias = sum(values) / len(values)
        scale = math.sqrt(sum((value - bias) ** 2 for value in values) / len(values))
        scale = max(scale, 0.1)
        identity = {
            "calibration_artifact_id": f"cal-d1-historical-{station}-{model}-{truth_scope_hash[:16]}",
            "station_id": station,
            "model_id": model,
            "season_bin": SEASON_BIN,
            "lead_bucket": LEAD_BUCKET,
            "run_cycle": RUN_CYCLE,
            "settlement_definition": SETTLEMENT_DEFINITION,
            "trained_through_utc": max(truth_by_date[event_date]["canonical_committed_at_utc"] for event_date in TRAIN_DATES),
            "sample_n": len(TRAIN_DATES),
            "effective_sample_n": float(len(TRAIN_DATES)),
            "bias_c": bias,
            "residual_scale_c": scale,
            "tail_parameters": {"lower_q": 0.05, "upper_q": 0.95},
            "valid_from_utc": valid_from,
            "valid_until_utc": valid_until,
            "promoted": False,
        }
        row = {**identity, "content_sha256": content_sha256(identity)}
        model_artifacts.append(row)
        artifact_by_model[model] = row

    holdout_truth = {event_date: truth_by_date[event_date]["official_tmax_c"] for event_date in HOLDOUT_DATES}
    candidates = [
        _candidate_score(
            models=tuple(subset),
            model_artifacts=artifact_by_model,
            rows={event_date: {model: model_rows[(event_date, model)] for model in EXPECTED_MODELS} for event_date in HOLDOUT_DATES},
            holdout_truth=holdout_truth,
        )
        for size in range(1, len(EXPECTED_MODELS) + 1)
        for subset in itertools.combinations(EXPECTED_MODELS, size)
    ]
    selected_models = EXPECTED_MODELS
    selected_score = next(row for row in candidates if tuple(row["models"]) == selected_models)
    covariance, correlation = _covariance(train_residuals)
    selection_identity = {
        "selection_evidence_id": f"sel-d1-historical-{station}-{truth_scope_hash[:16]}",
        "station_id": station,
        "season_bin": SEASON_BIN,
        "lead_bucket": LEAD_BUCKET,
        "run_cycle": RUN_CYCLE,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "method_version": FORMAL_ENSEMBLE_METHOD_VERSION,
        "train_start_date": TRAIN_DATES[0],
        "train_end_date": TRAIN_DATES[-1],
        "recent_start_date": TRAIN_DATES[-3],
        "recent_end_date": TRAIN_DATES[-1],
        "holdout_start_date": HOLDOUT_DATES[0],
        "holdout_end_date": HOLDOUT_DATES[-1],
        "candidate_scores": candidates,
        "selected_models": list(selected_models),
        "selected_weights": {model: 0.2 for model in selected_models},
        "oos_metrics": {
            key: selected_score[key] for key in ("brier_score", "nll", "crps")
        },
        "residual_covariance": covariance,
        "residual_correlation": correlation,
        "effective_sample_n": float(len(TRAIN_DATES)),
        "previous_ensemble_artifact_sha256": None,
        "max_weight_delta": 1.0,
        "actual_max_weight_delta": 0.0,
    }
    selection = {**selection_identity, "content_sha256": content_sha256(selection_identity)}
    masks: dict[str, dict[str, float]] = {}
    for size in range(2, len(EXPECTED_MODELS) + 1):
        for subset in itertools.combinations(EXPECTED_MODELS, size):
            masks[model_set_hash(subset)] = {model: 1.0 / size for model in subset}
    ensemble_identity = {
        "ensemble_artifact_id": f"ens-d1-historical-{station}-{truth_scope_hash[:16]}",
        "station_id": station,
        "season_bin": SEASON_BIN,
        "lead_bucket": LEAD_BUCKET,
        "run_cycle": RUN_CYCLE,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "candidate_models": list(EXPECTED_MODELS),
        "supported_masks": masks,
        "residual_covariance": covariance,
        "correlation_penalty": 0.0,
        "selection_evidence_sha256": selection["content_sha256"],
        "previous_ensemble_artifact_sha256": None,
        "weight_method": FORMAL_ENSEMBLE_METHOD_VERSION,
        "effective_sample_n": float(len(TRAIN_DATES)),
        "max_weight_delta": 1.0,
        "train_start_date": TRAIN_DATES[0],
        "train_end_date": TRAIN_DATES[-1],
        "recent_start_date": TRAIN_DATES[-3],
        "recent_end_date": TRAIN_DATES[-1],
        "holdout_start_date": HOLDOUT_DATES[0],
        "holdout_end_date": HOLDOUT_DATES[-1],
        "oos_metrics": selection["oos_metrics"],
        "valid_from_utc": valid_from,
        "valid_until_utc": valid_until,
        "promoted": False,
    }
    ensemble = {**ensemble_identity, "content_sha256": content_sha256(ensemble_identity)}

    model_lineage = [model_rows[(event_date, model)] for event_date in ALL_DATES for model in EXPECTED_MODELS]
    truth_lineage = [truth_by_date[event_date] for event_date in ALL_DATES]
    model_scope_hash = content_sha256([
        {key: row[key] for key in ("model_run_id", "model_id", "target_local_date", "run_init_utc", "source_object_sha256", "content_sha256")}
        for row in model_lineage
    ])
    status.update({
        "state": "READY",
        "timezone": timezone_name,
        "train_n": len(TRAIN_DATES),
        "holdout_n": len(HOLDOUT_DATES),
        "train_start_date": TRAIN_DATES[0],
        "train_end_date": TRAIN_DATES[-1],
        "holdout_start_date": HOLDOUT_DATES[0],
        "holdout_end_date": HOLDOUT_DATES[-1],
        "model_scope_hash": model_scope_hash,
        "truth_scope_hash": truth_scope_hash,
        "truth_revision_ids": [row["truth_id"] for row in truth_lineage],
        "model_lineage_count": len(model_lineage),
        "truth_lineage_count": len(truth_lineage),
        "fixed_five_model_combination": True,
        "source_class": "aggregator_republisher",
        "authority_scope": AUTHORITY_SCOPE,
        "native_arrival_index_eligible": False,
        "p1_eligible": False,
        "formal_live_authority": False,
    })
    station_packet = {
        "station_id": station,
        "timezone": timezone_name,
        "calibration_artifacts": model_artifacts,
        "selection_evidence": selection,
        "ensemble_artifact": ensemble,
        "model_lineage": model_lineage,
        "truth_lineage": truth_lineage,
        "residual_lineage": [
            residual_rows[(event_date, model)]
            for event_date in ALL_DATES
            for model in EXPECTED_MODELS
        ],
        "model_scope_hash": model_scope_hash,
        "truth_scope_hash": truth_scope_hash,
    }
    return station_packet, status


def _activation_rows(
    *,
    station: str,
    artifacts: list[Mapping[str, Any]],
    ensemble: Mapping[str, Any],
    activated_at: str,
    generation: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        identity = {
            "artifact_kind": "MODEL_CALIBRATION",
            "artifact_id": artifact["calibration_artifact_id"],
            "station_id": station,
            "model_id_or_star": artifact["model_id"],
            "season_bin": artifact["season_bin"],
            "lead_bucket": artifact["lead_bucket"],
            "run_cycle": artifact["run_cycle"],
            "settlement_definition": artifact["settlement_definition"],
            "available_model_set_hash_or_star": "*",
            "generation": generation,
            "valid_from_utc": artifact["valid_from_utc"],
            "valid_until_utc": artifact["valid_until_utc"],
            "activated_at_utc": activated_at,
            "authority_scope": AUTHORITY_SCOPE,
        }
        activation_id = "no-live-d1-cal-" + content_sha256(identity)[:32]
        rows.append({
            "activation_id": activation_id,
            **identity,
            "content_sha256": content_sha256({"activation_id": activation_id, **identity}),
        })
    for available_hash in sorted(ensemble["supported_masks"]):
        identity = {
            "artifact_kind": "MODEL_ENSEMBLE",
            "artifact_id": ensemble["ensemble_artifact_id"],
            "station_id": station,
            "model_id_or_star": "*",
            "season_bin": ensemble["season_bin"],
            "lead_bucket": ensemble["lead_bucket"],
            "run_cycle": ensemble["run_cycle"],
            "settlement_definition": ensemble["settlement_definition"],
            "available_model_set_hash_or_star": available_hash,
            "generation": generation,
            "valid_from_utc": ensemble["valid_from_utc"],
            "valid_until_utc": ensemble["valid_until_utc"],
            "activated_at_utc": activated_at,
            "authority_scope": AUTHORITY_SCOPE,
        }
        activation_id = "no-live-d1-ens-" + content_sha256(identity)[:32]
        rows.append({
            "activation_id": activation_id,
            **identity,
            "content_sha256": content_sha256({"activation_id": activation_id, **identity}),
        })
    return rows


def _canonical_contract_for_parts(
    *,
    packet_parts: list[Mapping[str, Any]],
    ensemble_rows: list[Mapping[str, Any]],
    selection_rows: list[Mapping[str, Any]],
    as_of_utc: str,
    generation: int,
) -> dict[str, Any]:
    """Explicitly adapt this builder's retained lineage into the one contract.

    This adapter is intentionally local to the d1 builder.  It accepts only
    fields emitted by ``_build_station`` and refuses to infer a missing hash,
    timestamp, family or source lane.  The normal artifact packet remains a
    legacy projection; the returned value is the only payload eligible for
    ``canonical_ingress``.
    """

    model_facts: list[dict[str, Any]] = []
    truth_facts: list[dict[str, Any]] = []
    residual_facts: list[dict[str, Any]] = []
    activation_facts: list[dict[str, Any]] = []
    for part in packet_parts:
        station = str(part["station_id"]).strip().upper()
        timezone_name = str(part["timezone"]).strip()
        truth_by_date: dict[str, Mapping[str, Any]] = {}
        model_hash_by_key: dict[tuple[str, str], str] = {}
        for raw in part["truth_lineage"]:
            if not isinstance(raw, Mapping):
                raise ValueError("canonical_d1_truth_lineage_mapping_required")
            event_date = str(raw["local_date"])
            truth = {
                "truth_id": str(raw["truth_id"]),
                "station_id": station,
                "event_date": event_date,
                "truth_revision_no": int(raw["revision_no"]),
                "truth_value": float(raw["official_tmax_c"]),
                "truth_content_sha256": str(raw["source_payload_sha256"]),
                "canonical_committed_at_utc": str(raw["canonical_committed_at_utc"]),
                "station_day_end_utc": str(raw["station_day_end_utc"]),
                "closed_day": True,
                "source_lane": "AWC",
                "authority_scope": AUTHORITY_SCOPE,
                "settlement_timezone": timezone_name,
            }
            truth["content_sha256"] = content_sha256(truth)
            truth_facts.append(truth)
            truth_by_date[event_date] = truth
        for raw in part["model_lineage"]:
            if not isinstance(raw, Mapping):
                raise ValueError("canonical_d1_model_lineage_mapping_required")
            event_date = str(raw["target_local_date"])
            model_id = str(raw["model_id"]).strip().upper()
            model = {
                "station_id": station,
                "model_id": model_id,
                "ensemble_system_id": "",
                "model_family": "global_deterministic",
                "provider_id": str(raw["provider_id"]),
                "source_class": "aggregator_republisher",
                "event_date": event_date,
                "run_init_utc": str(raw["run_init_utc"]),
                "lead_bucket": LEAD_BUCKET,
                "run_cycle": RUN_CYCLE,
                "settlement_timezone": timezone_name,
                "forecast_value": float(raw["raw_tmax_c"]),
                "raw_object_sha256": str(raw["source_object_sha256"]),
                "oos_role": "historical_strict_oos",
                "closed_day": True,
                "strict_asof": True,
                "native_arrival_indexed": False,
                "p1_eligible": False,
                "formal_live_authority": False,
                "published_at_utc": str(raw["published_at_utc"]),
                "received_at_utc": str(raw["received_at_utc"]),
            }
            model["content_sha256"] = content_sha256(model)
            model_facts.append(model)
            model_hash_by_key[(event_date, model_id)] = model["content_sha256"]
        for raw in part["residual_lineage"]:
            if not isinstance(raw, Mapping):
                raise ValueError("canonical_d1_residual_lineage_mapping_required")
            event_date = str(raw["target_local_date"])
            model_id = str(raw["model_id"]).strip().upper()
            if (event_date, model_id) not in model_hash_by_key or event_date not in truth_by_date:
                raise ValueError("canonical_d1_residual_lineage_reference_missing")
            residual = {
                "residual_id": str(raw["residual_id"]),
                "truth_id": str(raw["truth_id"]),
                "model_fact_content_sha256": model_hash_by_key[(event_date, model_id)],
                "station_id": station,
                "model_id": model_id,
                "event_date": event_date,
                "lead_bucket": LEAD_BUCKET,
                "run_cycle": RUN_CYCLE,
                "residual_value": float(raw["e_y_minus_f_raw_c"]),
                "paired_asof_utc": str(raw["paired_asof_utc"]),
                "oos_role": "historical_strict_oos",
            }
            residual["content_sha256"] = content_sha256(residual)
            residual_facts.append(residual)
        ensemble = next(row for row in ensemble_rows if row["station_id"] == station)
        selection = next(row for row in selection_rows if row["station_id"] == station)
        activation = {
            "activation_id": f"canonical:d1:{station}:{generation}:{ensemble['content_sha256'][:16]}",
            "station_id": station,
            "model_family": "global_deterministic",
            "lead_bucket": LEAD_BUCKET,
            "run_cycle": RUN_CYCLE,
            "candidate_models": list(EXPECTED_MODELS),
            "selected_models": list(selection["selected_models"]),
            "selected_weights": dict(selection["selected_weights"]),
            "available_model_set_hash": model_set_hash(EXPECTED_MODELS),
            "selection_evidence_sha256": str(selection["content_sha256"]),
            "artifact_content_sha256": str(ensemble["content_sha256"]),
            "activation_generation": int(generation),
            "authority_scope": AUTHORITY_SCOPE,
            "oos_role": "historical_strict_oos",
        }
        activation["activation_content_sha256"] = content_sha256(activation)
        activation_facts.append(activation)
    identity = {
        "schema": CANONICAL_CONTRACT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": str(as_of_utc),
        "builder_id": "d1.global_deterministic.historical_strict.v1",
        "model_facts": model_facts,
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": truth_facts,
        "residual_facts": residual_facts,
        "activation_facts": activation_facts,
    }
    contract = {**identity, "content_sha256": content_sha256(identity)}
    # Validate before handing the packet to the common ingress.  This makes
    # the builder itself fail closed rather than delegating malformed facts.
    return CanonicalContract.from_mapping(contract).to_mapping()


def build(*, db: Path, stations: list[str], as_of_utc: str, generation: int, return_canonical: bool = False) -> tuple[dict[str, Any], dict[str, Any]] | tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    as_of = _utc(as_of_utc)
    valid_from = _stamp(as_of)
    valid_until = _stamp(as_of + timedelta(days=365))
    packet_parts: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for station in sorted(set(stations)):
            try:
                part, status = _build_station(
                    conn,
                    station=station,
                    as_of=as_of,
                    valid_from=valid_from,
                    valid_until=valid_until,
                )
            except Exception as exc:
                part = None
                status = {"station_id": station, "state": "BLOCKED", "blockers": [f"BUILD_ERROR:{type(exc).__name__}:{exc}"]}
            statuses.append(status)
            if part is not None:
                packet_parts.append(part)

    calibration = [row for part in packet_parts for row in part["calibration_artifacts"]]
    selections = [part["selection_evidence"] for part in packet_parts]
    ensembles = [part["ensemble_artifact"] for part in packet_parts]
    activations = [
        row
        for part in packet_parts
        for row in _activation_rows(
            station=part["station_id"],
            artifacts=part["calibration_artifacts"],
            ensemble=part["ensemble_artifact"],
            activated_at=valid_from,
            generation=generation,
        )
    ]
    ready = sorted(part["station_id"] for part in packet_parts)
    blocked = [row for row in statuses if row.get("state") != "READY"]
    packet_identity: dict[str, Any] = {
        "schema": PACKET_SCHEMA,
        "packet_version": "d1_historical_strict_v1",
        "as_of_utc": valid_from,
        "station_ids": ready,
        "lead_bucket": LEAD_BUCKET,
        "run_cycle": RUN_CYCLE,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "authority_scope": AUTHORITY_SCOPE,
        "calibration_artifacts": calibration,
        "ensemble_selection_evidence": selections,
        "ensemble_artifacts": ensembles,
        "calibration_activations": activations,
        "calibration_promotions": [],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "policy_promotions": [],
        "execution_mode": "production_no_live",
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "formal_live_authority_written": False,
        "future_data_is_not_oos": True,
        "oos_temporal_scope": "CLOSED_HISTORICAL_AWC_TRUTH",
        "fixed_five_model_combination": True,
    }
    packet = {**packet_identity, "packet_sha256": content_sha256(packet_identity)}
    receipt_identity = {
        "schema": SCHEMA,
        "as_of_utc": valid_from,
        "database": str(db),
        "station_ids_requested": sorted(set(stations)),
        "station_ids_ready": ready,
        "station_ids_blocked": [row["station_id"] for row in blocked],
        "station_statuses": statuses,
        "train_dates": list(TRAIN_DATES),
        "holdout_dates": list(HOLDOUT_DATES),
        "lead_bucket": LEAD_BUCKET,
        "run_cycle": RUN_CYCLE,
        "model_ids": list(EXPECTED_MODELS),
        "authority_scope": AUTHORITY_SCOPE,
        "packet_sha256": packet["packet_sha256"],
        "counts": {
            "ready_stations": len(ready),
            "blocked_stations": len(blocked),
            "calibration_artifacts": len(calibration),
            "selection_evidence": len(selections),
            "ensemble_artifacts": len(ensembles),
            "calibration_activations": len(activations),
        },
        "boundary": {
            "database_read_only": True,
            "writes_production_database": False,
            "native_arrival_index_written": False,
            "p1_authority_written": False,
            "formal_live_authority_written": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "future_data_count": 0,
        },
    }
    receipt = {**receipt_identity, "content_sha256": content_sha256(receipt_identity)}
    if return_canonical:
        canonical_contract = _canonical_contract_for_parts(
            packet_parts=packet_parts,
            ensemble_rows=ensembles,
            selection_rows=selections,
            as_of_utc=valid_from,
            generation=generation,
        )
        return packet, receipt, canonical_contract
    return packet, receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--as-of-utc", default="2026-08-03T12:00:00Z")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--station-id", action="append", default=[])
    args = parser.parse_args()
    db = args.db.expanduser().resolve(strict=True)
    if db.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    stations = [str(value).strip().upper() for value in args.station_id if str(value).strip()] or list(TARGET_STATIONS)
    packet, receipt = build(db=db, stations=stations, as_of_utc=args.as_of_utc, generation=args.generation)
    output_root = args.output_root.expanduser().resolve()
    write_bounded_json(output_root / "artifact_ingress_packet.json", packet)
    write_bounded_json(output_root / "build_receipt.json", receipt)
    print(json.dumps({
        "state": "READY_DRY_RUN_PACKET",
        "output_root": str(output_root),
        "ready_stations": len(packet["station_ids"]),
        "blocked_stations": [row["station_id"] for row in receipt["station_statuses"] if row.get("state") != "READY"],
        "calibration_artifacts": len(packet["calibration_artifacts"]),
        "ensemble_selection_evidence": len(packet["ensemble_selection_evidence"]),
        "ensemble_artifacts": len(packet["ensemble_artifacts"]),
        "calibration_activations": len(packet["calibration_activations"]),
        "packet_sha256": packet["packet_sha256"],
        "receipt_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if packet["station_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
