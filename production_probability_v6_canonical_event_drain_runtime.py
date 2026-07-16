"""Unique production P0 and canonical METAR/SPECI drain for v6."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from physical_state_bayes_v6.contracts import JointCell
from production_probability_v6_p0_input_runtime import (
    DEFAULT_NCEI_ROOT,
    DEFAULT_RESOLUTION_AUTHORITY,
    build_v6_production_p0_inputs,
    build_zuuu_physical_public_slug,
)
from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_initial_writer import (
    commit_v6_initial_posterior,
    materialize_v6_initial_public_fact_row,
)
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_public_ssot_writer import (
    commit_v6_public_physical_transaction,
    materialize_v6_public_physical_fact_row,
)
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row


SCHEMA_VERSION = "production_probability_v6_canonical_event_drain_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def _state_reference(event_date: str) -> str:
    # The operator's current event-date selection is ET, but ZUUU canonical
    # station-day identities and their first valid observation remain anchored
    # to the resolution authority's Asia/Shanghai reporting day.  Preserve
    # this reference so existing canonical event identities remain replayable.
    local = datetime.fromisoformat(_text(event_date)[:10]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _initial_joint(inputs: Mapping[str, Any]) -> tuple[JointCell, ...]:
    prior = inputs["prior_context"]
    weak = inputs["weak_likelihood"]["joint_weak_likelihood"]
    prior_rows = prior["continuous_distribution"]["grid"]
    prior_mass = {float(row["temp_c"]): float(row["mass"]) for row in prior_rows}
    weak_mass = {
        float(row["temperature_c"]): float(row["likelihood"])
        for row in weak["joint_forecast_weak_likelihood"]
    }
    latent = prior["latent_state_prior_by_temperature"]
    rows = []
    for y_c in sorted(set(prior_mass) & set(weak_mass)):
        for state, state_probability in latent[f"{y_c:g}"].items():
            rows.append(
                JointCell(
                    y_c=y_c,
                    state=_text(state),
                    probability=prior_mass[y_c] * weak_mass[y_c] * float(state_probability),
                )
            )
    total = sum(row.probability for row in rows)
    if total <= 0.0 or set(prior_mass) != set(weak_mass):
        raise ValueError("v6_p0_prior_weak_grid_or_mass_invalid")
    return tuple(JointCell(row.y_c, row.state, row.probability / total) for row in rows)


def _augment_snapshot(row: Mapping[str, Any], weak: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = dict(row)
    paths = weak.get("forecast_scenario_paths")
    responsibilities = weak.get("candidate_scenario_responsibilities")
    if not isinstance(paths, Mapping) or not isinstance(responsibilities, Mapping):
        raise ValueError("v6_p0_weak_future_control_contract_missing")
    snapshot.update(
        {
            "forecast_scenario_paths_json": _json(paths),
            "forecast_scenario_paths_sha256": _text(paths.get("sha256")),
            "candidate_scenario_responsibilities_json": _json(responsibilities),
            "candidate_scenario_responsibilities_sha256": _text(responsibilities.get("sha256")),
        }
    )
    if len(snapshot["forecast_scenario_paths_sha256"]) != 64 or len(snapshot["candidate_scenario_responsibilities_sha256"]) != 64:
        raise ValueError("v6_p0_weak_future_control_sha_missing")
    return snapshot


def ensure_v6_daily_p0(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    decision_as_of: str,
    curve_root: Path | str,
    weak_artifact_path: Path | str | None = None,
    ncei_root: Path | str = DEFAULT_NCEI_ROOT,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    existing = conn.execute(
        "SELECT l.posterior_checkpoint_id,p.replay_generation FROM production_initial_posterior_lineage l "
        "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
        "WHERE l.target_identity=? AND l.activation_contract_sha256=?",
        (target, authorized["activation_contract_sha256"]),
    ).fetchall()
    if len(existing) > 1:
        raise ValueError("v6_daily_p0_lineage_not_unique")
    if existing:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "already_initialized",
            "posterior_checkpoint_id": _text(existing[0][0]),
            "replay_generation": int(existing[0][1] or 0),
            "places_orders": False,
        }
    inputs = build_v6_production_p0_inputs(
        event_date=day,
        decision_as_of=decision_as_of,
        curve_root=curve_root,
        weak_artifact_path=weak_artifact_path,
        ncei_root=ncei_root,
        resolution_authority_path=resolution_authority_path,
    )
    if inputs.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "detail": inputs, "places_orders": False}
    weak_envelope = inputs["weak_likelihood"]
    weak = weak_envelope["joint_weak_likelihood"]
    if (
        _text(inputs["prior_context"].get("prior_model_artifact_sha256"))
        != _text(authorized.get("prior_model_artifact_sha256"))
        or _text(weak.get("artifact_sha256")) != _text(authorized.get("weak_likelihood_artifact_sha256"))
    ):
        raise ValueError("v6_daily_p0_activation_input_artifact_mismatch")
    latest = conn.execute(
        "SELECT posterior_checkpoint_id,replay_generation FROM posterior_checkpoints "
        "WHERE target_identity=? AND state='committed' ORDER BY replay_generation DESC,rowid DESC LIMIT 1",
        (target,),
    ).fetchone()
    generation = int(latest[1] or 0) + 1 if latest else 0
    paths_sha = _text(weak["forecast_scenario_paths"].get("sha256"))
    p0 = compile_v6_p0_candidate(
        activation=authorized,
        target_identity=target,
        event_date=day,
        decision_as_of=decision_as_of,
        state_reference_valid_at=_state_reference(day),
        prior_context_id=_text(inputs["prior_context"].get("prior_context_id")),
        prior_model_artifact_sha256=_text(inputs["prior_context"].get("prior_model_artifact_sha256")),
        forecast_evidence_snapshot_id=_text(weak.get("forecast_evidence_snapshot_id")),
        weak_likelihood_artifact_sha256=_text(weak.get("artifact_sha256")),
        resolution_contract_id=_text(inputs["resolution_contract"].get("resolution_contract_id")),
        forcing_generation_sha256=paths_sha,
        initial_joint=_initial_joint(inputs),
        replay_generation=generation,
        supersedes_checkpoint_id=_text(latest[0]) if latest else "",
    )
    snapshot = _augment_snapshot(
        build_weak_forecast_evidence_snapshot_row(
            weak_envelope,
            target_identity=target,
            event_date=day,
            decision_as_of=decision_as_of,
        ),
        weak,
    )
    fact_id = "fact-v6-p0-" + _sha({"checkpoint": p0["checkpoint_row"]["posterior_checkpoint_id"]})
    public = materialize_v6_initial_public_fact_row(
        activation=authorized,
        checkpoint_row=p0["checkpoint_row"],
        fact_snapshot_id=fact_id,
        slug=build_zuuu_physical_public_slug(day),
        event_date=day,
        fact_as_of=decision_as_of,
        correlation_id="correlation-v6-p0-" + _sha({"target": target, "as_of": decision_as_of}),
    )
    return commit_v6_initial_posterior(
        conn,
        activation=authorized,
        p0_candidate=p0,
        weak_snapshot_row=snapshot,
        public_fact_row=public,
    )


def _latest_checkpoint(conn: sqlite3.Connection, activation: Mapping[str, Any], target: str) -> dict[str, Any]:
    generation = conn.execute(
        "SELECT p.replay_generation FROM production_initial_posterior_lineage l "
        "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
        "WHERE l.target_identity=? AND l.activation_contract_sha256=?",
        (target, _text(activation.get("activation_contract_sha256"))),
    ).fetchall()
    if len(generation) != 1:
        raise ValueError("v6_drain_active_generation_not_unique")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM posterior_checkpoints WHERE target_identity=? AND replay_generation=? "
        "ORDER BY rowid DESC LIMIT 1",
        (target, int(generation[0][0] or 0)),
    ).fetchone()
    if row is None:
        raise ValueError("v6_drain_checkpoint_missing")
    return dict(row)


def pending_v6_canonical_event_ids(
    conn: sqlite3.Connection, *, activation: Mapping[str, Any], event_date: str
) -> list[str]:
    target = f"physical_weather_day_v1:ZUUU:{_text(event_date)[:10]}"
    checkpoint = _latest_checkpoint(conn, activation, target)
    consumed = set(_parse(checkpoint.get("consumed_event_ids_json"), []))
    return [
        _text(row[0])
        for row in conn.execute(
            "SELECT canonical_observation_event_id FROM canonical_observation_events "
            "WHERE target_identity=? AND station_id='ZUUU' AND selected_source_id='zuuu_metar_raw_amsc' "
            "AND state='accepted_primary' ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id",
            (target,),
        ).fetchall()
        if _text(row[0]) not in consumed
    ]


def _event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM canonical_observation_events WHERE canonical_observation_event_id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise ValueError("v6_drain_event_missing")
    value = dict(row)
    value["parsed_observation"] = _parse(value.get("parsed_observation_json"), {})
    return value


def _weak_from_snapshot(conn: sqlite3.Connection, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
        (_text(checkpoint.get("forecast_evidence_snapshot_id")),),
    ).fetchone()
    if row is None:
        raise ValueError("v6_drain_weak_snapshot_missing")
    snapshot = dict(row)
    paths = _parse(snapshot.get("forecast_scenario_paths_json"), {})
    responsibilities = _parse(snapshot.get("candidate_scenario_responsibilities_json"), {})
    if not paths or not responsibilities:
        raise ValueError("v6_drain_weak_future_controls_missing")
    return {
        "artifact_sha256": _text(snapshot.get("weak_likelihood_artifact_sha256")),
        "joint_forecast_weak_likelihood_sha256": _text(snapshot.get("weak_likelihood_sha256")),
        "forecast_scenario_paths": paths,
        "candidate_scenario_responsibilities": responsibilities,
    }


def drain_v6_canonical_events(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    public_slug: str = "",
    max_events: int = 64,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    outcomes = []
    for event_id in pending_v6_canonical_event_ids(conn, activation=authorized, event_date=day)[:max_events]:
        event_started = time.monotonic()
        checkpoint = _latest_checkpoint(conn, authorized, target)
        consumed_ids = _parse(checkpoint.get("consumed_event_ids_json"), [])
        history = [_event(conn, identity) for identity in consumed_ids]
        event = _event(conn, event_id)
        weak = _weak_from_snapshot(conn, checkpoint)
        candidate = compose_v6_canonical_event(
            activation=authorized,
            previous_checkpoint=checkpoint,
            canonical_event=event,
            pre_event_history=history,
            weak_likelihood=weak,
            transition_artifact=json.loads((Path(__file__).parent / "config/physical_state_bayes_v6_transition_artifact.json").read_text()),
            observation_artifact=json.loads((Path(__file__).parent / "config/physical_state_bayes_v6_observation_artifact.json").read_text()),
            decision_as_of=_text(event.get("first_actionable_seen_at")),
            completed_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        fact_id = "fact-v6-event-" + _sha({"checkpoint": candidate["checkpoint_row"]["posterior_checkpoint_id"]})
        public = materialize_v6_public_physical_fact_row(
            activation=authorized,
            strong_update_row=candidate["strong_update_row"],
            checkpoint_row=candidate["checkpoint_row"],
            fact_snapshot_id=fact_id,
            slug=public_slug or build_zuuu_physical_public_slug(day),
            event_date=day,
            fact_as_of=_text(candidate["checkpoint_row"].get("committed_at")),
            correlation_id=_text(candidate["strong_update_row"].get("strong_observation_update_id")),
            market_bucket_probabilities_na_reason="resolution_bridge_not_closed",
        )
        committed = commit_v6_public_physical_transaction(
            conn,
            activation=authorized,
            expected_previous_checkpoint_id=_text(checkpoint.get("posterior_checkpoint_id")),
            expected_previous_posterior_run_id=_text(checkpoint.get("posterior_run_id")),
            strong_update_row=candidate["strong_update_row"],
            checkpoint_row=candidate["checkpoint_row"],
            public_fact_row=public,
        )
        outcomes.append(
            {
                "event_id": event_id,
                "commit": committed,
                "probability_transaction_elapsed_ms": round((time.monotonic() - event_started) * 1000.0, 3),
            }
        )
    remaining = pending_v6_canonical_event_ids(conn, activation=authorized, event_date=day)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not remaining,
        "state": "drained" if not remaining else "partial",
        "committed_event_count": len(outcomes),
        "remaining_event_ids": remaining,
        "outcomes": outcomes,
        "max_probability_transaction_elapsed_ms": max(
            (float(item["probability_transaction_elapsed_ms"]) for item in outcomes),
            default=0.0,
        ),
        "p95_probability_transaction_elapsed_ms": (
            sorted(float(item["probability_transaction_elapsed_ms"]) for item in outcomes)[
                max(0, int(len(outcomes) * 0.95 + 0.999999) - 1)
            ]
            if outcomes
            else 0.0
        ),
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "drain_v6_canonical_events",
    "ensure_v6_daily_p0",
    "pending_v6_canonical_event_ids",
]
