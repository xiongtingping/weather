"""Pure sequential Bayes filter for the target sequential shadow path.

The strong-evidence model supplies only a conditional raw log score and a
post-state proposal.  This module is the sole owner of centering, hard
conditioning, normalization, posterior delta, and checkpoint construction.
It has no database, network, market, or order dependency.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from lineage_identity_runtime import (
    build_posterior_checkpoint_id,
    build_posterior_run_id,
    build_strong_observation_update_id,
)
from strong_observation_lr_evaluator import evaluate_strong_observation_raw_log_scores
from strong_observation_lr_artifact_contract import build_strong_observation_lr_input
from temperature_trajectory_feature_runtime import (
    build_empty_pre_event_temperature_trajectory_feature_state,
    build_temperature_trajectory_feature_state,
)


SCHEMA_VERSION = "sequential_bayes_filter_shadow_v3"
UPDATE_SCHEMA_VERSION = "strong_observation_update_v3"
CHECKPOINT_SCHEMA_VERSION = "posterior_checkpoint_shadow_v2"
EPSILON = 1e-300


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _event_time_blockers(*, event: Mapping[str, Any], decision_as_of: str, deadline_at: str) -> list[str]:
    valid_at = _parse_time(event.get("valid_time"))
    transport_at = _parse_time(event.get("first_transport_seen_at"))
    event_at = _parse_time(event.get("first_actionable_seen_at"))
    as_of = _parse_time(decision_as_of)
    deadline = _parse_time(deadline_at)
    blockers: list[str] = []
    if valid_at is None:
        blockers.append("canonical_valid_time_missing_or_invalid")
    if transport_at is None:
        blockers.append("canonical_first_transport_seen_at_missing_or_invalid")
    if event_at is None:
        blockers.append("canonical_first_actionable_seen_at_missing_or_invalid")
    if as_of is None:
        blockers.append("decision_as_of_missing_or_invalid")
    if deadline is None:
        blockers.append("decision_deadline_missing_or_invalid")
    if blockers:
        return blockers
    if valid_at > transport_at:
        blockers.append("canonical_valid_time_after_first_transport")
    if valid_at > event_at:
        blockers.append("canonical_valid_time_after_first_actionable")
    if transport_at > event_at:
        blockers.append("canonical_first_transport_seen_at_after_first_actionable")
    if event_at > as_of:
        blockers.append("canonical_event_not_yet_actionable")
    if as_of > deadline:
        blockers.append("decision_deadline_exceeded")
    return blockers


def _joint_rows(checkpoint: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    raw = checkpoint.get("joint_posterior")
    rows: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return rows, ["previous_joint_posterior_missing"]
    for item in raw:
        if not isinstance(item, Mapping):
            return [], ["previous_joint_posterior_row_invalid"]
        temp = _finite(item.get("temperature_c"))
        state = _text(item.get("state"))
        probability = _finite(item.get("probability"))
        if temp is None or not state or probability is None or probability < 0.0:
            return [], ["previous_joint_posterior_row_invalid"]
        rows.append({"temperature_c": temp, "state": state, "probability": probability})
    total = sum(row["probability"] for row in rows)
    if not rows or abs(total - 1.0) > 1e-8:
        return [], ["previous_joint_posterior_not_normalized"]
    return rows, []


def _previous_hash(checkpoint: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    declared = _text(checkpoint.get("joint_posterior_sha256"))
    computed = _sha256(rows)
    return declared if declared == computed else ""


def _event_lower_bound(
    event: Mapping[str, Any], *, speci_soft_evidence_only: bool = False
) -> float | None:
    if speci_soft_evidence_only and _text(event.get("report_type")).upper() == "SPECI":
        return None
    parsed = _mapping(event.get("parsed_observation"))
    direct = _finite(
        event.get("observed_max_lower_bound_c")
        or parsed.get("temperature_interval_lower_c")
        or parsed.get("observed_max_lower_bound_c")
    )
    if direct is not None:
        return direct
    reported = _finite(parsed.get("temperature_c") or event.get("temperature_c"))
    # METAR integer temperatures are intervals; the observed-max lower bound
    # is the conservative lower edge, not the displayed integer as an exact
    # physical truth.
    return reported - 0.5 if reported is not None else None


def _artifact_score(
    artifact: Mapping[str, Any],
    *,
    temperature_c: float,
    state: str,
) -> float | None:
    by_cell = _mapping(artifact.get("raw_log_score_by_temperature_and_pre_state"))
    cell_key = f"{temperature_c:g}|{state}"
    if cell_key in by_cell:
        return _finite(by_cell.get(cell_key))
    by_state = _mapping(artifact.get("raw_log_score_by_pre_state"))
    if state in by_state:
        return _finite(by_state.get(state))
    if "default_raw_log_score" in artifact:
        return _finite(artifact.get("default_raw_log_score"))
    return None


def _transition_matrix(artifact: Mapping[str, Any], states: set[str]) -> tuple[dict[str, dict[str, float]], list[str]]:
    raw = _mapping(artifact.get("post_state_transition"))
    matrix: dict[str, dict[str, float]] = {}
    for state in sorted(states):
        row = _mapping(raw.get(state))
        if not row:
            matrix[state] = {state: 1.0}
            continue
        parsed: dict[str, float] = {}
        for post_state, value in row.items():
            probability = _finite(value)
            if not _text(post_state) or probability is None or probability < 0.0:
                return {}, ["post_state_transition_invalid"]
            parsed[_text(post_state)] = probability
        total = sum(parsed.values())
        if total <= 0.0 or abs(total - 1.0) > 1e-8:
            return {}, ["post_state_transition_not_normalized"]
        matrix[state] = parsed
    return matrix, []


def _artifact_block_reason(artifact: Mapping[str, Any]) -> str:
    for key in artifact:
        lowered = str(key).lower()
        if any(token in lowered for token in ("market", "price", "position", "pnl", "edge", "lcb", "beta_strong")):
            return "strong_artifact_contains_forbidden_input_surface"
    return ""


def _remaining_warming_marginal(
    post_joint: Sequence[Mapping[str, Any]],
    *,
    observed_peak_c: float | None,
    material_new_high_threshold_c: float = 0.5,
) -> tuple[list[dict[str, float]], dict[str, Any]]:
    """Project the same post-update X distribution into Termination Risk.

    This is a marginal read of the composer result, never a second likelihood,
    multiplier, threshold gate, or independent probability model.
    """

    if observed_peak_c is None:
        return [], {
            "schema": "termination_risk_marginal_v1",
            "state": "na",
            "na_reason": "observed_peak_missing",
            "creates_probability": False,
        }
    marginal: dict[float, float] = {}
    no_material_new_high = 0.0
    for raw in post_joint:
        temperature = _finite(raw.get("temperature_c"))
        probability = _finite(raw.get("probability"))
        if temperature is None or probability is None or probability < 0.0:
            continue
        remaining = round(temperature - observed_peak_c, 6)
        marginal[remaining] = marginal.get(remaining, 0.0) + probability
        if remaining < material_new_high_threshold_c - 1e-12:
            no_material_new_high += probability
    rows = [
        {"remaining_warming_c": remaining, "probability": probability}
        for remaining, probability in sorted(marginal.items())
    ]
    risk = min(1.0, max(0.0, no_material_new_high))
    return rows, {
        "schema": "termination_risk_marginal_v1",
        "state": "available",
        "observed_peak_c": observed_peak_c,
        "material_new_high_threshold_c": material_new_high_threshold_c,
        "probability_no_material_new_daily_high": risk,
        "probability_material_new_daily_high": 1.0 - risk,
        "calculation": "sum_post_event_joint_mass_where_X_minus_observed_peak_lt_threshold",
        "same_strong_update_marginal": True,
        "creates_probability": False,
        "updates_posterior": False,
    }


def _blocked_update(*, blockers: Iterable[str], previous_checkpoint: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": UPDATE_SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "previous_posterior_run_id": _text(previous_checkpoint.get("posterior_run_id")),
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "block_reasons": sorted(set(str(item) for item in blockers if str(item))),
        "read_only": True,
    }


def build_strong_observation_update(
    *,
    previous_checkpoint: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    forecast_evidence_snapshot_id: str,
    prior_context_id: str,
    approved_lr_artifact: Mapping[str, Any],
    strong_observation_input: Mapping[str, Any],
    decision_as_of: str,
    deadline_at: str,
    speci_soft_evidence_only: bool = False,
    previous_cumulative_raw_log_score: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one conditional update and its composer-owned centred delta.

    ``strong_observation_input`` is mandatory.  It is the sole path by which
    current D0 temperature, trajectory/momentum, cloud/rain/dewpoint and
    pre-event source reliability may enter the likelihood.  Static score
    tables without that bound input are deliberately not a strong update.
    """

    event = _mapping(canonical_observation_event)
    checkpoint = _mapping(previous_checkpoint)
    artifact = _mapping(approved_lr_artifact)
    rows, blockers = _joint_rows(checkpoint)
    previous_run_id = _text(checkpoint.get("posterior_run_id"))
    previous_checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
    previous_hash = _previous_hash(checkpoint, rows) if rows else ""
    event_id = _text(event.get("canonical_observation_event_id"))
    if not previous_run_id:
        blockers.append("previous_posterior_run_id_missing")
    if not previous_checkpoint_id:
        blockers.append("previous_checkpoint_id_missing")
    if not previous_hash:
        blockers.append("previous_posterior_sha256_missing_or_mismatch")
    if not event_id:
        blockers.append("canonical_observation_event_id_missing")
    if _text(event.get("state")) not in {"accepted_primary", "accepted_substitute"}:
        blockers.append("canonical_event_not_accepted")
    if not _text(forecast_evidence_snapshot_id):
        blockers.append("forecast_evidence_snapshot_id_missing")
    if not _text(prior_context_id):
        blockers.append("prior_context_id_missing")
    artifact_hash = _text(artifact.get("model_artifact_sha256"))
    if not artifact_hash:
        blockers.append("approved_lr_artifact_identity_missing")
    time_blockers = _event_time_blockers(
        event=event,
        decision_as_of=decision_as_of,
        deadline_at=deadline_at,
    )
    blockers.extend(time_blockers)
    consumed = {_text(value) for value in checkpoint.get("consumed_event_ids") or [] if _text(value)}
    if event_id in consumed:
        return {
            "schema": UPDATE_SCHEMA_VERSION,
            "ok": True,
            "state": "neutral",
            "no_posterior_change": True,
            "duplicate_or_confirmation": True,
            "previous_posterior_run_id": previous_run_id,
            "previous_checkpoint_id": previous_checkpoint_id,
            "canonical_observation_event_id": event_id,
            "read_only": True,
        }
    if blockers:
        return _blocked_update(blockers=blockers, previous_checkpoint=checkpoint, event=event)

    states = {row["state"] for row in rows}
    evidence_input = _mapping(strong_observation_input)
    if _text(evidence_input.get("canonical_observation_event_id")) != event_id:
        return _blocked_update(
            blockers=["strong_observation_input_event_binding_mismatch"],
            previous_checkpoint=checkpoint,
            event=event,
        )
    evaluated = evaluate_strong_observation_raw_log_scores(
        approved_lr_artifact=artifact,
        strong_observation_input=evidence_input,
        posterior_cells=rows,
    )
    if evaluated.get("ok") is not True:
        return _blocked_update(
            blockers=list(evaluated.get("block_reasons") or ["strong_observation_likelihood_evaluation_blocked"]),
            previous_checkpoint=checkpoint,
            event=event,
        )
    scoring_kind = _text(artifact.get("scoring_kind"))
    dynamic_transition = evaluated.get("post_state_transition")
    if scoring_kind in {
        "operational_physical_state_aware_lr_v2",
        "operational_physical_state_aware_lr_v3_first_cb_once",
    }:
        if not isinstance(dynamic_transition, Mapping):
            return _blocked_update(
                blockers=["state_aware_event_conditioned_transition_missing"],
                previous_checkpoint=checkpoint,
                event=event,
            )
        transition_source: Mapping[str, Any] = {
            "post_state_transition": dynamic_transition
        }
    else:
        transition_source = artifact
    transition, transition_blockers = _transition_matrix(transition_source, states)
    if transition_blockers:
        return _blocked_update(blockers=transition_blockers, previous_checkpoint=checkpoint, event=event)
    raw_rows = list(evaluated.get("raw_log_score") or [])
    if len(raw_rows) != len(rows):
        return _blocked_update(
            blockers=["strong_observation_likelihood_score_cell_count_mismatch"],
            previous_checkpoint=checkpoint,
            event=event,
        )
    raw_scores: list[float] = []
    for posterior_row, score_row in zip(rows, raw_rows):
        if (
            _finite(score_row.get("temperature_c")) != float(posterior_row["temperature_c"])
            or _text(score_row.get("pre_state")) != _text(posterior_row.get("state"))
        ):
            return _blocked_update(
                blockers=["strong_observation_likelihood_score_cell_binding_mismatch"],
                previous_checkpoint=checkpoint,
                event=event,
            )
        raw_score = _finite(score_row.get("raw_log_score"))
        if raw_score is None:
            return _blocked_update(
                blockers=["raw_log_score_missing_or_non_finite"],
                previous_checkpoint=checkpoint,
                event=event,
            )
        raw_scores.append(raw_score)
    previous_score_rows = list(previous_cumulative_raw_log_score or [])
    previous_scores: list[float] = []
    if previous_score_rows:
        previous_by_cell = {
            (_finite(score_row.get("temperature_c")), _text(score_row.get("pre_state"))): score_row
            for score_row in previous_score_rows
        }
        for posterior_row in rows:
            score_row = previous_by_cell.get(
                (float(posterior_row["temperature_c"]), _text(posterior_row.get("state")))
            )
            if score_row is None:
                return _blocked_update(
                    blockers=["previous_cumulative_likelihood_cell_binding_mismatch"],
                    previous_checkpoint=checkpoint,
                    event=event,
                )
            score = _finite(score_row.get("raw_log_score"))
            if score is None:
                return _blocked_update(
                    blockers=["previous_cumulative_likelihood_score_invalid"],
                    previous_checkpoint=checkpoint,
                    event=event,
                )
            previous_scores.append(score)
    else:
        previous_scores = [0.0 for _ in raw_scores]
    innovation_scores = [
        current - previous
        for current, previous in zip(raw_scores, previous_scores)
    ]
    previous_lb = _finite(checkpoint.get("observed_max_lower_bound_c"))
    event_lb = _event_lower_bound(
        event, speci_soft_evidence_only=speci_soft_evidence_only
    )
    observed_lb = max(value for value in (previous_lb, event_lb) if value is not None) if any(value is not None for value in (previous_lb, event_lb)) else None
    allowed = [observed_lb is None or float(row["temperature_c"]) >= observed_lb - 1e-12 for row in rows]
    unnormalized = [
        float(row["probability"]) * math.exp(score) if allow else 0.0
        for row, score, allow in zip(rows, innovation_scores, allowed)
    ]
    total = sum(unnormalized)
    if total <= 0.0 or not math.isfinite(total):
        return _blocked_update(
            blockers=["hard_condition_or_raw_score_zeroed_all_posterior_mass"],
            previous_checkpoint=checkpoint,
            event=event,
        )
    normalizer_offset = -math.log(total)
    delta_rows: list[dict[str, Any]] = []
    pre_weight: list[float] = []
    for row, innovation_score, allow, weight in zip(rows, innovation_scores, allowed, unnormalized):
        delta = innovation_score + normalizer_offset if allow else None
        delta_rows.append(
            {
                "temperature_c": row["temperature_c"],
                "pre_state": row["state"],
                "posterior_delta": delta,
                "hard_condition_allowed": allow,
            }
        )
        pre_weight.append(weight / total)
    post_mass: dict[tuple[float, str], float] = {}
    for row, probability in zip(rows, pre_weight):
        for post_state, transition_probability in transition[str(row["state"])].items():
            key = (float(row["temperature_c"]), post_state)
            post_mass[key] = post_mass.get(key, 0.0) + probability * transition_probability
    post_joint = [
        {"temperature_c": temp, "state": state, "probability": probability}
        for (temp, state), probability in sorted(post_mass.items())
        if probability > 0.0
    ]
    post_total = sum(row["probability"] for row in post_joint)
    if abs(post_total - 1.0) > 1e-8:
        return _blocked_update(
            blockers=["post_state_transition_not_normalized"],
            previous_checkpoint=checkpoint,
            event=event,
        )
    raw_constant = max(innovation_scores) - min(innovation_scores) <= 1e-12
    hard_changed = any(not value for value in allowed)
    identity_transition = all(matrix == {state: 1.0} for state, matrix in transition.items())
    state = "neutral" if raw_constant and not hard_changed and identity_transition else "applied"
    observed_peak = _finite(
        _mapping(evaluated.get("evidence_feature_values")).get(
            "trajectory.observed_peak_including_current_c"
        )
    )
    remaining_warming_marginal, termination_risk = _remaining_warming_marginal(
        post_joint,
        observed_peak_c=observed_peak,
    )
    update_id = build_strong_observation_update_id(
        previous_posterior_run_id=previous_run_id,
        previous_posterior_sha256=previous_hash,
        canonical_observation_event_id=event_id,
        forecast_evidence_snapshot_id=forecast_evidence_snapshot_id,
        model_artifact_sha256=artifact_hash,
    )
    conditional_innovation_rows = [
        {
            "temperature_c": row["temperature_c"],
            "pre_state": row["state"],
            "log_lr": score,
        }
        for row, score in zip(rows, innovation_scores)
    ]
    return {
        "schema": UPDATE_SCHEMA_VERSION,
        "ok": True,
        "state": state,
        "strong_observation_update_id": update_id,
        "target_identity": _text(checkpoint.get("target_identity")),
        "previous_posterior_run_id": previous_run_id,
        "previous_posterior_sha256": previous_hash,
        "previous_checkpoint_id": previous_checkpoint_id,
        "canonical_observation_event_id": event_id,
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "prior_context_id": _text(prior_context_id),
        "model_artifact_sha256": artifact_hash,
        "decision_as_of": _text(decision_as_of),
        "raw_log_score": raw_rows,
        "raw_log_score_sha256": _text(evaluated.get("raw_log_score_sha256")),
        "likelihood_update_kind": "cumulative_evidence_replacement_delta_log_lr_v1",
        "previous_cumulative_raw_log_score_sha256": _sha256(previous_score_rows) if previous_score_rows else "",
        "conditional_innovation_log_score": conditional_innovation_rows,
        "conditional_innovation_log_score_sha256": _sha256(conditional_innovation_rows),
        "strong_observation_input_sha256": _text(evaluated.get("strong_observation_input_sha256")),
        "strong_evidence_lineage": dict(_mapping(evaluated.get("evidence_lineage"))),
        "strong_evidence_feature_values": dict(_mapping(evaluated.get("evidence_feature_values"))),
        "strong_feature_trace_by_cell": [
            {
                "temperature_c": row.get("temperature_c"),
                "pre_state": row.get("pre_state"),
                "feature_values": dict(_mapping(row.get("feature_values"))),
                "feature_categories": dict(_mapping(row.get("feature_categories"))),
            }
            for row in raw_rows
        ],
        "normalizer_offset": normalizer_offset,
        "posterior_delta": delta_rows,
        "posterior_delta_sha256": _sha256(delta_rows),
        "pre_state_transition": {state: {state: 1.0} for state in sorted(states)},
        "post_state_transition": transition,
        "post_state_transition_trace": dict(
            _mapping(evaluated.get("post_state_transition_trace"))
        ),
        "observed_max_lower_bound_c": observed_lb,
        "hard_condition_trace": {
            "previous_lower_bound_c": previous_lb,
            "event_lower_bound_c": event_lb,
            "applied_lower_bound_c": observed_lb,
            "masked_cell_count": sum(1 for value in allowed if not value),
            "applied_before_normalization": True,
        },
        "remaining_warming_marginal": remaining_warming_marginal,
        "termination_risk": {
            **termination_risk,
            "canonical_observation_event_id": event_id,
            "model_artifact_sha256": artifact_hash,
        },
        "quality": {
            "canonical_event_state": _text(event.get("state")),
            "selection_role": _text(event.get("selection_role")),
            "quality_flags": list(event.get("quality_flags") or []),
        },
        "block_reasons": [],
        "candidate_post_joint": post_joint,
        "read_only": True,
    }


