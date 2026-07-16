"""Raw-archived AMSC METAR/SPECI to production probability SSOT ingress."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from archive_db_write_queue import archive_db_write_gate, connect_archive_db_rw
from joint_weak_live_evidence_runtime import DEFAULT_CURVE_ROOT
from lineage_identity_runtime import build_correlation_id
from p2_canonical_amsc_forward_writer import apply_p2_canonical_amsc_forward_append
from production_canonical_event_drain_runtime import drain_production_canonical_events
from production_initial_posterior_checkpoint import rebase_production_initial_posterior_successor
from production_initial_posterior_writer import commit_production_initial_posterior
from production_probability_activation_contract import (
    DEFAULT_PATH as DEFAULT_ACTIVATION_PATH,
    load_production_probability_activation,
    validate_production_probability_activation,
)
from production_probability_p0_runtime import (
    DEFAULT_NCEI_ROOT,
    DEFAULT_RESOLUTION_AUTHORITY,
    build_production_p0_inputs,
    build_zuuu_physical_public_slug,
    compile_production_p0_candidate,
)
from weather_fact_snapshot_writer import persist_source_observations_for_payload, utc_now_iso


SCHEMA_VERSION = "production_amsc_archive_probability_ingress_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def ensure_production_daily_p0(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    decision_as_of: str,
    curve_root: Path | str = DEFAULT_CURVE_ROOT,
    weak_artifact_path: Path | str | None = None,
    ncei_root: Path | str = DEFAULT_NCEI_ROOT,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
) -> dict[str, Any]:
    """Idempotently create the current activation's P0 for one physical day.

    The first AMSC observation after local midnight is allowed to discover
    that P0 is absent.  P0 is then compiled from prior + the one joint weak
    likelihood only, committed atomically, and the already-durable canonical
    observation is consumed afterwards.  Market discovery/finality and the
    X→S→B bridge are deliberately outside this boundary.
    """

    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    authorized = validate_production_probability_activation(activation)
    activation_sha = _text(authorized.get("activation_contract_sha256"))
    rows = conn.execute(
        """SELECT l.posterior_checkpoint_id,p.replay_generation,p.checkpoint_kind,p.state
           FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?
           ORDER BY l.created_at,l.rowid""",
        (target, activation_sha),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("production_daily_p0_current_activation_lineage_not_unique")
    if len(rows) == 1:
        row = rows[0]
        if _text(row[2]) != "initial" or _text(row[3]) != "committed":
            raise ValueError("production_daily_p0_existing_lineage_invalid")
        return {
            "schema": "production_daily_p0_ensure_v1",
            "ok": True,
            "state": "already_initialized",
            "event_date": day,
            "target_identity": target,
            "posterior_checkpoint_id": _text(row[0]),
            "replay_generation": int(row[1] or 0),
            "market_discovery_required": False,
            "places_orders": False,
        }

    inputs = build_production_p0_inputs(
        event_date=day,
        decision_as_of=_text(decision_as_of),
        curve_root=curve_root,
        weak_artifact_path=weak_artifact_path,
        ncei_root=ncei_root,
        resolution_authority_path=resolution_authority_path,
    )
    compiled = compile_production_p0_candidate(activation=authorized, inputs=inputs)
    if inputs.get("ok") is not True or compiled.get("ok") is not True:
        return {
            "schema": "production_daily_p0_ensure_v1",
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "target_identity": target,
            "block_reasons": list(
                inputs.get("block_reasons") or compiled.get("block_reasons") or ["production_daily_p0_inputs_not_ready"]
            ),
            "market_discovery_required": False,
            "places_orders": False,
        }

    existing = conn.execute(
        """SELECT posterior_checkpoint_id,replay_generation
           FROM posterior_checkpoints
           WHERE target_identity=? AND state='committed'
           ORDER BY replay_generation DESC,rowid DESC""",
        (target,),
    ).fetchall()
    candidate = compiled["candidate"]
    if existing:
        candidate = rebase_production_initial_posterior_successor(
            candidate,
            replay_generation=max(int(row[1] or 0) for row in existing) + 1,
            supersedes_checkpoint_id=_text(existing[0][0]),
        )
    committed = commit_production_initial_posterior(
        conn,
        activation=authorized,
        initial_checkpoint=candidate,
        slug=build_zuuu_physical_public_slug(day),
        event_date=day,
        fact_as_of=_text(decision_as_of),
        correlation_id=build_correlation_id("production_daily_p0", day, decision_as_of),
    )
    return {
        "schema": "production_daily_p0_ensure_v1",
        "ok": committed.get("ok") is True,
        "state": "initialized",
        "event_date": day,
        "target_identity": target,
        "posterior_checkpoint_id": committed.get("posterior_checkpoint_id"),
        "replay_generation": int(candidate.get("checkpoint", {}).get("replay_generation") or 0),
        "weak_source_set": (
            (inputs.get("weak_likelihood") or {}).get("joint_weak_likelihood") or {}
        ).get("weak_source_set"),
        "market_discovery_required": False,
        "places_orders": False,
    }


def apply_production_amsc_archive_probability_ingress(
    *,
    db_path: Path | str,
    raw_archive_dir: Path | str,
    event_date: str,
    decision_as_of: str,
    activation_path: Path | str = DEFAULT_ACTIVATION_PATH,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
    public_slug: str = "",
    max_events: int = 64,
    source_observation_payload: Mapping[str, Any] | None = None,
    source_observation_created_at: str = "",
    writer_queue_wait_sec: float | None = None,
    db_timeout_sec: float = 10.0,
    db_busy_timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Commit canonical facts first, then advance the posterior independently.

    The canonical AMSC fact remains durable if activation/P0/inference blocks.
    No market, decision, signer, outbox execution or order is owned here.
    """

    db = Path(db_path).expanduser().resolve()
    archive = Path(raw_archive_dir).expanduser().resolve()
    source_observations: dict[str, Any] = {
        "schema": "source_observation_persistence_v1",
        "ok": True,
        "state": "not_requested_already_persisted",
        "attempted": 0,
        "inserted": 0,
    }
    ingress_started = time.monotonic()
    stage_ms: dict[str, float] = {}
    # Claim the filesystem writer queue once for A0/A1 and Transaction B.
    # SQLite transactions remain separate, so canonical evidence still
    # survives inference failure, but no lower-priority archive writer can
    # jump into the gap and consume the rest of the three-second budget.
    with archive_db_write_gate(
        db,
        owner="production_amsc_probability_fast_lane",
        wait_sec=writer_queue_wait_sec,
    ):
        stage_ms["writer_queue_wait_ms"] = round((time.monotonic() - ingress_started) * 1000.0, 3)
        source_started = time.monotonic()
        canonical_connection_started = time.monotonic()
        with connect_archive_db_rw(
            db,
            owner="production_amsc_canonical_ingress",
            queue_wait_sec=writer_queue_wait_sec,
            timeout_sec=db_timeout_sec,
            busy_timeout_ms=db_busy_timeout_ms,
            wal_autocheckpoint_pages=0,
        ) as conn:
            if source_observation_payload is not None:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    source_observations = persist_source_observations_for_payload(
                        conn,
                        source_observation_payload,
                        created_at=str(source_observation_created_at or utc_now_iso()),
                    )
                    if source_observations.get("ok") is not True:
                        raise ValueError("amsc_source_observation_fast_ingress_blocked")
                    if source_observations.get("state") != "available":
                        raise ValueError("amsc_source_observation_schema_not_available")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            stage_ms["source_observation_commit_ms"] = round(
                (time.monotonic() - source_started) * 1000.0, 3
            )
            canonical_started = time.monotonic()
            canonical = apply_p2_canonical_amsc_forward_append(
                conn,
                event_date=str(event_date)[:10],
                station_id="ZUUU",
                raw_archive_dir=archive,
                decision_as_of=str(decision_as_of),
            )
            stage_ms["canonical_commit_ms"] = round(
                (time.monotonic() - canonical_started) * 1000.0, 3
            )
        canonical_connection_total_ms = round(
            (time.monotonic() - canonical_connection_started) * 1000.0, 3
        )
        stage_ms["canonical_connection_total_ms"] = canonical_connection_total_ms
        stage_ms["canonical_connection_close_ms"] = round(
            max(
                0.0,
                canonical_connection_total_ms
                - stage_ms["source_observation_commit_ms"]
                - stage_ms["canonical_commit_ms"],
            ),
            3,
        )
        activation_started = time.monotonic()
        try:
            activation = load_production_probability_activation(activation_path)
        except ValueError as exc:
            stage_ms["activation_load_ms"] = round(
                (time.monotonic() - activation_started) * 1000.0, 3
            )
            stage_ms["total_ingress_ms"] = round(
                (time.monotonic() - ingress_started) * 1000.0, 3
            )
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "canonical_retained_probability_activation_blocked",
                "source_observations": source_observations,
                "canonical": canonical,
                "probability": {"ok": False, "state": "blocked", "reason": str(exc)},
                "latency_stages_ms": stage_ms,
                "places_orders": False,
            }
        stage_ms["activation_load_ms"] = round(
            (time.monotonic() - activation_started) * 1000.0, 3
        )
        probability_started = time.monotonic()
        sequential_update_ms = 0.0
        with connect_archive_db_rw(
            db,
            owner="production_amsc_probability_update",
            queue_wait_sec=writer_queue_wait_sec,
            timeout_sec=db_timeout_sec,
            busy_timeout_ms=db_busy_timeout_ms,
            wal_autocheckpoint_pages=0,
        ) as conn:
            p0_started = time.monotonic()
            daily_p0 = ensure_production_daily_p0(
                conn,
                activation=activation,
                event_date=str(event_date)[:10],
                decision_as_of=str(decision_as_of),
                resolution_authority_path=resolution_authority_path,
            )
            stage_ms["daily_p0_ensure_ms"] = round(
                (time.monotonic() - p0_started) * 1000.0, 3
            )
            if daily_p0.get("ok") is True:
                sequential_started = time.monotonic()
                probability = drain_production_canonical_events(
                    conn,
                    activation=activation,
                    event_date=str(event_date)[:10],
                    public_slug=public_slug,
                    resolution_authority_path=resolution_authority_path,
                    max_events=max_events,
                )
                sequential_update_ms = round(
                    (time.monotonic() - sequential_started) * 1000.0, 3
                )
            else:
                probability = {
                    "ok": False,
                    "state": "blocked",
                    "reason": "production_daily_p0_unavailable",
                }
        stage_ms["probability_commit_ms"] = round(
            (time.monotonic() - probability_started) * 1000.0, 3
        )
        stage_ms["sequential_update_ms"] = sequential_update_ms
        stage_ms["probability_connection_close_ms"] = round(
            max(
                0.0,
                stage_ms["probability_commit_ms"]
                - stage_ms["daily_p0_ensure_ms"]
                - sequential_update_ms,
            ),
            3,
        )
    stage_ms["total_ingress_ms"] = round(
        (time.monotonic() - ingress_started) * 1000.0, 3
    )
    if daily_p0.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "canonical_retained_daily_p0_blocked",
            "source_observations": source_observations,
            "canonical": canonical,
            "daily_p0": daily_p0,
            "probability": probability,
            "latency_stages_ms": stage_ms,
            "places_orders": False,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": canonical.get("ok") is True and probability.get("ok") is True,
        "state": "canonical_and_probability_committed" if probability.get("ok") is True else "canonical_retained_probability_blocked",
        "source_observations": source_observations,
        "canonical": canonical,
        "daily_p0": daily_p0,
        "probability": probability,
        "latency_stages_ms": stage_ms,
        "places_orders": False,
        "boundary": {
            "amsc_metar_and_speci_are_realtime_observations": True,
            "speci_does_not_change_final_x_schedule_definition": True,
            "canonical_fact_commits_before_probability": True,
            "daily_p0_is_idempotently_ensured_before_observation_update": True,
            "daily_p0_does_not_wait_for_market_discovery_or_bridge": True,
            "source_observation_commits_before_canonical": True,
            "single_writer_queue_claim_across_a0_a1_and_transaction_b": True,
            "legacy_fact_projection_precedes_probability": False,
            "strict_asof_oos_not_live_gate": True,
            "no_market_or_execution": True,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "apply_production_amsc_archive_probability_ingress",
    "ensure_production_daily_p0",
]
