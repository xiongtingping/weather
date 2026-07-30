from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.maintenance_retention import (
    MaintenanceRetentionError,
    RetentionPolicy,
    apply_retention_plan,
    plan_maintenance_retention,
)


def _sqlite(path: Path, value: int = 1) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE proof(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO proof VALUES (?)", (value,))


def _mtime(path: Path, seconds: int) -> None:
    os.utime(path, ns=(seconds * 1_000_000_000, seconds * 1_000_000_000))


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    artifacts = workspace / "maintenance"
    artifacts.mkdir(parents=True)
    return workspace, artifacts


def _policy(**changes: object) -> RetentionPolicy:
    values: dict[str, object] = {
        "backup_prefix": "target-cutover-backup-",
        "failed_receipt_prefix": "target-cutover-failed-",
        "max_count": 5,
        "max_age_seconds": 1_000,
        "max_total_bytes": 10 * 1024 * 1024,
        "keep_failed_receipts": 2,
    }
    values.update(changes)
    return RetentionPolicy(**values)  # type: ignore[arg-type]


def test_plan_is_dry_run_and_keeps_newest_recoverable_and_failed_receipts(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    old_backup = artifacts / "target-cutover-backup-001.db"
    newest_backup = artifacts / "target-cutover-backup-002.db"
    corrupt_backup = artifacts / "target-cutover-backup-003.db"
    _sqlite(old_backup, 1)
    _sqlite(newest_backup, 2)
    corrupt_backup.write_bytes(b"not sqlite")
    receipts = [
        artifacts / f"target-cutover-failed-00{index}.json" for index in range(3)
    ]
    for index, receipt in enumerate(receipts):
        receipt.write_text(f'{{"failure": {index}}}', encoding="utf-8")
    _mtime(old_backup, 10)
    _mtime(newest_backup, 30)
    _mtime(corrupt_backup, 40)
    for index, receipt in enumerate(receipts, start=20):
        _mtime(receipt, index)

    plan = plan_maintenance_retention(
        artifacts,
        workspace_root=workspace,
        active_db_path=active,
        policy=_policy(max_count=3),
        now_ns=50 * 1_000_000_000,
    )

    actions = {entry.name: (entry.action, entry.reason) for entry in plan.entries}
    assert actions[newest_backup.name] == ("keep", "newest_recoverable_backup")
    assert actions[corrupt_backup.name] == ("delete", "backup_not_recoverable")
    assert actions[receipts[2].name] == ("keep", "recent_failed_receipt_minimum")
    assert actions[receipts[1].name] == ("keep", "recent_failed_receipt_minimum")
    assert plan.retained_count == 3
    assert plan.delete_count == 3
    assert all(path.exists() for path in [old_backup, newest_backup, corrupt_backup, *receipts])


def test_apply_requires_confirmation_and_deletes_oldest_first(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    active_wal = active.with_name(active.name + "-wal")
    active_shm = active.with_name(active.name + "-shm")
    active_wal.write_bytes(b"active wal")
    active_shm.write_bytes(b"active shm")
    keep = artifacts / "target-cutover-backup-keep.db"
    old_backup = artifacts / "target-cutover-backup-old.db"
    old_receipt = artifacts / "target-cutover-failed-old.json"
    newest_receipt = artifacts / "target-cutover-failed-new.json"
    _sqlite(keep)
    _sqlite(old_backup)
    old_receipt.write_text("{}", encoding="utf-8")
    newest_receipt.write_text("{}", encoding="utf-8")
    for path, timestamp in (
        (old_backup, 10),
        (old_receipt, 20),
        (newest_receipt, 30),
        (keep, 40),
    ):
        _mtime(path, timestamp)
    plan = plan_maintenance_retention(
        artifacts,
        workspace_root=workspace,
        active_db_path=active,
        policy=_policy(max_count=2, keep_failed_receipts=1),
        now_ns=50 * 1_000_000_000,
    )
    expected_order = [
        entry.path for entry in plan.entries if entry.action == "delete"
    ]
    assert expected_order == [str(old_backup), str(old_receipt)]

    with pytest.raises(
        MaintenanceRetentionError, match="retention_apply_confirmation_required"
    ):
        apply_retention_plan(plan)
    result = apply_retention_plan(plan, confirm=True)

    assert result["deleted"] == expected_order
    assert keep.exists()
    assert newest_receipt.exists()
    assert active.exists()
    assert active_wal.read_bytes() == b"active wal"
    assert active_shm.read_bytes() == b"active shm"


def test_only_explicit_prefixes_and_suffixes_are_candidates(tmp_path: Path) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    backup = artifacts / "target-cutover-backup-001.db"
    _sqlite(backup)
    unrelated = artifacts / "other-backup-001.db"
    wrong_type = artifacts / "target-cutover-backup-001.txt"
    receipt_like = artifacts / "other-failed-001.json"
    unrelated.write_bytes(b"leave me")
    wrong_type.write_bytes(b"leave me")
    receipt_like.write_bytes(b"leave me")

    plan = plan_maintenance_retention(
        artifacts,
        workspace_root=workspace,
        active_db_path=active,
        policy=_policy(),
    )

    assert {entry.name for entry in plan.entries} == {backup.name}
    apply_retention_plan(plan, confirm=True)
    assert unrelated.exists() and wrong_type.exists() and receipt_like.exists()


@pytest.mark.parametrize("target_kind", ["workspace", "root", "outside"])
def test_directory_scope_fail_closed(tmp_path: Path, target_kind: str) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    target = {
        "workspace": workspace,
        "root": Path("/"),
        "outside": tmp_path / "outside",
    }[target_kind]
    if target_kind == "outside":
        target.mkdir()
    with pytest.raises(MaintenanceRetentionError):
        plan_maintenance_retention(
            target,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(),
        )
    assert artifacts.exists()


def test_directory_and_matching_candidate_symlinks_fail_closed(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    backup = artifacts / "target-cutover-backup-real.db"
    _sqlite(backup)
    linked_directory = workspace / "linked-maintenance"
    linked_directory.symlink_to(artifacts, target_is_directory=True)
    with pytest.raises(
        MaintenanceRetentionError, match="retention_directory_symlink_forbidden"
    ):
        plan_maintenance_retention(
            linked_directory,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(),
        )

    linked_backup = artifacts / "target-cutover-backup-linked.db"
    linked_backup.symlink_to(backup)
    with pytest.raises(
        MaintenanceRetentionError, match="retention_candidate_symlink_forbidden"
    ):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(),
        )
    assert backup.exists()


def test_active_database_match_is_rejected_with_wal_and_shm_untouched(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = artifacts / "target-cutover-backup-active.db"
    _sqlite(active)
    wal = active.with_name(active.name + "-wal")
    shm = active.with_name(active.name + "-shm")
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    with pytest.raises(
        MaintenanceRetentionError,
        match="active_database_or_sidecar_matches_retention_pattern",
    ):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(),
        )
    assert active.exists() and wal.exists() and shm.exists()


def test_stale_plan_fails_before_any_delete(tmp_path: Path) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    newest = artifacts / "target-cutover-backup-new.db"
    old_a = artifacts / "target-cutover-backup-old-a.db"
    old_b = artifacts / "target-cutover-backup-old-b.db"
    for index, path in enumerate((old_a, old_b, newest), start=1):
        _sqlite(path, index)
        _mtime(path, index * 10)
    plan = plan_maintenance_retention(
        artifacts,
        workspace_root=workspace,
        active_db_path=active,
        policy=_policy(max_count=1, keep_failed_receipts=0),
        now_ns=40 * 1_000_000_000,
    )
    old_b.write_bytes(old_b.read_bytes() + b"changed")

    with pytest.raises(MaintenanceRetentionError, match="retention_plan_stale"):
        apply_retention_plan(plan, confirm=True)
    assert old_a.exists() and old_b.exists() and newest.exists()


def test_age_and_total_byte_limits_prune_optional_artifacts(tmp_path: Path) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    newest = artifacts / "target-cutover-backup-new.db"
    too_old = artifacts / "target-cutover-backup-too-old.db"
    too_large_optional = artifacts / "target-cutover-backup-too-large.db"
    for index, path in enumerate((too_old, too_large_optional, newest), start=1):
        _sqlite(path, index)
    _mtime(too_old, 1)
    _mtime(too_large_optional, 99)
    _mtime(newest, 100)
    plan = plan_maintenance_retention(
        artifacts,
        workspace_root=workspace,
        active_db_path=active,
        policy=_policy(
            max_count=3,
            keep_failed_receipts=0,
            max_age_seconds=50,
            max_total_bytes=newest.stat().st_size,
        ),
        now_ns=100 * 1_000_000_000,
    )
    reasons = {entry.name: entry.reason for entry in plan.entries}
    assert reasons[too_old.name] == "max_age_exceeded"
    assert reasons[too_large_optional.name] == "max_total_bytes_exceeded"
    assert reasons[newest.name] == "newest_recoverable_backup"


def test_scan_entry_and_byte_bounds_fail_before_deletion(tmp_path: Path) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    backup = artifacts / "target-cutover-backup-new.db"
    _sqlite(backup)
    extra = artifacts / "unrelated.txt"
    extra.write_bytes(b"x")
    with pytest.raises(
        MaintenanceRetentionError, match="retention_scan_entries_exceeded"
    ):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(max_scan_entries=1),
        )
    with pytest.raises(
        MaintenanceRetentionError, match="retention_scan_bytes_exceeded"
    ):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(max_scan_bytes=1),
        )
    assert backup.exists()


def test_recoverable_backup_is_mandatory_and_byte_overflow_is_explicit(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    backup = artifacts / "target-cutover-backup-new.db"
    _sqlite(backup)
    plan = plan_maintenance_retention(
        artifacts,
        workspace_root=workspace,
        active_db_path=active,
        policy=_policy(max_count=1, keep_failed_receipts=0, max_total_bytes=1),
    )
    assert plan.entries[0].action == "keep"
    assert not plan.limits_satisfied
    assert plan.limit_notes == (
        "hard_retention_minimum_exceeds_max_total_bytes",
    )


def test_no_recoverable_backup_fails_closed(tmp_path: Path) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    corrupt = artifacts / "target-cutover-backup-corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(
        MaintenanceRetentionError, match="recoverable_backup_required"
    ):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(),
        )
    assert corrupt.exists()


