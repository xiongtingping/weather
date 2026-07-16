from production_reported_x_weak_revision_event_runtime import (
    apply_reported_x_taf_revision_from_source_observations,
)


def test_non_target_day_taf_revision_does_not_touch_database() -> None:
    result = apply_reported_x_taf_revision_from_source_observations(
        None,
        activation={},
        event_date="2026-07-15",
        source_observations=[
            {
                "source_id": "zuuu_taf_forecast_amsc",
                "source_event_id": "taf-old-day",
                "source_observation_id": "taf-old-day",
                "event_date": "2026-07-14",
                "fetched_at": "2026-07-15T01:00:00Z",
                "payload": {
                    "event_date": "2026-07-14",
                    "first_seen_at": "2026-07-15T01:00:00Z",
                    "source_revision_id": "taf-old-day",
                },
            }
        ],
        decision_as_of="2026-07-15T01:00:00Z",
        public_slug="chengdu-high-2026-07-15",
    )
    assert result == {
        "schema": "production_reported_x_weak_revision_event_runtime_v1",
        "ok": True,
        "state": "no_new_taf_revision",
        "places_orders": False,
    }
