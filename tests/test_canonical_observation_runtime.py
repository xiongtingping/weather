from __future__ import annotations

from canonical_observation_runtime import canonicalize_observations
from source_observation_runtime import build_source_observation


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"
EVENT_DATE = "2026-07-03"


def _observation(
    source_id: str,
    *,
    valid_time: str = "2026-07-03T06:30:00Z",
    fetched_at: str = "2026-07-03T06:31:00Z",
    temperature_c: float = 30.0,
    correction: bool = False,
    report_type: str = "METAR",
) -> dict:
    observation = build_source_observation(
        source_id=source_id,
        station_id="ZUUU",
        event_date=EVENT_DATE,
        valid_time=valid_time,
        fetched_at=fetched_at,
        temperature_c=temperature_c,
        dewpoint_c=22.0,
        raw_ref=f"fixture://{source_id}/{report_type}/{valid_time}",
        raw_payload={
            "source": source_id,
            "report_type": report_type,
            "valid": valid_time,
            "temperature": temperature_c,
        },
        payload={"report_type": report_type, "is_correction": correction},
    )
    return observation.as_dict()


def _canonicalize(rows: list[dict], **kwargs):
    return canonicalize_observations(
        rows,
        target_identity=TARGET,
        event_date=EVENT_DATE,
        decision_as_of="2026-07-03T07:00:00Z",
        **kwargs,
    )


def test_amsc_half_hour_is_primary_and_awc_same_physical_event_only_confirms() -> None:
    result = _canonicalize(
        [
            _observation("zuuu_metar_raw_awc"),
            _observation("zuuu_metar_raw_amsc"),
        ]
    )

    assert result["accepted_event_count"] == 1
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["state"] == "accepted_primary"
    assert event["selection_role"] == "primary"
    assert event["selected_source_id"] == "zuuu_metar_raw_amsc"
    assert event["valid_time"] == "2026-07-03T06:30:00Z"
    assert {relation["relation"] for relation in result["relations"]} == {"selected", "confirmation"}


def test_amsc_metar_and_speci_are_distinct_canonical_events() -> None:
    result = _canonicalize(
        [
            _observation(
                "zuuu_metar_raw_amsc",
                valid_time="2026-07-03T06:30:00Z",
                fetched_at="2026-07-03T06:31:00Z",
                report_type="METAR",
            ),
            _observation(
                "zuuu_metar_raw_amsc",
                valid_time="2026-07-03T06:31:00Z",
                fetched_at="2026-07-03T06:31:10Z",
                report_type="SPECI",
            ),
        ]
    )

    assert result["accepted_event_count"] == 2
    assert [(event["report_type"], event["valid_time"]) for event in result["events"]] == [
        ("METAR", "2026-07-03T06:30:00Z"),
        ("SPECI", "2026-07-03T06:31:00Z"),
    ]
    assert len({event["canonical_observation_key"] for event in result["events"]}) == 2


def test_archived_speci_raw_text_remains_speci_when_legacy_payload_lacks_report_type() -> None:
    source = _observation(
        "zuuu_metar_raw_amsc",
        valid_time="2026-07-03T06:31:00Z",
        fetched_at="2026-07-03T06:31:10Z",
        report_type="SPECI",
    )
    source["payload"].pop("report_type")
    source.pop("report_type", None)
    source["raw_ref"] = "SPECI ZUUU 030631Z 02003MPS 9999 30/22 Q1002="

    result = _canonicalize([source])

    assert result["accepted_event_count"] == 1
    assert result["events"][0]["report_type"] == "SPECI"


def test_awc_is_substitute_only_when_policy_explicitly_allows_it() -> None:
    source = _observation("zuuu_metar_raw_awc")

    blocked = _canonicalize([source])
    allowed = _canonicalize([source], allow_awc_substitute=True)

    assert blocked["events"][0]["state"] == "candidate"
    assert blocked["accepted_event_count"] == 0
    assert allowed["events"][0]["state"] == "accepted_substitute"
    assert allowed["events"][0]["selection_role"] == "substitute"


def test_later_amsc_matching_existing_awc_substitute_is_confirmation_not_second_event() -> None:
    substitute = _canonicalize([_observation("zuuu_metar_raw_awc")], allow_awc_substitute=True)
    existing = substitute["events"]

    result = _canonicalize([_observation("zuuu_metar_raw_amsc")], existing_events=existing)

    assert result["events"] == []
    assert result["accepted_event_count"] == 0
    assert result["relations"][0]["canonical_observation_event_id"] == existing[0]["canonical_observation_event_id"]
    assert result["relations"][0]["relation"] == "confirmation"


def test_unexplained_physical_difference_is_conflict_blocked() -> None:
    result = _canonicalize(
        [
            _observation("zuuu_metar_raw_amsc", temperature_c=30.0),
            _observation("zuuu_metar_raw_awc", temperature_c=31.0),
        ]
    )

    assert result["accepted_event_count"] == 1
    assert {event["state"] for event in result["events"]} == {
        "accepted_primary",
        "conflict_blocked",
    }
    conflict = next(event for event in result["events"] if event["state"] == "conflict_blocked")
    assert conflict["conflict_group_id"] == conflict["canonical_observation_key"]


def test_correction_appends_next_revision_and_never_reuses_existing_revision_number() -> None:
    existing = _canonicalize(
        [_observation("zuuu_metar_raw_awc", temperature_c=30.0)],
        allow_awc_substitute=True,
    )["events"]

    result = _canonicalize(
        [_observation("zuuu_metar_raw_amsc", temperature_c=31.0, correction=True)],
        existing_events=existing,
    )

    assert len(result["events"]) == 1
    correction = result["events"][0]
    assert correction["revision_no"] == 1
    assert correction["state"] == "accepted_primary"
    assert correction["supersedes_event_id"] == existing[0]["canonical_observation_event_id"]


def test_future_transport_is_rejected_by_strict_as_of() -> None:
    future = _observation("zuuu_metar_raw_amsc", fetched_at="2026-07-03T07:01:00Z")

    result = _canonicalize([future])

    assert result["events"] == []
    assert result["rejected"] == [
        {
            "source_observation_id": future["source_observation_id"],
            "reason": "strict_as_of_future",
        }
    ]


def test_canonicalization_rejects_invalid_valid_time_and_impossible_timestamp_order() -> None:
    invalid_valid_time = _observation("zuuu_metar_raw_amsc", valid_time="not-a-time")
    future_valid_time = _observation("zuuu_metar_raw_amsc", valid_time="2026-07-03T06:32:00Z")

    result = _canonicalize([invalid_valid_time, future_valid_time])

    assert result["events"] == []
    assert {row["reason"] for row in result["rejected"]} == {
        "valid_time_missing_or_invalid",
        "valid_time_after_first_transport",
    }


def test_canonicalization_rejects_transport_time_after_actionable_time() -> None:
    observation = _observation("zuuu_metar_raw_amsc")
    observation["first_transport_seen_at"] = "2026-07-03T06:32:00Z"

    result = _canonicalize([observation])

    assert result["events"] == []
    assert result["rejected"][0]["reason"] == "first_transport_seen_at_after_first_actionable"
