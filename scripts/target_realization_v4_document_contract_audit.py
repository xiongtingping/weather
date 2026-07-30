#!/usr/bin/env python3
"""Read-only audit proving Target Realization v4 is retired and non-authoritative."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V4 = ROOT / "docs/contracts/TARGET_REALIZATION_V4.md"
ADR4 = ROOT / "docs/adr/ADR-0004-DYNAMIC-THREE-LEG-V4-POSTERIOR-NO-STANDING-EXIT.md"
V5 = ROOT / "docs/contracts/TARGET_REALIZATION_V5.md"
ADR5 = ROOT / "docs/adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md"


def main() -> int:
    checks: dict[str, bool] = {}
    blockers: list[dict[str, str]] = []
    paths = (V4, ADR4, V5, ADR5)
    for path in paths:
        exists = path.exists()
        checks[f"exists:{path.relative_to(ROOT).as_posix()}"] = exists
        if not exists:
            blockers.append({"code": "required_file_missing", "path": str(path)})

    if not blockers:
        v4 = V4.read_text(encoding="utf-8")
        adr4 = ADR4.read_text(encoding="utf-8")
        v5 = V5.read_text(encoding="utf-8")
        adr5 = ADR5.read_text(encoding="utf-8")
        retired_checks = {
            "v4_contract_superseded": "status: superseded" in v4,
            "v4_contract_never_activated": "activation_state: never-activated-retired" in v4,
            "v4_contract_points_to_v5": "superseded_by: TARGET_REALIZATION_V5.md" in v4,
            "adr4_superseded": "status: superseded" in adr4,
            "adr4_never_activated": "activation_state: never-activated-retired" in adr4,
            "adr4_points_to_adr5": "superseded_by: ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md" in adr4,
            "v5_contract_is_current": "activation_state: production-active-content-addressed" in v5,
            "adr5_is_current": "activation_state: production-active-content-addressed" in adr5,
        }
        checks.update(retired_checks)
        for name, ok in retired_checks.items():
            if not ok:
                blockers.append({"code": "v4_retirement_boundary_missing", "check": name})

    report = {
        "schema": "target_realization_v4_retirement_audit_v1",
        "ok": not blockers,
        "state": "retired_non_authoritative" if not blockers else "blocked",
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
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
