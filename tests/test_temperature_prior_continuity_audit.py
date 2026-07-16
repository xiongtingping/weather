from __future__ import annotations

from scripts.temperature_prior_continuity_audit import audit_payload


def _payload(*, noncontiguous: bool = True, effective_sample_count: int = 11) -> dict:
    labels = [
        "31°C or below",
        "32°C",
        "33°C",
        "34°C",
        "35°C",
        "36°C",
        "37°C",
        "38°C",
        "39°C",
        "40°C",
        "41°C or higher",
    ]
    if noncontiguous:
        posterior = {
            "34°C": 0.236,
            "35°C": 0.140,
            "36°C": 0.199,
            "37°C": 0.251,
            "38°C": 0.143,
        }
        prior = {label: 0.030303030303 for label in labels}
        prior["31°C or below"] = 0.393939393939
        prior["32°C"] = 0.151515151515
        prior["33°C"] = 0.090909090909
        prior["34°C"] = 0.151515151515
    else:
        posterior = {
            "35°C": 0.22,
            "36°C": 0.28,
            "37°C": 0.25,
            "38°C": 0.12,
        }
        prior = {label: 1.0 / len(labels) for label in labels}

    rows = []
    for index, label in enumerate(labels):
        rows.append(
            {
                "bucket": label,
                "probability": posterior.get(label, 0.001),
                "prior": prior.get(label, 0.0),
                "forecast_weak_likelihood": 0.1,
                "metar_strong_likelihood": 0.1,
                "hard_mask": 0.0 if index < 3 else 1.0,
            }
        )
    return {
        "fact_id": 107070,
        "slug": "highest-temperature-in-chengdu-on-july-9-2026",
        "event_date": "2026-07-09",
        "distribution": rows,
        "prior_distribution": prior,
        "layer_contributions": {
            "forecast_weak_likelihood": {
                "beta": 0.4,
                "distribution": {label: 0.1 for label in labels},
            },
            "metar_strong_likelihood": {
                "beta": 0.65,
                "distribution": {label: 0.1 for label in labels},
            },
        },
        "prior_meta": {
            "sample_count": 33,
            "raw_candidate_count": 33,
            "effective_sample_count": effective_sample_count,
            "date_window_sample_count": effective_sample_count,
            "date_window_weight": 1.0,
            "smoothing": 0.5,
            "sample_rows": [
                {"event_date": "2026-07-04", "source_table": "event_runs"},
                {"event_date": "2026-07-03", "source_table": "event_runs"},
            ],
        },
    }


def test_temperature_prior_continuity_audit_flags_noncontiguous_prior_spike_and_low_sample() -> None:
    result = audit_payload(_payload(), top_n=3, min_effective_samples=30)

    assert result["ok"] is False
    assert result["top_continuity"]["state"] == "noncontiguous"
    assert result["top_continuity"]["top_buckets"] == ["37°C", "34°C", "36°C"]
    assert result["top_continuity"]["missing_buckets_between_top_range"][0]["bucket"] == "35°C"
    assert set(result["blockers"]) == {
        "posterior_top_buckets_noncontiguous",
        "posterior_top_buckets_noncontiguous_prior_spike",
        "prior_effective_sample_count_below_audit_min",
    }
    assert result["sample_coverage"]["state"] == "low_effective_sample_count"
    assert result["sample_coverage"]["sample_rows_source_table_counts"] == {"event_runs": 2}
    assert any(
        evidence["top_bucket"] == "34°C"
        and evidence["missing_bucket"] == "35°C"
        and evidence["prior_ratio"] >= 5.0
        for evidence in result["prior_spike_evidence"]
    )


def test_temperature_prior_continuity_audit_accepts_contiguous_top_with_enough_samples() -> None:
    result = audit_payload(
        _payload(noncontiguous=False, effective_sample_count=42),
        top_n=3,
        min_effective_samples=30,
    )

    assert result["ok"] is True
    assert result["blockers"] == []
    assert result["top_continuity"]["state"] == "contiguous"
    assert result["sample_coverage"]["state"] == "ok"
