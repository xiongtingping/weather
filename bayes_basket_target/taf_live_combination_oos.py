"""Nested rolling-origin OOS training for the live NWP→TAF combination.

This module is deliberately separate from ``taf_nwp_transport_oos``.  The
transport evaluator asks whether an already fitted historical TAF artifact
can be moved to a different live stack.  This evaluator trains the TAF
revision procedure against the exact live stack contract itself.

The evaluator is offline and candidate-only.  It never changes a promoted
artifact, reads the order gateway, writes a database, or submits an order.
Every outer row is scored with parameters fitted strictly on dates before the
outer row.  The current live ensemble weights are used only as the initial
artifact contract; they are never replayed as the fitted weights on outer
labels.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .calibration import (
    build_taf_nwp_live_compatibility_fingerprint,
    validate_formal_ensemble_artifact,
)
from .calibration_evidence import (
    ENSEMBLE_MAX_WEIGHT_DELTA,
    _effective_sample_n as _weighted_effective_sample_n,
    _fit_rolling_probability_ensemble,
)
from .contracts import (
    FORMAL_ENSEMBLE_METHOD_VERSION,
    MIN_FORMAL_ENSEMBLE_MODELS,
    MODEL_IDS,
    ContractError,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    content_sha256,
)
from .resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "bayes_basket_taf_live_combination_oos_report_v1"
EVIDENCE_SCHEMA = "bayes_basket_taf_live_combination_oos_evidence_v1"
CANDIDATE_SCHEMA = "bayes_basket_taf_live_combination_artifact_candidate_v1"
LIVE_FINGERPRINT_SCHEMA = "bayes_basket_taf_nwp_live_compatibility_fingerprint_v1"
MIN_NESTED_TRAIN_DAYS = 3
MIN_LIVE_COMBINATION_SAMPLE_N = 24
MAX_ROWS = 366
MAX_RAW_FILES = 96
MAX_RAW_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 64 * 1024 * 1024
MIN_PROBABILITY = 1e-12
RIDGE = 1.0


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _hash_verified(raw: Mapping[str, Any], *, field: str = "content_sha256") -> str:
    supplied = _sha(field, raw.get(field))
    identity = {str(key): value for key, value in raw.items() if key != field}
    if content_sha256(identity) != supplied:
        raise ContractError(f"{field}_content_sha_mismatch")
    return supplied


def _packet_from_live_inbox(
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
    """Resolve one exact live packet and recompute its actual fingerprint."""

    if inbox.get("schema") != "bayes_basket_target_p0_inbox_v1":
        raise ContractError("live_combination_p0_inbox_schema_invalid")
    _hash_verified(inbox)
    packets = inbox.get("packets")
    if not isinstance(packets, list) or not packets or len(packets) > 256:
        raise ContractError("live_combination_p0_packets_invalid")
    matches: list[tuple[Mapping[str, Any], ModelEnsembleArtifact, dict[str, ModelCalibrationArtifact], dict[str, Any]]] = []
    for wrapper in packets:
        if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("packet"), Mapping):
            raise ContractError("live_combination_packet_invalid")
        packet = wrapper["packet"]
        packet_sha = _sha("live_combination_packet_content_sha256", wrapper.get("packet_content_sha256"))
        if content_sha256(packet) != packet_sha:
            raise ContractError("live_combination_packet_content_sha_mismatch")
        ensemble_raw = packet.get("ensemble_artifact")
        calibration_raw = packet.get("calibration_artifacts")
        if not isinstance(ensemble_raw, Mapping) or not isinstance(calibration_raw, Mapping):
            raise ContractError("live_combination_artifact_lineage_missing")
        ensemble = ModelEnsembleArtifact.from_mapping(ensemble_raw)
        calibrations = {
            str(model).strip().upper(): ModelCalibrationArtifact.from_mapping(value)
            for model, value in calibration_raw.items()
            if isinstance(value, Mapping)
        }
        models = tuple(sorted(calibrations))
        if len(models) < MIN_FORMAL_ENSEMBLE_MODELS or any(model not in MODEL_IDS for model in models):
            raise ContractError("live_combination_requires_at_least_two_models")
        validate_formal_ensemble_artifact(ensemble)
        fingerprint = build_taf_nwp_live_compatibility_fingerprint(
            ensemble_artifact=ensemble,
            calibration_artifacts=calibrations,
            available_models=models,
        )
        if (
            ensemble.station_id.upper(), ensemble.season_bin, ensemble.lead_bucket, ensemble.run_cycle
        ) != (station_id.upper(), season_bin, lead_bucket, run_cycle):
            continue
        if str(packet.get("station_id") or "").upper() != station_id.upper():
            continue
        matches.append((packet, ensemble, calibrations, fingerprint))
    if len(matches) != 1:
        raise ContractError("live_combination_exact_scope_not_unique")
    packet, ensemble, calibrations, fingerprint = matches[0]
    expected = packet.get("taf_live_combination_fingerprint")
    if expected is not None:
        if not isinstance(expected, Mapping) or expected != fingerprint:
            raise ContractError("live_combination_fingerprint_mismatch")
    return packet, ensemble, calibrations, fingerprint


def _model_from_row(row: Mapping[str, Any]) -> str:
    model = str(row.get("model_id") or row.get("model") or "").strip().upper()
    if model in MODEL_IDS:
        return model
    provider = str(row.get("provider_id") or "").strip().lower()
    if provider.startswith("ecmwf"):
        return "ECMWF"
    if provider.startswith("gfs"):
        return "GFS"
    if provider.startswith("icon"):
        return "ICON"
    return ""


def _bounded_file(path: Path, *, allowed_root: Path, cache: dict[Path, Mapping[str, Any]], total: list[int]) -> Mapping[str, Any]:
    resolved = path.expanduser().resolve()
    root = allowed_root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ContractError("live_combination_raw_ref_outside_allowed_root") from exc
    if resolved not in cache:
        if len(cache) >= MAX_RAW_FILES:
            raise ContractError("live_combination_raw_file_count_exceeded")
        try:
            size = int(resolved.stat().st_size)
        except OSError as exc:
            raise ContractError("live_combination_raw_file_stat_failed") from exc
        if size <= 0 or size > MAX_RAW_FILE_BYTES:
            raise ContractError("live_combination_raw_file_size_invalid")
        total[0] += size
        if total[0] > MAX_TOTAL_INPUT_BYTES:
            raise ContractError("live_combination_total_input_bytes_exceeded")
        loaded = load_bounded_json(resolved, limit=MAX_RAW_FILE_BYTES)
        if not isinstance(loaded, Mapping):
            raise ContractError("live_combination_raw_file_mapping_required")
        cache[resolved] = loaded
    return cache[resolved]


def _forecasts_from_observation(
    sample: Mapping[str, Any],
    *,
    models: tuple[str, ...],
    allowed_evidence_root: Path | None,
    cache: dict[Path, Mapping[str, Any]],
    total: list[int],
) -> tuple[dict[str, float], dict[str, str]]:
    nwp = sample.get("nwp_combination")
    if not isinstance(nwp, Mapping):
        raise ContractError("live_combination_nwp_missing")
    direct = nwp.get("historical_model_forecasts_c") or nwp.get("model_forecasts_c")
    values: dict[str, float] = {}
    lineage: dict[str, str] = {}
    if isinstance(direct, Mapping):
        direct_lineage = nwp.get("historical_model_forecast_lineage_sha256")
        if not isinstance(direct_lineage, Mapping):
            raise ContractError(
                "live_combination_direct_model_lineage_missing"
            )
        for model in models:
            if model not in direct:
                raise ContractError("live_combination_model_forecast_missing")
            values[model] = _finite("live_combination_model_forecast", direct[model])
            lineage[model] = _sha(
                "live_combination_direct_model_lineage_sha256",
                direct_lineage.get(model),
            )
        return values, lineage
    rows = nwp.get("model_rows")
    if not isinstance(rows, list) or len(rows) < len(models):
        raise ContractError("live_combination_model_rows_missing")
    event_date = str(sample.get("event_date") or "")
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ContractError("live_combination_model_row_invalid")
        model = _model_from_row(raw)
        if model not in models or model in values:
            continue
        if raw.get("raw_object_verified") is not True:
            raise ContractError("live_combination_raw_object_not_verified")
        source_sha = _sha(
            "live_combination_source_object_sha256",
            raw.get("source_object_sha256"),
        )
        raw_value = raw.get("forecast_daily_max_c")
        if raw_value is None:
            reference = str(raw.get("raw_object_ref") or "")
            path_text, separator, fragment = reference.partition("#")
            if separator != "#" or allowed_evidence_root is None:
                raise ContractError("live_combination_raw_reference_required")
            parts = fragment.split("/")
            if len(parts) != 3 or parts[0].upper() != str(sample.get("station_id") or "").upper() or parts[1] != event_date or parts[2].upper() != model:
                raise ContractError("live_combination_raw_reference_scope_invalid")
            evidence = _bounded_file(Path(path_text), allowed_root=allowed_evidence_root, cache=cache, total=total)
            evidence_rows = evidence.get("model_rows")
            if not isinstance(evidence_rows, list):
                raise ContractError("live_combination_raw_model_rows_missing")
            candidates = [
                item for item in evidence_rows
                if isinstance(item, Mapping)
                and str(item.get("station_id") or "").upper() == parts[0].upper()
                and str(item.get("event_date") or "") == event_date
                and _model_from_row(item) == model
                and (
                    not raw.get("model_run_id")
                    or str(item.get("model_run_lineage_sha256") or item.get("model_run_id") or "") == str(raw.get("model_run_id"))
                )
            ]
            if len(candidates) != 1 or candidates[0].get("daily_curve_complete") is not True:
                raise ContractError("live_combination_raw_exact_model_row_missing")
            raw_value = candidates[0].get("forecast_daily_max_c")
        lineage[model] = source_sha
        values[model] = _finite("live_combination_model_forecast", raw_value)
    if set(values) != set(models):
        raise ContractError("live_combination_model_set_incomplete")
    return values, lineage


def _normal_cdf(value: float, mean: float, scale: float) -> float:
    return 0.5 * (1.0 + math.erf((value - mean) / (scale * math.sqrt(2.0))))


def _buckets(packet: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    intervals = packet.get("bucket_intervals")
    if isinstance(intervals, Mapping) and intervals:
        parsed: list[tuple[str, float | None, float | None]] = []
        for bucket, bounds in intervals.items():
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ContractError("live_combination_bucket_interval_invalid")
            lower = None if bounds[0] is None else _finite("live_combination_bucket_lower", bounds[0])
            upper = None if bounds[1] is None else _finite("live_combination_bucket_upper", bounds[1])
            if lower is not None and upper is not None and lower >= upper:
                raise ContractError("live_combination_bucket_interval_invalid")
            parsed.append((str(bucket), lower, upper))
        parsed.sort(key=lambda item: (-math.inf if item[1] is None else item[1], item[0]))
        for left, right in zip(parsed, parsed[1:]):
            if left[2] is None or right[1] is None or abs(left[2] - right[1]) > 1e-9:
                raise ContractError("live_combination_bucket_intervals_not_contiguous")
        return tuple(item[0] for item in parsed)
    values = [int(math.floor(_finite("live_combination_truth", (item.get("truth") or {}).get("tmax_c")) + 0.5)) for item in observations]
    if not values:
        raise ContractError("live_combination_bucket_intervals_empty")
    lo, hi = min(values) - 8, max(values) + 8
    return tuple(str(item) for item in range(lo, hi + 1))


def _intervals_for_buckets(packet: Mapping[str, Any], buckets: tuple[str, ...]) -> dict[str, tuple[float | None, float | None]]:
    intervals = packet.get("bucket_intervals")
    if isinstance(intervals, Mapping) and set(str(key) for key in intervals) == set(buckets):
        return {
            str(bucket): (None if bounds[0] is None else float(bounds[0]), None if bounds[1] is None else float(bounds[1]))
            for bucket, bounds in intervals.items()
        }
    result: dict[str, tuple[float | None, float | None]] = {}
    for index, bucket in enumerate(buckets):
        value = float(bucket)
        result[bucket] = (None if index == 0 else value - 0.5, None if index == len(buckets) - 1 else value + 0.5)
    return result


def _probabilities(mean: float, scale: float, intervals: Mapping[str, tuple[float | None, float | None]]) -> dict[str, float]:
    if scale <= 0.0 or not math.isfinite(scale):
        raise ContractError("live_combination_probability_scale_invalid")
    result: dict[str, float] = {}
    for bucket, (lower, upper) in intervals.items():
        left = 0.0 if lower is None else _normal_cdf(lower, mean, scale)
        right = 1.0 if upper is None else _normal_cdf(upper, mean, scale)
        result[bucket] = max(0.0, right - left)
    total = sum(result.values())
    if total <= 0.0:
        raise ContractError("live_combination_probability_zero_mass")
    return {bucket: value / total for bucket, value in result.items()}


def _truth_bucket(value: float, intervals: Mapping[str, tuple[float | None, float | None]]) -> str:
    matches = [bucket for bucket, (lower, upper) in intervals.items() if (lower is None or value >= lower) and (upper is None or value < upper)]
    if len(matches) != 1:
        raise ContractError("live_combination_truth_bucket_not_unique")
    return matches[0]


def _metric_rows(rows: Sequence[Mapping[str, Any]], key: str, buckets: tuple[str, ...]) -> dict[str, float]:
    if not rows:
        raise ContractError("live_combination_metric_rows_empty")
    brier = nll = crps = mae = rmse = 0.0
    for row in rows:
        probs = row[f"{key}_probabilities"]
        truth_bucket = row["truth_bucket"]
        cumulative = truth_cdf = 0.0
        for bucket in buckets:
            probability = float(probs[bucket])
            brier += (probability - (1.0 if bucket == truth_bucket else 0.0)) ** 2
            cumulative += probability
            truth_cdf += 1.0 if bucket == truth_bucket else 0.0
            if bucket != buckets[-1]:
                crps += (cumulative - min(1.0, truth_cdf)) ** 2
        nll += -math.log(max(MIN_PROBABILITY, float(probs[truth_bucket])))
        error = float(row[f"{key}_mean_c"]) - float(row["truth_tmax_c"])
        mae += abs(error)
        rmse += error * error
    n = float(len(rows))
    return {"brier_score": brier / n, "nll": nll / n, "crps": crps / n, "mae_c": mae / n, "rmse_c": math.sqrt(rmse / n)}


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for pivot in range(n):
        best = max(range(pivot, n), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[best][pivot]) < 1e-10:
            augmented[pivot][pivot] += RIDGE
            best = pivot
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        divisor = augmented[pivot][pivot]
        for column in range(pivot, n + 1):
            augmented[pivot][column] /= divisor
        for row in range(n):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            if factor == 0.0:
                continue
            for column in range(pivot, n + 1):
                augmented[row][column] -= factor * augmented[pivot][column]
    return [augmented[index][n] for index in range(n)]


def _fit_taf_coefficients(rows: Sequence[Mapping[str, Any]], *, base_key: str = "base_mean_c") -> tuple[float, float, dict[str, float]]:
    if not rows:
        return 0.0, 0.0, {"cloud_cover_fraction": 0.0, "precipitation_probability": 0.0, "wind_speed_normalized": 0.0}
    features: list[list[float]] = []
    targets: list[float] = []
    for row in rows:
        taf = row["taf"]
        base = float(row[base_key])
        features.append([1.0, float(taf["tx_c"]) - base, float(taf["cloud_cover_fraction"]), float(taf["precipitation_probability"]), min(1.0, float(taf["wind_speed_mps"]) / 10.0)])
        targets.append(float(row["truth_tmax_c"]) - base)
    dimension = len(features[0])
    gram = [[0.0 for _ in range(dimension)] for _ in range(dimension)]
    rhs = [0.0 for _ in range(dimension)]
    for vector, target in zip(features, targets):
        for left in range(dimension):
            rhs[left] += vector[left] * target
            for right in range(dimension):
                gram[left][right] += vector[left] * vector[right]
    for index in range(dimension):
        gram[index][index] += RIDGE
    beta = _solve_linear(gram, rhs)
    residuals = [target - sum(coefficient * value for coefficient, value in zip(beta, vector)) for vector, target in zip(features, targets)]
    scale = math.sqrt(sum(value * value for value in residuals) / max(1, len(residuals) - 1)) if residuals else 1.0
    return beta[0], max(0.0, beta[1]), {
        "cloud_cover_fraction": beta[2],
        "precipitation_probability": beta[3],
        "wind_speed_normalized": beta[4],
        "residual_scale_c": max(0.25, scale),
    }


def _apply_taf(base: float, taf: Mapping[str, Any], tx_bias: float, coefficient: float, physical: Mapping[str, float]) -> float:
    return base + tx_bias + coefficient * (float(taf["tx_c"]) - base) + float(physical["cloud_cover_fraction"]) * float(taf["cloud_cover_fraction"]) + float(physical["precipitation_probability"]) * float(taf["precipitation_probability"]) + float(physical["wind_speed_normalized"]) * min(1.0, float(taf["wind_speed_mps"]) / 10.0)


def _normalize_observations(
    observations: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    settlement_timezone: str,
    models: tuple[str, ...],
    allowed_evidence_root: Path | None,
    live_fingerprint_sha256: str,
    live_ensemble_content_sha256: str,
    live_selection_evidence_sha256: str | None,
) -> tuple[list[dict[str, Any]], dict[Path, Mapping[str, Any]], int]:
    if isinstance(observations, Mapping) and observations.get("content_sha256") is not None:
        _hash_verified(observations)
    raw_rows = observations.get("samples") if isinstance(observations, Mapping) else observations
    if not isinstance(raw_rows, list) or not raw_rows or len(raw_rows) > MAX_ROWS:
        raise ContractError("live_combination_observation_rows_invalid")
    cache: dict[Path, Mapping[str, Any]] = {}
    total = [0]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sample in sorted(raw_rows, key=lambda item: str(item.get("event_date") or "") if isinstance(item, Mapping) else ""):
        if not isinstance(sample, Mapping):
            raise ContractError("live_combination_observation_invalid")
        event_date = _text("live_combination_event_date", sample.get("event_date"))
        try:
            date.fromisoformat(event_date)
        except ValueError as exc:
            raise ContractError("live_combination_event_date_invalid") from exc
        if event_date in seen:
            raise ContractError("live_combination_observation_date_duplicate")
        seen.add(event_date)
        if str(sample.get("station_id") or "").upper() != station_id.upper() or str(sample.get("settlement_timezone") or "") != settlement_timezone:
            raise ContractError("live_combination_observation_scope_mismatch")
        scope = sample.get("calibration_scope") or {}
        if not isinstance(scope, Mapping) or (str(scope.get("season_bin") or ""), str(scope.get("lead_bucket") or ""), str(scope.get("run_cycle") or "")) != (season_bin, lead_bucket, run_cycle):
            raise ContractError("live_combination_observation_calibration_scope_mismatch")
        if scope.get("settlement_definition") not in (None, "") and str(scope.get("settlement_definition")) != settlement_definition:
            raise ContractError("live_combination_settlement_definition_mismatch")
        truth = sample.get("truth")
        taf = sample.get("taf")
        window = sample.get("settlement_window")
        if not isinstance(truth, Mapping) or not isinstance(taf, Mapping) or not isinstance(window, Mapping):
            raise ContractError("live_combination_observation_fields_missing")
        truth_c = _finite("live_combination_truth_tmax_c", truth.get("tmax_c"))
        taf_values = {
            "tx_c": _finite("live_combination_taf_tx_c", taf.get("tx_c")),
            "cloud_cover_fraction": _finite("live_combination_taf_cloud", taf.get("cloud_cover_fraction")),
            "precipitation_probability": _finite("live_combination_taf_precipitation", taf.get("precipitation_probability")),
            "wind_speed_mps": _finite("live_combination_taf_wind", taf.get("wind_speed_mps")),
        }
        if not 0.0 <= taf_values["cloud_cover_fraction"] <= 1.0 or not 0.0 <= taf_values["precipitation_probability"] <= 1.0 or taf_values["wind_speed_mps"] < 0.0:
            raise ContractError("live_combination_taf_physical_fields_invalid")
        closed_at = _utc("live_combination_truth_closed_at", window.get("end_utc"))
        nwp = sample.get("nwp_combination")
        if not isinstance(nwp, Mapping):
            raise ContractError("live_combination_nwp_missing")
        decision_value = nwp.get("decision_at_utc") or sample.get("decision_anchor_utc")
        decision_at = None if decision_value in (None, "") else _utc("live_combination_nwp_decision_at", decision_value)
        if decision_at is None:
            raise ContractError("live_combination_nwp_decision_at_missing")
        if decision_at is not None and decision_at >= closed_at:
            raise ContractError("live_combination_nwp_as_of_after_truth_close")
        for name in ("decision_anchor_utc",):
            if sample.get(name) is not None and _utc(name, sample[name]) >= closed_at:
                raise ContractError("live_combination_as_of_after_truth_close")
        if taf.get("available_time_utc") is not None and _utc("live_combination_taf_available_time", taf["available_time_utc"]) >= closed_at:
            raise ContractError("live_combination_taf_as_of_after_truth_close")
        model_rows = nwp.get("model_rows")
        if isinstance(model_rows, list):
            for model_row in model_rows:
                if not isinstance(model_row, Mapping):
                    raise ContractError("live_combination_model_row_invalid")
                for timing_field in ("published_at_utc", "available_at_utc", "received_at_utc"):
                    if model_row.get(timing_field) in (None, ""):
                        continue
                    timing = _utc(f"live_combination_model_{timing_field}", model_row[timing_field])
                    if timing >= closed_at or (decision_at is not None and timing > decision_at):
                        raise ContractError("live_combination_model_as_of_after_decision")
        if nwp.get("prediction_oos") is not True:
            raise ContractError("live_combination_historical_prediction_not_oos")
        selected_models = tuple(sorted(
            str(model).strip().upper()
            for model in (nwp.get("selected_models") or [])
        ))
        if selected_models != models:
            raise ContractError("live_combination_historical_model_mask_mismatch")
        _sha(
            "live_combination_row_selection_evidence_sha256",
            nwp.get("selection_evidence_sha256"),
        )
        forecasts, lineage = _forecasts_from_observation(
            sample,
            models=models,
            allowed_evidence_root=allowed_evidence_root,
            cache=cache,
            total=total,
        )
        row_fingerprint = nwp.get("live_fingerprint_sha256")
        if row_fingerprint is not None and _sha("live_combination_row_live_fingerprint_sha256", row_fingerprint) != live_fingerprint_sha256:
            raise ContractError("live_combination_row_live_fingerprint_mismatch")
        live_lineage = sample.get("live_combination_lineage")
        if live_lineage is not None:
            if not isinstance(live_lineage, Mapping):
                raise ContractError("live_combination_row_live_lineage_invalid")
            if _sha("live_combination_lineage_fingerprint_sha256", live_lineage.get("fingerprint_sha256")) != live_fingerprint_sha256:
                raise ContractError("live_combination_row_live_lineage_fingerprint_mismatch")
            if _sha("live_combination_lineage_ensemble_content_sha256", live_lineage.get("ensemble_artifact_content_sha256")) != live_ensemble_content_sha256:
                raise ContractError("live_combination_row_live_lineage_ensemble_mismatch")
            if live_selection_evidence_sha256 is not None and _sha("live_combination_lineage_selection_evidence_sha256", live_lineage.get("selection_evidence_sha256")) != live_selection_evidence_sha256:
                raise ContractError("live_combination_row_live_lineage_selection_mismatch")
            lineage_models = tuple(sorted(str(model).strip().upper() for model in (live_lineage.get("model_mask") or [])))
            if lineage_models != models:
                raise ContractError("live_combination_row_live_lineage_model_mask_mismatch")
        normalized.append({
            "station_id": station_id.upper(),
            "event_date": event_date,
            "truth_tmax_c": truth_c,
            "taf": taf_values,
            "historical_model_forecasts_c": forecasts,
            "model_lineage": lineage,
            "truth_closed_at_utc": closed_at.isoformat().replace("+00:00", "Z"),
            "decision_at_utc": str(nwp.get("decision_at_utc") or sample.get("decision_anchor_utc") or ""),
            "row_live_fingerprint_sha256": None if row_fingerprint is None else _sha("live_combination_row_live_fingerprint_sha256", row_fingerprint),
            "selection_evidence_sha256": None if nwp.get("selection_evidence_sha256") is None else _sha("live_combination_row_selection_evidence_sha256", nwp.get("selection_evidence_sha256")),
        })
        assert_process_memory_budget()
    return normalized, cache, total[0]


def _fit_nested_weights(rows: Sequence[Mapping[str, Any]], models: tuple[str, ...], *, live_weights: Mapping[str, float], max_weight_delta: float) -> tuple[dict[str, float], dict[str, float], dict[str, float], float]:
    dates = [str(row["event_date"]) for row in rows]
    forecasts = {(model, str(row["event_date"])): float(row["historical_model_forecasts_c"][model]) for row in rows for model in models}
    truths = {str(row["event_date"]): float(row["truth_tmax_c"]) for row in rows}
    residuals = {(model, day): truths[day] - forecasts[(model, day)] for model in models for day in dates}
    if len(rows) < MIN_NESTED_TRAIN_DAYS:
        raise ContractError("live_combination_nested_history_insufficient")
    weights, parameters, _metrics = _fit_rolling_probability_ensemble(
        models=models,
        history_dates=dates,
        forecasts_by_model_date=forecasts,
        residuals_by_model_date=residuals,
        truths_by_date=truths,
        previous_weights=live_weights,
        max_weight_delta=max_weight_delta,
    )
    return weights, {model: parameters[model][0] for model in models}, {model: parameters[model][1] for model in models}, max(0.25, math.sqrt(sum((truths[day] - sum(weights[model] * (forecasts[(model, day)] + parameters[model][0]) for model in models)) ** 2 for day in dates) / max(1, len(dates) - 1)))


def evaluate_taf_live_combination_oos(
    *,
    observations: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    live_p0_inbox: Mapping[str, Any],
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    allowed_evidence_root: Path | None = None,
    minimum_sample_n: int = MIN_LIVE_COMBINATION_SAMPLE_N,
    minimum_train_days: int = MIN_NESTED_TRAIN_DAYS,
    expected_live_fingerprint: Mapping[str, Any] | None = None,
    live_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a TAF procedure against the exact current live NWP contract."""

    if minimum_sample_n < 1 or minimum_sample_n > MAX_ROWS or minimum_train_days < MIN_NESTED_TRAIN_DAYS:
        raise ContractError("live_combination_oos_threshold_invalid")
    assert_process_memory_budget()
    packet, ensemble, calibrations, fingerprint = _packet_from_live_inbox(live_p0_inbox, station_id=station_id, season_bin=season_bin, lead_bucket=lead_bucket, run_cycle=run_cycle)
    models = tuple(sorted(calibrations))
    if len(models) < MIN_FORMAL_ENSEMBLE_MODELS:
        raise ContractError("live_combination_formal_model_mask_requires_two")
    for supplied_fingerprint in (expected_live_fingerprint, live_fingerprint):
        if supplied_fingerprint is not None and supplied_fingerprint != fingerprint:
            raise ContractError("live_combination_expected_fingerprint_mismatch")
    live_weights = {model: float(value) for model, value in ensemble.resolve_weights(models).items()}
    if ensemble.weight_method != FORMAL_ENSEMBLE_METHOD_VERSION or ensemble.max_weight_delta is None or abs(float(ensemble.max_weight_delta) - ENSEMBLE_MAX_WEIGHT_DELTA) > 1e-12:
        raise ContractError("live_combination_training_contract_invalid")
    settlement_timezone = str(packet.get("settlement_timezone") or "")
    if not settlement_timezone:
        raise ContractError("live_combination_settlement_timezone_missing")
    rows, raw_cache, total_bytes = _normalize_observations(observations, station_id=station_id, season_bin=season_bin, lead_bucket=lead_bucket, run_cycle=run_cycle, settlement_definition=ensemble.settlement_definition, settlement_timezone=settlement_timezone, models=models, allowed_evidence_root=allowed_evidence_root, live_fingerprint_sha256=fingerprint["fingerprint_sha256"], live_ensemble_content_sha256=ensemble.content_sha256, live_selection_evidence_sha256=ensemble.selection_evidence_sha256)
    intervals = _intervals_for_buckets(packet, _buckets(packet, observations if isinstance(observations, Sequence) else (observations.get("samples") or [])))
    buckets = tuple(intervals)
    evaluated: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        history = rows[:index]
        if len(history) < minimum_train_days:
            excluded.append({"station_id": station_id.upper(), "event_date": row["event_date"], "reason": "nested_rolling_origin_burn_in"})
            continue
        nested_weights, biases, scales, base_scale = _fit_nested_weights(history, models, live_weights=live_weights, max_weight_delta=float(ensemble.max_weight_delta))
        base_mean = sum(nested_weights[model] * (float(row["historical_model_forecasts_c"][model]) + biases[model]) for model in models)
        history_for_taf = []
        for prior in history:
            prior_base = sum(nested_weights[model] * (float(prior["historical_model_forecasts_c"][model]) + biases[model]) for model in models)
            history_for_taf.append({**prior, "base_mean_c": prior_base})
        tx_bias, tx_coefficient, physical = _fit_taf_coefficients(history_for_taf)
        taf_physical = {key: value for key, value in physical.items() if key != "residual_scale_c"}
        taf_mean = _apply_taf(base_mean, row["taf"], tx_bias, tx_coefficient, taf_physical)
        scale = max(0.25, float(physical.get("residual_scale_c", base_scale)))
        base_probabilities = _probabilities(base_mean, max(0.25, base_scale), intervals)
        taf_probabilities = _probabilities(taf_mean, scale, intervals)
        evaluated.append({
            "station_id": station_id.upper(),
            "event_date": row["event_date"],
            "truth_closed_at_utc": row["truth_closed_at_utc"],
            "decision_at_utc": row["decision_at_utc"],
            "truth_tmax_c": row["truth_tmax_c"],
            "truth_bucket": _truth_bucket(row["truth_tmax_c"], intervals),
            "base_mean_c": base_mean,
            "taf_mean_c": taf_mean,
            "base_probabilities": base_probabilities,
            "taf_probabilities": taf_probabilities,
            "nested_train_start_date": history[0]["event_date"],
            "nested_train_end_date": history[-1]["event_date"],
            "nested_train_n": len(history),
            "nested_weights": nested_weights,
            "nested_calibration_bias_c": biases,
            "nested_calibration_scale_c": scales,
            "taf_coefficients": {"tx_bias_c": tx_bias, "taf_nwp_delta_coefficient": tx_coefficient, **taf_physical, "tx_residual_scale_c": scale},
            "historical_model_lineage": row["model_lineage"],
        })
        assert_process_memory_budget()
    base_metrics = _metric_rows(evaluated, "base", buckets) if evaluated else None
    taf_metrics = _metric_rows(evaluated, "taf", buckets) if evaluated else None
    effective_n = _weighted_effective_sample_n([row["event_date"] for row in evaluated], set(row["event_date"] for row in evaluated[-max(1, len(evaluated) // 2):])) if evaluated else 0.0
    enough = len(evaluated) >= minimum_sample_n
    nll_not_worse = bool(enough and base_metrics and taf_metrics and taf_metrics["nll"] <= base_metrics["nll"] + 1e-12)
    crps_not_worse = bool(enough and base_metrics and taf_metrics and taf_metrics["crps"] <= base_metrics["crps"] + 1e-12)
    validated = bool(enough and effective_n > 0.0 and nll_not_worse and crps_not_worse and len(models) >= 2)
    state = "PASS" if validated else ("NA" if not enough else "BLOCKED")
    gaps: list[dict[str, Any]] = []
    if not enough:
        gaps.append({"code": "live_combination_oos_sample_n_insufficient", "required_sample_n": minimum_sample_n, "actual_sample_n": len(evaluated), "missing_sample_n": minimum_sample_n - len(evaluated)})
    elif not validated:
        gaps.append({"code": "live_combination_oos_metric_gate_failed", "nll_not_worse": nll_not_worse, "crps_not_worse": crps_not_worse})
    evidence = None
    if evaluated and validated:
        evidence_identity = {
            "schema": EVIDENCE_SCHEMA,
            "evidence_id": "taf-live-combination-" + content_sha256({"fingerprint": fingerprint["fingerprint_sha256"], "dates": [row["event_date"] for row in evaluated]})[:32],
            "live_fingerprint_sha256": fingerprint["fingerprint_sha256"],
            "station_id": station_id.upper(), "season_bin": season_bin, "lead_bucket": lead_bucket, "run_cycle": run_cycle,
            "settlement_definition": ensemble.settlement_definition,
            "sample_n": len(evaluated), "effective_sample_n": effective_n,
            "baseline_oos_metrics": base_metrics, "taf_live_combination_oos_metrics": taf_metrics,
            "nll_not_worse": nll_not_worse, "crps_not_worse": crps_not_worse,
            "nested_oos_validated": True, "candidate_only": True,
        }
        evidence = {**evidence_identity, "content_sha256": content_sha256(evidence_identity)}
    candidate = None
    if rows:
        final_weights, final_biases, _final_scales, final_base_scale = _fit_nested_weights(rows, models, live_weights=live_weights, max_weight_delta=float(ensemble.max_weight_delta))
        final_base_rows = [{**row, "base_mean_c": sum(final_weights[model] * (float(row["historical_model_forecasts_c"][model]) + final_biases[model]) for model in models)} for row in rows]
        final_tx_bias, final_tx_coefficient, final_physical = _fit_taf_coefficients(final_base_rows)
        final_residual_scale = max(0.25, float(final_physical.pop("residual_scale_c", final_base_scale)))
        candidate_identity = {
            "schema": CANDIDATE_SCHEMA,
            "artifact_id": "taf-live-candidate-" + content_sha256({"fingerprint": fingerprint["fingerprint_sha256"], "last_event_date": rows[-1]["event_date"]})[:24],
            "station_id": station_id.upper(), "season_bin": season_bin, "lead_bucket": lead_bucket, "run_cycle": run_cycle,
            "settlement_definition": ensemble.settlement_definition, "settlement_timezone": str(packet.get("settlement_timezone") or ""),
            "trained_through_event_date": rows[-1]["event_date"], "sample_n": len(rows), "effective_sample_n": effective_n,
            "live_fingerprint_sha256": fingerprint["fingerprint_sha256"],
            "ensemble_artifact_id": ensemble.ensemble_artifact_id, "ensemble_artifact_content_sha256": ensemble.content_sha256,
            "selection_evidence_sha256": ensemble.selection_evidence_sha256,
            "model_mask": list(models), "live_resolved_weights": live_weights, "final_training_weights": final_weights,
            "calibration_lineage": {model: {"calibration_artifact_id": calibrations[model].calibration_artifact_id, "content_sha256": calibrations[model].content_sha256} for model in models},
            "tx_bias_c": final_tx_bias, "taf_nwp_delta_coefficient": final_tx_coefficient, "tx_residual_scale_c": final_residual_scale,
            "physical_feature_effect_c": final_physical,
            "oos_evidence_sha256": None if evidence is None else evidence["content_sha256"],
            "candidate_only": True, "activated": False, "online_weight_update": False,
        }
        candidate = {**candidate_identity, "content_sha256": content_sha256(candidate_identity)}
    report_identity = {
        "schema": SCHEMA, "state": state, "validated": validated, "candidate_only": True,
        "station_id": station_id.upper(), "season_bin": season_bin, "lead_bucket": lead_bucket, "run_cycle": run_cycle,
        "settlement_definition": ensemble.settlement_definition, "settlement_timezone": str(packet.get("settlement_timezone") or ""),
        "live_fingerprint": fingerprint,
        "live_training_contract": {
            "ensemble_artifact_id": ensemble.ensemble_artifact_id, "ensemble_artifact_content_sha256": ensemble.content_sha256,
            "model_mask": list(models), "resolved_weights": live_weights, "selection_evidence_sha256": ensemble.selection_evidence_sha256,
            "calibration_lineage": {model: {"calibration_artifact_id": calibrations[model].calibration_artifact_id, "content_sha256": calibrations[model].content_sha256} for model in models},
            "weight_method": ensemble.weight_method, "max_weight_delta": ensemble.max_weight_delta,
        },
        "nested_oos": {"split": "nested_rolling_origin", "minimum_inner_train_days": minimum_train_days, "final_live_weights_replayed_on_outer_labels": False, "online_weight_update": False},
        "sample_n": len(evaluated), "effective_sample_n": effective_n, "minimum_sample_n": minimum_sample_n,
        "baseline_oos_metrics": base_metrics, "taf_live_combination_oos_metrics": taf_metrics,
        "nll_not_worse": nll_not_worse, "crps_not_worse": crps_not_worse,
        "candidate_artifact": candidate, "bridge_evidence": evidence,
        "evaluated_rows": evaluated, "excluded_rows": excluded, "gaps": gaps,
        "resource_bounds": {"max_rows": MAX_ROWS, "max_raw_files": MAX_RAW_FILES, "max_total_input_bytes": MAX_TOTAL_INPUT_BYTES, "raw_file_count": len(raw_cache), "actual_total_input_bytes": total_bytes},
        "boundary": {"strict_as_of": True, "writes_configuration": False, "writes_database": False, "calls_order_gateway": False, "places_orders": False, "promotes_artifact": False},
    }
    return {**report_identity, "content_sha256": content_sha256(report_identity)}


def build_taf_live_combination_oos_report(
    *,
    observations_path: Path,
    live_p0_inbox_path: Path,
    allowed_evidence_root: Path | None,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    minimum_sample_n: int = MIN_LIVE_COMBINATION_SAMPLE_N,
    minimum_train_days: int = MIN_NESTED_TRAIN_DAYS,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Bounded filesystem entry point; blockers become a content-addressed report."""

    try:
        paths = [observations_path, live_p0_inbox_path]
        sizes = []
        for path in paths:
            resolved = Path(path).expanduser().resolve()
            size = int(resolved.stat().st_size)
            if size <= 0 or size > MAX_JSON_INPUT_BYTES:
                raise ContractError("live_combination_primary_input_size_invalid")
            sizes.append(size)
        if sum(sizes) > MAX_TOTAL_INPUT_BYTES:
            raise ContractError("live_combination_total_input_bytes_exceeded")
        observations = load_bounded_json(observations_path, limit=MAX_JSON_INPUT_BYTES)
        inbox = load_bounded_json(live_p0_inbox_path, limit=MAX_JSON_INPUT_BYTES)
        if not isinstance(observations, (Mapping, list)) or not isinstance(inbox, Mapping):
            raise ContractError("live_combination_primary_input_type_invalid")
        report = evaluate_taf_live_combination_oos(observations=observations, live_p0_inbox=inbox, station_id=station_id, season_bin=season_bin, lead_bucket=lead_bucket, run_cycle=run_cycle, allowed_evidence_root=allowed_evidence_root, minimum_sample_n=minimum_sample_n, minimum_train_days=minimum_train_days)
    except Exception as exc:
        identity = {"schema": SCHEMA, "state": "BLOCKED", "validated": False, "candidate_only": True, "station_id": station_id.upper(), "season_bin": season_bin, "lead_bucket": lead_bucket, "run_cycle": run_cycle, "candidate_artifact": None, "bridge_evidence": None, "gaps": [{"code": "live_combination_oos_contract_blocker", "reason": f"{type(exc).__name__}:{exc}"}], "boundary": {"strict_as_of": True, "writes_configuration": False, "writes_database": False, "calls_order_gateway": False, "places_orders": False, "promotes_artifact": False}}
        report = {**identity, "content_sha256": content_sha256(identity)}
    if output_path is not None:
        write_bounded_json_if_changed(output_path, report)
    return report


__all__ = [
    "CANDIDATE_SCHEMA", "EVIDENCE_SCHEMA", "MAX_ROWS", "MIN_LIVE_COMBINATION_SAMPLE_N", "MIN_NESTED_TRAIN_DAYS", "SCHEMA",
    "build_taf_live_combination_oos_report", "evaluate_taf_live_combination_oos",
]
