"""Compile a Bayes-coherent, orderless weak-forecast revision transaction."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from lineage_identity_runtime import build_fact_snapshot_id, build_posterior_checkpoint_id, build_posterior_run_id
from weak_forecast_revision_bayes_runtime import apply_weak_revision_ratio_to_joint_rows


SCHEMA_VERSION = "weak_forecast_revision_transaction_candidate_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _parsed(value: Any, expected: type) -> Any:
    if isinstance(value, expected):
        return value
    parsed = json.loads(_text(value))
    if not isinstance(parsed, expected):
        raise ValueError("weak_revision_persisted_json_shape_invalid")
    return parsed


def compile_weak_forecast_revision_candidate(
    *,
    previous_checkpoint_row: Mapping[str, Any],
    previous_snapshot_row: Mapping[str, Any],
    revised_snapshot_row: Mapping[str, Any],
    trigger_source_id: str,
    trigger_source_revision_id: str,
    revision_kind: str,
    public_slug: str,
    decision_as_of: str,
    replay_generation: int,
) -> dict[str, Any]:
    previous = dict(previous_checkpoint_row)
    old = dict(previous_snapshot_row)
    new = dict(revised_snapshot_row)
    target = _text(previous.get("target_identity"))
    event_date = _text(new.get("event_date"))[:10]
    if revision_kind not in {"original", "amendment", "correction"}:
        raise ValueError("weak_forecast_revision_kind_invalid")
    if (
        not target
        or _text(old.get("target_identity")) != target
        or _text(new.get("target_identity")) != target
        or _text(previous.get("forecast_evidence_snapshot_id")) != _text(old.get("forecast_evidence_snapshot_id"))
        or _text(old.get("weak_likelihood_artifact_sha256")) != _text(new.get("weak_likelihood_artifact_sha256"))
    ):
        raise ValueError("weak_forecast_revision_lineage_mismatch")
    joint = _parsed(previous.get("joint_posterior_json"), list)
    old_likelihood = _parsed(old.get("likelihood_by_x_json"), dict)
    new_likelihood = _parsed(new.get("likelihood_by_x_json"), dict)
    applied = apply_weak_revision_ratio_to_joint_rows(
        joint,
        previous_weak_likelihood_by_x=old_likelihood,
        revised_weak_likelihood_by_x=new_likelihood,
    )
    posterior = applied["posterior_joint"]
    physical: dict[str, float] = {}
    latent: dict[str, float] = {}
    old_physical = _parsed(previous.get("physical_high_distribution_json"), dict)
    for row in posterior:
        x = f"{float(row['temperature_c']):g}"
        state = _text(row.get("state"))
        probability = float(row["probability"])
        physical[x] = physical.get(x, 0.0) + probability
        latent[state] = latent.get(state, 0.0) + probability
    posterior_delta = {
        key: physical.get(key, 0.0) - float(old_physical.get(key, 0.0))
        for key in sorted(set(old_physical) | set(physical), key=float)
    }
    posterior_sha = _sha(posterior)
    revision_identity = {
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "revised_forecast_evidence_snapshot_id": _text(new.get("forecast_evidence_snapshot_id")),
        "trigger_source_revision_id": _text(trigger_source_revision_id),
    }
    update_id = "weakupd_" + _sha(revision_identity)[:24]
    fact_snapshot_id = build_fact_snapshot_id(
        slug=_text(public_slug),
        event_date=event_date,
        fact_as_of=_text(decision_as_of),
        source_event_ids=[_text(trigger_source_revision_id)],
        identity_salt=revision_identity,
    )
    posterior_run_id = build_posterior_run_id(
        slug=target,
        event_date=event_date,
        fact_snapshot_id=fact_snapshot_id,
        source_event_ids=[
            *_parsed(previous.get("consumed_event_ids_json"), list),
            _text(trigger_source_revision_id),
        ],
        posterior_hash=posterior_sha,
        phase="production_weak_forecast_revision",
    )
    checkpoint_id = build_posterior_checkpoint_id(
        target_identity=target,
        posterior_run_id=posterior_run_id,
        replay_generation=int(replay_generation),
        joint_posterior_sha256=posterior_sha,
    )
    replacement_lr = applied["weak_revision_log_lr_by_x"]
    update_row = {
        "weak_forecast_revision_update_id": update_id,
        "target_identity": target,
        "previous_posterior_run_id": _text(previous.get("posterior_run_id")),
        "previous_posterior_sha256": _text(previous.get("joint_posterior_sha256")),
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "successor_checkpoint_id": checkpoint_id,
        "previous_forecast_evidence_snapshot_id": _text(old.get("forecast_evidence_snapshot_id")),
        "revised_forecast_evidence_snapshot_id": _text(new.get("forecast_evidence_snapshot_id")),
        "trigger_source_id": _text(trigger_source_id),
        "trigger_source_revision_id": _text(trigger_source_revision_id),
        "revision_kind": revision_kind,
        "weak_likelihood_artifact_sha256": _text(new.get("weak_likelihood_artifact_sha256")),
        "decision_as_of": _text(decision_as_of),
        "replacement_log_lr_by_x_json": _json(replacement_lr),
        "replacement_log_lr_sha256": _sha(replacement_lr),
        "previous_weak_likelihood_sha256": _text(old.get("weak_likelihood_sha256")),
        "revised_weak_likelihood_sha256": _text(new.get("weak_likelihood_sha256")),
        "posterior_delta_json": _json(posterior_delta),
        "posterior_delta_sha256": _sha(posterior_delta),
        "state": "neutral" if all(abs(value) < 1e-15 for value in posterior_delta.values()) else "applied",
        "quality_json": _json({"forecast_only": True, "replacement_not_independent_multiplication": True}),
        "block_reasons_json": "[]",
        "created_at": _text(decision_as_of),
    }
    checkpoint = {**previous}
    checkpoint.update({
        "posterior_checkpoint_id": checkpoint_id,
        "posterior_run_id": posterior_run_id,
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "checkpoint_kind": "incremental",
        "forecast_evidence_snapshot_id": _text(new.get("forecast_evidence_snapshot_id")),
        "joint_posterior_json": _json(posterior),
        "joint_posterior_sha256": posterior_sha,
        "physical_high_distribution_json": _json(physical),
        "latent_post_state_json": _json(latent),
        "state": "production_ready",
        "created_at": _text(decision_as_of),
        "committed_at": _text(decision_as_of),
    })
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "candidate_ready",
        "previous_checkpoint_id": update_row["previous_checkpoint_id"],
        "snapshot_row": new,
        "update_row": update_row,
        "checkpoint_row": checkpoint,
        "fact_snapshot_id": fact_snapshot_id,
        "public_slug": _text(public_slug),
        "event_date": event_date,
        "decision_as_of": _text(decision_as_of),
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "compile_weak_forecast_revision_candidate"]
