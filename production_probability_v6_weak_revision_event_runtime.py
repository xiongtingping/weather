"""AMSC TAF/AMD/COR forward-only weak revision ingress for production v6."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from independent_weak_live_source_runtime import load_live_independent_weak_source_observations
from joint_weak_live_evidence_runtime import (
    bind_live_joint_weak_likelihood_for_production,
    build_live_joint_weak_likelihood,
)
from production_probability_v6_activation_contract import CHECKPOINT_SCHEMA, validate_v6_activation
from production_probability_v6_canonical_event_drain_runtime import _augment_snapshot
from production_probability_v6_initial_writer import materialize_v6_initial_public_fact_row
from production_probability_v6_p0_input_runtime import build_zuuu_physical_public_slug
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256
from production_probability_v6_weak_revision_runtime import compile_v6_weak_revision_candidate
from production_probability_v6_weak_revision_writer import (
    commit_v6_weak_revision_transaction,
    materialize_v6_weak_revision_public_fact_row,
)
from taf_revision_candidate_selection_runtime import dedupe_source_observations, select_new_taf_revision_candidates
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row


SCHEMA_VERSION = "production_probability_v6_weak_revision_event_runtime_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _blocked(reason: str, **detail: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "reason": reason,
        **detail,
        "places_orders": False,
    }


def _active_checkpoint(
    conn: sqlite3.Connection, *, activation: Mapping[str, Any], target: str
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT p.replay_generation FROM production_initial_posterior_lineage l "
        "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
        "WHERE l.target_identity=? AND l.activation_contract_sha256=?",
        (target, _text(activation.get("activation_contract_sha256"))),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("v6_weak_revision_active_generation_not_unique")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM posterior_checkpoints WHERE target_identity=? AND replay_generation=? "
        "AND state='committed' ORDER BY rowid DESC LIMIT 1",
        (target, int(rows[0][0] or 0)),
    ).fetchone()
    if row is None:
        raise ValueError("v6_weak_revision_current_checkpoint_missing")
    checkpoint = dict(row)
    if _text(checkpoint.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA:
        raise ValueError("v6_weak_revision_current_checkpoint_schema_invalid")
    return checkpoint


def apply_v6_taf_revision_from_source_observations(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    source_observations: Iterable[Mapping[str, Any]],
    decision_as_of: str,
    public_slug: str = "",
) -> dict[str, Any]:
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    incoming = [dict(row) for row in source_observations if isinstance(row, Mapping)]
    if not any(
        _text(row.get("source_id")) == "zuuu_taf_forecast_amsc"
        and _text(row.get("event_date") or _mapping(row.get("payload")).get("event_date"))[:10] == day
        for row in incoming
    ):
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "no_new_taf_revision",
            "places_orders": False,
        }
    try:
        authorized = validate_v6_activation(activation)
        if _text(authorized.get("runtime_manifest_sha256")) != v6_runtime_manifest_sha256():
            raise ValueError("v6_weak_revision_runtime_manifest_drift")
        checkpoint = _active_checkpoint(conn, activation=authorized, target=target)
        conn.row_factory = sqlite3.Row
        old_row = conn.execute(
            "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
            (_text(checkpoint.get("forecast_evidence_snapshot_id")),),
        ).fetchone()
        if old_row is None:
            raise ValueError("v6_weak_revision_current_snapshot_missing")
        old_snapshot = dict(old_row)
        applied_ids = {
            _text(row[0])
            for row in conn.execute(
                "SELECT trigger_source_revision_id FROM weak_forecast_revision_updates "
                "WHERE target_identity=? AND state='applied'",
                (target,),
            ).fetchall()
            if _text(row[0])
        }
        candidates = select_new_taf_revision_candidates(
            incoming,
            event_date=day,
            decision_as_of=decision_as_of,
            visible_after=_text(old_snapshot.get("decision_as_of")),
            applied_revision_ids=applied_ids,
        )
        if not candidates:
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "no_new_taf_revision",
                "places_orders": False,
            }
        independent = load_live_independent_weak_source_observations(event_date=day)
        combined = dedupe_source_observations([*(independent.get("source_observations") or []), *incoming])
        grid = [float(value) for value in json.loads(_text(old_snapshot.get("temperature_grid_json")))]
        weak_envelope = bind_live_joint_weak_likelihood_for_production(
            build_live_joint_weak_likelihood(
                temperature_grid_c=grid,
                event_date=day,
                decision_as_of=_text(decision_as_of),
                source_observations=combined,
            )
        )
        if weak_envelope.get("ok") is not True:
            return _blocked("v6_revised_joint_weak_likelihood_unavailable", detail=weak_envelope)
        weak = _mapping(weak_envelope.get("joint_weak_likelihood"))
        snapshot = _augment_snapshot(
            build_weak_forecast_evidence_snapshot_row(
                weak_envelope,
                target_identity=target,
                event_date=day,
                decision_as_of=_text(decision_as_of),
            ),
            weak,
        )
        if _text(snapshot.get("forecast_evidence_snapshot_id")) == _text(old_snapshot.get("forecast_evidence_snapshot_id")):
            return {"schema": SCHEMA_VERSION, "ok": True, "state": "revision_already_visible", "places_orders": False}
        trigger = candidates[-1]
        payload = _mapping(trigger.get("payload"))
        trigger_id = _text(payload.get("source_revision_id") or trigger.get("source_event_id"))
        revision_kind = _text(payload.get("forecast_revision_kind")) or "original"
        candidate = compile_v6_weak_revision_candidate(
            activation=authorized,
            previous_checkpoint=checkpoint,
            previous_snapshot=old_snapshot,
            revised_snapshot=snapshot,
            trigger_source_id="zuuu_taf_forecast_amsc",
            trigger_source_revision_id=trigger_id,
            revision_kind=revision_kind,
            decision_as_of=_text(decision_as_of),
        )
        slug = _text(public_slug) or build_zuuu_physical_public_slug(day)
        latest = conn.execute(
            "SELECT h.fact_json FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id "
            "WHERE l.slug=? AND l.event_date=?",
            (slug, day),
        ).fetchone()
        if latest is None:
            raise ValueError("v6_weak_revision_current_public_fact_missing")
        update = _mapping(candidate.get("update_row"))
        successor = _mapping(candidate.get("checkpoint_row"))
        fact_snapshot_id = "fact-" + _text(update.get("weak_forecast_revision_update_id"))
        public = materialize_v6_weak_revision_public_fact_row(
            activation=authorized,
            previous_fact_json=latest[0],
            checkpoint_row=successor,
            update_row=update,
            fact_snapshot_id=fact_snapshot_id,
            slug=slug,
            event_date=day,
            fact_as_of=_text(decision_as_of),
        )
        committed = commit_v6_weak_revision_transaction(
            conn,
            activation=authorized,
            candidate=candidate,
            public_fact_row=public,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _blocked(f"v6_taf_revision_rejected:{type(exc).__name__}", exception_detail=str(exc)[:300])
    return {
        "schema": SCHEMA_VERSION,
        "ok": committed.get("ok") is True,
        "state": _text(committed.get("state")),
        "trigger_source_revision_id": trigger_id,
        "forecast_evidence_snapshot_id": _text(snapshot.get("forecast_evidence_snapshot_id")),
        "commit": committed,
        "past_state_transitions_replayed": False,
        "future_forcing_replaced": True,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "apply_v6_taf_revision_from_source_observations"]
