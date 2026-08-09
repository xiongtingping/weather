from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any

from bayes_basket_target.awc_observation_contract import (
    AUTHORITY_SCOPE,
    CONTRACT_SCHEMA,
    RAW_OBJECT_KIND,
)
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import content_sha256

from tests.pr12_authority_projection_test_support import authority_projection


def _stamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _seal(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "content_sha256": content_sha256(value)}


def awc_contract(
    *,
    station_id: str,
    observation_id: str,
    event_date: str,
    settlement_timezone: str,
    observed_at_utc: str,
    raw_object_sha256: str,
    reported_temperature_c: float = 38.0,
) -> tuple[dict[str, Any], str]:
    stamp = _stamp(observed_at_utc)
    # Every fact names the projection that authorises its scope, and the
    # contract carries that projection so the link can be checked without a
    # second lookup.  Both sides have to come from the same object or the
    # parent-mismatch guard rejects the contract.
    projection = authority_projection(primary_station=station_id)
    revision = _seal(
        {
            "station_id": station_id,
            "observation_id": observation_id,
            "revision_no": 1,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "source_lane": "AWC",
            "authority_scope": AUTHORITY_SCOPE,
            "authority_scope_projection_content_sha256": (
                projection["content_sha256"]
            ),
            "report_type": "METAR",
            "sequence": 1,
            "observed_at_utc": stamp,
            "received_at_utc": stamp,
            "first_seen_at_utc": stamp,
            "parsed_at_utc": stamp,
            "fetch_started_at_utc": stamp,
            "fetch_completed_at_utc": stamp,
            "origin_committed_at_utc": stamp,
            "source_observation_committed_at_utc": stamp,
            "canonical_committed_at_utc": stamp,
            "reported_temperature_c": float(reported_temperature_c),
            "raw_object_kind": RAW_OBJECT_KIND,
            "raw_object_sha256": raw_object_sha256,
            "awc_source_receipt_sha256": "b" * 64,
            "origin_source_id": "global_noaa_awc_relay",
            "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
            "origin_url": "https://aviationweather.gov/data/api/",
            "origin_transport": "official_awc_raw_metar_batch_direct",
            "origin_registry_sha256": "c" * 64,
            "origin_record_sha256": "d" * 64,
            "correction_kind": "NONE",
            "correction_state": "NO_CORRECTION",
            "supersedes_observation_fact_sha256": None,
            "qc_state": "ACCEPTED",
            "review_receipt_sha256": None,
            "halt_state": "CLEAR",
        }
    )
    contract = _seal(
        {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": stamp,
            "builder_id": "tests.pr12.atomic.v1",
            "authority_scope_projection": projection,
            "revision_facts": [revision],
            "day_bounds_facts": [],
        }
    )
    return contract, str(revision["content_sha256"])


def install_canonical_v12(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
    ).fetchone()
    if exists is None:
        conn.execute(TARGET_TABLE_DDL)
        for ddl in TARGET_TRIGGER_DDL:
            conn.execute(ddl)
