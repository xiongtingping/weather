from __future__ import annotations

import copy

import pytest

from bayes_basket_target.canonical_event_replay import replay_canonical_events
from bayes_basket_target.contracts import ContractError


def _events() -> list[dict[str, object]]:
    return [
        {
            "observation_id": "o1",
            "station_id": "ZUUU",
            "source_lane": "AMSC",
            "report_type": "METAR",
            "event_sequence": 1,
            "observed_at_utc": "2026-07-23T00:10:00Z",
            "raw_sha256": "1" * 64,
        },
        {
            "observation_id": "o2",
            "station_id": "ZUUU",
            "source_lane": "AMSC",
            "report_type": "SPECI",
            "event_sequence": 2,
            "observed_at_utc": "2026-07-23T00:20:00Z",
            "raw_sha256": "2" * 64,
        },
    ]


def test_duplicate_and_incremental_replay_are_idempotent() -> None:
    events = _events()
    full = replay_canonical_events([*events, copy.deepcopy(events[1])], station_id="ZUUU", source_lane="AMSC")
    prefix = replay_canonical_events(events[:1], station_id="ZUUU", source_lane="AMSC")
    suffix = replay_canonical_events(events[1:], station_id="ZUUU", source_lane="AMSC", initial_state={
        "last_sequence": prefix["last_sequence"],
        "last_observed_at_utc": prefix["last_observed_at_utc"],
        "seen_event_ids": prefix["seen_event_ids"],
    })
    assert full["accepted_event_ids"] == prefix["accepted_event_ids"] + suffix["accepted_event_ids"]
    assert full["duplicate_event_ids"] == ["o2"]
    assert full["boundary"]["places_orders"] is False


def test_rewind_route_and_correction_fail_closed() -> None:
    events = _events()
    with pytest.raises(ContractError, match="sequence_rewind"):
        replay_canonical_events([*events, {**events[0], "observation_id": "o3"}], station_id="ZUUU", source_lane="AMSC")
    with pytest.raises(ContractError, match="source_route_mismatch"):
        replay_canonical_events(events, station_id="ZUUU", source_lane="AWC")
    with pytest.raises(ContractError, match="after_correction_halted"):
        replay_canonical_events(
            [{**events[0], "correction_suspected": True}, events[1]],
            station_id="ZUUU", source_lane="AMSC",
        )
