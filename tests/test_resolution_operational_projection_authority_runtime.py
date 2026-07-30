from resolution_operational_projection_authority_runtime import (
    DEFAULT_PATH,
    load_operational_projection_authority,
)


def test_operational_authority_allows_soft_live_projection_without_samples_but_no_hard_no() -> None:
    contract, result = load_operational_projection_authority(DEFAULT_PATH)
    assert contract["source_authority_contract_id"] == "zuuu_amsc_wunderground_daily_high_v4"
    assert result["ok"] is True
    assert result["soft_projection_authorized"] is True
    assert result["sample_or_oos_live_gate"] is False
    assert result["hard_exclusion_authorized"] is False
    assert result["direct_order_authorized"] is False
    assert result["blocks_physical_x"] is False
