"""Fail-closed bridge while the official-NWP v3.1 successor is retrained.

The retired artifact was trained on Open-Meteo GFS/JMA payload semantics.  It
must never reinterpret direct ECMWF/ICON/GFS/JMA snapshots merely because a
field happens to have a similar name.  A separately approved artifact is
required before official source generations can influence the posterior.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ncei_zuuu_station_feature_runtime import (
    number,
    parse_utc,
    settlement_bucket_map,
    station_feature_row,
)
from scripts import join_ncei_zuuu_multimodel_synoptic_features as joiner
from scripts import join_ncei_zuuu_synoptic_features as single_joiner
from scripts import train_ncei_zuuu_multimodel_allday_bayes_v31 as trainer
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


SCHEMA_VERSION = "ncei_zuuu_multimodel_allday_v31_runtime_v1"
OFFICIAL_NWP_CATALOG_ID = "official_nwp_zuuu_v1"
DEFAULT_ARTIFACT = (
    Path(__file__).resolve().parent
    / "data"
    / "research"
    / "ncei_zuuu_joint_model"
    / "2021_2025"
    / "model_v31_multimodel_allday"
    / "model_artifact.json"
)


def _enabled() -> bool:
    return str(os.environ.get("PM_NCEI_ZUUU_V31_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _model_rows(bundle: Mapping[str, Any], as_of_dt: Any) -> tuple[dict[str, dict[str, Any]], list[str], list[str], str]:
    models = bundle.get("models") if isinstance(bundle.get("models"), Mapping) else {}
    required = {"gfs": "gfs_seamless", "jma": "jma_gsm"}
    rows: dict[str, dict[str, Any]] = {}
    event_ids: list[str] = []
    observation_ids: list[str] = []
    for alias, expected_model in required.items():
        snapshot = models.get(alias) if isinstance(models.get(alias), Mapping) else {}
        if snapshot.get("state") != "available":
            return {}, [], [], f"{alias}_snapshot_missing"
        if str(snapshot.get("model") or "") != expected_model:
            return {}, [], [], f"{alias}_historical_live_model_mismatch"
        valid = parse_utc(snapshot.get("valid_time_utc"))
        if valid is None:
            return {}, [], [], f"{alias}_valid_time_missing"
        age_hours = (as_of_dt - valid).total_seconds() / 3600.0
        if age_hours < -1e-6 or age_hours > 7.0:
            return {}, [], [], f"{alias}_snapshot_stale_or_future"
        features = snapshot.get("normalized_features")
        if not isinstance(features, Mapping):
            return {}, [], [], f"{alias}_normalized_features_missing"
        row = {key: value for key, value in features.items() if key != "valid_time_utc"}
        row.update(single_joiner._derive(row))
        row["synoptic_lag_hours"] = age_hours
        rows[alias] = row
        source_event_id = str(snapshot.get("source_event_id") or "").strip()
        source_observation_id = str(snapshot.get("source_observation_id") or "").strip()
        if source_event_id:
            event_ids.append(source_event_id)
        if source_observation_id:
            observation_ids.append(source_observation_id)
    return rows, event_ids, observation_ids, ""


def build_v31_multimodel_posterior(
    *,
    buckets: Sequence[Any],
    observation_inputs: Mapping[str, Any],
    as_of: Any,
    event_date: str,
    rounding_rule: str = "rounded_degrees",
    artifact_path: Path | str | None = None,
) -> dict[str, Any]:
    labels = [str(item.get("bucket") if isinstance(item, Mapping) else item).strip() for item in buckets or []]
    labels = [label for label in labels if label]
    base = {
        "schema": SCHEMA_VERSION,
        "state": "unavailable",
        "activation_mode": "live_primary",
        "model_version": "v3.1_multimodel_allday",
        "labels": labels,
        "rounding_rule": rounding_rule,
    }
    if not _enabled():
        return {**base, "state_reason": "disabled_by_env"}
    path = Path(artifact_path or os.environ.get("PM_NCEI_ZUUU_V31_ARTIFACT", "") or DEFAULT_ARTIFACT)
    if not path.exists():
        return {**base, "state_reason": "artifact_missing", "artifact_path": str(path)}
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if artifact.get("production_ready") is not True or artifact.get("schema") != trainer.MODEL_SCHEMA:
        return {**base, "state_reason": "artifact_release_contract_failed", "artifact_path": str(path)}
    bundle = observation_inputs.get("official_nwp_generation")
    if not isinstance(bundle, Mapping) or bundle.get("state") != "available":
        return {**base, "state_reason": "official_nwp_generation_missing", "artifact_path": str(path)}
    if (
        bundle.get("schema") != "official_nwp_multimodel_live_bundle_v1"
        or bundle.get("catalog_id") != OFFICIAL_NWP_CATALOG_ID
        or bundle.get("open_meteo") is not False
        or bundle.get("aggregator_or_republisher") is not False
    ):
        return {**base, "state_reason": "official_nwp_generation_contract_failed", "artifact_path": str(path)}
    # The old model coefficients encode the former Open-Meteo GFS/JMA
    # feature distribution.  A source switch is a model-version switch, not a
    # transport-only change.  Until the new artifact is trained and approved,
    # fail closed rather than emitting confidently miscalibrated maxima.
    source_contract = artifact.get("official_nwp_source_contract")
    if not isinstance(source_contract, Mapping) or source_contract.get("catalog_id") != OFFICIAL_NWP_CATALOG_ID:
        return {**base, "state_reason": "official_nwp_artifact_not_approved", "artifact_path": str(path)}
    as_of_dt = parse_utc(as_of)
    if as_of_dt is None:
        return {**base, "state_reason": "as_of_missing", "artifact_path": str(path)}
    completed = parse_utc(bundle.get("response_completed_at"))
    if completed is not None and (completed - as_of_dt).total_seconds() > 60.0:
        return {**base, "state_reason": "multimodel_future_fetch_time", "artifact_path": str(path)}
    model_rows, source_event_ids, source_observation_ids, model_error = _model_rows(bundle, as_of_dt)
    if model_error:
        return {**base, "state_reason": model_error, "artifact_path": str(path)}
    points = observation_inputs.get("_source_observation_temperature_points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes, bytearray)):
        return {**base, "state_reason": "amsc_temperature_path_missing", "artifact_path": str(path)}
    row = station_feature_row([point for point in points if isinstance(point, Mapping)], event_date)
    if not row:
        return {**base, "state_reason": "current_station_feature_row_missing", "artifact_path": str(path)}
    local_hour = float(row.get("local_hour") or 0.0)
    regime = "gfs_jma_multimodel" if local_hour < 6.0 else "gfs_only"
    submodels = artifact.get("submodels") if isinstance(artifact.get("submodels"), Mapping) else {}
    submodel = submodels.get(regime) if isinstance(submodels.get(regime), Mapping) else {}
    if not submodel:
        return {**base, "state_reason": "regime_submodel_missing", "artifact_path": str(path), "regime": regime}
    for alias, values in model_rows.items():
        for key, value in values.items():
            row[f"{alias}__{key}"] = value
    row.update(joiner._consensus(row, ["gfs", "jma"]))
    event_day = date.fromisoformat(event_date)
    climo_map = artifact.get("climo_by_day_of_year_c") if isinstance(artifact.get("climo_by_day_of_year_c"), Mapping) else {}
    climo = number(climo_map.get(str(event_day.timetuple().tm_yday)))
    if climo is None:
        return {**base, "state_reason": "artifact_climo_missing", "artifact_path": str(path)}
    numeric = list(submodel.get("numeric_features") or [])
    boolean = list(submodel.get("boolean_features") or [])
    design = trainer.AllDayDesign(numeric, boolean)
    design.means = np.asarray(submodel.get("means") or [], dtype=float)
    design.scales = np.asarray(submodel.get("scales") or [], dtype=float)
    x = design.transform([row], [climo])
    feature_names = list(submodel.get("feature_names") or [])
    if x.shape != (1, len(feature_names)):
        return {**base, "state_reason": "artifact_feature_dimension_mismatch", "artifact_path": str(path)}
    zero_beta = np.asarray(submodel.get("peak_already_coefficients") or [], dtype=float)
    positive_beta = np.asarray(submodel.get("positive_remaining_coefficients") or [], dtype=float)
    if len(zero_beta) != x.shape[1] or len(positive_beta) != x.shape[1]:
        return {**base, "state_reason": "artifact_coefficient_dimension_mismatch", "artifact_path": str(path)}
    p_zero = float(v1._predict_zero(x, zero_beta)[0])
    positive_mu = float((x @ positive_beta)[0])
    sigma = float(submodel["positive_log1p_residual_sigma"])
    observed_max = float(row["observed_max_to_time_c"])
    integer_probs = v1._bucket_probs(observed_max, p_zero, positive_mu, sigma)
    bucket_map = settlement_bucket_map(labels, integer_probs)
    expected_remaining = (1.0 - p_zero) * min(15.0, v1._expected_positive(positive_mu, sigma))
    numeric_width = len(numeric)
    standardized = x[0, 1 : 1 + numeric_width]
    return {
        **base,
        "state": "available",
        "state_reason": "same_distribution_multimodel_allday_v31_ready",
        "artifact_path": str(path),
        "artifact_schema": artifact.get("schema"),
        "trained_through_year": artifact.get("trained_through_year"),
        "trained_local_hour_min": artifact.get("trained_local_hour_min"),
        "trained_local_hour_max": artifact.get("trained_local_hour_max"),
        "regime": regime,
        "source_event_id": str(bundle.get("source_event_id") or ""),
        "source_observation_id": str(bundle.get("source_observation_id") or ""),
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
        "source_id": "gfs_jma_sichuan_synoptic_open_meteo",
        "hard_eligible": False,
        "cannot_override_amsc": True,
        "current_temperature_c": row.get("temperature_c"),
        "observed_today_max_c": observed_max,
        "peak_already_probability": p_zero,
        "continue_warming_probability": 1.0 - p_zero,
        "positive_remaining_log_mu": positive_mu,
        "positive_remaining_log_sigma": sigma,
        "expected_remaining_warming_c": expected_remaining,
        "expected_final_max_c": observed_max + expected_remaining,
        "bucket_probabilities": bucket_map,
        "integer_bucket_probabilities": {
            str(int(round(float(bucket)))): float(probability)
            for bucket, probability in zip(v1.BUCKETS, integer_probs)
        },
        "input_feature_summary": {
            "feature_count": len(feature_names),
            "numeric_feature_count": numeric_width,
            "missing_indicator_count": int(np.sum(x[0, 1 + numeric_width : 1 + 2 * numeric_width])),
            "max_absolute_standardized_numeric": float(np.max(np.abs(standardized))) if numeric_width else 0.0,
            "local_hour": local_hour,
            "climo_prior_mean_c": climo,
            "regime": regime,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "historical_backfill": False,
            "hard_observation_override": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "build_v31_multimodel_posterior"]
