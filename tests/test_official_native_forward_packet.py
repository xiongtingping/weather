from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.official_native_forward_packet import (
    ECCC_GEPS_MULTISTATION_SCHEMA,
    ECMWF_MULTISTATION_SCHEMA,
    MIXED_NATIVE_CYCLES,
    NOAA_GEFS_MULTISTATION_SCHEMA,
    build_official_native_forward_packet,
)


def _archive(
    *,
    schema: str,
    system_id: str,
    family_id: str,
    intervals: dict[str, list[float | None]],
    run_cycle: str = "18Z",
    run_init_utc: str = "2026-06-29T18:00:00Z",
    archive_query_at_utc: str = "2026-07-31T00:00:00Z",
) -> dict[str, object]:
    capture_identity = {
        "station_id": "SAEZ",
        "event_date": "2026-07-01",
        "settlement_timezone": "America/Argentina/Buenos_Aires",
        "run_cycle": run_cycle,
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "member_count": 1,
        "members": [{
            "member_id": "member_01",
            "member_role": "PERTURBED",
            "raw_tmax_c": 17.25,
        }],
        "member_set_sha256": "1" * 64,
        "bucket_intervals": intervals,
        "bucket_contract_sha256": content_sha256(intervals),
    }
    capture = {
        **capture_identity,
        "content_sha256": content_sha256(capture_identity),
    }
    archive_identity = {
        "schema": schema,
        "event_date": "2026-07-01",
        "archive_lead_bucket": "d2",
        "run_cycle": run_cycle,
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "member_count": 1,
        "run_init_utc": run_init_utc,
        "archive_query_at_utc": archive_query_at_utc,
        "captures": [capture],
        "training_only": True,
        "holdout_eligible": False,
    }
    return {
        **archive_identity,
        "content_sha256": content_sha256(archive_identity),
    }


def test_authoritative_target_contract_rebins_mismatched_archives() -> None:
    gefs = _archive(
        schema=NOAA_GEFS_MULTISTATION_SCHEMA,
        system_id="NOAA_GEFS_05",
        family_id="NOAA_GEFS",
        intervals={
            "16°C or below": [None, 16.5],
            "17°C or higher": [16.5, None],
        },
    )
    ecmwf = _archive(
        schema=ECMWF_MULTISTATION_SCHEMA,
        system_id="ECMWF_IFS_ENS_OPEN_PF50",
        family_id="ECMWF",
        intervals={
            "14°C or below": [None, 14.5],
            "15°C or higher": [14.5, None],
        },
    )
    with pytest.raises(
        ContractError,
        match="official_native_forward_station_contract_mismatch",
    ):
        build_official_native_forward_packet(
            archives=[gefs, ecmwf],
            station_id="SAEZ",
        )

    target_intervals = {
        "15°C or below": [None, 15.5],
        "16°C": [15.5, 16.5],
        "17°C or higher": [16.5, None],
    }
    packet = build_official_native_forward_packet(
        archives=[gefs, ecmwf],
        station_id="SAEZ",
        target_bucket_contract={
            "schema": (
                "official_native_forward_target_bucket_contract_v1"
            ),
            "station_id": "SAEZ",
            "event_date": "2026-07-01",
            "settlement_timezone": (
                "America/Argentina/Buenos_Aires"
            ),
            "bucket_intervals": target_intervals,
            "bucket_contract_sha256": content_sha256(
                target_intervals
            ),
            "authority_content_sha256": "a" * 64,
        },
    )

    assert packet["bucket_intervals"] == target_intervals
    assert packet["bucket_contract_sha256"] == content_sha256(
        target_intervals
    )
    assert packet["bucket_contract_authority"] == {
        "source_class": "AUTHORITATIVE_MARKET_REGISTRY_STATION_DAY",
        "authority_content_sha256": "a" * 64,
    }
    assert {
        row["source_bucket_contract_sha256"]
        for row in packet["source_archives"]
    } == {
        content_sha256(gefs["captures"][0]["bucket_intervals"]),
        content_sha256(ecmwf["captures"][0]["bucket_intervals"]),
    }


