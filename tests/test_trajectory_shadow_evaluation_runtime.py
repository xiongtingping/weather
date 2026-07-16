from trajectory_label_sample_runtime import build_trajectory_label_sample
from trajectory_shadow_evaluation_runtime import build_shadow_prediction, evaluate_shadow_predictions


BUCKETS = ["35°C", "36°C", "37°C", "38°C", "39°C or higher"]


def _source(valid_time: str, temperature_c: float):
    day = valid_time[8:10]
    hour_minute = valid_time[11:16].replace(":", "")
    return {
        "source_id": "zuuu_metar_raw_amsc",
        "source_class": "hard_observation_candidate",
        "station_id": "ZUUU",
        "temperature_c": temperature_c,
        "valid_time": valid_time,
        "fetched_at": valid_time,
        "parsed_at": valid_time,
        "raw_sha256": f"sha{hour_minute}",
        "raw_object_ref": f"objects/aa/sha{hour_minute}.json.gz",
        "source_observation_id": f"srcobs{hour_minute}",
        "parser_version": "amsc_zuuu_metar_taf_fetcher_v1",
        "payload": {
            "raw_text": f"METAR ZUUU {day}{hour_minute}Z 00000MPS CAVOK {int(temperature_c)}/20 Q1000=",
            "first_seen_at": valid_time,
            "arrival_time_quality": "observed_first_seen",
        },
    }


def _sample():
    return build_trajectory_label_sample(
        station_id="ZUUU",
        event_date="2026-07-05",
        cutoff_as_of="2026-07-05T03:05:00Z",
        source_observations=[_source("2026-07-05T02:00:00Z", 36), _source("2026-07-05T03:00:00Z", 37)],
        label={"label_kind": "formal_settlement", "settlement_actual": 38.0},
        coverage_manifest={
            "event_date": "2026-07-05",
            "station_id": "ZUUU",
            "coverage_sha256": "coverage",
            "replay_eligibility": {"a_class_as_of_replay_eligible": True},
        },
    )


def test_shadow_prediction_is_candidate_only_and_one_day_cannot_claim_performance():
    prediction = build_shadow_prediction(
        _sample(),
        buckets=BUCKETS,
        baseline_likelihood_map={"35°C": 0.05, "36°C": 0.1, "37°C": 0.2, "38°C": 0.5, "39°C or higher": 0.15},
    )
    report = evaluate_shadow_predictions([prediction])

    assert prediction["candidate_only"] is True
    assert prediction["production_applied"] is False
    assert report["event_day_count"] == 1
    assert report["paired_baseline_event_day_count"] == 1
    assert report["candidate_event_day_mean_rps"] is not None
    assert report["performance_conclusion_allowed"] is False
    assert report["performance_conclusion_reason"] == "insufficient_a_class_event_days_no_performance_conclusion"


def test_evaluation_averages_cutoffs_by_event_day_and_reports_premature_cutoff_guardrail():
    probabilities = {"35°C": 0.05, "36°C": 0.1, "37°C": 0.2, "38°C": 0.5, "39°C or higher": 0.15}
    rows = [
        {
            "sample_id": "a",
            "event_date": "2026-07-05",
            "cutoff_as_of": "2026-07-05T07:00:00Z",
            "evaluation_eligible": True,
            "label": {"final_daily_max_label": 38.0},
            "observed_today_max_c_at_cutoff": 37.0,
            "candidate_likelihood_map": probabilities,
            "baseline_likelihood_map": probabilities,
            "candidate_physical_state": {"physical_cutoff_confirmed": True},
            "candidate_remaining_distribution": {"remaining_increment_positive_quantiles_c": {"p90": 1.0}},
        },
        {
            "sample_id": "b",
            "event_date": "2026-07-05",
            "cutoff_as_of": "2026-07-05T08:00:00Z",
            "evaluation_eligible": True,
            "label": {"final_daily_max_label": 38.0},
            "observed_today_max_c_at_cutoff": 37.0,
            "candidate_likelihood_map": probabilities,
            "baseline_likelihood_map": probabilities,
            "candidate_physical_state": {"physical_cutoff_confirmed": False, "evidence_codes": ["recovery_signal"]},
            "candidate_remaining_distribution": {"remaining_increment_positive_quantiles_c": {"p90": 1.0}},
        },
        {"sample_id": "c", "event_date": "2026-07-06", "evaluation_eligible": False},
    ]

    report = evaluate_shadow_predictions(rows)

    assert report["event_day_count"] == 1
    assert len(report["daily_rows"]) == 1
    assert report["daily_rows"][0]["cutoff_count"] == 2
    assert report["premature_cutoff_count"] == 1
    assert report["post_15_tail_case_count"] == 2
    assert report["post_15_tail_capture_count"] == 2
    assert report["recovery_case_count"] == 1
    assert report["recovery_capture_count"] == 1
    assert report["excluded"][0]["reason"] == "sample_not_a_class"
