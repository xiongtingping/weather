from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target.pr05_pr09_model_oos_basis import (
    PR05PR09ModelOOSBasisError,
    build_pr05_pr09_model_oos_basis,
)


ROOT = Path(__file__).resolve().parents[1]
AUDITS = ROOT / "output" / "audits"
INPUTS = {
    "republisher_fixed_lead_path": AUDITS / "pr10_five_model_historical_fixed_lead_canonical_truth_20260805T120000Z_v4.json",
    "republisher_cohort_path": AUDITS / "pr10_five_model_exact_28_cohorts_20260805T120000Z_v1.json",
    "icon_eps_basis_path": AUDITS / "pr07_icon_eps_et_historical_member_basis_20260802_v1.json",
    "native_ensemble_terminal_path": AUDITS / "pr06_pr09_native_ensemble_et_gap_20260728_30_v1.json",
    "aifs_terminal_path": AUDITS / "pr06_aifs_single_historical_terminal_20260805T120000Z_v1.json",
    "coverage_scope_path": AUDITS / "pr05_pr09_v13_model_oos_scope_bundle_20260803T210800Z_v6.json",
    "regional_retained_terminal_path": AUDITS / "pr05_pr09_regional_retained_terminal_20260805T120000Z_v1.json",
    "icon_legacy_terminal_path": AUDITS / "pr05_pr09_icon_legacy_terminal_20260805T120000Z_v1.json",
}
AS_OF = "2026-08-05T12:00:00Z"

# ``output/audits/`` is gitignored (.gitignore:38), so these inputs exist
# only on a machine that has run the audits that produce them.  Without the
# skip this file is red on every clean checkout and on CI, and a permanent
# red is a red nobody reads -- it sat inside the ten "baseline failures" this
# week's work kept comparing against, and got counted as a known quantity
# rather than as a missing input.  Skipping on the specific missing file
# keeps the test real wherever the artefacts do exist.
_MISSING = sorted(
    path.name for path in INPUTS.values() if not path.exists()
)
requires_audit_inputs = pytest.mark.skipif(
    bool(_MISSING),
    reason="gitignored audit artefacts absent: %s" % ", ".join(_MISSING),
)


def _build(**overrides: Path) -> dict[str, object]:
    return build_pr05_pr09_model_oos_basis(
        as_of_utc=AS_OF, **{**INPUTS, **overrides}
    )


@requires_audit_inputs
def test_real_domain_inputs_materialize_only_the_unpublished_gap_ledger() -> None:
    result = _build()

    assert result["state"] == "MATERIALIZED_UNPUBLISHED"
    assert result["release_state"] == "BLOCKED"
    assert result["consumable"] is False
    assert result["writes_database"] is False
    assert result["publishes_pr04_pointer"] is False
    assert result["starts_services"] is False
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0
    assert result["canonical_model_oos_event_count"] == 6000
    assert result["canonical_model_oos_cohort_count"] == 20
    assert result["oos_ready_scope_count"] == 12
    assert result["historical_member_fact_count"] == 18
    assert result["scope_count"] == 2565
    assert result["unscoped_terminal_count"] == 462

    scopes = list(result["scopes"])
    assert {item["scope_kind"] for item in scopes} == {
        "CANONICAL_MODEL_OOS_SCOPE",
        "NATIVE_ENSEMBLE_MEMBER_HISTORY",
    }
    assert sum(item["state"] == "OOS_READY" for item in scopes) == 12
    assert all(item["consumable"] is False for item in scopes)
    canonical_scopes = [
        item for item in scopes if item["scope_kind"] == "CANONICAL_MODEL_OOS_SCOPE"
    ]
    assert any(item["canonical_model_oos_event_count"] for item in canonical_scopes)
    assert sum(
        item["evidence_roles"] == ["CANONICAL_EVENT", "EXPECTED_SCOPE", "TERMINAL_RECEIPT"]
        for item in canonical_scopes
    ) == 250
    assert sum(item["cohort_evaluation_state"] == "OOS_REJECTED" for item in canonical_scopes) == 8
    retained_regional = [
        item for item in canonical_scopes
        if item["scope_identity"]["model_family"] == "regional"
        and item["scope_identity"]["lead_bucket"] == "d1"
        and any(
            receipt["diagnostic"].startswith("retained_raw_exists")
            for receipt in item["upstream_terminal_receipts"]
        )
    ]
    assert len(retained_regional) == 44
    assert all(item["blockers"] == ["LINEAGE_MISMATCH"] for item in retained_regional)
    icon_legacy = [
        item for item in canonical_scopes
        if item["scope_identity"]["model_id"] == "ICON"
        and item["scope_identity"]["lead_bucket"] == "d0"
        and any(
            receipt["diagnostic"].startswith("legacy_retroactive")
            for receipt in item["upstream_terminal_receipts"]
        )
    ]
    assert len(icon_legacy) == 44
    assert all(item["blockers"] == ["LINEAGE_MISMATCH"] for item in icon_legacy)
    assert all(
        item["blockers"] == ["DATA_MISSING"]
        for item in canonical_scopes
        if not item["canonical_model_oos_event_count"]
        and item not in retained_regional
        and item not in icon_legacy
        and not any(
            blocker in {"LINEAGE_MISMATCH", "TIMEZONE_MISMATCH"}
            for blocker in item["blockers"]
        )
    )
    assert all(
        item["source_artifact_content_sha256"]
        for item in result["unscoped_terminal_receipts"]
    )
    assert sum(item["historical_member_fact_count"] for item in scopes) == 18
    assert all(
        item["canonical_model_oos_event_count"] == 0
        and item["cohort_eligible_event_count"] == 0
        for item in scopes
        if "MEMBER_FACT" in item["evidence_roles"]
    )


@requires_audit_inputs
def test_content_addressed_source_tampering_fails_closed(tmp_path: Path) -> None:
    source = INPUTS["coverage_scope_path"]
    tampered = json.loads(source.read_text(encoding="utf-8"))
    tampered["terminal_scopes"][0]["blocker"] = "OTHER"
    target = tmp_path / "coverage-tampered.json"
    target.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(
        PR05PR09ModelOOSBasisError,
        match="coverage_scopes_content_sha256_mismatch",
    ):
        _build(coverage_scope_path=target)


@requires_audit_inputs
def test_stale_product_registry_receipts_fail_closed() -> None:
    with pytest.raises(
        PR05PR09ModelOOSBasisError,
        match="coverage_product_registry_mismatch",
    ):
        _build(
            coverage_scope_path=(
                AUDITS
                / "pr05_pr09_v13_model_oos_scope_bundle_20260805T120000Z_v5.json"
            )
        )
