from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.nwp_rotation_state import (
    build_rotation_state,
    load_rotation_state,
    persist_rotation_state,
    validate_rotation_state,
)


STATIONS = ("EGLC", "RKPK")


def _scope(station: str, date: str) -> dict:
    return {
        "station_ids": [station],
        "event_date": date,
        "settlement_timezone": (
            "Europe/London" if station == "EGLC" else "Asia/Seoul"
        ),
    }


def test_rotation_state_wraps_and_survives_restart(tmp_path) -> None:
    scopes = [
        _scope("EGLC", "2026-07-30"),
        _scope("RKPK", "2026-07-30"),
    ]
    first = build_rotation_state(
        station_ids=STATIONS,
        scopes=scopes,
        selected_scopes=[scopes[0]],
        cursor_by_priority={0: 1},
        outcomes=[{
            "scope_key": "EGLC|2026-07-30|Europe/London",
            "state": "failed",
            "reason": "provider_timeout",
        }],
        previous=None,
        updated_at_utc="2026-07-29T12:00:00Z",
    )
    assert persist_rotation_state(tmp_path / "rotation.json", first) is True
    loaded = load_rotation_state(
        tmp_path / "rotation.json",
        station_ids=STATIONS,
    )
    assert loaded == first
    second = build_rotation_state(
        station_ids=STATIONS,
        scopes=scopes,
        selected_scopes=[scopes[1]],
        cursor_by_priority={0: 0},
        outcomes=[{
            "scope_key": "RKPK|2026-07-30|Asia/Seoul",
            "state": "candidate_capture_only",
            "candidate_only": True,
        }],
        previous=loaded,
        updated_at_utc="2026-07-29T12:15:00Z",
    )
    assert second["generation"] == 2
    assert second["attempted_scope_count"] == 2
    assert second["complete_rotation"] is True


def test_catalog_change_resets_bounded_coverage() -> None:
    first_scope = _scope("RKPK", "2026-07-30")
    first = build_rotation_state(
        station_ids=STATIONS,
        scopes=[first_scope],
        selected_scopes=[first_scope],
        cursor_by_priority={0: 0},
        outcomes=[{
            "scope_key": "RKPK|2026-07-30|Asia/Seoul",
            "state": "captured",
        }],
        previous=None,
        updated_at_utc="2026-07-29T12:00:00Z",
    )
    next_scope = _scope("RKPK", "2026-07-31")
    second = build_rotation_state(
        station_ids=STATIONS,
        scopes=[next_scope],
        selected_scopes=[next_scope],
        cursor_by_priority={0: 0},
        outcomes=[{
            "scope_key": "RKPK|2026-07-31|Asia/Seoul",
            "state": "captured",
        }],
        previous=first,
        updated_at_utc="2026-07-30T12:00:00Z",
    )
    assert second["generation"] == 1
    assert second["previous_content_sha256"] == ""
    assert second["attempted_scope_keys"] == [
        "RKPK|2026-07-31|Asia/Seoul"
    ]


def test_tamper_and_scope_drift_fail_closed(tmp_path) -> None:
    scope = _scope("RKPK", "2026-07-30")
    value = build_rotation_state(
        station_ids=STATIONS,
        scopes=[scope],
        selected_scopes=[scope],
        cursor_by_priority={0: 0},
        outcomes=[{
            "scope_key": "RKPK|2026-07-30|Asia/Seoul",
            "state": "captured",
        }],
        previous=None,
        updated_at_utc="2026-07-29T12:00:00Z",
    )
    value["attempted_scope_keys"] = []
    with pytest.raises(ContractError, match="sha256"):
        validate_rotation_state(value, station_ids=STATIONS)

    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "rotation.json"
    link.symlink_to(target)
    with pytest.raises(ContractError, match="symlink"):
        persist_rotation_state(link, value)


def test_135_scope_rotation_completes_in_34_bounded_cycles() -> None:
    stations = tuple(f"K{index:03d}" for index in range(45))
    scopes = [
        {
            "station_ids": [station],
            "event_date": event_date,
            "settlement_timezone": "UTC",
        }
        for event_date in (
            "2026-07-30",
            "2026-07-31",
            "2026-08-01",
        )
        for station in stations
    ]
    previous = None
    for cycle in range(34):
        selected = scopes[cycle * 4:(cycle + 1) * 4]
        previous = build_rotation_state(
            station_ids=stations,
            scopes=scopes,
            selected_scopes=selected,
            cursor_by_priority={0: ((cycle + 1) * 4) % len(scopes)},
            outcomes=[
                {
                    "scope_key": (
                        f"{row['station_ids'][0]}|"
                        f"{row['event_date']}|UTC"
                    ),
                    # Failed network attempts still prove fair admission;
                    # success remains a separate materialization contract.
                    "state": "BLOCKED_FAIL_CLOSED",
                    "reason": "provider_timeout",
                }
                for row in selected
            ],
            previous=previous,
            updated_at_utc=f"2026-07-30T{cycle % 24:02d}:00:00Z",
        )
    assert previous is not None
    assert previous["generation"] == 34
    assert previous["catalog_scope_count"] == 135
    assert previous["attempted_scope_count"] == 135
    assert previous["complete_rotation"] is True
    assert len(previous["latest_selected_scope_keys"]) == 3


def test_catalog_low_high_low_resets_coverage_each_time() -> None:
    low = [_scope("RKPK", "2026-07-30")]
    high = low + [_scope("EGLC", "2026-07-30")]
    first = build_rotation_state(
        station_ids=STATIONS,
        scopes=low,
        selected_scopes=low,
        cursor_by_priority={0: 0},
        outcomes=[{
            "scope_key": "RKPK|2026-07-30|Asia/Seoul",
            "state": "captured",
        }],
        previous=None,
        updated_at_utc="2026-07-29T12:00:00Z",
    )
    second = build_rotation_state(
        station_ids=STATIONS,
        scopes=high,
        selected_scopes=[high[1]],
        cursor_by_priority={0: 0},
        outcomes=[{
            "scope_key": "EGLC|2026-07-30|Europe/London",
            "state": "captured",
        }],
        previous=first,
        updated_at_utc="2026-07-29T12:15:00Z",
    )
    third = build_rotation_state(
        station_ids=STATIONS,
        scopes=low,
        selected_scopes=low,
        cursor_by_priority={0: 0},
        outcomes=[{
            "scope_key": "RKPK|2026-07-30|Asia/Seoul",
            "state": "captured",
        }],
        previous=second,
        updated_at_utc="2026-07-29T12:30:00Z",
    )
    assert second["generation"] == 1
    assert second["attempted_scope_keys"] == [
        "EGLC|2026-07-30|Europe/London"
    ]
    assert third["generation"] == 1
    assert third["attempted_scope_keys"] == [
        "RKPK|2026-07-30|Asia/Seoul"
    ]
