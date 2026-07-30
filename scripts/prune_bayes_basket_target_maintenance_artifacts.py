#!/usr/bin/env python3
"""Plan or apply bounded retention for target maintenance artifacts."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from bayes_basket_target.maintenance_retention import (  # noqa: E402
    RetentionPolicy,
    apply_retention_plan,
    plan_maintenance_retention,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


CONFIRM = "APPLY_BOUNDED_MAINTENANCE_RETENTION"
DATA = ROOT / "data"
AUDITS = ROOT / "output" / "audits"
ACTIVE_DB = DATA / "bayes_basket_target_ssot.db"
MAX_DIRECTORY_ENTRIES = 4096


def _matching_count(
    directory: Path,
    *,
    prefix: str,
    suffix: str,
) -> int:
    count = 0
    for index, path in enumerate(directory.iterdir(), start=1):
        if index > MAX_DIRECTORY_ENTRIES:
            raise RuntimeError("maintenance_retention_directory_entries_exceeded")
        if path.name.startswith(prefix) and path.name.endswith(suffix):
            count += 1
    return count


def _backup_policy(prefix: str) -> RetentionPolicy:
    return RetentionPolicy(
        backup_prefix=prefix,
        failed_receipt_prefix="__no_failed_receipt_match__",
        backup_suffixes=(".db",),
        max_count=2,
        max_age_seconds=30 * 24 * 60 * 60,
        max_total_bytes=512 * 1024 * 1024,
        keep_failed_receipts=0,
        max_scan_entries=MAX_DIRECTORY_ENTRIES,
        max_scan_bytes=2 * 1024 * 1024 * 1024,
        max_backup_bytes=256 * 1024 * 1024,
        require_recoverable_backup=True,
    )


def _receipt_policy() -> RetentionPolicy:
    return RetentionPolicy(
        backup_prefix="__no_backup_match__",
        failed_receipt_prefix=(
            "bayes_basket_target_ssot_v4_cutover_failed_"
        ),
        max_count=8,
        max_age_seconds=30 * 24 * 60 * 60,
        max_total_bytes=16 * 1024 * 1024,
        keep_failed_receipts=8,
        max_scan_entries=MAX_DIRECTORY_ENTRIES,
        max_scan_bytes=64 * 1024 * 1024,
        max_backup_bytes=256 * 1024 * 1024,
        require_recoverable_backup=False,
    )


def build_plans() -> list[tuple[str, Any]]:
    plans: list[tuple[str, Any]] = []
    for name, prefix in (
        ("target_backups", "bayes_basket_target_ssot.pre_"),
        (
            "target_activation_snapshot_backups",
            "bayes_basket_target_ssot.activation-",
        ),
        ("hard_no_backups", "hard_no_decisions.pre_timezone_"),
    ):
        if _matching_count(DATA, prefix=prefix, suffix=".db") == 0:
            continue
        plans.append(
            (
                name,
                plan_maintenance_retention(
                    DATA,
                    workspace_root=ROOT,
                    active_db_path=ACTIVE_DB,
                    policy=_backup_policy(prefix),
                ),
            )
        )
    failure_prefix = "bayes_basket_target_ssot_v4_cutover_failed_"
    if _matching_count(AUDITS, prefix=failure_prefix, suffix=".json"):
        plans.append(
            (
                "failed_cutover_receipts",
                plan_maintenance_retention(
                    AUDITS,
                    workspace_root=ROOT,
                    active_db_path=ACTIVE_DB,
                    policy=_receipt_policy(),
                ),
            )
        )
    return plans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRM:
        raise SystemExit("maintenance_retention_confirmation_invalid")
    plans = build_plans()
    groups: list[dict[str, Any]] = []
    for name, plan in plans:
        item: dict[str, Any] = {
            "name": name,
            "plan": plan.as_dict(),
        }
        if args.apply:
            item["apply"] = apply_retention_plan(plan, confirm=True)
        groups.append(item)
    result = {
        "schema": "bayes_basket_target_maintenance_retention_run_v1",
        "state": "applied" if args.apply else "planned",
        "generated_at_ns": time.time_ns(),
        "group_count": len(groups),
        "groups": groups,
        "active_database": str(ACTIVE_DB.resolve()),
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
    }
    if args.receipt is not None:
        write_bounded_json(args.receipt, result)
    print(
        __import__("json").dumps(
            {
                "state": result["state"],
                "group_count": len(groups),
                "delete_count": sum(
                    int(
                        (
                            group.get("apply")
                            or group["plan"]
                        ).get("deleted_count")
                        or group["plan"].get("delete_count")
                        or 0
                    )
                    for group in groups
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
