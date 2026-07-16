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
    text = script.read_text(encoding="utf-8") if script.is_file() else ""
    if not text:
        blockers.append("production_cutover_script_missing")
    forbidden = {
        "cutover_references_volumes_path": "/Volumes/",
        "cutover_references_external_backup_variable": "EXTERNAL_BACKUP",
        "cutover_references_external_volume_variable": "EXTERNAL_VOLUME",
        "cutover_references_external_backups_symlink": "data/backups",
    }
    forbidden_hits = [label for label, token in forbidden.items() if token in text]
    blockers.extend(forbidden_hits)
    cow_clone = '/bin/cp -c data/archive.db "$LOCAL_STAGE"' in text
    if not cow_clone:
        blockers.append("local_apfs_cow_rollback_clone_missing")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "production_storage_boundary_aligned" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "archive_db": str(db),
        "archive_db_is_regular_non_symlink": db_is_regular,
        "archive_db_same_device_as_workspace": same_device_as_root,
        "archive_db_same_device_as_private_tmp": same_device_as_private_tmp,
        "local_apfs_cow_rollback_clone": cow_clone,
        "external_disk_probed": False,
        "external_disk_required": False,
        "external_disk_role": "discardable_rebuildable_cold_archive_only",
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
