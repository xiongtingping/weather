from amsc_half_hour_trigger_runtime import classify_half_hour_fetch


def _plan(*, fact_as_of: str, metar_at: str, taf_first_seen: str = "") -> dict:
    observations = []
    if taf_first_seen:
        observations.append(
            {
                "source_id": "zuuu_taf_forecast_amsc",
                "source_event_id": "srcevt_taf_amd",
                "source_observation_id": "srcobs_taf_amd",
                "event_date": "2026-07-15",
                "fetched_at": taf_first_seen,
                "forecast_peak_c": 35.0,
                "raw_sha256": "abc",
                "payload": {
                    "first_seen_at": taf_first_seen,
                    "raw_sha256": "abc",
                    "raw_text": "TAF AMD ZUUU 150030Z 1500/1600 TX35/1508Z=",
                },
            }
        )
    return {
        "fact_as_of": fact_as_of,
        "effective_payload": {
            "weather_likelihood_features": {"zuuu_metar_raw_amsc_latest_at": metar_at},
            "source_observations": observations,
        },
    }


def test_overnight_half_hour_triggers_and_emits_new_taf_without_reusing_old_metar() -> None:
    result = classify_half_hour_fetch(
        _plan(
            fact_as_of="2026-07-14T16:30:41Z",
            metar_at="2026-07-14T16:00:00Z",
            taf_first_seen="2026-07-14T16:30:41Z",
        ),
        attempt_started_at="2026-07-14T16:30:35Z",
    )
    assert result["triggered"] is True
    assert result["scheduled_regular_metar_slot"] is False
    assert result["strong_observation_candidate"] is False
    assert result["forecast_revision_candidate_count"] == 1
    assert result["state"] == "forecast_revision_only"
    assert result["stale_metar_reconsumed"] is False


def test_overnight_half_hour_with_no_new_report_is_audited_noop() -> None:
    result = classify_half_hour_fetch(
        _plan(fact_as_of="2026-07-14T16:30:41Z", metar_at="2026-07-14T16:00:00Z"),
        attempt_started_at="2026-07-14T16:30:35Z",
    )
    assert result["state"] == "no_new_event"
    assert result["strong_observation_candidate"] is False
    assert result["forecast_revision_candidate_count"] == 0


def test_scheduled_exact_metar_remains_strong_candidate() -> None:
    result = classify_half_hour_fetch(
        _plan(fact_as_of="2026-07-14T17:00:41Z", metar_at="2026-07-14T17:00:00Z"),
        attempt_started_at="2026-07-14T17:00:35Z",
    )
    assert result["scheduled_regular_metar_slot"] is True
    assert result["strong_observation_candidate"] is True
    assert result["settlement_metar_slot"] is True
    assert result["state"] == "strong_observation"


def test_daytime_half_hour_exact_metar_triggers_strong_candidate() -> None:
    result = classify_half_hour_fetch(
        _plan(fact_as_of="2026-07-15T06:30:41Z", metar_at="2026-07-15T06:30:00Z"),
        attempt_started_at="2026-07-15T06:30:35Z",
    )
    assert result["trigger_slot_at"] == "2026-07-15T06:30:00Z"
    assert result["scheduled_regular_metar_slot"] is True
    assert result["settlement_metar_slot"] is False
    assert result["strong_observation_candidate"] is False
    assert result["strong_observation_na_reason"] == "half_hour_reference_only"
    assert result["state"] == "no_new_event"


def test_delayed_local_fetch_still_triggers_taf_revision() -> None:
    plan = _plan(
        fact_as_of="2026-07-14T18:01:07Z",
        metar_at="2026-07-14T18:00:00Z",
        taf_first_seen="2026-07-14T17:30:40Z",
    )
    plan["effective_payload"]["source_observations"][0]["fetched_at"] = "2026-07-14T18:01:07Z"
    result = classify_half_hour_fetch(
        plan,
        attempt_started_at="2026-07-14T18:00:35Z",
    )
    assert result["forecast_revision_candidate_count"] == 1
    assert result["forecast_revision_candidates"][0]["first_system_seen_at"] == "2026-07-14T18:01:07Z"
