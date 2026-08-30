from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.station_source_authority import (
    migrate_global_awc_source_authorities,
    resolve_global_awc_source_authority,
)
from bayes_basket_target.fleet import expected_station_count


def _stations(conn: sqlite3.Connection) -> list[str]:
    stations = [f"T{index:03d}" for index in range(expected_station_count())]
    for index, station in enumerate(stations):
        identity = {
            "station_id": station,
            "icao_code": station,
            "city_label": station,
            "timezone_name": "Etc/UTC",
            "source_lane": "AMSC" if index == 0 else "AWC",
            "settlement_definition": "official_daily_tmax_c",
            "registry_version": "legacy-v1",
            "source_role": "CANONICAL_AMSC_AWC_INGRESS",
        }
        conn.execute(
            "INSERT INTO calibration_station_registry("
            "station_id,icao_code,city_label,timezone_name,source_lane,"
            "settlement_definition,registry_version,source_role,content_sha256"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            tuple(identity.values()) + (content_sha256(identity),),
        )
    return stations


def test_global_awc_authority_is_append_only_and_supersedes_legacy_lane() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        stations = _stations(conn)
        report = migrate_global_awc_source_authorities(
            conn,
            station_ids=stations,
            authority_version="global_awc_ssot_v1",
            activated_at_utc="2026-08-01T00:00:00Z",
            lane_map_content_sha256="a" * 64,
            commit=True,
        )

        assert report["wrote_count"] == expected_station_count()
        assert conn.execute(
            "SELECT source_lane FROM calibration_station_registry WHERE station_id='T000'"
        ).fetchone()[0] == "AMSC"
        authority = resolve_global_awc_source_authority(conn, station_id="T000")
        assert authority["source_lane"] == "AWC"
        assert authority["lineage"]["legacy_registry_source_lane"] == "AMSC"
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                "UPDATE calibration_station_source_authority SET source_lane='AMSC' "
                "WHERE station_id='T000'"
            )

        replay = migrate_global_awc_source_authorities(
            conn,
            station_ids=stations,
            authority_version="global_awc_ssot_v1",
            activated_at_utc="2026-08-01T00:00:00Z",
            lane_map_content_sha256="a" * 64,
            commit=True,
        )
        assert replay["idempotent_count"] == expected_station_count()

