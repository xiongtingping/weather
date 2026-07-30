"""Bounded OOF meta-combination of P_model and same-day climatology.

The climate anchor is not multiplied into model probabilities directly.
An activated rolling-origin OOF artifact maps the difference between the
independent climate and model centers to a bounded exponential tilt.  Every
runtime application is returned as a deterministic ``P0HistoricalTiltProof``.

This is a pure-domain module: it performs no I/O, does not materialize P0 and
does not imply that any OOS candidate has been promoted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import ContractError, content_sha256
from .same_day_climatology import SameDayClimatologyArtifact


P0_HISTORICAL_META_SCHEMA = (
    "bayes_basket_p0_historical_meta_combination_artifact_v1"
)
P0_HISTORICAL_PROOF_SCHEMA = "bayes_basket_p0_historical_tilt_proof_v1"
P0_HISTORICAL_META_METHOD = "rolling_origin_oof_bounded_exponential_tilt_v1"
P0_HISTORICAL_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
REVERSAL_POLICIES = ("FORBID_STRONG_REVERSAL", "ALLOW_OOS_PROVEN_REVERSAL")
_EPS = 1e-12


def _strict_fields(
    raw: Mapping[str, Any], *, allowed: set[str], contract: str
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ContractError(
            f"{contract}_unknown_fields:"
            + ",".join(sorted(str(field) for field in unknown))
        )


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


def _range(name: str, value: Any, lower: float, upper: float) -> float:
    result = _finite(name, value)
    if result < lower or result > upper:
        raise ContractError(f"{name}_out_of_range")
    return result


def _positive(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result <= 0.0:
        raise ContractError(f"{name}_must_be_positive")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ContractError(f"{name}_invalid")
    return result


def _iso_date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result


def _timestamp_lt(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) < datetime.fromisoformat(
        right.replace("Z", "+00:00")
    )


def _timestamp_in_window(value: str, start: str, end: str) -> bool:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        datetime.fromisoformat(start.replace("Z", "+00:00"))
        <= parsed
        < datetime.fromisoformat(end.replace("Z", "+00:00"))
    )


def _probabilities(name: str, raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping) or len(raw) < 1:
        raise ContractError(f"{name}_missing")
    result: dict[str, float] = {}
    for bucket, value in raw.items():
        key = _text(f"{name}_bucket", bucket)
        result[key] = _range(f"{name}_{key}", value, 0.0, 1.0)
    if abs(sum(result.values()) - 1.0) > 1e-9:
        raise ContractError(f"{name}_not_normalized")
    return result


def _centers(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping) or len(raw) < 1:
        raise ContractError("bucket_centers_c_missing")
    parsed = {
        _text("bucket", bucket): _finite("bucket_center_c", center)
        for bucket, center in raw.items()
    }
    if len(set(parsed.values())) != len(parsed):
        raise ContractError("bucket_centers_not_strictly_increasing")
    return {
        bucket: center
        for bucket, center in sorted(
            parsed.items(), key=lambda item: (item[1], item[0])
        )
    }


def _metrics(name: str, raw: Any) -> Mapping[str, float]:
    if not isinstance(raw, Mapping):
        raise ContractError(f"{name}_missing")
    _strict_fields(
        raw,
        allowed={"nll", "brier_score", "crps"},
        contract=name,
    )
    if set(raw) != {"nll", "brier_score", "crps"}:
        raise ContractError(f"{name}_incomplete")
    return MappingProxyType(
        {
            metric: _range(f"{name}_{metric}", raw[metric], 0.0, math.inf)
            for metric in ("nll", "brier_score", "crps")
        }
    )


@dataclass(frozen=True)
class P0HistoricalOOFFold:
    fold_id: str
    target_local_date: str
    evaluation_truth_id: str
    model_probability_sha256: str
    model_training_truth_ids: tuple[str, ...]
    climatology_training_truth_ids: tuple[str, ...]
    climatology_artifact_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "P0HistoricalOOFFold":
        if not isinstance(raw, Mapping):
            raise ContractError("p0_historical_oof_fold_invalid")
        _strict_fields(
            raw,
            allowed={
                "fold_id",
                "target_local_date",
                "evaluation_truth_id",
                "model_probability_sha256",
                "model_training_truth_ids",
                "climatology_training_truth_ids",
                "climatology_artifact_sha256",
            },
            contract="p0_historical_oof_fold",
        )
        model_training = tuple(
            sorted(
                {
                    _text("model_training_truth_id", truth_id)
                    for truth_id in raw.get("model_training_truth_ids", ())
                }
            )
        )
        climate_training = tuple(
            sorted(
                {
                    _text("climatology_training_truth_id", truth_id)
                    for truth_id in raw.get("climatology_training_truth_ids", ())
                }
            )
        )
        if not model_training or not climate_training:
            raise ContractError("p0_historical_oof_training_lineage_missing")
        evaluation_truth_id = _text(
            "evaluation_truth_id", raw.get("evaluation_truth_id")
        )
        if evaluation_truth_id in model_training or evaluation_truth_id in climate_training:
            raise ContractError("p0_historical_same_fold_truth_leakage")
        return cls(
            fold_id=_text("fold_id", raw.get("fold_id")),
            target_local_date=_iso_date(
                "target_local_date", raw.get("target_local_date")
            ),
            evaluation_truth_id=evaluation_truth_id,
            model_probability_sha256=_sha(
                "model_probability_sha256", raw.get("model_probability_sha256")
            ),
            model_training_truth_ids=model_training,
            climatology_training_truth_ids=climate_training,
            climatology_artifact_sha256=_sha(
                "climatology_artifact_sha256",
                raw.get("climatology_artifact_sha256"),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "target_local_date": self.target_local_date,
            "evaluation_truth_id": self.evaluation_truth_id,
            "model_probability_sha256": self.model_probability_sha256,
            "model_training_truth_ids": list(self.model_training_truth_ids),
            "climatology_training_truth_ids": list(
                self.climatology_training_truth_ids
            ),
            "climatology_artifact_sha256": self.climatology_artifact_sha256,
        }


@dataclass(frozen=True)
class P0HistoricalMetaCombinationArtifact:
    artifact_id: str
    activation_id: str
    activation_content_sha256: str
    station_id: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    settlement_definition: str
    settlement_timezone: str
    method_version: str
    trained_through_utc: str
    oos_training_evidence_sha256: str
    base_ensemble_artifact_sha256: str
    base_ensemble_selection_evidence_sha256: str
    oof_folds: tuple[P0HistoricalOOFFold, ...]
    sample_n: int
    effective_sample_n: float
    minimum_effective_sample_n: float
    baseline_oos_metrics: Mapping[str, float]
    candidate_oos_metrics: Mapping[str, float]
    stable_oos_gain: bool
    center_delta_to_theta: float
    max_abs_theta: float
    max_center_displacement_buckets: int
    max_kl_divergence: float
    max_total_probability_mass_transfer: float
    max_single_bucket_amplification: float
    strong_direction_threshold: float
    reversal_policy: str
    oos_reversal_validated: bool
    valid_from_utc: str
    valid_until_utc: str
    authority_scope: str
    activated: bool
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "P0HistoricalMetaCombinationArtifact":
        if not isinstance(raw, Mapping):
            raise ContractError("p0_historical_meta_artifact_invalid")
        _strict_fields(
            raw,
            allowed={
                "schema",
                "artifact_id",
                "activation_id",
                "activation_content_sha256",
                "station_id",
                "season_bin",
                "lead_bucket",
                "run_cycle",
                "settlement_definition",
                "settlement_timezone",
                "method_version",
                "trained_through_utc",
                "oos_training_evidence_sha256",
                "base_ensemble_artifact_sha256",
                "base_ensemble_selection_evidence_sha256",
                "oof_folds",
                "sample_n",
                "effective_sample_n",
                "minimum_effective_sample_n",
                "baseline_oos_metrics",
                "candidate_oos_metrics",
                "stable_oos_gain",
                "center_delta_to_theta",
                "max_abs_theta",
                "max_center_displacement_buckets",
                "max_kl_divergence",
                "max_total_probability_mass_transfer",
                "max_single_bucket_amplification",
                "strong_direction_threshold",
                "reversal_policy",
                "oos_reversal_validated",
                "valid_from_utc",
                "valid_until_utc",
                "authority_scope",
                "activated",
                "content_sha256",
            },
            contract="p0_historical_meta_artifact",
        )
        if raw.get("schema") != P0_HISTORICAL_META_SCHEMA:
            raise ContractError("p0_historical_meta_schema_invalid")
        folds_raw = raw.get("oof_folds")
        if not isinstance(folds_raw, (list, tuple)) or not folds_raw:
            raise ContractError("p0_historical_oof_folds_missing")
        folds = tuple(P0HistoricalOOFFold.from_mapping(fold) for fold in folds_raw)
        sample_n = int(raw.get("sample_n", 0))
        displacement = int(raw.get("max_center_displacement_buckets", -1))
        value = cls(
            artifact_id=_text("artifact_id", raw.get("artifact_id")),
            activation_id=_text("activation_id", raw.get("activation_id")),
            activation_content_sha256=_sha(
                "activation_content_sha256", raw.get("activation_content_sha256")
            ),
            station_id=_text("station_id", raw.get("station_id")).upper(),
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            settlement_definition=_text(
                "settlement_definition", raw.get("settlement_definition")
            ),
            settlement_timezone=_text(
                "settlement_timezone", raw.get("settlement_timezone")
            ),
            method_version=_text("method_version", raw.get("method_version")),
            trained_through_utc=_timestamp(
                "trained_through_utc", raw.get("trained_through_utc")
            ),
            oos_training_evidence_sha256=_sha(
                "oos_training_evidence_sha256",
                raw.get("oos_training_evidence_sha256"),
            ),
            base_ensemble_artifact_sha256=_sha(
                "base_ensemble_artifact_sha256",
                raw.get("base_ensemble_artifact_sha256"),
            ),
            base_ensemble_selection_evidence_sha256=_sha(
                "base_ensemble_selection_evidence_sha256",
                raw.get("base_ensemble_selection_evidence_sha256"),
            ),
            oof_folds=folds,
            sample_n=sample_n,
            effective_sample_n=_positive(
                "effective_sample_n", raw.get("effective_sample_n")
            ),
            minimum_effective_sample_n=_positive(
                "minimum_effective_sample_n",
                raw.get("minimum_effective_sample_n"),
            ),
            baseline_oos_metrics=_metrics(
                "baseline_oos_metrics", raw.get("baseline_oos_metrics")
            ),
            candidate_oos_metrics=_metrics(
                "candidate_oos_metrics", raw.get("candidate_oos_metrics")
            ),
            stable_oos_gain=raw.get("stable_oos_gain") is True,
            center_delta_to_theta=_finite(
                "center_delta_to_theta", raw.get("center_delta_to_theta")
            ),
            max_abs_theta=_range(
                "max_abs_theta", raw.get("max_abs_theta"), 0.0, math.inf
            ),
            max_center_displacement_buckets=displacement,
            max_kl_divergence=_range(
                "max_kl_divergence",
                raw.get("max_kl_divergence"),
                0.0,
                math.inf,
            ),
            max_total_probability_mass_transfer=_range(
                "max_total_probability_mass_transfer",
                raw.get("max_total_probability_mass_transfer"),
                0.0,
                1.0,
            ),
            max_single_bucket_amplification=_range(
                "max_single_bucket_amplification",
                raw.get("max_single_bucket_amplification"),
                1.0,
                math.inf,
            ),
            strong_direction_threshold=_range(
                "strong_direction_threshold",
                raw.get("strong_direction_threshold"),
                0.0,
                1.0,
            ),
            reversal_policy=_text(
                "reversal_policy", raw.get("reversal_policy")
            ).upper(),
            oos_reversal_validated=raw.get("oos_reversal_validated") is True,
            valid_from_utc=_timestamp("valid_from_utc", raw.get("valid_from_utc")),
            valid_until_utc=_timestamp(
                "valid_until_utc", raw.get("valid_until_utc")
            ),
            authority_scope=_text(
                "authority_scope", raw.get("authority_scope")
            ).upper(),
            activated=raw.get("activated") is True,
            content_sha256=_sha("content_sha256", raw.get("content_sha256")),
        )
        value._validate()
        return value

    def _validate(self) -> None:
        if self.method_version != P0_HISTORICAL_META_METHOD:
            raise ContractError("p0_historical_meta_method_invalid")
        if self.sample_n != len(self.oof_folds) or self.sample_n < 1:
            raise ContractError("p0_historical_sample_n_invalid")
        if self.max_center_displacement_buckets < 0:
            raise ContractError("p0_historical_max_center_displacement_invalid")
        if self.effective_sample_n > self.sample_n + 1e-9:
            raise ContractError("p0_historical_effective_sample_n_invalid")
        expected_oos_evidence_sha = content_sha256({
            "schema": "bayes_basket_p0_historical_meta_oos_evidence_v1",
            "station_id": self.station_id,
            "season_bin": self.season_bin,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "settlement_definition": self.settlement_definition,
            "settlement_timezone": self.settlement_timezone,
            "trained_through_utc": self.trained_through_utc,
            "base_ensemble_artifact_sha256": (
                self.base_ensemble_artifact_sha256
            ),
            "base_ensemble_selection_evidence_sha256": (
                self.base_ensemble_selection_evidence_sha256
            ),
            "oof_folds": [fold.to_dict() for fold in self.oof_folds],
            "baseline_oos_metrics": dict(self.baseline_oos_metrics),
            "candidate_oos_metrics": dict(self.candidate_oos_metrics),
            "center_delta_to_theta": self.center_delta_to_theta,
            "max_abs_theta": self.max_abs_theta,
        })
        if self.oos_training_evidence_sha256 != expected_oos_evidence_sha:
            raise ContractError(
                "p0_historical_oos_training_evidence_sha256_mismatch"
            )
        if len({fold.fold_id for fold in self.oof_folds}) != len(self.oof_folds):
            raise ContractError("p0_historical_duplicate_fold_id")
        if len({fold.evaluation_truth_id for fold in self.oof_folds}) != len(
            self.oof_folds
        ):
            raise ContractError("p0_historical_duplicate_evaluation_truth")
        target_dates = [fold.target_local_date for fold in self.oof_folds]
        if target_dates != sorted(target_dates):
            raise ContractError("p0_historical_folds_not_rolling_origin_ordered")
        if len(set(target_dates)) != len(target_dates):
            raise ContractError("p0_historical_duplicate_station_day_fold")
        trained_through_date = datetime.fromisoformat(
            self.trained_through_utc.replace("Z", "+00:00")
        ).date()
        if any(date.fromisoformat(day) > trained_through_date for day in target_dates):
            raise ContractError("p0_historical_fold_after_training_window")
        if self.reversal_policy not in REVERSAL_POLICIES:
            raise ContractError("p0_historical_reversal_policy_invalid")
        if (
            self.reversal_policy == "ALLOW_OOS_PROVEN_REVERSAL"
            and not self.oos_reversal_validated
        ):
            raise ContractError("p0_historical_reversal_not_oos_validated")
        if (
            self.authority_scope != P0_HISTORICAL_AUTHORITY_SCOPE
            or not self.activated
        ):
            raise ContractError("p0_historical_meta_not_active_for_no_live")
        if not _timestamp_lt(self.valid_from_utc, self.valid_until_utc):
            raise ContractError("p0_historical_meta_valid_window_invalid")
        gain = _has_stable_metric_gain(
            self.baseline_oos_metrics, self.candidate_oos_metrics
        )
        if self.stable_oos_gain != gain:
            raise ContractError("p0_historical_stable_oos_gain_mismatch")
        if not self.stable_oos_gain and (
            abs(self.center_delta_to_theta) > _EPS
            or abs(self.max_abs_theta) > _EPS
        ):
            raise ContractError("p0_historical_no_gain_requires_theta_zero")
        if (
            self.effective_sample_n < self.minimum_effective_sample_n
            and (
                abs(self.center_delta_to_theta) > _EPS
                or abs(self.max_abs_theta) > _EPS
            )
        ):
            raise ContractError("p0_historical_insufficient_sample_requires_theta_zero")
        if content_sha256(self.to_dict(include_content_sha256=False)) != self.content_sha256:
            raise ContractError("p0_historical_meta_content_sha256_mismatch")
        identity_without_id = self.to_dict(include_content_sha256=False)
        identity_without_id.pop("artifact_id")
        expected_artifact_id = (
            "p0-historical-meta-"
            + content_sha256(identity_without_id)[:32]
        )
        if self.artifact_id != expected_artifact_id:
            raise ContractError("p0_historical_meta_artifact_id_mismatch")

    @property
    def theta_enabled(self) -> bool:
        return (
            self.stable_oos_gain
            and self.effective_sample_n >= self.minimum_effective_sample_n
            and self.max_abs_theta > 0.0
            and abs(self.center_delta_to_theta) > 0.0
        )

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": P0_HISTORICAL_META_SCHEMA,
            "artifact_id": self.artifact_id,
            "activation_id": self.activation_id,
            "activation_content_sha256": self.activation_content_sha256,
            "station_id": self.station_id,
            "season_bin": self.season_bin,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "settlement_definition": self.settlement_definition,
            "settlement_timezone": self.settlement_timezone,
            "method_version": self.method_version,
            "trained_through_utc": self.trained_through_utc,
            "oos_training_evidence_sha256": (
                self.oos_training_evidence_sha256
            ),
            "base_ensemble_artifact_sha256": (
                self.base_ensemble_artifact_sha256
            ),
            "base_ensemble_selection_evidence_sha256": (
                self.base_ensemble_selection_evidence_sha256
            ),
            "oof_folds": [fold.to_dict() for fold in self.oof_folds],
            "sample_n": self.sample_n,
            "effective_sample_n": self.effective_sample_n,
            "minimum_effective_sample_n": self.minimum_effective_sample_n,
            "baseline_oos_metrics": dict(self.baseline_oos_metrics),
            "candidate_oos_metrics": dict(self.candidate_oos_metrics),
            "stable_oos_gain": self.stable_oos_gain,
            "center_delta_to_theta": self.center_delta_to_theta,
            "max_abs_theta": self.max_abs_theta,
            "max_center_displacement_buckets": self.max_center_displacement_buckets,
            "max_kl_divergence": self.max_kl_divergence,
            "max_total_probability_mass_transfer": (
                self.max_total_probability_mass_transfer
            ),
            "max_single_bucket_amplification": (
                self.max_single_bucket_amplification
            ),
            "strong_direction_threshold": self.strong_direction_threshold,
            "reversal_policy": self.reversal_policy,
            "oos_reversal_validated": self.oos_reversal_validated,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "authority_scope": self.authority_scope,
            "activated": self.activated,
        }
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def _has_stable_metric_gain(
    baseline: Mapping[str, float], candidate: Mapping[str, float]
) -> bool:
    non_worse = all(candidate[name] <= baseline[name] + _EPS for name in baseline)
    strict = any(candidate[name] < baseline[name] - _EPS for name in baseline)
    return non_worse and strict


@dataclass(frozen=True)
class P0HistoricalTiltProof:
    proof_id: str
    station_id: str
    target_local_date: str
    settlement_timezone: str
    settlement_definition: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    as_of_utc: str
    model_probability_sha256: str
    ensemble_artifact_sha256: str
    ensemble_selection_evidence_sha256: str
    ensemble_activation_sha256: str
    climatology_artifact_id: str
    climatology_artifact_sha256: str
    climatology_activation_id: str
    climatology_activation_sha256: str
    meta_artifact_id: str
    meta_artifact_sha256: str
    meta_activation_id: str
    meta_activation_sha256: str
    model_center_c: float
    climatology_center_c: float
    historical_model_center_delta_c: float
    applied_theta: float
    theta_zero_fallback: bool
    probabilities_before: Mapping[str, float]
    probabilities_after: Mapping[str, float]
    bucket_centers_c: Mapping[str, float]
    center_bucket_before: str
    center_bucket_after: str
    expected_center_c_before: float
    expected_center_c_after: float
    center_displacement_buckets: int
    direction_before: str
    direction_after: str
    direction_mass_before: float
    direction_mass_after: float
    kl_divergence: float
    total_probability_mass_transfer: float
    max_single_bucket_amplification: float
    center_displacement_limit_buckets: int
    kl_divergence_limit: float
    probability_mass_transfer_limit: float
    single_bucket_amplification_limit: float
    strong_direction_threshold: float
    reversal_policy: str
    center_displacement_margin_buckets: int
    kl_margin: float
    mass_transfer_margin: float
    single_bucket_amplification_margin: float
    reversal_applied: bool
    reversal_oos_validated: bool
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "P0HistoricalTiltProof":
        if not isinstance(raw, Mapping):
            raise ContractError("p0_historical_tilt_proof_invalid")
        allowed = set(_proof_identity_field_names()) | {"schema", "content_sha256"}
        _strict_fields(raw, allowed=allowed, contract="p0_historical_tilt_proof")
        if raw.get("schema") != P0_HISTORICAL_PROOF_SCHEMA:
            raise ContractError("p0_historical_tilt_proof_schema_invalid")
        before = _probabilities(
            "probabilities_before", raw.get("probabilities_before")
        )
        after = _probabilities("probabilities_after", raw.get("probabilities_after"))
        if set(before) != set(after):
            raise ContractError("p0_historical_proof_bucket_support_mismatch")
        centers = _centers(raw.get("bucket_centers_c"))
        if set(centers) != set(before):
            raise ContractError("p0_historical_proof_bucket_mapping_mismatch")
        value = cls(
            proof_id=_text("proof_id", raw.get("proof_id")),
            station_id=_text("station_id", raw.get("station_id")).upper(),
            target_local_date=_iso_date(
                "target_local_date", raw.get("target_local_date")
            ),
            settlement_timezone=_text(
                "settlement_timezone", raw.get("settlement_timezone")
            ),
            settlement_definition=_text(
                "settlement_definition", raw.get("settlement_definition")
            ),
            season_bin=_text("season_bin", raw.get("season_bin")),
            lead_bucket=_text("lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("run_cycle", raw.get("run_cycle")),
            as_of_utc=_timestamp("as_of_utc", raw.get("as_of_utc")),
            model_probability_sha256=_sha(
                "model_probability_sha256", raw.get("model_probability_sha256")
            ),
            ensemble_artifact_sha256=_sha(
                "ensemble_artifact_sha256", raw.get("ensemble_artifact_sha256")
            ),
            ensemble_selection_evidence_sha256=_sha(
                "ensemble_selection_evidence_sha256",
                raw.get("ensemble_selection_evidence_sha256"),
            ),
            ensemble_activation_sha256=_sha(
                "ensemble_activation_sha256", raw.get("ensemble_activation_sha256")
            ),
            climatology_artifact_id=_text(
                "climatology_artifact_id", raw.get("climatology_artifact_id")
            ),
            climatology_artifact_sha256=_sha(
                "climatology_artifact_sha256",
                raw.get("climatology_artifact_sha256"),
            ),
            climatology_activation_id=_text(
                "climatology_activation_id", raw.get("climatology_activation_id")
            ),
            climatology_activation_sha256=_sha(
                "climatology_activation_sha256",
                raw.get("climatology_activation_sha256"),
            ),
            meta_artifact_id=_text("meta_artifact_id", raw.get("meta_artifact_id")),
            meta_artifact_sha256=_sha(
                "meta_artifact_sha256", raw.get("meta_artifact_sha256")
            ),
            meta_activation_id=_text(
                "meta_activation_id", raw.get("meta_activation_id")
            ),
            meta_activation_sha256=_sha(
                "meta_activation_sha256", raw.get("meta_activation_sha256")
            ),
            model_center_c=_finite("model_center_c", raw.get("model_center_c")),
            climatology_center_c=_finite(
                "climatology_center_c", raw.get("climatology_center_c")
            ),
            historical_model_center_delta_c=_finite(
                "historical_model_center_delta_c",
                raw.get("historical_model_center_delta_c"),
            ),
            applied_theta=_finite("applied_theta", raw.get("applied_theta")),
            theta_zero_fallback=raw.get("theta_zero_fallback") is True,
            probabilities_before=MappingProxyType(before),
            probabilities_after=MappingProxyType(after),
            bucket_centers_c=MappingProxyType(centers),
            center_bucket_before=_text(
                "center_bucket_before", raw.get("center_bucket_before")
            ),
            center_bucket_after=_text(
                "center_bucket_after", raw.get("center_bucket_after")
            ),
            expected_center_c_before=_finite(
                "expected_center_c_before", raw.get("expected_center_c_before")
            ),
            expected_center_c_after=_finite(
                "expected_center_c_after", raw.get("expected_center_c_after")
            ),
            center_displacement_buckets=int(
                raw.get("center_displacement_buckets", -1)
            ),
            direction_before=_text(
                "direction_before", raw.get("direction_before")
            ).upper(),
            direction_after=_text(
                "direction_after", raw.get("direction_after")
            ).upper(),
            direction_mass_before=_finite(
                "direction_mass_before", raw.get("direction_mass_before")
            ),
            direction_mass_after=_finite(
                "direction_mass_after", raw.get("direction_mass_after")
            ),
            kl_divergence=_range(
                "kl_divergence", raw.get("kl_divergence"), 0.0, math.inf
            ),
            total_probability_mass_transfer=_range(
                "total_probability_mass_transfer",
                raw.get("total_probability_mass_transfer"),
                0.0,
                1.0,
            ),
            max_single_bucket_amplification=_range(
                "max_single_bucket_amplification",
                raw.get("max_single_bucket_amplification"),
                1.0,
                math.inf,
            ),
            center_displacement_limit_buckets=int(
                raw.get("center_displacement_limit_buckets", -1)
            ),
            kl_divergence_limit=_range(
                "kl_divergence_limit",
                raw.get("kl_divergence_limit"),
                0.0,
                math.inf,
            ),
            probability_mass_transfer_limit=_range(
                "probability_mass_transfer_limit",
                raw.get("probability_mass_transfer_limit"),
                0.0,
                1.0,
            ),
            single_bucket_amplification_limit=_range(
                "single_bucket_amplification_limit",
                raw.get("single_bucket_amplification_limit"),
                1.0,
                math.inf,
            ),
            strong_direction_threshold=_range(
                "strong_direction_threshold",
                raw.get("strong_direction_threshold"),
                0.0,
                1.0,
            ),
            reversal_policy=_text(
                "reversal_policy", raw.get("reversal_policy")
            ).upper(),
            center_displacement_margin_buckets=int(
                raw.get("center_displacement_margin_buckets", -1)
            ),
            kl_margin=_finite("kl_margin", raw.get("kl_margin")),
            mass_transfer_margin=_finite(
                "mass_transfer_margin", raw.get("mass_transfer_margin")
            ),
            single_bucket_amplification_margin=_finite(
                "single_bucket_amplification_margin",
                raw.get("single_bucket_amplification_margin"),
            ),
            reversal_applied=raw.get("reversal_applied") is True,
            reversal_oos_validated=raw.get("reversal_oos_validated") is True,
            content_sha256=_sha("content_sha256", raw.get("content_sha256")),
        )
        value._validate()
        return value

    def _validate(self) -> None:
        if self.center_displacement_buckets < 0:
            raise ContractError("p0_historical_center_displacement_invalid")
        if self.center_displacement_margin_buckets < 0:
            raise ContractError("p0_historical_center_displacement_limit_exceeded")
        if (
            self.kl_margin < -1e-10
            or self.mass_transfer_margin < -1e-10
            or self.single_bucket_amplification_margin < -1e-10
        ):
            raise ContractError("p0_historical_tilt_limit_exceeded")
        if self.direction_before not in {"UP", "DOWN", "NEUTRAL"} or self.direction_after not in {
            "UP",
            "DOWN",
            "NEUTRAL",
        }:
            raise ContractError("p0_historical_direction_invalid")
        if self.reversal_policy not in REVERSAL_POLICIES:
            raise ContractError("p0_historical_reversal_policy_invalid")
        if self.center_displacement_limit_buckets < 0:
            raise ContractError("p0_historical_center_displacement_limit_invalid")
        center_before = _mode_bucket(
            self.probabilities_before, self.bucket_centers_c
        )
        center_after = _mode_bucket(
            self.probabilities_after, self.bucket_centers_c
        )
        ordinals = {
            bucket: index for index, bucket in enumerate(self.bucket_centers_c)
        }
        displacement = abs(ordinals[center_after] - ordinals[center_before])
        expected_before = sum(
            self.bucket_centers_c[bucket] * probability
            for bucket, probability in self.probabilities_before.items()
        )
        expected_after = sum(
            self.bucket_centers_c[bucket] * probability
            for bucket, probability in self.probabilities_after.items()
        )
        direction_before, mass_before = _direction(
            self.probabilities_before,
            self.bucket_centers_c,
            threshold=self.strong_direction_threshold,
        )
        direction_after, mass_after = _direction(
            self.probabilities_after,
            self.bucket_centers_c,
            threshold=self.strong_direction_threshold,
        )
        if any(
            self.probabilities_before[bucket] <= 0.0
            and self.probabilities_after[bucket] > 0.0
            for bucket in self.probabilities_after
        ):
            raise ContractError("p0_historical_zero_probability_bucket_revived")
        kl = sum(
            self.probabilities_after[bucket]
            * math.log(
                self.probabilities_after[bucket]
                / self.probabilities_before[bucket]
            )
            for bucket in self.probabilities_after
            if self.probabilities_after[bucket] > 0.0
        )
        mass_transfer = 0.5 * sum(
            abs(
                self.probabilities_after[bucket]
                - self.probabilities_before[bucket]
            )
            for bucket in self.probabilities_after
        )
        amplification = max(
            (
                self.probabilities_after[bucket]
                / self.probabilities_before[bucket]
                if self.probabilities_before[bucket] > 0.0
                else 1.0
            )
            for bucket in self.probabilities_after
        )
        derived_values = (
            (self.model_center_c, expected_before, "model_center"),
            (
                self.historical_model_center_delta_c,
                self.climatology_center_c - expected_before,
                "center_delta",
            ),
            (self.expected_center_c_before, expected_before, "expected_center_before"),
            (self.expected_center_c_after, expected_after, "expected_center_after"),
            (self.direction_mass_before, mass_before, "direction_mass_before"),
            (self.direction_mass_after, mass_after, "direction_mass_after"),
            (self.kl_divergence, kl, "kl"),
            (
                self.total_probability_mass_transfer,
                mass_transfer,
                "mass_transfer",
            ),
            (
                self.max_single_bucket_amplification,
                amplification,
                "single_bucket_amplification",
            ),
        )
        for supplied, expected, name in derived_values:
            if abs(supplied - expected) > 1e-9:
                raise ContractError(f"p0_historical_proof_{name}_mismatch")
        if (
            self.center_bucket_before != center_before
            or self.center_bucket_after != center_after
            or self.center_displacement_buckets != displacement
            or self.direction_before != direction_before
            or self.direction_after != direction_after
        ):
            raise ContractError("p0_historical_proof_center_or_direction_mismatch")
        expected_reversal = {direction_before, direction_after} == {"UP", "DOWN"}
        if self.reversal_applied != expected_reversal:
            raise ContractError("p0_historical_proof_reversal_mismatch")
        if self.center_displacement_margin_buckets != (
            self.center_displacement_limit_buckets - displacement
        ):
            raise ContractError("p0_historical_proof_center_margin_mismatch")
        margin_values = (
            (self.kl_margin, self.kl_divergence_limit - kl, "kl"),
            (
                self.mass_transfer_margin,
                self.probability_mass_transfer_limit - mass_transfer,
                "mass_transfer",
            ),
            (
                self.single_bucket_amplification_margin,
                self.single_bucket_amplification_limit - amplification,
                "single_bucket_amplification",
            ),
        )
        for supplied, expected, name in margin_values:
            if abs(supplied - expected) > 1e-9:
                raise ContractError(f"p0_historical_proof_{name}_margin_mismatch")
        if self.reversal_applied and not self.reversal_oos_validated:
            raise ContractError("p0_historical_unproven_direction_reversal")
        if self.theta_zero_fallback:
            if abs(self.applied_theta) > _EPS or any(
                abs(self.probabilities_before[bucket] - self.probabilities_after[bucket])
                > 1e-12
                for bucket in self.probabilities_before
            ):
                raise ContractError("p0_historical_theta_zero_fallback_changed_probability")
        if content_sha256(self.to_dict(include_content_sha256=False)) != self.content_sha256:
            raise ContractError("p0_historical_tilt_proof_content_sha256_mismatch")
        identity_without_id = self.to_dict(include_content_sha256=False)
        identity_without_id.pop("proof_id")
        expected_proof_id = (
            "p0-historical-proof-"
            + content_sha256(identity_without_id)[:32]
        )
        if self.proof_id != expected_proof_id:
            raise ContractError("p0_historical_tilt_proof_id_mismatch")

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {"schema": P0_HISTORICAL_PROOF_SCHEMA}
        for field in _proof_identity_field_names():
            value = getattr(self, field)
            if isinstance(value, Mapping):
                value = dict(value)
            result[field] = value
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def _proof_identity_field_names() -> tuple[str, ...]:
    return (
        "proof_id",
        "station_id",
        "target_local_date",
        "settlement_timezone",
        "settlement_definition",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "as_of_utc",
        "model_probability_sha256",
        "ensemble_artifact_sha256",
        "ensemble_selection_evidence_sha256",
        "ensemble_activation_sha256",
        "climatology_artifact_id",
        "climatology_artifact_sha256",
        "climatology_activation_id",
        "climatology_activation_sha256",
        "meta_artifact_id",
        "meta_artifact_sha256",
        "meta_activation_id",
        "meta_activation_sha256",
        "model_center_c",
        "climatology_center_c",
        "historical_model_center_delta_c",
        "applied_theta",
        "theta_zero_fallback",
        "probabilities_before",
        "probabilities_after",
        "bucket_centers_c",
        "center_bucket_before",
        "center_bucket_after",
        "expected_center_c_before",
        "expected_center_c_after",
        "center_displacement_buckets",
        "direction_before",
        "direction_after",
        "direction_mass_before",
        "direction_mass_after",
        "kl_divergence",
        "total_probability_mass_transfer",
        "max_single_bucket_amplification",
        "center_displacement_limit_buckets",
        "kl_divergence_limit",
        "probability_mass_transfer_limit",
        "single_bucket_amplification_limit",
        "strong_direction_threshold",
        "reversal_policy",
        "center_displacement_margin_buckets",
        "kl_margin",
        "mass_transfer_margin",
        "single_bucket_amplification_margin",
        "reversal_applied",
        "reversal_oos_validated",
    )


def build_p0_historical_meta_combination_artifact(
    *,
    activation_id: str,
    activation_content_sha256: str,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    settlement_timezone: str,
    trained_through_utc: str,
    base_ensemble_artifact_sha256: str,
    base_ensemble_selection_evidence_sha256: str,
    oof_folds: Iterable[Mapping[str, Any]],
    effective_sample_n: float,
    minimum_effective_sample_n: float,
    baseline_oos_metrics: Mapping[str, float],
    candidate_oos_metrics: Mapping[str, float],
    center_delta_to_theta: float,
    max_abs_theta: float,
    max_center_displacement_buckets: int,
    max_kl_divergence: float,
    max_total_probability_mass_transfer: float,
    max_single_bucket_amplification: float,
    strong_direction_threshold: float,
    valid_from_utc: str,
    valid_until_utc: str,
    reversal_policy: str = "FORBID_STRONG_REVERSAL",
    oos_reversal_validated: bool = False,
    authority_scope: str = P0_HISTORICAL_AUTHORITY_SCOPE,
    activated: bool = True,
) -> P0HistoricalMetaCombinationArtifact:
    """Build an immutable OOF meta-combination policy artifact."""

    folds = tuple(
        sorted(
            (P0HistoricalOOFFold.from_mapping(fold) for fold in oof_folds),
            key=lambda fold: (fold.target_local_date, fold.fold_id),
        )
    )
    if not folds:
        raise ContractError("p0_historical_oof_folds_missing")
    baseline = _metrics("baseline_oos_metrics", baseline_oos_metrics)
    candidate = _metrics("candidate_oos_metrics", candidate_oos_metrics)
    stable_gain = _has_stable_metric_gain(baseline, candidate)
    effective_n = _positive("effective_sample_n", effective_sample_n)
    minimum_n = _positive("minimum_effective_sample_n", minimum_effective_sample_n)
    coefficient = _finite("center_delta_to_theta", center_delta_to_theta)
    theta_limit = _range("max_abs_theta", max_abs_theta, 0.0, math.inf)
    if not stable_gain or effective_n < minimum_n:
        coefficient = 0.0
        theta_limit = 0.0
    oos_training_evidence_sha256 = content_sha256({
        "schema": "bayes_basket_p0_historical_meta_oos_evidence_v1",
        "station_id": _text("station_id", station_id).upper(),
        "season_bin": _text("season_bin", season_bin),
        "lead_bucket": _text("lead_bucket", lead_bucket),
        "run_cycle": _text("run_cycle", run_cycle),
        "settlement_definition": _text(
            "settlement_definition", settlement_definition
        ),
        "settlement_timezone": _text(
            "settlement_timezone", settlement_timezone
        ),
        "trained_through_utc": _timestamp(
            "trained_through_utc", trained_through_utc
        ),
        "base_ensemble_artifact_sha256": _sha(
            "base_ensemble_artifact_sha256",
            base_ensemble_artifact_sha256,
        ),
        "base_ensemble_selection_evidence_sha256": _sha(
            "base_ensemble_selection_evidence_sha256",
            base_ensemble_selection_evidence_sha256,
        ),
        "oof_folds": [fold.to_dict() for fold in folds],
        "baseline_oos_metrics": dict(baseline),
        "candidate_oos_metrics": dict(candidate),
        "center_delta_to_theta": coefficient,
        "max_abs_theta": theta_limit,
    })
    base = {
        "schema": P0_HISTORICAL_META_SCHEMA,
        "activation_id": _text("activation_id", activation_id),
        "activation_content_sha256": _sha(
            "activation_content_sha256", activation_content_sha256
        ),
        "station_id": _text("station_id", station_id).upper(),
        "season_bin": _text("season_bin", season_bin),
        "lead_bucket": _text("lead_bucket", lead_bucket),
        "run_cycle": _text("run_cycle", run_cycle),
        "settlement_definition": _text(
            "settlement_definition", settlement_definition
        ),
        "settlement_timezone": _text("settlement_timezone", settlement_timezone),
        "method_version": P0_HISTORICAL_META_METHOD,
        "trained_through_utc": _timestamp(
            "trained_through_utc", trained_through_utc
        ),
        "oos_training_evidence_sha256": oos_training_evidence_sha256,
        "base_ensemble_artifact_sha256": _sha(
            "base_ensemble_artifact_sha256",
            base_ensemble_artifact_sha256,
        ),
        "base_ensemble_selection_evidence_sha256": _sha(
            "base_ensemble_selection_evidence_sha256",
            base_ensemble_selection_evidence_sha256,
        ),
        "oof_folds": [fold.to_dict() for fold in folds],
        "sample_n": len(folds),
        "effective_sample_n": effective_n,
        "minimum_effective_sample_n": minimum_n,
        "baseline_oos_metrics": dict(baseline),
        "candidate_oos_metrics": dict(candidate),
        "stable_oos_gain": stable_gain,
        "center_delta_to_theta": coefficient,
        "max_abs_theta": theta_limit,
        "max_center_displacement_buckets": int(max_center_displacement_buckets),
        "max_kl_divergence": _range(
            "max_kl_divergence", max_kl_divergence, 0.0, math.inf
        ),
        "max_total_probability_mass_transfer": _range(
            "max_total_probability_mass_transfer",
            max_total_probability_mass_transfer,
            0.0,
            1.0,
        ),
        "max_single_bucket_amplification": _range(
            "max_single_bucket_amplification",
            max_single_bucket_amplification,
            1.0,
            math.inf,
        ),
        "strong_direction_threshold": _range(
            "strong_direction_threshold", strong_direction_threshold, 0.0, 1.0
        ),
        "reversal_policy": _text("reversal_policy", reversal_policy).upper(),
        "oos_reversal_validated": oos_reversal_validated is True,
        "valid_from_utc": _timestamp("valid_from_utc", valid_from_utc),
        "valid_until_utc": _timestamp("valid_until_utc", valid_until_utc),
        "authority_scope": _text("authority_scope", authority_scope).upper(),
        "activated": activated is True,
    }
    artifact_id = "p0-historical-meta-" + content_sha256(base)[:32]
    identity = {**base, "artifact_id": artifact_id}
    identity["content_sha256"] = content_sha256(identity)
    return P0HistoricalMetaCombinationArtifact.from_mapping(identity)


def _mode_bucket(
    probabilities: Mapping[str, float], centers: Mapping[str, float]
) -> str:
    return min(
        probabilities,
        key=lambda bucket: (-probabilities[bucket], centers[bucket], bucket),
    )


def _direction(
    probabilities: Mapping[str, float],
    centers: Mapping[str, float],
    *,
    threshold: float,
) -> tuple[str, float]:
    center_bucket = _mode_bucket(probabilities, centers)
    center = centers[center_bucket]
    high = sum(
        probability
        for bucket, probability in probabilities.items()
        if centers[bucket] > center
    )
    low = sum(
        probability
        for bucket, probability in probabilities.items()
        if centers[bucket] < center
    )
    mass = high - low
    if mass > threshold:
        return "UP", mass
    if mass < -threshold:
        return "DOWN", mass
    return "NEUTRAL", mass


def _apply_tilt(
    probabilities: Mapping[str, float],
    centers: Mapping[str, float],
    *,
    theta: float,
    model_center: float,
) -> dict[str, float]:
    if abs(theta) <= _EPS:
        return dict(probabilities)
    scale = max(max(centers.values()) - min(centers.values()), _EPS)
    log_weights: dict[str, float] = {}
    maximum = -math.inf
    for bucket, probability in probabilities.items():
        if probability <= 0.0:
            log_weight = -math.inf
        else:
            z_score = (centers[bucket] - model_center) / scale
            log_weight = math.log(probability) + theta * z_score
        log_weights[bucket] = log_weight
        maximum = max(maximum, log_weight)
    weights = {
        bucket: 0.0 if value == -math.inf else math.exp(value - maximum)
        for bucket, value in log_weights.items()
    }
    total = sum(weights.values())
    if total <= 0.0:
        raise ContractError("p0_historical_tilt_zero_total")
    return {bucket: value / total for bucket, value in weights.items()}


def compile_p0_historical_tilt_proof(
    *,
    artifact: P0HistoricalMetaCombinationArtifact | Mapping[str, Any],
    climatology: SameDayClimatologyArtifact | Mapping[str, Any],
    station_id: str,
    target_local_date: str,
    settlement_timezone: str,
    settlement_definition: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    as_of_utc: str,
    model_probabilities: Mapping[str, float],
    bucket_centers_c: Mapping[str, float],
    model_probability_sha256: str,
    ensemble_artifact_sha256: str,
    ensemble_selection_evidence_sha256: str,
    ensemble_activation_sha256: str,
    climatology_activation_sha256: str,
    meta_activation_id: str,
    meta_activation_sha256: str,
) -> P0HistoricalTiltProof:
    """Compile the only admissible historical same-day input to P0.

    The computation is root-based and stateless.  Replaying an earlier input
    therefore reproduces the exact earlier proof instead of accumulating a
    previous tilt.
    """

    meta = (
        artifact
        if isinstance(artifact, P0HistoricalMetaCombinationArtifact)
        else P0HistoricalMetaCombinationArtifact.from_mapping(artifact)
    )
    climate = (
        climatology
        if isinstance(climatology, SameDayClimatologyArtifact)
        else SameDayClimatologyArtifact.from_mapping(climatology)
    )
    station = _text("station_id", station_id).upper()
    target_date = _iso_date("target_local_date", target_local_date)
    timezone = _text("settlement_timezone", settlement_timezone)
    definition = _text("settlement_definition", settlement_definition)
    as_of = _timestamp("as_of_utc", as_of_utc)
    if station != meta.station_id or station != climate.station_id:
        raise ContractError("p0_historical_station_scope_mismatch")
    if (
        timezone != meta.settlement_timezone
        or timezone != climate.settlement_timezone
    ):
        raise ContractError("p0_historical_timezone_scope_mismatch")
    if (
        definition != meta.settlement_definition
        or definition != climate.settlement_definition
    ):
        raise ContractError("p0_historical_settlement_scope_mismatch")
    if (
        _text("season_bin", season_bin) != meta.season_bin
        or _text("lead_bucket", lead_bucket) != meta.lead_bucket
        or _text("run_cycle", run_cycle) != meta.run_cycle
    ):
        raise ContractError("p0_historical_model_scope_mismatch")
    parsed_target = date.fromisoformat(target_date)
    if climate.target_year != parsed_target.year:
        raise ContractError("p0_historical_climatology_target_year_mismatch")
    if climate.calendar_day != parsed_target.strftime("%m-%d"):
        raise ContractError("p0_historical_climatology_calendar_day_mismatch")
    if not _timestamp_in_window(as_of, meta.valid_from_utc, meta.valid_until_utc):
        raise ContractError("p0_historical_meta_activation_not_valid_asof")
    if not _timestamp_in_window(
        as_of, climate.valid_from_utc, climate.valid_until_utc
    ):
        raise ContractError("p0_historical_climatology_activation_not_valid_asof")
    probabilities = _probabilities("model_probabilities", model_probabilities)
    centers = _centers(bucket_centers_c)
    if (
        set(probabilities) != set(centers)
        or set(probabilities) != set(climate.bucket_probabilities)
        or any(
            abs(centers[bucket] - climate.bucket_centers_c[bucket]) > 1e-9
            for bucket in centers
        )
    ):
        raise ContractError("p0_historical_bucket_mapping_mismatch")
    expected_model_sha = content_sha256(
        {
            "bucket_centers_c": centers,
            "probabilities": probabilities,
        }
    )
    supplied_model_sha = _sha(
        "model_probability_sha256", model_probability_sha256
    )
    if supplied_model_sha != expected_model_sha:
        raise ContractError("p0_historical_model_probability_sha256_mismatch")
    supplied_ensemble_artifact_sha = _sha(
        "ensemble_artifact_sha256", ensemble_artifact_sha256
    )
    if supplied_ensemble_artifact_sha != meta.base_ensemble_artifact_sha256:
        raise ContractError(
            "p0_historical_base_ensemble_artifact_sha256_mismatch"
        )
    supplied_selection_evidence_sha = _sha(
        "ensemble_selection_evidence_sha256",
        ensemble_selection_evidence_sha256,
    )
    if (
        supplied_selection_evidence_sha
        != meta.base_ensemble_selection_evidence_sha256
    ):
        raise ContractError(
            "p0_historical_base_ensemble_selection_evidence_sha256_mismatch"
        )
    supplied_meta_activation_id = _text(
        "meta_activation_id", meta_activation_id
    )
    if supplied_meta_activation_id != meta.activation_id:
        raise ContractError("p0_historical_meta_activation_id_mismatch")
    supplied_meta_activation_sha = _sha(
        "meta_activation_sha256", meta_activation_sha256
    )
    model_center = sum(
        centers[bucket] * probability
        for bucket, probability in probabilities.items()
    )
    delta = climate.center_c - model_center
    if meta.theta_enabled:
        theta = max(
            -meta.max_abs_theta,
            min(meta.max_abs_theta, meta.center_delta_to_theta * delta),
        )
    else:
        theta = 0.0
    posterior = _apply_tilt(
        probabilities, centers, theta=theta, model_center=model_center
    )
    center_before = _mode_bucket(probabilities, centers)
    center_after = _mode_bucket(posterior, centers)
    ordinals = {bucket: index for index, bucket in enumerate(centers)}
    displacement = abs(ordinals[center_after] - ordinals[center_before])
    expected_after = sum(
        centers[bucket] * probability for bucket, probability in posterior.items()
    )
    kl = sum(
        posterior[bucket] * math.log(posterior[bucket] / probabilities[bucket])
        for bucket in posterior
        if posterior[bucket] > 0.0
    )
    mass_transfer = 0.5 * sum(
        abs(posterior[bucket] - probabilities[bucket]) for bucket in posterior
    )
    amplification = max(
        (
            posterior[bucket] / probabilities[bucket]
            if probabilities[bucket] > 0.0
            else 1.0
        )
        for bucket in posterior
    )
    direction_before, direction_mass_before = _direction(
        probabilities, centers, threshold=meta.strong_direction_threshold
    )
    direction_after, direction_mass_after = _direction(
        posterior, centers, threshold=meta.strong_direction_threshold
    )
    reversal = {direction_before, direction_after} == {"UP", "DOWN"}
    reversal_allowed = (
        meta.reversal_policy == "ALLOW_OOS_PROVEN_REVERSAL"
        and meta.oos_reversal_validated
    )
    if reversal and not reversal_allowed:
        raise ContractError("p0_historical_unproven_direction_reversal")
    if displacement > meta.max_center_displacement_buckets:
        raise ContractError("p0_historical_center_displacement_limit_exceeded")
    if kl > meta.max_kl_divergence + 1e-10:
        raise ContractError("p0_historical_kl_limit_exceeded")
    if mass_transfer > meta.max_total_probability_mass_transfer + 1e-10:
        raise ContractError("p0_historical_mass_transfer_limit_exceeded")
    if amplification > meta.max_single_bucket_amplification + 1e-10:
        raise ContractError("p0_historical_single_bucket_amplification_limit_exceeded")
    identity = {
        "schema": P0_HISTORICAL_PROOF_SCHEMA,
        "station_id": station,
        "target_local_date": target_date,
        "settlement_timezone": timezone,
        "settlement_definition": definition,
        "season_bin": meta.season_bin,
        "lead_bucket": meta.lead_bucket,
        "run_cycle": meta.run_cycle,
        "as_of_utc": as_of,
        "model_probability_sha256": supplied_model_sha,
        "ensemble_artifact_sha256": supplied_ensemble_artifact_sha,
        "ensemble_selection_evidence_sha256": (
            supplied_selection_evidence_sha
        ),
        "ensemble_activation_sha256": _sha(
            "ensemble_activation_sha256", ensemble_activation_sha256
        ),
        "climatology_artifact_id": climate.artifact_id,
        "climatology_artifact_sha256": climate.content_sha256,
        "climatology_activation_id": climate.activation_id,
        "climatology_activation_sha256": _sha(
            "climatology_activation_sha256", climatology_activation_sha256
        ),
        "meta_artifact_id": meta.artifact_id,
        "meta_artifact_sha256": meta.content_sha256,
        "meta_activation_id": supplied_meta_activation_id,
        "meta_activation_sha256": supplied_meta_activation_sha,
        "model_center_c": model_center,
        "climatology_center_c": climate.center_c,
        "historical_model_center_delta_c": delta,
        "applied_theta": theta,
        "theta_zero_fallback": abs(theta) <= _EPS,
        "probabilities_before": probabilities,
        "probabilities_after": posterior,
        "bucket_centers_c": centers,
        "center_bucket_before": center_before,
        "center_bucket_after": center_after,
        "expected_center_c_before": model_center,
        "expected_center_c_after": expected_after,
        "center_displacement_buckets": displacement,
        "direction_before": direction_before,
        "direction_after": direction_after,
        "direction_mass_before": direction_mass_before,
        "direction_mass_after": direction_mass_after,
        "kl_divergence": kl,
        "total_probability_mass_transfer": mass_transfer,
        "max_single_bucket_amplification": amplification,
        "center_displacement_limit_buckets": (
            meta.max_center_displacement_buckets
        ),
        "kl_divergence_limit": meta.max_kl_divergence,
        "probability_mass_transfer_limit": (
            meta.max_total_probability_mass_transfer
        ),
        "single_bucket_amplification_limit": (
            meta.max_single_bucket_amplification
        ),
        "strong_direction_threshold": meta.strong_direction_threshold,
        "reversal_policy": meta.reversal_policy,
        "center_displacement_margin_buckets": (
            meta.max_center_displacement_buckets - displacement
        ),
        "kl_margin": meta.max_kl_divergence - kl,
        "mass_transfer_margin": (
            meta.max_total_probability_mass_transfer - mass_transfer
        ),
        "single_bucket_amplification_margin": (
            meta.max_single_bucket_amplification - amplification
        ),
        "reversal_applied": reversal,
        "reversal_oos_validated": reversal_allowed,
    }
    proof_id = "p0-historical-proof-" + content_sha256(identity)[:32]
    identity["proof_id"] = proof_id
    identity["content_sha256"] = content_sha256(identity)
    return P0HistoricalTiltProof.from_mapping(identity)


def compile_p0_historical_zero_tilt_proof(
    *,
    station_id: str,
    target_local_date: str,
    settlement_timezone: str,
    settlement_definition: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    as_of_utc: str,
    model_probabilities: Mapping[str, float],
    bucket_centers_c: Mapping[str, float],
    ensemble_artifact_sha256: str,
    ensemble_selection_evidence_sha256: str,
    ensemble_activation_sha256: str,
) -> P0HistoricalTiltProof:
    """Seal the deterministic no-artifact/no-OOS fallback as an explicit proof."""

    probabilities = _probabilities(
        "model_probabilities", model_probabilities
    )
    centers = _centers(bucket_centers_c)
    if set(probabilities) != set(centers):
        raise ContractError("p0_historical_bucket_mapping_mismatch")
    model_probability_sha = content_sha256({
        "bucket_centers_c": centers,
        "probabilities": probabilities,
    })
    center = _mode_bucket(probabilities, centers)
    expected = sum(
        centers[bucket] * probability
        for bucket, probability in probabilities.items()
    )
    direction, direction_mass = _direction(
        probabilities,
        centers,
        threshold=0.0,
    )
    identity = {
        "schema": P0_HISTORICAL_PROOF_SCHEMA,
        "station_id": _text("station_id", station_id).upper(),
        "target_local_date": _iso_date(
            "target_local_date", target_local_date
        ),
        "settlement_timezone": _text(
            "settlement_timezone", settlement_timezone
        ),
        "settlement_definition": _text(
            "settlement_definition", settlement_definition
        ),
        "season_bin": _text("season_bin", season_bin),
        "lead_bucket": _text("lead_bucket", lead_bucket),
        "run_cycle": _text("run_cycle", run_cycle),
        "as_of_utc": _timestamp("as_of_utc", as_of_utc),
        "model_probability_sha256": model_probability_sha,
        "ensemble_artifact_sha256": _sha(
            "ensemble_artifact_sha256", ensemble_artifact_sha256
        ),
        "ensemble_selection_evidence_sha256": _sha(
            "ensemble_selection_evidence_sha256",
            ensemble_selection_evidence_sha256,
        ),
        "ensemble_activation_sha256": _sha(
            "ensemble_activation_sha256", ensemble_activation_sha256
        ),
        "climatology_artifact_id": "NONE",
        "climatology_artifact_sha256": "0" * 64,
        "climatology_activation_id": "NONE",
        "climatology_activation_sha256": "0" * 64,
        "meta_artifact_id": "NONE",
        "meta_artifact_sha256": "0" * 64,
        "meta_activation_id": "NONE",
        "meta_activation_sha256": "0" * 64,
        "model_center_c": expected,
        "climatology_center_c": expected,
        "historical_model_center_delta_c": 0.0,
        "applied_theta": 0.0,
        "theta_zero_fallback": True,
        "probabilities_before": probabilities,
        "probabilities_after": probabilities,
        "bucket_centers_c": centers,
        "center_bucket_before": center,
        "center_bucket_after": center,
        "expected_center_c_before": expected,
        "expected_center_c_after": expected,
        "center_displacement_buckets": 0,
        "direction_before": direction,
        "direction_after": direction,
        "direction_mass_before": float(direction_mass),
        "direction_mass_after": float(direction_mass),
        "kl_divergence": 0.0,
        "total_probability_mass_transfer": 0.0,
        "max_single_bucket_amplification": 1.0,
        "center_displacement_limit_buckets": 0,
        "kl_divergence_limit": 0.0,
        "probability_mass_transfer_limit": 0.0,
        "single_bucket_amplification_limit": 1.0,
        "strong_direction_threshold": 0.0,
        "reversal_policy": "FORBID_STRONG_REVERSAL",
        "center_displacement_margin_buckets": 0,
        "kl_margin": 0.0,
        "mass_transfer_margin": 0.0,
        "single_bucket_amplification_margin": 0.0,
        "reversal_applied": False,
        "reversal_oos_validated": False,
    }
    proof_id = "p0-historical-proof-" + content_sha256(identity)[:32]
    identity["proof_id"] = proof_id
    identity["content_sha256"] = content_sha256(identity)
    return P0HistoricalTiltProof.from_mapping(identity)


validate_p0_historical_meta_combination_artifact = (
    P0HistoricalMetaCombinationArtifact.from_mapping
)
validate_p0_historical_tilt_proof = P0HistoricalTiltProof.from_mapping


__all__ = [
    "P0_HISTORICAL_AUTHORITY_SCOPE",
    "P0_HISTORICAL_META_METHOD",
    "P0_HISTORICAL_META_SCHEMA",
    "P0_HISTORICAL_PROOF_SCHEMA",
    "P0HistoricalMetaCombinationArtifact",
    "P0HistoricalOOFFold",
    "P0HistoricalTiltProof",
    "REVERSAL_POLICIES",
    "build_p0_historical_meta_combination_artifact",
    "compile_p0_historical_tilt_proof",
    "compile_p0_historical_zero_tilt_proof",
    "validate_p0_historical_meta_combination_artifact",
    "validate_p0_historical_tilt_proof",
]
