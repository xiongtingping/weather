from production_weak_forecast_revision_event_runtime import (
    apply_production_taf_revision_from_source_observations,
    select_new_taf_revision_candidates,
)


AS_OF = "2026-07-14T17:30:40Z"


def _taf(day: str, revision_id: str, *, issue_time: str = "2026-07-14T15:08:00Z") -> dict:
    return {
        "source_id": "zuuu_taf_forecast_amsc",
        "source_event_id": f"event-{revision_id}",
        "source_observation_id": f"observation-{revision_id}",
        "source_class": "forecast_weak_source",
        "event_date": day,
        "fetched_at": AS_OF,
        "issue_time_utc": issue_time,
        "forecast_peak_c": 38.0,
        "raw_sha256": revision_id,
        "payload": {
            "event_date": day,
            "first_seen_at": AS_OF,
            "source_revision_id": revision_id,
            "forecast_revision_kind": "amendment",
            "issue_time_utc": issue_time,
        },
    }


def test_same_fetch_revisions_for_two_target_days_are_isolated_before_update() -> None:
    selected = select_new_taf_revision_candidates(
        [
            _taf("2026-07-15", "current-day-amd"),
            _taf("2026-07-14", "previous-day-amd"),
        ],
        event_date="2026-07-15",
        decision_as_of=AS_OF,
    )

    assert [row["payload"]["source_revision_id"] for row in selected] == ["current-day-amd"]


def test_previous_day_revision_alone_is_not_applied_to_current_day() -> None:
    result = apply_production_taf_revision_from_source_observations(
        None,  # no DB access is permitted when the target day has no revision
        activation={},
        event_date="2026-07-15",
        source_observations=[_taf("2026-07-14", "previous-day-amd")],
        decision_as_of=AS_OF,
        public_slug="highest-temperature-in-chengdu-on-july-15-2026",
    )

    assert result["ok"] is True
    assert result["state"] == "no_new_taf_revision"
    assert result["places_orders"] is False


def test_multiple_new_same_day_revisions_select_latest_issue_instead_of_blocking_batch() -> None:
    selected = select_new_taf_revision_candidates(
        [
            _taf("2026-07-15", "older-amd", issue_time="2026-07-14T12:49:00Z"),
            _taf("2026-07-15", "newer-original", issue_time="2026-07-14T15:08:00Z"),
        ],
        event_date="2026-07-15",
        decision_as_of=AS_OF,
    )

    assert [row["payload"]["source_revision_id"] for row in selected] == [
        "older-amd", "newer-original",
    ]


def test_delayed_fetch_is_selected_inside_checkpoint_window() -> None:
    row = _taf("2026-07-15", "delayed-amd")
    row["payload"]["first_seen_at"] = "2026-07-14T17:30:40Z"
    row["fetched_at"] = "2026-07-14T18:01:07Z"
    selected = select_new_taf_revision_candidates(
        [row],
        event_date="2026-07-15",
        visible_after="2026-07-14T17:45:00Z",
        decision_as_of="2026-07-14T18:01:07Z",
    )
    assert [item["payload"]["source_revision_id"] for item in selected] == ["delayed-amd"]


def test_latest_applied_revision_covers_older_batch_members() -> None:
    selected = select_new_taf_revision_candidates(
        [
            _taf("2026-07-15", "older-amd", issue_time="2026-07-14T12:49:00Z"),
            _taf("2026-07-15", "newer-amd", issue_time="2026-07-14T15:08:00Z"),
        ],
        event_date="2026-07-15",
        decision_as_of=AS_OF,
        applied_revision_ids={"newer-amd"},
    )
    assert selected == []
