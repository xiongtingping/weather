from scripts.prepare_operational_bridge_market_release import ROOT, prepare


def test_prepare_dry_run_does_not_touch_active_or_candidate_pointers() -> None:
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