def test_receipt_only_policy_does_not_require_sqlite_backup(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    receipts = [
        artifacts / f"target-cutover-failed-{index}.json"
        for index in range(4)
    ]
    for index, receipt in enumerate(receipts, start=1):
        receipt.write_text("{}", encoding="utf-8")
        _mtime(receipt, index)

    plan = plan_maintenance_retention(
        artifacts,
        workspace_root=workspace,
        active_db_path=active,
        policy=_policy(
            require_recoverable_backup=False,
            max_count=2,
            keep_failed_receipts=2,
        ),
        now_ns=5 * 1_000_000_000,
    )

    kept = [entry.name for entry in plan.entries if entry.action == "keep"]
    assert set(kept) == {receipts[3].name, receipts[2].name}
    result = apply_retention_plan(plan, confirm=True)
    assert result["deleted_count"] == 2
    assert receipts[3].exists() and receipts[2].exists()
    assert not receipts[1].exists() and not receipts[0].exists()


def test_future_dated_mandatory_candidate_fails_closed(tmp_path: Path) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    backup = artifacts / "target-cutover-backup-future.db"
    _sqlite(backup)
    _mtime(backup, 101)
    with pytest.raises(
        MaintenanceRetentionError, match="retention_candidate_mtime_in_future"
    ):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(),
            now_ns=100 * 1_000_000_000,
        )


