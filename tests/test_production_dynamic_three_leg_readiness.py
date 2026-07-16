from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readiness_is_read_only_and_separates_default_off_from_live_activation() -> None:
    path = ROOT / "scripts" / "production_dynamic_three_leg_readiness.py"
    text = path.read_text(encoding="utf-8")
    ast.parse(text)
    assert '"read_only": True' in text
    assert '"places_orders": False' in text
    assert '"changes_activation": False' in text
    assert "default_off_blockers" in text
    assert "live_activation_blockers" in text
    assert "training_samples_block_live" in text
    assert "strict_asof_oos_blocks_live" in text
    assert "production_gateway_attested_t1_to_t5_evidence_missing" in text
    assert "market_bucket_projection_unavailable" in text
    assert "audit_probability_runtime" in text
    assert "production_probability_transaction_b_cutover_readiness" not in text
    assert "production_probability_v6_live_acceptance" in text
    assert "posterior_checkpoint_v4_physical_state_bayes_v6" in text
    assert "physical_state_bayes_v6_production" in text
    assert "sequential_bayes_v3_reported_x_production" not in text
    assert "INSERT " not in text
    assert "UPDATE " not in text
    assert "DELETE " not in text
