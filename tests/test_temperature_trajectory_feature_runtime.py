from __future__ import annotations

from temperature_trajectory_feature_runtime import (
    OWNER_ID,
    SCHEMA_VERSION,
    build_temperature_trajectory_feature_state,
    cloud_text_contains_cb,
)


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"


def _event(index: int, temperature_c: float, *, actionable_minute: int | None = None, state: str = "accepted_primary", cloud_text: str = "FEW020") -> dict:
    minute = index * 30
    hour = 6 + minute // 60
    minute = minute % 60
    actionable = actionable_minute if actionable_minute is not None else hour * 60 + minute + 1
    action_hour, action_minute = divmod(actionable, 60)
    return {
        "canonical_observation_event_id": f"canobs_trajectory_{index:02d}_1234567890",
        "canonical_observation_key": f"canokey_trajectory_{index:02d}",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "state": state,
        "valid_time": f"2026-07-03T{hour:02d}:{minute:02d}:00Z",
        "first_actionable_seen_at": f"2026-07-03T{action_hour:02d}:{action_minute:02d}:00Z",
        "parsed_observation": {
            "temperature_c": temperature_c,
            "dewpoint_c": 22.0,
            "cloud_text": cloud_text,
        },
    }


def _build(temperatures: list[float], **overrides) -> dict:
    values = {
        "canonical_events": [_event(index, temperature) for index, temperature in enumerate(temperatures)],
        "decision_as_of": "2026-07-03T09:00:00Z",
    }
    values.update(overrides)
    return build_temperature_trajectory_feature_state(**values)


def test_sustained_integer_warming_is_not_overridden_as_platform() -> None:
    state = _build([31.0, 32.0, 33.0, 34.0])

    assert state["schema"] == SCHEMA_VERSION
    assert state["owner_id"] == OWNER_ID
    assert state["ok"] is True
    assert state["path_state_candidate"] == "sustained_warming"
    assert state["path_state_evidence"]["temporary_platform"] is False
    assert state["slopes"]["30m"]["display_rate_c_per_hour"] == 2.0
    assert state["slopes"]["30m"]["interval_rate_lower_c_per_hour"] == 0.0
    assert state["observed_max_lower_bound_c"] == 33.5
    assert state["boundary"]["not_a_strategy_signal"] is True


def test_confirmed_cooling_requires_two_display_drops_and_rebound_is_bidirectional() -> None:
    cooling = _build([34.0, 33.0, 32.0])
    pullback = _build([34.0, 33.0])
    rebound = _build([34.0, 33.0, 34.0])

    assert cooling["path_state_candidate"] == "cooling"
    assert cooling["path_state_evidence"]["confirmed_display_cooling"] is True
    assert cooling["path_state_evidence"]["cooling_depth_display_c"] == 2.0
    assert pullback["path_state_candidate"] == "pullback_watch"
    assert rebound["path_state_candidate"] == "rebound"
    assert rebound["path_state_evidence"]["rebound"] is True


def test_future_duplicate_or_nonaccepted_canonical_events_fail_closed() -> None:
    future = _event(0, 30.0, actionable_minute=10 * 60)
    first = _event(1, 31.0)
    duplicate = _event(2, 32.0)
    duplicate["canonical_observation_event_id"] = first["canonical_observation_event_id"]
    nonaccepted = _event(3, 33.0, state="confirmed")
    state = build_temperature_trajectory_feature_state(
        canonical_events=[future, first, duplicate, nonaccepted],
        decision_as_of="2026-07-03T09:00:00Z",
    )

    assert state["ok"] is False
    assert "trajectory_canonical_event_after_asof" in state["block_reasons"]
    assert "trajectory_canonical_event_not_accepted" in state["block_reasons"]
    assert "trajectory_canonical_event_duplicate" in state["block_reasons"]


def test_first_cb_lineage_is_causal_and_does_not_conflate_tcu() -> None:
    assert cloud_text_contains_cb("FEW033CB FEW040") is True
    assert cloud_text_contains_cb("FEW033TCU SCT040") is False
    events = [
        _event(0, 36.0, cloud_text="FEW033TCU"),
        _event(1, 37.0, cloud_text="SCT033"),
        _event(2, 38.0, cloud_text="FEW033CB FEW040"),
    ]
    pre_cb = build_temperature_trajectory_feature_state(
        canonical_events=events[:2],
        decision_as_of=events[2]["valid_time"],
        temporal_role="pre_event",
    )
    post_cb = build_temperature_trajectory_feature_state(
        canonical_events=events,
        decision_as_of="2026-07-03T09:00:00Z",
        temporal_role="post_event",
    )

    assert pre_cb["first_cb_evidence"]["cb_seen"] is False
    assert post_cb["first_cb_evidence"]["cb_seen"] is True
    assert post_cb["first_cb_evidence"]["first_cb_canonical_observation_event_id"] == events[2]["canonical_observation_event_id"]
    assert post_cb["first_cb_evidence"]["first_cb_temperature_c"] == 38.0
    assert post_cb["first_cb_evidence"]["first_cb_at_observed_peak"] is True
