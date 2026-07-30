"""Forward-only forecast-source revision ingress for production v6."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from independent_weak_live_source_runtime import load_live_independent_weak_source_observations
from joint_weak_live_evidence_runtime import (
    PRODUCTION_FORECAST_SOURCE_IDS,
    bind_live_joint_weak_likelihood_for_production,
    build_live_joint_weak_likelihood,
)
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_canonical_event_drain_runtime import (
    DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
    DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
    _augment_snapshot,
    _weak_from_snapshot,
)
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


def _parse_mapping_list(value: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(_text(value)) if not isinstance(value, list) else value
    except (TypeError, ValueError):
        return []
    return [dict(row) for row in parsed if isinstance(row, Mapping)] if isinstance(parsed, list) else []


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _assert_no_visible_unconsumed_event_before_revision(
    conn: sqlite3.Connection,
    *,
    target: str,
    checkpoint: Mapping[str, Any],
    decision_as_of: str,
) -> None:
    cutoff = _instant(decision_as_of)
    if cutoff is None:
        raise ValueError("physical_weak_revision_decision_as_of_invalid")
    try:
        consumed_raw = json.loads(_text(checkpoint.get("consumed_event_ids_json")) or "[]")
    except (TypeError, ValueError) as exc:
        raise ValueError("physical_weak_revision_consumed_sequence_invalid") from exc
    consumed = {str(item) for item in consumed_raw} if isinstance(consumed_raw, list) else set()
    rows = conn.execute(
        "SELECT canonical_observation_event_id,valid_time,first_actionable_seen_at,"
        "COALESCE(supersedes_event_id,'') "
        "FROM canonical_observation_events WHERE target_identity=? AND station_id='ZUUU' "
        "AND selected_source_id='zuuu_metar_raw_amsc' AND state='accepted_primary'",
        (target,),
    ).fetchall()
    blockers = []
    corrections = []
    for event_id, valid_time, seen_at, supersedes_event_id in rows:
        valid = _instant(valid_time)
        seen = _instant(seen_at)
        if _text(supersedes_event_id) and _text(event_id) not in consumed:
            corrections.append(_text(event_id))
            continue
        if (
            _text(event_id) not in consumed
            and valid is not None
            and seen is not None
            and valid <= cutoff
            and seen <= cutoff
        ):
            blockers.append(_text(event_id))
    if corrections:
        raise ValueError(
            "physical_weak_revision_correction_requires_manual_replay:"
            + ",".join(sorted(corrections))
        )
    if blockers:
        raise ValueError(
            "physical_weak_revision_visible_canonical_event_must_drain_first:"
            + ",".join(sorted(blockers))
        )


def _archived_taf_source_observations(conn: sqlite3.Connection, *, event_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT observation_json FROM source_observations "
        "WHERE source_id='zuuu_taf_forecast_amsc' AND event_date=? ORDER BY fetched_at,source_event_id",
        (_text(event_date)[:10],),
    ).fetchall()
    observations: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(_text(row[0]))
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            observations.append(dict(value))
    return observations


def _changed_source_revision(
    old_snapshot: Mapping[str, Any], new_revisions: Iterable[Mapping[str, Any]]
) -> tuple[dict[str, Any] | None, list[str]]:
    old_rows = _parse_mapping_list(old_snapshot.get("source_revisions_json"))
    old_by_source = {_text(row.get("source_id")): row for row in old_rows if _text(row.get("source_id"))}
    new_by_source = {
        _text(row.get("source_id")): dict(row)
        for row in new_revisions
        if isinstance(row, Mapping) and _text(row.get("source_id"))
    }
    missing = sorted(set(old_by_source) - set(new_by_source))
    # A previously materialized snapshot may contain GFS from the retired
    # basket source policy.  Its deliberate removal is a production-policy
    # rebase, not source-set corruption; the new snapshot must be committed
    # so the public lineage no longer advertises GFS as a basket input.
    policy_removed = sorted(
        source for source in missing
        if source not in set(PRODUCTION_FORECAST_SOURCE_IDS)
        and source in {"gfs", "jma", "wu", "twc"}
    )
    missing = [source for source in missing if source not in policy_removed]
    changed = [
        row
        for source_id, row in new_by_source.items()
        if _text(row.get("source_revision_id"))
        != _text(old_by_source.get(source_id, {}).get("source_revision_id"))
    ]
    if not changed and policy_removed:
        return {
            "source_id": "production_source_policy",
            "source_revision_id": "exclude:" + "+".join(policy_removed),
            "available_at": "",
        }, missing
    if not changed:
        return None, missing
    return max(
        changed,
        key=lambda row: (
            _text(row.get("available_at")),
            _text(row.get("source_id")),
            _text(row.get("source_revision_id")),
        ),
    ), missing


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


def _commit_current_weak_surface(
    conn: sqlite3.Connection,
    *,
    authorized: Mapping[str, Any],
    day: str,
    target: str,
    checkpoint: Mapping[str, Any],
    old_snapshot: Mapping[str, Any],
    source_observations: Iterable[Mapping[str, Any]],
    decision_as_of: str,
    public_slug: str,
    trigger_source_id: str = "",
    trigger_source_revision_id: str = "",
    revision_kind: str = "original",
    market_inputs: Mapping[str, Any] | None = None,
    physical_state_model_artifact_path: Path | str = DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
    physical_reporting_operator_artifact_path: Path | str = DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
) -> dict[str, Any]:
    grid = [float(value) for value in json.loads(_text(old_snapshot.get("temperature_grid_json")))]
    weak_envelope = bind_live_joint_weak_likelihood_for_production(
        build_live_joint_weak_likelihood(
            temperature_grid_c=grid,
            event_date=day,
            decision_as_of=_text(decision_as_of),
            source_observations=source_observations,
            allowed_source_ids=PRODUCTION_FORECAST_SOURCE_IDS,
        )
    )
    if weak_envelope.get("ok") is not True:
        return _blocked("v6_revised_joint_weak_likelihood_unavailable", detail=weak_envelope)
    evidence = _mapping(weak_envelope.get("forecast_evidence"))
    changed, missing = _changed_source_revision(old_snapshot, evidence.get("source_revisions") or [])
    if missing:
        return _blocked("v6_revised_joint_weak_source_set_regression", missing_source_ids=missing)
    if changed is None:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "no_new_weak_source_revision", "places_orders": False}
    resolved_trigger_source_id = _text(trigger_source_id) or _text(changed.get("source_id"))
    resolved_trigger_revision_id = _text(trigger_source_revision_id) or _text(changed.get("source_revision_id"))
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
    identifiable_physical = (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
    previous_weak = None
    state_model = None
    reporting_operator = None
    if identifiable_physical:
        _assert_no_visible_unconsumed_event_before_revision(
            conn,
            target=target,
            checkpoint=checkpoint,
            decision_as_of=decision_as_of,
        )
        previous_weak = _weak_from_snapshot(
            conn,
            checkpoint,
            identifiable_physical=True,
        )
        state_model = json.loads(
            Path(physical_state_model_artifact_path).read_text(encoding="utf-8")
        )
        reporting_operator = json.loads(
            Path(physical_reporting_operator_artifact_path).read_text(
                encoding="utf-8"
            )
        )
    candidate = compile_v6_weak_revision_candidate(
        activation=authorized,
        previous_checkpoint=checkpoint,
        previous_snapshot=old_snapshot,
        revised_snapshot=snapshot,
        trigger_source_id=resolved_trigger_source_id,
        trigger_source_revision_id=resolved_trigger_revision_id,
        revision_kind=_text(revision_kind),
        decision_as_of=_text(decision_as_of),
        previous_weak_likelihood=previous_weak,
        revised_weak_likelihood=weak if identifiable_physical else None,
        physical_state_model_artifact=state_model,
        physical_reporting_operator_artifact=reporting_operator,
    )
    inputs = _mapping(market_inputs)
    market_context = _mapping(
        _mapping(inputs.get("market_context_result")).get("market_context")
    )
    slug = (
        _text(market_context.get("slug"))
        or _text(public_slug)
        or build_zuuu_physical_public_slug(day)
    )
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
        market_inputs=inputs,
    )
    committed = commit_v6_weak_revision_transaction(
        conn,
        activation=authorized,
        candidate=candidate,
        public_fact_row=public,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": committed.get("ok") is True,
        "state": _text(committed.get("state")),
        "trigger_source_id": resolved_trigger_source_id,
        "trigger_source_revision_id": resolved_trigger_revision_id,
        "forecast_evidence_snapshot_id": _text(snapshot.get("forecast_evidence_snapshot_id")),
        "commit": committed,
        "past_state_transitions_replayed": False,
        "future_forcing_replaced": True,
        "places_orders": False,
    }


def apply_v6_taf_revision_from_source_observations(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    source_observations: Iterable[Mapping[str, Any]],
    decision_as_of: str,
    public_slug: str = "",
    market_inputs: Mapping[str, Any] | None = None,
    physical_state_model_artifact_path: Path | str = DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
    physical_reporting_operator_artifact_path: Path | str = DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
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
        trigger = candidates[-1]
        payload = _mapping(trigger.get("payload"))
        trigger_id = _text(payload.get("source_revision_id") or trigger.get("source_event_id"))
        revision_kind = _text(payload.get("forecast_revision_kind")) or "original"
        return _commit_current_weak_surface(
            conn,
            authorized=authorized,
            day=day,
            target=target,
            checkpoint=checkpoint,
            old_snapshot=old_snapshot,
            source_observations=combined,
            decision_as_of=decision_as_of,
            public_slug=public_slug,
            trigger_source_id="zuuu_taf_forecast_amsc",
            trigger_source_revision_id=trigger_id,
            revision_kind=revision_kind,
            market_inputs=market_inputs,
            physical_state_model_artifact_path=physical_state_model_artifact_path,
            physical_reporting_operator_artifact_path=physical_reporting_operator_artifact_path,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _blocked(f"v6_taf_revision_rejected:{type(exc).__name__}", exception_detail=str(exc)[:300])


def apply_v6_current_weak_source_revisions(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    decision_as_of: str,
    public_slug: str = "",
    market_inputs: Mapping[str, Any] | None = None,
    physical_state_model_artifact_path: Path | str = DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
    physical_reporting_operator_artifact_path: Path | str = DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
) -> dict[str, Any]:
    """Recompose v6 when any currently visible weak source revision changes."""

    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
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
        independent = load_live_independent_weak_source_observations(event_date=day)
        combined = dedupe_source_observations(
            [
                *(independent.get("source_observations") or []),
                *_archived_taf_source_observations(conn, event_date=day),
            ]
        )
        return _commit_current_weak_surface(
            conn,
            authorized=authorized,
            day=day,
            target=target,
            checkpoint=checkpoint,
            old_snapshot=dict(old_row),
            source_observations=combined,
            decision_as_of=decision_as_of,
            public_slug=public_slug,
            market_inputs=market_inputs,
            physical_state_model_artifact_path=physical_state_model_artifact_path,
            physical_reporting_operator_artifact_path=physical_reporting_operator_artifact_path,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _blocked(f"v6_weak_source_revision_rejected:{type(exc).__name__}", exception_detail=str(exc)[:300])


__all__ = [
    "SCHEMA_VERSION",
    "apply_v6_current_weak_source_revisions",
    "apply_v6_taf_revision_from_source_observations",
]
