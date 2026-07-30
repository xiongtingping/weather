"""Frozen operational baseline for the unique joint weak-likelihood model.

The baseline exists for the period before enough closed physical-X samples
exist to fit and promote calibration parameters.  It is *not* a legacy-model
fallback: its source universe, source identity, error model, missingness
semantics and correlation treatment are those of the joint weak model.

Every actual source-set/context pair is deterministically materialised from
the immutable template by ``weak_likelihood_source_set_runtime``.  The
generated surface is exact for that pair and carries the frozen artifact SHA;
it never borrows a trained full-source surface.  Subsequent trained artifacts
replace this baseline through the normal immutable approval route.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from joint_forecast_weak_likelihood_runtime import (
    ARTIFACT_SCHEMA_VERSION,
    canonical_artifact_sha256,
    validate_joint_forecast_weak_artifact,
)
from joint_weak_bias_calibration_runtime import (
    DEFAULT_EWMA_ALPHA,
    SCHEMA_VERSION as BIAS_CALIBRATION_SCHEMA_VERSION,
)


BASELINE_TEMPLATE_SCHEMA = "joint_weak_exact_surface_baseline_template_v1"
BASELINE_ARTIFACT_SCHEMA = ARTIFACT_SCHEMA_VERSION
DEFAULT_ARTIFACT_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "runtime"
    / "joint_forecast_weak_likelihood"
    / "approved"
    / "joint_weak_d1_settlement_calibrated_v1.json"
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def build_joint_weak_operational_baseline_artifact() -> dict[str, Any]:
    """Build the fixed, low-information pre-calibration artifact.

    The numbers are a governed no-sample fallback, not an assertion that any
    source has earned a historical skill advantage.  They may neither force
    multiple modes nor smear disagreement into a synthetic consensus.  A
    promoted artifact replaces them with condition-specific bias and residual
    distributions learned from closed physical X.
    """

    source_catalog = {
        "ecmwf": {"provider_family": "official_nwp"},
        "icon": {"provider_family": "official_nwp"},
        "gfs": {"provider_family": "official_nwp"},
        "jma": {"provider_family": "official_nwp"},
        "taf": {"provider_family": "taf"},
        "wu": {"provider_family": "consumer_weather"},
        "twc": {"provider_family": "consumer_weather"},
    }
    template = {
        "schema": BASELINE_TEMPLATE_SCHEMA,
        "surface_generation": "deterministic_exact_context_and_source_set_v1",
        "surface_identity": "artifact_sha256+context_key+available_source_ids+missing_source_ids",
        "no_nearest_context_or_source_set_fallback": True,
        "source_base_weights": {
            "ecmwf": 1.0,
            "icon": 1.0,
            "gfs": 1.0,
            "jma": 1.0,
            "taf": 1.0,
            "wu": 0.8,
            "twc": 0.8,
        },
        # These are conservative scenario-kernel widths for the untrained
        # baseline, not claims of historical RMSE. Missing-source and
        # pre-calibration uncertainty are expressed by the low information
        # scale/confidence below. Making each kernel 3-4C wide destroys real
        # forecast disagreement and invents a middle-temperature consensus.
        "source_error_sigma_c": {
            "ecmwf": 1.0,
            "icon": 1.0,
            "gfs": 1.2,
            "jma": 1.2,
            "taf": 1.2,
            "wu": 1.4,
            "twc": 1.4,
        },
        "source_residual_distribution_family": "gaussian_scale_fallback",
        "family_information_caps": {
            "official_nwp": 0.60,
            "taf": 0.35,
            "consumer_weather": 0.25,
        },
        "family_pair_correlation": {
            "consumer_weather|consumer_weather": 0.85,
            "consumer_weather|official_nwp": 0.35,
            "consumer_weather|taf": 0.35,
            "official_nwp|official_nwp": 0.72,
            "official_nwp|taf": 0.30,
            "taf|taf": 1.0,
        },
        "base_joint_information_scale": 0.45,
        "base_information_confidence": 0.35,
        "maximum_joint_information_scale": 0.45,
        "training_event_day_count": 0,
        "calibration_state": "operator_approved_precalibration_baseline",
        "bias_policy": "explicit_zero_until_closed_physical_x_artifact_replaces_baseline",
        "uncertainty_separation_policy": "source_residual_scale_controls_within_source_uncertainty_while_correlation_and_information_scale_control_joint_confidence",
        "mode_formation_policy": "emergent_from_bias_corrected_distance_residual_overlap_and_source_correlation",
        "force_multimodality": False,
        "force_unimodality": False,
    }
    artifact = {
        "schema": BASELINE_ARTIFACT_SCHEMA,
        "approval_state": "approved",
        "approval_kind": "operational_precalibration_baseline",
        "source_data_policy": "forecast_only_asof",
        "training_data_policy": "no_closed_physical_x_required_for_frozen_baseline",
        "replacement_policy": "only_approved_trained_joint_weak_artifact_may_replace",
        "source_catalog": source_catalog,
        "bias_correction": {
            "schema": BIAS_CALIBRATION_SCHEMA_VERSION,
            "residual_definition": "forecast_peak_c_minus_physical_x_final_c",
            "source_data_policy": "closed_physical_x_only_no_d0_observation_or_market",
            "ewma_alpha": DEFAULT_EWMA_ALPHA,
            "preregistered_ewma_alpha_candidates": [0.03, 0.05],
            "minimum_independent_event_days": 30,
            "bias_clamp_c": 1.5,
            "d0_deduplication": "latest_strict_asof_revision_per_source_event_day_d0_time_bin",
            "source_global_states": {},
            "season_states": {},
            "context_states": {},
            "training_row_count": 0,
            "training_event_day_count": 0,
            "promotion_note": "baseline_uses_explicit_zero_bias_until_closed_physical_x_training_candidate_is_approved",
        },
        "exact_surface_baseline_template": template,
        "source_set_surfaces": {},
        "boundary": {
            "all_forecast_sources_are_predictions": True,
            "d0_observation_not_consumed": True,
            "market_or_trading_not_consumed": True,
            "strict_asof_oos_is_replacement_promotion_evidence_not_live_gate": True,
            "legacy_open_meteo_or_v31_coefficients_used": False,
            "missing_source_is_explicit_model_input": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    return artifact


def load_approved_joint_weak_artifact(path: Path | str | None = None) -> dict[str, Any]:
    """Load only a valid approved artifact; never fall back to legacy models."""

    target = Path(
        path
        or os.environ.get("PM_JOINT_WEAK_ARTIFACT", "")
        or DEFAULT_ARTIFACT_PATH
    ).expanduser().resolve()
    try:
        artifact = _mapping(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "ok": False,
            "state": "blocked",
            "block_reasons": ["joint_weak_approved_artifact_unreadable"],
            "artifact_path": str(target),
            "artifact": {},
            "read_only": True,
        }
    contract = validate_joint_forecast_weak_artifact(artifact)
    if contract.get("ok") is not True:
        return {
            "ok": False,
            "state": "blocked",
            "block_reasons": list(contract.get("issues") or ["joint_weak_approved_artifact_invalid"]),
            "artifact_path": str(target),
            "artifact": artifact,
            "artifact_contract": contract,
            "read_only": True,
        }
    return {
        "ok": True,
        "state": "available",
        "artifact_path": str(target),
        "artifact": artifact,
        "artifact_contract": contract,
        "read_only": True,
    }


__all__ = [
    "BASELINE_ARTIFACT_SCHEMA",
    "BASELINE_TEMPLATE_SCHEMA",
    "DEFAULT_ARTIFACT_PATH",
    "build_joint_weak_operational_baseline_artifact",
    "load_approved_joint_weak_artifact",
]
