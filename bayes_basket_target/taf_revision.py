"""OOS-governed TAF confirmation and bounded-correction compiler.

TAF is forecast evidence, never a hard fact.  This module is the only target
path allowed to turn decoded TAF fields into a P0-M likelihood-ratio proof.
It performs no I/O and never authorizes an order.
"""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    timestamp_in_window,
    timestamp_lt,
)
from .event_contracts import ForecastRevision
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
    validate_cross_native_ensemble_weights,
)


TAF_ARTIFACT_SCHEMA = "bayes_basket_taf_revision_artifact_v2"
TAF_PROOF_SCHEMA = "bayes_basket_taf_revision_proof_v1"
TAF_METHOD_VERSION = "oos_taf_confirmation_bounded_correction_v2"
TAF_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
TAF_CORRELATION_METHOD = "holdout_taf_vs_nwp_combination_residual_pearson_v1"
TAF_NWP_CROSSFIT_ARTIFACT_KIND = (
    "strict_oos_crossfit_prediction_artifact"
)
TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION = (
    "taf_nwp_crossfit_to_live_probability_stack_transport_v1"
)
TAF_NWP_LIVE_FINGERPRINT_SCHEMA = (
    "bayes_basket_taf_nwp_live_compatibility_fingerprint_v1"
)
_PHYSICAL_FEATURES = (
    "cloud_cover_fraction",
    "precipitation_probability",
    "wind_speed_normalized",
)


def taf_scope_lead_bucket(scope: Mapping[str, Any]) -> str:
    """The lead vocabulary this chain joins a TAF artifact on.

    Two different questions have shared the name "lead".  ``lead_bucket`` is
    the calendar label -- how many days before the event day a decision was
    made -- and station policy, preselection and the operator-facing scope are
    keyed by it.  ``training_lead_bucket`` is the group the P0 weights were
    actually fitted in, measured in hours to the station's own local day, and
    it is what makes two predictions comparable: the calendar groups overlap
    badly enough that d1 and d2 both contain runs issued 18-31h ahead.

    A TAF revision is fitted from a series of P0 predictions, so its scope is
    the grouping that made those predictions homogeneous -- the training one.
    Roots compiled before that field existed carry only the calendar label, and
    for them it remains the answer, so an artifact fitted under the old
    vocabulary keeps matching exactly the roots it always did.
    """

    if not isinstance(scope, Mapping):
        return ""
    training = str(scope.get("training_lead_bucket") or "").strip()
    return training or str(scope.get("lead_bucket") or "").strip()


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


