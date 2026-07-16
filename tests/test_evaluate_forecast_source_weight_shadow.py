from scripts.evaluate_forecast_source_weight_shadow import evaluate_contextual_weight_oos


def _rows(day_count=12, cutoff_ids=("c12",), *, shared_family=False):
    rows = []
    for day in range(1, day_count + 1):
        for cutoff in cutoff_ids:
            for source, probability in (("a", 0.9), ("b", 0.1)):
                rows.append(
                    {
                        "state": "available",
                        "event_id": f"event-{day}",
                        "cutoff_id": cutoff,
                        "event_date_local": f"2026-01-{day:02d}",
                        "settlement_at": f"2026-01-{day:02d}T23:00:00Z",
                        "lead_hours": 12.0,
                        "context": {"spread_c": 1.0, "segments": ["dry"]},
                        "source_id": source,
                        "provider_family": "shared" if shared_family else source,
                        "winning_bucket": "30",
                        "winning_bucket_edge": False,
                        "bucket_order": ["29", "30"],
                        "bucket_probabilities": {"29": 1.0 - probability, "30": probability},
                        "forecast_snapshot_ids": [f"sha-{day}-{cutoff}-{source}"],
                        "lineage_quality": "immutable_envelope",
                    }
                )
    return rows


def test_oos_gate_uses_only_prior_event_days_and_improves_both_scores():
    report = evaluate_contextual_weight_oos(
        _rows(),
        context_fields=["spread_c"],
        minimum_train_event_days=4,
        fold_event_days=2,
        bootstrap_replicates=200,
        production_minimum_event_days=8,
        segment_minimum_ess=50,
        model_options={"max_iterations": 150, "source_cap": 0.9, "family_cap": 0.9},
    )
    assert report["release_gate"]["strict_oos_evaluated"] is True
    assert all(fold["train_through_event_date"] < fold["test_from_event_date"] for fold in report["folds"])
    assert report["release_gate"]["nll_improved"] is True
    assert report["release_gate"]["rps_improved"] is True
    assert report["release_gate"]["production_promotion_proposal_eligible"] is True
    assert report["release_gate"]["curve_production_promotion_proposal_eligible"] is False
    assert report["release_gate"]["production_apply_allowed"] is False


def test_paired_sample_threshold_counts_event_days_not_cutoffs():
    report = evaluate_contextual_weight_oos(
        _rows(day_count=5, cutoff_ids=("c06", "c12")),
        minimum_train_event_days=2,
        fold_event_days=1,
        bootstrap_replicates=20,
        production_minimum_event_days=8,
        segment_minimum_ess=50,
        model_options={"max_iterations": 50, "source_cap": 0.9, "family_cap": 0.9},
    )
    assert report["training_manifest"]["paired_event_day_count"] == 5
    assert report["oos_prediction_count"] == 6
    assert report["release_gate"]["production_promotion_proposal_eligible"] is False
    assert "paired_event_days_below_production_proposal_minimum" in report["release_gate"]["blockers"]


def test_weak_lineage_never_enters_release_scoring():
    rows = _rows()
    for row in rows[:2]:
        row["lineage_quality"] = "weak_or_mixed_lineage"
    report = evaluate_contextual_weight_oos(
        rows,
        minimum_train_event_days=4,
        fold_event_days=4,
        bootstrap_replicates=20,
        model_options={"max_iterations": 50, "source_cap": 0.9, "family_cap": 0.9},
    )
    assert report["training_manifest"]["excluded_counts"]["lineage_not_immutable"] == 2


def test_insufficient_history_returns_explicit_blocker_without_nan_or_crash():
    report = evaluate_contextual_weight_oos(_rows(day_count=3), minimum_train_event_days=30)
    assert report["oos_prediction_count"] == 0
    assert report["paired_deltas"]["nll"]["champion"] is None
    assert "strict_oos_predictions_missing" in report["release_gate"]["blockers"]


def test_curve_lane_is_incremental_to_contextual_and_preserves_separate_gate():
    curve_inputs = []
    for day in range(1, 13):
        observations = [
            {"valid_time": f"2026-01-{day:02d}T01:00:00Z", "as_of_eligible_at": f"2026-01-{day:02d}T01:00:00Z", "arrival_time_quality": "observed_first_seen", "temperature_c": 20.0},
            {"valid_time": f"2026-01-{day:02d}T02:00:00Z", "as_of_eligible_at": f"2026-01-{day:02d}T02:00:00Z", "arrival_time_quality": "observed_first_seen", "temperature_c": 21.0},
        ]
        forecasts = []
        for hour, observed in ((1, 20.0), (2, 21.0)):
            valid = f"2026-01-{day:02d}T{hour:02d}:00:00Z"
            forecasts.extend(
                [
                    {"source_id": "a", "valid_time": valid, "issued_at": f"2026-01-{day:02d}T00:00:00Z", "temperature_c": observed},
                    {"source_id": "b", "valid_time": valid, "issued_at": f"2026-01-{day:02d}T00:00:00Z", "temperature_c": observed + 5.0},
                ]
            )
        curve_inputs.append({"event_id": f"event-{day}", "cutoff_id": "c12", "curve_cutoff_at": f"2026-01-{day:02d}T02:00:00Z", "observations": observations, "forecasts": forecasts})
    report = evaluate_contextual_weight_oos(
        _rows(shared_family=True),
        curve_inputs=curve_inputs,
        minimum_train_event_days=4,
        fold_event_days=2,
        bootstrap_replicates=100,
        production_minimum_event_days=8,
        shadow_minimum_event_days=4,
        segment_minimum_ess=50,
        model_options={"max_iterations": 100, "source_cap": 0.9, "family_cap": 0.9},
    )
    assert report["curve_prediction_count"] == 8
    assert report["release_gate"]["curve_incremental_statistical_gate_passed"] is True
    assert report["release_gate"]["curve_production_promotion_proposal_eligible"] is True
    assert report["guards"]["amsc_curve_reliability_consumed_once"] is True


def test_curve_shadow_filters_observations_after_declared_curve_cutoff():
    day = 5
    curve_inputs = [
        {
            "event_id": f"event-{day}",
            "cutoff_id": "c12",
            "curve_cutoff_at": f"2026-01-{day:02d}T01:00:00Z",
            "observations": [
                {"valid_time": f"2026-01-{day:02d}T01:00:00Z", "as_of_eligible_at": f"2026-01-{day:02d}T01:00:00Z", "arrival_time_quality": "observed_first_seen", "temperature_c": 20.0},
                {"valid_time": f"2026-01-{day:02d}T02:00:00Z", "as_of_eligible_at": f"2026-01-{day:02d}T02:00:00Z", "arrival_time_quality": "observed_first_seen", "temperature_c": 99.0},
            ],
            "forecasts": [
                {"source_id": source, "valid_time": f"2026-01-{day:02d}T01:00:00Z", "issued_at": f"2026-01-{day:02d}T00:00:00Z", "temperature_c": value}
                for source, value in (("a", 20.0), ("b", 25.0))
            ],
        }
    ]
    report = evaluate_contextual_weight_oos(
        _rows(day_count=6),
        curve_inputs=curve_inputs,
        minimum_train_event_days=4,
        fold_event_days=1,
        bootstrap_replicates=10,
        shadow_minimum_event_days=1,
        production_minimum_event_days=1,
        segment_minimum_ess=50,
        model_options={"max_iterations": 20, "source_cap": 0.9, "family_cap": 0.9},
    )

    assert report["curve_prediction_count"] == 1
    assert report["metrics"]["challenger_contextual_curve"]["sample_count"] == 1
