"""Pure checkpoint/SSOT adapter for the report-defined physical-X epoch.

This module is intentionally outside the active production manifest until the
successor activation is cut over.  It owns the only legal conversion from the
continuous scheduled-slot posterior Z to the whole-degree AMSC target X.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from report_defined_physical_x_runtime import (
    CHECKPOINT_PROJECTION_SCHEMA_VERSION,
    LATENT_TYPE,
    TARGET_TYPE,
    build_report_defined_checkpoint_projection,
)
from resolution_bridge_runtime import (
    build_physical_only_resolution_bridge,
    build_resolution_bridge,
)


SCHEMA_VERSION = "reported_x_successor_checkpoint_runtime_v1"
CHECKPOINT_SCHEMA = "posterior_checkpoint_v3_reported_x"
PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v3_reported_x"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse(value: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    return json.loads(_text(value))


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _checkpoint_object(value: Mapping[str, Any]) -> dict[str, Any]:
    checkpoint = dict(_mapping(value))
    if not isinstance(checkpoint.get("joint_posterior"), list):
        try:
            checkpoint["joint_posterior"] = list(_parse(checkpoint.get("joint_posterior_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            checkpoint["joint_posterior"] = []
    return checkpoint


def build_successor_checkpoint_surfaces(
    checkpoint: Mapping[str, Any],
    *,
    canonical_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create all successor probability surfaces from one posterior only."""

    projection = build_report_defined_checkpoint_projection(
        _checkpoint_object(checkpoint),
        canonical_events=canonical_events,
    )
    if projection.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(projection.get("block_reasons") or ["reported_x_projection_blocked"]),
            "projection": projection,
            "read_only": True,
            "places_orders": False,
        }
    core = {
        "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
        "probability_target_type": TARGET_TYPE,
        "latent_type": LATENT_TYPE,
        "latent_scheduled_slot_high_distribution": dict(
            projection["latent_scheduled_slot_high_distribution"]
        ),
        "reported_physical_x_distribution": dict(
            projection["reported_physical_x_distribution"]
        ),
        "latent_observed_lower_bound_c": projection.get("latent_observed_lower_bound_c"),
        "reported_x_lower_bound_c": projection.get("reported_x_lower_bound_c"),
        "reported_x_termination": dict(projection.get("reported_x_termination") or {}),
        "reported_x_projection_sha256": _text(projection.get("projection_sha256")),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "successor_surfaces_ready",
        **core,
        "surfaces_sha256": _sha(core),
        "read_only": True,
        "places_orders": False,
    }


