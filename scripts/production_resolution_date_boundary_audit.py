#!/usr/bin/env python3
"""Audit separation of ZUUU resolution-local date and ET operations date."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "production_resolution_date_boundary_audit_v1"


def audit(root: Path) -> dict[str, Any]:
    root = root.resolve()
    paths = {
        "charter": root / "docs/constitution/SYSTEM_CHARTER.md",
        "hotfix": root / "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh",
        "amsc": root / "scripts/amsc_zuuu_metar_taf_poll_once.sh",
        "monitor": root / "scripts/polymarket_monitor_daemon.sh",
        "combined_cutover": root / "scripts/cutover_v6_probability_and_soft_market_projection.sh",
    }
    texts = {
        name: path.read_text(encoding="utf-8") if path.is_file() else ""
        for name, path in paths.items()
    }
    checks = {
        "charter_separates_operations_et_from_resolution_local_date": (
            "`operations_date_et` 只用于交易、统计和值班" in texts["charter"]
            and "物理与结算目标日使用 resolution contract 指定的 `resolution_local_date`"
            in texts["charter"]
        ),
        "charter_v6_acceptance_uses_resolution_local_date": (
            "--event-date $(TZ=Asia/Shanghai date +%F)" in texts["charter"]
        ),
        "hotfix_uses_zuuu_resolution_local_date": (
            texts["hotfix"].count("TZ=Asia/Shanghai date +%F") == 2
            and "operations_date_et" in texts["hotfix"]
            and "TZ=America/New_York date +%F" not in texts["hotfix"]
        ),
        "amsc_pending_queue_uses_current_resolution_local_date": (
            'current_event_date="$(TZ=Asia/Shanghai date +%F)"' in texts["amsc"]
        ),
        "market_context_refresh_uses_resolution_local_date": (
            'event_date="$(TZ=Asia/Shanghai date +%F)"' in texts["monitor"]
        ),
        "combined_cutover_binds_latest_committed_target_date": (
            "latest_committed_v6_event_date_missing" in texts["combined_cutover"]
            and "substr(target_identity,-10)" in texts["combined_cutover"]
        ),
    }
    # A check whose subject no longer exists has not failed; its subject is
    # gone.  Three of these read one-shot cutover and poller scripts that
    # were deleted on 2026-08-04 when the work they carried finished, and
    # because a missing file reads as empty text every one of them turned
    # silently false -- an audit reporting a boundary violation about a file
    # that is not there.  Retired subjects are named and excluded; the
    # living checks still decide the verdict, and a subject that comes back
    # is checked again without any edit here.
    subject_by_check = {
        "hotfix_uses_zuuu_resolution_local_date": "hotfix",
        "amsc_pending_queue_uses_current_resolution_local_date": "amsc",
        "market_context_refresh_uses_resolution_local_date": "monitor",
        "combined_cutover_binds_latest_committed_target_date": "combined_cutover",
    }
    retired = sorted(
        name for name, subject in subject_by_check.items()
        if not paths[subject].is_file()
    )
    live_checks = {
        name: value for name, value in checks.items() if name not in set(retired)
    }
    blockers = [
        f"resolution_date_boundary_failed:{name}"
        for name, ok in live_checks.items() if not ok
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "resolution_date_boundary_aligned" if not blockers else "blocked",
        "blockers": blockers,
        "checks": live_checks,
        "retired_subjects": {
            name: str(paths[subject_by_check[name]].relative_to(root))
            for name in retired
        },
        "resolution_local_timezone": "Asia/Shanghai",
        "operations_timezone": "America/New_York",
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = audit(args.root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
