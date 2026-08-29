from __future__ import annotations

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.pr12_authority_scope_projection import (
    AUTHORITY_VERSION, BUILDER_ID, REQUIRED_DOCTRINE_SHA256, SCHEMA,
    SETTLEMENT_TIMEZONE_SOURCE,
)


def authority_projection(
    *, primary_station: str = "KATL", settlement_timezone: str = "America/New_York"
) -> dict:
    """Stations settle on the zone their own listing declares, so the zone is
    an argument here rather than a constant -- a fixture that can only build
    an Eastern fleet cannot exercise the 42 stations that are not."""

    # Sized from the coordinate registry, not from a literal: the guard this
    # fixture feeds derives the expected count the same way, so a fleet
    # change that moves one moves the other.  Onboarding LLBG/LTFM/UUWW on
    # 2026-08-30 broke every consumer of this helper for exactly that
    # reason -- 45 rows offered to a guard that had started expecting 48.
    stations = sorted(
        [primary_station]
        + [f"T{index:03d}" for index in range(expected_station_count() - 1)]
    )
    rows = []
    for station in stations:
        identity = {
            "station_id": station,
            "source_lane": "AWC",
            "source_authority_version": "fixture-v1",
            "source_authority_content_sha256": "c" * 64,
            "legacy_station_registry_content_sha256": "b" * 64,
            "settlement_definition": "official_daily_tmax_c",
            "settlement_timezone": settlement_timezone,
            "settlement_timezone_authority": AUTHORITY_VERSION,
        }
        rows.append({**identity, "content_sha256": content_sha256(identity)})
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": "2026-08-02T06:00:00.000Z",
        "state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone_source": SETTLEMENT_TIMEZONE_SOURCE,
        "settlement_timezone_authority": AUTHORITY_VERSION,
        "doctrine_receipts": [
            {"document_name": name, "raw_sha256": sha}
            for name, sha in sorted(REQUIRED_DOCTRINE_SHA256.items())
        ],
        "doctrine_receipt_set_sha256": content_sha256([
            {"document_name": name, "raw_sha256": sha}
            for name, sha in sorted(REQUIRED_DOCTRINE_SHA256.items())
        ]),
        "station_count": len(rows),
        "station_authority_rowset_sha256": content_sha256(rows),
        "source_authority_vector_sha256": content_sha256([
            [row["station_id"], row["source_authority_content_sha256"]]
            for row in rows
        ]),
        "station_registry_vector_sha256": content_sha256([
            [row["station_id"], row["legacy_station_registry_content_sha256"]]
            for row in rows
        ]),
        "rows": rows,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}
