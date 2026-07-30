"""Strict-time OOS evidence for transporting TAF from crossfit to live P0.

The historical TAF artifact was fitted against a rolling-origin crossfit NWP
combination.  The live P0 uses a different probability-stack ensemble and
model-calibration artifacts.  This module evaluates that transport without
equating the two artifact hashes and without applying today's forecast to
historical labels.

It is offline, bounded, read-only, and never binds or activates an artifact.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping, Sequence

from .calibration import (
    TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA,
    build_taf_nwp_live_compatibility_fingerprint,
)
from .calibration_evidence import (
    ENSEMBLE_MAX_WEIGHT_DELTA,
    _fit_rolling_probability_ensemble,
)
from .contracts import (
    ContractError,
    FORMAL_ENSEMBLE_METHOD_VERSION,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    content_sha256,
)
from .resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
)
from .settlement_time import settlement_day_window
from .taf_revision import TafRevisionArtifact


SCHEMA = "bayes_basket_taf_nwp_transport_oos_report_v1"
EXACT_JOIN_SCHEMA = "bayes_basket_taf_increment_calibration_dataset_v2"
CROSSFIT_SCHEMA = "bayes_basket_nwp_crossfit_predictions_v1"
P0_INBOX_SCHEMA = "bayes_basket_target_p0_inbox_v1"
AWC_TAF_DATASET_SCHEMA = "bayes_basket_awc_taf_calibration_dataset_v1"
MIN_TRANSPORT_SAMPLE_N = 24
NESTED_MIN_TRAIN_DAYS = 3
MAX_ROWS = 366
MAX_REFERENCE_FILES = 64
MAX_TOTAL_INPUT_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_FILE_BYTES = 16 * 1024 * 1024
MIN_PROBABILITY = 1e-12


def _utc(name: str, value: Any) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(parsed):
        raise ContractError(f"{name}_invalid")
    return parsed


def _sha(name: str, value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractError(f"{name}_invalid")
    return text


def _bounded_size(path: Path, *, limit: int, name: str) -> int:
    resolved = path.expanduser().resolve()
    try:
        size = int(resolved.stat().st_size)
    except OSError as exc:
        raise ContractError(f"{name}_stat_failed") from exc
    if size <= 0 or size > limit:
        raise ContractError(f"{name}_size_invalid")
    return size


def _validate_content_hash(raw: Mapping[str, Any], *, name: str) -> str:
    supplied = _sha(f"{name}_content_sha256", raw.get("content_sha256"))
    identity = {
        str(key): value
        for key, value in raw.items()
        if key != "content_sha256"
    }
    if content_sha256(identity) != supplied:
        raise ContractError(f"{name}_content_sha_mismatch")
    return supplied


def _validate_crossfit(raw: Mapping[str, Any]) -> tuple[str, dict[tuple[str, str], Mapping[str, Any]]]:
    if raw.get("schema") != CROSSFIT_SCHEMA:
        raise ContractError("transport_crossfit_schema_invalid")
    predictions = raw.get("predictions")
    if not isinstance(predictions, list) or len(predictions) > MAX_ROWS:
        raise ContractError("transport_crossfit_rows_invalid")
    supplied = _sha(
        "transport_crossfit_artifact_sha256",
        raw.get("crossfit_prediction_artifact_sha256"),
    )
    stripped_predictions = []
    by_scope: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in predictions:
        if not isinstance(row, Mapping):
            raise ContractError("transport_crossfit_row_invalid")
        if (
            _sha(
                "transport_crossfit_row_artifact_sha256",
                row.get("crossfit_prediction_artifact_sha256"),
            )
            != supplied
        ):
            raise ContractError("transport_crossfit_row_artifact_mismatch")
        stripped_predictions.append({
            str(key): value
            for key, value in row.items()
            if key != "crossfit_prediction_artifact_sha256"
        })
        key = (
            str(row.get("station_id") or "").upper(),
            str(row.get("event_date") or ""),
        )
        if not all(key) or key in by_scope:
            raise ContractError("transport_crossfit_scope_duplicate")
        by_scope[key] = row
    identity = {
        str(key): value
        for key, value in raw.items()
        if key != "crossfit_prediction_artifact_sha256"
    }
    identity["predictions"] = stripped_predictions
    if content_sha256(identity) != supplied:
        raise ContractError("transport_crossfit_artifact_content_sha_mismatch")
    return supplied, by_scope


def _validate_exact_join(
    raw: Mapping[str, Any],
    *,
    crossfit_sha256: str,
) -> list[Mapping[str, Any]]:
    if (
        raw.get("schema") != EXACT_JOIN_SCHEMA
        or raw.get("source_class") != "strict_oos_crossfit_exact_join"
    ):
        raise ContractError("transport_exact_join_schema_invalid")
    _validate_content_hash(raw, name="transport_exact_join")
    if (
        _sha(
            "transport_exact_join_crossfit_sha256",
            raw.get("nwp_crossfit_prediction_artifact_sha256"),
        )
        != crossfit_sha256
    ):
        raise ContractError("transport_exact_join_crossfit_mismatch")
    rows = raw.get("samples")
    if (
        not isinstance(rows, list)
        or len(rows) > MAX_ROWS
        or int(raw.get("sample_count", -1)) != len(rows)
        or int(raw.get("blocker_count", -1)) != 0
        or raw.get("blockers") != []
    ):
        raise ContractError("transport_exact_join_rows_invalid")
    return rows


def _select_live_packet(
    inbox: Mapping[str, Any],
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
) -> tuple[
    Mapping[str, Any],
    ModelEnsembleArtifact,
    dict[str, ModelCalibrationArtifact],
    dict[str, Any],
]:
    if inbox.get("schema") != P0_INBOX_SCHEMA:
        raise ContractError("transport_live_p0_inbox_schema_invalid")
    _validate_content_hash(inbox, name="transport_live_p0_inbox")
    packets = inbox.get("packets")
    if not isinstance(packets, list) or not packets or len(packets) > 256:
        raise ContractError("transport_live_p0_packets_invalid")
    candidates = []
    for wrapper in packets:
        if not isinstance(wrapper, Mapping) or not isinstance(
            wrapper.get("packet"), Mapping
        ):
            raise ContractError("transport_live_p0_packet_invalid")
        packet = wrapper["packet"]
        if (
            str(wrapper.get("packet_content_sha256") or "")
            != content_sha256(packet)
        ):
            raise ContractError("transport_live_p0_packet_content_sha_mismatch")
        ensemble_raw = packet.get("ensemble_artifact")
        calibrations_raw = packet.get("calibration_artifacts")
        if not isinstance(ensemble_raw, Mapping) or not isinstance(
            calibrations_raw, Mapping
        ):
            raise ContractError("transport_live_artifact_lineage_missing")
        ensemble = ModelEnsembleArtifact.from_mapping(ensemble_raw)
        if (
            str(packet.get("station_id") or "").upper(),
            ensemble.season_bin,
            ensemble.lead_bucket,
            ensemble.run_cycle,
        ) != (
            station_id.upper(),
            season_bin,
            lead_bucket,
            run_cycle,
        ):
            continue
        calibrations = {
            str(model).upper(): ModelCalibrationArtifact.from_mapping(raw)
            for model, raw in calibrations_raw.items()
            if isinstance(raw, Mapping)
        }
        runs = packet.get("current_model_runs")
        if not isinstance(runs, list) or len(runs) < 2:
            raise ContractError("transport_live_model_runs_invalid")
        models = tuple(sorted(
            str(run.get("model_id") or "").upper()
            for run in runs
            if isinstance(run, Mapping)
        ))
        if len(models) != len(runs) or len(set(models)) != len(models):
            raise ContractError("transport_live_model_set_invalid")
        fingerprint = build_taf_nwp_live_compatibility_fingerprint(
            ensemble_artifact=ensemble,
            calibration_artifacts=calibrations,
            available_models=models,
        )
        candidates.append((packet, ensemble, calibrations, fingerprint))
    if len(candidates) != 1:
        raise ContractError("transport_live_exact_scope_not_unique")
    return candidates[0]


def _resolve_reference(
    raw_ref: str,
    *,
    allowed_root: Path,
) -> tuple[Path, str, str, str]:
    path_text, separator, fragment = str(raw_ref or "").partition("#")
    parts = fragment.split("/")
    if separator != "#" or len(parts) != 3 or any(not part for part in parts):
        raise ContractError("transport_raw_object_ref_invalid")
    path = Path(path_text).expanduser().resolve()
    root = allowed_root.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContractError("transport_raw_object_ref_outside_allowed_root") from exc
    return path, parts[0].upper(), parts[1], parts[2].upper()


def _historical_forecasts(
    prediction: Mapping[str, Any],
    *,
    models: Sequence[str],
    allowed_root: Path,
    cache: dict[Path, Mapping[str, Any]],
    total_bytes: list[int],
) -> dict[str, float]:
    rows = prediction.get("model_rows")
    if not isinstance(rows, list) or len(rows) != len(models):
        raise ContractError("transport_historical_model_lineage_incomplete")
    result: dict[str, float] = {}
    for lineage in rows:
        if not isinstance(lineage, Mapping):
            raise ContractError("transport_historical_model_lineage_invalid")
        model = str(lineage.get("model_id") or "").upper()
        if model not in models or model in result:
            raise ContractError("transport_historical_model_scope_mismatch")
        path, station, event_date, ref_model = _resolve_reference(
            str(lineage.get("raw_object_ref") or ""),
            allowed_root=allowed_root,
        )
        if (
            station != str(prediction.get("station_id") or "").upper()
            or event_date != str(prediction.get("event_date") or "")
            or ref_model != model
        ):
            raise ContractError("transport_raw_object_ref_scope_mismatch")
        if path not in cache:
            if len(cache) >= MAX_REFERENCE_FILES:
                raise ContractError("transport_reference_file_count_exceeded")
            size = _bounded_size(
                path,
                limit=MAX_REFERENCE_FILE_BYTES,
                name="transport_reference_file",
            )
            total_bytes[0] += size
            if total_bytes[0] > MAX_TOTAL_INPUT_BYTES:
                raise ContractError("transport_total_input_bytes_exceeded")
            loaded = load_bounded_json(path, limit=MAX_REFERENCE_FILE_BYTES)
            if not isinstance(loaded, Mapping):
                raise ContractError("transport_reference_mapping_required")
            cache[path] = loaded
        evidence = cache[path]
        evidence_rows = evidence.get("model_rows")
        if not isinstance(evidence_rows, list):
            raise ContractError("transport_reference_model_rows_missing")
        matches = [
            item for item in evidence_rows
            if isinstance(item, Mapping)
            and str(item.get("station_id") or "").upper() == station
            and str(item.get("event_date") or "") == event_date
            and (
                "ECMWF"
                if str(item.get("provider_id") or "").startswith("ecmwf")
                else "GFS"
                if str(item.get("provider_id") or "").startswith("gfs")
                else "ICON"
                if str(item.get("provider_id") or "").startswith("icon")
                else ""
            ) == model
            and str(item.get("model_run_lineage_sha256") or "")
            == str(lineage.get("model_run_id") or "")
        ]
        if len(matches) != 1 or matches[0].get("daily_curve_complete") is not True:
            raise ContractError("transport_reference_exact_model_row_missing")
        result[model] = _finite(
            "transport_historical_forecast_daily_max_c",
            matches[0].get("forecast_daily_max_c"),
        )
        assert_process_memory_budget()
    if set(result) != set(models):
        raise ContractError("transport_historical_model_set_incomplete")
    return result


def _bucket_probabilities(
    *,
    mean: float,
    scale: float,
    bucket_intervals: Mapping[str, Any],
) -> tuple[list[str], dict[str, float]]:
    if scale <= 0.0 or not math.isfinite(scale):
        raise ContractError("transport_probability_scale_invalid")
    parsed = []
    for bucket, bounds in bucket_intervals.items():
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ContractError("transport_bucket_interval_invalid")
        lower = None if bounds[0] is None else _finite(
            "transport_bucket_lower", bounds[0]
        )
        upper = None if bounds[1] is None else _finite(
            "transport_bucket_upper", bounds[1]
        )
        if lower is not None and upper is not None and lower >= upper:
            raise ContractError("transport_bucket_interval_invalid")
        parsed.append((str(bucket), lower, upper))
    parsed.sort(key=lambda item: (
        -math.inf if item[1] is None else item[1],
        math.inf if item[2] is None else item[2],
        item[0],
    ))
    if not parsed:
        raise ContractError("transport_bucket_intervals_empty")
    for left, right in zip(parsed, parsed[1:]):
        if left[2] is None or right[1] is None or abs(left[2] - right[1]) > 1e-9:
            raise ContractError("transport_bucket_intervals_not_contiguous")
    normal = NormalDist(mu=mean, sigma=scale)
    probabilities = {}
    for bucket, lower, upper in parsed:
        left = 0.0 if lower is None else normal.cdf(lower)
        right = 1.0 if upper is None else normal.cdf(upper)
        probabilities[bucket] = max(0.0, right - left)
    total = sum(probabilities.values())
    if total <= 0.0:
        raise ContractError("transport_probability_mass_invalid")
    probabilities = {
        bucket: value / total for bucket, value in probabilities.items()
    }
    return [item[0] for item in parsed], probabilities


def _truth_bucket(
    truth_c: float,
    bucket_intervals: Mapping[str, Any],
) -> str:
    matches = []
    for bucket, bounds in bucket_intervals.items():
        lower = None if bounds[0] is None else float(bounds[0])
        upper = None if bounds[1] is None else float(bounds[1])
        if (lower is None or truth_c >= lower) and (
            upper is None or truth_c < upper
        ):
            matches.append(str(bucket))
    if len(matches) != 1:
        raise ContractError("transport_truth_bucket_not_unique")
    return matches[0]


def _taf_mean(
    *,
    base_mean: float,
    taf: Mapping[str, Any],
    artifact: TafRevisionArtifact,
) -> float:
    tx = _finite("transport_taf_tx_c", taf.get("tx_c"))
    cloud = _finite(
        "transport_taf_cloud_cover_fraction",
        taf.get("cloud_cover_fraction"),
    )
    precipitation = _finite(
        "transport_taf_precipitation_probability",
        taf.get("precipitation_probability"),
    )
    wind = _finite(
        "transport_taf_wind_speed_mps",
        taf.get("wind_speed_mps"),
    )
    if not 0.0 <= cloud <= 1.0 or not 0.0 <= precipitation <= 1.0 or wind < 0.0:
        raise ContractError("transport_taf_physical_fields_invalid")
    physical = (
        artifact.physical_feature_effect_c["cloud_cover_fraction"] * cloud
        + artifact.physical_feature_effect_c["precipitation_probability"]
        * precipitation
        + artifact.physical_feature_effect_c["wind_speed_normalized"]
        * min(1.0, wind / artifact.wind_speed_cap_mps)
    )
    return (
        base_mean
        + artifact.tx_bias_c
        + artifact.taf_nwp_delta_coefficient * (tx - base_mean)
        + physical
    )


def _metric_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    prefix: str,
) -> dict[str, float]:
    if not rows:
        raise ContractError("transport_metric_rows_empty")
    brier = 0.0
    nll = 0.0
    crps = 0.0
    for row in rows:
        probabilities = row[f"{prefix}_probabilities"]
        ordered = row["ordered_buckets"]
        truth_bucket = row["truth_bucket"]
        brier += sum(
            (
                float(probabilities[bucket])
                - (1.0 if bucket == truth_bucket else 0.0)
            ) ** 2
            for bucket in ordered
        )
        truth_probability = max(
            MIN_PROBABILITY,
            float(probabilities[truth_bucket]),
        )
        nll += -math.log(truth_probability)
        forecast_cdf = 0.0
        truth_cdf = 0.0
        for bucket in ordered[:-1]:
            forecast_cdf += float(probabilities[bucket])
            truth_cdf += 1.0 if bucket == truth_bucket else 0.0
            crps += (forecast_cdf - min(1.0, truth_cdf)) ** 2
    count = len(rows)
    return {
        "brier_score": brier / count,
        "nll": nll / count,
        "crps": crps / count,
    }


def _effective_sample_n(residuals: Sequence[float]) -> float:
    count = len(residuals)
    if count < 2:
        return float(count)
    mean = sum(residuals) / count
    denominator = sum((value - mean) ** 2 for value in residuals)
    if denominator <= 1e-12:
        return float(count)
    numerator = sum(
        (left - mean) * (right - mean)
        for left, right in zip(residuals, residuals[1:])
    )
    rho = max(-0.8, min(0.8, numerator / denominator))
    return max(1.0, min(float(count), count * (1.0 - rho) / (1.0 + rho)))


def build_nested_history_prefix(
    *,
    taf_dataset: Mapping[str, Any],
    historical_evidence: Sequence[Mapping[str, Any]],
    station_id: str,
    settlement_timezone: str,
    models: Sequence[str],
) -> list[dict[str, Any]]:
    """Build training-only rows that precede every outer OOS evaluation row."""

    if (
        taf_dataset.get("schema") != AWC_TAF_DATASET_SCHEMA
        or taf_dataset.get("source_class")
        != "official_date_addressed_historical_replay"
        or str(taf_dataset.get("station_id") or "").upper()
        != station_id.upper()
        or str(taf_dataset.get("settlement_timezone") or "")
        != settlement_timezone
    ):
        raise ContractError("transport_history_prefix_taf_scope_invalid")
    _validate_content_hash(
        taf_dataset,
        name="transport_history_prefix_taf",
    )
    samples = taf_dataset.get("samples")
    if (
        not isinstance(samples, list)
        or not samples
        or len(samples) > NESTED_MIN_TRAIN_DAYS
        or int(taf_dataset.get("sample_count", -1)) != len(samples)
        or taf_dataset.get("blockers") != []
    ):
        raise ContractError("transport_history_prefix_taf_rows_invalid")

    evidence_rows: dict[tuple[str, str], tuple[float, str]] = {}
    evidence_hashes: list[str] = []
    for evidence in historical_evidence:
        if not isinstance(evidence, Mapping):
            raise ContractError("transport_history_prefix_evidence_invalid")
        evidence_hash = content_sha256(evidence)
        evidence_hashes.append(evidence_hash)
        rows = evidence.get("model_rows")
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ContractError("transport_history_prefix_model_rows_invalid")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ContractError(
                    "transport_history_prefix_model_row_invalid"
                )
            provider = str(row.get("provider_id") or "")
            model = (
                "ECMWF" if provider.startswith("ecmwf")
                else "GFS" if provider.startswith("gfs")
                else "ICON" if provider.startswith("icon")
                else ""
            )
            if model not in models:
                continue
            if (
                str(row.get("station_id") or "").upper()
                != station_id.upper()
                or str(row.get("target_timezone") or "")
                != settlement_timezone
                or row.get("daily_curve_complete") is not True
            ):
                continue
            event_date = str(row.get("event_date") or "")
            key = (event_date, model)
            value = (
                _finite(
                    "transport_history_prefix_forecast_daily_max_c",
                    row.get("forecast_daily_max_c"),
                ),
                evidence_hash,
            )
            prior = evidence_rows.get(key)
            if prior is not None and prior != value:
                raise ContractError(
                    "transport_history_prefix_model_content_conflict"
                )
            evidence_rows[key] = value

    prefix: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for sample in sorted(
        samples,
        key=lambda item: str((item or {}).get("event_date") or ""),
    ):
        if not isinstance(sample, Mapping):
            raise ContractError("transport_history_prefix_sample_invalid")
        event_date = str(sample.get("event_date") or "")
        if not event_date or event_date in seen_dates:
            raise ContractError("transport_history_prefix_date_duplicate")
        seen_dates.add(event_date)
        if (
            str(sample.get("station_id") or "").upper()
            != station_id.upper()
            or str(sample.get("settlement_timezone") or "")
            != settlement_timezone
        ):
            raise ContractError("transport_history_prefix_sample_scope_invalid")
        truth = sample.get("truth")
        window = sample.get("settlement_window")
        if not isinstance(truth, Mapping) or not isinstance(window, Mapping):
            raise ContractError("transport_history_prefix_truth_missing")
        expected_window = settlement_day_window(
            event_date,
            settlement_timezone,
        )
        if (
            str(window.get("start_utc") or "")
            != expected_window["target_day_start_utc"]
            or str(window.get("end_utc") or "")
            != expected_window["target_day_end_utc"]
        ):
            raise ContractError("transport_history_prefix_window_mismatch")
        forecasts: dict[str, float] = {}
        lineage: dict[str, str] = {}
        for model in models:
            matched = evidence_rows.get((event_date, model))
            if matched is None:
                raise ContractError(
                    "transport_history_prefix_exact_model_row_missing"
                )
            forecasts[model] = matched[0]
            lineage[model] = matched[1]
        prefix.append({
            "station_id": station_id.upper(),
            "event_date": event_date,
            "truth_tmax_c": _finite(
                "transport_history_prefix_truth_tmax_c",
                truth.get("tmax_c"),
            ),
            "historical_model_forecasts_c": forecasts,
            "taf_dataset_content_sha256": str(
                taf_dataset["content_sha256"]
            ),
            "model_evidence_content_sha256": lineage,
        })
    if len(prefix) != NESTED_MIN_TRAIN_DAYS:
        raise ContractError("transport_history_prefix_count_invalid")
    if len(set(evidence_hashes)) != len(evidence_hashes):
        raise ContractError("transport_history_prefix_evidence_duplicate")
    return prefix


def evaluate_transport_oos(
    *,
    exact_join: Mapping[str, Any],
    crossfit: Mapping[str, Any],
    live_p0_inbox: Mapping[str, Any],
    taf_artifact_raw: Mapping[str, Any],
    allowed_evidence_root: Path,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    nested_history_prefix: Sequence[Mapping[str, Any]] = (),
    total_input_bytes: int = 0,
    minimum_sample_n: int = MIN_TRANSPORT_SAMPLE_N,
) -> dict[str, Any]:
    """Evaluate one exact station×season×lead×cycle transport scope."""

    if minimum_sample_n < MIN_TRANSPORT_SAMPLE_N or minimum_sample_n > MAX_ROWS:
        raise ContractError("transport_minimum_sample_n_invalid")
    assert_process_memory_budget()
    crossfit_sha, predictions = _validate_crossfit(crossfit)
    samples = _validate_exact_join(
        exact_join,
        crossfit_sha256=crossfit_sha,
    )
    packet, ensemble, calibrations, live_fingerprint = _select_live_packet(
        live_p0_inbox,
        station_id=station_id,
        season_bin=season_bin,
        lead_bucket=lead_bucket,
        run_cycle=run_cycle,
    )
    taf_artifact = TafRevisionArtifact.from_mapping(taf_artifact_raw)
    if (
        taf_artifact.station_id,
        taf_artifact.season_bin,
        taf_artifact.lead_bucket,
        taf_artifact.run_cycle,
        taf_artifact.settlement_definition,
    ) != (
        station_id.upper(),
        season_bin,
        lead_bucket,
        run_cycle,
        ensemble.settlement_definition,
    ):
        raise ContractError("transport_taf_artifact_scope_mismatch")
    if taf_artifact.nwp_combination_artifact_sha256 != crossfit_sha:
        raise ContractError("transport_taf_crossfit_artifact_mismatch")
    if crossfit_sha == ensemble.content_sha256:
        raise ContractError("transport_crossfit_live_hash_alias_forbidden")

    timezone_name = taf_artifact.settlement_timezone
    models = tuple(sorted(calibrations))
    final_weights = ensemble.resolve_weights(models)
    if (
        ensemble.weight_method != FORMAL_ENSEMBLE_METHOD_VERSION
        or ensemble.max_weight_delta is None
        or abs(float(ensemble.max_weight_delta) - ENSEMBLE_MAX_WEIGHT_DELTA)
        > 1e-12
        or set(final_weights) != set(models)
    ):
        raise ContractError("transport_final_artifact_training_contract_mismatch")
    bucket_intervals = packet.get("bucket_intervals")
    if not isinstance(bucket_intervals, Mapping):
        raise ContractError("transport_live_bucket_intervals_missing")
    cache: dict[Path, Mapping[str, Any]] = {}
    bytes_used = [int(total_input_bytes)]
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for sample in sorted(samples, key=lambda item: str(item.get("event_date") or "")):
        if not isinstance(sample, Mapping):
            raise ContractError("transport_exact_join_sample_invalid")
        station = str(sample.get("station_id") or "").upper()
        event_date = str(sample.get("event_date") or "")
        key = (station, event_date)
        if key in seen:
            raise ContractError("transport_exact_join_scope_duplicate")
        seen.add(key)
        scope = sample.get("calibration_scope")
        if (
            station,
            str((scope or {}).get("season_bin") or ""),
            str((scope or {}).get("lead_bucket") or ""),
            str((scope or {}).get("run_cycle") or ""),
        ) != (
            station_id.upper(),
            season_bin,
            lead_bucket,
            run_cycle,
        ):
            continue
        truth = sample.get("truth")
        window = sample.get("settlement_window")
        taf = sample.get("taf")
        nwp = sample.get("nwp_combination")
        if not all(isinstance(item, Mapping) for item in (truth, window, taf, nwp)):
            raise ContractError("transport_sample_fields_incomplete")
        truth_closed = _utc("transport_truth_closed_at", window.get("end_utc"))
        prediction = predictions.get(key)
        if prediction is None:
            excluded.append({
                "station_id": station,
                "event_date": event_date,
                "reason": "crossfit_prediction_missing",
            })
            continue
        if prediction.get("prediction_oos") is not True:
            raise ContractError("transport_crossfit_prediction_not_oos")
        if date.fromisoformat(
            str(prediction.get("train_end_date") or "")
        ) >= date.fromisoformat(event_date):
            raise ContractError("transport_crossfit_training_leakage")
        forecasts = _historical_forecasts(
            prediction,
            models=models,
            allowed_root=allowed_evidence_root,
            cache=cache,
            total_bytes=bytes_used,
        )
        crossfit_base_mean = _finite(
            "transport_crossfit_combined_tmax_c",
            nwp.get("combined_tmax_c"),
        )
        if (
            _sha(
                "transport_sample_crossfit_artifact_sha256",
                nwp.get("artifact_sha256"),
            )
            != crossfit_sha
            or abs(
                crossfit_base_mean
                - _finite(
                    "transport_prediction_combined_tmax_c",
                    prediction.get("combined_tmax_c"),
                )
            )
            > 1e-9
        ):
            raise ContractError("transport_sample_crossfit_lineage_mismatch")
        truth_c = _finite("transport_truth_tmax_c", truth.get("tmax_c"))
        candidates.append({
            "station_id": station,
            "event_date": event_date,
            "truth_closed_at_utc": truth_closed.isoformat().replace(
                "+00:00", "Z"
            ),
            "crossfit_decision_at_utc": str(
                prediction.get("decision_at_utc") or ""
            ),
            "truth_tmax_c": truth_c,
            "crossfit_base_mean_c": crossfit_base_mean,
            "historical_model_forecasts_c": forecasts,
            "taf": dict(taf),
        })
        assert_process_memory_budget()

    evaluated: list[dict[str, Any]] = []
    history_dates: list[str] = []
    forecasts_by_model_date: dict[tuple[str, str], float] = {}
    residuals_by_model_date: dict[tuple[str, str], float] = {}
    truths_by_date: dict[str, float] = {}
    first_candidate_date = (
        str(candidates[0]["event_date"]) if candidates else ""
    )
    prefix_receipts: list[dict[str, Any]] = []
    for raw_prefix in nested_history_prefix:
        if not isinstance(raw_prefix, Mapping):
            raise ContractError("transport_history_prefix_row_invalid")
        prefix_date = str(raw_prefix.get("event_date") or "")
        forecasts = raw_prefix.get("historical_model_forecasts_c")
        if (
            str(raw_prefix.get("station_id") or "").upper()
            != station_id.upper()
            or not prefix_date
            or (first_candidate_date and prefix_date >= first_candidate_date)
            or prefix_date in history_dates
            or not isinstance(forecasts, Mapping)
            or set(str(key).upper() for key in forecasts) != set(models)
        ):
            raise ContractError("transport_history_prefix_scope_invalid")
        truth_c = _finite(
            "transport_history_prefix_truth_tmax_c",
            raw_prefix.get("truth_tmax_c"),
        )
        for model in models:
            forecast = _finite(
                "transport_history_prefix_forecast_c",
                forecasts[model],
            )
            forecasts_by_model_date[(model, prefix_date)] = forecast
            residuals_by_model_date[(model, prefix_date)] = (
                truth_c - forecast
            )
        truths_by_date[prefix_date] = truth_c
        history_dates.append(prefix_date)
        prefix_receipts.append({
            "station_id": station_id.upper(),
            "event_date": prefix_date,
            "taf_dataset_content_sha256": _sha(
                "transport_history_prefix_taf_sha256",
                raw_prefix.get("taf_dataset_content_sha256"),
            ),
            "model_evidence_content_sha256": {
                model: _sha(
                    "transport_history_prefix_model_evidence_sha256",
                    (raw_prefix.get(
                        "model_evidence_content_sha256"
                    ) or {}).get(model),
                )
                for model in models
            },
        })
    previous_weights: Mapping[str, float] | None = None
    for row in candidates:
        event_date = str(row["event_date"])
        truth_c = float(row["truth_tmax_c"])
        forecasts = dict(row["historical_model_forecasts_c"])
        for model in models:
            forecasts_by_model_date[(model, event_date)] = float(
                forecasts[model]
            )
            residuals_by_model_date[(model, event_date)] = (
                truth_c - float(forecasts[model])
            )
        truths_by_date[event_date] = truth_c
        if len(history_dates) < NESTED_MIN_TRAIN_DAYS:
            excluded.append({
                "station_id": station_id.upper(),
                "event_date": event_date,
                "reason": "nested_rolling_origin_burn_in",
            })
            history_dates.append(event_date)
            continue
        nested_weights, parameters, _fit_metrics = (
            _fit_rolling_probability_ensemble(
            models=models,
            history_dates=history_dates,
            forecasts_by_model_date=forecasts_by_model_date,
            residuals_by_model_date=residuals_by_model_date,
            truths_by_date=truths_by_date,
            previous_weights=previous_weights,
            max_weight_delta=float(ensemble.max_weight_delta),
            )
        )
        live_base_mean = sum(
            nested_weights[model]
            * (float(forecasts[model]) + parameters[model][0])
            for model in models
        )
        crossfit_base_mean = float(row["crossfit_base_mean_c"])
        baseline_mean = _taf_mean(
            base_mean=crossfit_base_mean,
            taf=row["taf"],
            artifact=taf_artifact,
        )
        transported_mean = _taf_mean(
            base_mean=live_base_mean,
            taf=row["taf"],
            artifact=taf_artifact,
        )
        ordered, baseline_probabilities = _bucket_probabilities(
            mean=baseline_mean,
            scale=taf_artifact.tx_residual_scale_c,
            bucket_intervals=bucket_intervals,
        )
        transported_ordered, transported_probabilities = _bucket_probabilities(
            mean=transported_mean,
            scale=taf_artifact.tx_residual_scale_c,
            bucket_intervals=bucket_intervals,
        )
        if transported_ordered != ordered:
            raise ContractError("transport_bucket_order_mismatch")
        evaluated.append({
            "station_id": station_id.upper(),
            "event_date": event_date,
            "truth_closed_at_utc": row["truth_closed_at_utc"],
            "crossfit_decision_at_utc": row["crossfit_decision_at_utc"],
            "truth_tmax_c": truth_c,
            "truth_bucket": _truth_bucket(truth_c, bucket_intervals),
            "ordered_buckets": ordered,
            "crossfit_base_mean_c": crossfit_base_mean,
            "transport_live_base_mean_c": live_base_mean,
            "baseline_taf_mean_c": baseline_mean,
            "transport_taf_mean_c": transported_mean,
            "baseline_probabilities": baseline_probabilities,
            "transport_probabilities": transported_probabilities,
            "historical_model_forecasts_c": forecasts,
            "nested_train_start_date": history_dates[0],
            "nested_train_end_date": history_dates[-1],
            "nested_train_n": len(history_dates),
            "nested_weights": nested_weights,
            "nested_calibration_parameters": {
                model: {
                    "bias_c": parameters[model][0],
                    "residual_scale_c": parameters[model][1],
                }
                for model in models
            },
        })
        previous_weights = nested_weights
        history_dates.append(event_date)
        assert_process_memory_budget()

    sample_n = len(evaluated)
    baseline_metrics = (
        _metric_rows(evaluated, prefix="baseline")
        if evaluated else None
    )
    transport_metrics = (
        _metric_rows(evaluated, prefix="transport")
        if evaluated else None
    )
    effective_n = _effective_sample_n([
        float(row["truth_tmax_c"]) - float(row["transport_taf_mean_c"])
        for row in evaluated
    ])
    enough_samples = sample_n >= minimum_sample_n
    nll_not_worse = bool(
        enough_samples
        and baseline_metrics is not None
        and transport_metrics is not None
        and transport_metrics["nll"] <= baseline_metrics["nll"] + 1e-12
    )
    crps_not_worse = bool(
        enough_samples
        and baseline_metrics is not None
        and transport_metrics is not None
        and transport_metrics["crps"] <= baseline_metrics["crps"] + 1e-12
    )
    validated = enough_samples and nll_not_worse and crps_not_worse
    evidence = None
    if evaluated:
        evidence_identity = {
            "schema": TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA,
            "evidence_id": "taf-nwp-transport-" + content_sha256({
                "crossfit_prediction_artifact_sha256": crossfit_sha,
                "live_fingerprint_sha256": live_fingerprint[
                    "fingerprint_sha256"
                ],
                "sample_ids": [
                    f"{row['station_id']}:{row['event_date']}"
                    for row in evaluated
                ],
            })[:32],
            "crossfit_prediction_artifact_sha256": crossfit_sha,
            "live_fingerprint_sha256": live_fingerprint[
                "fingerprint_sha256"
            ],
            "station_id": station_id.upper(),
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "settlement_definition": ensemble.settlement_definition,
            "sample_n": sample_n,
            "effective_sample_n": effective_n,
            "baseline_oos_metrics": baseline_metrics,
            "transport_oos_metrics": transport_metrics,
            "nll_not_worse": nll_not_worse,
            "crps_not_worse": crps_not_worse,
            "oos_transport_validated": validated,
        }
        evidence = {
            **evidence_identity,
            "content_sha256": content_sha256(evidence_identity),
        }
    if not enough_samples:
        state = "NA"
        gaps = [{
            "code": "transport_oos_post_training_sample_n_insufficient",
            "required_sample_n": minimum_sample_n,
            "actual_sample_n": sample_n,
            "missing_sample_n": minimum_sample_n - sample_n,
            "required_data": (
                "additional closed exact-scope outer OOS rows after nested "
                "rolling-origin burn-in, with matching TAF, truth and "
                "historical ECMWF/GFS raw daily maxima"
            ),
        }]
    elif not validated:
        state = "BLOCKED"
        gaps = [{
            "code": "transport_oos_metric_gate_failed",
            "nll_not_worse": nll_not_worse,
            "crps_not_worse": crps_not_worse,
        }]
    else:
        state = "PASS"
        gaps = []
    report_identity = {
        "schema": SCHEMA,
        "state": state,
        "station_id": station_id.upper(),
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_timezone": timezone_name,
        "settlement_definition": ensemble.settlement_definition,
        "validated_object": (
            "training_procedure_plus_final_artifact_scope"
        ),
        "training_procedure": {
            "method_version": FORMAL_ENSEMBLE_METHOD_VERSION,
            "split": "nested_rolling_origin",
            "minimum_inner_train_days": NESTED_MIN_TRAIN_DAYS,
            "model_mask": list(models),
            "max_weight_delta": float(ensemble.max_weight_delta),
            "final_resolved_weights_not_replayed_on_outer_labels": True,
            "training_only_history_prefix": prefix_receipts,
        },
        "crossfit_prediction_artifact_sha256": crossfit_sha,
        "live_fingerprint": live_fingerprint,
        "taf_artifact_id": taf_artifact.artifact_id,
        "taf_artifact_content_sha256": taf_artifact.content_sha256,
        "sample_n": sample_n,
        "effective_sample_n": effective_n,
        "minimum_sample_n": minimum_sample_n,
        "baseline_oos_metrics": baseline_metrics,
        "transport_oos_metrics": transport_metrics,
        "nll_not_worse": nll_not_worse,
        "crps_not_worse": crps_not_worse,
        "validated": validated,
        "evidence": evidence,
        "evaluated_rows": evaluated,
        "excluded_rows": excluded,
        "gaps": gaps,
        "resource_bounds": {
            "max_rows": MAX_ROWS,
            "max_reference_files": MAX_REFERENCE_FILES,
            "max_total_input_bytes": MAX_TOTAL_INPUT_BYTES,
            "reference_file_count": len(cache),
            "actual_total_input_bytes": bytes_used[0],
        },
        "boundary": {
            "strict_time_split": True,
            "current_live_prediction_reused_as_historical_label": False,
            "final_live_weights_replayed_on_historical_labels": False,
            "writes_configuration": False,
            "writes_database": False,
            "binds_artifact": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {
        **report_identity,
        "content_sha256": content_sha256(report_identity),
    }


def build_transport_oos_report(
    *,
    exact_join_path: Path,
    crossfit_path: Path,
    live_p0_inbox_path: Path,
    taf_artifact_path: Path,
    allowed_evidence_root: Path,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    history_prefix_taf_path: Path | None = None,
    history_prefix_evidence_paths: Sequence[Path] = (),
    minimum_sample_n: int = MIN_TRANSPORT_SAMPLE_N,
) -> dict[str, Any]:
    """Bounded filesystem wrapper returning BLOCKED instead of traceback."""

    paths = (
        exact_join_path,
        crossfit_path,
        live_p0_inbox_path,
        taf_artifact_path,
    )
    try:
        extra_paths = tuple(history_prefix_evidence_paths)
        if history_prefix_taf_path is not None:
            extra_paths = (history_prefix_taf_path, *extra_paths)
        total = sum(
            _bounded_size(
                path,
                limit=MAX_JSON_INPUT_BYTES,
                name="transport_primary_input",
            )
            for path in (*paths, *extra_paths)
        )
        if total > MAX_TOTAL_INPUT_BYTES:
            raise ContractError("transport_total_input_bytes_exceeded")
        values = [
            load_bounded_json(path, limit=MAX_JSON_INPUT_BYTES)
            for path in paths
        ]
        if any(not isinstance(value, Mapping) for value in values):
            raise ContractError("transport_primary_input_mapping_required")
        prefix: list[dict[str, Any]] = []
        if history_prefix_taf_path is not None:
            prefix_values = [
                load_bounded_json(path, limit=MAX_JSON_INPUT_BYTES)
                for path in extra_paths
            ]
            if (
                not prefix_values
                or any(
                    not isinstance(value, Mapping)
                    for value in prefix_values
                )
            ):
                raise ContractError(
                    "transport_history_prefix_input_mapping_required"
                )
            packet, ensemble, calibrations, _ = _select_live_packet(
                values[2],
                station_id=station_id,
                season_bin=season_bin,
                lead_bucket=lead_bucket,
                run_cycle=run_cycle,
            )
            prefix = build_nested_history_prefix(
                taf_dataset=prefix_values[0],
                historical_evidence=prefix_values[1:],
                station_id=station_id,
                settlement_timezone=(
                    TafRevisionArtifact.from_mapping(
                        values[3]
                    ).settlement_timezone
                ),
                models=tuple(sorted(calibrations)),
            )
        return evaluate_transport_oos(
            exact_join=values[0],
            crossfit=values[1],
            live_p0_inbox=values[2],
            taf_artifact_raw=values[3],
            allowed_evidence_root=allowed_evidence_root,
            station_id=station_id,
            season_bin=season_bin,
            lead_bucket=lead_bucket,
            run_cycle=run_cycle,
            nested_history_prefix=prefix,
            total_input_bytes=total,
            minimum_sample_n=minimum_sample_n,
        )
    except Exception as exc:
        identity = {
            "schema": SCHEMA,
            "state": "BLOCKED",
            "station_id": station_id.upper(),
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "validated": False,
            "evidence": None,
            "gaps": [{
                "code": "transport_oos_contract_blocker",
                "reason": f"{type(exc).__name__}:{exc}",
            }],
            "boundary": {
                "strict_time_split": True,
                "current_live_prediction_reused_as_historical_label": False,
                "writes_configuration": False,
                "writes_database": False,
                "binds_artifact": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
        return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "MAX_ROWS",
    "MIN_TRANSPORT_SAMPLE_N",
    "SCHEMA",
    "build_nested_history_prefix",
    "build_transport_oos_report",
    "evaluate_transport_oos",
]