def _strict_fields(
    raw: Mapping[str, Any],
    *,
    allowed: set[str],
    contract: str,
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ContractError(
            f"{contract}_unknown_fields:"
            + ",".join(sorted(str(item) for item in unknown))
        )


@dataclass(frozen=True)
class TafRevisionArtifact:
    artifact_id: str
    activation_id: str
    station_id: str
    season_bin: str
    lead_bucket: str
    run_cycle: str
    settlement_definition: str
    settlement_timezone: str
    method_version: str
    trained_through_utc: str
    sample_n: int
    effective_sample_n: float
    oos_metrics: Mapping[str, float]
    tx_bias_c: float
    taf_nwp_delta_coefficient: float
    tx_residual_scale_c: float
    physical_feature_effect_c: Mapping[str, float]
    wind_speed_cap_mps: float
    confirmation_tolerance_c: float
    max_confirmation_strength: float
    max_correction_strength: float
    max_probability_mass_transfer: float
    max_center_displacement_buckets: int
    likelihood_ratio_floor: float
    likelihood_ratio_ceiling: float
    nwp_combination_artifact_sha256: str
    nwp_combination_artifact_kind: str | None
    nwp_live_compatibility_contract_version: str | None
    nwp_live_compatibility_fingerprint_sha256: str | None
    nwp_live_compatibility_evidence_sha256: str | None
    nwp_live_compatibility_validated: bool
    oos_taf_nwp_residual_correlation: float
    correlation_discount_method: str
    correlation_discount: float
    joint_physical_oos_validated: bool
    valid_from_utc: str
    valid_until_utc: str
    authority_scope: str
    activated: bool
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TafRevisionArtifact":
        allowed = {
            "schema", "artifact_id", "activation_id", "station_id",
            "season_bin", "lead_bucket", "run_cycle", "settlement_definition",
            "settlement_timezone", "method_version", "trained_through_utc",
            "sample_n", "effective_sample_n", "oos_metrics", "tx_bias_c",
            "taf_nwp_delta_coefficient",
            "tx_residual_scale_c", "physical_feature_effect_c",
            "wind_speed_cap_mps", "confirmation_tolerance_c",
            "max_confirmation_strength", "max_correction_strength",
            "max_probability_mass_transfer", "max_center_displacement_buckets",
            "likelihood_ratio_floor", "likelihood_ratio_ceiling",
            "nwp_combination_artifact_sha256",
            "nwp_combination_artifact_kind",
            "nwp_live_compatibility_contract_version",
            "nwp_live_compatibility_fingerprint_sha256",
            "nwp_live_compatibility_evidence_sha256",
            "nwp_live_compatibility_validated",
            "oos_taf_nwp_residual_correlation",
            "correlation_discount_method", "correlation_discount",
            "joint_physical_oos_validated",
            "valid_from_utc", "valid_until_utc",
            "authority_scope", "activated", "content_sha256",
        }
        _strict_fields(raw, allowed=allowed, contract="taf_revision_artifact")
        if raw.get("schema") not in {None, TAF_ARTIFACT_SCHEMA}:
            raise ContractError("taf_revision_artifact_schema_invalid")
        effects_raw = raw.get("physical_feature_effect_c")
        if not isinstance(effects_raw, Mapping) or set(effects_raw) != set(_PHYSICAL_FEATURES):
            raise ContractError("taf_physical_feature_effects_incomplete")
        metrics_raw = raw.get("oos_metrics")
        if not isinstance(metrics_raw, Mapping):
            raise ContractError("taf_oos_metrics_missing")
        metrics = {
            name: _range(f"taf_oos_{name}", metrics_raw.get(name), 0.0, math.inf)
            for name in ("brier_score", "nll", "crps")
        }
        sample_n = int(raw.get("sample_n", 0))
        if sample_n < 1:
            raise ContractError("taf_sample_n_invalid")
        displacement = int(raw.get("max_center_displacement_buckets", -1))
        if displacement < 0:
            raise ContractError("taf_max_center_displacement_invalid")
        bridge_fields = {
            "nwp_combination_artifact_kind",
            "nwp_live_compatibility_contract_version",
            "nwp_live_compatibility_fingerprint_sha256",
            "nwp_live_compatibility_evidence_sha256",
            "nwp_live_compatibility_validated",
        }
        bridge_present = {
            key for key in bridge_fields if key in raw
        }
        if bridge_present and bridge_present != bridge_fields:
            raise ContractError("taf_nwp_live_compatibility_bridge_incomplete")
        value = cls(
            artifact_id=_text("taf_artifact_id", raw.get("artifact_id")),
            activation_id=_text("taf_activation_id", raw.get("activation_id")),
            station_id=_text("taf_station_id", raw.get("station_id")).upper(),
            season_bin=_text("taf_season_bin", raw.get("season_bin")),
            lead_bucket=_text("taf_lead_bucket", raw.get("lead_bucket")),
            run_cycle=_text("taf_run_cycle", raw.get("run_cycle")),
            settlement_definition=_text(
                "taf_settlement_definition", raw.get("settlement_definition")
            ),
            settlement_timezone=_text(
                "taf_settlement_timezone", raw.get("settlement_timezone")
            ),
            method_version=_text("taf_method_version", raw.get("method_version")),
            trained_through_utc=_timestamp(
                "taf_trained_through_utc", raw.get("trained_through_utc")
            ),
            sample_n=sample_n,
            effective_sample_n=_positive(
                "taf_effective_sample_n", raw.get("effective_sample_n")
            ),
            oos_metrics=metrics,
            tx_bias_c=_finite("taf_tx_bias_c", raw.get("tx_bias_c")),
            taf_nwp_delta_coefficient=_finite(
                "taf_nwp_delta_coefficient",
                raw.get("taf_nwp_delta_coefficient"),
            ),
            tx_residual_scale_c=_positive(
                "taf_tx_residual_scale_c", raw.get("tx_residual_scale_c")
            ),
            physical_feature_effect_c={
                name: _finite(f"taf_{name}_effect_c", effects_raw[name])
                for name in _PHYSICAL_FEATURES
            },
            wind_speed_cap_mps=_positive(
                "taf_wind_speed_cap_mps", raw.get("wind_speed_cap_mps")
            ),
            confirmation_tolerance_c=_range(
                "taf_confirmation_tolerance_c",
                raw.get("confirmation_tolerance_c"),
                0.0,
                math.inf,
            ),
            max_confirmation_strength=_range(
                "taf_max_confirmation_strength",
                raw.get("max_confirmation_strength"),
                0.0,
                1.0,
            ),
            max_correction_strength=_range(
                "taf_max_correction_strength",
                raw.get("max_correction_strength"),
                0.0,
                1.0,
            ),
            max_probability_mass_transfer=_range(
                "taf_max_probability_mass_transfer",
                raw.get("max_probability_mass_transfer"),
                0.0,
                1.0,
            ),
            max_center_displacement_buckets=displacement,
            likelihood_ratio_floor=_positive(
                "taf_likelihood_ratio_floor", raw.get("likelihood_ratio_floor")
            ),
            likelihood_ratio_ceiling=_positive(
                "taf_likelihood_ratio_ceiling", raw.get("likelihood_ratio_ceiling")
            ),
            nwp_combination_artifact_sha256=_sha(
                "taf_nwp_combination_artifact_sha256",
                raw.get("nwp_combination_artifact_sha256"),
            ),
            nwp_combination_artifact_kind=(
                _text(
                    "taf_nwp_combination_artifact_kind",
                    raw.get("nwp_combination_artifact_kind"),
                )
                if bridge_present else None
            ),
            nwp_live_compatibility_contract_version=(
                _text(
                    "taf_nwp_live_compatibility_contract_version",
                    raw.get("nwp_live_compatibility_contract_version"),
                )
                if bridge_present else None
            ),
            nwp_live_compatibility_fingerprint_sha256=(
                _sha(
                    "taf_nwp_live_compatibility_fingerprint_sha256",
                    raw.get("nwp_live_compatibility_fingerprint_sha256"),
                )
                if bridge_present else None
            ),
            nwp_live_compatibility_evidence_sha256=(
                _sha(
                    "taf_nwp_live_compatibility_evidence_sha256",
                    raw.get("nwp_live_compatibility_evidence_sha256"),
                )
                if bridge_present else None
            ),
            nwp_live_compatibility_validated=(
                raw.get("nwp_live_compatibility_validated") is True
                if bridge_present else False
            ),
            oos_taf_nwp_residual_correlation=_range(
                "taf_oos_taf_nwp_residual_correlation",
                raw.get("oos_taf_nwp_residual_correlation"),
                -1.0,
                1.0,
            ),
            correlation_discount_method=_text(
                "taf_correlation_discount_method",
                raw.get("correlation_discount_method"),
            ),
            correlation_discount=_range(
                "taf_correlation_discount", raw.get("correlation_discount"), 0.0, 1.0
            ),
            joint_physical_oos_validated=(
                raw.get("joint_physical_oos_validated") is True
            ),
            valid_from_utc=_timestamp("taf_valid_from_utc", raw.get("valid_from_utc")),
            valid_until_utc=_timestamp(
                "taf_valid_until_utc", raw.get("valid_until_utc")
            ),
            authority_scope=_text(
                "taf_authority_scope", raw.get("authority_scope")
            ).upper(),
            activated=bool(raw.get("activated", False)),
            content_sha256=_sha("taf_content_sha256", raw.get("content_sha256")),
        )
        if value.method_version != TAF_METHOD_VERSION:
            raise ContractError("taf_method_version_invalid")
        if value.correlation_discount_method != TAF_CORRELATION_METHOD:
            raise ContractError("taf_correlation_discount_method_invalid")
        if not value.joint_physical_oos_validated:
            raise ContractError("taf_joint_physical_oos_not_validated")
        if bridge_present and (
            value.nwp_combination_artifact_kind
            != TAF_NWP_CROSSFIT_ARTIFACT_KIND
            or value.nwp_live_compatibility_contract_version
            != TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
            or not value.nwp_live_compatibility_validated
        ):
            raise ContractError("taf_nwp_live_compatibility_bridge_invalid")
        if value.authority_scope != TAF_AUTHORITY_SCOPE or not value.activated:
            raise ContractError("taf_artifact_not_active_for_production_no_live")
        if not timestamp_lt(value.valid_from_utc, value.valid_until_utc):
            raise ContractError("taf_artifact_valid_window_invalid")
        if value.effective_sample_n > float(value.sample_n) + 1e-9:
            raise ContractError("taf_effective_sample_n_invalid")
        if not (
            value.likelihood_ratio_floor <= 1.0
            <= value.likelihood_ratio_ceiling
        ):
            raise ContractError("taf_likelihood_ratio_bounds_invalid")
        identity = value.to_dict(include_content_sha256=False)
        if content_sha256(identity) != value.content_sha256:
            raise ContractError("taf_artifact_content_sha256_mismatch")
        return value

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": TAF_ARTIFACT_SCHEMA,
            "artifact_id": self.artifact_id,
            "activation_id": self.activation_id,
            "station_id": self.station_id,
            "season_bin": self.season_bin,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "settlement_definition": self.settlement_definition,
            "settlement_timezone": self.settlement_timezone,
            "method_version": self.method_version,
            "trained_through_utc": self.trained_through_utc,
            "sample_n": self.sample_n,
            "effective_sample_n": self.effective_sample_n,
            "oos_metrics": dict(self.oos_metrics),
            "tx_bias_c": self.tx_bias_c,
            "taf_nwp_delta_coefficient": self.taf_nwp_delta_coefficient,
            "tx_residual_scale_c": self.tx_residual_scale_c,
            "physical_feature_effect_c": dict(self.physical_feature_effect_c),
            "wind_speed_cap_mps": self.wind_speed_cap_mps,
            "confirmation_tolerance_c": self.confirmation_tolerance_c,
            "max_confirmation_strength": self.max_confirmation_strength,
            "max_correction_strength": self.max_correction_strength,
            "max_probability_mass_transfer": self.max_probability_mass_transfer,
            "max_center_displacement_buckets": self.max_center_displacement_buckets,
            "likelihood_ratio_floor": self.likelihood_ratio_floor,
            "likelihood_ratio_ceiling": self.likelihood_ratio_ceiling,
            "nwp_combination_artifact_sha256": (
                self.nwp_combination_artifact_sha256
            ),
            "oos_taf_nwp_residual_correlation": (
                self.oos_taf_nwp_residual_correlation
            ),
            "correlation_discount_method": self.correlation_discount_method,
            "correlation_discount": self.correlation_discount,
            "joint_physical_oos_validated": (
                self.joint_physical_oos_validated
            ),
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "authority_scope": self.authority_scope,
            "activated": self.activated,
        }
        if self.nwp_live_compatibility_contract_version is not None:
            result.update({
                "nwp_combination_artifact_kind": (
                    self.nwp_combination_artifact_kind
                ),
                "nwp_live_compatibility_contract_version": (
                    self.nwp_live_compatibility_contract_version
                ),
                "nwp_live_compatibility_fingerprint_sha256": (
                    self.nwp_live_compatibility_fingerprint_sha256
                ),
                "nwp_live_compatibility_evidence_sha256": (
                    self.nwp_live_compatibility_evidence_sha256
                ),
                "nwp_live_compatibility_validated": (
                    self.nwp_live_compatibility_validated
                ),
            })
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


