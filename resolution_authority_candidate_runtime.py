"""Read-only discovery of G1 final X/S authority candidates.

This module never promotes a market hint, an AMSC intraday sequence, or a
legacy scalar into a final label source.  It simply turns the currently
available market metadata and raw-archive coverage into an explicit candidate
packet so the final authority decision can be frozen once, audited, and then
implemented without inventing a source role in a model runtime.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from amsc_raw_history_archive import build_amsc_daily_coverage_manifest


SCHEMA_VERSION = "resolution_authority_candidate_audit_v1"
PHYSICAL_ROLE = "physical_daily_high_final_truth"
SETTLEMENT_ROLE = "resolution_settlement_final"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    )


def _market_resolution_rows(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    slug: str,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "event_runs"):
        return []
    clauses = ["event_date=?", "COALESCE(resolution_source,'')<>''"]
    params: list[Any] = [_text(event_date)[:10]]
    if _text(slug):
        clauses.append("slug=?")
        params.append(_text(slug))
    rows = conn.execute(
        f"""
        SELECT id,slug,event_date,resolution_source,station_id,market_var,
               market_unit,tz,rounding_rule,created_at
        FROM event_runs
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC,id DESC
        """,
        params,
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        source = _text(row[3])
        key = (_text(row[1]), source)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "event_run_id": int(row[0] or 0),
                "slug": _text(row[1]),
                "event_date": _text(row[2])[:10],
                "resolution_source": source,
                "station_id": _text(row[4]).upper(),
                "market_var": _text(row[5]),
                "market_unit": _text(row[6]),
                "timezone": _text(row[7]),
                "rounding_rule": _text(row[8]),
                "metadata_seen_at": _text(row[9]),
            }
        )
    return result


def _settlement_candidate(row: dict[str, Any]) -> dict[str, Any]:
    source = _text(row.get("resolution_source"))
    source_lower = source.lower()
    is_wu_daily = "wunderground.com/history/daily/" in source_lower
    return {
        "candidate_id": "wunderground_daily_resolution_candidate_v1" if is_wu_daily else "unrecognized_market_resolution_candidate_v1",
        "role_to_freeze": SETTLEMENT_ROLE,
        "source_url": source,
        "station_id": _text(row.get("station_id")),
        "market_var": _text(row.get("market_var")),
        "market_unit": _text(row.get("market_unit")),
        "resolution_local_date": _text(row.get("event_date")),
        "metadata_seen_at": _text(row.get("metadata_seen_at")),
        "candidate_kind": "market_declared_wunderground_daily_history" if is_wu_daily else "market_metadata_unrecognized_source",
        "eligible_to_promote_now": False,
        "promotion_blockers": [
            "market_resolution_rule_text_and_raw_endpoint_not_frozen",
            "immutable_final_settlement_raw_capture_not_available",
            "finality_revision_and_dispute_policy_not_frozen",
        ],
        "legacy_scalar_is_not_evidence": True,
    }


def _physical_candidate(
    *,
    archive_dir: Path | str,
    event_date: str,
    station_id: str,
) -> dict[str, Any]:
    try:
        coverage = build_amsc_daily_coverage_manifest(
            archive_dir,
            event_date=_text(event_date)[:10],
            station_id=_text(station_id).upper(),
        )
    except Exception as exc:
        coverage = {"state": "unavailable", "issues": [f"coverage_read_failed:{type(exc).__name__}"]}
    blockers = [
        "physical_final_source_not_explicitly_approved",
        "daily_finality_and_revision_policy_not_frozen",
        "half_hour_sampling_bias_not_accepted_as_final_physical_definition",
    ]
    if _text(coverage.get("day_state")) != "complete":
        blockers.append("amsc_physical_day_not_complete")
    if list(coverage.get("issues") or []):
        blockers.append("amsc_raw_coverage_integrity_or_completeness_not_ready")
    return {
        "candidate_id": "amsc_zuuu_half_hour_daily_max_candidate_v1",
        "role_to_freeze": PHYSICAL_ROLE,
        "source_id": "zuuu_metar_raw_amsc",
        "station_id": _text(station_id).upper(),
        "physical_local_date": _text(event_date)[:10],
        "candidate_kind": "raw_archived_half_hour_observational_daily_max",
        "coverage": coverage,
        "eligible_to_promote_now": False,
        "promotion_blockers": sorted(set(blockers)),
        "not_market_settlement_source": True,
    }


def build_resolution_authority_candidate_audit(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    archive_dir: Path | str,
    station_id: str = "ZUUU",
    slug: str = "",
) -> dict[str, Any]:
    """Return candidates and freezing requirements without setting any role."""

    day = _text(event_date)[:10]
    station = _text(station_id).upper()
    market_rows = _market_resolution_rows(conn, event_date=day, slug=slug)
    settlement_candidates = [_settlement_candidate(row) for row in market_rows]
    physical = _physical_candidate(archive_dir=archive_dir, event_date=day, station_id=station)
    blockers: list[str] = ["physical_final_source_authority_not_frozen"]
    if not settlement_candidates:
        blockers.append("market_resolution_source_candidate_missing")
    else:
        blockers.append("settlement_final_source_authority_not_frozen")
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "authority_decision_required",
        "event_date": day,
        "station_id": station,
        "slug": _text(slug),
        "physical_candidate": physical,
        "settlement_candidates": settlement_candidates,
        "required_freeze": {
            "physical": [
                "source_id_and_immutable_raw_endpoint",
                "station_mapping_and_local_day_boundary",
                "daily_max_definition_and_sampling_acceptance",
                "finality_close_time_correction_and_revision_policy",
            ],
            "settlement": [
                "market_rule_text_hash_and_resolution_source_url",
                "immutable_final_raw_capture_endpoint_and_payload_hash",
                "unit_rounding_and_bucket_contract",
                "finality_cancellation_dispute_and_revision_policy",
            ],
            "joint": [
                "versioned_p_S_given_X_kernel",
                "explicit_X_to_S_to_B_contract_identity",
                "append_only_final_label_registry_destination",
            ],
        },
        "blockers": sorted(set(blockers)),
        "boundary": {
            "does_not_assign_final_source_roles": True,
            "does_not_promote_legacy_scalar": True,
            "does_not_write_registry_or_source_observations": True,
            "does_not_train_or_activate_artifacts": True,
        },
        "read_only": True,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "build_resolution_authority_candidate_audit"]
