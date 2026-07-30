import json
from pathlib import Path

from operational_bridge_release_runtime import (
    build_daily_market_context_release,
    build_operational_bridge_release,
)
from tests.test_resolution_target_contract_runtime import _proposal


ROOT = Path(__file__).resolve().parents[1]


def test_release_binds_two_authorities_and_keeps_hard_no_disabled() -> None:
    source = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
    release = build_operational_bridge_release(
        source_authority_contract=source,
        approved_at="2026-07-16T10:24:22Z",
    )
    artifact = release["production_artifact"]
    assert release["ok"] is True
    assert artifact["operational_projection_authority_contract_id"] == "zuuu_x_s_soft_projection_operational_v1"
    assert artifact["coverage"]["accepted_pair_count"] == 0
    assert artifact["operational_baseline"]["hard_exclusion_certificates_authorized"] is False
    assert release["production_activation"]["activation_sha256"]
    assert release["places_orders"] is False


def test_daily_context_is_content_addressed_and_bound_to_bridge() -> None:
    source = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
    bridge = build_operational_bridge_release(
        source_authority_contract=source,
        approved_at="2026-07-16T10:24:22Z",
    )["production_artifact"]
    target = _proposal()
    release = build_daily_market_context_release(
        target_contract=target, bridge_artifact=bridge
    )
    assert release["context"]["bridge_artifact_sha256"] == bridge["artifact_sha256"]
    assert release["activation"]["market_context_sha256"] == release["context"]["market_context_sha256"]
    assert release["activation"]["blocks_physical_x"] is False