@dataclass(frozen=True)
class TafRevisionProof:
    proof_id: str
    mode: str
    forecast_revision_id: str
    forecast_source_object_sha256: str
    artifact_id: str
    artifact_content_sha256: str
    activation_id: str
    nwp_crossfit_prediction_artifact_sha256: str
    nwp_live_compatibility_contract_version: str
    nwp_live_compatibility_fingerprint_sha256: str
    nwp_live_compatibility_evidence_sha256: str
    base_p0_checkpoint_id: str
    base_p0_content_sha256: str
    predecessor_checkpoint_id: str
    predecessor_content_sha256: str
    settlement_timezone: str
    likelihood_ratio_by_bucket: Mapping[str, float]
    posterior_probabilities: Mapping[str, float]
    probability_mass_transfer: float
    center_before: str
    center_after: str
    center_displacement_buckets: int
    taf_tx_c: float
    physical_features: Mapping[str, float]
    physical_features_sha256: str
    tx_calibrated_center_c: float
    physically_confirmed_center_c: float
    physical_adjustment_c: float
    physical_confirmation_state: str
    physical_consistency_factor: float
    applied_strength: float
    hard_excluded_buckets: tuple[str, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TafRevisionProof":
        allowed = {
            "schema", "proof_id", "mode", "forecast_revision_id",
            "forecast_source_object_sha256", "artifact_id",
            "artifact_content_sha256", "activation_id",
            "nwp_crossfit_prediction_artifact_sha256",
            "nwp_live_compatibility_contract_version",
            "nwp_live_compatibility_fingerprint_sha256",
            "nwp_live_compatibility_evidence_sha256",
            "base_p0_checkpoint_id", "base_p0_content_sha256",
            "predecessor_checkpoint_id", "predecessor_content_sha256",
            "settlement_timezone", "likelihood_ratio_by_bucket",
            "posterior_probabilities", "probability_mass_transfer",
            "center_before", "center_after", "center_displacement_buckets",
            "taf_tx_c", "physical_features", "physical_features_sha256",
            "tx_calibrated_center_c", "physically_confirmed_center_c",
            "physical_adjustment_c", "physical_confirmation_state",
            "physical_consistency_factor", "applied_strength",
            "hard_excluded_buckets", "content_sha256",
        }
        _strict_fields(raw, allowed=allowed, contract="taf_revision_proof")
        if raw.get("schema") not in {None, TAF_PROOF_SCHEMA}:
            raise ContractError("taf_revision_proof_schema_invalid")
        mode = _text("taf_revision_mode", raw.get("mode")).upper()
        if mode not in {"CONFIRMATION", "CORRECTION"}:
            raise ContractError("taf_revision_mode_invalid")
        ratios_raw = raw.get("likelihood_ratio_by_bucket")
        posterior_raw = raw.get("posterior_probabilities")
        if not isinstance(ratios_raw, Mapping) or not isinstance(posterior_raw, Mapping):
            raise ContractError("taf_revision_probability_vectors_missing")
        excluded = tuple(str(item) for item in (raw.get("hard_excluded_buckets") or ()))
        if excluded:
            raise ContractError("taf_revision_hard_exclusion_forbidden")
        physical_features_raw = raw.get("physical_features")
        if not isinstance(physical_features_raw, Mapping):
            raise ContractError("taf_physical_features_missing")
        _strict_fields(
            physical_features_raw,
            allowed=set(_PHYSICAL_FEATURES),
            contract="taf_physical_features",
        )
        if set(physical_features_raw) != set(_PHYSICAL_FEATURES):
            raise ContractError("taf_physical_features_incomplete")
        physical_features = {
            name: _range(
                f"taf_{name}",
                physical_features_raw[name],
                0.0,
                1.0,
            )
            for name in _PHYSICAL_FEATURES
        }
        value = cls(
            proof_id=_text("taf_proof_id", raw.get("proof_id")),
            mode=mode,
            forecast_revision_id=_text(
                "taf_forecast_revision_id", raw.get("forecast_revision_id")
            ),
            forecast_source_object_sha256=_sha(
                "taf_forecast_source_object_sha256",
                raw.get("forecast_source_object_sha256"),
            ),
            artifact_id=_text("taf_artifact_id", raw.get("artifact_id")),
            artifact_content_sha256=_sha(
                "taf_artifact_content_sha256", raw.get("artifact_content_sha256")
            ),
            activation_id=_text("taf_activation_id", raw.get("activation_id")),
            nwp_crossfit_prediction_artifact_sha256=_sha(
                "taf_nwp_crossfit_prediction_artifact_sha256",
                raw.get("nwp_crossfit_prediction_artifact_sha256"),
            ),
            nwp_live_compatibility_contract_version=_text(
                "taf_nwp_live_compatibility_contract_version",
                raw.get("nwp_live_compatibility_contract_version"),
            ),
            nwp_live_compatibility_fingerprint_sha256=_sha(
                "taf_nwp_live_compatibility_fingerprint_sha256",
                raw.get("nwp_live_compatibility_fingerprint_sha256"),
            ),
            nwp_live_compatibility_evidence_sha256=_sha(
                "taf_nwp_live_compatibility_evidence_sha256",
                raw.get("nwp_live_compatibility_evidence_sha256"),
            ),
            base_p0_checkpoint_id=_text(
                "taf_base_p0_checkpoint_id", raw.get("base_p0_checkpoint_id")
            ),
            base_p0_content_sha256=_sha(
                "taf_base_p0_content_sha256", raw.get("base_p0_content_sha256")
            ),
            predecessor_checkpoint_id=_text(
                "taf_predecessor_checkpoint_id", raw.get("predecessor_checkpoint_id")
            ),
            predecessor_content_sha256=_sha(
                "taf_predecessor_content_sha256",
                raw.get("predecessor_content_sha256"),
            ),
            settlement_timezone=_text(
                "taf_settlement_timezone", raw.get("settlement_timezone")
            ),
            likelihood_ratio_by_bucket={
                str(bucket): _positive("taf_likelihood_ratio", ratio)
                for bucket, ratio in ratios_raw.items()
            },
            posterior_probabilities={
                str(bucket): _range("taf_posterior_probability", probability, 0.0, 1.0)
                for bucket, probability in posterior_raw.items()
            },
            probability_mass_transfer=_range(
                "taf_probability_mass_transfer",
                raw.get("probability_mass_transfer"),
                0.0,
                1.0,
            ),
            center_before=_text("taf_center_before", raw.get("center_before")),
            center_after=_text("taf_center_after", raw.get("center_after")),
            center_displacement_buckets=int(raw.get("center_displacement_buckets", -1)),
            taf_tx_c=_finite("taf_tx_c", raw.get("taf_tx_c")),
            physical_features=physical_features,
            physical_features_sha256=_sha(
                "taf_physical_features_sha256",
                raw.get("physical_features_sha256"),
            ),
            tx_calibrated_center_c=_finite(
                "taf_tx_calibrated_center_c",
                raw.get("tx_calibrated_center_c"),
            ),
            physically_confirmed_center_c=_finite(
                "taf_physically_confirmed_center_c",
                raw.get("physically_confirmed_center_c"),
            ),
            physical_adjustment_c=_finite(
                "taf_physical_adjustment_c", raw.get("physical_adjustment_c")
            ),
            physical_confirmation_state=_text(
                "taf_physical_confirmation_state",
                raw.get("physical_confirmation_state"),
            ).upper(),
            physical_consistency_factor=_range(
                "taf_physical_consistency_factor",
                raw.get("physical_consistency_factor"),
                0.0,
                1.0,
            ),
            applied_strength=_range(
                "taf_applied_strength", raw.get("applied_strength"), 0.0, 1.0
            ),
            hard_excluded_buckets=excluded,
            content_sha256=_sha("taf_proof_content_sha256", raw.get("content_sha256")),
        )
        if value.center_displacement_buckets < 0:
            raise ContractError("taf_center_displacement_invalid")
        if (
            value.nwp_live_compatibility_contract_version
            != TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ):
            raise ContractError(
                "taf_nwp_live_compatibility_contract_version_invalid"
            )
        if (
            content_sha256(dict(value.physical_features))
            != value.physical_features_sha256
        ):
            raise ContractError("taf_physical_features_sha256_mismatch")
        if value.physical_confirmation_state not in {
            "CONFIRMS", "CONFLICTS", "NEUTRAL"
        }:
            raise ContractError("taf_physical_confirmation_state_invalid")
        if (
            value.physical_confirmation_state == "CONFLICTS"
            and value.physical_consistency_factor >= 1.0
        ):
            raise ContractError("taf_physical_conflict_not_discounted")
        if set(value.likelihood_ratio_by_bucket) != set(value.posterior_probabilities):
            raise ContractError("taf_revision_bucket_support_mismatch")
        if abs(sum(value.posterior_probabilities.values()) - 1.0) > 1e-7:
            raise ContractError("taf_revision_posterior_not_normalized")
        if content_sha256(value.to_dict(include_content_sha256=False)) != value.content_sha256:
            raise ContractError("taf_revision_proof_content_sha256_mismatch")
        return value

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": TAF_PROOF_SCHEMA,
            "proof_id": self.proof_id,
            "mode": self.mode,
            "forecast_revision_id": self.forecast_revision_id,
            "forecast_source_object_sha256": self.forecast_source_object_sha256,
            "artifact_id": self.artifact_id,
            "artifact_content_sha256": self.artifact_content_sha256,
            "activation_id": self.activation_id,
            "nwp_crossfit_prediction_artifact_sha256": (
                self.nwp_crossfit_prediction_artifact_sha256
            ),
            "nwp_live_compatibility_contract_version": (
                self.nwp_live_compatibility_contract_version
            ),
            "nwp_live_compatibility_fingerprint_sha256": (
                self.nwp_live_compatibility_fingerprint_sha256
            ),
            "nwp_live_compatibility_evidence_sha256": (
                self.nwp_live_compatibility_evidence_sha256
            ),
            "base_p0_checkpoint_id": self.base_p0_checkpoint_id,
            "base_p0_content_sha256": self.base_p0_content_sha256,
            "predecessor_checkpoint_id": self.predecessor_checkpoint_id,
            "predecessor_content_sha256": self.predecessor_content_sha256,
            "settlement_timezone": self.settlement_timezone,
            "likelihood_ratio_by_bucket": dict(self.likelihood_ratio_by_bucket),
            "posterior_probabilities": dict(self.posterior_probabilities),
            "probability_mass_transfer": self.probability_mass_transfer,
            "center_before": self.center_before,
            "center_after": self.center_after,
            "center_displacement_buckets": self.center_displacement_buckets,
            "taf_tx_c": self.taf_tx_c,
            "physical_features": dict(self.physical_features),
            "physical_features_sha256": self.physical_features_sha256,
            "tx_calibrated_center_c": self.tx_calibrated_center_c,
            "physically_confirmed_center_c": self.physically_confirmed_center_c,
            "physical_adjustment_c": self.physical_adjustment_c,
            "physical_confirmation_state": self.physical_confirmation_state,
            "physical_consistency_factor": self.physical_consistency_factor,
            "applied_strength": self.applied_strength,
            "hard_excluded_buckets": list(self.hard_excluded_buckets),
        }
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def _normal_weights(
    centers: Mapping[str, float],
    *,
    mean: float,
    scale: float,
) -> dict[str, float]:
    log_weights = {
        bucket: -0.5 * ((center - mean) / scale) ** 2
        for bucket, center in centers.items()
    }
    maximum = max(log_weights.values())
    weights = {
        bucket: math.exp(max(-700.0, value - maximum))
        for bucket, value in log_weights.items()
    }
    total = sum(weights.values())
    return {bucket: value / total for bucket, value in weights.items()}


