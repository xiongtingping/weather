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
    archive_lead_bucket: str = "d2",
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
        "archive_lead_bucket": archive_lead_bucket,
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


def _lead_archives(lead_bucket: str, run_init_utc: str, run_cycle: str):
    intervals = {
        "16°C or below": [None, 16.5],
        "17°C or higher": [16.5, None],
    }
    return [
        _archive(
            schema=NOAA_GEFS_MULTISTATION_SCHEMA,
            system_id="NOAA_GEFS_05",
            family_id="NOAA_GEFS",
            intervals=intervals,
            run_cycle=run_cycle,
            run_init_utc=run_init_utc,
            archive_lead_bucket=lead_bucket,
        ),
        _archive(
            schema=ECMWF_MULTISTATION_SCHEMA,
            system_id="ECMWF_IFS_ENS_OPEN_PF50",
            family_id="ECMWF",
            intervals=intervals,
            run_cycle=run_cycle,
            run_init_utc=run_init_utc,
            archive_lead_bucket=lead_bucket,
        ),
    ]


@pytest.mark.parametrize(
    "stated_lead,run_init_utc,run_cycle,derived_lead",
    [
        # The packet's lead is derived from the run against the station's own
        # local day, not read from the stamped calendar label: SAEZ's 07-01
        # begins at 03Z, so the 06-29T18Z run stands 33h ahead and the
        # 06-30T00Z run 27h -- the two calendar labels d2 and d1 name the same
        # training group, which is the overlap the hour vocabulary exists to
        # dissolve.
        ("d2", "2026-06-29T18:00:00Z", "18Z", "h24_48"),
        ("d1", "2026-06-30T00:00:00Z", "00Z", "h24_48"),
        ("d0", "2026-07-01T00:00:00Z", "00Z", "h00_12"),
    ],
)
def test_every_supported_lead_builds_a_packet(
    stated_lead: str, run_init_utc: str, run_cycle: str, derived_lead: str
) -> None:
    # P0 is recomputed as the models update, so the same station-day arrives at
    # d2, then d1, then d0.  This builder used to accept d2 alone: the shorter
    # leads were captured, downloaded and correctly labelled, and then rejected
    # here -- so nothing but d2 had ever reached a packet.
    packet = build_official_native_forward_packet(
        archives=_lead_archives(stated_lead, run_init_utc, run_cycle),
        station_id="SAEZ",
    )

    assert packet["lead_bucket"] == derived_lead
    assert packet["run_cycle"] == run_cycle


def test_archives_from_different_runs_blend_with_per_system_leads() -> None:
    # Under run-driven capture the systems legitimately stand at different
    # cycles -- ECMWF follows a six-hourly ladder while ECCC publishes twice a
    # day -- so one packet carries one run per system and each system keeps its
    # own lead.  The packet-level label takes the freshest.  This replaces the
    # calendar-era rule that all archives state one shared lead, which rejected
    # every mixed-cycle capture after it had done all the downloading.
    mixed = [
        _lead_archives("d2", "2026-06-29T18:00:00Z", "18Z")[0],
        _lead_archives("d1", "2026-06-30T12:00:00Z", "12Z")[1],
    ]

    packet = build_official_native_forward_packet(
        archives=mixed, station_id="SAEZ"
    )

    # 06-29T18Z stands 33h ahead of SAEZ's 07-01 (h24_48); 06-30T12Z stands
    # 15h ahead (h12_24).  Each system keeps its own, the packet claims the
    # freshest.
    assert packet["lead_bucket"] == "h12_24"
    leads = packet["lead_bucket_by_system"]
    assert leads["NOAA_GEFS_05"] == "h24_48"
    assert leads["ECMWF_IFS_ENS_OPEN_PF50"] == "h12_24"


def test_an_unsupported_lead_is_still_rejected() -> None:
    with pytest.raises(
        ContractError, match="official_native_forward_scope_mismatch"
    ):
        build_official_native_forward_packet(
            archives=_lead_archives("d7", "2026-06-24T18:00:00Z", "18Z"),
            station_id="SAEZ",
        )


def _with_capture_query(archive: dict, capture_query: str, file_query: str) -> dict:
    capture = {
        key: value
        for key, value in archive["captures"][0].items()
        if key != "content_sha256"
    }
    capture["archive_query_at_utc"] = capture_query
    capture = {**capture, "content_sha256": content_sha256(capture)}
    identity = {
        key: value for key, value in archive.items() if key != "content_sha256"
    }
    identity["captures"] = [capture]
    identity["archive_query_at_utc"] = file_query
    return {**identity, "content_sha256": content_sha256(identity)}


def test_a_station_arrives_when_its_own_capture_was_read() -> None:
    # A follow file read again for later-listed stations carries the newest
    # read at file level; a station from the first read keeps its own instant.
    intervals = {"15°C or below": [None, 15.5], "16°C or higher": [15.5, None]}
    gefs = _archive(
        schema=NOAA_GEFS_MULTISTATION_SCHEMA, system_id="NOAA_GEFS_05",
        family_id="NOAA_GEFS", intervals=intervals,
    )
    eccc = _with_capture_query(
        _archive(
            schema=ECCC_GEPS_MULTISTATION_SCHEMA,
            system_id="ECCC_GEM_GLOBAL_ENS", family_id="ECCC_GEM",
            intervals=intervals, run_cycle="00Z",
            run_init_utc="2026-06-30T00:00:00Z",
        ),
        capture_query="2026-07-31T00:02:00Z",
        file_query="2026-07-31T05:00:00Z",
    )
    packet = build_official_native_forward_packet(
        archives=[gefs, eccc], station_id="SAEZ"
    )
    assert packet["archive_query_at_utc_by_system"]["ECCC_GEM_GLOBAL_ENS"] == (
        "2026-07-31T00:02:00Z"
    )
    assert packet["availability_evidence"]["available_at_utc"] == (
        "2026-07-31T00:02:00Z"
    )
