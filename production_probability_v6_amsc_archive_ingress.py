"""AMSC raw/canonical ingress wired to the unique production v6 drain."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from archive_db_write_queue import archive_db_write_gate, connect_archive_db_rw
from joint_weak_live_evidence_runtime import DEFAULT_CURVE_ROOT
from p2_canonical_amsc_forward_writer import apply_p2_canonical_amsc_forward_append
from production_probability_v6_p0_input_runtime import DEFAULT_NCEI_ROOT, DEFAULT_RESOLUTION_AUTHORITY
from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_canonical_event_drain_runtime import drain_v6_canonical_events, ensure_v6_daily_p0
from weather_fact_snapshot_writer import persist_source_observations_for_payload, utc_now_iso


SCHEMA_VERSION = "production_probability_v6_amsc_archive_ingress_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _load_activation(path: Path | str) -> dict[str, Any]:
    return validate_v6_activation(json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8")))


def apply_v6_amsc_archive_probability_ingress(
    *,
    db_path: Path | str,
    raw_archive_dir: Path | str,
    event_date: str,
    decision_as_of: str,
    activation_path: Path | str,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
    public_slug: str = "",
    max_events: int = 64,
    source_observation_payload: Mapping[str, Any] | None = None,
    source_observation_created_at: str = "",
    writer_queue_wait_sec: float | None = None,
    db_timeout_sec: float = 10.0,
    db_busy_timeout_ms: int = 5000,
    curve_root: Path | str = DEFAULT_CURVE_ROOT,
    weak_artifact_path: Path | str | None = None,
    ncei_root: Path | str = DEFAULT_NCEI_ROOT,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    archive = Path(raw_archive_dir).expanduser().resolve()
    day = _text(event_date)[:10]
    started = time.monotonic()
    stages: dict[str, float] = {}
    source_result: dict[str, Any] = {"ok": True, "state": "not_requested_already_persisted"}
    incremental_ids: list[str] | None = None
    with archive_db_write_gate(db, owner="physical_state_bayes_v6_amsc_ingress", wait_sec=writer_queue_wait_sec):
        with connect_archive_db_rw(
            db,
            owner="physical_state_bayes_v6_canonical_append",
            queue_wait_sec=writer_queue_wait_sec,
            timeout_sec=db_timeout_sec,
            busy_timeout_ms=db_busy_timeout_ms,
            wal_autocheckpoint_pages=0,
        ) as conn:
            if source_observation_payload is not None:
                stage_started = time.monotonic()
                rows = source_observation_payload.get("source_observations")
                primary = [
                    row for row in (rows if isinstance(rows, list) else [])
                    if isinstance(row, Mapping)
                    and _text(row.get("source_id")) == "zuuu_metar_raw_amsc"
                    and _text(row.get("event_date"))[:10] == day
                ]
                if any(not _text(row.get("source_observation_id")) for row in primary):
                    raise ValueError("v6_primary_source_observation_identity_missing")
                identities = sorted({_text(row.get("source_observation_id")) for row in primary})
                existing = {
                    _text(row[0])
                    for row in conn.execute(
                        "SELECT source_observation_id FROM canonical_observation_event_sources WHERE source_observation_id IN ("
                        + ",".join("?" for _ in identities) + ")",
                        tuple(identities),
                    ).fetchall()
                } if identities else set()
                incremental_ids = [identity for identity in identities if identity not in existing]
                incremental = dict(source_observation_payload)
                incremental["source_observations"] = [
                    row for row in primary if _text(row.get("source_observation_id")) in set(incremental_ids)
                ]
                conn.execute("BEGIN IMMEDIATE")
                try:
                    source_result = persist_source_observations_for_payload(
                        conn,
                        incremental,
                        created_at=_text(source_observation_created_at) or utc_now_iso(),
                    )
                    if source_result.get("ok") is not True:
                        raise ValueError("v6_source_observation_ingress_blocked")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                stages["source_observation_commit_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
            stage_started = time.monotonic()
            canonical = apply_p2_canonical_amsc_forward_append(
                conn,
                event_date=day,
                station_id="ZUUU",
                raw_archive_dir=archive,
                decision_as_of=_text(decision_as_of),
                source_observation_ids=incremental_ids,
            )
            stages["canonical_append_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
        stage_started = time.monotonic()
        activation = _load_activation(activation_path)
        stages["activation_load_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
        with connect_archive_db_rw(
            db,
            owner="physical_state_bayes_v6_probability_update",
            queue_wait_sec=writer_queue_wait_sec,
            timeout_sec=db_timeout_sec,
            busy_timeout_ms=db_busy_timeout_ms,
            wal_autocheckpoint_pages=0,
        ) as conn:
            stage_started = time.monotonic()
            p0 = ensure_v6_daily_p0(
                conn,
                activation=activation,
                event_date=day,
                decision_as_of=_text(decision_as_of),
                curve_root=curve_root,
                weak_artifact_path=weak_artifact_path,
                ncei_root=ncei_root,
                resolution_authority_path=resolution_authority_path,
            )
            stages["daily_p0_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
            stage_started = time.monotonic()
            probability = (
                drain_v6_canonical_events(
                    conn,
                    activation=activation,
                    event_date=day,
                    public_slug=public_slug,
                    max_events=max_events,
                )
                if p0.get("ok") is True
                else {"ok": False, "state": "blocked", "reason": "v6_daily_p0_unavailable"}
            )
            stages["canonical_probability_drain_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
    stages["total_ingress_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    ok = canonical.get("ok") is True and p0.get("ok") is True and probability.get("ok") is True
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": "canonical_and_v6_probability_committed" if ok else "canonical_retained_v6_probability_blocked",
        "source_observations": source_result,
        "canonical": canonical,
        "daily_p0": p0,
        "probability": probability,
        "latency_stages_ms": stages,
        "probability_ssot_3000ms_slo_met": bool(ok and stages["total_ingress_ms"] <= 3000.0),
        "places_orders": False,
        "boundary": {
            "canonical_fact_commits_before_probability": True,
            "speci_is_soft_strong_evidence_only": True,
            "regular_metar_only_x_hard_bound": True,
            "strict_asof_oos_not_live_gate": True,
            "v5_fallback_forbidden": True,
        },
    }


__all__ = ["SCHEMA_VERSION", "apply_v6_amsc_archive_probability_ingress"]
