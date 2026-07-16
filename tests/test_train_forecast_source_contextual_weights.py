from scripts.train_forecast_source_contextual_weights import (
    build_training_samples,
    train_contextual_weight_challenger,
)


def _row(day, source, winner_probability, *, lineage="immutable_envelope"):
    return {
        "state": "available",
        "event_id": f"event-{day}",
        "cutoff_id": "c12",
        "event_date_local": f"2026-01-{day:02d}",
        "settlement_at": f"2026-01-{day:02d}T23:00:00Z",
        "lead_hours": 12,
        "context": {"spread_c": 1.0},
        "source_id": source,
        "provider_family": source,
        "winning_bucket": "30",
        "bucket_probabilities": {"29": 1.0 - winner_probability, "30": winner_probability},
        "forecast_snapshot_ids": [f"sha-{day}-{source}"],
        "lineage_quality": lineage,
    }


def test_build_training_samples_deduplicates_event_cutoff_and_excludes_weak_lineage():
    rows = [_row(1, "a", 0.8), _row(1, "b", 0.2), _row(2, "a", 0.8, lineage="weak")]
    samples, manifest = build_training_samples(rows)
    assert len(samples) == 1
    assert set(samples[0]["distributions"]) == {"a", "b"}
    assert manifest["paired_event_day_count"] == 1
    assert manifest["excluded_counts"] == {"lineage_not_immutable": 1}


def test_training_is_blocked_below_minimum_paired_days():
    report = train_contextual_weight_challenger([_row(1, "a", 0.8), _row(1, "b", 0.2)])
    assert report["state"] == "blocked"
    assert report["artifact"] is None
    assert report["release_gate"]["production_promotion_eligible"] is False


def test_training_artifact_hash_is_reproducible():
    rows = []
    for day in range(1, 5):
        rows.extend((_row(day, "a", 0.85), _row(day, "b", 0.15)))
    options = {"max_iterations": 100, "source_cap": 0.9, "family_cap": 0.9}
    first = train_contextual_weight_challenger(
        rows,
        context_fields=["spread_c"],
        minimum_training_event_days=2,
        model_options=options,
    )
    second = train_contextual_weight_challenger(
        list(reversed(rows)),
        context_fields=["spread_c"],
        minimum_training_event_days=2,
        model_options=options,
    )
    assert first["state"] == "trained"
    assert first["artifact"]["artifact_sha256"] == second["artifact"]["artifact_sha256"]
    assert first["artifact"]["production_ready"] is False


def test_multiple_intraday_cutoffs_are_normalized_to_one_weight_per_event_day():
    rows = []
    for source in ("a", "b"):
        first = _row(1, source, 0.8 if source == "a" else 0.2)
        second = dict(first)
        second["cutoff_id"] = "forecast_availability_x"
        second["forecast_snapshot_ids"] = [f"sha-1-late-{source}"]
        rows.extend((first, second, _row(2, source, 0.8 if source == "a" else 0.2)))

    samples, manifest = build_training_samples(rows)

    day_one_weights = [sample["sample_weight"] for sample in samples if sample["event_date_local"] == "2026-01-01"]
    assert day_one_weights == [0.5, 0.5]
    assert manifest["effective_event_day_weight_sum"] == 2.0
