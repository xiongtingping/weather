"""Pure P0 compiler for the report-defined-X successor activation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from initial_posterior_checkpoint_shadow import build_initial_posterior_checkpoint_for_mode
from lineage_identity_runtime import (
    build_fact_snapshot_id,
    build_posterior_checkpoint_id,
    build_posterior_run_id,
)
from production_probability_successor_activation_contract import (
    validate_successor_activation,
)
from reported_x_successor_checkpoint_runtime import upgrade_initial_candidate
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row


SCHEMA_VERSION = "production_initial_posterior_checkpoint_compiler_v2_reported_x"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def compile_reported_x_initial_checkpoint(
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
    authorized = validate_successor_activation(activation)
    if resolution_contract.get("bridge_closed") is True:
        raise ValueError("reported_x_initial_closed_bridge_requires_native_successor_projection")
    for value, label in (
        (prior_context, "prior"),
        (joint_weak_likelihood, "weak"),
        (active_strong_lr_artifact, "strong_artifact"),
    ):
        if value.get("production_runtime_eligible") is not True or value.get("shadow_only") is not False:
            raise ValueError(f"reported_x_initial_{label}_not_production_eligible")
    if _text(active_strong_lr_artifact.get("model_artifact_sha256")) != _text(
        authorized.get("strong_lr_artifact_sha256")
    ):
        raise ValueError("reported_x_initial_activation_artifact_mismatch")
    if _text(prior_context.get("prior_model_artifact_sha256")) != _text(
        authorized.get("prior_model_artifact_sha256")
    ):
        raise ValueError("reported_x_initial_activation_prior_artifact_mismatch")
    if _text(joint_weak_likelihood.get("artifact_sha256")) != _text(
        authorized.get("weak_likelihood_artifact_sha256")
    ):
        raise ValueError("reported_x_initial_activation_weak_artifact_mismatch")
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
    upgraded = upgrade_initial_candidate(
        compiled,
        resolution_contract=resolution_contract,
    )
    if upgraded.get("ok") is not True:
        return {
            **upgraded,
            "schema": SCHEMA_VERSION,
            "compiler_state": "blocked",
            "production": True,
            "shadow_only": False,
        }
    checkpoint = dict(_mapping(upgraded.get("checkpoint")))
    lineage = dict(_mapping(upgraded.get("initial_lineage")))
    lineage.update(
        {
            "projection_schema": _text(authorized.get("projection_schema")),
            "latent_type": _text(authorized.get("latent_type")),
            "target_type": _text(authorized.get("target_type")),
            "reported_x_projection_sha256": _text(
                checkpoint.get("reported_x_projection_sha256")
            ),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "public_ssot_schema": _text(authorized.get("public_ssot_schema")),
        }
    )
    evidence_sha = _sha(lineage)
    fact_snapshot_id = build_fact_snapshot_id(
        slug=target_identity,
        event_date=event_date,
        fact_as_of=decision_as_of,
        source_event_ids=(),
        identity_salt={
            "initial_evidence_sha256": evidence_sha,
            "activation_contract_sha256": _text(
                authorized.get("activation_contract_sha256")
            ),
        },
    )
    posterior_run_id = build_posterior_run_id(
        slug=target_identity,
        event_date=event_date,
        fact_snapshot_id=fact_snapshot_id,
        source_event_ids=(),
        posterior_hash=_text(checkpoint.get("joint_posterior_sha256")),
        phase="production_initial_prior_weak_reported_x",
    )
    checkpoint["posterior_run_id"] = posterior_run_id
    checkpoint["posterior_checkpoint_id"] = build_posterior_checkpoint_id(
        target_identity=target_identity,
        posterior_run_id=posterior_run_id,
        replay_generation=int(checkpoint.get("replay_generation") or 0),
        joint_posterior_sha256=_text(checkpoint.get("joint_posterior_sha256")),
    )
    checkpoint["initial_evidence_snapshot_id"] = fact_snapshot_id
    checkpoint["initial_evidence_sha256"] = evidence_sha
    lineage["initial_evidence_snapshot_id"] = fact_snapshot_id
    lineage["initial_evidence_sha256"] = evidence_sha
    lineage["joint_posterior_sha256"] = _text(checkpoint.get("joint_posterior_sha256"))
    upgraded["checkpoint"] = checkpoint
    upgraded["initial_lineage"] = lineage
    source_revisions = [
        {
            "source_id": _text(row.get("source_id")),
            "source_revision_id": _text(row.get("source_revision_id")),
        }
        for row in joint_weak_likelihood.get("source_expert_trace") or []
        if isinstance(row, Mapping) and _text(row.get("source_revision_id"))
    ]
    weak_snapshot = build_weak_forecast_evidence_snapshot_row(
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
        **upgraded,
        "schema": SCHEMA_VERSION,
        "compiler_state": "candidate_ready",
        "production": True,
        "shadow_only": False,
        "activation_contract_sha256": _text(
            authorized.get("activation_contract_sha256")
        ),
        "weak_forecast_evidence_snapshot_row": weak_snapshot,
        "boundary": {
            **dict(_mapping(upgraded.get("boundary"))),
            "sqlite_mutation": False,
            "network": False,
            "market_read": False,
            "decision": False,
            "execution": False,
            "places_orders": False,
        },
    }


def rebase_reported_x_initial_successor(
    candidate: Mapping[str, Any],
    *,
    replay_generation: int,
    supersedes_checkpoint_id: str,
) -> dict[str, Any]:
    value = dict(_mapping(candidate))
    checkpoint = dict(_mapping(value.get("checkpoint")))
    generation = int(replay_generation)
    supersedes = _text(supersedes_checkpoint_id)
    if value.get("ok") is not True or generation <= 0 or not supersedes:
        raise ValueError("reported_x_initial_successor_identity_invalid")
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
    "compile_reported_x_initial_checkpoint",
    "rebase_reported_x_initial_successor",
]