def _apply_tempered_ratios(
    base: Mapping[str, float],
    taf: Mapping[str, float],
    *,
    strength: float,
    floor: float,
    ceiling: float,
) -> tuple[dict[str, float], dict[str, float]]:
    ratios: dict[str, float] = {}
    unnormalized: dict[str, float] = {}
    for bucket, prior in base.items():
        if prior <= 0.0:
            ratio = 1.0
        else:
            raw_ratio = max(floor, min(ceiling, taf[bucket] / prior))
            ratio = math.exp(strength * math.log(raw_ratio))
            ratio = max(floor, min(ceiling, ratio))
        ratios[bucket] = ratio
        unnormalized[bucket] = prior * ratio
    total = sum(unnormalized.values())
    posterior = {bucket: value / total for bucket, value in unnormalized.items()}
    return ratios, posterior


def _validate_live_nwp_compatibility_binding(
    *,
    root: ProbabilityEnvelope,
    artifact: TafRevisionArtifact,
) -> str:
    """Bind the TAF crossfit source to the exact live P0 combination.

    ``artifact.nwp_combination_artifact_sha256`` names the historical
    crossfit prediction artifact.  It is never compared directly with a live
    ensemble artifact content hash.  The explicit transport bridge instead
    targets the content-addressed live-combination fingerprint materialized
    in P0 lineage.
    """

    if (
        artifact.nwp_combination_artifact_kind
        != TAF_NWP_CROSSFIT_ARTIFACT_KIND
        or artifact.nwp_live_compatibility_contract_version
        != TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        or artifact.nwp_live_compatibility_fingerprint_sha256 is None
        or artifact.nwp_live_compatibility_evidence_sha256 is None
        or not artifact.nwp_live_compatibility_validated
    ):
        raise ContractError("taf_nwp_live_compatibility_bridge_missing")
    fingerprint = str(
        root.lineage.get(
            "taf_nwp_live_compatibility_fingerprint_sha256"
        )
        or ""
    ).lower()
    _sha("taf_p0_live_compatibility_fingerprint_sha256", fingerprint)
    raw_json = str(
        root.lineage.get("taf_nwp_live_compatibility_json") or ""
    )
    if not raw_json:
        raise ContractError("taf_p0_live_compatibility_lineage_missing")
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(
            "taf_p0_live_compatibility_lineage_invalid"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ContractError("taf_p0_live_compatibility_lineage_invalid")
    fingerprint_schema = str(raw.get("schema") or "")
    if fingerprint_schema not in {
        TAF_NWP_LIVE_FINGERPRINT_SCHEMA,
        TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
    }:
        raise ContractError("taf_p0_live_compatibility_schema_invalid")
    supplied = str(raw.get("fingerprint_sha256") or "").lower()
    if supplied != fingerprint:
        raise ContractError("taf_p0_live_compatibility_fingerprint_mismatch")
    identity = {
        str(key): value
        for key, value in raw.items()
        if key != "fingerprint_sha256"
    }
    if content_sha256(identity) != fingerprint:
        raise ContractError(
            "taf_p0_live_compatibility_content_sha_mismatch"
        )
    if (
        str(raw.get("station_id") or "").upper(),
        str(raw.get("season_bin") or ""),
        str(raw.get("lead_bucket") or ""),
        str(raw.get("run_cycle") or ""),
        str(raw.get("settlement_definition") or ""),
    ) != (
        root.station_id.upper(),
        str(root.lineage.get("season_bin") or ""),
        str(root.lineage.get("lead_bucket") or ""),
        str(root.lineage.get("run_cycle") or ""),
        str(root.lineage.get("settlement_definition") or ""),
    ):
        raise ContractError("taf_p0_live_compatibility_scope_mismatch")
    if fingerprint_schema == TAF_NWP_LIVE_FINGERPRINT_SCHEMA:
        calibrations = raw.get("model_calibration_artifacts")
        models = raw.get("available_models")
        weights = raw.get("resolved_weights")
        if (
            not isinstance(calibrations, Mapping)
            or not isinstance(models, list)
            or not isinstance(weights, Mapping)
            or len(models) < 2
            or set(str(item) for item in models) != set(calibrations)
            or set(str(item) for item in models) != set(weights)
        ):
            raise ContractError(
                "taf_p0_live_compatibility_model_lineage_incomplete"
            )
        live_ensemble_sha = _sha(
            "taf_p0_live_ensemble_artifact_content_sha256",
            raw.get("ensemble_artifact_content_sha256"),
        )
    else:
        systems = raw.get("ensemble_system_ids")
        families = raw.get("independence_family_ids")
        system_families = raw.get("system_family_ids")
        weights = raw.get("resolved_system_weights")
        family_caps = raw.get("family_weight_caps")
        calibrations = raw.get("system_calibration_artifacts")
        if (
            raw.get("probability_pathway") != P0_PROBABILITY_PATHWAY
            or not isinstance(systems, list)
            or len(systems) < 2
            or len(set(map(str, systems))) != len(systems)
            or not isinstance(families, list)
            or len(set(map(str, families))) < 2
            or not isinstance(system_families, Mapping)
            or not isinstance(weights, Mapping)
            or not isinstance(family_caps, Mapping)
            or not isinstance(calibrations, Mapping)
            or set(map(str, systems)) != set(map(str, system_families))
            or set(map(str, systems)) != set(map(str, weights))
            or set(map(str, systems)) != set(map(str, calibrations))
        ):
            raise ContractError(
                "taf_p0_live_compatibility_native_lineage_incomplete"
            )
        receipt = validate_cross_native_ensemble_weights(
            system_weights={
                str(key): float(value)
                for key, value in weights.items()
            },
            systems_by_id={
                str(system_id): {
                    "independence_family_id": str(
                        system_families[str(system_id)]
                    ),
                }
                for system_id in systems
            },
            family_weight_caps={
                str(key): float(value)
                for key, value in family_caps.items()
            },
        )
        if set(receipt["family_weights"]) != set(map(str, families)):
            raise ContractError(
                "taf_p0_live_compatibility_native_family_mismatch"
            )
        for binding in calibrations.values():
            if not isinstance(binding, Mapping):
                raise ContractError(
                    "taf_p0_live_compatibility_native_lineage_incomplete"
                )
            _sha(
                "taf_p0_native_system_artifact_sha256",
                binding.get("artifact_content_sha256"),
            )
            _sha(
                "taf_p0_native_system_activation_sha256",
                binding.get("activation_content_sha256"),
            )
        live_ensemble_sha = _sha(
            "taf_p0_live_cross_ensemble_artifact_sha256",
            raw.get("cross_ensemble_artifact_sha256"),
        )
    if (
        artifact.nwp_combination_artifact_sha256
        == live_ensemble_sha
    ):
        raise ContractError(
            "taf_crossfit_live_artifact_hash_alias_forbidden"
        )
    if (
        fingerprint
        != artifact.nwp_live_compatibility_fingerprint_sha256
    ):
        raise ContractError("taf_nwp_live_compatibility_bridge_mismatch")
    return fingerprint


def compile_taf_revision_proof(
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    *,
    p0_root: ProbabilityEnvelope | Mapping[str, Any],
    forecast_revision: ForecastRevision | Mapping[str, Any],
    artifact: TafRevisionArtifact | Mapping[str, Any],
    bucket_centers_c: Mapping[str, Any],
    taf_fields: Mapping[str, Any],
    decision_at_utc: str,
) -> TafRevisionProof:
    """Compile one TAF proof against the immutable NWP P0 root."""

    previous = (
        predecessor
        if isinstance(predecessor, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(predecessor)
    )
    root = (
        p0_root
        if isinstance(p0_root, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(p0_root)
    )
    revision = ForecastRevision.from_mapping(
        forecast_revision.to_dict()
        if isinstance(forecast_revision, ForecastRevision)
        else forecast_revision
    )
    governed = TafRevisionArtifact.from_mapping(
        artifact.to_dict()
        if isinstance(artifact, TafRevisionArtifact)
        else artifact
    )
    decision = _timestamp("taf_decision_at_utc", decision_at_utc)
    if root.stage != "P0":
        raise ContractError("taf_p0_root_stage_invalid")
    if previous.stage not in {"P0", "P0-M"}:
        raise ContractError("taf_predecessor_stage_invalid")
    if (
        previous.station_id,
        previous.event_date,
        previous.settlement_timezone,
    ) != (root.station_id, root.event_date, root.settlement_timezone):
        raise ContractError("taf_probability_scope_mismatch")
    if (revision.station_id, revision.event_date) != (
        root.station_id,
        root.event_date,
    ):
        raise ContractError("taf_forecast_revision_scope_mismatch")
    if governed.station_id != root.station_id:
        raise ContractError("taf_artifact_station_mismatch")
    if governed.settlement_timezone != root.settlement_timezone:
        raise ContractError("taf_artifact_settlement_timezone_mismatch")
    live_compatibility_fingerprint = (
        _validate_live_nwp_compatibility_binding(
            root=root,
            artifact=governed,
        )
    )
    if not timestamp_in_window(
        decision, governed.valid_from_utc, governed.valid_until_utc
    ):
        raise ContractError("taf_artifact_not_active_asof")
    if not timestamp_lt(governed.trained_through_utc, revision.issued_at_utc):
        raise ContractError("taf_artifact_training_leakage")
    if revision.received_at_utc > decision:
        raise ContractError("taf_revision_not_available_asof")
    scope = {
        "season_bin": governed.season_bin,
        "run_cycle": governed.run_cycle,
        "settlement_definition": governed.settlement_definition,
    }
    for key, expected in scope.items():
        actual = str(root.lineage.get(key) or "")
        if not actual:
            raise ContractError(f"taf_p0_root_{key}_missing")
        if actual != expected:
            raise ContractError(f"taf_artifact_{key}_mismatch")
    # Read through the same resolver the selection used, so an artifact can
    # never be chosen under one lead vocabulary and then rejected under the
    # other.
    root_lead_bucket = taf_scope_lead_bucket(root.lineage)
    if not root_lead_bucket:
        raise ContractError("taf_p0_root_lead_bucket_missing")
    if root_lead_bucket != governed.lead_bucket:
        raise ContractError("taf_artifact_lead_bucket_mismatch")

    if not isinstance(bucket_centers_c, Mapping) or set(bucket_centers_c) != set(
        root.probabilities
    ):
        raise ContractError("taf_bucket_centers_support_mismatch")
    centers = {
        str(bucket): _finite("taf_bucket_center_c", center)
        for bucket, center in bucket_centers_c.items()
    }
    if len(set(centers.values())) != len(centers):
        raise ContractError("taf_bucket_centers_duplicate")
    ordered_buckets = sorted(
        centers, key=lambda bucket: root.bucket_ordinals[bucket]
    )
    if any(
        centers[left] >= centers[right]
        for left, right in zip(
            ordered_buckets,
            ordered_buckets[1:],
        )
    ):
        raise ContractError("taf_bucket_centers_not_increasing")
    if not isinstance(taf_fields, Mapping):
        raise ContractError("taf_fields_invalid")
    _strict_fields(
        taf_fields,
        allowed={
            "tx_c", "cloud_cover_fraction", "precipitation_probability",
            "wind_speed_mps",
        },
        contract="taf_fields",
    )
    tx_c = _finite("taf_tx_c", taf_fields.get("tx_c"))
    cloud = _range(
        "taf_cloud_cover_fraction",
        taf_fields.get("cloud_cover_fraction"),
        0.0,
        1.0,
    )
    precipitation = _range(
        "taf_precipitation_probability",
        taf_fields.get("precipitation_probability"),
        0.0,
        1.0,
    )
    wind_speed = _range(
        "taf_wind_speed_mps",
        taf_fields.get("wind_speed_mps"),
        0.0,
        math.inf,
    )
    normalized_features = {
        "cloud_cover_fraction": cloud,
        "precipitation_probability": precipitation,
        "wind_speed_normalized": min(
            1.0, wind_speed / governed.wind_speed_cap_mps
        ),
    }
    physical_adjustment = sum(
        normalized_features[name] * governed.physical_feature_effect_c[name]
        for name in _PHYSICAL_FEATURES
    )
    base_mean = sum(root.probabilities[bucket] * centers[bucket] for bucket in centers)
    tx_only_center = (
        base_mean
        + governed.tx_bias_c
        + governed.taf_nwp_delta_coefficient * (tx_c - base_mean)
    )
    calibrated_center = tx_only_center + physical_adjustment
    delta = calibrated_center - base_mean
    mode = (
        "CONFIRMATION"
        if abs(delta) <= governed.confirmation_tolerance_c
        else "CORRECTION"
    )
    tx_delta = tx_only_center - base_mean
    if abs(physical_adjustment) <= 1e-12 or abs(tx_delta) <= 1e-12:
        physical_confirmation_state = "NEUTRAL"
        physical_consistency = 1.0
    elif math.copysign(1.0, physical_adjustment) == math.copysign(
        1.0, tx_delta
    ):
        physical_confirmation_state = "CONFIRMS"
        physical_consistency = 1.0
    else:
        physical_confirmation_state = "CONFLICTS"
        physical_consistency = 0.5
    cap = (
        governed.max_confirmation_strength
        if mode == "CONFIRMATION"
        else governed.max_correction_strength
    )
    strength = cap * governed.correlation_discount * physical_consistency
    taf_probabilities = _normal_weights(
        centers,
        mean=calibrated_center,
        scale=governed.tx_residual_scale_c,
    )
    base_center = root.center_bucket

    def candidate(
        value: float,
    ) -> tuple[dict[str, float], dict[str, float], float, str, int]:
        ratios, posterior = _apply_tempered_ratios(
            root.probabilities,
            taf_probabilities,
            strength=value,
            floor=governed.likelihood_ratio_floor,
            ceiling=governed.likelihood_ratio_ceiling,
        )
        transfer = 0.5 * sum(
            abs(posterior[bucket] - root.probabilities[bucket])
            for bucket in root.probabilities
        )
        center = min(
            posterior,
            key=lambda bucket: (
                -posterior[bucket],
                root.bucket_ordinals[bucket],
                bucket,
            ),
        )
        displacement = abs(
            root.bucket_ordinals[center] - root.bucket_ordinals[base_center]
        )
        return ratios, posterior, transfer, center, displacement

    ratios, posterior, transfer, center_after, displacement = candidate(strength)
    if (
        transfer > governed.max_probability_mass_transfer + 1e-12
        or displacement > governed.max_center_displacement_buckets
    ):
        low, high = 0.0, strength
        for _ in range(64):
            middle = (low + high) / 2.0
            _, _, middle_transfer, _, middle_displacement = candidate(middle)
            if (
                middle_transfer <= governed.max_probability_mass_transfer + 1e-12
                and middle_displacement <= governed.max_center_displacement_buckets
            ):
                low = middle
            else:
                high = middle
        strength = low
        ratios, posterior, transfer, center_after, displacement = candidate(strength)

    identity = {
        "schema": TAF_PROOF_SCHEMA,
        "mode": mode,
        "forecast_revision_id": revision.revision_id,
        "forecast_source_object_sha256": revision.source_object_sha256,
        "artifact_id": governed.artifact_id,
        "artifact_content_sha256": governed.content_sha256,
        "activation_id": governed.activation_id,
        "nwp_crossfit_prediction_artifact_sha256": (
            governed.nwp_combination_artifact_sha256
        ),
        "nwp_live_compatibility_contract_version": (
            governed.nwp_live_compatibility_contract_version
        ),
        "nwp_live_compatibility_fingerprint_sha256": (
            live_compatibility_fingerprint
        ),
        "nwp_live_compatibility_evidence_sha256": (
            governed.nwp_live_compatibility_evidence_sha256
        ),
        "base_p0_checkpoint_id": root.checkpoint_id,
        "base_p0_content_sha256": root.content_sha256,
        "predecessor_checkpoint_id": previous.checkpoint_id,
        "predecessor_content_sha256": previous.content_sha256,
        "settlement_timezone": root.settlement_timezone,
        "likelihood_ratio_by_bucket": ratios,
        "posterior_probabilities": posterior,
        "probability_mass_transfer": transfer,
        "center_before": base_center,
        "center_after": center_after,
        "center_displacement_buckets": displacement,
        "taf_tx_c": tx_c,
        "physical_features": normalized_features,
        "physical_features_sha256": content_sha256(normalized_features),
        "tx_calibrated_center_c": tx_only_center,
        "physically_confirmed_center_c": calibrated_center,
        "physical_adjustment_c": physical_adjustment,
        "physical_confirmation_state": physical_confirmation_state,
        "physical_consistency_factor": physical_consistency,
        "applied_strength": strength,
        "hard_excluded_buckets": [],
    }
    proof_id = "taf-proof-" + content_sha256(identity)[:32]
    proof_identity = {"proof_id": proof_id, **identity}
    return TafRevisionProof.from_mapping(
        {
            **proof_identity,
            "content_sha256": content_sha256(proof_identity),
        }
    )


def validate_taf_revision_proof(
    proof: TafRevisionProof | Mapping[str, Any],
    *,
    artifact: TafRevisionArtifact | Mapping[str, Any],
    predecessor: ProbabilityEnvelope,
    p0_root: ProbabilityEnvelope,
) -> TafRevisionProof:
    value = TafRevisionProof.from_mapping(
        proof.to_dict() if isinstance(proof, TafRevisionProof) else proof
    )
    governed = TafRevisionArtifact.from_mapping(
        artifact.to_dict()
        if isinstance(artifact, TafRevisionArtifact)
        else artifact
    )
    live_compatibility_fingerprint = (
        _validate_live_nwp_compatibility_binding(
            root=p0_root,
            artifact=governed,
        )
    )
    if (
        value.artifact_id,
        value.artifact_content_sha256,
        value.activation_id,
    ) != (
        governed.artifact_id,
        governed.content_sha256,
        governed.activation_id,
    ):
        raise ContractError("taf_revision_artifact_binding_mismatch")
    if (
        value.nwp_crossfit_prediction_artifact_sha256,
        value.nwp_live_compatibility_contract_version,
        value.nwp_live_compatibility_fingerprint_sha256,
        value.nwp_live_compatibility_evidence_sha256,
    ) != (
        governed.nwp_combination_artifact_sha256,
        governed.nwp_live_compatibility_contract_version,
        live_compatibility_fingerprint,
        governed.nwp_live_compatibility_evidence_sha256,
    ):
        raise ContractError(
            "taf_revision_nwp_live_compatibility_binding_mismatch"
        )
    if (
        value.predecessor_checkpoint_id,
        value.predecessor_content_sha256,
    ) != (predecessor.checkpoint_id, predecessor.content_sha256):
        raise ContractError("taf_revision_predecessor_binding_mismatch")
    if (
        value.base_p0_checkpoint_id,
        value.base_p0_content_sha256,
    ) != (p0_root.checkpoint_id, p0_root.content_sha256):
        raise ContractError("taf_revision_p0_root_binding_mismatch")
    if value.settlement_timezone != p0_root.settlement_timezone:
        raise ContractError("taf_revision_settlement_timezone_mismatch")
    if (
        content_sha256(dict(value.physical_features))
        != value.physical_features_sha256
    ):
        raise ContractError("taf_physical_features_sha256_mismatch")
    recomputed_physical_adjustment = sum(
        value.physical_features[name]
        * governed.physical_feature_effect_c[name]
        for name in _PHYSICAL_FEATURES
    )
    if (
        abs(recomputed_physical_adjustment - value.physical_adjustment_c)
        > 1e-9
    ):
        raise ContractError("taf_physical_adjustment_proof_mismatch")
    if set(value.likelihood_ratio_by_bucket) != set(p0_root.probabilities):
        raise ContractError("taf_revision_bucket_support_mismatch")
    if any(
        ratio < governed.likelihood_ratio_floor - 1e-12
        or ratio > governed.likelihood_ratio_ceiling + 1e-12
        for ratio in value.likelihood_ratio_by_bucket.values()
    ):
        raise ContractError("taf_revision_likelihood_ratio_cap_exceeded")
    strength_cap = (
        governed.max_confirmation_strength
        if value.mode == "CONFIRMATION"
        else governed.max_correction_strength
    )
    strength_cap *= governed.correlation_discount
    strength_cap *= value.physical_consistency_factor
    if value.applied_strength > strength_cap + 1e-12:
        raise ContractError("taf_revision_strength_cap_exceeded")
    if abs(
        value.physically_confirmed_center_c
        - value.tx_calibrated_center_c
        - value.physical_adjustment_c
    ) > 1e-9:
        raise ContractError("taf_physical_second_confirmation_arithmetic_invalid")
    expected_physical_factor = (
        0.5 if value.physical_confirmation_state == "CONFLICTS" else 1.0
    )
    if abs(value.physical_consistency_factor - expected_physical_factor) > 1e-12:
        raise ContractError("taf_physical_confirmation_factor_invalid")
    unnormalized = {
        bucket: p0_root.probabilities[bucket]
        * value.likelihood_ratio_by_bucket[bucket]
        for bucket in p0_root.probabilities
    }
    total = sum(unnormalized.values())
    recomputed = {bucket: amount / total for bucket, amount in unnormalized.items()}
    if any(
        abs(recomputed[bucket] - value.posterior_probabilities[bucket]) > 1e-9
        for bucket in recomputed
    ):
        raise ContractError("taf_revision_posterior_proof_mismatch")
    actual_transfer = 0.5 * sum(
        abs(recomputed[bucket] - p0_root.probabilities[bucket])
        for bucket in recomputed
    )
    if abs(actual_transfer - value.probability_mass_transfer) > 1e-9:
        raise ContractError("taf_revision_mass_transfer_proof_mismatch")
    if actual_transfer > governed.max_probability_mass_transfer + 1e-12:
        raise ContractError("taf_revision_mass_transfer_cap_exceeded")
    actual_center_before = p0_root.center_bucket
    actual_center_after = min(
        recomputed,
        key=lambda bucket: (
            -recomputed[bucket],
            p0_root.bucket_ordinals[bucket],
            bucket,
        ),
    )
    actual_displacement = abs(
        p0_root.bucket_ordinals[actual_center_after]
        - p0_root.bucket_ordinals[actual_center_before]
    )
    if (
        value.center_before != actual_center_before
        or value.center_after != actual_center_after
        or value.center_displacement_buckets != actual_displacement
    ):
        raise ContractError("taf_revision_center_proof_mismatch")
    if actual_displacement > governed.max_center_displacement_buckets:
        raise ContractError("taf_revision_center_displacement_cap_exceeded")
    if any(probability == 0.0 and p0_root.probabilities[bucket] > 0.0 for bucket, probability in recomputed.items()):
        raise ContractError("taf_revision_probability_zero_forbidden")
    return value


__all__ = [
    "TAF_ARTIFACT_SCHEMA",
    "TAF_AUTHORITY_SCOPE",
    "TAF_CORRELATION_METHOD",
    "TAF_METHOD_VERSION",
    "TAF_NWP_CROSSFIT_ARTIFACT_KIND",
    "TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION",
    "TAF_NWP_LIVE_FINGERPRINT_SCHEMA",
    "TAF_PROOF_SCHEMA",
    "TafRevisionArtifact",
    "TafRevisionProof",
    "compile_taf_revision_proof",
    "taf_scope_lead_bucket",
    "validate_taf_revision_proof",
]
