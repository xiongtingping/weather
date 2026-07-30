"""Bounded retention for Bayes basket target maintenance artifacts.

The module is intentionally small and side-effect free while planning.  It
only considers regular files matching caller-supplied, explicit prefixes and
suffixes.  Applying a plan requires an explicit confirmation and repeats the
entire bounded scan so a stale plan cannot delete a changed file.

The newest SQLite backup that passes ``PRAGMA quick_check(1)`` and the newest
configured number of failed receipts are hard retention minima.  Age, count,
and byte limits are then applied newest-first to all optional artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Sequence


MIB = 1024 * 1024
DEFAULT_MAX_SCAN_ENTRIES = 4096
DEFAULT_MAX_SCAN_BYTES = 1024 * MIB
DEFAULT_MAX_BACKUP_BYTES = 256 * MIB


class MaintenanceRetentionError(RuntimeError):
    """Raised when retention cannot be proved safe and bounded."""


@dataclass(frozen=True)
class RetentionPolicy:
    """Explicit matching and bounded retention policy.

    ``max_count`` and ``max_total_bytes`` apply to the combined backup and
    failed-receipt set.  Hard retention minima take precedence over age and
    byte limits; an unavoidable byte overflow is reported in the plan.
    """

    backup_prefix: str
    failed_receipt_prefix: str
    backup_suffixes: tuple[str, ...] = (".db",)
    receipt_suffixes: tuple[str, ...] = (".json",)
    max_count: int = 16
    max_age_seconds: int = 30 * 24 * 60 * 60
    max_total_bytes: int = 512 * MIB
    keep_failed_receipts: int = 3
    require_recoverable_backup: bool = True
    max_scan_entries: int = DEFAULT_MAX_SCAN_ENTRIES
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES
    max_backup_bytes: int = DEFAULT_MAX_BACKUP_BYTES


@dataclass(frozen=True)
class RetentionEntry:
    """Immutable filesystem identity captured by a dry-run plan."""

    name: str
    path: str
    kind: Literal["backup", "failed_receipt"]
    size: int
    mtime_ns: int
    device: int
    inode: int
    recoverable: bool | None
    action: Literal["keep", "delete"]
    reason: str


@dataclass(frozen=True)
class RetentionPlan:
    """Content-addressed dry-run output accepted by :func:`apply_retention_plan`."""

    schema: str
    workspace_root: str
    directory: str
    active_db_path: str
    directory_device: int
    directory_inode: int
    planned_at_ns: int
    policy: RetentionPolicy
    entries: tuple[RetentionEntry, ...]
    retained_count: int
    retained_bytes: int
    delete_count: int
    delete_bytes: int
    limits_satisfied: bool
    limit_notes: tuple[str, ...]
    plan_sha256: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable plan without changing its identity."""

        result = asdict(self)
        result["policy"]["backup_suffixes"] = list(self.policy.backup_suffixes)
        result["policy"]["receipt_suffixes"] = list(self.policy.receipt_suffixes)
        return result


