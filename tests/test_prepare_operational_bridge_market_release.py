import pytest

from bayes_basket_target.offloaded_paths import is_offloaded
from scripts.prepare_operational_bridge_market_release import ROOT, prepare


def test_prepare_dry_run_does_not_touch_active_or_candidate_pointers() -> None:
    # The registry it reads is data/shadow, which charter 167 moved to the
    # external volume: unplugged, the dry run cannot open it and the test is
    # measuring the disk, not the release script.
    if is_offloaded(ROOT / "data/shadow"):
        pytest.skip("data/shadow is on the external volume (charter 167), not mounted")
    candidate = ROOT / "data/candidates/resolution_bridge_production_activation.json"
    before = candidate.read_bytes() if candidate.exists() else None
    result = prepare(
        event_date="2026-07-16",
        approved_at="2026-07-16T10:24:22Z",
        registry=ROOT / "data/shadow/resolution_final_labels.db",
        apply=False,
    )
    after = candidate.read_bytes() if candidate.exists() else None
    assert result["ok"] is True
    assert result["state"] == "dry_run_ready"
    assert before == after
    assert result["hard_exclusion_certificates_authorized"] is False
    assert result["external_disk_required"] is False
