"""Pure compiler for the production P0 prior×weak checkpoint.

P0 is a probability bootstrap, not a database migration and not a trading
decision.  This boundary validates the frozen production activation and then
uses the shared Bayes mathematics with a production-specific identity.
"""

from __future__ import annotations

from typing import Any, Mapping

from initial_posterior_checkpoint_shadow import build_initial_posterior_checkpoint_for_mode
from lineage_identity_runtime import build_posterior_checkpoint_id
from production_probability_activation_contract import validate_production_probability_activation
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row


SCHEMA_VERSION = "production_initial_posterior_checkpoint_compiler_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def compile_production_initial_posterior_checkpoint(
    *,
    activation: Mapping[str, Any],
    target_identity: str,
    event_date: str,
    station_id: str,
    decision_as_of: str,
    prior_context: Mapping[str, Any],
    joint_weak_likelihood: Mapping[str, Any],
    active_strong_lr_artifact: Mapping[str, Any],
    resolution_contract: Mapping[str, Any],
    source_reliability_initial_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile a production P0 without opening SQLite or touching a market."""

    validated_activation = validate_production_probability_activation(activation)
    if (
        prior_context.get("production_runtime_eligible") is not True
        or prior_context.get("shadow_only") is not False
    ):
        raise ValueError("production_initial_checkpoint_prior_not_production_eligible")
    if (
        joint_weak_likelihood.get("production_runtime_eligible") is not True
        or joint_weak_likelihood.get("shadow_only") is not False
    ):
        raise ValueError("production_initial_checkpoint_weak_not_production_eligible")
    if (
        active_strong_lr_artifact.get("production_runtime_eligible") is not True
        or active_strong_lr_artifact.get("shadow_only") is not False
    ):
        raise ValueError("production_initial_checkpoint_strong_artifact_not_production_eligible")
    artifact_sha = _text(_mapping(active_strong_lr_artifact).get("model_artifact_sha256"))
    if artifact_sha != _text(validated_activation.get("strong_lr_artifact_sha256")):
        raise ValueError("production_initial_checkpoint_activation_artifact_mismatch")
    if _text(_mapping(prior_context).get("prior_model_artifact_sha256")) != _text(validated_activation.get("prior_model_artifact_sha256")):
        raise ValueError("production_initial_checkpoint_activation_prior_artifact_mismatch")
    if _text(_mapping(joint_weak_likelihood).get("artifact_sha256")) != _text(validated_activation.get("weak_likelihood_artifact_sha256")):
        raise ValueError("production_initial_checkpoint_activation_weak_artifact_mismatch")
    compiled = build_initial_posterior_checkpoint_for_mode(
        initial_mode="production",
        target_identity=target_identity,
        event_date=event_date,
        station_id=station_id,
        decision_as_of=decision_as_of,
        prior_context=prior_context,
        joint_weak_likelihood=joint_weak_likelihood,
        active_strong_lr_artifact=active_strong_lr_artifact,
        resolution_contract=resolution_contract,
        source_reliability_initial_state=source_reliability_initial_state,
    )
    if compiled.get("ok") is not True:
        return {
            **compiled,
            "schema": SCHEMA_VERSION,
            "compiler_state": "blocked",
            "production": True,
            "shadow_only": False,
        }
    checkpoint = _mapping(compiled.get("checkpoint"))
    if (
        _text(compiled.get("initial_mode")) != "production"
        or compiled.get("shadow_only") is not False
        or _text(checkpoint.get("state")) != "production_ready"
    ):
        raise RuntimeError("production_initial_checkpoint_mode_boundary_invalid")
    source_revisions = [
        {
            "source_id": _text(row.get("source_id")),
            "source_revision_id": _text(row.get("source_revision_id")),
        }
        for row in joint_weak_likelihood.get("source_expert_trace") or []
        if isinstance(row, Mapping) and _text(row.get("source_revision_id"))
    ]
    weak_snapshot_row = build_weak_forecast_evidence_snapshot_row(
        {
            "ok": True,
            "forecast_evidence": {"source_revisions": source_revisions},
            "joint_weak_likelihood": joint_weak_likelihood,
        },
        target_identity=target_identity,
        event_date=event_date,
        decision_as_of=decision_as_of,
    )
    return {
        **compiled,
        "schema": SCHEMA_VERSION,
        "compiler_state": "candidate_ready",
        "production": True,
        "shadow_only": False,
        "activation_contract_sha256": _text(validated_activation.get("activation_contract_sha256")),
        "weak_forecast_evidence_snapshot_row": weak_snapshot_row,
        "boundary": {
            **dict(_mapping(compiled.get("boundary"))),
            "sqlite_mutation": False,
            "network": False,
            "market_read": False,
            "decision": False,
            "execution": False,
            "places_orders": False,
        },
    }


def rebase_production_initial_posterior_successor(
    candidate: Mapping[str, Any],
    *,
    replay_generation: int,
    supersedes_checkpoint_id: str,
) -> dict[str, Any]:
    """Re-identify a P0 as an append-only successor after a failed cutover.

    The prior failed activation remains auditable.  A retry starts a new root
    generation and replays canonical observations; it never deletes or
    mutates the abandoned generation.
    """

    value = dict(_mapping(candidate))
    checkpoint = dict(_mapping(value.get("checkpoint")))
    generation = int(replay_generation)
    supersedes = _text(supersedes_checkpoint_id)
    if value.get("ok") is not True or generation <= 0 or not supersedes:
        raise ValueError("production_initial_successor_identity_invalid")
    checkpoint["replay_generation"] = generation
    checkpoint["supersedes_checkpoint_id"] = supersedes
    checkpoint["posterior_checkpoint_id"] = build_posterior_checkpoint_id(
        target_identity=_text(checkpoint.get("target_identity")),
        posterior_run_id=_text(checkpoint.get("posterior_run_id")),
        replay_generation=generation,
        joint_posterior_sha256=_text(checkpoint.get("joint_posterior_sha256")),
    )
    value["checkpoint"] = checkpoint
    value["activation_successor"] = {
        "replay_generation": generation,
        "supersedes_checkpoint_id": supersedes,
        "append_only": True,
    }
    return value


__all__ = [
    "SCHEMA_VERSION",
    "compile_production_initial_posterior_checkpoint",
    "rebase_production_initial_posterior_successor",
]
