from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bayes_basket_target.awc_observation_builder import (
    EVIDENCE_SCHEMA,
    _legacy_awc_origin_authority_binding,
    build_awc_observation_contract,
)
from bayes_basket_target.awc_observation_contract import (
    AWC_ORIGIN_URL,
    RAW_OBJECT_KIND,
    CanonicalAWCObservationContract,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from tests.pr12_authority_projection_test_support import authority_projection


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _slots(day: str, zone_name: str) -> list[datetime]:
    zone = ZoneInfo(zone_name)
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), zone)
    end = datetime.combine(
        date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), zone
    )
    cursor = start.astimezone(timezone.utc)
    result: list[datetime] = []
    while cursor < end.astimezone(timezone.utc):
        result.append(cursor)
        cursor += timedelta(hours=1)
    return result


def _row(
    slot: datetime,
    index: int,
    *,
    day: str,
    zone: str,
    report_type: str = "METAR",
    temperature: float = 25.0,
) -> dict[str, object]:
    first = slot + timedelta(minutes=1)
    return {
        "observation_id": f"obs-{index}",
        "station_id": "KJFK",
        "event_date": day,
        "settlement_timezone": zone,
        "source_lane": "AWC",
        "report_type": report_type,
        "observed_at_utc": _stamp(slot),
        "received_at_utc": _stamp(first),
        "first_seen_at_utc": _stamp(first),
        "parsed_at_utc": _stamp(first + timedelta(milliseconds=100)),
        "fetch_started_at_utc": _stamp(slot + timedelta(seconds=30)),
        "fetch_completed_at_utc": _stamp(first),
        "canonical_committed_at_utc": _stamp(first + timedelta(seconds=1)),
        "reported_temperature_c": temperature,
        "raw_text": f"METAR KJFK {index:02d}0000Z 00000KT CAVOK {int(temperature):02d}/10 A3000",
        "raw_object_kind": RAW_OBJECT_KIND,
        "raw_object_sha256": f"{index + 1:064x}",
        "source_poll_payload_sha256": f"{index + 101:064x}",
        "awc_source_receipt_sha256": f"{index + 201:064x}",
        "origin_source_id": "global_noaa_awc_relay",
        "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
        "origin_url": AWC_ORIGIN_URL,
        "origin_transport": "official_awc_raw_metar_batch_direct",
        "origin_registry_sha256": "a" * 64,
        "origin_record_sha256": f"{index + 301:064x}",
        "origin_committed_at_utc": _stamp(first + timedelta(milliseconds=500)),
        "origin_lineage_ok": True,
        "policy_excluded": False,
        "correction_kind": "NONE",
        "correction_suspected": 0,
    }