def test_active_db_symlink_and_hardlink_candidates_fail_closed(
    tmp_path: Path,
) -> None:
    workspace, artifacts = _layout(tmp_path)
    real_active = workspace / "real-active.db"
    _sqlite(real_active)
    linked_active = workspace / "active.db"
    linked_active.symlink_to(real_active)
    backup = artifacts / "target-cutover-backup-good.db"
    _sqlite(backup)
    with pytest.raises(MaintenanceRetentionError, match="active_db_symlink_forbidden"):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=linked_active,
            policy=_policy(),
        )
    linked_active.unlink()
    os.link(real_active, artifacts / "target-cutover-backup-active-hardlink.db")
    with pytest.raises(
        MaintenanceRetentionError,
        match="active_database_or_sidecar_hardlink_forbidden",
    ):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=real_active,
            policy=_policy(),
        )


def test_invalid_or_ambiguous_patterns_fail_closed(tmp_path: Path) -> None:
    workspace, artifacts = _layout(tmp_path)
    active = workspace / "active.db"
    _sqlite(active)
    backup = artifacts / "same-001.db"
    _sqlite(backup)
    with pytest.raises(MaintenanceRetentionError, match="backup_prefix_invalid"):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(backup_prefix="../"),
        )
    with pytest.raises(MaintenanceRetentionError, match="retention_pattern_ambiguous"):
        plan_maintenance_retention(
            artifacts,
            workspace_root=workspace,
            active_db_path=active,
            policy=_policy(
                backup_prefix="same-",
                failed_receipt_prefix="same-",
                receipt_suffixes=(".db",),
            ),
        )
