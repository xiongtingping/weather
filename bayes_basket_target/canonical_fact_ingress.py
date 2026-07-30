"""Append canonical observation identities to the isolated target SSOT."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .et_history import assert_event_day_started
from .resource_bounds import assert_connection_target_capacity


SCHEMA = "bayes_basket_target_canonical_fact_ingress_v1"


def ingest_canonical_observation_evidence(
    conn: sqlite3.Connection,
    evidence: Mapping[str, Any],
    *,
    commit: bool = False,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Copy only accepted canonical identities; never copy weather payloads."""

    if evidence.get("schema") != "bayes_basket_target_canonical_observation_evidence_v1":
        raise ContractError("canonical_fact_evidence_schema_invalid")
    if commit:
        assert_connection_target_capacity(conn)
    if evidence.get("state") != "EXPORTED":
        raise ContractError("canonical_fact_evidence_blocked")
    checks = evidence.get("checks")
    if not isinstance(checks, Mapping) or checks.get("strict_timestamp_ordering") is not True:
        raise ContractError("canonical_fact_timestamp_check_failed")
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_target_observation_facts'"
    ).fetchone()
    if table is None:
        raise ContractError("target_observation_fact_table_missing")
    rows = []
    for raw in evidence.get("observations") or []:
        if not isinstance(raw, Mapping) or raw.get("qc_status") != "ACCEPTED":
            continue
        required = {
            "observation_id": str(raw.get("observation_id") or "").strip(),
            "station_id": str(raw.get("station_id") or "").strip(),
            "event_date": str(raw.get("event_date_local") or "").strip(),
            "settlement_timezone": str(
                raw.get("station_timezone") or ""
            ).strip(),
            "source_lane": str(raw.get("source_lane") or "").strip().upper(),
            "report_type": str(raw.get("report_type") or "").strip().upper(),
            "raw_sha256": str(raw.get("raw_sha256") or "").strip().lower(),
            "canonical_committed_at": str(raw.get("canonical_committed_at_utc") or "").strip(),
        }
        if not all(required.values()):
            raise ContractError("canonical_fact_required_field_missing")
        if required["source_lane"] not in {"AMSC", "AWC"}:
            raise ContractError("canonical_fact_source_lane_invalid")
        if required["report_type"] not in {"METAR", "SPECI"}:
            raise ContractError("canonical_fact_report_type_invalid")
        if len(required["raw_sha256"]) != 64:
            raise ContractError("canonical_fact_raw_sha_invalid")
        assert_event_day_started(
            required["event_date"],
            as_of_utc=required["canonical_committed_at"],
            target_timezone=required["settlement_timezone"],
        )
        identity = {
            "schema": "basket_target_observation_fact_v1",
            **{
                key: value
                for key, value in required.items()
                if key != "settlement_timezone"
            },
        }
        rows.append({
            **required,
            "observation_fact_id": "obsfact-" + content_sha256(identity)[:32],
            "content_sha256": content_sha256(identity),
        })
    rows.sort(key=lambda item: item["observation_id"])
    savepoint = "basket_target_canonical_fact_ingress"
    conn.execute(f"SAVEPOINT {savepoint}")
    inserted = 0
    replayed = 0
    try:
        for row in rows:
            existing = conn.execute(
                "SELECT content_sha256 FROM basket_target_observation_facts WHERE observation_id=?",
                (row["observation_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != row["content_sha256"]:
                    raise ContractError("canonical_fact_identity_conflict")
                replayed += 1
                continue
            conn.execute(
                "INSERT INTO basket_target_observation_facts(observation_fact_id,observation_id,station_id,event_date,source_lane,report_type,raw_sha256,canonical_committed_at,content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row["observation_fact_id"], row["observation_id"], row["station_id"], row["event_date"],
                    row["source_lane"], row["report_type"], row["raw_sha256"], row["canonical_committed_at"],
                    row["content_sha256"], created_at_utc or row["canonical_committed_at"],
                ),
            )
            inserted += 1
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
    result = {
        "schema": SCHEMA,
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "accepted_observation_count": len(rows),
        "inserted_count": inserted,
        "replayed_count": replayed,
        "writes_shared_live_database": False,
        "writes_target_database": bool(commit and inserted),
        "calls_order_gateway": False,
        "places_orders": False,
    }
    return {**result, "receipt_sha256": content_sha256(result)}


__all__ = ["SCHEMA", "ingest_canonical_observation_evidence"]
