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
from independent_weak_live_source_runtime import load_live_independent_weak_source_observations
from production_probability_v6_p0_input_runtime import (
    DEFAULT_NCEI_ROOT,
    DEFAULT_RESOLUTION_AUTHORITY,
    build_v6_production_p0_inputs,
    build_zuuu_physical_public_slug,
)
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_initial_writer import (
    commit_v6_initial_posterior,
    materialize_v6_initial_public_fact_row,
)
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_path_auxiliary_runtime import (
    enrich_v6_candidate_with_post_event_auxiliary,
)
from production_probability_v6_transaction_commit_runtime import (
    commit_v6_candidate_transaction,
)
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row
from zuuu_metar_reporting_schedule import is_settlement_metar_slot_utc


SCHEMA_VERSION = "production_probability_v6_canonical_event_drain_v1"
ROOT = Path(__file__).resolve().parent
DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT = (
    ROOT / "config/artifacts/identifiable_physical_state_model_v1.json"
)
DEFAULT_PHYSICAL_OBSERVATION_ARTIFACT = (
    ROOT / "config/artifacts/identifiable_physical_observation_v1.json"
)
DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT = (
    ROOT / "config/artifacts/amsc_frozen_reporting_operator_v1.json"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _frozen_weak_source_observations(
    conn: sqlite3.Connection, *, event_date: str, decision_as_of: str
) -> list[dict[str, Any]]:
    cutoff = _instant(decision_as_of)
    if cutoff is None:
        raise ValueError("v6_p0_weak_source_cutoff_invalid")
    rows: list[dict[str, Any]] = []
    try:
        stored = conn.execute(
            "SELECT observation_json FROM source_observations "
            "WHERE event_date=? AND source_class='forecast_weak_source'",
            (_text(event_date)[:10],),
        ).fetchall()
    except sqlite3.OperationalError:
        stored = []
    for raw in stored:
        observation = _parse(raw[0], {})
        completed = _instant(observation.get("response_completed_at")) if isinstance(observation, Mapping) else None
        if isinstance(observation, Mapping) and completed is not None and completed <= cutoff:
            rows.append(dict(observation))
    external = load_live_independent_weak_source_observations(event_date=_text(event_date)[:10])
    rows.extend(
        dict(row) for row in external.get("source_observations") or [] if isinstance(row, Mapping)
    )
    deduplicated = {}
    for row in rows:
        identity = _text(row.get("source_event_id") or row.get("source_observation_id"))
        if identity:
            deduplicated[identity] = row
    return [deduplicated[key] for key in sorted(deduplicated)]


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


def _augment_snapshot(
    row: Mapping[str, Any],
    weak: Mapping[str, Any],
    *,
    temperature_prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    prior = dict(temperature_prior) if isinstance(temperature_prior, Mapping) else {}
    if prior:
        snapshot["prior_context_audit_json"] = _json(
            {
                key: prior.get(key)
                for key in (
                    "schema",
                    "policy_version",
                    "approval_kind",
                    "event_date",
                    "decision_as_of",
                    "station_id",
                    "basin_context",
                    "seasonal_window_days",
                    "kernel_bandwidth_c",
                    "reference_sample_count",
                    "reference_measurement_caveat",
                    "artifact_sha256",
                    "prior_model_artifact_sha256",
                    "prior_context_id",
                    "boundary",
                )
                if prior.get(key) not in (None, "", [], {})
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
    physical_state_model_artifact_path: Path | str = DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
    physical_reporting_operator_artifact_path: Path | str = DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
    market_inputs: Mapping[str, Any] | None = None,
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
        source_observations=_frozen_weak_source_observations(
            conn, event_date=day, decision_as_of=decision_as_of
        ),
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
    identifiable_physical = (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
    state_model_artifact = (
        json.loads(Path(physical_state_model_artifact_path).read_text(encoding="utf-8"))
        if identifiable_physical
        else None
    )
    reporting_operator_artifact = (
        json.loads(Path(physical_reporting_operator_artifact_path).read_text(encoding="utf-8"))
        if identifiable_physical
        else None
    )
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
        initial_joint=() if identifiable_physical else _initial_joint(inputs),
        candidate_scenario_responsibilities=weak["candidate_scenario_responsibilities"],
        replay_generation=generation,
        supersedes_checkpoint_id=_text(latest[0]) if latest else "",
        prior_context=inputs["prior_context"] if identifiable_physical else None,
        weak_likelihood=weak if identifiable_physical else None,
        physical_state_model_artifact=state_model_artifact,
        physical_reporting_operator_artifact=reporting_operator_artifact,
    )
    snapshot = _augment_snapshot(
        build_weak_forecast_evidence_snapshot_row(
            weak_envelope,
            target_identity=target,
            event_date=day,
            decision_as_of=decision_as_of,
        ),
        weak,
        temperature_prior=inputs.get("temperature_prior"),
    )
    fact_id = "fact-v6-p0-" + _sha({"checkpoint": p0["checkpoint_row"]["posterior_checkpoint_id"]})
    market_context_result = _parse(
        _mapping(market_inputs).get("market_context_result"), {}
    )
    market_context = _parse(
        _mapping(market_context_result).get("market_context"), {}
    )
    p0_public_slug = (
        _text(_mapping(market_context).get("slug"))
        if _mapping(market_inputs).get("market_projection_available") is True
        else ""
    ) or build_zuuu_physical_public_slug(day)
    public = materialize_v6_initial_public_fact_row(
        activation=authorized,
        checkpoint_row=p0["checkpoint_row"],
        fact_snapshot_id=fact_id,
        slug=p0_public_slug,
        event_date=day,
        fact_as_of=decision_as_of,
        correlation_id="correlation-v6-p0-" + _sha({"target": target, "as_of": decision_as_of}),
        market_inputs=market_inputs,
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
            "AND state='accepted_primary' AND COALESCE(supersedes_event_id,'')='' "
            "ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id",
            (target,),
        ).fetchall()
        if _text(row[0]) not in consumed
    ]


def physical_v6_correction_event_ids(
    conn: sqlite3.Connection, *, event_date: str
) -> list[str]:
    """Return canonical corrections that require manual station-day replay."""

    target = f"physical_weather_day_v1:ZUUU:{_text(event_date)[:10]}"
    return [
        _text(row[0])
        for row in conn.execute(
            "SELECT canonical_observation_event_id FROM canonical_observation_events "
            "WHERE target_identity=? AND station_id='ZUUU' "
            "AND selected_source_id='zuuu_metar_raw_amsc' "
            "AND state='accepted_primary' AND COALESCE(supersedes_event_id,'')<>'' "
            "ORDER BY valid_time,revision_no,canonical_observation_event_id",
            (target,),
        ).fetchall()
        if _text(row[0])
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
    day = _text(value.get("target_identity"))[-10:]
    value["is_contract_report_slot"] = bool(
        _text(value.get("station_id")).upper() == "ZUUU"
        and _text(value.get("selected_source_id")) == "zuuu_metar_raw_amsc"
        and _text(value.get("report_type")).upper() == "METAR"
        and is_settlement_metar_slot_utc(day, _text(value.get("valid_time")))
    )
    return value


def _weak_from_snapshot(
    conn: sqlite3.Connection,
    checkpoint: Mapping[str, Any],
    *,
    identifiable_physical: bool = False,
) -> dict[str, Any]:
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
    peak_timing = _parse(snapshot.get("forecast_peak_timing_prior_json"), {})
    background_context = _parse(snapshot.get("forecast_background_context_json"), {})
    if not paths or (not identifiable_physical and not responsibilities):
        raise ValueError("v6_drain_weak_future_controls_missing")
    if not identifiable_physical and not peak_timing:
        raise ValueError("v6_drain_forecast_peak_timing_prior_missing")
    if not identifiable_physical and not background_context:
        raise ValueError("v6_drain_forecast_background_context_missing")
    return {
        "artifact_sha256": _text(snapshot.get("weak_likelihood_artifact_sha256")),
        "joint_forecast_weak_likelihood_sha256": _text(snapshot.get("weak_likelihood_sha256")),
        "forecast_scenario_paths": paths,
        "candidate_scenario_responsibilities": responsibilities,
        "forecast_peak_timing_prior": peak_timing,
        "forecast_background_context": background_context,
        "source_revisions": [
            dict(row)
            for row in (_parse(snapshot.get("source_revisions_json"), []) or [])
            if isinstance(row, Mapping)
        ],
    }


def drain_v6_canonical_events(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    public_slug: str = "",
    max_events: int = 64,
    market_inputs: Mapping[str, Any] | None = None,
    physical_state_model_artifact_path: Path | str = DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
    physical_observation_artifact_path: Path | str = DEFAULT_PHYSICAL_OBSERVATION_ARTIFACT,
    physical_reporting_operator_artifact_path: Path | str = DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    identifiable_physical = (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    if identifiable_physical:
        corrections = physical_v6_correction_event_ids(conn, event_date=day)
        try:
            correction_checkpoint = _latest_checkpoint(conn, authorized, target)
            correction_consumed = set(
                _parse(correction_checkpoint.get("consumed_event_ids_json"), [])
            )
        except (ValueError, sqlite3.Error):
            correction_consumed = set()
        corrections = [
            event_id for event_id in corrections if event_id not in correction_consumed
        ]
        if corrections:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked_correction_requires_manual_replay",
                "correction_event_ids": corrections,
                "automatic_reverse_update_forbidden": True,
                "places_orders": False,
            }
    physical_state_model_artifact = (
        json.loads(Path(physical_state_model_artifact_path).read_text(encoding="utf-8"))
        if identifiable_physical
        else None
    )
    physical_observation_artifact = (
        json.loads(Path(physical_observation_artifact_path).read_text(encoding="utf-8"))
        if identifiable_physical
        else None
    )
    physical_reporting_operator_artifact = (
        json.loads(Path(physical_reporting_operator_artifact_path).read_text(encoding="utf-8"))
        if identifiable_physical
        else None
    )
    outcomes = []
    for event_id in pending_v6_canonical_event_ids(conn, activation=authorized, event_date=day)[:max_events]:
        event_started = time.monotonic()
        latency_stages_ms: dict[str, float] = {}
        stage_started = time.monotonic()
        checkpoint = _latest_checkpoint(conn, authorized, target)
        consumed_ids = _parse(checkpoint.get("consumed_event_ids_json"), [])
        history = [_event(conn, identity) for identity in consumed_ids]
        event = _event(conn, event_id)
        weak = _weak_from_snapshot(
            conn,
            checkpoint,
            identifiable_physical=identifiable_physical,
        )
        latency_stages_ms["ssot_inputs_load_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
        stage_started = time.monotonic()
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
            physical_state_model_artifact=physical_state_model_artifact,
            physical_observation_artifact=physical_observation_artifact,
            physical_reporting_operator_artifact=physical_reporting_operator_artifact,
        )
        latency_stages_ms["bayes_compose_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
        stage_started = time.monotonic()
        if not identifiable_physical:
            candidate = enrich_v6_candidate_with_post_event_auxiliary(
                candidate=candidate,
                canonical_events_through_current=[*history, event],
                forecast_peak_timing_prior=(
                    weak.get("forecast_peak_timing_prior")
                    if isinstance(weak.get("forecast_peak_timing_prior"), Mapping)
                    else {}
                ),
            )
        latency_stages_ms["post_event_auxiliary_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
        fact_id = "fact-v6-event-" + _sha({"checkpoint": candidate["checkpoint_row"]["posterior_checkpoint_id"]})
        stage_started = time.monotonic()
        branch_commit = commit_v6_candidate_transaction(
            conn,
            activation=authorized,
            previous_checkpoint=checkpoint,
            candidate=candidate,
            canonical_observation_event=event,
            market_inputs=market_inputs,
            fact_snapshot_id=fact_id,
            slug=public_slug or build_zuuu_physical_public_slug(day),
            event_date=day,
            fact_as_of=_text(candidate["checkpoint_row"].get("committed_at")),
            correlation_id=_text(candidate["strong_update_row"].get("strong_observation_update_id")),
        )
        latency_stages_ms["transaction_b_commit_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
        latency_stages_ms["canonical_event_to_outbox_total_ms"] = round((time.monotonic() - event_started) * 1000.0, 3)
        committed = branch_commit["commit"]
        outcomes.append(
            {
                "event_id": event_id,
                "commit": committed,
                "writer_route": _text(branch_commit.get("writer_route")),
                "market_projection_available": bool(branch_commit.get("market_projection_available")),
                "market_projection_na_reason": _text(branch_commit.get("market_projection_na_reason")),
                "latency_stages_ms": latency_stages_ms,
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
    "physical_v6_correction_event_ids",
]
