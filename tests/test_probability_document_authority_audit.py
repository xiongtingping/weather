from pathlib import Path

from probability_document_authority_audit import audit_probability_document_authority


ROOT = Path(__file__).resolve().parents[1]


def test_repository_probability_document_authority_is_aligned() -> None:
    result = audit_probability_document_authority(ROOT)

    assert result["ok"] is True
    assert result["state"] == "aligned"
    assert result["blockers"] == []
    assert all(result["checks"].values())
    assert result["checks"]["l0_charter_records_v6_production_enabled"] is True
    assert result["read_only"] is True
    assert result["places_orders"] is False


def test_old_runbook_cannot_silently_regain_authority(tmp_path: Path) -> None:
    for relative in (
        "docs/constitution/SYSTEM_CHARTER.md",
        "docs/adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md",
        "docs/adr/ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md",
        "docs/adr/ADR-0001-PRODUCTION-PROBABILITY-MODEL-IDENTITY.md",
        "docs/architecture/TARGET_ARCHITECTURE_V2.md",
        "docs/architecture/CURRENT_ARCHITECTURE.md",
        "docs/contracts/STRONG_LR_FIT_CONTRACT.md",
        "docs/runbooks/REPORTED_X_PROBABILITY_CUTOVER.md",
        "docs/runbooks/PHYSICAL_STATE_BAYES_V6_PRODUCTION_CUTOVER.md",
        "docs/runbooks/SEQUENTIAL_BAYES_PRODUCTION_CUTOVER.md",
        "docs/runbooks/V31_ONLY_DEPLOYMENT_ACCEPTANCE.md",
        "docs/runbooks/DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA_CUTOVER.md",
    ):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    old = tmp_path / "docs/runbooks/REPORTED_X_PROBABILITY_CUTOVER.md"
    text = old.read_text(encoding="utf-8").replace("status: superseded", "status: accepted", 1)
    old.write_text(text, encoding="utf-8")

    result = audit_probability_document_authority(tmp_path)
    assert result["ok"] is False
    assert "authority_check_failed:v5_runbook_is_superseded_and_retired" in result["blockers"]


def test_current_architecture_cannot_reclaim_retired_joint_composer(tmp_path: Path) -> None:
    for relative in (
        "docs/constitution/SYSTEM_CHARTER.md",
        "docs/adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md",
        "docs/adr/ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md",
        "docs/adr/ADR-0001-PRODUCTION-PROBABILITY-MODEL-IDENTITY.md",
        "docs/architecture/TARGET_ARCHITECTURE_V2.md",
        "docs/architecture/CURRENT_ARCHITECTURE.md",
        "docs/contracts/STRONG_LR_FIT_CONTRACT.md",
        "docs/runbooks/REPORTED_X_PROBABILITY_CUTOVER.md",
        "docs/runbooks/PHYSICAL_STATE_BAYES_V6_PRODUCTION_CUTOVER.md",
        "docs/runbooks/SEQUENTIAL_BAYES_PRODUCTION_CUTOVER.md",
        "docs/runbooks/V31_ONLY_DEPLOYMENT_ACCEPTANCE.md",
        "docs/runbooks/DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA_CUTOVER.md",
    ):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    current = tmp_path / "docs/architecture/CURRENT_ARCHITECTURE.md"
    current.write_text(
        current.read_text(encoding="utf-8")
        + "\n生产概率身份只有一个：`joint_weak_physical_strong_composer_v1`。\n",
        encoding="utf-8",
    )

    result = audit_probability_document_authority(tmp_path)
    assert result["ok"] is False
    assert (
        "authority_check_failed:current_architecture_does_not_claim_retired_joint_composer"
        in result["blockers"]
    )
