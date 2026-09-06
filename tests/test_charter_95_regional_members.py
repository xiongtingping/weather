"""Charter 95: the regional deterministic models may contest P0 seats.

User ruling 2026-09-06 ("接入").  JMA MSM, NCEP HRRR, NCEP NBM and DWD ICON-EU
are numerical models; the 2026-08-15 exclusion of point forecasts from the P0
combination was written for the forecaster's TAF TX and the METAR physical
path, which stay out.  Their forward runs reach the packet through the same
point-archive shape the TAF uses, and their registry rows are written by the
capture lanes so the compiler can resolve them.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_contract import (
    validate_native_ensemble_transport_gate,
)
from bayes_basket_target.native_ensemble_training_facts import (
    build_regional_point_forward_archives_for_station,
    register_regional_point_systems,
)
from bayes_basket_target.official_native_forward_packet import (
    ECMWF_MULTISTATION_SCHEMA,
    NOAA_GEFS_MULTISTATION_SCHEMA,
    REGIONAL_DETERMINISTIC_SYSTEM_IDS,
    SUPPORTED_ARCHIVE_SCHEMAS,
    build_official_native_forward_packet,
    is_fusion_excluded_point_system,
)

ROOT = Path(__file__).resolve().parents[1]


def test_every_point_system_stays_out_of_p0_fusion_while_the_regional_stage_is_p1() -> None:
    from bayes_basket_target import official_native_forward_packet as M

    assert M.REGIONAL_MODELS_STAGE == "P1"
    assert is_fusion_excluded_point_system("OFFICIAL_AIRPORT_TAF_TX") is True
    assert is_fusion_excluded_point_system("METAR_PHYSICAL_PATH_TMAX") is True
    for system_id in REGIONAL_DETERMINISTIC_SYSTEM_IDS:
        assert is_fusion_excluded_point_system(system_id) is True, (
            "re-ruled the same day: regional models are P1 evidence, not P0 members"
        )
    assert is_fusion_excluded_point_system("ECMWF_IFS_ENS_OPEN_PF50") is False


def test_the_p0_door_reopens_only_if_the_stage_is_moved_back(monkeypatch) -> None:
    from bayes_basket_target import official_native_forward_packet as M

    monkeypatch.setattr(M, "REGIONAL_MODELS_STAGE", "P0")
    assert M.is_fusion_excluded_point_system("NCEP_NBM_POINT") is False
    assert M.is_fusion_excluded_point_system("OFFICIAL_AIRPORT_TAF_TX") is True
    assert REGIONAL_DETERMINISTIC_SYSTEM_IDS == {
        "JMA_MSM_POINT", "NCEP_HRRR_POINT", "NCEP_NBM_POINT", "DWD_ICON_EU_POINT",
    }


def test_every_fusion_site_reads_the_one_predicate() -> None:
    oos = (ROOT / "bayes_basket_target" / "native_ensemble_oos.py").read_text(encoding="utf-8")
    p0 = (ROOT / "bayes_basket_target" / "native_ensemble_p0.py").read_text(encoding="utf-8")
    assert oos.count("is_fusion_excluded_point_system(system_id)") == 2
    assert "if system_id in POINT_FORECAST_SYSTEMS:\n            continue" not in oos
    assert "if not is_fusion_excluded_point_system(system_id)" in p0
    assert "if is_fusion_excluded_point_system(system_id)" in p0
    assert "for demoted_id in sorted(POINT_FORECAST_SYSTEMS):" not in p0


def test_the_transport_gate_resolves_one_provider_with_two_products() -> None:
    systems = {
        "NCEP_HRRR_POINT": {"provider_id": "NCEP_NOAA", "source_class": "NATIVE_ENSEMBLE_PROVIDER", "aggregator_or_republisher": 0},
        "NCEP_NBM_POINT": {"provider_id": "NCEP_NOAA", "source_class": "NATIVE_ENSEMBLE_PROVIDER", "aggregator_or_republisher": 0},
        "DWD_ICON_EU_POINT": {"provider_id": "DWD", "source_class": "NATIVE_ENSEMBLE_PROVIDER", "aggregator_or_republisher": 0},
        "JMA_MSM_POINT": {"provider_id": "JMA_RJTD", "source_class": "NATIVE_ENSEMBLE_PROVIDER", "aggregator_or_republisher": 0},
    }
    resolved = validate_native_ensemble_transport_gate(
        selected_system_ids=list(systems),
        systems_by_id=systems,
        transport_provider_by_system={
            "NCEP_HRRR_POINT": "NOAA_HRRR_PDS_S3", "NCEP_NBM_POINT": "NOAA_NBM_PDS_S3",
            "DWD_ICON_EU_POINT": "DWD_OPENDATA_ICON_EU", "JMA_MSM_POINT": "RISH_KYOTO_JMA_ARCHIVE",
        },
    )
    assert resolved["NCEP_NBM_POINT"] == "NOAA_NBM_PDS_S3"
    assert resolved["DWD_ICON_EU_POINT"] == "DWD_OPENDATA_ICON_EU"
    with pytest.raises(ContractError, match="native_ensemble_transport_not_official"):
        validate_native_ensemble_transport_gate(
            selected_system_ids=["NCEP_NBM_POINT"],
            systems_by_id=systems,
            transport_provider_by_system={"NCEP_NBM_POINT": "NOAA_HRRR_PDS_S3"},
        )


REGISTRY_DDL = """
CREATE TABLE calibration_native_ensemble_system (
  ensemble_system_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL UNIQUE,
  independence_family_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  source_class TEXT NOT NULL CHECK(source_class='NATIVE_ENSEMBLE_PROVIDER'),
  aggregator_or_republisher INTEGER NOT NULL DEFAULT 0 CHECK(aggregator_or_republisher=0),
  expected_member_count INTEGER NOT NULL CHECK(expected_member_count>=1),
  member_identity_contract_version TEXT NOT NULL,
  publication_contract_version TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64)
);
"""
MEMBER_RUN_DDL = """
CREATE TABLE calibration_native_ensemble_historical_member_run (
  historical_member_run_id TEXT PRIMARY KEY, ensemble_system_id TEXT NOT NULL, station_id TEXT NOT NULL,
  target_local_date TEXT NOT NULL, archive_lead_bucket TEXT NOT NULL, run_cycle TEXT NOT NULL,
  member_id TEXT NOT NULL, member_role TEXT NOT NULL, raw_tmax_c REAL NOT NULL, settlement_definition TEXT NOT NULL,
  extraction_version TEXT NOT NULL, provider_id TEXT NOT NULL, transport_provider_id TEXT NOT NULL,
  native_run_init_utc TEXT, offline_oos_eligible INTEGER NOT NULL DEFAULT 0, archive_query_at_utc TEXT NOT NULL,
  source_url TEXT NOT NULL, raw_object_ref TEXT NOT NULL, source_object_sha256 TEXT NOT NULL,
  qc_status TEXT NOT NULL, authority_scope TEXT NOT NULL, holdout_eligible INTEGER NOT NULL, content_sha256 TEXT NOT NULL UNIQUE
);
"""


def test_the_registry_rows_are_written_once() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(REGISTRY_DDL)
    assert register_regional_point_systems(conn) == 4
    assert register_regional_point_systems(conn) == 0
    rows = conn.execute(
        "SELECT ensemble_system_id, independence_family_id, provider_id, expected_member_count"
        " FROM calibration_native_ensemble_system ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in rows] == ["DWD_ICON_EU_POINT", "JMA_MSM_POINT", "NCEP_HRRR_POINT", "NCEP_NBM_POINT"]
    assert all(r[3] == 1 for r in rows)
    assert dict((r[0], r[1]) for r in rows)["DWD_ICON_EU_POINT"] == "DWD_ICON_EPS", (
        "ICON-EU shares the ICON physics family, it does not open a second one"
    )
    assert dict((r[0], r[2]) for r in rows)["NCEP_HRRR_POINT"] == "NCEP_NOAA"


def _member_run(conn, *, system_id, cycle, lead, init, query, value, station="KATL", day="2026-09-07"):
    identity = {"s": system_id, "c": cycle, "l": lead, "i": init, "v": value}
    conn.execute(
        "INSERT INTO calibration_native_ensemble_historical_member_run VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            content_sha256(identity)[:32], system_id, station, day, lead, cycle,
            "HRRR_CONTROL" if "HRRR" in system_id else "NBM_BLEND", "CONTROL", value,
            "official_daily_tmax_c", "x_v1", "NCEP_NOAA",
            "NOAA_HRRR_PDS_S3" if "HRRR" in system_id else "NOAA_NBM_PDS_S3",
            init, 1, query, "s3://x", "ref", "a" * 64, "ACCEPTED", "CALIBRATION_TRAIN_ONLY", 0,
            content_sha256(identity),
        ),
    )


INTERVALS = {"29°C or below": [None, 29.5], "30°C": [29.5, 30.5], "31°C": [30.5, 31.5], "32°C or higher": [31.5, None]}


def test_the_freshest_admissible_run_per_system_becomes_an_archive() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(MEMBER_RUN_DDL)
    _member_run(conn, system_id="NCEP_HRRR_POINT", cycle="00Z", lead="h24_48", init="2026-09-06T00:00:00Z", query="2026-09-06T03:00:00Z", value=30.4)
    _member_run(conn, system_id="NCEP_HRRR_POINT", cycle="12Z", lead="h12_24", init="2026-09-06T12:00:00Z", query="2026-09-06T14:10:00Z", value=31.1)
    _member_run(conn, system_id="NCEP_NBM_POINT", cycle="12Z", lead="h12_24", init="2026-09-06T12:00:00Z", query="2026-09-06T14:12:00Z", value=30.8)
    # A run whose archive query lies after the packet clock is not yet knowable.
    _member_run(conn, system_id="NCEP_NBM_POINT", cycle="18Z", lead="h12_24", init="2026-09-06T18:00:00Z", query="2026-09-06T20:00:00Z", value=29.9)
    archives = build_regional_point_forward_archives_for_station(
        conn, station_id="KATL", event_date="2026-09-07", settlement_timezone="America/New_York",
        as_of_utc="2026-09-06T15:00:00Z", bucket_intervals=INTERVALS,
    )
    by_system = {a["ensemble_system_id"]: a for a in archives}
    assert set(by_system) == {"NCEP_HRRR_POINT", "NCEP_NBM_POINT"}
    hrrr = by_system["NCEP_HRRR_POINT"]
    assert hrrr["run_cycle"] == "12Z" and hrrr["archive_lead_bucket"] == "h12_24"
    assert hrrr["captures"][0]["members"] == [{"member_id": "HRRR_CONTROL", "member_role": "CONTROL", "raw_tmax_c": 31.1}]
    assert by_system["NCEP_NBM_POINT"]["run_cycle"] == "12Z", "the 18Z run is not knowable at 15:00Z"
    assert SUPPORTED_ARCHIVE_SCHEMAS[hrrr["schema"]] == "NOAA_HRRR_PDS_S3"
    assert SUPPORTED_ARCHIVE_SCHEMAS[by_system["NCEP_NBM_POINT"]["schema"]] == "NOAA_NBM_PDS_S3"
    # Nothing captured yet -> nothing offered.
    assert build_regional_point_forward_archives_for_station(
        conn, station_id="KMIA", event_date="2026-09-07", settlement_timezone="America/New_York",
        as_of_utc="2026-09-06T15:00:00Z", bucket_intervals=INTERVALS,
    ) == []
    # The archives are accepted beside the model archives and become packet systems.
    def _base(schema, system_id, family):
        capture_identity = {
            "station_id": "KATL", "event_date": "2026-09-07", "settlement_timezone": "America/New_York",
            "run_cycle": "12Z", "ensemble_system_id": system_id, "independence_family_id": family,
            "member_count": 1, "members": [{"member_id": "m01", "member_role": "PERTURBED", "raw_tmax_c": 30.6}],
            "member_set_sha256": "1" * 64, "bucket_intervals": INTERVALS, "bucket_contract_sha256": content_sha256(INTERVALS),
        }
        capture = {**capture_identity, "content_sha256": content_sha256(capture_identity)}
        archive_identity = {
            "schema": schema, "event_date": "2026-09-07", "archive_lead_bucket": "h12_24", "run_cycle": "12Z",
            "ensemble_system_id": system_id, "independence_family_id": family, "member_count": 1,
            "run_init_utc": "2026-09-06T12:00:00Z", "archive_query_at_utc": "2026-09-06T14:00:00Z",
            "captures": [capture], "training_only": True, "holdout_eligible": False,
        }
        return {**archive_identity, "content_sha256": content_sha256(archive_identity)}
    packet = build_official_native_forward_packet(
        archives=[_base(NOAA_GEFS_MULTISTATION_SCHEMA, "NOAA_GEFS_05", "NOAA_GEFS"), _base(ECMWF_MULTISTATION_SCHEMA, "ECMWF_IFS_ENS_OPEN_PF50", "ECMWF"), *archives],
        station_id="KATL",
    )
    system_ids = {row["ensemble_system_id"] for row in packet["systems"]}
    assert {"NCEP_HRRR_POINT", "NCEP_NBM_POINT"} <= system_ids
    assert packet["run_cycle_by_system"]["NCEP_HRRR_POINT"] == "12Z"
    assert {row["ensemble_system_id"]: row["transport_provider_id"] for row in packet["source_archives"]}["NCEP_NBM_POINT"] == "NOAA_NBM_PDS_S3"
