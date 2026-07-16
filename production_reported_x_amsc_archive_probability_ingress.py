"""Raw-archived AMSC METAR/SPECI ingress for the reported-X successor."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Mapping

from archive_db_write_queue import archive_db_write_gate, connect_archive_db_rw
from joint_weak_live_evidence_runtime import DEFAULT_CURVE_ROOT
from lineage_identity_runtime import build_correlation_id
from p2_canonical_amsc_forward_writer import apply_p2_canonical_amsc_forward_append
from production_probability_p0_runtime import (
    DEFAULT_NCEI_ROOT,
    DEFAULT_RESOLUTION_AUTHORITY,
    build_production_p0_inputs,
    build_zuuu_physical_public_slug,
)
from production_probability_successor_activation_contract import (
    PRODUCTION_PATH as DEFAULT_ACTIVATION_PATH,
    load_successor_activation,
    validate_successor_activation,
)
from production_reported_x_canonical_event_drain_runtime import (
    drain_reported_x_canonical_events,
)
from production_reported_x_initial_checkpoint import (
    compile_reported_x_initial_checkpoint,
    rebase_reported_x_initial_successor,
)
from production_reported_x_initial_writer import commit_reported_x_initial_posterior
from weather_fact_snapshot_writer import persist_source_observations_for_payload, utc_now_iso


SCHEMA_VERSION = "production_reported_x_amsc_archive_probability_ingress_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def ensure_reported_x_daily_p0(
    conn,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    decision_as_of: str,
    curve_root: Path | str = DEFAULT_CURVE_ROOT,
    weak_artifact_path: Path | str | None = None,
    ncei_root: Path | str = DEFAULT_NCEI_ROOT,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
) -> dict[str, Any]:
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    authorized = validate_successor_activation(activation)
    rows = conn.execute(
        """SELECT l.posterior_checkpoint_id,p.replay_generation,p.checkpoint_kind,p.state
           FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?
           ORDER BY l.created_at,l.rowid""",
        (target, _text(authorized.get("activation_contract_sha256"))),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("reported_x_daily_p0_lineage_not_unique")
    if len(rows) == 1:
        if _text(rows[0][2]) != "initial" or _text(rows[0][3]) != "committed":
            raise ValueError("reported_x_daily_p0_existing_lineage_invalid")
        return {
            "schema": "reported_x_daily_p0_ensure_v1",
            "ok": True,
            "state": "already_initialized",
            "event_date": day,
            "target_identity": target,
            "posterior_checkpoint_id": _text(rows[0][0]),
            "replay_generation": int(rows[0][1] or 0),
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
    if inputs.get("ok") is not True:
        return {
            "schema": "reported_x_daily_p0_ensure_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": list(inputs.get("block_reasons") or ["reported_x_p0_inputs_blocked"]),
            "places_orders": False,
        }
    weak = dict((inputs.get("weak_likelihood") or {}).get("joint_weak_likelihood") or {})
    candidate = compile_reported_x_initial_checkpoint(
        activation=authorized,
        target_identity=target,
        event_date=day,
        station_id="ZUUU",
        decision_as_of=_text(decision_as_of),
        prior_context=dict(inputs.get("prior_context") or {}),
        joint_weak_likelihood=weak,
        active_strong_lr_artifact=dict(inputs.get("strong_lr_artifact") or {}),
        resolution_contract=dict(inputs.get("resolution_contract") or {}),
        source_reliability_initial_state={
            "amsc_zuuu_scheduled_metar_daily_max_v2": {"state": "unknown"},
            "amsc_zuuu_speci_realtime_v1": {"state": "unknown"},
            "awc_zuuu_metar_crosscheck": {"state": "unknown"},
        },
    )
    if candidate.get("ok") is not True:
        return {
            "schema": "reported_x_daily_p0_ensure_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": list(candidate.get("block_reasons") or ["reported_x_p0_compile_blocked"]),
            "places_orders": False,
        }
    existing = conn.execute(
        """SELECT posterior_checkpoint_id,replay_generation FROM posterior_checkpoints
           WHERE target_identity=? AND state='committed'
           ORDER BY replay_generation DESC,rowid DESC""",
        (target,),
    ).fetchall()
    if existing:
        candidate = rebase_reported_x_initial_successor(
            candidate,
            replay_generation=max(int(row[1] or 0) for row in existing) + 1,
            supersedes_checkpoint_id=_text(existing[0][0]),
        )
    committed = commit_reported_x_initial_posterior(
        conn,
        activation=authorized,
        initial_checkpoint=candidate,
        slug=build_zuuu_physical_public_slug(day),
        event_date=day,
        fact_as_of=_text(decision_as_of),
        correlation_id=build_correlation_id("reported_x_daily_p0", day, decision_as_of),
    )
    return {
        "schema": "reported_x_daily_p0_ensure_v1",
        "ok": committed.get("ok") is True,
        "state": "initialized",
        "event_date": day,
        "target_identity": target,
        "posterior_checkpoint_id": committed.get("posterior_checkpoint_id"),
        "replay_generation": int(candidate.get("checkpoint", {}).get("replay_generation") or 0),
        "places_orders": False,
    }


def apply_reported_x_amsc_archive_probability_ingress(
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
    db = Path(db_path).expanduser().resolve()
    archive = Path(raw_archive_dir).expanduser().resolve()
    started = time.monotonic()
    stages: dict[str, float] = {}
    source_result: dict[str, Any] = {"ok": True, "state": "not_requested_already_persisted"}
    incremental_source_observation_ids: list[str] | None = None
    with archive_db_write_gate(db, owner="reported_x_amsc_probability_fast_lane", wait_sec=writer_queue_wait_sec):
        stages["writer_queue_wait_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        canonical_connection_started = time.monotonic()
        with connect_archive_db_rw(
            db,
            owner="reported_x_amsc_canonical_ingress",
            queue_wait_sec=writer_queue_wait_sec,
            timeout_sec=db_timeout_sec,
            busy_timeout_ms=db_busy_timeout_ms,
            wal_autocheckpoint_pages=0,
        ) as conn:
            source_started = time.monotonic()
            if source_observation_payload is not None:
                payload_rows = source_observation_payload.get("source_observations")
                event_day = _text(event_date)[:10]
                primary_rows = [
                    row
                    for row in (payload_rows if isinstance(payload_rows, list) else [])
                    if isinstance(row, Mapping)
                    and _text(row.get("source_id")) == "zuuu_metar_raw_amsc"
                    and _text(row.get("event_date"))[:10] == event_day
                ]
                if any(not _text(row.get("source_observation_id")) for row in primary_rows):
                    raise ValueError("reported_x_primary_source_observation_identity_missing")
                payload_ids = sorted({_text(row.get("source_observation_id")) for row in primary_rows})
                existing_ids = {
                    _text(row[0])
                    for row in conn.execute(
                        "SELECT source_observation_id FROM canonical_observation_event_sources "
                        "WHERE source_observation_id IN (" + ",".join("?" for _ in payload_ids) + ")",
                        tuple(payload_ids),
                    ).fetchall()
                } if payload_ids else set()
                incremental_source_observation_ids = [
                    value for value in payload_ids if value not in existing_ids
                ]
                incremental_id_set = set(incremental_source_observation_ids)
                # A0 is the strong-observation hot path, not the complete
                # provider-window archive projection. Retain the original
                # payload metadata but persist/revalidate only primary rows
                # that still need a canonical event. TAF and compatibility
                # rows remain in the immutable raw plan for post-T3 lanes.
                incremental_payload = dict(source_observation_payload)
                incremental_payload["source_observations"] = [
                    row
                    for row in primary_rows
                    if _text(row.get("source_observation_id")) in incremental_id_set
                ]
                conn.execute("BEGIN IMMEDIATE")
                try:
                    source_result = persist_source_observations_for_payload(
                        conn,
                        incremental_payload,
                        created_at=str(source_observation_created_at or utc_now_iso()),
                    )
                    if source_result.get("ok") is not True or source_result.get("state") != "available":
                        raise ValueError("reported_x_source_observation_ingress_blocked")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                source_result = dict(source_result)
                source_result["provider_window_primary_candidate_count"] = len(primary_rows)
                source_result["incremental_primary_candidate_count"] = len(
                    incremental_source_observation_ids
                )
                source_result["provider_window_non_primary_rows_deferred_after_t3"] = max(
                    0,
                    len(payload_rows if isinstance(payload_rows, list) else []) - len(primary_rows),
                )
            stages["source_observation_append_ms"] = round(
                (time.monotonic() - source_started) * 1000.0, 3
            )
            canonical_started = time.monotonic()
            canonical = apply_p2_canonical_amsc_forward_append(
                conn,
                event_date=_text(event_date)[:10],
                station_id="ZUUU",
                raw_archive_dir=archive,
                decision_as_of=_text(decision_as_of),
                source_observation_ids=incremental_source_observation_ids,
            )
            stages["canonical_append_ms"] = round(
                (time.monotonic() - canonical_started) * 1000.0, 3
            )
        stages["source_and_canonical_connection_ms"] = round(
            (time.monotonic() - canonical_connection_started) * 1000.0, 3
        )
        activation_started = time.monotonic()
        try:
            activation = load_successor_activation(activation_path)
        except ValueError as exc:
            stages["activation_load_ms"] = round(
                (time.monotonic() - activation_started) * 1000.0, 3
            )
            stages["total_ingress_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "canonical_retained_probability_activation_blocked",
                "source_observations": source_result,
                "canonical": canonical,
                "probability": {"ok": False, "state": "blocked", "reason": str(exc)},
                "latency_stages_ms": stages,
                "places_orders": False,
            }
        stages["activation_load_ms"] = round(
            (time.monotonic() - activation_started) * 1000.0, 3
        )
        probability_connection_started = time.monotonic()
        with connect_archive_db_rw(
            db,
            owner="reported_x_amsc_probability_update",
            queue_wait_sec=writer_queue_wait_sec,
            timeout_sec=db_timeout_sec,
            busy_timeout_ms=db_busy_timeout_ms,
            wal_autocheckpoint_pages=0,
        ) as conn:
            p0_started = time.monotonic()
            daily_p0 = ensure_reported_x_daily_p0(
                conn,
                activation=activation,
                event_date=_text(event_date)[:10],
                decision_as_of=_text(decision_as_of),
                resolution_authority_path=resolution_authority_path,
            )
            stages["daily_p0_ensure_ms"] = round(
                (time.monotonic() - p0_started) * 1000.0, 3
            )
            drain_started = time.monotonic()
            probability = (
                drain_reported_x_canonical_events(
                    conn,
                    activation=activation,
                    event_date=_text(event_date)[:10],
                    public_slug=public_slug,
                    resolution_authority_path=resolution_authority_path,
                    max_events=max_events,
                )
                if daily_p0.get("ok") is True
                else {"ok": False, "state": "blocked", "reason": "reported_x_daily_p0_unavailable"}
            )
            stages["canonical_probability_drain_ms"] = round(
                (time.monotonic() - drain_started) * 1000.0, 3
            )
        stages["probability_connection_ms"] = round(
            (time.monotonic() - probability_connection_started) * 1000.0, 3
        )
    stages["total_ingress_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    ok = canonical.get("ok") is True and daily_p0.get("ok") is True and probability.get("ok") is True
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": "canonical_and_reported_x_probability_committed" if ok else "canonical_retained_probability_blocked",
        "source_observations": source_result,
        "canonical": canonical,
        "daily_p0": daily_p0,
        "probability": probability,
        "latency_stages_ms": stages,
        "places_orders": False,
        "boundary": {
            "canonical_fact_commits_before_probability": True,
            "speci_is_soft_strong_evidence_only": True,
            "regular_metar_only_x_hard_bound": True,
            "public_ssot_is_reported_x_only": True,
            "strict_asof_oos_not_live_gate": True,
            "no_market_or_execution": True,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "apply_reported_x_amsc_archive_probability_ingress",
    "ensure_reported_x_daily_p0",
]
