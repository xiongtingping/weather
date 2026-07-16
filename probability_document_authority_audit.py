"""Read-only audit for the unique production v6 probability document authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any


SCHEMA_VERSION = "probability_document_authority_audit_v2_physical_state_bayes_v6"
V6_FORMULA_ID = (
    "p0_weak_conditioned_continuous_y_x_8_state_then_pre_event_controlled_predict_"
    "then_canonical_joint_emission_then_regular_metar_hard_bound_then_reported_x_projection_v1"
)
V6_COMPOSER = "physical_state_bayes_v6_pre_event_predict_update"
V6_CUTOVER = "scripts/cutover_physical_state_bayes_v6_production.sh"
RETIRED_V5_CUTOVER = "scripts/cutover_cumulative_strong_likelihood_v5.sh"
RETIRED_STATE_AWARE_CUTOVER = "scripts/cutover_state_aware_strong_lr_probability_epoch.sh"


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() and not line.startswith((" ", "\t")):
            result[key.strip()] = value.strip()
    return result


def audit_probability_document_authority(root: Path | str) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    paths = {
        "charter": base / "docs/constitution/SYSTEM_CHARTER.md",
        "current_adr": base / "docs/adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md",
        "retired_v5_adr": base / "docs/adr/ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md",
        "retired_probability_adr": base / "docs/adr/ADR-0001-PRODUCTION-PROBABILITY-MODEL-IDENTITY.md",
        "architecture": base / "docs/architecture/TARGET_ARCHITECTURE_V2.md",
        "current_architecture": base / "docs/architecture/CURRENT_ARCHITECTURE.md",
        "active_runbook": base / "docs/runbooks/PHYSICAL_STATE_BAYES_V6_PRODUCTION_CUTOVER.md",
        "retired_v5_runbook": base / "docs/runbooks/REPORTED_X_PROBABILITY_CUTOVER.md",
        "retired_runbook": base / "docs/runbooks/SEQUENTIAL_BAYES_PRODUCTION_CUTOVER.md",
        "retired_v31_runbook": base / "docs/runbooks/V31_ONLY_DEPLOYMENT_ACCEPTANCE.md",
        "execution_schema_runbook": base / "docs/runbooks/DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA_CUTOVER.md",
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    blockers = [f"document_missing:{name}" for name in missing]
    texts = {name: path.read_text(encoding="utf-8") for name, path in paths.items() if path.is_file()}
    metadata = {name: _frontmatter(text) for name, text in texts.items()}

    charter = texts.get("charter", "")
    current_adr = texts.get("current_adr", "")
    retired_v5_adr = texts.get("retired_v5_adr", "")
    architecture = texts.get("architecture", "")
    current_architecture = texts.get("current_architecture", "")
    active_runbook = texts.get("active_runbook", "")
    retired_v5_runbook = texts.get("retired_v5_runbook", "")
    retired_runbook = texts.get("retired_runbook", "")
    retired_v31_runbook = texts.get("retired_v31_runbook", "")
    execution_schema_runbook = texts.get("execution_schema_runbook", "")

    checks = {
        "l0_charter_is_accepted": metadata.get("charter", {}).get("status") == "accepted",
        "l0_charter_records_v6_production_enabled": (
            metadata.get("charter", {}).get("deployment_state")
            == "v6-pre-event-state-space-production-enabled-v5-retired-no-fallback"
            and V6_COMPOSER in charter
            and "v5 composer/writer 已退役且禁止 fallback" in charter
        ),
        "current_probability_adr_is_v6_production_authority": (
            metadata.get("current_adr", {}).get("status") == "accepted"
            and metadata.get("current_adr", {}).get("activation_state")
            == "production-v6-enabled-v5-retired-no-fallback"
            and V6_COMPOSER in current_adr
            and V6_FORMULA_ID in current_architecture
        ),
        "v5_probability_adr_is_superseded": (
            metadata.get("retired_v5_adr", {}).get("status") == "superseded"
            and metadata.get("retired_v5_adr", {}).get("superseded_by")
            == "ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md"
            and "v5 composer、writer、post-transition" in retired_v5_adr
        ),
        "target_architecture_names_v6_authority": (
            metadata.get("architecture", {}).get("activation_state")
            == "production-v6-enabled-v5-retired-market-execution-independent"
            and V6_COMPOSER in architecture
            and "production_v6_active_v5_retired_no_fallback" in architecture
        ),
        "current_architecture_is_v6_accepted": (
            metadata.get("current_architecture", {}).get("status") == "accepted"
            and metadata.get("current_architecture", {}).get("deployment_state")
            == "production-v6-active-v5-retired-no-fallback"
            and V6_COMPOSER in current_architecture
            and V6_FORMULA_ID in current_architecture
        ),
        "current_architecture_does_not_claim_retired_joint_composer": (
            "生产概率身份只有一个：`joint_weak_physical_strong_composer_v1`" not in current_architecture
            and "唯一 composer 是 `joint_weak_physical_strong_composer_v1`" not in current_architecture
        ),
        "active_runbook_is_completed_v6_authority": (
            metadata.get("active_runbook", {}).get("status") == "accepted"
            and metadata.get("active_runbook", {}).get("activation_state")
            == "completed-production-v6-active-v5-retired-no-fallback"
            and V6_CUTOVER in active_runbook
            and "不允许回退 v5" in active_runbook
        ),
        "v5_runbook_is_superseded_and_retired": (
            metadata.get("retired_v5_runbook", {}).get("status") == "superseded"
            and metadata.get("retired_v5_runbook", {}).get("superseded_by")
            == "PHYSICAL_STATE_BAYES_V6_PRODUCTION_CUTOVER.md"
            and "RETIRED; DO NOT RUN" in retired_v5_runbook
        ),
        "older_runbooks_remain_superseded": (
            metadata.get("retired_runbook", {}).get("status") == "superseded"
            and metadata.get("retired_v31_runbook", {}).get("status") == "superseded"
            and "RETIRED; DO NOT RUN" in retired_runbook
            and "RETIRED; DO NOT RUN" in retired_v31_runbook
        ),
        "execution_schema_runbook_decouples_probability": (
            "概率切换与执行账本 schema 切换必须解耦" in execution_schema_runbook
            and "旧合并入口" in execution_schema_runbook
            and "已退役" in execution_schema_runbook
        ),
        "no_retired_cutover_claims_current_authority": (
            f"{RETIRED_V5_CUTOVER}` 是唯一" not in "\n".join(texts.values())
            and f"{RETIRED_STATE_AWARE_CUTOVER}` 是唯一" not in "\n".join(texts.values())
        ),
    }
    blockers.extend(f"authority_check_failed:{name}" for name, ok in checks.items() if not ok)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "aligned" if not blockers else "blocked",
        "blockers": blockers,
        "checks": checks,
        "documents": {name: str(path) for name, path in paths.items()},
        "read_only": True,
        "writes_files": False,
        "writes_database": False,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "V6_COMPOSER",
    "V6_CUTOVER",
    "V6_FORMULA_ID",
    "audit_probability_document_authority",
]
