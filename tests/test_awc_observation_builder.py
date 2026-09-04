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
    else:
        # An unreviewed correction with its halt still standing: the shape
        # every corrected report has for the minutes before the standing
        # policy reviews it.
        evidence["rows"][0]["correction_kind"] = "COR"
        evidence["rows"][0]["correction_suspected"] = 1
        evidence["station_day_halts"] = [_halt()]
    contract, receipt = build_awc_observation_contract(_reseal(evidence))
    assert contract["day_bounds_facts"] == []
    assert expected in receipt["terminal_gaps"][0]["failure_codes"]


def _halt(*, reason_code: str = "metar_correction_requires_manual_review") -> dict[str, object]:
    return {
        "source_lane": "awc",
        "station_id": "KJFK",
        "event_date": "2026-08-01",
        "state": "active",
        "reason_code": reason_code,
        "triggering_observation_id": "obs-0",
        "created_at": "2026-08-01T05:01:00Z",
        "cleared_at": None,
    }


def _review(decision: str, *, trigger: str = "obs-0", reviewed_at: str = "2026-08-01T05:12:00Z") -> dict[str, object]:
    return {
        "review_id": f"rthreview_{decision.lower()}_{trigger}",
        "source_lane": "awc",
        "station_id": "KJFK",
        "event_date": "2026-08-01",
        "triggering_observation_id": trigger,
        "decision": decision,
        "reviewer": "policy.correction_auto_clear.v1",
        "rationale": "test",
        "reviewed_at": reviewed_at,
        "supersedes_review_id": None,
        "evidence_json": "{}",
        "evidence_sha256": "e" * 64,
        "content_sha256": "c" * 64,
    }


@pytest.mark.parametrize("decision", ("CLEAR_REFERENCE_ONLY", "CLEAR_WITH_REPLACEMENT"))
def test_reviewed_correction_emits_revision_facts_while_the_halt_row_stays_active(
    decision: str,
) -> None:
    """The receipt resolves the correction; the halt row is never flipped.

    Nothing but the manual script ever wrote ``cleared``: all 356 halts in
    the archive were active on 2026-09-02, 41 of the 43 recent ones already
    carrying a CLEAR receipt, and the whole station-day's revision facts were
    withheld for it (SAEZ 09-02: cleared 10:11:49Z, facts stopped 03:36Z).
    """

    evidence = _evidence()
    evidence["rows"][0]["correction_kind"] = "COR"
    evidence["rows"][0]["correction_suspected"] = 1
    evidence["station_day_halts"] = [_halt()]
    evidence["station_day_reviews"] = [_review(decision)]
    contract, receipt = build_awc_observation_contract(_reseal(evidence))

    # Every slot is identified.  The correction stands in its own slot here
    # (nothing else was filed for that hour), so it becomes the fact.
    assert len(contract["revision_facts"]) == len(evidence["rows"])
    assert any(
        fact["observation_id"] == evidence["rows"][0]["observation_id"]
        for fact in contract["revision_facts"]
    )
    assert all(
        "REVISION_REVIEW_REQUIRED" not in gap.get("failure_codes", ())
        for gap in receipt["terminal_gaps"]
    )
    assert all(
        gap.get("revision_facts_emitted", True)
        for gap in receipt["terminal_gaps"]
    )


def test_quarantine_still_withholds_the_station_day() -> None:
    by_receipt = _evidence()
    by_receipt["rows"][0]["correction_kind"] = "COR"
    by_receipt["rows"][0]["correction_suspected"] = 1
    by_receipt["station_day_reviews"] = [
        _review("CLEAR_REFERENCE_ONLY"),
        _review("QUARANTINE_STATION_DAY", reviewed_at="2026-08-01T06:00:00Z"),
    ]
    contract, receipt = build_awc_observation_contract(_reseal(by_receipt))
    assert contract["revision_facts"] == []
    assert "REVISION_REVIEW_REQUIRED" in receipt["terminal_gaps"][0]["failure_codes"]

    by_halt = _evidence()
    by_halt["station_day_halts"] = [
        _halt(reason_code="manual_review_quarantine_station_day")
    ]
    contract, receipt = build_awc_observation_contract(_reseal(by_halt))
    assert contract["revision_facts"] == []
    assert "REVISION_REVIEW_REQUIRED" in receipt["terminal_gaps"][0]["failure_codes"]


