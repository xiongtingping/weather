from __future__ import annotations

from amsc_specli_resident_arrival_contract import (
    build_amsc_specli_resident_arrival_envelope,
    validate_amsc_specli_resident_arrival_envelope,
)
from source_observation_runtime import build_source_observation


def _source() -> dict:
    return build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:31:00Z",
        response_completed_at="2026-07-03T06:31:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://arrival-contract",
        raw_payload={"report_type": "METAR"},
        payload={
            "report_type": "METAR",
            "arrival_time_quality": "observed_first_seen",
            "first_seen_at": "2026-07-03T06:31:00Z",
            "as_of_eligible_at": "2026-07-03T06:31:00Z",
        },
    ).as_dict()


def test_primary_observation_builds_t1_from_persisted_first_seen_not_response_time() -> None:
    source = _source()
    source["response_completed_at"] = "2026-07-03T06:40:00Z"
    source["fetched_at"] = "2026-07-03T06:40:00Z"

    envelope = build_amsc_specli_resident_arrival_envelope(source)

    assert envelope["ok"] is True
    assert envelope["first_transport_seen_at"] == "2026-07-03T06:31:00Z"
    assert envelope["first_actionable_seen_at"] == "2026-07-03T06:31:00Z"
    assert envelope["response_completed_at"] == "2026-07-03T06:40:00Z"
    assert envelope["timestamp_provenance"]["response_completed_at_resets_t1"] is False
    assert envelope["dispatch_source_observation"]["first_transport_seen_at"] == "2026-07-03T06:31:00Z"


def test_arrival_envelope_rejects_missing_observed_first_seen_or_tampered_t1() -> None:
    source = _source()
    source["payload"].pop("first_seen_at")
    missing = build_amsc_specli_resident_arrival_envelope(source)
    assert missing["ok"] is False
    assert "amsc_resident_arrival_first_seen_at_missing_or_invalid" in missing["blockers"]

    envelope = build_amsc_specli_resident_arrival_envelope(_source())
    envelope["first_actionable_seen_at"] = "2026-07-03T06:32:00Z"
    tampered = validate_amsc_specli_resident_arrival_envelope(envelope)
    assert tampered["ok"] is False
    assert "amsc_resident_arrival_envelope_first_actionable_seen_at_mismatch" in tampered["blockers"]
