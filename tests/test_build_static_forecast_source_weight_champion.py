from forecast_source_weight_artifact_registry import validate_artifact
from scripts.build_static_forecast_source_weight_champion import build_static_champion


def test_static_champion_is_valid_equal_weight_fail_closed_artifact():
    rows = []
    for source in ("a", "b", "c"):
        rows.append(
            {
                "state": "available",
                "event_id": "event-1",
                "cutoff_id": "c0",
                "event_date_local": "2026-07-10",
                "settlement_at": "2026-07-10T12:00:00Z",
                "source_id": source,
                "provider_family": source,
                "winning_bucket": "30",
                "bucket_order": ["29", "30"],
                "bucket_probabilities": {"29": 0.4, "30": 0.6},
                "forecast_snapshot_ids": [f"sha-{source}"],
                "lineage_quality": "immutable_envelope",
            }
        )

    artifact = build_static_champion(rows)

    assert validate_artifact(artifact)["ok"] is True
    assert artifact["version"] == "static_champion_research_v1"
    assert all(abs(weight - 1 / 3) < 1e-12 for weight in artifact["global_weights"].values())
    assert artifact["gates"]["promotion_eligible"] is False