def test_a_review_for_one_correction_does_not_clear_another() -> None:
    evidence = _evidence()
    for index in (0, 1):
        evidence["rows"][index]["correction_kind"] = "COR"
        evidence["rows"][index]["correction_suspected"] = 1
    evidence["station_day_halts"] = [_halt()]
    evidence["station_day_reviews"] = [_review("CLEAR_WITH_REPLACEMENT", trigger="obs-0")]
    contract, receipt = build_awc_observation_contract(_reseal(evidence))
    assert contract["revision_facts"] == []
    assert "REVISION_REVIEW_REQUIRED" in receipt["terminal_gaps"][0]["failure_codes"]


@pytest.mark.parametrize(
    ("day", "expected_count"),
    (("2026-03-08", 23), ("2026-11-01", 25)),
)
def test_dst_day_uses_actual_local_day_hour_count(day: str, expected_count: int) -> None:
    evidence = _evidence(day=day)
    contract, receipt = build_awc_observation_contract(evidence)
    assert len(contract["revision_facts"]) == expected_count
    assert receipt["coverage_receipts"][0]["expected_hourly_metar_count"] == expected_count


def test_two_bodies_in_one_slot_are_a_supersession_not_a_defect() -> None:
    """The later transmission stands in the slot; the earlier is named.

    Publishers re-transmit: a COR restating the observation, a spacing typo
    fixed in place.  Both bodies used to survive into the slot-hash check and
    the day was refused ``REVISION_LINEAGE_MISSING`` -- the ordinary life of a
    METAR read as a broken revision chain.  Since the 2026-09-03 ruling the
    slot resolves by publication order and the superseded report is recorded
    rather than counted against the day.
    """

    evidence = _evidence()
    later = deepcopy(evidence["rows"][0])
    later["observation_id"] = "obs-0-restated"
    later["raw_object_sha256"] = "f" * 64
    later["canonical_committed_at_utc"] = "2026-08-01T05:03:00.000Z"
    evidence["rows"].append(later)

    contract, receipt = build_awc_observation_contract(_reseal(evidence))

    assert receipt["terminal_gaps"] == []
    identified = {fact["observation_id"] for fact in contract["revision_facts"]}
    assert "obs-0-restated" in identified
    assert "obs-0" not in identified
    assert len(contract["revision_facts"]) == len(evidence["rows"]) - 1



def _late_speci(day: str, zone: str, *, hour: int, minute: int = 30) -> dict[str, object]:
    slot = _slots(day, zone)[hour] + timedelta(minutes=minute)
    row = _row(slot, 100, day=day, zone=zone, report_type="SPECI", temperature=24.0)
    # Landed two hours after the reports around it were already on file.
    late = slot + timedelta(hours=2)
    row["received_at_utc"] = _stamp(late)
    row["first_seen_at_utc"] = _stamp(late)
    row["parsed_at_utc"] = _stamp(late + timedelta(milliseconds=100))
    row["fetch_started_at_utc"] = _stamp(late - timedelta(seconds=30))
    row["fetch_completed_at_utc"] = _stamp(late)
    row["origin_committed_at_utc"] = _stamp(late + timedelta(milliseconds=500))
    row["canonical_committed_at_utc"] = _stamp(late + timedelta(seconds=1))
    row["raw_text"] = "SPECI KJFK 1030Z 00000KT CAVOK 24/10 A3000"
    return row


