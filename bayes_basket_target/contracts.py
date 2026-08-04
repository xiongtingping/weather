"""Immutable contracts for the Bayes × basket target compiler."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping


GLOBAL_FORMAL_MODEL_IDS = ("ECMWF", "GFS", "ICON", "GEM", "CMA")
# Backward-compatible production hot-path name. Do not widen this alias:
# P0, formal activation, native arrival and production probability readers
# intentionally remain bound to the global five-model authority.
MODEL_IDS = GLOBAL_FORMAL_MODEL_IDS
REGIONAL_CALIBRATION_MODEL_IDS = (
    "HRRR",
    "NBM",
    "NAM",
    "ICON-EU",
    "ICON-D2",
    "AROME-FR",
    "UKMO-2KM",
    "HARMONIE-NL",
    "HARMONIE-EU",
    "JMA-MSM",
)
CALIBRATION_CANDIDATE_MODEL_IDS = (
    GLOBAL_FORMAL_MODEL_IDS + REGIONAL_CALIBRATION_MODEL_IDS
)
MIN_FORMAL_ENSEMBLE_MODELS = 2
FORMAL_ENSEMBLE_METHOD_VERSION = (
    "rolling_origin_probability_stack_v4_five_model_pool_min2"
)
STAGES = ("P0", "P0-M", "P1")
EPS = 1e-9


class ContractError(ValueError):
    """Raised when a target-architecture contract cannot be trusted."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def model_set_hash(models: Iterable[str]) -> str:
    members = tuple(sorted({str(model).strip().upper() for model in models}))
    if not members or any(model not in MODEL_IDS for model in members):
        raise ContractError("model_set_invalid")
    return hashlib.sha256("|".join(members).encode("utf-8")).hexdigest()


def calibration_candidate_model_set_hash(models: Iterable[str]) -> str:
    """Hash an offline candidate set without widening formal authority."""

    members = tuple(sorted({str(model).strip().upper() for model in models}))
    if not members or any(
        model not in CALIBRATION_CANDIDATE_MODEL_IDS for model in members
    ):
        raise ContractError("calibration_candidate_model_set_invalid")
    return hashlib.sha256("|".join(members).encode("utf-8")).hexdigest()


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_must_be_finite_number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_must_be_finite_number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_must_be_finite_number")
    return result


