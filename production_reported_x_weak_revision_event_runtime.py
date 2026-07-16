"""TAF/AMD weak-revision ingress for the reported-X successor epoch."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from independent_weak_live_source_runtime import load_live_independent_weak_source_observations
from joint_weak_live_evidence_runtime import (
    bind_live_joint_weak_likelihood_for_production,
    build_live_joint_weak_likelihood,
)
from production_probability_successor_activation_contract import validate_successor_activation
from production_probability_successor_runtime_manifest import successor_runtime_manifest_sha256
from production_reported_x_weak_revision_writer import commit_reported_x_weak_forecast_revision
from taf_revision_candidate_selection_runtime import (
    dedupe_source_observations,
    select_new_taf_revision_candidates,
)
from reported_x_successor_checkpoint_runtime import (
    upgrade_weak_revision_candidate,
    validate_successor_checkpoint_row,
)
from sequential_bayes_shadow_pipeline import load_canonical_observation_sequence_asof
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row
from weak_forecast_revision_transaction_runtime import compile_weak_forecast_revision_candidate


SCHEMA_VERSION = "production_reported_x_weak_revision_event_runtime_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _blocked(reason: str, **detail: Any) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": reason, **detail, "places_orders": False}


def apply_reported_x_taf_revision_from_source_observations(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    source_observations: Iterable[Mapping[str, Any]],
    decision_as_of: str,
    public_slug: str,
) -> dict[str, Any]:
    """Replace the weak forecast evidence once, then republish integer X."""

    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    incoming = [dict(row) for row in source_observations if isinstance(row, Mapping)]
    candidates = select_new_taf_revision_candidates(incoming, event_date=day, decision_as_of=decision_as_of)
    if not candidates:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "no_new_taf_revision", "places_orders": False}
    try:
        authorized = validate_successor_activation(activation)
    except ValueError as exc:
        return _blocked(f"reported_x_weak_revision_activation_rejected:{exc}")
    current_manifest = successor_runtime_manifest_sha256()
    if _text(authorized.get("runtime_manifest_sha256")) != current_manifest:
        return _blocked("reported_x_weak_revision_runtime_manifest_drift")
    conn.row_factory = sqlite3.Row
    generations = conn.execute(
        """SELECT p.replay_generation FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
        (target, _text(authorized.get("activation_contract_sha256"))),
    ).fetchall()
    if len(generations) != 1:
        return _blocked("reported_x_current_activation_p0_lineage_not_unique")
    generation = int(generations[0][0] or 0)
    checkpoint_record = conn.execute(
        """SELECT * FROM posterior_checkpoints WHERE target_identity=? AND replay_generation=?
           AND state='committed' ORDER BY rowid DESC LIMIT 1""",
        (target, generation),
    ).fetchone()
    if checkpoint_record is None:
        return _blocked("reported_x_current_checkpoint_missing")
    checkpoint = dict(checkpoint_record)
    validation = validate_successor_checkpoint_row(checkpoint)
    if validation.get("ok") is not True:
        return _blocked("reported_x_current_checkpoint_invalid", issues=validation.get("issues") or [])
    old_snapshot_record = conn.execute(
        "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
        (_text(checkpoint.get("forecast_evidence_snapshot_id")),),
    ).fetchone()
    if old_snapshot_record is None:
        return _blocked("reported_x_current_weak_snapshot_missing")
    old_snapshot = dict(old_snapshot_record)
    independent = load_live_independent_weak_source_observations(event_date=day)
    combined = dedupe_source_observations(
        [*(independent.get("source_observations") or []), *incoming]
    )
    grid = [float(item) for item in json.loads(_text(old_snapshot.get("temperature_grid_json")))]
    weak = bind_live_joint_weak_likelihood_for_production(
        build_live_joint_weak_likelihood(
            temperature_grid_c=grid,
            event_date=day,
            decision_as_of=_text(decision_as_of),
            source_observations=combined,
        )
    )
    if weak.get("ok") is not True:
        return _blocked("reported_x_revised_joint_weak_likelihood_unavailable", detail=weak)
    snapshot = build_weak_forecast_evidence_snapshot_row(
        weak, target_identity=target, event_date=day, decision_as_of=_text(decision_as_of)
    )
    if _text(snapshot.get("forecast_evidence_snapshot_id")) == _text(old_snapshot.get("forecast_evidence_snapshot_id")):
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "revision_already_visible", "places_orders": False}
    trigger = candidates[-1]
    payload = _mapping(trigger.get("payload"))
    trigger_id = _text(payload.get("source_revision_id") or trigger.get("source_event_id"))
    revision_kind = _text(payload.get("forecast_revision_kind")) or "original"
    base = compile_weak_forecast_revision_candidate(
        previous_checkpoint_row=checkpoint,
        previous_snapshot_row=old_snapshot,
        revised_snapshot_row=snapshot,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id=trigger_id,
        revision_kind=revision_kind,
        public_slug=_text(public_slug),
        decision_as_of=_text(decision_as_of),
        replay_generation=generation,
    )
    sequence = load_canonical_observation_sequence_asof(
        conn, target_identity=target, decision_as_of=_text(decision_as_of)
    )
    if sequence.get("ok") is not True:
        return _blocked("reported_x_canonical_sequence_unavailable")
    try:
        consumed_ids = {
            _text(item)
            for item in json.loads(_text(checkpoint.get("consumed_event_ids_json")))
            if _text(item)
        }
    except (TypeError, ValueError):
        return _blocked("reported_x_consumed_event_ids_invalid")
    projection_events = [
        item
        for item in (sequence.get("canonical_observation_events") or [])
        if _text(_mapping(item).get("canonical_observation_event_id")) in consumed_ids
    ]
    if len(projection_events) != len(consumed_ids):
        return _blocked("reported_x_consumed_canonical_projection_sequence_incomplete")
    upgraded = upgrade_weak_revision_candidate(
        base,
        previous_checkpoint_row=checkpoint,
        canonical_events=projection_events,
    )
    if upgraded.get("ok") is not True:
        return _blocked("reported_x_weak_revision_projection_blocked", detail=upgraded)
    committed = commit_reported_x_weak_forecast_revision(
        conn, activation=authorized, candidate=upgraded
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": committed.get("ok") is True,
        "state": _text(committed.get("state")),
        "trigger_source_revision_id": trigger_id,
        "forecast_evidence_snapshot_id": _text(snapshot.get("forecast_evidence_snapshot_id")),
        "commit": committed,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "apply_reported_x_taf_revision_from_source_observations"]
