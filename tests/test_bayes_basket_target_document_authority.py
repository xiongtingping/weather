from __future__ import annotations

import json
from pathlib import Path

from scripts.bayes_basket_target_document_authority_audit import (
    _machine_stage_contract,
    build_audit,
)


def test_target_doctrine_has_exactly_one_no_live_authority() -> None:
    result = build_audit(
        Path(__file__).resolve().parents[1]
        / "config/bayes_basket_target/document_authority_v1.json"
    )

    assert result["status"] == "PASS"
    assert result["current_authority_count"] == 1
    assert result["checks"]["authority_pair_consistent"] is True
    assert result["checks"]["roadmap_consistent"] is True
    assert result["boundary"]["reads_shared_live_db"] is False
    assert len(result["manifest_sha256"]) == 64
    assert len(result["documents"][0]["markdown_sha256"]) == 64
    assert len(result["documents"][0]["json_sha256"]) == 64
    assert len(result["roadmap"]["markdown_sha256"]) == 64
    assert len(result["roadmap"]["json_sha256"]) == 64
    checks = result["documents"][0]["checks"]
    assert checks["machine_probability_stages_exact"] is True
    assert checks["machine_forbids_p0f"] is True
    assert checks["machine_p0_is_nwp_model_stage"] is True
    assert checks["machine_p0m_is_atomic_taf_physical_confirmation"] is True
    assert checks["machine_p1_is_metar_speci_stage"] is True


def test_machine_authority_rejects_old_stage_and_taf_physical_drift() -> None:
    root = Path(__file__).resolve().parents[1]
    machine = json.loads(
        (
            root
            / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/"
            "doctrine/bayes_basket_target_architecture_v1.json"
        ).read_text(encoding="utf-8")
    )
    old_stage = json.loads(json.dumps(machine))
    old_stage["probability_stages"][1]["stage"] = "P0-F^r"
    old_stage_checks = _machine_stage_contract(old_stage)
    assert old_stage_checks["machine_probability_stages_exact"] is False
    assert old_stage_checks["machine_forbids_p0f"] is False

    no_physical_confirmation = json.loads(json.dumps(machine))
    no_physical_confirmation["probability_stages"][1]["evidence"] = (
        "TAF TX revision only"
    )
    physical_checks = _machine_stage_contract(no_physical_confirmation)
    assert (
        physical_checks[
            "machine_p0m_is_atomic_taf_physical_confirmation"
        ]
        is False
    )
