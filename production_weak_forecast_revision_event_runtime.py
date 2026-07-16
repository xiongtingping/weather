"""Live TAF revision ingress for the unique production probability SSOT."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from independent_weak_live_source_runtime import load_live_independent_weak_source_observations
from joint_weak_live_evidence_runtime import bind_live_joint_weak_likelihood_for_production, build_live_joint_weak_likelihood
from production_probability_activation_contract import validate_production_probability_activation
from production_weak_forecast_revision_writer import commit_production_weak_forecast_revision
from taf_revision_candidate_selection_runtime import (
    dedupe_source_observations,
    select_new_taf_revision_candidates,
)
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row
from weak_forecast_revision_transaction_runtime import compile_weak_forecast_revision_candidate


SCHEMA_VERSION = "production_weak_forecast_revision_event_runtime_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("payload")
    return value if isinstance(value, Mapping) else {}


def apply_production_taf_revision_from_source_observations(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    source_observations: Iterable[Mapping[str, Any]],
    decision_as_of: str,
    public_slug: str,
) -> dict[str, Any]:
    """Apply only a newly first-seen AMSC TAF revision; never consume METAR."""

    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    incoming = [dict(row) for row in source_observations if isinstance(row, Mapping)]
    candidates = select_new_taf_revision_candidates(
        incoming, event_date=day, decision_as_of=decision_as_of,
    )
    if not candidates:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "no_new_taf_revision", "places_orders": False}
    conn.row_factory = sqlite3.Row
    authorized = validate_production_probability_activation(activation)
    trigger = candidates[-1]
    trigger_payload = _payload(trigger)
    revision_kind = _text(trigger_payload.get("forecast_revision_kind")) or "original"
    trigger_revision_id = _text(trigger_payload.get("source_revision_id") or trigger.get("source_event_id"))
    active_generations = conn.execute(
        """SELECT p.replay_generation
           FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
        (target, _text(authorized.get("activation_contract_sha256"))),
    ).fetchall()
    if len(active_generations) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "current_activation_p0_lineage_not_unique", "places_orders": False}
    active_generation = int(active_generations[0][0] or 0)
    checkpoint = conn.execute(
        """SELECT * FROM posterior_checkpoints
           WHERE target_identity=? AND replay_generation=? AND state='committed'
           ORDER BY rowid DESC LIMIT 1""",
        (target, active_generation),
    ).fetchone()
    if checkpoint is None:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "current_station_day_checkpoint_missing", "places_orders": False}
    checkpoint_row = dict(checkpoint)
    old_snapshot = conn.execute(
        "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
        (_text(checkpoint_row.get("forecast_evidence_snapshot_id")),),
    ).fetchone()
    if old_snapshot is None:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "current_weak_forecast_snapshot_missing", "places_orders": False}
    independent = load_live_independent_weak_source_observations(event_date=day)
    combined = dedupe_source_observations(
        [*(independent.get("source_observations") or []), *incoming]
    )
    grid = [float(value) for value in json.loads(_text(old_snapshot["temperature_grid_json"]))]
    weak = bind_live_joint_weak_likelihood_for_production(
        build_live_joint_weak_likelihood(
            temperature_grid_c=grid,
            event_date=day,
            decision_as_of=_text(decision_as_of),
            source_observations=combined,
        )
    )
    if weak.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "revised_joint_weak_likelihood_unavailable", "detail": weak, "places_orders": False}
    snapshot = build_weak_forecast_evidence_snapshot_row(
        weak,
        target_identity=target,
        event_date=day,
        decision_as_of=_text(decision_as_of),
    )
    if _text(snapshot.get("forecast_evidence_snapshot_id")) == _text(old_snapshot["forecast_evidence_snapshot_id"]):
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "revision_already_visible",
            "trigger_source_revision_id": trigger_revision_id,
            "new_taf_revision_candidate_count": len(candidates),
            "forecast_evidence_snapshot_id": snapshot["forecast_evidence_snapshot_id"],
            "places_orders": False,
        }
    candidate = compile_weak_forecast_revision_candidate(
        previous_checkpoint_row=checkpoint_row,
        previous_snapshot_row=dict(old_snapshot),
        revised_snapshot_row=snapshot,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id=trigger_revision_id,
        revision_kind=revision_kind,
        public_slug=_text(public_slug),
        decision_as_of=_text(decision_as_of),
        replay_generation=int(checkpoint_row.get("replay_generation") or 0),
    )
    committed = commit_production_weak_forecast_revision(conn, activation=authorized, candidate=candidate)
    return {
        "schema": SCHEMA_VERSION,
        "ok": committed.get("ok") is True,
        "state": committed.get("state"),
        "new_taf_revision_candidate_count": len(candidates),
        "trigger_source_revision_id": trigger_revision_id,
        "forecast_evidence_snapshot_id": snapshot["forecast_evidence_snapshot_id"],
        "commit": committed,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "apply_production_taf_revision_from_source_observations",
    "select_new_taf_revision_candidates",
]
