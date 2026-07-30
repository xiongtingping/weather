"""Sample-nonblocking operational X→S residual prior for the fixed ZUUU authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from resolution_bridge_artifact_runtime import (
    AUTHORITY_CONTRACT_ID,
    CANDIDATE_STATE,
    OPERATIONAL_BASELINE_KERNEL_KIND,
    PHYSICAL_SOURCE_ID,
    SETTLEMENT_SOURCE_ID,
    artifact_sha256,
    validate_resolution_bridge_artifact,
)


SCHEMA_VERSION = "resolution_bridge_operational_baseline_v1"
SUPPORT = tuple(range(-20, 21))
# Same-station, integer-reported maxima make zero residual structurally more
# plausible.  Because this is a *soft* prior rather than an authority hard
# envelope, both tails must retain non-zero mass; otherwise distant market
# buckets would be rendered as false exact zeros.
PRIOR_COUNTS = {
    residual: (
        8.0
        if residual == 0
        else 1.0
        if abs(residual) <= 2
        else 0.1 * (0.5 ** (abs(residual) - 3))
    )
    for residual in SUPPORT
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_resolution_bridge_operational_baseline_candidate(
    *,
    authority_contract_sha256: str,
    operational_projection_authority_contract_sha256: str,
    observed_pairs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build one immutable zero-sample baseline.

    Real X/S pairs belong exclusively to the calibrated-artifact pipeline.
    Accepting them here would create an unreviewed online-learning bypass.
    """

    if observed_pairs:
        raise ValueError("operational_bridge_baseline_must_not_consume_observed_pairs")
    counts = dict(PRIOR_COUNTS)
    total = sum(counts.values())
    pmf = {str(key): value / total for key, value in sorted(counts.items())}
    dataset_sha = _sha({"kind": "zero_sample_operational_baseline", "observed_pairs": []})
    core = {
        "schema": "resolution_x_s_bridge_artifact_v1",
        "state": CANDIDATE_STATE,
        "authority_contract_id": AUTHORITY_CONTRACT_ID,
        "authority_contract_sha256": _text(authority_contract_sha256),
        "operational_projection_authority_contract_id": "zuuu_x_s_soft_projection_operational_v1",
        "operational_projection_authority_contract_sha256": _text(
            operational_projection_authority_contract_sha256
        ),
        "physical_source_id": PHYSICAL_SOURCE_ID,
        "settlement_source_id": SETTLEMENT_SOURCE_ID,
        "target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "settlement_type": "wunderground_zuuu_historical_observation_daily_max_c",
        "kernel_kind": OPERATIONAL_BASELINE_KERNEL_KIND,
        "training_dataset_sha256": dataset_sha,
        "global_residual_pmf": pmf,
        "seasonal_residual_pmf": {},
        "coverage": {
            "distinct_event_days": 0,
            "distinct_physical_x_values": 0,
            "distinct_settlement_s_values": 0,
            "accepted_pair_count": 0,
            "required_for_runtime": False,
        },
        "oos_evaluation": {
            "state": "not_required_for_operational_baseline",
            "evaluated_event_days": 0,
            "required_for_runtime": False,
        },
        "operational_baseline": {
            "construction": "same_station_integer_maximum_soft_heavy_tail_prior_v2",
            "residual_support_c": list(SUPPORT),
            "prior_pseudocount_by_residual": {str(key): value for key, value in sorted(PRIOR_COUNTS.items())},
            "accepted_pair_count": 0,
            "sample_or_oos_required_for_runtime": False,
            "calibrated_artifact_replaces_baseline_when_approved": True,
            "settlement_exclusion_requires_full_residual_support_exclusion": True,
            "hard_exclusion_certificates_authorized": False,
            "residual_support_is_probabilistic_not_authority_hard_envelope": True,
            "soft_tail_mass_present": True,
            "soft_projection_exact_zero_forbidden_without_hard_certificate": True,
        },
        "bridge_closed": False,
        "validated": False,
        "production_runtime_eligible": False,
        "shadow_only": True,
    }
    candidate = {**core, "artifact_sha256": artifact_sha256(core)}
    return validate_resolution_bridge_artifact(candidate)


__all__ = [
    "PRIOR_COUNTS",
    "SCHEMA_VERSION",
    "SUPPORT",
    "build_resolution_bridge_operational_baseline_candidate",
]