def test_mixed_native_cycles_keep_per_system_lineage() -> None:
    intervals = {
        "15°C or below": [None, 15.5],
        "16°C or higher": [15.5, None],
    }
    gefs = _archive(
        schema=NOAA_GEFS_MULTISTATION_SCHEMA,
        system_id="NOAA_GEFS_05",
        family_id="NOAA_GEFS",
        intervals=intervals,
        run_cycle="18Z",
        run_init_utc="2026-06-29T18:00:00Z",
        archive_query_at_utc="2026-07-31T00:00:00Z",
    )
    ecmwf = _archive(
        schema=ECMWF_MULTISTATION_SCHEMA,
        system_id="ECMWF_IFS_ENS_OPEN_PF50",
        family_id="ECMWF",
        intervals=intervals,
        run_cycle="18Z",
        run_init_utc="2026-06-29T18:00:00Z",
        archive_query_at_utc="2026-07-31T00:01:00Z",
    )
    eccc = _archive(
        schema=ECCC_GEPS_MULTISTATION_SCHEMA,
        system_id="ECCC_GEM_GLOBAL_ENS",
        family_id="ECCC_GEM",
        intervals=intervals,
        run_cycle="00Z",
        run_init_utc="2026-06-30T00:00:00Z",
        archive_query_at_utc="2026-07-31T00:02:00Z",
    )

    packet = build_official_native_forward_packet(
        archives=[gefs, ecmwf, eccc],
        station_id="SAEZ",
    )

    assert packet["run_cycle"] == MIXED_NATIVE_CYCLES
    assert packet["run_cycle_scope"] == "PER_SYSTEM_NATIVE_CYCLE"
    assert packet["run_cycle_by_system"] == {
        "ECMWF_IFS_ENS_OPEN_PF50": "18Z",
        "ECCC_GEM_GLOBAL_ENS": "00Z",
        "NOAA_GEFS_05": "18Z",
    }
    assert packet["run_init_utc_by_system"] == {
        "ECMWF_IFS_ENS_OPEN_PF50": "2026-06-29T18:00:00Z",
        "ECCC_GEM_GLOBAL_ENS": "2026-06-30T00:00:00Z",
        "NOAA_GEFS_05": "2026-06-29T18:00:00Z",
    }
    assert packet["source_class"] == (
        "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
    )
    assert packet["availability_evidence"]["available_at_utc"] == (
        "2026-07-31T00:02:00Z"
    )
    assert packet["availability_evidence"]["by_system"] == {
        "ECMWF_IFS_ENS_OPEN_PF50": {
            "run_cycle": "18Z",
            "run_init_utc": "2026-06-29T18:00:00Z",
            "archive_query_at_utc": "2026-07-31T00:01:00Z",
            "archive_content_sha256": ecmwf["content_sha256"],
            "station_capture_content_sha256": (
                ecmwf["captures"][0]["content_sha256"]
            ),
        },
        "ECCC_GEM_GLOBAL_ENS": {
            "run_cycle": "00Z",
            "run_init_utc": "2026-06-30T00:00:00Z",
            "archive_query_at_utc": "2026-07-31T00:02:00Z",
            "archive_content_sha256": eccc["content_sha256"],
            "station_capture_content_sha256": (
                eccc["captures"][0]["content_sha256"]
            ),
        },
        "NOAA_GEFS_05": {
            "run_cycle": "18Z",
            "run_init_utc": "2026-06-29T18:00:00Z",
            "archive_query_at_utc": "2026-07-31T00:00:00Z",
            "archive_content_sha256": gefs["content_sha256"],
            "station_capture_content_sha256": (
                gefs["captures"][0]["content_sha256"]
            ),
        },
    }
    assert {
        (row["ensemble_system_id"], row["run_cycle"])
        for row in packet["source_archives"]
    } == {
        ("ECMWF_IFS_ENS_OPEN_PF50", "18Z"),
        ("ECCC_GEM_GLOBAL_ENS", "00Z"),
        ("NOAA_GEFS_05", "18Z"),
    }
    assert packet["snapshot_content_sha256"] == content_sha256({
        key: value
        for key, value in packet.items()
        if key != "snapshot_content_sha256"
    })