def build_next_checkpoint(
    *,
    previous_checkpoint: Mapping[str, Any],
    strong_update: Mapping[str, Any],
    fact_snapshot_id: str,
    resolution_contract_id: str,
    committed_at: str,
    replay_generation: int = 0,
) -> dict[str, Any]:
    """Create a shadow checkpoint from one already-centered update."""

    update = _mapping(strong_update)
    previous = _mapping(previous_checkpoint)
    if not bool(update.get("ok")) or _text(update.get("state")) == "blocked":
        return {"schema": CHECKPOINT_SCHEMA_VERSION, "ok": False, "reason": "strong_update_blocked", "read_only": True}
    if bool(update.get("no_posterior_change")):
        return {"schema": CHECKPOINT_SCHEMA_VERSION, "ok": True, "reused_previous": True, "checkpoint": dict(previous), "read_only": True}
    post_joint = update.get("candidate_post_joint")
    if not isinstance(post_joint, list) or not post_joint:
        return {"schema": CHECKPOINT_SCHEMA_VERSION, "ok": False, "reason": "candidate_post_joint_missing", "read_only": True}
    previous_run_id = _text(previous.get("posterior_run_id"))
    event_id = _text(update.get("canonical_observation_event_id"))
    consumed = [
        _text(value)
        for value in previous.get("consumed_event_ids") or []
        if _text(value)
    ]
    consumed.append(event_id)
    posterior_hash = _sha256(post_joint)
    posterior_run_id = build_posterior_run_id(
        slug=_text(previous.get("target_identity")),
        event_date=_text(previous.get("event_date")),
        fact_snapshot_id=_text(fact_snapshot_id),
        # A P1 incremental run is identified by its full canonical-observation
        # sequence.  Do not use transient caller ``source_event_ids`` here:
        # that field was not persisted and would make a restart or a second
        # update silently drop earlier events from the run identity.
        source_event_ids=consumed,
        posterior_hash=posterior_hash,
        phase="shadow_incremental",
    )
    checkpoint_id = build_posterior_checkpoint_id(
        target_identity=_text(previous.get("target_identity")),
        posterior_run_id=posterior_run_id,
        replay_generation=int(replay_generation),
        joint_posterior_sha256=posterior_hash,
    )
    state_probs: dict[str, float] = {}
    physical_distribution: dict[str, float] = {}
    for row in post_joint:
        state_probs[str(row["state"])] = state_probs.get(str(row["state"]), 0.0) + float(row["probability"])
        temperature_key = f"{float(row['temperature_c']):g}"
        physical_distribution[temperature_key] = physical_distribution.get(temperature_key, 0.0) + float(row["probability"])
    checkpoint = {
        "schema": CHECKPOINT_SCHEMA_VERSION,
        "ok": True,
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": _text(previous.get("target_identity")),
        "event_date": _text(previous.get("event_date")),
        "posterior_run_id": posterior_run_id,
        "previous_posterior_run_id": previous_run_id,
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "checkpoint_kind": "incremental" if int(replay_generation) == 0 else "replay",
        "last_strong_update_id": _text(update.get("strong_observation_update_id")),
        "last_canonical_event_id": event_id,
        "joint_posterior": post_joint,
        "joint_posterior_sha256": posterior_hash,
        "physical_high_distribution": physical_distribution,
        "latent_post_state_distribution": state_probs,
        "observed_max_lower_bound_c": update.get("observed_max_lower_bound_c"),
        "remaining_warming_marginal": list(update.get("remaining_warming_marginal") or []),
        "termination_risk": dict(_mapping(update.get("termination_risk"))),
        "consumed_event_ids": consumed,
        "source_event_ids": list(consumed),
        "consumed_ancestor_sha256": _sha256(consumed),
        "forecast_evidence_snapshot_id": _text(update.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(update.get("prior_context_id")),
        "resolution_contract_id": _text(resolution_contract_id),
        "model_artifact_sha256": _text(update.get("model_artifact_sha256")),
        "replay_generation": int(replay_generation),
        "state": "shadow_ready",
        "created_at": _text(committed_at),
        "committed_at": _text(committed_at),
        "read_only": True,
    }
    return checkpoint


def replay_canonical_sequence(
    *,
    initial_checkpoint: Mapping[str, Any],
    canonical_events: Iterable[Mapping[str, Any]],
    forecast_evidence_snapshot_id: str,
    prior_context_id: str,
    approved_lr_artifact: Mapping[str, Any],
    decision_as_of: str,
    deadline_at: str,
    fact_snapshot_id_prefix: str,
    resolution_contract_id: str,
    source_reliability_pre_state: Mapping[str, Any] | None = None,
    temperature_quantization_c: float = 1.0,
) -> dict[str, Any]:
    """Full replay ordered by valid-time, then first actionable time, then ID."""

    events = [dict(event) for event in canonical_events if isinstance(event, Mapping)]
    events.sort(key=lambda event: (_text(event.get("valid_time")), _text(event.get("first_actionable_seen_at")), _text(event.get("canonical_observation_event_id"))))
    checkpoint = dict(initial_checkpoint)
    updates: list[dict[str, Any]] = []
    previous_cumulative_raw_log_score: list[Mapping[str, Any]] = []
    for sequence, event in enumerate(events, start=1):
        pre_events = events[: sequence - 1]
        if pre_events:
            trajectory = build_temperature_trajectory_feature_state(
                canonical_events=pre_events,
                decision_as_of=decision_as_of,
                temperature_quantization_c=temperature_quantization_c,
                temporal_role="pre_event",
            )
        else:
            trajectory = build_empty_pre_event_temperature_trajectory_feature_state(
                target_identity=_text(event.get("target_identity")),
                station_id=_text(event.get("station_id")),
                decision_as_of=decision_as_of,
                temperature_quantization_c=temperature_quantization_c,
            )
        if trajectory.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "updates": updates,
                "reason": "replay_trajectory_blocked",
                "trajectory_feature_state": trajectory,
                "read_only": True,
            }
        strong_input = build_strong_observation_lr_input(
            canonical_observation_event=event,
            trajectory_feature_state=trajectory,
            forecast_evidence_snapshot_id=forecast_evidence_snapshot_id,
            prior_context_id=prior_context_id,
            source_reliability_pre_state=_mapping(source_reliability_pre_state),
        )
        if strong_input.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "updates": updates,
                "reason": "replay_strong_observation_input_blocked",
                "strong_observation_input": strong_input,
                "read_only": True,
            }
        update = build_strong_observation_update(
            previous_checkpoint=checkpoint,
            canonical_observation_event=event,
            forecast_evidence_snapshot_id=forecast_evidence_snapshot_id,
            prior_context_id=prior_context_id,
            approved_lr_artifact=approved_lr_artifact,
            strong_observation_input=strong_input,
            decision_as_of=decision_as_of,
            deadline_at=deadline_at,
            previous_cumulative_raw_log_score=previous_cumulative_raw_log_score,
        )
        updates.append(update)
        if not update.get("ok"):
            return {"schema": SCHEMA_VERSION, "ok": False, "updates": updates, "reason": "replay_update_blocked", "read_only": True}
        next_checkpoint = build_next_checkpoint(
            previous_checkpoint=checkpoint,
            strong_update=update,
            fact_snapshot_id=f"{_text(fact_snapshot_id_prefix)}:{sequence}",
            resolution_contract_id=resolution_contract_id,
            committed_at=decision_as_of,
            replay_generation=1,
        )
        if not next_checkpoint.get("ok"):
            return {"schema": SCHEMA_VERSION, "ok": False, "updates": updates, "reason": "replay_checkpoint_blocked", "read_only": True}
        checkpoint = dict(next_checkpoint.get("checkpoint") or next_checkpoint)
        previous_cumulative_raw_log_score = list(update.get("raw_log_score") or [])
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "updates": updates,
        "checkpoint": checkpoint,
        "canonical_sequence_sha256": _sha256([_text(event.get("canonical_observation_event_id")) for event in events]),
        "read_only": True,
    }