@dataclass(frozen=True)
class _Candidate:
    name: str
    path: Path
    kind: Literal["backup", "failed_receipt"]
    size: int
    mtime_ns: int
    device: int
    inode: int
    recoverable: bool | None


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _validate_component_symlinks(path: Path, *, error: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise MaintenanceRetentionError(error)


def _validate_prefix(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise MaintenanceRetentionError(f"{name}_invalid")
    if value != Path(value).name or "/" in value or "\\" in value:
        raise MaintenanceRetentionError(f"{name}_invalid")
    return value


def _validate_suffixes(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        raise MaintenanceRetentionError(f"{name}_invalid")
    normalized: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or len(value) < 2
            or not value.startswith(".")
            or value != Path(value).name
            or "/" in value
            or "\\" in value
        ):
            raise MaintenanceRetentionError(f"{name}_invalid")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise MaintenanceRetentionError(f"{name}_duplicate")
    return tuple(normalized)


def _validated_policy(policy: RetentionPolicy) -> RetentionPolicy:
    backup_prefix = _validate_prefix(policy.backup_prefix, name="backup_prefix")
    receipt_prefix = _validate_prefix(
        policy.failed_receipt_prefix, name="failed_receipt_prefix"
    )
    backup_suffixes = _validate_suffixes(
        policy.backup_suffixes, name="backup_suffixes"
    )
    receipt_suffixes = _validate_suffixes(
        policy.receipt_suffixes, name="receipt_suffixes"
    )
    integer_fields = {
        "max_count": policy.max_count,
        "max_age_seconds": policy.max_age_seconds,
        "max_total_bytes": policy.max_total_bytes,
        "keep_failed_receipts": policy.keep_failed_receipts,
        "max_scan_entries": policy.max_scan_entries,
        "max_scan_bytes": policy.max_scan_bytes,
        "max_backup_bytes": policy.max_backup_bytes,
    }
    for name, value in integer_fields.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MaintenanceRetentionError(f"{name}_invalid")
    if not isinstance(policy.require_recoverable_backup, bool):
        raise MaintenanceRetentionError(
            "require_recoverable_backup_invalid"
        )
    hard_backup_minimum = 1 if policy.require_recoverable_backup else 0
    if policy.max_count < hard_backup_minimum + policy.keep_failed_receipts:
        raise MaintenanceRetentionError("max_count_below_hard_retention_minimum")
    if (
        policy.max_total_bytes <= 0
        or policy.max_scan_entries <= 0
        or policy.max_scan_bytes <= 0
        or policy.max_backup_bytes <= 0
    ):
        raise MaintenanceRetentionError("resource_limit_must_be_positive")
    return RetentionPolicy(
        backup_prefix=backup_prefix,
        failed_receipt_prefix=receipt_prefix,
        backup_suffixes=backup_suffixes,
        receipt_suffixes=receipt_suffixes,
        max_count=policy.max_count,
        max_age_seconds=policy.max_age_seconds,
        max_total_bytes=policy.max_total_bytes,
        keep_failed_receipts=policy.keep_failed_receipts,
        require_recoverable_backup=policy.require_recoverable_backup,
        max_scan_entries=policy.max_scan_entries,
        max_scan_bytes=policy.max_scan_bytes,
        max_backup_bytes=policy.max_backup_bytes,
    )


def _matches(name: str, prefix: str, suffixes: tuple[str, ...]) -> bool:
    return name.startswith(prefix) and any(name.endswith(suffix) for suffix in suffixes)


def _validated_roots(
    directory: Path | str,
    workspace_root: Path | str,
    active_db_path: Path | str,
) -> tuple[Path, Path, Path, os.stat_result]:
    workspace = _absolute(workspace_root)
    target = _absolute(directory)
    active = _absolute(active_db_path)
    _validate_component_symlinks(workspace, error="workspace_root_symlink_forbidden")
    _validate_component_symlinks(target, error="retention_directory_symlink_forbidden")
    _validate_component_symlinks(active, error="active_db_symlink_forbidden")
    if not workspace.is_dir():
        raise MaintenanceRetentionError("workspace_root_not_directory")
    if not target.is_dir():
        raise MaintenanceRetentionError("retention_directory_not_directory")
    workspace = workspace.resolve()
    target = target.resolve()
    active = active.resolve(strict=False)
    if target == Path(target.anchor):
        raise MaintenanceRetentionError("retention_directory_root_forbidden")
    if target == workspace:
        raise MaintenanceRetentionError("retention_directory_workspace_root_forbidden")
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise MaintenanceRetentionError(
            "retention_directory_outside_workspace"
        ) from exc
    try:
        directory_stat = target.stat()
    except OSError as exc:
        raise MaintenanceRetentionError("retention_directory_stat_failed") from exc
    return target, workspace, active, directory_stat


def _sqlite_backup_recoverable(path: Path, *, size: int) -> bool:
    if size <= 0:
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=1.0,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-2048")
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        return bool(row and row[0] == "ok")
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


def _scan_candidates(
    directory: Path,
    *,
    active_db: Path,
    policy: RetentionPolicy,
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    scanned_entries = 0
    matched_bytes = 0
    backup_bytes = 0
    protected = {
        active_db,
        active_db.with_name(active_db.name + "-wal"),
        active_db.with_name(active_db.name + "-shm"),
    }
    protected_identities: set[tuple[int, int]] = set()
    for protected_path in protected:
        if protected_path.is_symlink():
            raise MaintenanceRetentionError(
                "active_database_or_sidecar_symlink_forbidden"
            )
        try:
            protected_stat = protected_path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MaintenanceRetentionError(
                "active_database_or_sidecar_stat_failed"
            ) from exc
        protected_identities.add(
            (int(protected_stat.st_dev), int(protected_stat.st_ino))
        )
    candidate_identities: set[tuple[int, int]] = set()
    try:
        iterator = os.scandir(directory)
    except OSError as exc:
        raise MaintenanceRetentionError("retention_directory_scan_failed") from exc
    with iterator:
        for item in iterator:
            scanned_entries += 1
            if scanned_entries > policy.max_scan_entries:
                raise MaintenanceRetentionError("retention_scan_entries_exceeded")
            backup_match = _matches(
                item.name, policy.backup_prefix, policy.backup_suffixes
            )
            receipt_match = _matches(
                item.name,
                policy.failed_receipt_prefix,
                policy.receipt_suffixes,
            )
            if not backup_match and not receipt_match:
                continue
            if backup_match and receipt_match:
                raise MaintenanceRetentionError("retention_pattern_ambiguous")
            if item.is_symlink():
                raise MaintenanceRetentionError("retention_candidate_symlink_forbidden")
            try:
                item_stat = item.stat(follow_symlinks=False)
            except OSError as exc:
                raise MaintenanceRetentionError(
                    "retention_candidate_stat_failed"
                ) from exc
            if not stat.S_ISREG(item_stat.st_mode):
                raise MaintenanceRetentionError(
                    "retention_candidate_not_regular_file"
                )
            path = directory / item.name
            if path in protected:
                raise MaintenanceRetentionError(
                    "active_database_or_sidecar_matches_retention_pattern"
                )
            identity = (int(item_stat.st_dev), int(item_stat.st_ino))
            if identity in protected_identities:
                raise MaintenanceRetentionError(
                    "active_database_or_sidecar_hardlink_forbidden"
                )
            if identity in candidate_identities:
                raise MaintenanceRetentionError(
                    "retention_candidate_hardlink_ambiguous"
                )
            candidate_identities.add(identity)
            size = int(item_stat.st_size)
            if size < 0:
                raise MaintenanceRetentionError("retention_candidate_size_invalid")
            matched_bytes += size
            if matched_bytes > policy.max_scan_bytes:
                raise MaintenanceRetentionError("retention_scan_bytes_exceeded")
            kind: Literal["backup", "failed_receipt"] = (
                "backup" if backup_match else "failed_receipt"
            )
            recoverable: bool | None = None
            if kind == "backup":
                if size > policy.max_backup_bytes:
                    raise MaintenanceRetentionError(
                        "retention_backup_validation_bytes_exceeded"
                    )
                backup_bytes += size
                if backup_bytes > policy.max_scan_bytes:
                    raise MaintenanceRetentionError(
                        "retention_backup_validation_total_bytes_exceeded"
                    )
                recoverable = _sqlite_backup_recoverable(path, size=size)
            candidates.append(
                _Candidate(
                    name=item.name,
                    path=path,
                    kind=kind,
                    size=size,
                    mtime_ns=int(item_stat.st_mtime_ns),
                    device=int(item_stat.st_dev),
                    inode=int(item_stat.st_ino),
                    recoverable=recoverable,
                )
            )
    return tuple(
        sorted(candidates, key=lambda item: (-item.mtime_ns, item.name))
    )


def _plan_identity(
    *,
    workspace: Path,
    directory: Path,
    active_db: Path,
    directory_stat: os.stat_result,
    planned_at_ns: int,
    policy: RetentionPolicy,
    entries: tuple[RetentionEntry, ...],
    retained_count: int,
    retained_bytes: int,
    delete_count: int,
    delete_bytes: int,
    limits_satisfied: bool,
    limit_notes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema": "bayes_basket_target_maintenance_retention_plan_v1",
        "workspace_root": str(workspace),
        "directory": str(directory),
        "active_db_path": str(active_db),
        "directory_device": int(directory_stat.st_dev),
        "directory_inode": int(directory_stat.st_ino),
        "planned_at_ns": planned_at_ns,
        "policy": asdict(policy),
        "entries": [asdict(item) for item in entries],
        "retained_count": retained_count,
        "retained_bytes": retained_bytes,
        "delete_count": delete_count,
        "delete_bytes": delete_bytes,
        "limits_satisfied": limits_satisfied,
        "limit_notes": list(limit_notes),
    }


def _sha256(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def plan_maintenance_retention(
    directory: Path | str,
    *,
    workspace_root: Path | str,
    active_db_path: Path | str,
    policy: RetentionPolicy,
    now_ns: int | None = None,
) -> RetentionPlan:
    """Build a bounded dry-run plan without deleting or writing any file."""

    checked_policy = _validated_policy(policy)
    target, workspace, active_db, directory_stat = _validated_roots(
        directory, workspace_root, active_db_path
    )
    if now_ns is None:
        now_ns = time.time_ns()
    if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
        raise MaintenanceRetentionError("planned_at_ns_invalid")
    candidates = _scan_candidates(
        target, active_db=active_db, policy=checked_policy
    )
    if any(item.mtime_ns > now_ns for item in candidates):
        raise MaintenanceRetentionError("retention_candidate_mtime_in_future")
    recoverable = [
        item
        for item in candidates
        if item.kind == "backup" and item.recoverable is True
    ]
    if checked_policy.require_recoverable_backup and not recoverable:
        raise MaintenanceRetentionError("recoverable_backup_required")

    receipts = [item for item in candidates if item.kind == "failed_receipt"]
    mandatory_names = (
        {recoverable[0].name}
        if checked_policy.require_recoverable_backup
        else set()
    )
    mandatory_names.update(
        item.name for item in receipts[: checked_policy.keep_failed_receipts]
    )

    retained_names = set(mandatory_names)
    retained_count = len(mandatory_names)
    retained_bytes = sum(
        item.size for item in candidates if item.name in mandatory_names
    )
    reasons: dict[str, str] = {}
    if checked_policy.require_recoverable_backup:
        reasons[recoverable[0].name] = "newest_recoverable_backup"
    for item in receipts[: checked_policy.keep_failed_receipts]:
        reasons[item.name] = "recent_failed_receipt_minimum"

    delete_reasons: dict[str, str] = {}
    max_age_ns = checked_policy.max_age_seconds * 1_000_000_000
    for item in candidates:
        if item.name in mandatory_names:
            continue
        if item.kind == "backup" and item.recoverable is not True:
            delete_reasons[item.name] = "backup_not_recoverable"
            continue
        if now_ns - item.mtime_ns > max_age_ns:
            delete_reasons[item.name] = "max_age_exceeded"
            continue
        if retained_count + 1 > checked_policy.max_count:
            delete_reasons[item.name] = "max_count_exceeded"
            continue
        if retained_bytes + item.size > checked_policy.max_total_bytes:
            delete_reasons[item.name] = "max_total_bytes_exceeded"
            continue
        retained_names.add(item.name)
        retained_count += 1
        retained_bytes += item.size
        reasons[item.name] = "within_retention_limits"

    limit_notes: list[str] = []
    if retained_bytes > checked_policy.max_total_bytes:
        limit_notes.append("hard_retention_minimum_exceeds_max_total_bytes")
    if retained_count > checked_policy.max_count:
        limit_notes.append("hard_retention_minimum_exceeds_max_count")

    entries = tuple(
        RetentionEntry(
            name=item.name,
            path=str(item.path),
            kind=item.kind,
            size=item.size,
            mtime_ns=item.mtime_ns,
            device=item.device,
            inode=item.inode,
            recoverable=item.recoverable,
            action="keep" if item.name in retained_names else "delete",
            reason=(
                reasons[item.name]
                if item.name in retained_names
                else delete_reasons[item.name]
            ),
        )
        for item in sorted(
            candidates,
            key=lambda candidate: (
                candidate.name in retained_names,
                candidate.mtime_ns,
                candidate.name,
            ),
        )
    )
    delete_entries = [item for item in entries if item.action == "delete"]
    delete_count = len(delete_entries)
    delete_bytes = sum(item.size for item in delete_entries)
    notes = tuple(limit_notes)
    identity = _plan_identity(
        workspace=workspace,
        directory=target,
        active_db=active_db,
        directory_stat=directory_stat,
        planned_at_ns=now_ns,
        policy=checked_policy,
        entries=entries,
        retained_count=retained_count,
        retained_bytes=retained_bytes,
        delete_count=delete_count,
        delete_bytes=delete_bytes,
        limits_satisfied=not notes,
        limit_notes=notes,
    )
    return RetentionPlan(
        schema=str(identity["schema"]),
        workspace_root=str(identity["workspace_root"]),
        directory=str(identity["directory"]),
        active_db_path=str(identity["active_db_path"]),
        directory_device=int(identity["directory_device"]),
        directory_inode=int(identity["directory_inode"]),
        planned_at_ns=int(identity["planned_at_ns"]),
        policy=checked_policy,
        entries=entries,
        retained_count=retained_count,
        retained_bytes=retained_bytes,
        delete_count=delete_count,
        delete_bytes=delete_bytes,
        limits_satisfied=not notes,
        limit_notes=notes,
        plan_sha256=_sha256(identity),
    )


def apply_retention_plan(
    plan: RetentionPlan,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """Apply exactly one unchanged dry-run plan after explicit confirmation."""

    if confirm is not True:
        raise MaintenanceRetentionError("retention_apply_confirmation_required")
    if not isinstance(plan, RetentionPlan):
        raise MaintenanceRetentionError("retention_plan_type_invalid")

    try:
        fresh = plan_maintenance_retention(
            plan.directory,
            workspace_root=plan.workspace_root,
            active_db_path=plan.active_db_path,
            policy=plan.policy,
            now_ns=plan.planned_at_ns,
        )
    except MaintenanceRetentionError as exc:
        raise MaintenanceRetentionError("retention_plan_stale") from exc
    if fresh.plan_sha256 != plan.plan_sha256:
        raise MaintenanceRetentionError("retention_plan_stale")

    directory = Path(plan.directory)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except OSError as exc:
        raise MaintenanceRetentionError("retention_directory_open_failed") from exc
    deleted: list[str] = []
    deleted_bytes = 0
    try:
        current_directory_stat = os.fstat(directory_fd)
        if (
            int(current_directory_stat.st_dev) != plan.directory_device
            or int(current_directory_stat.st_ino) != plan.directory_inode
        ):
            raise MaintenanceRetentionError("retention_directory_identity_changed")
        for entry in (item for item in plan.entries if item.action == "delete"):
            try:
                current = os.stat(
                    entry.name, dir_fd=directory_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise MaintenanceRetentionError(
                    "retention_candidate_changed_before_delete"
                ) from exc
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise MaintenanceRetentionError(
                    "retention_candidate_changed_before_delete"
                )
            if (
                int(current.st_dev) != entry.device
                or int(current.st_ino) != entry.inode
                or int(current.st_size) != entry.size
                or int(current.st_mtime_ns) != entry.mtime_ns
            ):
                raise MaintenanceRetentionError(
                    "retention_candidate_changed_before_delete"
                )
            try:
                os.unlink(entry.name, dir_fd=directory_fd)
            except OSError as exc:
                raise MaintenanceRetentionError(
                    "retention_candidate_delete_failed"
                ) from exc
            deleted.append(entry.path)
            deleted_bytes += entry.size
    finally:
        os.close(directory_fd)

    return {
        "schema": "bayes_basket_target_maintenance_retention_apply_v1",
        "plan_sha256": plan.plan_sha256,
        "directory": plan.directory,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "deleted_bytes": deleted_bytes,
        "retained_count": plan.retained_count,
        "retained_bytes": plan.retained_bytes,
        "limits_satisfied": plan.limits_satisfied,
        "limit_notes": list(plan.limit_notes),
    }


__all__ = [
    "DEFAULT_MAX_BACKUP_BYTES",
    "DEFAULT_MAX_SCAN_BYTES",
    "DEFAULT_MAX_SCAN_ENTRIES",
    "MaintenanceRetentionError",
    "RetentionEntry",
    "RetentionPlan",
    "RetentionPolicy",
    "apply_retention_plan",
    "plan_maintenance_retention",
]