def test_a_late_insertion_reissues_the_shifted_reports_as_the_next_revision() -> None:
    """SBGR 2026-09-03: a 10:30Z SPECI landed at 12:34Z, after the day had been
    identified through 12:00Z.  Rebuilt, every later report moved one place,
    the stored facts no longer matched, and the whole day was quarantined --
    so the reports that followed were never identified and P1 lost its
    revision hash for the rest of the day.  With the stored revisions in the
    packet, unchanged reports re-emit their stored fact byte for byte, the
    shifted ones are re-issued as revision two linked to their stored fact,
    and the SPECI itself is a first revision."""

    evidence = _evidence()
    first_contract, _ = build_awc_observation_contract(evidence)
    stored = list(first_contract["revision_facts"])
    assert len(stored) == 24
    by_obs = {fact["observation_id"]: fact for fact in stored}

    later = deepcopy(evidence)
    later["rows"].append(_late_speci("2026-08-01", "America/New_York", hour=10))
    later["stored_revision_facts"] = stored
    contract, receipt = build_awc_observation_contract(_reseal(later))
    parsed = CanonicalAWCObservationContract.from_mapping(contract)
    facts = {x.raw["observation_id"]: x.raw for x in parsed.revision_facts}
    assert len(facts) == 25
    for index in range(0, 11):
        fact = facts[f"obs-{index}"]
        assert fact["revision_no"] == 1
        assert fact["content_sha256"] == by_obs[f"obs-{index}"]["content_sha256"]
    speci = facts["obs-100"]
    assert speci["report_type"] == "SPECI"
    assert speci["revision_no"] == 1 and speci["sequence"] == 12
    assert speci["supersedes_observation_fact_sha256"] is None
    for index in range(11, 24):
        fact = facts[f"obs-{index}"]
        old = by_obs[f"obs-{index}"]
        assert fact["revision_no"] == 2
        assert fact["sequence"] == old["sequence"] + 1
        assert fact["supersedes_observation_fact_sha256"] == old["content_sha256"]
        assert fact["reported_temperature_c"] == old["reported_temperature_c"]
    assert receipt["late_insertion_supersession_count"] == 13
    assert {x["observation_id"] for x in receipt["late_insertion_supersessions"]} == {
        f"obs-{index}" for index in range(11, 24)
    }
    assert receipt["late_insertion_supersessions"][0]["reason"] == "LATE_INSERTION_RENUMBERED"
    # The day still closes: one bounds fact over the re-issued vector.
    assert len(parsed.day_bounds_facts) == 1


def test_stored_revisions_only_absorb_a_change_of_place_not_of_content() -> None:
    """Any other difference is the quarantine's to name, never the build's to
    paper over with a revision."""

    evidence = _evidence()
    first_contract, _ = build_awc_observation_contract(evidence)
    stored = list(first_contract["revision_facts"])
    drifted = deepcopy(stored)
    drifted[5]["reported_temperature_c"] = 99.0
    drifted[5] = {
        **{k: v for k, v in drifted[5].items() if k != "content_sha256"},
        "content_sha256": content_sha256(
            {k: v for k, v in drifted[5].items() if k != "content_sha256"}
        ),
    }
    again = deepcopy(evidence)
    again["stored_revision_facts"] = drifted
    contract, receipt = build_awc_observation_contract(_reseal(again))
    fact = next(x for x in contract["revision_facts"] if x["observation_id"] == "obs-5")
    assert fact["revision_no"] == 1
    assert fact["supersedes_observation_fact_sha256"] is None
    assert fact["content_sha256"] != drifted[5]["content_sha256"]
    assert receipt["late_insertion_supersession_count"] == 0
    # And an unchanged stored set is re-emitted byte for byte.
    same = deepcopy(evidence)
    same["stored_revision_facts"] = stored
    contract, receipt = build_awc_observation_contract(_reseal(same))
    assert [x["content_sha256"] for x in contract["revision_facts"]] == [
        x["content_sha256"] for x in stored
    ]
    assert receipt["late_insertion_supersession_count"] == 0
