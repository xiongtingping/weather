#!/usr/bin/env python3
"""Build a bounded RKPK five-model historical OOS research artifact.

The Previous Runs API exposes fixed 24/48-hour lead forecasts.  This script
pairs those date-addressed historical forecasts with already captured,
closed AWC truth.  It produces research/OOS evidence only: ECMWF and GFS are
aggregator copies here, so the result can never activate P0 or claim native
publication/arrival authority.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration_evidence import (  # noqa: E402
    ENSEMBLE_MAX_WEIGHT_DELTA,
    ENSEMBLE_RECENT_FRACTION,
    _effective_sample_n,
    _fit_probability_weights,
    _fixed_probability_rows,
    _metrics,
    _probability_metrics,
    _residual_matrices,
    _rolling_probability_rows,
)
from bayes_basket_target.contracts import (  # noqa: E402
    FORMAL_ENSEMBLE_METHOD_VERSION,
    MIN_FORMAL_ENSEMBLE_MODELS,
    MODEL_IDS,
    ContractError,
    ResidualObservation,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.calibration import (  # noqa: E402
    fit_model_calibration_artifact,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)


SCHEMA = "rkpk_open_meteo_five_model_historical_oos_v1"
ENDPOINT = "https://previous-runs-api.open-meteo.com/v1/forecast"
STATION_ID = "RKPK"
SETTLEMENT_TIMEZONE = "Asia/Seoul"
LATITUDE = 35.179
LONGITUDE = 128.938
MAX_DAYS = 120
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
# Formal station/run-cycle calibration needs enough history on both sides of
# the chronological split.  Four holdout days made one weather episode able
# to flip activation.  Keep at least twenty training days and eight untouched
# evaluation days; forward OOS remains observational and non-blocking.
MIN_TRAIN_N = 20
MIN_HOLDOUT_N = 8
# Below six weeks of exact station/run-cycle history, shrink weight movement
# toward the prior/uniform combination even when the minimum 20+8 split is
# available.
STANDARD_EVIDENCE_N = 42
LIMITED_SAMPLE_MAX_WEIGHT_DELTA = 0.20
MODELS: Mapping[str, Mapping[str, Any]] = {
    "ECMWF": {
        "api_model": "ecmwf_ifs",
        "provider_id": "ecmwf_ifs_open_meteo_previous_runs",
        "formal_lineage_eligible": False,
    },
    "GFS": {
        "api_model": "gfs_global",
        "provider_id": "gfs_global_open_meteo_previous_runs",
        "formal_lineage_eligible": False,
    },
    "ICON": {
        "api_model": "icon_global",
        "provider_id": "icon_global_open_meteo",
        "formal_lineage_eligible": False,
    },
    "GEM": {
        "api_model": "gem_global",
        "provider_id": "gem_global_open_meteo",
        "formal_lineage_eligible": False,
    },
    "CMA": {
        "api_model": "cma_grapes_global",
        "provider_id": "cma_grapes_global_open_meteo",
        "formal_lineage_eligible": False,
    },
}
NATIVE_REBUILD_SCHEMA = (
    "bayes_basket_target_historical_native_rebuild_evidence_v1"
)
NATIVE_PROVIDER_BY_MODEL = {
    "ECMWF": "ecmwf_ifs025_official",
    "GFS": "gfs_0p25_official",
}


def _meteorological_season(*, target_date: date, latitude: float) -> str:
    northern = {
        12: "winter", 1: "winter", 2: "winter",
        3: "spring", 4: "spring", 5: "spring",
        6: "summer", 7: "summer", 8: "summer",
        9: "autumn", 10: "autumn", 11: "autumn",
    }[target_date.month]
    if latitude >= 0:
        return northern
    return {
        "winter": "summer",
        "spring": "autumn",
        "summer": "winter",
        "autumn": "spring",
    }[northern]


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _event_window(
    event_date: str,
    settlement_timezone: str = SETTLEMENT_TIMEZONE,
) -> tuple[datetime, datetime]:
    day = date.fromisoformat(event_date)
    zone = ZoneInfo(settlement_timezone)
    start = datetime.combine(day, wall_time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    end = datetime.combine(
        day + timedelta(days=1), wall_time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return start, end


def _bounded_json_response(
    session: requests.Session,
    *,
    params: Mapping[str, Any],
    timeout_seconds: float,
    max_response_bytes: int,
    request_attempts: int = 3,
) -> tuple[dict[str, Any], bytes, str]:
    if not 1 <= request_attempts <= 3:
        raise ContractError("previous_runs_request_attempts_invalid")
    for attempt in range(request_attempts):
        try:
            with session.get(
                ENDPOINT,
                params=params,
                timeout=timeout_seconds,
                stream=True,
            ) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > max_response_bytes:
                    raise ContractError(
                        "previous_runs_content_length_exceeded"
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_response_bytes:
                        raise ContractError(
                            "previous_runs_response_bytes_exceeded"
                        )
                    chunks.append(chunk)
                    assert_process_memory_budget(projected_bytes=total * 3)
                raw = b"".join(chunks)
                try:
                    payload = json.loads(raw)
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    raise ContractError(
                        "previous_runs_response_json_invalid"
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise ContractError(
                        "previous_runs_response_shape_invalid"
                    )
                if payload.get("error"):
                    raise ContractError(
                        "previous_runs_api_error:"
                        + str(payload.get("reason") or "unknown")[:160]
                    )
                return dict(payload), raw, str(response.url)
        except requests.RequestException:
            if attempt + 1 >= request_attempts:
                raise
            time.sleep(min(8.0, float(2 ** attempt)))
    raise AssertionError("unreachable")


def _truth_by_date(
    payload: Mapping[str, Any],
    *,
    start_date: str,
    end_date: str,
    station_id: str = STATION_ID,
    settlement_timezone: str = SETTLEMENT_TIMEZONE,
    truth_source_lane: str = "AWC",
) -> dict[str, dict[str, Any]]:
    if payload.get("schema") not in {
        "bayes_basket_awc_taf_calibration_dataset_v1",
        "bayes_basket_station_calibration_dataset_v1",
    }:
        raise ContractError("awc_truth_artifact_schema_invalid")
    if (
        payload.get("station_id") != station_id
        or payload.get("settlement_timezone") != settlement_timezone
    ):
        raise ContractError("awc_truth_artifact_scope_invalid")
    expected_lane = truth_source_lane.strip().upper()
    if expected_lane not in {"AMSC", "AWC"}:
        raise ContractError("truth_source_lane_invalid")
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    rows: dict[str, dict[str, Any]] = {}
    for sample in payload.get("samples") or []:
        if not isinstance(sample, Mapping):
            raise ContractError("awc_truth_sample_invalid")
        target = str(sample.get("event_date") or "")
        if not target or not first <= date.fromisoformat(target) <= last:
            continue
        truth = sample.get("truth")
        window = sample.get("settlement_window")
        if (
            not isinstance(truth, Mapping)
            or str(truth.get("source_lane") or "").upper() != expected_lane
            or not isinstance(window, Mapping)
        ):
            raise ContractError("awc_truth_sample_lineage_invalid")
        observations = truth.get("observations")
        if not isinstance(observations, list) or not observations:
            raise ContractError("awc_truth_observations_missing")
        tmax = float(truth.get("tmax_c"))
        if not math.isfinite(tmax):
            raise ContractError("awc_truth_tmax_invalid")
        row = {
            "event_date": target,
            "official_tmax_c": tmax,
            "truth_source_lane": expected_lane,
            "truth_source_artifact_sha256": str(
                payload.get("content_sha256") or ""
            ),
            "truth_sample_id": str(sample.get("sample_id") or ""),
            "truth_rows_sha256": content_sha256(observations),
            "settlement_window": {
                "start_utc": str(window.get("start_utc") or ""),
                "end_utc": str(window.get("end_utc") or ""),
            },
        }
        expected_start, expected_end = _event_window(
            target, settlement_timezone
        )
        if row["settlement_window"] != {
            "start_utc": _utc_text(expected_start),
            "end_utc": _utc_text(expected_end),
        }:
            raise ContractError("awc_truth_settlement_window_mismatch")
        if target in rows and rows[target] != row:
            raise ContractError("awc_truth_date_conflict")
        rows[target] = row
    if not rows:
        raise ContractError("awc_truth_range_empty")
    return rows


def _hourly_series(
    payload: Mapping[str, Any],
    *,
    variable: str,
) -> list[dict[str, Any]]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ContractError("previous_runs_hourly_missing")
    times = hourly.get("time")
    values = hourly.get(variable)
    if (
        not isinstance(times, list)
        or not isinstance(values, list)
        or len(times) != len(values)
        or not times
    ):
        raise ContractError("previous_runs_hourly_shape_invalid")
    rows: list[dict[str, Any]] = []
    for raw_time, raw_value in zip(times, values):
        if raw_value is None:
            continue
        value = float(raw_value)
        if not math.isfinite(value):
            raise ContractError("previous_runs_temperature_invalid")
        parsed = datetime.fromisoformat(str(raw_time)).replace(
            tzinfo=timezone.utc
        )
        rows.append({
            "valid_time_utc": _utc_text(parsed),
            "temperature_2m_c": value,
        })
    if not rows:
        raise ContractError("previous_runs_temperature_series_empty")
    return rows


def _daily_rows(
    hourly_rows: Sequence[Mapping[str, Any]],
    *,
    truth_rows: Mapping[str, Mapping[str, Any]],
    settlement_timezone: str = SETTLEMENT_TIMEZONE,
) -> list[dict[str, Any]]:
    parsed = [
        (
            datetime.fromisoformat(
                str(row["valid_time_utc"]).replace("Z", "+00:00")
            ),
            float(row["temperature_2m_c"]),
        )
        for row in hourly_rows
    ]
    output: list[dict[str, Any]] = []
    for target, truth in sorted(truth_rows.items()):
        start, end = _event_window(target, settlement_timezone)
        values = [value for stamp, value in parsed if start <= stamp < end]
        # Previous Runs is hourly.  A complete KST settlement day must contain
        # all 24 valid hours; partial curves never enter OOS.
        if len(values) != 24:
            continue
        output.append({
            "event_date": target,
            "forecast_tmax_c": max(values),
            "official_tmax_c": float(truth["official_tmax_c"]),
            "residual_c": float(truth["official_tmax_c"]) - max(values),
            "valid_hour_count": 24,
            "settlement_window": dict(truth["settlement_window"]),
            "truth_sample_id": truth["truth_sample_id"],
            "truth_rows_sha256": truth["truth_rows_sha256"],
        })
    return output


def _native_daily_rows(
    paths: Sequence[Path],
    *,
    model_id: str,
    station_id: str,
    settlement_timezone: str,
    truth_rows: Mapping[str, Mapping[str, Any]],
    lead_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if model_id not in NATIVE_PROVIDER_BY_MODEL:
        raise ContractError("five_model_native_overlay_model_invalid")
    if lead_days != 2:
        raise ContractError("five_model_native_overlay_d2_only")
    expected_provider = NATIVE_PROVIDER_BY_MODEL[model_id]
    by_date: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        receipt_identity = {
            key: value
            for key, value in payload.items()
            if key != "receipt_sha256"
        }
        if (
            payload.get("schema") != NATIVE_REBUILD_SCHEMA
            or payload.get("provider_id") != expected_provider
            or payload.get("run_cycle") != "18Z"
            or int(payload.get("run_day_offset") or 0) != lead_days
            or payload.get("checks", {}).get("official_provider_only")
            is not True
            or payload.get("checks", {}).get("no_open_meteo") is not True
            or payload.get("checks", {}).get("calls_order_gateway") is not False
            or payload.get("checks", {}).get("places_orders") is not False
            or payload.get("blockers")
            or str(payload.get("receipt_sha256") or "")
            != content_sha256(receipt_identity)
        ):
            raise ContractError(
                f"five_model_native_overlay_evidence_invalid:{model_id}"
            )
        matched = 0
        for raw in payload.get("model_rows") or []:
            if (
                not isinstance(raw, Mapping)
                or str(raw.get("station_id") or "").upper() != station_id
            ):
                continue
            target = str(raw.get("event_date") or "")
            truth = truth_rows.get(target)
            if truth is None:
                continue
            if (
                raw.get("provider_id") != expected_provider
                or raw.get("target_timezone") != settlement_timezone
                or raw.get("daily_curve_complete") is not True
                or int(raw.get("sample_count") or 0) < 4
                or not str(raw.get("model_run_lineage_sha256") or "")
                or not str(raw.get("receipt_sha256") or "")
            ):
                raise ContractError(
                    f"five_model_native_overlay_row_invalid:{model_id}:{target}"
                )
            expected_run = datetime.combine(
                date.fromisoformat(target) - timedelta(days=lead_days),
                wall_time(hour=18),
                tzinfo=timezone.utc,
            )
            if str(raw.get("model_run_initialization_utc") or "") != _utc_text(
                expected_run
            ):
                raise ContractError(
                    f"five_model_native_overlay_run_mismatch:{model_id}:{target}"
                )
            forecast = float(raw.get("forecast_daily_max_c"))
            if not math.isfinite(forecast):
                raise ContractError(
                    f"five_model_native_overlay_temperature_invalid:{model_id}"
                )
            row = {
                "event_date": target,
                "forecast_tmax_c": forecast,
                "official_tmax_c": float(truth["official_tmax_c"]),
                "residual_c": float(truth["official_tmax_c"]) - forecast,
                "valid_hour_count": int(raw.get("sample_count") or 0),
                "settlement_window": dict(truth["settlement_window"]),
                "truth_sample_id": truth["truth_sample_id"],
                "truth_rows_sha256": truth["truth_rows_sha256"],
                "model_id": model_id,
                "provider_id": expected_provider,
                "source_class": "official_native_historical_rebuild",
                "availability_class": "historical_publication_only",
                "native_arrival_claimed": False,
                "model_run_initialization_utc": raw[
                    "model_run_initialization_utc"
                ],
                "model_run_lineage_sha256": raw[
                    "model_run_lineage_sha256"
                ],
                "curve_receipt_sha256": raw["receipt_sha256"],
                "evidence_receipt_sha256": payload["receipt_sha256"],
            }
            if target in by_date and by_date[target] != row:
                raise ContractError(
                    f"five_model_native_overlay_date_conflict:{model_id}:{target}"
                )
            by_date[target] = row
            matched += 1
        evidence.append({
            "model_id": model_id,
            "provider_id": expected_provider,
            "source_class": "official_native_historical_rebuild",
            "path": str(path),
            "receipt_sha256": payload["receipt_sha256"],
            "matched_station_day_count": matched,
            "native_arrival_claimed": False,
            "offline_oos_lineage_eligible": True,
        })
    return [by_date[target] for target in sorted(by_date)], evidence


def _exact_join_rows(
    rows_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    common_dates: Sequence[str],
) -> list[dict[str, Any]]:
    indexed = {
        (model, str(row["event_date"])): row
        for model in MODEL_IDS
        for row in rows_by_model[model]
    }
    joined: list[dict[str, Any]] = []
    for target in common_dates:
        model_rows = {
            model: indexed[(model, target)] for model in MODEL_IDS
        }
        truth_ids = {
            str(row["truth_sample_id"]) for row in model_rows.values()
        }
        truth_hashes = {
            str(row["truth_rows_sha256"]) for row in model_rows.values()
        }
        windows = {
            json.dumps(row["settlement_window"], sort_keys=True)
            for row in model_rows.values()
        }
        if len(truth_ids) != 1 or len(truth_hashes) != 1 or len(windows) != 1:
            raise ContractError(
                f"five_model_exact_join_truth_mismatch:{target}"
            )
        joined.append({
            "event_date": target,
            "truth_sample_id": next(iter(truth_ids)),
            "truth_rows_sha256": next(iter(truth_hashes)),
            "settlement_window": dict(
                next(iter(model_rows.values()))["settlement_window"]
            ),
            "models": {
                model: {
                    key: row.get(key)
                    for key in (
                        "forecast_tmax_c",
                        "residual_c",
                        "provider_id",
                        "source_class",
                        "availability_class",
                        "model_run_initialization_utc",
                        "model_run_lineage_sha256",
                        "curve_receipt_sha256",
                    )
                }
                for model, row in model_rows.items()
            },
        })
    return joined


def _artifact_mapping(value: Any) -> dict[str, Any]:
    return {
        "calibration_artifact_id": value.calibration_artifact_id,
        "station_id": value.station_id,
        "model_id": value.model_id,
        "season_bin": value.season_bin,
        "lead_bucket": value.lead_bucket,
        "run_cycle": value.run_cycle,
        "settlement_definition": value.settlement_definition,
        "trained_through_utc": value.trained_through_utc,
        "sample_n": value.sample_n,
        "effective_sample_n": value.effective_sample_n,
        "bias_c": value.bias_c,
        "residual_scale_c": value.residual_scale_c,
        "tail_parameters": dict(value.tail_parameters),
        "valid_from_utc": value.valid_from_utc,
        "valid_until_utc": value.valid_until_utc,
        "content_sha256": value.content_sha256,
        "promoted": value.promoted,
    }


def _build_oos(
    *,
    rows_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    lead_days: int,
    fetched_at_utc: str,
    station_id: str = STATION_ID,
    settlement_timezone: str = SETTLEMENT_TIMEZONE,
    season_bin: str = "summer",
    model_ids: Sequence[str] = MODEL_IDS,
    run_cycle: str | None = None,
) -> dict[str, Any]:
    models = tuple(str(model).strip().upper() for model in model_ids)
    if (
        len(models) < MIN_FORMAL_ENSEMBLE_MODELS
        or len(set(models)) != len(models)
        or set(models) - set(MODEL_IDS)
    ):
        raise ContractError("oos_model_scope_invalid")
    run_cycle_value = str(
        run_cycle or f"FIXED_{lead_days * 24}H"
    ).strip().upper()
    common_dates = sorted(
        set.intersection(
            *[
                {str(row["event_date"]) for row in rows_by_model[model]}
                for model in models
            ]
        )
    )
    minimum = MIN_TRAIN_N + MIN_HOLDOUT_N
    blockers: list[str] = []
    if len(common_dates) < minimum:
        blockers.append(
            f"common_closed_sample_n_below_{minimum}:{len(common_dates)}"
        )
    if blockers:
        return {
            "state": "BLOCKED_INSUFFICIENT_EVIDENCE",
            "common_dates": common_dates,
            "common_sample_n": len(common_dates),
            "minimum_train_n": MIN_TRAIN_N,
            "minimum_holdout_n": MIN_HOLDOUT_N,
            "blockers": blockers,
            "calibration_artifacts": [],
            "candidate_scores": [],
            "research_winner": None,
        }
    holdout_n = max(MIN_HOLDOUT_N, int(math.ceil(len(common_dates) * 0.20)))
    train_dates = common_dates[:-holdout_n]
    holdout_dates = common_dates[-holdout_n:]
    if len(train_dates) < MIN_TRAIN_N:
        raise ContractError("five_model_train_window_too_small")
    by_model_date = {
        (model, str(row["event_date"])): dict(row)
        for model in models
        for row in rows_by_model[model]
        if str(row["event_date"]) in set(common_dates)
    }
    forecasts = {
        key: float(row["forecast_tmax_c"])
        for key, row in by_model_date.items()
    }
    residuals = {
        key: float(row["residual_c"])
        for key, row in by_model_date.items()
    }
    truths = {
        target: float(by_model_date[(models[0], target)]["official_tmax_c"])
        for target in common_dates
    }
    for target in common_dates:
        if len({
            float(by_model_date[(model, target)]["official_tmax_c"])
            for model in models
        }) != 1:
            raise ContractError("five_model_truth_mismatch")
    trained_through = _event_window(
        train_dates[-1], settlement_timezone
    )[1]
    valid_from = datetime.fromisoformat(
        fetched_at_utc.replace("Z", "+00:00")
    )
    valid_until = valid_from + timedelta(days=365)
    artifacts = []
    model_oos = []
    for model in models:
        observations = [
            ResidualObservation(
                residual_id=(
                    "prev-runs-"
                    + content_sha256({
                        "model": model,
                        "lead_days": lead_days,
                        "event_date": target,
                    })[:32]
                ),
                station_id=station_id,
                model_id=model,
                season_bin=season_bin,
                lead_bucket=f"d{lead_days}",
                run_cycle=run_cycle_value,
                residual_c=residuals[(model, target)],
                target_local_date=target,
                paired_asof_utc=_utc_text(
                    _event_window(target, settlement_timezone)[1]
                ),
            )
            for target in train_dates
        ]
        artifact = fit_model_calibration_artifact(
            observations,
            artifact_id=(
                f"cal-research-{station_id}-{model}-d{lead_days}-"
                + content_sha256(train_dates)[:12]
            ),
            trained_through_utc=_utc_text(trained_through),
            valid_from_utc=_utc_text(valid_from),
            valid_until_utc=_utc_text(valid_until),
            parent_scale_c=1.5,
            kappa=8.0,
            bias_enabled=True,
            promoted=False,
        )
        artifacts.append(_artifact_mapping(artifact))
        raw_holdout = [residuals[(model, target)] for target in holdout_dates]
        model_oos.append({
            "model_id": model,
            "train_n": len(train_dates),
            "holdout_n": len(holdout_dates),
            "bias_c": artifact.bias_c,
            "raw": _metrics(raw_holdout),
            "calibrated": _metrics(raw_holdout, artifact.bias_c),
        })
    covariance, correlation = _residual_matrices(
        models=models,
        dates=train_dates,
        residuals_by_model_date=residuals,
    )
    recent_n = max(1, int(math.ceil(
        len(train_dates) * ENSEMBLE_RECENT_FRACTION
    )))
    recent_dates = set(train_dates[-recent_n:])
    evidence_tier = (
        "STANDARD"
        if len(common_dates) >= STANDARD_EVIDENCE_N
        else "LIMITED_SAMPLE_SHRUNK"
    )
    candidate_scores: list[dict[str, Any]] = []
    for size in range(1, len(models) + 1):
        for subset in itertools.combinations(models, size):
            rolling = _rolling_probability_rows(
                models=subset,
                dates=train_dates,
                forecasts_by_model_date=forecasts,
                residuals_by_model_date=residuals,
                truths_by_date=truths,
            )
            subset_correlation = {
                model: {
                    other: correlation[model][other] for other in subset
                }
                for model in subset
            }
            weights, metrics = _fit_probability_weights(
                models=subset,
                rows=rolling,
                recent_dates=recent_dates,
                residual_correlation=subset_correlation,
                effective_sample_n=_effective_sample_n(
                    [row[0] for row in rolling], recent_dates
                ),
                previous_weights=(
                    None
                    if evidence_tier == "STANDARD"
                    else {
                        model: 1.0 / len(subset) for model in subset
                    }
                ),
                max_weight_delta=(
                    1.0
                    if evidence_tier == "STANDARD"
                    else LIMITED_SAMPLE_MAX_WEIGHT_DELTA
                ),
            )
            candidate_scores.append({
                "models": list(subset),
                "candidate_role": (
                    "single_model_ablation"
                    if len(subset) == 1
                    else "research_ensemble"
                ),
                "research_selection_eligible": (
                    len(subset) >= MIN_FORMAL_ENSEMBLE_MODELS
                ),
                "formal_activation_eligible": False,
                "weights": weights,
                **metrics,
            })
    eligible = [
        row for row in candidate_scores
        if row["research_selection_eligible"]
    ]
    winner = min(
        eligible,
        key=lambda row: (
            float(row["objective"]),
            len(row["models"]),
            tuple(row["models"]),
        ),
    )
    selected = tuple(str(model) for model in winner["models"])
    holdout_rows = _fixed_probability_rows(
        models=selected,
        train_dates=train_dates,
        evaluation_dates=holdout_dates,
        forecasts_by_model_date=forecasts,
        residuals_by_model_date=residuals,
        truths_by_date=truths,
    )
    holdout_metrics = _probability_metrics(
        holdout_rows,
        winner["weights"],
        recent_dates=set(holdout_dates),
    )
    uniform_weights = {
        model: 1.0 / len(selected) for model in selected
    }
    uniform_holdout_metrics = _probability_metrics(
        holdout_rows,
        uniform_weights,
        recent_dates=set(holdout_dates),
    )
    probability_non_degradation = all(
        float(holdout_metrics[key])
        <= float(uniform_holdout_metrics[key]) + 1e-12
        for key in ("brier_score", "nll", "crps")
    )
    selection_identity = {
        "method_version": FORMAL_ENSEMBLE_METHOD_VERSION,
        "station_id": station_id,
        "season_bin": season_bin,
        "lead_bucket": f"d{lead_days}",
        "run_cycle": run_cycle_value,
        "train_dates": train_dates,
        "holdout_dates": holdout_dates,
        "candidate_model_pool": list(models),
        "candidate_count": len(candidate_scores),
        "selected_models": list(selected),
        "selected_model_set_hash": model_set_hash(selected),
        "selected_weights": dict(winner["weights"]),
        "holdout_oos": {
            key: holdout_metrics[key]
            for key in ("n", "brier_score", "nll", "crps")
        },
        "uniform_holdout_oos": {
            key: uniform_holdout_metrics[key]
            for key in ("n", "brier_score", "nll", "crps")
        },
        "probability_non_degradation_vs_uniform":
            probability_non_degradation,
    }
    explicit_run_scope = run_cycle is not None
    return {
        "state": (
            "OOS_COMPLETE_NO_LIVE_REVIEW"
            if explicit_run_scope
            else "OOS_RESEARCH_COMPLETE_ACTIVATION_BLOCKED"
        ),
        "evidence_tier": evidence_tier,
        "common_dates": common_dates,
        "common_sample_n": len(common_dates),
        "minimum_train_n": MIN_TRAIN_N,
        "minimum_holdout_n": MIN_HOLDOUT_N,
        "standard_evidence_n": STANDARD_EVIDENCE_N,
        "limited_sample_max_weight_delta":
            LIMITED_SAMPLE_MAX_WEIGHT_DELTA,
        "train_dates": train_dates,
        "holdout_dates": holdout_dates,
        "calibration_artifacts": artifacts,
        "model_oos": model_oos,
        "residual_covariance": covariance,
        "residual_correlation": correlation,
        "candidate_scores": candidate_scores,
        "research_winner": {
            **selection_identity,
            "selection_evidence_sha256": content_sha256(selection_identity),
        },
        "blockers": (
            ["formal_promotion_not_written"]
            if explicit_run_scope
            else [
                "aggregator_republisher_not_native_arrival_authority",
                "ecmwf_gfs_native_lineage_not_bound_to_research_comparison",
                "formal_activation_requires_separate_native_aligned_forward_oos",
            ]
        ),
    }


def build(
    *,
    truth_payload: Mapping[str, Any],
    start_date: str,
    end_date: str,
    lead_days: int,
    timeout_seconds: float,
    max_response_bytes: int,
    max_total_response_bytes: int,
    session: requests.Session,
    fetched_at_utc: str | None = None,
    station_id: str = STATION_ID,
    settlement_timezone: str = SETTLEMENT_TIMEZONE,
    latitude: float = LATITUDE,
    longitude: float = LONGITUDE,
    truth_source_lane: str = "AWC",
    season_bin: str | None = None,
    native_ecmwf_evidence: Sequence[Path] = (),
    native_gfs_evidence: Sequence[Path] = (),
) -> dict[str, Any]:
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    if first > last or (last - first).days + 1 > MAX_DAYS:
        raise ContractError("five_model_history_date_range_invalid")
    if lead_days not in {1, 2}:
        raise ContractError("five_model_history_lead_invalid")
    station_id = str(station_id or "").strip().upper()
    if not station_id:
        raise ContractError("five_model_station_id_invalid")
    try:
        ZoneInfo(settlement_timezone)
    except Exception as exc:
        raise ContractError("five_model_settlement_timezone_invalid") from exc
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90.0 <= latitude <= 90.0
        or not -180.0 <= longitude <= 180.0
    ):
        raise ContractError("five_model_station_coordinates_invalid")
    resolved_season = season_bin or _meteorological_season(
        target_date=first + (last - first) // 2,
        latitude=latitude,
    )
    if not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
        raise ContractError("five_model_history_response_budget_invalid")
    if not max_response_bytes <= max_total_response_bytes <= (
        MAX_TOTAL_RESPONSE_BYTES
    ):
        raise ContractError("five_model_history_total_budget_invalid")
    truth_rows = _truth_by_date(
        truth_payload,
        start_date=start_date,
        end_date=end_date,
        station_id=station_id,
        settlement_timezone=settlement_timezone,
        truth_source_lane=truth_source_lane,
    )
    fetched = fetched_at_utc or _utc_text(datetime.now(timezone.utc))
    variable = f"temperature_2m_previous_day{lead_days}"
    first_window_start, _ = _event_window(
        first.isoformat(), settlement_timezone
    )
    _, last_window_end = _event_window(
        last.isoformat(), settlement_timezone
    )
    query_start = first_window_start.date().isoformat()
    query_end = (last_window_end - timedelta(microseconds=1)).date().isoformat()
    total_bytes = 0
    captures: list[dict[str, Any]] = []
    rows_by_model: dict[str, list[dict[str, Any]]] = {}
    for model in MODEL_IDS:
        assert_process_memory_budget()
        contract = MODELS[model]
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": query_start,
            "end_date": query_end,
            "hourly": variable,
            "models": contract["api_model"],
            "timezone": "GMT",
        }
        payload, raw, source_url = _bounded_json_response(
            session,
            params=params,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        total_bytes += len(raw)
        if total_bytes > max_total_response_bytes:
            raise ContractError("five_model_history_total_bytes_exceeded")
        hourly = _hourly_series(payload, variable=variable)
        daily = _daily_rows(
            hourly,
            truth_rows=truth_rows,
            settlement_timezone=settlement_timezone,
        )
        for row in daily:
            row.update({
                "model_id": model,
                "provider_id": contract["provider_id"],
                "source_class": "aggregator_republisher",
                "availability_class": "retrospective_aggregator",
                "native_arrival_claimed": False,
            })
        rows_by_model[model] = daily
        captures.append({
            "model_id": model,
            "api_model": contract["api_model"],
            "provider_id": contract["provider_id"],
            "source_class": "aggregator_republisher",
            "previous_run_semantics": (
                f"value_predicted_{lead_days * 24}_hours_before_valid_time"
            ),
            "formal_lineage_eligible": False,
            "source_url": source_url,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "response_bytes": len(raw),
            "grid": {
                "latitude": payload.get("latitude"),
                "longitude": payload.get("longitude"),
                "elevation_m": payload.get("elevation"),
            },
            # Retain the smallest reconstructable object used to derive each
            # daily curve.  No unrelated weather fields are stored.
            "hourly_temperature_rows": hourly,
            "daily_rows": daily,
        })
    native_inputs = {
        "ECMWF": tuple(native_ecmwf_evidence),
        "GFS": tuple(native_gfs_evidence),
    }
    native_overlays: list[dict[str, Any]] = []
    for model, paths in native_inputs.items():
        if not paths:
            continue
        native_rows, evidence = _native_daily_rows(
            paths,
            model_id=model,
            station_id=station_id,
            settlement_timezone=settlement_timezone,
            truth_rows=truth_rows,
            lead_days=lead_days,
        )
        if not native_rows:
            raise ContractError(
                f"five_model_native_overlay_station_empty:{model}:{station_id}"
            )
        overlay_dates = {str(row["event_date"]) for row in native_rows}
        rows_by_model[model] = [
            row
            for row in rows_by_model[model]
            if str(row["event_date"]) not in overlay_dates
        ] + native_rows
        rows_by_model[model].sort(key=lambda row: str(row["event_date"]))
        native_overlays.extend(evidence)
    oos = _build_oos(
        rows_by_model=rows_by_model,
        lead_days=lead_days,
        fetched_at_utc=fetched,
        station_id=station_id,
        settlement_timezone=settlement_timezone,
        season_bin=resolved_season,
    )
    exact_join_rows = _exact_join_rows(
        rows_by_model,
        common_dates=oos.get("common_dates") or [],
    )
    exact_join_identity = {
        "station_id": station_id,
        "settlement_timezone": settlement_timezone,
        "model_ids": list(MODEL_IDS),
        "rows": exact_join_rows,
    }
    identity = {
        "schema": SCHEMA,
        "station_id": station_id,
        "settlement_timezone": settlement_timezone,
        "start_date": start_date,
        "end_date": end_date,
        "lead_days": lead_days,
        "season_bin": resolved_season,
        "fetched_at_utc": fetched,
        "source_contract": {
            "endpoint": ENDPOINT,
            "documentation": (
                "https://open-meteo.com/en/docs/previous-runs-api"
            ),
            "source_class": "mixed_native_overlay_and_aggregator_republisher",
            "fixed_lead_semantics": (
                f"predicted_{lead_days * 24}_hours_before_each_valid_time"
            ),
            "native_publication_or_arrival_claimed": False,
        },
        "truth_contract": {
            "source_lane": truth_source_lane.strip().upper(),
            "label_authority": str(
                truth_payload.get("label_authority")
                or "FORMAL_STATION_LANE_TRUTH"
            ),
            "closed_settlement_days_only": True,
            "truth_source_artifact_sha256": str(
                truth_payload.get("content_sha256") or ""
            ),
            "truth_date_count": len(truth_rows),
        },
        "captures": captures,
        "native_overlays": native_overlays,
        "exact_join": {
            **exact_join_identity,
            "row_count": len(exact_join_rows),
            "content_sha256": content_sha256(exact_join_identity),
        },
        "oos": oos,
        "resource_bounds": {
            "max_days": MAX_DAYS,
            "max_response_bytes": max_response_bytes,
            "max_total_response_bytes": max_total_response_bytes,
            "actual_total_response_bytes": total_bytes,
            "max_output_bytes": MAX_OUTPUT_BYTES,
            "minimum_free_bytes": MIN_FREE_BYTES,
        },
        "authority": {
            "research_oos_complete": oos["state"].startswith(
                "OOS_RESEARCH_COMPLETE"
            ),
            "formal_oos_selection_contract_valid": bool(
                oos.get("research_winner")
                and len(
                    oos["research_winner"].get("selected_models") or []
                )
                >= MIN_FORMAL_ENSEMBLE_MODELS
                and len(exact_join_rows)
                == int(oos.get("common_sample_n") or 0)
            ),
            "formal_calibration_activation_eligible": False,
            "p0_activation_eligible": False,
            "native_arrival_index_eligible": False,
            "hard_no_eligible": False,
        },
        "boundary": {
            "writes_production_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    raw = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ContractError("five_model_history_output_bytes_exceeded")
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free - len(raw) < MIN_FREE_BYTES:
        raise ContractError("five_model_history_minimum_free_disk_guard")
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth-artifact", type=Path, required=True)
    parser.add_argument("--station-id", default=STATION_ID)
    parser.add_argument(
        "--settlement-timezone", default=SETTLEMENT_TIMEZONE
    )
    parser.add_argument(
        "--truth-source-lane", choices=("AMSC", "AWC"), default="AWC"
    )
    parser.add_argument("--season-bin", default="")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--lead-days", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--proxy-url",
        default="",
        help="Explicit isolated research proxy; shared runtime config is unchanged.",
    )
    parser.add_argument(
        "--max-response-bytes", type=int, default=MAX_RESPONSE_BYTES
    )
    parser.add_argument(
        "--max-total-response-bytes",
        type=int,
        default=MAX_TOTAL_RESPONSE_BYTES,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--native-ecmwf-evidence", type=Path, action="append", default=[]
    )
    parser.add_argument(
        "--native-gfs-evidence", type=Path, action="append", default=[]
    )
    args = parser.parse_args()
    truth = json.loads(
        args.truth_artifact.expanduser().resolve().read_text()
    )
    coordinates = station_coordinates(args.station_id)
    session = requests.Session()
    if args.proxy_url:
        session.proxies.update({
            "http": args.proxy_url,
            "https": args.proxy_url,
        })
    result = build(
        truth_payload=truth,
        start_date=args.start_date,
        end_date=args.end_date,
        lead_days=args.lead_days,
        timeout_seconds=args.timeout_seconds,
        max_response_bytes=args.max_response_bytes,
        max_total_response_bytes=args.max_total_response_bytes,
        session=session,
        station_id=args.station_id,
        settlement_timezone=args.settlement_timezone,
        latitude=float(coordinates["latitude"]),
        longitude=float(coordinates["longitude"]),
        truth_source_lane=args.truth_source_lane,
        season_bin=args.season_bin or None,
        native_ecmwf_evidence=args.native_ecmwf_evidence,
        native_gfs_evidence=args.native_gfs_evidence,
    )
    output = args.output.expanduser().resolve()
    _atomic_write(output, result)
    print(json.dumps({
        "schema": result["schema"],
        "state": result["oos"]["state"],
        "common_sample_n": result["oos"]["common_sample_n"],
        "research_winner": result["oos"].get("research_winner"),
        "formal_calibration_activation_eligible": result["authority"][
            "formal_calibration_activation_eligible"
        ],
        "output": str(output),
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