def _nonnegative(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result < 0:
        raise ContractError(f"{name}_must_be_nonnegative")
    return result


def _positive(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result <= 0:
        raise ContractError(f"{name}_must_be_positive")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def timestamp_lte(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) <= datetime.fromisoformat(right.replace("Z", "+00:00"))


timestamp_leq = timestamp_lte


def timestamp_lt(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) < datetime.fromisoformat(right.replace("Z", "+00:00"))


def timestamp_eq(left: str, right: str) -> bool:
    """Compare timestamp instants, independent of ISO fractional precision."""

    try:
        left_dt = datetime.fromisoformat(
            str(left).replace("Z", "+00:00")
        )
        right_dt = datetime.fromisoformat(
            str(right).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    if (
        left_dt.tzinfo is None
        or left_dt.utcoffset() is None
        or right_dt.tzinfo is None
        or right_dt.utcoffset() is None
    ):
        return False
    return left_dt.astimezone(timezone.utc) == right_dt.astimezone(
        timezone.utc
    )


def timestamp_in_window(value: str, valid_from: str, valid_until: str) -> bool:
    return timestamp_lte(valid_from, value) and timestamp_lt(value, valid_until)


def assert_timestamp_not_future(
    value: str,
    *,
    name: str = "as_of_utc",
    now_utc: datetime | None = None,
) -> str:
    """Reject evidence clocks that are later than the host's current UTC."""

    normalized = _timestamp(name, value)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    current = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if parsed > current:
        raise ContractError(f"{name}_in_future")
    return normalized


def _local_date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_date") from exc
    return result


def strict_asof(published_at: str, received_at: str, decision_at: str) -> bool:
    """Return whether both source timestamps are known by the decision point."""

    return timestamp_lte(published_at, decision_at) and timestamp_lte(received_at, decision_at)


@dataclass(frozen=True)
class ResidualObservation:
    residual_id: str
    station_id: str
    model_id: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    residual_c: float
    target_local_date: str
    paired_asof_utc: str
    qc_status: str = "ACCEPTED"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResidualObservation":
        model = _text("model_id", raw.get("model_id")).upper()
        if model not in MODEL_IDS:
            raise ContractError("model_id_invalid")
        qc = _text("qc_status", raw.get("qc_status", "ACCEPTED")).upper()
        if qc not in {"ACCEPTED", "QUARANTINED"}:
            raise ContractError("residual_qc_status_invalid")
        return cls(
            residual_id=_text("residual_id", raw.get("residual_id")),
            station_id=_text("station_id", raw.get("station_id")),
            model_id=model,
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            residual_c=_finite("residual_c", raw.get("residual_c")),
            target_local_date=_local_date("target_local_date", raw.get("target_local_date")),
            paired_asof_utc=_timestamp("paired_asof_utc", raw.get("paired_asof_utc")),
            qc_status=qc,
        )


@dataclass(frozen=True)
class CurrentAsOfModelRun:
    model_run_id: str
    model_id: str
    station_id: str
    target_local_date: str
    run_init_utc: str
    published_at_utc: str | None
    received_at_utc: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    raw_tmax_c: float
    settlement_definition: str
    source_object_sha256: str
    source_class: str = "native_official"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CurrentAsOfModelRun":
        model = _text("model_id", raw.get("model_id")).upper()
        if model not in MODEL_IDS:
            raise ContractError("model_id_invalid")
        source_class = _text(
            "source_class",
            raw.get("source_class", "native_official"),
        ).lower()
        if source_class not in {
            "native_official",
            "official_native",
            "native_or_official_cloud_replica",
            "aggregator_republisher",
        }:
            raise ContractError("model_run_source_class_invalid")
        published_raw = raw.get("published_at_utc")
        if source_class == "aggregator_republisher":
            published_at = (
                None
                if published_raw in (None, "")
                else _timestamp("published_at_utc", published_raw)
            )
        else:
            published_at = _timestamp("published_at_utc", published_raw)
        return cls(
            model_run_id=_text("model_run_id", raw.get("model_run_id")),
            model_id=model,
            station_id=_text("station_id", raw.get("station_id")),
            target_local_date=_local_date("target_local_date", raw.get("target_local_date")),
            run_init_utc=_timestamp("run_init_utc", raw.get("run_init_utc")),
            published_at_utc=published_at,
            received_at_utc=_timestamp("received_at_utc", raw.get("received_at_utc")),
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            raw_tmax_c=_finite("raw_tmax_c", raw.get("raw_tmax_c")),
            settlement_definition=_text("settlement_definition", raw.get("settlement_definition")),
            source_object_sha256=_sha("source_object_sha256", raw.get("source_object_sha256")),
            source_class=source_class,
        )

    def is_asof(self, decision_at_utc: str) -> bool:
        if not timestamp_lte(self.received_at_utc, decision_at_utc):
            return False
        if self.source_class == "aggregator_republisher":
            return (
                self.published_at_utc is None
                or timestamp_lte(self.published_at_utc, decision_at_utc)
            )
        if self.published_at_utc is None:
            return False
        return strict_asof(
            self.published_at_utc,
            self.received_at_utc,
            decision_at_utc,
        )


@dataclass(frozen=True)
class ModelCalibrationArtifact:
    calibration_artifact_id: str
    station_id: str
    model_id: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    settlement_definition: str
    trained_through_utc: str
    sample_n: int
    effective_sample_n: float
    bias_c: float
    residual_scale_c: float
    tail_parameters: Mapping[str, float]
    valid_from_utc: str
    valid_until_utc: str
    content_sha256: str
    promoted: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelCalibrationArtifact":
        model = _text("model_id", raw.get("model_id")).upper()
        if model not in MODEL_IDS:
            raise ContractError("model_id_invalid")
        sample_n = int(raw.get("sample_n", 0))
        if sample_n < 0:
            raise ContractError("sample_n_invalid")
        value = cls(
            calibration_artifact_id=_text("calibration_artifact_id", raw.get("calibration_artifact_id")),
            station_id=_text("station_id", raw.get("station_id")),
            model_id=model,
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            settlement_definition=_text("settlement_definition", raw.get("settlement_definition")),
            trained_through_utc=_timestamp("trained_through_utc", raw.get("trained_through_utc")),
            sample_n=sample_n,
            effective_sample_n=_nonnegative("effective_sample_n", raw.get("effective_sample_n", sample_n)),
            bias_c=_finite("bias_c", raw.get("bias_c")),
            residual_scale_c=_positive("residual_scale_c", raw.get("residual_scale_c")),
            tail_parameters={str(key): _finite("tail_parameter", item) for key, item in (raw.get("tail_parameters") or {}).items()},
            valid_from_utc=_timestamp("valid_from_utc", raw.get("valid_from_utc")),
            valid_until_utc=_timestamp("valid_until_utc", raw.get("valid_until_utc")),
            content_sha256=_sha("content_sha256", raw.get("content_sha256")),
            promoted=bool(raw.get("promoted", True)),
        )
        if not timestamp_lt(value.valid_from_utc, value.valid_until_utc):
            raise ContractError("calibration_artifact_valid_window_invalid")
        return value


@dataclass(frozen=True)
class ModelEnsembleSelectionEvidence:
    selection_evidence_id: str
    station_id: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    settlement_definition: str
    method_version: str
    train_start_date: str
    train_end_date: str
    recent_start_date: str
    recent_end_date: str
    holdout_start_date: str
    holdout_end_date: str
    candidate_scores: tuple[Mapping[str, Any], ...]
    selected_models: tuple[str, ...]
    selected_weights: Mapping[str, float]
    oos_metrics: Mapping[str, Any]
    residual_covariance: Mapping[str, Mapping[str, float]]
    residual_correlation: Mapping[str, Mapping[str, float]]
    effective_sample_n: float
    previous_ensemble_artifact_sha256: str | None
    max_weight_delta: float
    actual_max_weight_delta: float
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelEnsembleSelectionEvidence":
        candidates_raw = raw.get("candidate_scores")
        if not isinstance(candidates_raw, list) or not candidates_raw or any(
            not isinstance(item, Mapping) for item in candidates_raw
        ):
            raise ContractError("ensemble_selection_candidate_scores_invalid")
        selected_models = tuple(sorted({
            str(item).strip().upper() for item in (raw.get("selected_models") or [])
        }))
        if (
            len(selected_models) < MIN_FORMAL_ENSEMBLE_MODELS
            or any(item not in MODEL_IDS for item in selected_models)
        ):
            raise ContractError("ensemble_selection_models_invalid")
        selected_weights_raw = raw.get("selected_weights")
        if not isinstance(selected_weights_raw, Mapping):
            raise ContractError("ensemble_selection_weights_missing")
        selected_weights = {
            str(model).strip().upper(): _nonnegative("ensemble_selection_weight", weight)
            for model, weight in selected_weights_raw.items()
        }
        if (
            set(selected_weights) != set(selected_models)
            or abs(sum(selected_weights.values()) - 1.0) > 1e-8
            or sum(1 for weight in selected_weights.values() if weight > EPS)
            < MIN_FORMAL_ENSEMBLE_MODELS
        ):
            raise ContractError("ensemble_selection_weights_invalid")
        oos_metrics = raw.get("oos_metrics")
        if not isinstance(oos_metrics, Mapping):
            raise ContractError("ensemble_selection_oos_metrics_missing")
        for metric in ("brier_score", "nll", "crps"):
            _nonnegative(f"ensemble_selection_{metric}", oos_metrics.get(metric))
        covariance = raw.get("residual_covariance")
        correlation = raw.get("residual_correlation")
        if not isinstance(covariance, Mapping) or not isinstance(correlation, Mapping):
            raise ContractError("ensemble_selection_residual_matrix_missing")
        previous_raw = raw.get("previous_ensemble_artifact_sha256")
        previous = None if previous_raw in (None, "") else _sha(
            "previous_ensemble_artifact_sha256", previous_raw
        )
        value = cls(
            selection_evidence_id=_text("selection_evidence_id", raw.get("selection_evidence_id")),
            station_id=_text("station_id", raw.get("station_id")),
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            settlement_definition=_text("settlement_definition", raw.get("settlement_definition")),
            method_version=_text("method_version", raw.get("method_version")),
            train_start_date=_local_date("train_start_date", raw.get("train_start_date")),
            train_end_date=_local_date("train_end_date", raw.get("train_end_date")),
            recent_start_date=_local_date("recent_start_date", raw.get("recent_start_date")),
            recent_end_date=_local_date("recent_end_date", raw.get("recent_end_date")),
            holdout_start_date=_local_date("holdout_start_date", raw.get("holdout_start_date")),
            holdout_end_date=_local_date("holdout_end_date", raw.get("holdout_end_date")),
            candidate_scores=tuple(dict(item) for item in candidates_raw),
            selected_models=selected_models,
            selected_weights=selected_weights,
            oos_metrics=dict(oos_metrics),
            residual_covariance={
                str(model): {str(other): _finite("residual_covariance", item) for other, item in values.items()}
                for model, values in covariance.items()
                if isinstance(values, Mapping)
            },
            residual_correlation={
                str(model): {str(other): _finite("residual_correlation", item) for other, item in values.items()}
                for model, values in correlation.items()
                if isinstance(values, Mapping)
            },
            effective_sample_n=_positive("ensemble_selection_effective_sample_n", raw.get("effective_sample_n")),
            previous_ensemble_artifact_sha256=previous,
            max_weight_delta=_nonnegative("ensemble_selection_max_weight_delta", raw.get("max_weight_delta")),
            actual_max_weight_delta=_nonnegative(
                "ensemble_selection_actual_max_weight_delta", raw.get("actual_max_weight_delta")
            ),
            content_sha256=_sha("content_sha256", raw.get("content_sha256")),
        )
        if not (
            value.train_start_date <= value.recent_start_date
            <= value.recent_end_date <= value.train_end_date
            < value.holdout_start_date <= value.holdout_end_date
        ):
            raise ContractError("ensemble_selection_window_order_invalid")
        if value.method_version != FORMAL_ENSEMBLE_METHOD_VERSION:
            raise ContractError("ensemble_selection_method_version_invalid")
        if value.actual_max_weight_delta > value.max_weight_delta + EPS:
            raise ContractError("ensemble_selection_weight_delta_exceeded")
        identity = {
            "selection_evidence_id": value.selection_evidence_id,
            "station_id": value.station_id,
            "season_bin": value.season_bin,
            "lead_bucket": value.lead_bucket,
            "run_cycle": value.run_cycle,
            "settlement_definition": value.settlement_definition,
            "method_version": value.method_version,
            "train_start_date": value.train_start_date,
            "train_end_date": value.train_end_date,
            "recent_start_date": value.recent_start_date,
            "recent_end_date": value.recent_end_date,
            "holdout_start_date": value.holdout_start_date,
            "holdout_end_date": value.holdout_end_date,
            "candidate_scores": [dict(item) for item in value.candidate_scores],
            "selected_models": list(value.selected_models),
            "selected_weights": dict(value.selected_weights),
            "oos_metrics": dict(value.oos_metrics),
            "residual_covariance": {
                key: dict(item) for key, item in value.residual_covariance.items()
            },
            "residual_correlation": {
                key: dict(item) for key, item in value.residual_correlation.items()
            },
            "effective_sample_n": value.effective_sample_n,
            "previous_ensemble_artifact_sha256": value.previous_ensemble_artifact_sha256,
            "max_weight_delta": value.max_weight_delta,
            "actual_max_weight_delta": value.actual_max_weight_delta,
        }
        if content_sha256(identity) != value.content_sha256:
            raise ContractError("ensemble_selection_content_sha256_mismatch")
        return value


@dataclass(frozen=True)
class ModelEnsembleArtifact:
    ensemble_artifact_id: str
    station_id: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    settlement_definition: str
    candidate_models: tuple[str, ...]
    supported_masks: Mapping[str, Mapping[str, float]]
    residual_covariance: Mapping[str, Mapping[str, float]]
    correlation_penalty: float
    selection_evidence_sha256: str | None
    previous_ensemble_artifact_sha256: str | None
    weight_method: str | None
    effective_sample_n: float | None
    max_weight_delta: float | None
    train_start_date: str | None
    train_end_date: str | None
    recent_start_date: str | None
    recent_end_date: str | None
    holdout_start_date: str | None
    holdout_end_date: str | None
    oos_metrics: Mapping[str, Any]
    valid_from_utc: str
    valid_until_utc: str
    content_sha256: str
    promoted: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelEnsembleArtifact":
        candidates = tuple(sorted({str(item).strip().upper() for item in (raw.get("candidate_models") or [])}))
        if (
            len(candidates) < MIN_FORMAL_ENSEMBLE_MODELS
            or any(item not in MODEL_IDS for item in candidates)
        ):
            raise ContractError("ensemble_candidate_models_invalid")
        masks_raw = raw.get("supported_masks")
        if not isinstance(masks_raw, Mapping) or not masks_raw:
            raise ContractError("ensemble_supported_masks_missing")
        masks: dict[str, dict[str, float]] = {}
        for key, weights_raw in masks_raw.items():
            if not isinstance(weights_raw, Mapping):
                raise ContractError("ensemble_mask_weights_invalid")
            weights = {str(model).strip().upper(): _nonnegative("ensemble_weight", weight) for model, weight in weights_raw.items()}
            if (
                len(weights) < MIN_FORMAL_ENSEMBLE_MODELS
                or abs(sum(weights.values()) - 1.0) > 1e-8
                or sum(1 for weight in weights.values() if weight > EPS)
                < MIN_FORMAL_ENSEMBLE_MODELS
            ):
                raise ContractError("ensemble_mask_weights_not_normalized")
            masks[str(key)] = weights
        selection_sha_raw = raw.get("selection_evidence_sha256")
        previous_sha_raw = raw.get("previous_ensemble_artifact_sha256")
        governed = selection_sha_raw not in (None, "")
        window_names = (
            "train_start_date", "train_end_date", "recent_start_date",
            "recent_end_date", "holdout_start_date", "holdout_end_date",
        )
        windows = {
            name: (_local_date(name, raw.get(name)) if governed else None)
            for name in window_names
        }
        oos_metrics = raw.get("oos_metrics") if isinstance(raw.get("oos_metrics"), Mapping) else {}
        if governed:
            for metric in ("brier_score", "nll", "crps"):
                _nonnegative(f"ensemble_{metric}", oos_metrics.get(metric))
        value = cls(
            ensemble_artifact_id=_text("ensemble_artifact_id", raw.get("ensemble_artifact_id")),
            station_id=_text("station_id", raw.get("station_id")),
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            settlement_definition=_text("settlement_definition", raw.get("settlement_definition")),
            candidate_models=candidates,
            supported_masks=masks,
            residual_covariance=raw.get("residual_covariance") if isinstance(raw.get("residual_covariance"), Mapping) else {},
            correlation_penalty=_nonnegative("correlation_penalty", raw.get("correlation_penalty", 0.0)),
            selection_evidence_sha256=(
                _sha("selection_evidence_sha256", selection_sha_raw) if governed else None
            ),
            previous_ensemble_artifact_sha256=(
                None if previous_sha_raw in (None, "") else _sha(
                    "previous_ensemble_artifact_sha256", previous_sha_raw
                )
            ),
            weight_method=(
                _text("weight_method", raw.get("weight_method")) if governed else None
            ),
            effective_sample_n=(
                _positive("ensemble_effective_sample_n", raw.get("effective_sample_n"))
                if governed else None
            ),
            max_weight_delta=(
                _nonnegative("ensemble_max_weight_delta", raw.get("max_weight_delta"))
                if governed else None
            ),
            train_start_date=windows["train_start_date"],
            train_end_date=windows["train_end_date"],
            recent_start_date=windows["recent_start_date"],
            recent_end_date=windows["recent_end_date"],
            holdout_start_date=windows["holdout_start_date"],
            holdout_end_date=windows["holdout_end_date"],
            oos_metrics=dict(oos_metrics),
            valid_from_utc=_timestamp("valid_from_utc", raw.get("valid_from_utc")),
            valid_until_utc=_timestamp("valid_until_utc", raw.get("valid_until_utc")),
            content_sha256=_sha("content_sha256", raw.get("content_sha256")),
            promoted=bool(raw.get("promoted", True)),
        )
        if not timestamp_lt(value.valid_from_utc, value.valid_until_utc):
            raise ContractError("ensemble_artifact_valid_window_invalid")
        if governed and not (
            value.train_start_date <= value.recent_start_date
            <= value.recent_end_date <= value.train_end_date
            < value.holdout_start_date <= value.holdout_end_date
        ):
            raise ContractError("ensemble_artifact_selection_window_invalid")
        if governed and value.weight_method != FORMAL_ENSEMBLE_METHOD_VERSION:
            raise ContractError("ensemble_artifact_weight_method_invalid")
        for key, weights in value.supported_masks.items():
            members = tuple(sorted(weights))
            if key != model_set_hash(members) or any(model not in value.candidate_models for model in members):
                raise ContractError("ensemble_exact_mask_key_invalid")
        return value

    @property
    def governance_complete(self) -> bool:
        return self.selection_evidence_sha256 is not None

    def resolve_weights(self, available_models: Iterable[str]) -> Mapping[str, float]:
        members = tuple(sorted({str(model).strip().upper() for model in available_models}))
        if len(members) < MIN_FORMAL_ENSEMBLE_MODELS:
            raise ContractError("formal_ensemble_requires_at_least_two_models")
        key = model_set_hash(members)
        weights = self.supported_masks.get(key)
        if weights is None:
            raise ContractError("unsupported_exact_model_mask")
        if set(weights) != set(members):
            raise ContractError("ensemble_mask_weight_membership_mismatch")
        return dict(weights)


@dataclass(frozen=True)
class CalibrationActivation:
    activation_id: str
    artifact_kind: str
    artifact_id: str
    station_id: str
    model_id_or_star: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    settlement_definition: str
    available_model_set_hash_or_star: str
    generation: int
    valid_from_utc: str
    valid_until_utc: str
    activated_at_utc: str
    content_sha256: str
    authority_scope: str = "FORMAL_PROMOTED"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CalibrationActivation":
        kind = _text("artifact_kind", raw.get("artifact_kind")).upper()
        if kind not in {"MODEL_CALIBRATION", "MODEL_ENSEMBLE"}:
            raise ContractError("artifact_kind_invalid")
        generation = int(raw.get("generation", 0))
        if generation < 1:
            raise ContractError("activation_generation_invalid")
        authority_scope = str(raw.get("authority_scope") or "FORMAL_PROMOTED").strip().upper()
        if authority_scope not in {"FORMAL_PROMOTED", "PRODUCTION_NO_LIVE_ONLY"}:
            raise ContractError("calibration_activation_authority_scope_invalid")
        value = cls(
            activation_id=_text("activation_id", raw.get("activation_id")),
            artifact_kind=kind,
            artifact_id=_text("artifact_id", raw.get("artifact_id")),
            station_id=_text("station_id", raw.get("station_id")),
            model_id_or_star=_text("model_id_or_star", raw.get("model_id_or_star")),
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            settlement_definition=_text("settlement_definition", raw.get("settlement_definition")),
            available_model_set_hash_or_star=_text("available_model_set_hash_or_star", raw.get("available_model_set_hash_or_star")),
            generation=generation,
            valid_from_utc=_timestamp("valid_from_utc", raw.get("valid_from_utc")),
            valid_until_utc=_timestamp("valid_until_utc", raw.get("valid_until_utc")),
            activated_at_utc=_timestamp("activated_at_utc", raw.get("activated_at_utc")),
            content_sha256=_sha("content_sha256", raw.get("content_sha256")),
            authority_scope=authority_scope,
        )
        if not timestamp_lt(value.valid_from_utc, value.valid_until_utc):
            raise ContractError("activation_valid_window_invalid")
        if kind == "MODEL_CALIBRATION":
            if value.model_id_or_star not in MODEL_IDS or value.available_model_set_hash_or_star != "*":
                raise ContractError("calibration_activation_scope_invalid")
        elif value.model_id_or_star != "*" or value.available_model_set_hash_or_star == "*":
            raise ContractError("ensemble_activation_scope_invalid")
        return value


@dataclass(frozen=True)
class ProbabilityEnvelope:
    stage: str
    checkpoint_id: str
    station_id: str
    event_date: str
    settlement_timezone: str
    decision_at_utc: str
    fresh_until_utc: str
    bucket_ordinals: Mapping[str, int]
    probabilities: Mapping[str, float]
    posterior_version: str
    posterior_sha256: str
    bridge_sha256: str
    lineage: Mapping[str, str]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProbabilityEnvelope":
        allowed = {
            "schema",
            "stage",
            "checkpoint_id",
            "station_id",
            "event_date",
            "settlement_timezone",
            "decision_at_utc",
            "fresh_until_utc",
            "bucket_ordinals",
            "probabilities",
            "posterior_version",
            "posterior_sha256",
            "bridge_sha256",
            "lineage",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ContractError(
                "probability_envelope_unknown_fields:" + ",".join(sorted(str(item) for item in unknown))
            )
        schema = raw.get("schema")
        if schema is not None and schema != "bayes_basket_probability_envelope_v1":
            raise ContractError("probability_envelope_schema_invalid")
        stage = _text("stage", raw.get("stage"))
        if stage not in STAGES:
            raise ContractError("probability_stage_invalid")
        probabilities_raw = raw.get("probabilities")
        ordinals_raw = raw.get("bucket_ordinals")
        if not isinstance(probabilities_raw, Mapping) or not probabilities_raw:
            raise ContractError("probability_vector_missing")
        if not isinstance(ordinals_raw, Mapping) or set(ordinals_raw) != set(probabilities_raw):
            raise ContractError("probability_bucket_ordinals_incomplete")
        probabilities = {str(bucket): _nonnegative("bucket_probability", probability) for bucket, probability in probabilities_raw.items()}
        if abs(sum(probabilities.values()) - 1.0) > 1e-7:
            raise ContractError("probability_vector_not_normalized")
        ordinals = {str(bucket): int(ordinal) for bucket, ordinal in ordinals_raw.items()}
        if len(set(ordinals.values())) != len(ordinals):
            raise ContractError("probability_bucket_ordinals_duplicate")
        value = cls(
            stage=stage,
            checkpoint_id=_text("checkpoint_id", raw.get("checkpoint_id")),
            station_id=_text("station_id", raw.get("station_id")),
            event_date=_local_date("event_date", raw.get("event_date")),
            settlement_timezone=_text(
                "settlement_timezone",
                raw.get("settlement_timezone"),
            ),
            decision_at_utc=_timestamp("decision_at_utc", raw.get("decision_at_utc")),
            fresh_until_utc=_timestamp("fresh_until_utc", raw.get("fresh_until_utc")),
            bucket_ordinals=ordinals,
            probabilities=probabilities,
            posterior_version=_text("posterior_version", raw.get("posterior_version")),
            posterior_sha256=_sha("posterior_sha256", raw.get("posterior_sha256")),
            bridge_sha256=_sha("bridge_sha256", raw.get("bridge_sha256")),
            lineage={str(key): _text("lineage_value", item) for key, item in (raw.get("lineage") or {}).items()},
        )
        if not timestamp_lt(value.decision_at_utc, value.fresh_until_utc):
            raise ContractError("probability_freshness_window_invalid")
        if not value.lineage:
            raise ContractError("probability_lineage_missing")
        return value

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())

    @property
    def center_bucket(self) -> str:
        return min(self.probabilities, key=lambda bucket: (-self.probabilities[bucket], self.bucket_ordinals[bucket], bucket))

    def direction_skew(self) -> float:
        center = self.bucket_ordinals[self.center_bucket]
        return sum(value for bucket, value in self.probabilities.items() if self.bucket_ordinals[bucket] > center) - sum(
            value for bucket, value in self.probabilities.items() if self.bucket_ordinals[bucket] < center
        )

    def is_fresh(self, at_utc: str) -> bool:
        return timestamp_lte(self.decision_at_utc, at_utc) and timestamp_lt(at_utc, self.fresh_until_utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "bayes_basket_probability_envelope_v1",
            "stage": self.stage,
            "checkpoint_id": self.checkpoint_id,
            "station_id": self.station_id,
            "event_date": self.event_date,
            "settlement_timezone": self.settlement_timezone,
            "decision_at_utc": self.decision_at_utc,
            "fresh_until_utc": self.fresh_until_utc,
            "bucket_ordinals": dict(self.bucket_ordinals),
            "probabilities": dict(self.probabilities),
            "posterior_version": self.posterior_version,
            "posterior_sha256": self.posterior_sha256,
            "bridge_sha256": self.bridge_sha256,
            "lineage": dict(self.lineage),
        }


__all__ = [
    "CALIBRATION_CANDIDATE_MODEL_IDS",
    "CalibrationActivation",
    "ContractError",
    "CurrentAsOfModelRun",
    "EPS",
    "FORMAL_ENSEMBLE_METHOD_VERSION",
    "GLOBAL_FORMAL_MODEL_IDS",
    "MIN_FORMAL_ENSEMBLE_MODELS",
    "MODEL_IDS",
    "ModelCalibrationArtifact",
    "ModelEnsembleArtifact",
    "ModelEnsembleSelectionEvidence",
    "ProbabilityEnvelope",
    "REGIONAL_CALIBRATION_MODEL_IDS",
    "ResidualObservation",
    "STAGES",
    "canonical_json",
    "calibration_candidate_model_set_hash",
    "content_sha256",
    "model_set_hash",
    "strict_asof",
    "timestamp_in_window",
    "timestamp_leq",
    "timestamp_eq",
    "timestamp_lte",
    "timestamp_lt",
]
