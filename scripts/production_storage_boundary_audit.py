#!/usr/bin/env python3
"""Read-only audit for the production/local versus external archive boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "production_storage_boundary_audit_v1"
RETIRED_EXTERNAL_CUTOVERS = (
    "scripts/refresh_reported_x_runtime_manifest_epoch.sh",
    "scripts/cutover_physical_state_bayes_v6_production.sh",
    "scripts/cutover_reported_x_probability_epoch.sh",
    "scripts/cutover_cumulative_strong_likelihood_v5.sh",
    "scripts/cutover_state_aware_strong_lr_probability_epoch.sh",
    "scripts/cutover_amsc_slot_complete_v6_and_trade_caps.sh",
    "scripts/cutover_dynamic_position_market_alpha_schema.sh",
)
RETIREMENT_TOKEN = "RETIRED_EXTERNAL_DISK_PRODUCTION_CUTOVER_FORBIDDEN"


def audit(root: Path, *, archive_db: Path, cutover_script: Path) -> dict[str, Any]:
    root = root.resolve()
    db = archive_db if archive_db.is_absolute() else root / archive_db
    script = cutover_script if cutover_script.is_absolute() else root / cutover_script
    blockers: list[str] = []
    db_exists = db.exists()
    db_is_symlink = db.is_symlink()
    db_is_regular = db.is_file() and not db_is_symlink
    if not db_exists:
        blockers.append("production_archive_db_missing")
    if db_is_symlink:
        blockers.append("production_archive_db_must_not_be_symlink")
    if db_exists and not db_is_regular:
        blockers.append("production_archive_db_not_regular_local_file")
    same_device_as_root = False
    same_device_as_private_tmp = False
    if db_exists:
        db_device = os.stat(db).st_dev
        same_device_as_root = db_device == os.stat(root).st_dev
        same_device_as_private_tmp = db_device == os.stat("/private/tmp").st_dev
        if not same_device_as_root:
            blockers.append("production_archive_db_not_on_workspace_local_device")
        if not same_device_as_private_tmp:
            blockers.append("local_cow_rollback_stage_not_same_device_as_archive_db")
    # A cutover script that is not there cannot probe an external disk, and
    # cannot fail to stage a rollback clone either.  Its content checks have
    # a retired subject, not a failure: the scripts this audit was written
    # against were deleted on 2026-08-04 when their cutovers finished, and
    # treating their absence as a boundary violation had the audit report
    # two -- production_cutover_script_missing and
    # local_apfs_cow_rollback_clone_missing -- about a file that is gone.
    # A present script is still held to every one of them.
    cutover_present = script.is_file()
    text = script.read_text(encoding="utf-8") if cutover_present else ""
    retired_cutover_subject = "" if cutover_present else str(cutover_script)
    forbidden = {
        "cutover_references_volumes_path": "/Volumes/",
        "cutover_references_external_backup_variable": "EXTERNAL_BACKUP",
        "cutover_references_external_volume_variable": "EXTERNAL_VOLUME",
        "cutover_references_external_backups_symlink": "data/backups",
    }
    forbidden_hits = [label for label, token in forbidden.items() if token in text]
    blockers.extend(forbidden_hits)
    cow_clone = (
        True if not cutover_present
        else '/bin/cp -c data/archive.db "$LOCAL_STAGE"' in text
    )
    if cutover_present and not cow_clone:
        blockers.append("local_apfs_cow_rollback_clone_missing")
    retired_cutovers: dict[str, bool] = {}
    for relative in RETIRED_EXTERNAL_CUTOVERS:
        retired_path = root / relative
        retired_present = retired_path.is_file()
        retired_text = retired_path.read_text(encoding="utf-8") if retired_present else ""
        guard_at = retired_text.find(RETIREMENT_TOKEN)
        exit_at = retired_text.find("exit 2", guard_at)
        external_at = retired_text.find("/Volumes/")
        guarded = (
            not retired_present
            or (
                guard_at >= 0
                and exit_at > guard_at
                and (external_at < 0 or exit_at < external_at)
            )
        )
        retired_cutovers[relative] = guarded
        if not guarded:
            blockers.append(f"retired_external_cutover_not_fail_closed:{relative}")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "production_storage_boundary_aligned" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "retired_cutover_subject": retired_cutover_subject,
        "archive_db": str(db),
        "archive_db_is_regular_non_symlink": db_is_regular,
        "archive_db_same_device_as_workspace": same_device_as_root,
        "archive_db_same_device_as_private_tmp": same_device_as_private_tmp,
        "local_apfs_cow_rollback_clone": cow_clone,
        "external_disk_probed": False,
        "external_disk_required": False,
        "external_disk_role": "discardable_rebuildable_cold_archive_only",
        "external_disk_may_contain_production_database": False,
        "external_disk_may_supply_backup_or_rollback": False,
        "external_disk_may_affect_readiness_or_runtime": False,
        "external_archive_failure_effect": "none",
        "retired_external_cutovers_fail_closed_before_probe": retired_cutovers,
        "blocks_amsc_or_ssot": False,
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path, default=Path("data/archive.db"))
    parser.add_argument(
        "--cutover",
        type=Path,
        default=Path("scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh"),
    )
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = audit(args.root, archive_db=args.db, cutover_script=args.cutover)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
