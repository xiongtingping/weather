from __future__ import annotations

from datetime import datetime, timedelta, timezone

from madis_hfmetar import (
    AsosTemperatureProduct,
    build_asos_p1_oos_artifact,
    build_asos_p1_observation,
    match_metar_to_asos,
    exact_asos_temperature_product,
)
from bayes_basket_target.event_contracts import CanonicalObservationEvent
from station_p1_preobservation_schedule import load_schedule


def _minutes(start: datetime, count: int = 20) -> list[AsosTemperatureProduct]:
    return [
        AsosTemperatureProduct(start + timedelta(minutes=index), 20.0 + index / 10, f"{index:064x}")
        for index in range(count)
    ]


def test_exact_asos_product_uses_published_minute_without_second_average() -> None:
    start = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
    value = exact_asos_temperature_product(_minutes(start), at_utc=start + timedelta(minutes=10))
    assert value is not None
    assert value["published_at_utc"] == "2026-08-05T08:10:00.000Z"
    assert value["temperature_c"] == 21.0
    assert "minute_observation_sha256s" not in value


def test_exact_asos_product_does_not_backfill_a_missing_scheduled_minute() -> None:
    start = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
    rows = _minutes(start, 5)
    assert exact_asos_temperature_product(rows, at_utc=start + timedelta(minutes=4)) is not None
    assert exact_asos_temperature_product(rows, at_utc=start + timedelta(minutes=5)) is None


def test_metar_exact_minute_match_is_distinct_from_prior_madis_cycle() -> None:
    start = datetime(2026, 8, 5, 8, 40, tzinfo=timezone.utc)
    matched = match_metar_to_asos(
        {
            "station_id": "KLAX", "observation_id": "metar-1",
            "observed_at_utc": "2026-08-05T08:51:00.000Z",
            "reported_temperature_c": 21.0, "raw_sha256": "a" * 64,
        },
        _minutes(start, 20),
        [{
            "station_id": "KLAX", "observed_at_utc": "2026-08-05T08:50:00.000Z",
            "temperature_c": 20.0, "content_sha256": "b" * 64, "qc_eligible": True,
        }],
    )
    assert matched["match_state"] == "EXACT_ONE_MINUTE_MATCH"
    assert matched["exact_asos_temperature_product"]["published_at_utc"] == "2026-08-05T08:51:00.000Z"
    assert matched["prior_madis_hf_cycle"]["age_seconds"] == 60


def test_independent_asos_p1_oos_artifact_requires_20_plus_8_exact_matches() -> None:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    matches = []
    for index in range(28):
        matched = match_metar_to_asos(
            {
                "station_id": "KLAX", "observation_id": f"metar-{index}",
                "observed_at_utc": (start + timedelta(days=index, minutes=10)).isoformat(),
                "reported_temperature_c": 21.0, "raw_sha256": f"{index + 100:064x}",
            },
            _minutes(start + timedelta(days=index), 20),
            [],
        )
        matches.append(matched)
    blocked = build_asos_p1_oos_artifact(matches[:27])
    assert blocked["state"] == "BLOCKED"
    assert blocked["failure_code"] == "OOS_SAMPLE_SHORT"
    artifact = build_asos_p1_oos_artifact(matches)
    assert artifact["state"] == "OOS_READY"
    assert artifact["train_count"] == 20
    assert artifact["holdout_count"] == 8
    assert artifact["enters_p1"] is True
    assert artifact["formal_live_authority"] is False
    observation = build_asos_p1_observation(
        {
            "station_id": "KLAX", "observed_at_utc": "2026-08-05T08:53:00.000Z",
            "temperature_c": 20.0, "raw_sha256": "c" * 64,
            "qc_eligible": True,
        },
        artifact,
        source_product="EXACT_ASOS_1MIN",
        schedule=load_schedule(),
        planned_metar_observation_at_utc="2026-08-05T08:53:00.000Z",
        event_date="2026-08-05",
        settlement_timezone="America/Los_Angeles",
        canonical_committed_at_utc="2026-08-05T08:53:30.000Z",
    )
    assert observation["source_lane"] == "asos_1min"
    assert observation["report_type"] == "ASOS_1MIN"
    assert observation["schedule_content_sha256"] == load_schedule()["content_sha256"]
    assert observation["evidence_role"] == "FORMAL"
    event = CanonicalObservationEvent.from_mapping({
        "observation_id": observation["observation_id"],
        "station_id": observation["station_id"],
        "event_date": observation["event_date_local"],
        "settlement_timezone": observation["settlement_timezone"],
        "source_lane": observation["source_lane"],
        "report_type": observation["report_type"],
        "sequence": 1,
        "observed_at_utc": observation["valid_at_utc"],
        "received_at_utc": observation["canonical_committed_at_utc"],
        "raw_sha256": observation["raw_sha256"],
        "qc_state": "ACCEPTED",
        "supersedes_observation_id": None,
    })
    assert event.source_lane == "asos_1min"
    assert event.report_type == "ASOS_1MIN"
