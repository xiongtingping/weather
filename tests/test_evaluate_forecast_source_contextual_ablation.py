from scripts.evaluate_forecast_source_contextual_ablation import evaluate_contextual_ablation


def _rows():
    rows = []
    for day in range(1, 13):
        for source, probability in (("a", 0.99), ("b", 0.4), ("c", 0.4)):
            rows.append(
                {
                    "state": "available",
                    "event_id": f"event-{day}",
                    "cutoff_id": "c12",
                    "event_date_local": f"2026-01-{day:02d}",
                    "lead_hours": 12,
                    "context": {"spread_c": 1.0},
                    "source_id": source,
                    "provider_family": source,
                    "winning_bucket": "30",
                    "bucket_order": ["29", "30"],
                    "bucket_probabilities": {"29": 1.0 - probability, "30": probability},
                    "forecast_snapshot_ids": [f"sha-{day}-{source}"],
                    "lineage_quality": "immutable_envelope",
                }
            )
    return rows


def test_ablation_does_not_keep_weather_layer_when_it_adds_no_oos_gain():
    report = evaluate_contextual_ablation(
        _rows(),
        context_fields=["spread_c"],
        minimum_train_event_days=4,
        fold_event_days=2,
        bootstrap_replicates=50,
        model_iterations=100,
    )
    assert report["layers"]["global"]["retained"] is True
    assert report["layers"]["seasonal_lead"]["incremental_vs"] == "global"
    assert report["layers"]["weather_context"]["incremental_vs"] == "seasonal_lead"
    assert report["layers"]["weather_context"]["retained"] is False
    assert report["selected_specification"] in {"global", "seasonal_lead"}
    assert report["production_apply_allowed"] is False