def materialize_successor_checkpoint_columns(
    checkpoint: Mapping[str, Any],
    *,
    canonical_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    surfaces = build_successor_checkpoint_surfaces(
        checkpoint,
        canonical_events=canonical_events,
    )
    if surfaces.get("ok") is not True:
        return surfaces
    columns = {
        "checkpoint_probability_schema": surfaces["checkpoint_probability_schema"],
        "probability_target_type": surfaces["probability_target_type"],
        "latent_scheduled_slot_high_distribution_json": _json(
            surfaces["latent_scheduled_slot_high_distribution"]
        ),
        "reported_physical_x_distribution_json": _json(
            surfaces["reported_physical_x_distribution"]
        ),
        "latent_observed_lower_bound_c": surfaces.get("latent_observed_lower_bound_c"),
        "reported_x_lower_bound_c": surfaces.get("reported_x_lower_bound_c"),
        "reported_x_termination_json": _json(surfaces.get("reported_x_termination") or {}),
        "reported_x_projection_sha256": surfaces["reported_x_projection_sha256"],
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "successor_checkpoint_columns_ready",
        "columns": columns,
        "surfaces_sha256": surfaces["surfaces_sha256"],
        "read_only": True,
        "places_orders": False,
    }


def validate_successor_checkpoint_row(row: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(row)
    issues: list[str] = []
    if _text(value.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA:
        issues.append("successor_checkpoint_schema_invalid")
    if _text(value.get("probability_target_type")) != TARGET_TYPE:
        issues.append("successor_target_type_invalid")
    try:
        latent = dict(_parse(value.get("latent_scheduled_slot_high_distribution_json")))
        reported = dict(_parse(value.get("reported_physical_x_distribution_json")))
        termination = dict(_parse(value.get("reported_x_termination_json")))
    except (TypeError, ValueError, json.JSONDecodeError):
        latent, reported, termination = {}, {}, {}
        issues.append("successor_probability_json_invalid")
    for name, distribution, integer_support in (
        ("latent", latent, False),
        ("reported_x", reported, True),
    ):
        total = 0.0
        for raw_key, raw_probability in distribution.items():
            try:
                key = float(raw_key)
                probability = float(raw_probability)
            except (TypeError, ValueError):
                issues.append(f"{name}_distribution_row_invalid")
                continue
            if not math.isfinite(key) or not math.isfinite(probability) or probability < 0.0:
                issues.append(f"{name}_distribution_row_invalid")
            if integer_support and not key.is_integer():
                issues.append("reported_x_support_not_integer")
            total += probability
        if not distribution or abs(total - 1.0) > 1e-8:
            issues.append(f"{name}_distribution_not_normalized")
    lower = value.get("reported_x_lower_bound_c")
    if lower is not None:
        try:
            numeric_lower = float(lower)
        except (TypeError, ValueError):
            numeric_lower = math.nan
        if not math.isfinite(numeric_lower) or not numeric_lower.is_integer():
            issues.append("reported_x_lower_bound_not_integer")
        elif any(float(key) < numeric_lower for key in reported):
            issues.append("reported_x_mass_below_hard_bound")
    if termination and termination.get("updates_posterior") is not False:
        issues.append("reported_x_termination_must_not_update_posterior")
    declared_sha = _text(value.get("reported_x_projection_sha256"))
    if len(declared_sha) != 64:
        issues.append("reported_x_projection_sha256_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not issues,
        "state": "valid" if not issues else "blocked",
        "issues": sorted(set(issues)),
        "latent_scheduled_slot_high_distribution": latent,
        "reported_physical_x_distribution": reported,
        "reported_x_termination": termination,
        "read_only": True,
    }


def build_successor_resolution_bridge(
    checkpoint_row: Mapping[str, Any],
    *,
    resolution_contract: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_successor_checkpoint_row(checkpoint_row)
    if validated.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(validated.get("issues") or []),
            "read_only": True,
        }
    distribution = validated["reported_physical_x_distribution"]
    contract = _mapping(resolution_contract)
    if contract.get("bridge_closed") is True and contract.get("validated") is True:
        bridge = build_resolution_bridge(
            physical_high_distribution=distribution,
            resolution_contract=contract,
        )
    else:
        bridge = build_physical_only_resolution_bridge(
            physical_high_distribution=distribution,
            resolution_contract=contract,
        )
    return {
        **bridge,
        "input_probability_field": "reported_physical_x_distribution",
        "input_target_type": TARGET_TYPE,
        "continuous_latent_distribution_consumed": False,
    }


def build_successor_public_ssot_surfaces(checkpoint_row: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_successor_checkpoint_row(checkpoint_row)
    if validated.get("ok") is not True:
        return {
            "schema": PUBLIC_SSOT_SCHEMA,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(validated.get("issues") or []),
            "read_only": True,
        }
    return {
        "schema": PUBLIC_SSOT_SCHEMA,
        "ok": True,
        "state": "available",
        "probability_target_type": TARGET_TYPE,
        "reported_physical_x_distribution": validated[
            "reported_physical_x_distribution"
        ],
        "reported_x_lower_bound_c": checkpoint_row.get("reported_x_lower_bound_c"),
        "reported_x_termination": validated["reported_x_termination"],
        "latent_probability_public": False,
        "physical_high_distribution_retired": True,
        "read_only": True,
    }


def _reported_termination_for_update(
    surfaces: Mapping[str, Any],
    *,
    canonical_observation_event_id: str,
    model_artifact_sha256: str,
) -> dict[str, Any]:
    source = _mapping(surfaces.get("reported_x_termination"))
    if _text(source.get("state")) != "available":
        return dict(source)
    no_new = float(source["probability_no_future_reported_new_high"])
    new = float(source["probability_future_reported_new_high"])
    return {
        **dict(source),
        "schema": "termination_risk_marginal_v2_reported_x",
        "probability_no_material_new_daily_high": no_new,
        "probability_material_new_daily_high": new,
        "canonical_observation_event_id": _text(canonical_observation_event_id),
        "model_artifact_sha256": _text(model_artifact_sha256),
        "calculation": "same_reported_x_posterior_mass_at_or_below_current_regular_metar_max",
        "same_strong_update_marginal": True,
        "updates_posterior": False,
    }


def upgrade_physical_only_strong_candidate(
    candidate: Mapping[str, Any],
    *,
    canonical_events: Iterable[Mapping[str, Any]],
    resolution_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Upgrade an orderless strong candidate before its atomic commit.

    A closed-bridge candidate is refused because its realization/outbox was
    already calculated from the old ambiguous surface.  The successor native
    closed-bridge path must rebuild those objects rather than patch them.
    """

    value = dict(_mapping(candidate))
    if value.get("ok") is not True or _text(value.get("state")) != "physical_only_candidate_ready":
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["successor_strong_candidate_must_be_physical_only"],
            "read_only": True,
            "places_orders": False,
        }
    checkpoint = dict(_mapping(value.get("checkpoint")))
    materialized = materialize_successor_checkpoint_columns(
        checkpoint,
        canonical_events=canonical_events,
    )
    if materialized.get("ok") is not True:
        return dict(materialized)
    columns = dict(materialized["columns"])
    checkpoint.update(
        {
            "schema": CHECKPOINT_SCHEMA,
            "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
            "probability_target_type": columns["probability_target_type"],
            "latent_scheduled_slot_high_distribution": json.loads(
                columns["latent_scheduled_slot_high_distribution_json"]
            ),
            "reported_physical_x_distribution": json.loads(
                columns["reported_physical_x_distribution_json"]
            ),
            "latent_observed_lower_bound_c": columns["latent_observed_lower_bound_c"],
            "reported_x_lower_bound_c": columns["reported_x_lower_bound_c"],
            "reported_x_termination": json.loads(columns["reported_x_termination_json"]),
            "reported_x_projection_sha256": columns["reported_x_projection_sha256"],
        }
    )
    checkpoint.pop("physical_high_distribution", None)
    checkpoint_row = dict(_mapping(value.get("checkpoint_row")))
    checkpoint_row.update(columns)
    # The NOT NULL legacy storage column remains structurally present until a
    # future table rebuild, but successor rows never put probability in it.
    checkpoint_row["physical_high_distribution_json"] = "{}"
    bridge = build_successor_resolution_bridge(
        checkpoint_row,
        resolution_contract=resolution_contract,
    )
    if bridge.get("ok") is not True or bridge.get("bridge_closed") is not False:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["successor_physical_only_bridge_invalid"],
            "resolution_bridge": bridge,
            "read_only": True,
            "places_orders": False,
        }
    checkpoint_row["settlement_scalar_distribution_json"] = _json(
        bridge.get("settlement_scalar_distribution") or []
    )
    checkpoint_row["market_bucket_distribution_json"] = _json(
        bridge.get("market_bucket_probabilities") or {}
    )
    strong_update = dict(_mapping(value.get("strong_observation_update")))
    surfaces = build_successor_checkpoint_surfaces(
        checkpoint,
        canonical_events=canonical_events,
    )
    termination = _reported_termination_for_update(
        surfaces,
        canonical_observation_event_id=_text(
            strong_update.get("canonical_observation_event_id")
        ),
        model_artifact_sha256=_text(strong_update.get("model_artifact_sha256")),
    )
    strong_update["schema"] = "strong_observation_update_v5_cumulative_replacement_speci_soft"
    strong_update["reported_x_lower_bound_c"] = columns["reported_x_lower_bound_c"]
    strong_update["termination_risk"] = termination
    update_row = dict(_mapping(value.get("strong_update_row")))
    try:
        auxiliary = dict(json.loads(_text(update_row.get("path_auxiliary_json")) or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        auxiliary = {}
    auxiliary["termination_risk"] = termination
    auxiliary["reported_x_lower_bound_c"] = columns["reported_x_lower_bound_c"]
    auxiliary["probability_target_type"] = TARGET_TYPE
    likelihood_identity = {
        "schema": "strong_likelihood_cumulative_replacement_identity_v1",
        "likelihood_update_kind": _text(strong_update.get("likelihood_update_kind")),
        "current_cumulative_raw_log_score_sha256": _text(
            strong_update.get("raw_log_score_sha256")
        ),
        "previous_cumulative_raw_log_score_sha256": _text(
            strong_update.get("previous_cumulative_raw_log_score_sha256")
        ),
        "conditional_innovation_log_score_sha256": _text(
            strong_update.get("conditional_innovation_log_score_sha256")
        ),
        "previous_checkpoint_id": _text(strong_update.get("previous_checkpoint_id")),
    }
    if (
        likelihood_identity["likelihood_update_kind"]
        != "cumulative_evidence_replacement_delta_log_lr_v1"
        or not _is_sha256(likelihood_identity["current_cumulative_raw_log_score_sha256"])
        or not _is_sha256(likelihood_identity["conditional_innovation_log_score_sha256"])
        or not likelihood_identity["previous_checkpoint_id"]
    ):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["successor_strong_likelihood_replacement_identity_invalid"],
            "read_only": True,
            "places_orders": False,
        }
    auxiliary["strong_likelihood_update_identity"] = likelihood_identity
    update_row["path_auxiliary_json"] = _json(auxiliary)
    return {
        **value,
        "schema": "sequential_bayes_candidate_pipeline_v2_reported_x",
        "checkpoint": checkpoint,
        "checkpoint_row": checkpoint_row,
        "strong_observation_update": strong_update,
        "strong_update_row": update_row,
        "resolution_bridge": bridge,
        "successor_surfaces_sha256": materialized["surfaces_sha256"],
        "ok": True,
        "state": "physical_only_candidate_ready",
        "places_orders": False,
    }


def upgrade_initial_candidate(
    candidate: Mapping[str, Any],
    *,
    resolution_contract: Mapping[str, Any],
) -> dict[str, Any]:
    value = dict(_mapping(candidate))
    checkpoint = dict(_mapping(value.get("checkpoint")))
    if value.get("ok") is not True or _text(checkpoint.get("checkpoint_kind")) != "initial":
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["successor_initial_candidate_invalid"],
            "read_only": True,
            "places_orders": False,
        }
    materialized = materialize_successor_checkpoint_columns(
        checkpoint,
        canonical_events=[],
    )
    if materialized.get("ok") is not True:
        return dict(materialized)
    columns = dict(materialized["columns"])
    checkpoint.update(
        {
            "schema": CHECKPOINT_SCHEMA,
            "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
            "probability_target_type": columns["probability_target_type"],
            "latent_scheduled_slot_high_distribution": json.loads(
                columns["latent_scheduled_slot_high_distribution_json"]
            ),
            "reported_physical_x_distribution": json.loads(
                columns["reported_physical_x_distribution_json"]
            ),
            "latent_observed_lower_bound_c": None,
            "reported_x_lower_bound_c": None,
            "reported_x_termination": json.loads(columns["reported_x_termination_json"]),
            "reported_x_projection_sha256": columns["reported_x_projection_sha256"],
        }
    )
    checkpoint.pop("physical_high_distribution", None)
    synthetic_row = {**columns}
    bridge = build_successor_resolution_bridge(
        synthetic_row,
        resolution_contract=resolution_contract,
    )
    if bridge.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["successor_initial_bridge_invalid"],
            "resolution_bridge": bridge,
            "read_only": True,
            "places_orders": False,
        }
    return {
        **value,
        "schema": "production_initial_posterior_checkpoint_compiler_v2_reported_x",
        "checkpoint": checkpoint,
        "resolution_bridge": bridge,
        "successor_checkpoint_columns": columns,
        "successor_surfaces_sha256": materialized["surfaces_sha256"],
        "ok": True,
        "places_orders": False,
    }


def upgrade_weak_revision_candidate(
    candidate: Mapping[str, Any],
    *,
    previous_checkpoint_row: Mapping[str, Any],
    canonical_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    value = dict(_mapping(candidate))
    checkpoint_row = dict(_mapping(value.get("checkpoint_row")))
    if value.get("ok") is not True or _text(value.get("state")) != "candidate_ready":
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["successor_weak_revision_candidate_invalid"],
            "read_only": True,
            "places_orders": False,
        }
    materialized = materialize_successor_checkpoint_columns(
        checkpoint_row,
        canonical_events=canonical_events,
    )
    if materialized.get("ok") is not True:
        return dict(materialized)
    previous_validation = validate_successor_checkpoint_row(previous_checkpoint_row)
    if previous_validation.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["successor_weak_revision_predecessor_invalid"],
            "predecessor_issues": list(previous_validation.get("issues") or []),
            "read_only": True,
            "places_orders": False,
        }
    columns = dict(materialized["columns"])
    checkpoint_row.update(columns)
    checkpoint_row["physical_high_distribution_json"] = "{}"
    current_reported = json.loads(columns["reported_physical_x_distribution_json"])
    previous_reported = previous_validation["reported_physical_x_distribution"]
    delta = {
        key: float(current_reported.get(key, 0.0))
        - float(previous_reported.get(key, 0.0))
        for key in sorted(set(current_reported) | set(previous_reported), key=float)
    }
    update_row = dict(_mapping(value.get("update_row")))
    update_row["posterior_delta_json"] = _json(delta)
    update_row["posterior_delta_sha256"] = _sha(delta)
    update_row["state"] = (
        "neutral" if all(abs(number) < 1e-15 for number in delta.values()) else "applied"
    )
    return {
        **value,
        "schema": "weak_forecast_revision_transaction_candidate_v2_reported_x",
        "checkpoint_row": checkpoint_row,
        "update_row": update_row,
        "successor_surfaces_sha256": materialized["surfaces_sha256"],
        "ok": True,
        "places_orders": False,
    }


__all__ = [
    "CHECKPOINT_SCHEMA",
    "PUBLIC_SSOT_SCHEMA",
    "SCHEMA_VERSION",
    "build_successor_checkpoint_surfaces",
    "build_successor_public_ssot_surfaces",
    "build_successor_resolution_bridge",
    "materialize_successor_checkpoint_columns",
    "upgrade_initial_candidate",
    "upgrade_physical_only_strong_candidate",
    "upgrade_weak_revision_candidate",
    "validate_successor_checkpoint_row",
]
