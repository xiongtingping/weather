from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_preparation_is_candidate_only_and_freezes_required_authorities() -> None:
    text = (ROOT / "scripts/prepare_dynamic_three_leg_production_release.py").read_text(encoding="utf-8")
    ast.parse(text)
    assert "required_probability_activation_contract_sha256" in text
    assert "required_bridge_activation_sha256" in text
    assert "required_market_context_activation_sha256" in text
    assert "data/candidates/dynamic_three_leg_production_activation.json" in text
    assert '"writes_archive_db": False' in text
    assert '"places_orders": False' in text
    assert "external_disk_required" in text
    assert "post_order" not in text
