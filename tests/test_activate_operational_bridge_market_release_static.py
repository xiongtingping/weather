from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_activation_has_safe_midpoint_and_single_exposure_linearization() -> None:
    source = (ROOT / "scripts/activate_operational_bridge_market_release.py").read_text()
    bridge_write = source.index("_write_bytes(active_bridge")
    context_write = source.index("_write_bytes(active_context")
    assert bridge_write < context_write
    assert "old\n+        # disabled/stale context can only select physical-only" not in source
    assert '"linearization_point": "market_context_activation_replace"' in source
    assert "previous_bridge" in source and "previous_context" in source
    assert "production_market_projection_ready" in source
    assert '"hard_exclusion_certificates_authorized": False' in source
    assert '"external_disk_required": False' in source
