from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cutover_accepts_only_content_addressed_expected_v6_drift() -> None:
    cutover = (ROOT / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh").read_text()
    predecessor = (ROOT / "scripts/production_probability_v6_predecessor_acceptance.py").read_text()
    assert "production_probability_v6_predecessor_acceptance.py" in cutover
    assert '"active_v6_weak_source_snapshot_stale"' in predecessor
    assert '"active_v6_source_time_freshness_invalid"' in predecessor
    assert 'pending_canonical_event_count' in predecessor
    assert 'stale_newer_revision_not_consumed' in predecessor
    assert 'temporal_violations' in predecessor
    assert "prepared.get(\"ok\") is True" in predecessor
    assert '"v5_fallback": False' in predecessor
