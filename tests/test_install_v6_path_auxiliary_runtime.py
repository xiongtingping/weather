from pathlib import Path

from scripts.install_v6_path_auxiliary_runtime import transform


ROOT = Path(__file__).resolve().parents[1]


def test_transform_is_exact_and_idempotent() -> None:
    source = (ROOT / "production_probability_v6_canonical_event_drain_runtime.py").read_text()
    candidate = transform(source)

    assert "enrich_v6_candidate_with_post_event_auxiliary" in candidate
    assert "inject_v6_peak_timing_into_public_fact" in candidate
    assert 'peak_timing = _parse(snapshot.get("forecast_peak_timing_prior_json"), {})' in candidate
    assert "canonical_events_through_current=[*history, event]" in candidate
    assert 'if isinstance(weak.get("forecast_peak_timing_prior"), Mapping)' in candidate
    assert transform(candidate) == candidate
