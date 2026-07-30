#!/usr/bin/env python3
"""Read-only authority audit for the accepted Target Realization v6 successor."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADR6 = ROOT / "docs/adr/ADR-0006-DYNAMIC-THREE-LEG-V6-SUCCESSOR.md"
V6 = ROOT / "docs/contracts/TARGET_REALIZATION_V6.md"
ADR5 = ROOT / "docs/adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md"
V5 = ROOT / "docs/contracts/TARGET_REALIZATION_V5.md"

SYNCED = {
    ROOT / "docs/README.md": ("ADR-0006", "Target Realization v6"),
    ROOT / "docs/constitution/SYSTEM_CHARTER.md": ("Target Realization v6", "generation 2"),
    ROOT / "docs/WEATHER_DECISION_CORE.md": ("Target Realization v6", "capital_scale_factor"),
    ROOT / "docs/contracts/SSOT.md": ("Target Realization v6", "main_yes_position_cycle_id"),
    ROOT / "docs/contracts/RESOLUTION_X_S_B_BRIDGE.md": ("当前 Target Realization v6", "200 USDC"),
    ROOT / "docs/architecture/CURRENT_ARCHITECTURE.md": ("Target Realization v6 generation 2", "唯一 deployed"),
    ROOT / "docs/architecture/TARGET_ARCHITECTURE_V2.md": ("Current Target Realization v6", "capital_scale_factor"),
    ROOT / "docs/architecture/GLOSSARY.md": ("策略 v6（当前）", "main_yes_position_cycle_id"),
    ROOT / "AGENTS.md": ("Target Realization v6", "generation 2"),
}

V6_REQUIRED = (
    "status: accepted",
    "authority: L1",
    "activation_state: production-active-content-addressed-generation-2",
    "successor_to: TARGET_REALIZATION_V5.md",
    "`normal`",
    "`upside_risk`",
    "`hard_locked`",
    "`invalidated`",
    "invalidated > hard_locked > upside_risk > normal",
    "`10%–<20%` | 25%",
    "`20%–<35%` | 50%",
    "`35%–<50%` | 75%",
    "`>=50%` | 100%",
    "min(risk\\_target\\_ratio,time\\_cap\\_ratio)",
    "main_yes_position_cycle_id",
    "连续两条新的 canonical 强观测",
    "`70%–<80%` | 10 USDC",
    "`80%–<90%` | 25 USDC",
    "`90%–<95%` | 50 USDC",
    "`95%–<98%` | 75 USDC",
    "`>=98%` | 100 USDC",
    "允许低于 200",
    "capital_scale_factor",
    "limit_price=0.999 USDC",
    "market_alpha_take_profit_main_yes_999",
    "market_alpha_take_profit_second_yes_999",
    "market_alpha_take_profit_trajectory_no_999",
    "market_alpha_take_profit_hard_exclusion_no_999",
    "target_realization_main_yes_upside_risk_stage_1",
    "target_realization_main_yes_upside_risk_stage_4",
    "target_realization_main_yes_invalidated",
)

ADR6_REQUIRED = (
    "status: accepted",
    "authority: L0",
    "activation_state: production-active-content-addressed-generation-2",
    "Target Realization v6",
    "min(risk_target_ratio,time_cap_ratio)",
    "main_yes_position_cycle_id",
    "capital_scale_factor",
    "generation 2",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def build_report() -> dict[str, object]:
    checks: dict[str, bool] = {}
    blockers: list[dict[str, object]] = []

    required_paths = (ADR6, V6, ADR5, V5, *SYNCED)
    for path in required_paths:
        exists = path.exists()
        checks[f"exists:{path.relative_to(ROOT).as_posix()}"] = exists
        if not exists:
            blockers.append({"code": "required_file_missing", "path": str(path)})
    if blockers:
        return _report(checks, blockers)

    v6_text = _read(V6)
    for token in V6_REQUIRED:
        present = token in v6_text
        checks[f"v6:{token}"] = present
        if not present:
            blockers.append({"code": "v6_contract_token_missing", "token": token})

    adr6_text = _read(ADR6)
    for token in ADR6_REQUIRED:
        present = token in adr6_text
        checks[f"adr6:{token}"] = present
        if not present:
            blockers.append({"code": "adr6_token_missing", "token": token})

    v5_superseded = (
        "status: accepted" in _read(V5)
        and "activation_state: superseded-by-target-realization-v6-no-fallback" in _read(V5)
        and "status: accepted" in _read(ADR5)
        and "activation_state: superseded-by-adr-0006-no-fallback" in _read(ADR5)
    )
    checks["v5_superseded_no_fallback"] = v5_superseded
    if not v5_superseded:
        blockers.append({"code": "v5_superseded_boundary_not_explicit"})

    v6_claims_current = (
        "production-active-content-addressed-generation-2" in v6_text
        and "production-active-content-addressed-generation-2" in adr6_text
        and "当前 production" in v6_text
    )
    checks["v6_generation_2_claims_current"] = v6_claims_current
    if not v6_claims_current:
        blockers.append({"code": "v6_current_boundary_not_explicit"})

    try:
        active_v6 = json.loads((ROOT / "config/trading/target_realization_v6_production_activation.json").read_text())
        active_v5 = json.loads((ROOT / "config/trading/dynamic_three_leg_production_activation.json").read_text())
        active_alpha = json.loads((ROOT / "config/trading/market_alpha_take_profit_production_activation.json").read_text())
        activation_aligned = (
            active_v6.get("state") == "enabled"
            and int(active_v6.get("activation_generation") or 0) >= 2
            and active_v6.get("v5_fallback_forbidden") is True
            and active_v5.get("state") == "disabled_superseded_by_target_realization_v6"
            and active_alpha.get("state") == "disabled_superseded_by_target_realization_v6"
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        activation_aligned = False
    checks["deployment_activation_is_exclusive_v6_generation_2"] = activation_aligned
    if not activation_aligned:
        blockers.append({"code": "deployment_activation_not_exclusive_v6_generation_2"})

    for path, tokens in SYNCED.items():
        text = _read(path)
        rel = path.relative_to(ROOT).as_posix()
        for token in tokens:
            present = token in text
            checks[f"sync:{rel}:{token}"] = present
            if not present:
                blockers.append({"code": "successor_authority_not_synchronized", "path": rel, "token": token})

    agents_line_count = len(_read(ROOT / "AGENTS.md").splitlines())
    checks["agents_md_at_most_100_lines"] = agents_line_count <= 100
    if agents_line_count > 100:
        blockers.append({"code": "agents_md_too_long", "line_count": agents_line_count})

    return _report(checks, blockers)


def _report(checks: dict[str, bool], blockers: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "target_realization_v6_document_contract_audit_v1",
        "ok": not blockers,
        "state": "aligned" if not blockers else "blocked",
        "read_only": True,
        "checks": checks,
        "blockers": blockers,
        "guards": {
            "writes_files": False,
            "writes_archive_db": False,
            "changes_activation": False,
            "places_orders": False,
            "restarts_services": False,
        },
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
