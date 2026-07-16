"""Production canonical-event runtime for the unique sequential Bayes SSOT."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from lineage_identity_runtime import build_correlation_id, build_fact_snapshot_id
from production_probability_activation_contract import validate_production_probability_activation
from production_probability_p0_runtime import (
    DEFAULT_RESOLUTION_AUTHORITY,
    build_physical_only_resolution_contract,
)
from production_probability_runtime_manifest import current_runtime_manifest_sha256
from production_sequential_bayes_transaction_b_host import ProductionSequentialBayesTransactionBHost
from sequential_bayes_checkpoint_reader import load_latest_persisted_checkpoint
from sequential_bayes_shadow_pipeline import (
    build_canonical_event_transaction_b_candidate,
    load_canonical_observation_event,
)
from strong_observation_lr_operational_state_baseline import (
    build_operational_state_aware_strong_lr_baseline,
)


SCHEMA_VERSION = "production_sequential_bayes_event_runtime_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _blocked(reason: str, **context: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "reason": reason,
        **context,
        "places_orders": False,
    }


def _realization_policy() -> dict[str, Any]:
    core = {
        "schema": "realization_state_policy_v2",
        "approval_state": "approved",
        "credible_probability_floor": 0.05,
        "strong_support_log_probability_ratio_floor": 0.0,
        "strong_exclusion_probability_ceiling": 0.01,
        "strong_exclusion_log_probability_ratio_ceiling": -1.0,
        "scope": "production_physical_posterior_market_projection_na",
    }
    return {**core, "artifact_sha256": _sha(core)}


def load_production_probability_cycle(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    strong_lr_artifact: Mapping[str, Any] | None = None,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
) -> dict[str, Any]:
    """Load the exact frozen P0 context and current durable predecessor."""

    try:
        authorized = validate_production_probability_activation(activation)
    except ValueError as exc:
        return _blocked(f"production_probability_activation_rejected:{exc}")
    current_manifest_sha256 = current_runtime_manifest_sha256()
    if _text(authorized.get("runtime_manifest_sha256")) != current_manifest_sha256:
        return _blocked(
            "production_probability_runtime_manifest_drift",
            authorized_runtime_manifest_sha256=_text(authorized.get("runtime_manifest_sha256")),
            current_runtime_manifest_sha256=current_manifest_sha256,
        )
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    rows = conn.execute(
        """
        SELECT l.prior_context_id,l.prior_artifact_sha256,l.prior_model_artifact_sha256,
               l.weak_likelihood_artifact_sha256,l.forecast_evidence_snapshot_id,
               l.strong_lr_artifact_sha256,l.resolution_contract_id,
               l.activation_contract_sha256,l.posterior_run_id,
               l.lineage_json,p.replay_generation,p.checkpoint_kind,p.state
        FROM production_initial_posterior_lineage l
        JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
        WHERE l.target_identity=? AND l.activation_contract_sha256=?
        ORDER BY l.created_at,l.rowid
        """,
        (target, _text(authorized.get("activation_contract_sha256"))),
    ).fetchall()
    if len(rows) != 1:
        return _blocked("production_probability_current_activation_lineage_not_unique")
    lineage = rows[0]
    if _text(lineage[11]) != "initial" or _text(lineage[12]) != "committed":
        return _blocked("production_probability_current_activation_p0_invalid")
    active_replay_generation = int(lineage[10] or 0)
    try:
        previous = load_latest_persisted_checkpoint(
            conn,
            target_identity=target,
            event_date=day,
            replay_generation=active_replay_generation,
        )
    except (sqlite3.Error, ValueError) as exc:
        return _blocked(f"production_probability_checkpoint_unavailable:{exc}")
    if previous is None:
        return _blocked("production_probability_p0_missing")
    # Resolve the public slug from this activation's immutable P0 fact identity,
    # not from event_fact_latest.  A failed later activation generation may
    # have advanced the public pointer before an operator rolls the activation
    # file back; using the current P0 fact keeps generation selection coherent
    # without scanning append-only history.
    try:
        lineage_json = json.loads(_text(lineage[9]))
    except (TypeError, ValueError):
        lineage_json = {}
    initial_fact_snapshot_id = _text(_mapping(lineage_json).get("initial_evidence_snapshot_id"))
    public_rows = conn.execute(
        """SELECT slug,event_date FROM event_fact_history
           WHERE fact_snapshot_id=? AND fact_snapshot_id<>'' AND posterior_run_id=?""",
        (initial_fact_snapshot_id, _text(lineage[8])),
    ).fetchall()
    if len(public_rows) != 1 or _text(public_rows[0][1]) != day:
        return _blocked("production_probability_initial_public_slug_not_unique")
    public_slug = _text(public_rows[0][0])
    if not public_slug:
        return _blocked("production_probability_initial_public_slug_missing")
    artifact = dict(strong_lr_artifact or build_operational_state_aware_strong_lr_baseline())
    try:
        resolution = build_physical_only_resolution_contract(
            resolution_authority_path,
            event_date=day,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _blocked(f"production_probability_resolution_unavailable:{type(exc).__name__}")
    expected = (
        _text(lineage[2]),
        _text(lineage[3]),
        _text(lineage[5]),
        _text(lineage[7]),
    )
    active = (
        _text(authorized.get("prior_model_artifact_sha256")),
        _text(authorized.get("weak_likelihood_artifact_sha256")),
        _text(authorized.get("strong_lr_artifact_sha256")),
        _text(authorized.get("activation_contract_sha256")),
    )
    if expected != active:
        return _blocked("production_probability_activation_initial_lineage_mismatch")
    frozen = {
        "prior_context_id": _text(lineage[0]),
        "resolution_contract_id": _text(lineage[6]),
        "model_artifact_sha256": _text(lineage[5]),
    }
    if (
        _text(artifact.get("model_artifact_sha256")) != frozen["model_artifact_sha256"]
        or artifact.get("production_runtime_eligible") is not True
        or artifact.get("shadow_only") is not False
    ):
        return _blocked("production_probability_strong_artifact_binding_mismatch")
    if _text(resolution.get("resolution_contract_id")) != frozen["resolution_contract_id"]:
        return _blocked("production_probability_resolution_contract_drift")
    if any(_text(previous.get(key)) != value for key, value in frozen.items()):
        return _blocked("production_probability_checkpoint_frozen_context_drift")
    active_forecast_snapshot_id = _text(previous.get("forecast_evidence_snapshot_id"))
    forecast_peak_timing_prior = dict(
        _mapping(_mapping(lineage_json).get("forecast_peak_timing_prior"))
    )
    if active_forecast_snapshot_id != _text(lineage[4]):
        try:
            snapshot = conn.execute(
                """SELECT weak_likelihood_artifact_sha256,forecast_peak_timing_prior_json,state
                   FROM weak_forecast_evidence_snapshots
                   WHERE forecast_evidence_snapshot_id=? AND target_identity=?""",
                (active_forecast_snapshot_id, target),
            ).fetchone()
        except sqlite3.Error:
            snapshot = None
        if (
            snapshot is None
            or _text(snapshot[0]) != _text(authorized.get("weak_likelihood_artifact_sha256"))
            or _text(snapshot[2]) != "active"
        ):
            return _blocked("production_probability_active_weak_snapshot_lineage_invalid")
        try:
            forecast_peak_timing_prior = dict(json.loads(_text(snapshot[1])))
        except (TypeError, ValueError):
            return _blocked("production_probability_active_weak_timing_prior_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "ready",
        "target_identity": target,
        "event_date": day,
        "public_slug": public_slug,
        "activation": authorized,
        "runtime_manifest_sha256": current_manifest_sha256,
        "active_replay_generation": active_replay_generation,
        "previous_checkpoint": previous,
        "forecast_evidence_snapshot_id": active_forecast_snapshot_id,
        "forecast_peak_timing_prior": forecast_peak_timing_prior,
        "prior_context_id": frozen["prior_context_id"],
        "strong_lr_artifact": artifact,
        "resolution_contract": resolution,
        "source_reliability_state": dict(_mapping(previous.get("source_reliability_state"))),
        "places_orders": False,
    }


def build_production_canonical_event_candidate(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    canonical_observation_event_id: str,
    started_at: str,
    completed_at: str,
    public_slug: str = "",
    strong_lr_artifact: Mapping[str, Any] | None = None,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
) -> dict[str, Any]:
    """Build one deterministic production update from a persisted METAR/SPECI."""

    cycle = load_production_probability_cycle(
        conn,
        activation=activation,
        event_date=event_date,
        strong_lr_artifact=strong_lr_artifact,
        resolution_authority_path=resolution_authority_path,
    )
    if cycle.get("ok") is not True:
        return dict(cycle)
    loaded = load_canonical_observation_event(
        conn,
        canonical_observation_event_id=canonical_observation_event_id,
    )
    if loaded.get("ok") is not True:
        return _blocked("production_canonical_observation_unavailable", canonical_event=loaded)
    event = _mapping(loaded.get("canonical_observation_event"))
    t1 = _time(event.get("first_actionable_seen_at"))
    predecessor_as_of = _time(_mapping(cycle.get("previous_checkpoint")).get("committed_at"))
    started = _time(started_at)
    completed = _time(completed_at)
    if t1 is None or predecessor_as_of is None or started is None or completed is None or completed < started:
        return _blocked("production_canonical_event_runtime_timestamps_invalid")
    decision_time = max(t1, predecessor_as_of)
    if started < decision_time:
        return _blocked("production_canonical_event_runtime_before_predecessor_asof")
    target = _text(cycle.get("target_identity"))
    day = _text(cycle.get("event_date"))
    frozen_public_slug = _text(cycle.get("public_slug"))
    supplied_public_slug = _text(public_slug)
    if supplied_public_slug and supplied_public_slug != frozen_public_slug:
        return _blocked("production_probability_public_slug_drift")
    slug = frozen_public_slug
    previous = _mapping(cycle.get("previous_checkpoint"))
    fact_snapshot_id = build_fact_snapshot_id(
        slug=slug,
        event_date=day,
        fact_as_of=_iso(decision_time),
        source_event_ids=[_text(canonical_observation_event_id)],
        identity_salt={"previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id"))},
    )
    correlation_id = build_correlation_id(
        target,
        canonical_observation_event_id,
        previous.get("posterior_checkpoint_id"),
    )
    outbox_event_id = f"pout_{_sha({'fact_snapshot_id': fact_snapshot_id, 'correlation_id': correlation_id})[:24]}"
    candidate = build_canonical_event_transaction_b_candidate(
        conn,
        candidate_mode="production",
        previous_checkpoint=previous,
        canonical_observation_event_id=canonical_observation_event_id,
        forecast_evidence_snapshot_id=_text(cycle.get("forecast_evidence_snapshot_id")),
        prior_context_id=_text(cycle.get("prior_context_id")),
        approved_lr_artifact=_mapping(cycle.get("strong_lr_artifact")),
        resolution_contract=_mapping(cycle.get("resolution_contract")),
        realization_policy=_realization_policy(),
        temperature_quantization_c=0.25,
        fact_snapshot_id=fact_snapshot_id,
        correlation_id=correlation_id,
        outbox_event_id=outbox_event_id,
        decision_as_of=_iso(decision_time),
        # The inference budget starts at the effective Bayes decision clock.
        # A cutover replay may occur after T1; execution eligibility still
        # remains bound separately to canonical T1+3s by the outbox contract.
        deadline_at=_iso(decision_time + timedelta(seconds=3)),
        started_at=_iso(started),
        completed_at=_iso(completed),
        source_reliability_before=_mapping(cycle.get("source_reliability_state")),
        source_reliability_after=_mapping(cycle.get("source_reliability_state")),
        path_auxiliary={
            "production_probability_mainline": True,
            "strict_asof_oos_not_live_gate": True,
            "termination_risk_is_posterior_marginal": True,
            "forecast_peak_timing_prior": dict(
                _mapping(cycle.get("forecast_peak_timing_prior"))
            ),
            "places_orders": False,
        },
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": candidate.get("ok") is True,
        "state": candidate.get("state"),
        "cycle": cycle,
        "candidate": candidate,
        "public_slug": slug,
        "fact_snapshot_id": fact_snapshot_id,
        "correlation_id": correlation_id,
        "places_orders": False,
    }


def commit_production_canonical_event(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    prepared: Mapping[str, Any],
    completed_at: str,
) -> dict[str, Any]:
    """Commit the prepared probability update; this boundary never orders."""

    value = _mapping(prepared)
    candidate = _mapping(value.get("candidate"))
    cycle = _mapping(value.get("cycle"))
    if value.get("ok") is not True:
        return _blocked("production_canonical_event_candidate_not_ready")
    if _text(candidate.get("state")) == "duplicate_or_confirmation":
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "duplicate_or_confirmation",
            "places_orders": False,
        }
    host = ProductionSequentialBayesTransactionBHost(conn, activation=activation)
    previous = _mapping(cycle.get("previous_checkpoint"))
    if _text(candidate.get("state")) == "physical_only_candidate_ready":
        result = host.commit_physical_only_candidate(
            candidate=candidate,
            previous_checkpoint=previous,
            slug=_text(value.get("public_slug")),
            event_date=_text(cycle.get("event_date")),
            fact_as_of=_text(completed_at),
            fact_snapshot_id=_text(value.get("fact_snapshot_id")),
            correlation_id=_text(value.get("correlation_id")),
        )
    elif _text(candidate.get("state")) == "candidate_ready":
        result = host.commit_candidate(
            candidate=candidate,
            previous_checkpoint=previous,
            slug=_text(value.get("public_slug")),
            event_date=_text(cycle.get("event_date")),
            fact_as_of=_text(completed_at),
        )
    else:
        return _blocked("production_canonical_event_candidate_state_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": result.get("ok") is True,
        "state": result.get("state"),
        "transaction_b": result,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "build_production_canonical_event_candidate",
    "commit_production_canonical_event",
    "load_production_probability_cycle",
]
