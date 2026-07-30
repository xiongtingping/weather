"""Bridge canonical AMSC/AWC observations into one target P1 checkpoint."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import ContractError, ProbabilityEnvelope
from .materialization import insert_probability_envelope, materialize_p1


def materialize_p1_from_canonical_observation(
    conn: sqlite3.Connection,
    *,
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    prediction_root: ProbabilityEnvelope | Mapping[str, Any],
    observation_id: str,
    trajectory_artifact: Mapping[str, Any],
    trajectory_proof: Mapping[str, Any],
    trajectory_observations: Sequence[Mapping[str, Any]],
    bucket_centers_c: Mapping[str, Any],
    bucket_upper_bounds_c: Mapping[str, Any],
    reported_high_advanced: bool,
    decision_at_utc: str,
    fresh_until_utc: str,
    created_at_utc: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Validate one full-prefix trajectory proof against canonical authority."""

    event_id = str(observation_id or "").strip()
    if not event_id:
        raise ContractError("canonical_observation_id_missing")
    from_target_fact = False
    target_fact_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_target_observation_facts'"
    ).fetchone()
    if target_fact_table is not None:
        row = conn.execute(
            "SELECT observation_id,source_lane,station_id,report_type,raw_sha256,canonical_committed_at,0 "
            "FROM basket_target_observation_facts WHERE observation_id=?",
            (event_id,),
        ).fetchone()
        from_target_fact = row is not None
        # Historical in-memory contract fixtures contain the old source table
        # but no M5 fact row. Keep that fixture path only in memory; a file
        # backed target remains target-fact-only and fails closed below.
        if row is None:
            db_path = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
            legacy_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reported_trajectory_observations'"
            ).fetchone()
            if db_path in {"", ":memory:"} and legacy_table is not None:
                row = conn.execute(
                    "SELECT observation_id,source_lane,station_id,report_type,raw_sha256,canonical_committed_at,correction_suspected "
                    "FROM reported_trajectory_observations WHERE observation_id=?",
                    (event_id,),
                ).fetchone()
    else:
        # In-memory contract fixtures predate the isolated M5 fact table. A
        # file-backed target database must never use this compatibility path.
        db_path = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
        if db_path not in {"", ":memory:"}:
            raise ContractError("target_canonical_fact_table_missing")
        row = conn.execute(
            "SELECT observation_id,source_lane,station_id,report_type,raw_sha256,canonical_committed_at,correction_suspected "
            "FROM reported_trajectory_observations WHERE observation_id=?",
            (event_id,),
        ).fetchone()
    if row is None:
        raise ContractError("canonical_observation_missing")
    if str(row[1] or "").lower() not in {"amsc", "awc"}:
        raise ContractError("canonical_observation_source_lane_invalid")
    if str(row[3] or "").upper() not in {"METAR", "SPECI"}:
        raise ContractError("canonical_observation_report_type_invalid")
    if int(row[6] or 0) != 0:
        raise ContractError("canonical_observation_correction_requires_review")
    canonical_sha = str(row[4] or "").strip().lower()
    if len(canonical_sha) != 64 or any(char not in "0123456789abcdef" for char in canonical_sha):
        raise ContractError("canonical_observation_sha_invalid")
    canonical_committed_at = str(row[5] or "").strip()
    if not canonical_committed_at:
        raise ContractError("canonical_observation_committed_at_missing")
    try:
        committed_at = datetime.fromisoformat(canonical_committed_at.replace("Z", "+00:00"))
        decision_at = datetime.fromisoformat(str(decision_at_utc or "").strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("canonical_observation_asof_timestamp_invalid") from exc
    if committed_at.tzinfo is None or decision_at.tzinfo is None:
        raise ContractError("canonical_observation_asof_timezone_required")
    if committed_at > decision_at:
        raise ContractError("canonical_observation_not_strict_asof")
    proof_ids = tuple(
        str(item)
        for item in trajectory_proof.get(
            "canonical_observation_ids", ()
        )
    )
    if event_id not in proof_ids or proof_ids[-1] != event_id:
        raise ContractError(
            "canonical_observation_not_trajectory_proof_tail"
        )

    envelope = predecessor if isinstance(predecessor, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(predecessor)
    if str(row[2] or "") != envelope.station_id:
        raise ContractError("canonical_observation_station_mismatch")
    if from_target_fact:
        fact_event_date = str(
            conn.execute(
                "SELECT event_date FROM basket_target_observation_facts WHERE observation_id=?",
                (event_id,),
            ).fetchone()[0]
            or ""
        )
        if fact_event_date != envelope.event_date:
            raise ContractError("canonical_observation_event_date_mismatch")
    # materialize_p1 validates the complete predecessor scope and creates a
    # lineage with no raw source fields.
    result = materialize_p1(
        envelope,
        prediction_root=prediction_root,
        trajectory_artifact=trajectory_artifact,
        trajectory_proof=trajectory_proof,
        trajectory_observations=trajectory_observations,
        bucket_centers_c=bucket_centers_c,
        bucket_upper_bounds_c=bucket_upper_bounds_c,
        reported_high_advanced=reported_high_advanced,
        decision_at_utc=decision_at_utc,
        fresh_until_utc=fresh_until_utc,
        bridge_sha256=envelope.bridge_sha256,
    )
    savepoint = "basket_target_p1_canonical_ingress"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        receipt = insert_probability_envelope(
            conn,
            result,
            created_at_utc=created_at_utc or decision_at_utc,
        )
        if commit:
            conn.execute(f"RELEASE {savepoint}")
            conn.commit()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return {
        "schema": "bayes_basket_target_canonical_p1_ingress_v1",
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "observation_id": event_id,
        "source_lane": str(row[1]).lower(),
        "p1_checkpoint_id": result.checkpoint_id,
        "receipt": receipt,
        "places_orders": False,
        "calls_order_gateway": False,
    }


__all__ = ["materialize_p1_from_canonical_observation"]
