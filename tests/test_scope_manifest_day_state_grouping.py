"""One event day can be open at some stations and not started at others.

``event_day_state`` reads like a property of the event day and is not: it is
a property of the day AT A STATION, and the fleet spans every timezone.  For
the hours around midnight the same event date is genuinely open in Tokyo and
not started in Los Angeles -- measured 2026-08-31T23:45Z, 2026-09-01 was open
at 32 stations and not started at 16.

The merge refuses to fuse those, and correctly: the merged document carries a
single ``event_day_state`` and downstream reads it, so one group's stations
would be described by the other's state.  What was wrong was asking the merge
to swallow a mixed set at all.  They are two ingress passes over one day.
"""

from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.production_worker import (
    _group_scope_manifests_by_day_state,
    _merge_scope_manifests,
)


def _scope(station: str, day: str, state: str) -> dict:
    return {
        "scope_key": f"{station}|{day}",
        "station_id": station,
        "event_date": day,
        "event_day_state": state,
        "station_scope": {"mode": "allowlist", "station_ids": [station]},
        "quotes_by_token": {f"{station}-tok": {"bid": 0.4}},
    }


def test_a_mixed_day_splits_into_one_group_per_state() -> None:
    scopes = [
        _scope("RJTT", "2026-09-01", "open"),
        _scope("KLAX", "2026-09-01", "not_started"),
        _scope("EGLC", "2026-09-01", "open"),
    ]
    groups = _group_scope_manifests_by_day_state(scopes)

    assert [state for state, _ in groups] == ["open", "not_started"]
    assert [len(group) for _, group in groups] == [2, 1]


def test_open_is_served_first() -> None:
    # The day that can still trade gets the cycle's lease before the day that
    # has not begun.
    groups = _group_scope_manifests_by_day_state([
        _scope("KLAX", "2026-09-01", "not_started"),
        _scope("RJTT", "2026-09-01", "open"),
    ])
    assert groups[0][0] == "open"


def test_each_group_merges_where_the_mixed_set_could_not() -> None:
    scopes = [
        _scope("RJTT", "2026-09-01", "open"),
        _scope("KLAX", "2026-09-01", "not_started"),
    ]
    with pytest.raises(
        ContractError, match="resident_target_ws_sweep_scope_not_one_day"
    ):
        _merge_scope_manifests(scopes)

    for _state, group in _group_scope_manifests_by_day_state(scopes):
        merged = _merge_scope_manifests(group + group[:1])
        assert merged["event_date"] == "2026-09-01"


def test_a_single_state_day_still_makes_one_group() -> None:
    scopes = [
        _scope("RJTT", "2026-09-01", "open"),
        _scope("EGLC", "2026-09-01", "open"),
    ]
    groups = _group_scope_manifests_by_day_state(scopes)
    assert len(groups) == 1
    merged = _merge_scope_manifests(groups[0][1])
    assert sorted(merged["station_scope"]["station_ids"]) == ["EGLC", "RJTT"]


def test_the_cross_day_refusal_is_untouched() -> None:
    # Merging across event days stays fatal: everything downstream reads one
    # event_date off the merged document.
    with pytest.raises(
        ContractError, match="resident_target_ws_sweep_scope_not_one_day"
    ):
        _merge_scope_manifests([
            _scope("RJTT", "2026-09-01", "open"),
            _scope("RJTT", "2026-09-02", "open"),
        ])


def test_non_mappings_are_dropped_rather_than_grouped() -> None:
    groups = _group_scope_manifests_by_day_state([
        _scope("RJTT", "2026-09-01", "open"),
        "not-a-manifest",
        None,
    ])
    assert len(groups) == 1
    assert len(groups[0][1]) == 1