def _evidence(
    *,
    day: str = "2026-08-01",
    zone: str = "America/New_York",
) -> dict[str, object]:
    slots = _slots(day, zone)
    rows = [
        _row(slot, index, day=day, zone=zone, temperature=25.0 + (index == 12))
        for index, slot in enumerate(slots)
    ]
    end = slots[-1] + timedelta(hours=2)
    registry_sha = "b" * 64
    projection = authority_projection(
        primary_station="KJFK", settlement_timezone=zone
    )
    identity = {
        "schema": EVIDENCE_SCHEMA,
        "as_of_utc": _stamp(end),
        "source_database": "/read-only/archive.db",
        "station_registry": {
            "KJFK": {
                "station_id": "KJFK",
                "settlement_definition": "official_daily_tmax_c",
                "content_sha256": registry_sha,
            }
        },
        "authority_scope_projection": projection,
        "station_timezones": {"KJFK": zone},
        "rows": rows,
        "station_day_halts": [],
        "station_day_reviews": [],
        "extraction_gaps": [],
        "source_read_only": True,
        "source_query_only": True,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def _reseal(evidence: dict[str, object]) -> dict[str, object]:
    identity = {k: v for k, v in evidence.items() if k != "receipt_sha256"}
    return {**identity, "receipt_sha256": content_sha256(identity)}


def test_clean_day_builds_revision_vector_and_bounds() -> None:
    contract, receipt = build_awc_observation_contract(_evidence())
    parsed = CanonicalAWCObservationContract.from_mapping(contract)
    assert len(parsed.revision_facts) == 24
    assert [x.raw["sequence"] for x in parsed.revision_facts] == list(range(1, 25))
    assert len(parsed.day_bounds_facts) == 1
    assert receipt["state"] == "MATERIALIZED_UNPUBLISHED"
    assert receipt["terminal_gaps"] == []


def test_a_station_files_its_reports_under_its_own_local_day() -> None:
    """The day is the one the station's market listing settles on.

    Until 2026-08-26 this lane pinned every station to America/New_York, so a
    Tokyo station's reports were grouped into an Eastern day: RJTT 2026-07-18
    reads 32C over that window and 29C over its own, and 29C is what the
    market pays. Only 3 of the 45 stations settle on an Eastern day.
    """

    zone = "Asia/Tokyo"
    evidence = _evidence(zone=zone)
    contract, receipt = build_awc_observation_contract(evidence)
    parsed = CanonicalAWCObservationContract.from_mapping(contract)

    assert {x.raw["settlement_timezone"] for x in parsed.revision_facts} == {zone}
    assert len(parsed.day_bounds_facts) == 1
    bounds = parsed.day_bounds_facts[0].raw
    assert bounds["settlement_timezone"] == zone
    # The window is that local day, not an Eastern one: Tokyo runs UTC+9, so
    # its 2026-08-01 opens at 15:00Z the day before.
    assert bounds["day_start_utc"] == "2026-07-31T15:00:00.000Z"
    assert bounds["day_end_utc"] == "2026-08-01T15:00:00.000Z"
    assert receipt["terminal_gaps"] == []


def test_a_station_that_never_publishes_on_the_hour_still_closes_its_day() -> None:
    """An hour is covered by the report that falls in it, wherever it falls.

    Coverage compared report instants against the day's clock hours, so a
    station publishing at :51 -- which is every ASOS-fed United States
    station, and 18 of the 45 in all -- reported 24 missing and 24 extra
    slots every day and could never close one.  Eight stations held no
    revision fact of any kind because of it.
    """

    evidence = _evidence()
    rows = []
    for row in evidence["rows"]:
        moved = dict(row)
        for field in (
            "observed_at_utc", "received_at_utc", "first_seen_at_utc",
            "parsed_at_utc", "fetch_started_at_utc", "fetch_completed_at_utc",
            "canonical_committed_at_utc",
        ):
            stamp = datetime.fromisoformat(
                str(moved[field]).replace("Z", "+00:00")
            ) + timedelta(minutes=51)
            moved[field] = _stamp(stamp)
        rows.append(moved)
    evidence["rows"] = rows
    contract, receipt = build_awc_observation_contract(_reseal(evidence))

    parsed = CanonicalAWCObservationContract.from_mapping(contract)
    assert len(parsed.revision_facts) == 24
    assert [x.raw["sequence"] for x in parsed.revision_facts] == list(range(1, 25))
    assert parsed.revision_facts[0].raw["observed_at_utc"].endswith("51:00.000Z")
    assert len(parsed.day_bounds_facts) == 1
    assert receipt["terminal_gaps"] == []


def test_exact_legacy_awc_origin_binding_is_explicit_and_tamper_closed() -> None:
    projection = authority_projection(primary_station="KJFK")
    authority = next(
        row for row in projection["rows"] if row["station_id"] == "KJFK"
    )
    predecessor = {
        "observation_id": "obs-legacy",
        "source_lane": "awc",
        "station_id": "KJFK",
        "origin_source_id": "global_noaa_awc_relay",
        "origin_country_code": "ZZ",
        "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
        "origin_url": AWC_ORIGIN_URL,
        "origin_transport": "official_awc_raw_metar_batch_direct",
        "direct_country_official": 0,
        "fallback_used": 1,
        "fallback_reason": "historical_observation_pre_country_router",
        "preferred_origin_source_id": "",
        "preferred_origin_authority": "",
        "official_source_registry_sha256": "",
        "origin_json": {"origin_source_id": "global_noaa_awc_relay"},
        "committed_at": "2026-07-17T11:24:42.364Z",
    }
    binding = _legacy_awc_origin_authority_binding(
        predecessor,
        authority_row=authority,
        authority_scope_projection_content_sha256=projection["content_sha256"],
        bound_at_utc=projection["as_of_utc"],
    )
    assert binding["predecessor_official_source_registry_present"] is False
    assert binding["legacy_origin_profile"] == "PRE_COUNTRY_ROUTER_GLOBAL_AWC"
    assert binding["reason"] == "EXACT_LEGACY_GLOBAL_AWC_AUTHORITY_BINDING"
    tampered = deepcopy(predecessor)
    tampered["fallback_reason"] = "guessed"
    with pytest.raises(ContractError, match="binding_ineligible"):
        _legacy_awc_origin_authority_binding(
            tampered,
            authority_row=authority,
            authority_scope_projection_content_sha256=projection["content_sha256"],
            bound_at_utc=projection["as_of_utc"],
        )


def test_legacy_geographic_timezone_is_rejected_from_canonical_evidence() -> None:
    evidence = _evidence()
    evidence["station_registry"]["KJFK"]["timezone_name"] = "Asia/Shanghai"
    contract, receipt = build_awc_observation_contract(_reseal(evidence))
    assert contract["day_bounds_facts"] == []
    assert "LINEAGE_MISMATCH" in receipt["terminal_gaps"][0]["failure_codes"]


def test_awc_authority_projection_tamper_fails_closed() -> None:
    evidence = _evidence()
    evidence["authority_scope_projection"]["rows"][0]["source_lane"] = "AMSC"
    with pytest.raises(ContractError, match="authority_projection"):
        build_awc_observation_contract(_reseal(evidence))


def test_settlement_timezone_binding_tamper_fails_closed() -> None:
    evidence = _evidence()
    evidence["authority_scope_projection"]["settlement_timezone"] = "UTC"
    with pytest.raises(ContractError, match="authority_projection"):
        build_awc_observation_contract(_reseal(evidence))


def test_speci_enters_vector_but_not_required_hour_set() -> None:
    evidence = _evidence()
    slot = _slots("2026-08-01", "America/New_York")[12] + timedelta(minutes=35)
    evidence["rows"].append(
        _row(
            slot,
            100,
            day="2026-08-01",
            zone="America/New_York",
            report_type="SPECI",
            temperature=28.0,
        )
    )
    contract, receipt = build_awc_observation_contract(_reseal(evidence))
    assert len(contract["revision_facts"]) == 25
    assert contract["day_bounds_facts"][0]["running_max_c"] == 28.0
    assert receipt["coverage_receipts"][0]["expected_hourly_metar_count"] == 24


def test_missing_metar_is_blocked_even_with_speci_in_same_hour() -> None:
    evidence = _evidence()
    missing = evidence["rows"].pop(7)
    slot = datetime.fromisoformat(str(missing["observed_at_utc"]).replace("Z", "+00:00"))
    evidence["rows"].append(
        _row(
            slot + timedelta(minutes=15),
            101,
            day="2026-08-01",
            zone="America/New_York",
            report_type="SPECI",
        )
    )
    contract, receipt = build_awc_observation_contract(_reseal(evidence))
    assert contract["day_bounds_facts"] == []
    assert receipt["state"] == "BLOCKED"
    assert "DATA_MISSING" in receipt["terminal_gaps"][0]["failure_codes"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("origin", "LINEAGE_MISMATCH"),
        ("correction", "REVISION_REVIEW_REQUIRED"),
        ("conflict", "REVISION_LINEAGE_MISSING"),
        ("halt", "REVISION_REVIEW_REQUIRED"),
    ),
)
def test_lineage_revision_and_halt_fail_closed(mutation: str, expected: str) -> None:
    evidence = _evidence()
    if mutation == "origin":
        evidence["rows"][0]["origin_lineage_ok"] = False
    elif mutation == "correction":
        evidence["rows"][0]["correction_kind"] = "COR"
        evidence["rows"][0]["correction_suspected"] = 1
    elif mutation == "conflict":
        duplicate = deepcopy(evidence["rows"][0])
        duplicate["observation_id"] = "conflict"
        duplicate["raw_object_sha256"] = "f" * 64
        evidence["rows"].append(duplicate)
    else:
        evidence["station_day_halts"] = [
            {
                "source_lane": "awc",
                "station_id": "KJFK",
                "event_date": "2026-08-01",
                "state": "active",
                "reason_code": "metar_correction_requires_manual_review",
                "triggering_observation_id": "obs-0",
                "created_at": "2026-08-01T05:01:00Z",
                "cleared_at": None,
            }
        ]
    contract, receipt = build_awc_observation_contract(_reseal(evidence))
    assert contract["day_bounds_facts"] == []
    assert expected in receipt["terminal_gaps"][0]["failure_codes"]


@pytest.mark.parametrize(
    ("day", "expected_count"),
    (("2026-03-08", 23), ("2026-11-01", 25)),
)
def test_dst_day_uses_actual_local_day_hour_count(day: str, expected_count: int) -> None:
    evidence = _evidence(day=day)
    contract, receipt = build_awc_observation_contract(evidence)
    assert len(contract["revision_facts"]) == expected_count
    assert receipt["coverage_receipts"][0]["expected_hourly_metar_count"] == expected_count
