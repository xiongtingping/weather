"""Forward-only weak-evidence replacement for the production v6 successor."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from physical_state_bayes_v6.contracts import DurationClocks, JointCell, PostEventState
from physical_state_bayes_v6.core import project_reported_x
from production_probability_v6_activation_contract import CHECKPOINT_SCHEMA, validate_v6_activation


SCHEMA_VERSION = "production_probability_v6_weak_revision_candidate_v1"
UPDATE_SCHEMA = "weak_forecast_revision_update_v2_physical_state_bayes_v6"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse(value: Any, expected: type) -> Any:
    if isinstance(value, expected):
        return value
    parsed = json.loads(_text(value))
    if not isinstance(parsed, expected):
        raise ValueError("v6_weak_revision_json_shape_invalid")
    return parsed


def _positive_surface(snapshot: Mapping[str, Any]) -> dict[float, float]:
    raw = _parse(snapshot.get("likelihood_by_x_json"), dict)
    result: dict[float, float] = {}
    for key, value in raw.items():
        y_c = float(key)
        likelihood = float(value)
        if not math.isfinite(y_c) or not math.isfinite(likelihood) or likelihood <= 0.0:
            raise ValueError("v6_weak_revision_surface_invalid")
        result[y_c] = likelihood
    if not result:
        raise ValueError("v6_weak_revision_surface_missing")
    return result


def _joint(checkpoint: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = _parse(checkpoint.get("joint_posterior_json"), list)
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("v6_weak_revision_joint_invalid")
        y_c = float(item.get("continuous_y_c"))
        state = _text(item.get("state"))
        probability = float(item.get("probability"))
        if not state or not math.isfinite(y_c) or probability < 0.0 or not math.isfinite(probability):
            raise ValueError("v6_weak_revision_joint_invalid")
        rows.append({"continuous_y_c": y_c, "state": state, "probability": probability})
    if not rows or abs(sum(row["probability"] for row in rows) - 1.0) > 1e-8:
        raise ValueError("v6_weak_revision_joint_not_normalized")
    return rows


def compile_v6_weak_revision_candidate(
    *,
    activation: Mapping[str, Any],
    previous_checkpoint: Mapping[str, Any],
    previous_snapshot: Mapping[str, Any],
    revised_snapshot: Mapping[str, Any],
    trigger_source_id: str,
    trigger_source_revision_id: str,
    revision_kind: str,
    decision_as_of: str,
) -> dict[str, Any]:
    """Replace weak evidence at the current checkpoint without replaying past transitions."""

    authorized = validate_v6_activation(activation)
    previous = dict(previous_checkpoint)
    old = dict(previous_snapshot)
    new = dict(revised_snapshot)
    if _text(previous.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA:
        raise ValueError("v6_weak_revision_checkpoint_schema_invalid")
    if revision_kind not in {"original", "amendment", "correction"}:
        raise ValueError("v6_weak_revision_kind_invalid")
    target = _text(previous.get("target_identity"))
    old_id = _text(old.get("forecast_evidence_snapshot_id"))
    new_id = _text(new.get("forecast_evidence_snapshot_id"))
    if (
        not target
        or _text(previous.get("forecast_evidence_snapshot_id")) != old_id
        or _text(old.get("target_identity")) != target
        or _text(new.get("target_identity")) != target
        or not new_id
        or new_id == old_id
    ):
        raise ValueError("v6_weak_revision_lineage_mismatch")
    artifact = _text(new.get("weak_likelihood_artifact_sha256"))
    if artifact != _text(authorized.get("weak_likelihood_artifact_sha256")) or artifact != _text(
        old.get("weak_likelihood_artifact_sha256")
    ):
        raise ValueError("v6_weak_revision_artifact_mismatch")
    forcing_sha = _text(new.get("forecast_scenario_paths_sha256"))
    responsibility_sha = _text(new.get("candidate_scenario_responsibilities_sha256"))
    if len(forcing_sha) != 64 or len(responsibility_sha) != 64:
        raise ValueError("v6_weak_revision_future_control_lineage_missing")

    rows = _joint(previous)
    old_surface = _positive_surface(old)
    new_surface = _positive_surface(new)
    support = {row["continuous_y_c"] for row in rows}
    if set(old_surface) != set(new_surface) or not support.issubset(old_surface):
        raise ValueError("v6_weak_revision_y_support_mismatch")
    log_weights: list[float] = []
    replacement_lr: dict[str, float] = {}
    for y_c in sorted(support):
        replacement_lr[f"{y_c:g}"] = math.log(new_surface[y_c] / old_surface[y_c])
    for row in rows:
        probability = row["probability"]
        log_weights.append(
            -math.inf if probability == 0.0 else math.log(probability) + replacement_lr[f"{row['continuous_y_c']:g}"]
        )
    finite = [value for value in log_weights if math.isfinite(value)]
    if not finite:
        raise ValueError("v6_weak_revision_eliminated_all_mass")
    anchor = max(finite)
    weights = [0.0 if not math.isfinite(value) else math.exp(value - anchor) for value in log_weights]
    normalizer = sum(weights)
    posterior = [
        {**row, "probability": weight / normalizer}
        for row, weight in zip(rows, weights, strict=True)
    ]
    # The replacement ratio depends only on Y, so P(Z|Y) is preserved exactly.
    joint_sha = _sha(posterior)
    x_distribution = project_reported_x(
        PostEventState(
            schema="physical_state_bayes_v6_weak_revision_projection_v1",
            event_id=_text(previous.get("last_canonical_event_id")),
            pre_event_prediction_sha256=_text(previous.get("pre_event_prediction_sha256")),
            joint=tuple(
                JointCell(r["continuous_y_c"], r["state"], r["probability"])
                for r in posterior
            ),
            observed_reported_regular_max_c=previous.get("reported_x_lower_bound_c"),
            y_lower_bound_c=previous.get("latent_observed_lower_bound_c"),
            duration_clocks_after=DurationClocks(),
            observation_artifact_sha256=_text(previous.get("observation_artifact_sha256")),
            hard_y_lower_bound_c=previous.get("latent_observed_lower_bound_c"),
            normalizer_log=math.log(normalizer) + anchor,
            invariant_report={"weak_revision_projection_only": True},
        )
    )
    y_marginal: dict[str, float] = {}
    state_marginal: dict[str, float] = {}
    for row in posterior:
        y_key = f"{row['continuous_y_c']:g}"
        y_marginal[y_key] = y_marginal.get(y_key, 0.0) + row["probability"]
        state_marginal[row["state"]] = state_marginal.get(row["state"], 0.0) + row["probability"]
    identity = {
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "revised_forecast_evidence_snapshot_id": new_id,
        "trigger_source_revision_id": _text(trigger_source_revision_id),
        "joint_posterior_sha256": joint_sha,
    }
    update_id = "weak-v6-" + _sha(identity)
    run_id = "posterior-v6-weak-" + _sha({**identity, "update_id": update_id})
    checkpoint_id = "checkpoint-v6-weak-" + _sha({"posterior_run_id": run_id})
    checkpoint = {**previous}
    checkpoint.update(
        {
            "posterior_checkpoint_id": checkpoint_id,
            "posterior_run_id": run_id,
            "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
            "checkpoint_kind": "incremental",
            "forecast_evidence_snapshot_id": new_id,
            "joint_posterior_json": _json(posterior),
            "joint_posterior_sha256": joint_sha,
            "continuous_y_joint_sha256": joint_sha,
            "physical_high_distribution_json": _json(y_marginal),
            "latent_scheduled_slot_high_distribution_json": _json(y_marginal),
            "latent_post_state_json": _json(state_marginal),
            "reported_physical_x_distribution_json": _json({str(k): v for k, v in x_distribution.items()}),
            "reported_x_projection_sha256": _sha(x_distribution),
            "forcing_generation_sha256": forcing_sha,
            "scenario_control_lineage_sha256": responsibility_sha,
            "state": "production_successor_ready",
            "created_at": _text(decision_as_of),
            "committed_at": _text(decision_as_of),
        }
    )
    update = {
        "weak_forecast_revision_update_id": update_id,
        "target_identity": target,
        "previous_posterior_run_id": _text(previous.get("posterior_run_id")),
        "previous_posterior_sha256": _text(previous.get("joint_posterior_sha256")),
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "successor_checkpoint_id": checkpoint_id,
        "previous_forecast_evidence_snapshot_id": old_id,
        "revised_forecast_evidence_snapshot_id": new_id,
        "trigger_source_id": _text(trigger_source_id),
        "trigger_source_revision_id": _text(trigger_source_revision_id),
        "revision_kind": revision_kind,
        "weak_likelihood_artifact_sha256": artifact,
        "decision_as_of": _text(decision_as_of),
        "replacement_log_lr_by_x_json": _json(replacement_lr),
        "replacement_log_lr_sha256": _sha(replacement_lr),
        "previous_weak_likelihood_sha256": _text(old.get("weak_likelihood_sha256")),
        "revised_weak_likelihood_sha256": _text(new.get("weak_likelihood_sha256")),
        "posterior_delta_json": _json({}),
        "posterior_delta_sha256": _sha({}),
        "state": "applied",
        "quality_json": _json(
            {
                "forecast_only": True,
                "replacement_not_independent_multiplication": True,
                "past_state_transitions_replayed": False,
                "current_observation_consumed": False,
            }
        ),
        "block_reasons_json": "[]",
        "created_at": _text(decision_as_of),
        "probability_update_schema": UPDATE_SCHEMA,
        "previous_forcing_generation_sha256": _text(previous.get("forcing_generation_sha256")),
        "revised_forcing_generation_sha256": forcing_sha,
        "scenario_control_lineage_sha256": responsibility_sha,
        "no_retroactive_state_transition": 1,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_successor_weak_revision_candidate_ready",
        "snapshot_row": new,
        "update_row": update,
        "checkpoint_row": checkpoint,
        "reported_physical_x_distribution": x_distribution,
        "past_state_transitions_replayed": False,
        "future_forcing_replaced": True,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "UPDATE_SCHEMA", "compile_v6_weak_revision_candidate"]
