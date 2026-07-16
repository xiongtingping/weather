from scripts.build_official_forecast_source_accuracy_report import build_official_forecast_source_accuracy_report


def _event(*, cutoff: str, peak: float, truth: float) -> dict:
    return {
        "event_date": "2026-07-13",
        "decision_as_of": cutoff,
        "physical_x_final": {"temperature_c": truth},
        "source_revisions": [
            {
                "source_id": "gfs",
                "available_at": "2026-07-12T15:00:00Z",
                "forecast_peak_c": peak,
                "strict_asof_tier_a": True,
                "response_headers_observed": True,
                "source_revision_id": "revision-1",
            }
        ],
    }


def test_scores_only_strict_tier_a_against_physical_x_and_separates_d1_d0():
    report = build_official_forecast_source_accuracy_report(
        [
            _event(cutoff="2026-07-12T16:00:00Z", peak=35.0, truth=34.0),
            _event(cutoff="2026-07-13T01:00:00Z", peak=33.0, truth=34.0),
        ]
    )

    rows = {row["horizon"]: row for row in report["rows"]}
    assert report["label"] == "closed_amsc_physical_x_final"
    assert rows["D1"]["physical_x_mae_c"] == 1.0
    assert rows["D1"]["mean_bias_c"] == 1.0
    assert rows["D0"]["mean_bias_c"] == -1.0
    assert rows["D0"]["ranking_state"] == "insufficient_independent_event_days"


def test_excludes_non_tier_a_revision():
    event = _event(cutoff="2026-07-12T16:00:00Z", peak=35.0, truth=34.0)
    event["source_revisions"][0]["response_headers_observed"] = False
    report = build_official_forecast_source_accuracy_report([event])

    assert report["state"] == "pending_formal_samples"
    assert report["coverage"]["excluded_counts"] == {"revision_not_strict_tier_a_asof": 1}
