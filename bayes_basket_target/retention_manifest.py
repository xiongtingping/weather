"""Read-only, bounded retention classification for the target lane.

This module is deliberately an audit contract, not a cleanup implementation.
It accepts only explicitly supplied paths, never follows symlinks, never opens
SQLite databases, and never exposes file contents in a receipt.  Unknown
objects fail safe to ``KEEP``; only the small set of explicit temporary rules
can produce ``DELETE_CANDIDATE``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable, Mapping, Sequence

from .resource_bounds import (
    MIB,
    MAX_TARGET_PROCESS_RSS_BYTES,
    ResourceBoundError,
    assert_process_memory_budget,
    bounded_json_bytes,
)


SCHEMA = "bayes_basket_target_retention_manifest_v1"
KEEP = "KEEP"
DELETE_CANDIDATE = "DELETE_CANDIDATE"
MAINTENANCE_REQUIRED = "MAINTENANCE_REQUIRED"
CATEGORIES = frozenset({KEEP, DELETE_CANDIDATE, MAINTENANCE_REQUIRED})

DEFAULT_MAX_FILES = 4096
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_HASH_BYTES = 64 * MIB
DEFAULT_MIN_FREE_BYTES = 512 * MIB
HASH_CHUNK_BYTES = 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTERNAL_ROOT_PARTS = frozenset({"Volumes", "mnt", "media", "run"})


class RetentionManifestError(ResourceBoundError):
    """Raised when the audit cannot prove its resource or path contract."""


def _absolute(path: Path | str) -> Path:
    """Make a lexical absolute path without resolving symlink targets."""

    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _is_external(path: Path) -> bool:
    parts = path.parts
    if path.is_absolute() and len(parts) > 1 and parts[1] in _EXTERNAL_ROOT_PARTS:
        return True
    return any(part in {"Volumes", "mnt", "media", "run", "external"} for part in parts)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_root(project_root: Path | str) -> Path:
    root = _absolute(project_root)
    if _is_external(root):
        raise RetentionManifestError("retention_external_path_forbidden")
    try:
        if not root.is_dir():
            raise RetentionManifestError("retention_project_root_not_directory")
    except OSError as exc:
        raise RetentionManifestError("retention_project_root_probe_failed") from exc
    return root


def _validate_references(
    referenced_paths: Iterable[Path | str],
    referenced_hashes: Iterable[str],
    *,
    project_root: Path,
) -> tuple[set[Path], set[str]]:
    paths: set[Path] = set()
    for value in referenced_paths:
        path = _absolute(value)
        if _is_external(path) or not _inside(path, project_root):
            raise RetentionManifestError("retention_reference_path_forbidden")
        paths.add(path)
    hashes: set[str] = set()
    for value in referenced_hashes:
        digest = str(value).strip().lower()
        if not _SHA256.fullmatch(digest):
            raise RetentionManifestError("retention_reference_hash_invalid")
        hashes.add(digest)
    return paths, hashes


def _path_tokens(path: Path, root: Path) -> set[str]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return {part.lower() for part in relative.parts}


def _protected_reason(path: Path, root: Path) -> tuple[str, bool] | None:
    """Return (reason, maintenance-only) for authoritative objects."""

    name = path.name.lower()
    tokens = _path_tokens(path, root)
    suffixes = tuple(path.suffixes)

    if name == "archive.db" or name.startswith("archive.db-"):
        return "authoritative_archive_db_or_wal_maintenance_window_only", True
    if "bayes_basket_target_ssot" in name or name in {"ssot.db", "ssot.db-wal", "ssot.db-shm"}:
        return "current_target_ssot_authoritative", False

    # These names/segments are intentionally explicit.  An unrecognised
    # directory is never inferred to be disposable.
    if tokens & {
        "lineage",
        "raw",
        "historical_truth_raw",
        "model_arrivals",
        "model_payload",
        "model_payloads",
        "calibration",
        "calibrations",
        "doctrine",
        "migration",
        "migrations",
        "backup",
        "backups",
    }:
        return "lineage_raw_model_calibration_doctrine_or_migration_authority", False
    if "truth" in name and ("raw" in name or "official" in name):
        return "official_truth_authority", False
    if "calibration" in name or "doctrine" in name:
        return "calibration_or_doctrine_authority", False
    if "migration" in name and suffixes:
        return "migration_backup_authority", False
    return None


def _candidate_reason(path: Path, root: Path, size_bytes: int, *, referenced: bool) -> str | None:
    if referenced:
        return None
    name = path.name.lower()
    tokens = _path_tokens(path, root)
    if "__pycache__" in tokens:
        return "unreferenced_python_cache"
    if name == ".ds_store":
        return "unreferenced_macos_metadata"
    if name == ".playwright-cli" or ".playwright-cli" in tokens:
        return "unreferenced_playwright_temporary"
    if name == "ui_server.err.log":
        return "retired_ui_error_log"
    if size_bytes == 0 and (
        name == "target.db"
        or name.endswith("test.db")
        or name.endswith("_test.sqlite")
        or name.endswith(".test.sqlite")
        or "test" in name and name.endswith((".db", ".sqlite", ".sqlite3"))
    ):
        return "empty_unreferenced_test_database"
    return None


def classify_path(
    path: Path | str,
    *,
    project_root: Path | str,
    size_bytes: int,
    reference_status: str = "unreferenced",
) -> dict[str, Any]:
    """Classify one explicit path using the fail-safe retention rules."""

    root = _absolute(project_root)
    candidate = _absolute(path)
    if _is_external(candidate) or not _inside(candidate, root):
        raise RetentionManifestError("retention_path_outside_project_root")
    status = str(reference_status)
    if status not in {"referenced", "unreferenced", "unknown"}:
        raise RetentionManifestError("retention_reference_status_invalid")
    protected = _protected_reason(candidate, root)
    if status == "referenced":
        category, reason = KEEP, "explicit_lineage_reference"
    elif protected is not None:
        reason, maintenance = protected
        category = MAINTENANCE_REQUIRED if maintenance else KEEP
    else:
        reason_candidate = _candidate_reason(candidate, root, int(size_bytes), referenced=False)
        if reason_candidate is not None and status == "unreferenced":
            category, reason = DELETE_CANDIDATE, reason_candidate
        else:
            category, reason = KEEP, "unknown_or_unproven_reference_fail_closed"
    return {
        "category": category,
        "path": str(candidate),
        "reason": reason,
        "reference_status": status,
    }


def _disk_free_bytes(root: Path) -> int:
    try:
        return int(shutil.disk_usage(root).free)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise RetentionManifestError("retention_disk_probe_unavailable") from exc


def _iter_explicit_paths(paths: Sequence[Path], *, max_files: int) -> Iterable[Path]:
    stack = list(reversed(paths))
    yielded = 0
    while stack:
        path = stack.pop()
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise RetentionManifestError("retention_path_stat_failed") from exc
        if not os.path.isdir(path) or os.path.islink(path):
            yielded += 1
            if yielded > max_files:
                raise RetentionManifestError("retention_max_files_exceeded")
            yield path
            continue
        try:
            children = sorted((Path(entry.path) for entry in os.scandir(path)), reverse=True)
        except OSError as exc:
            raise RetentionManifestError("retention_directory_scan_failed") from exc
        stack.extend(children)


def _hash_if_bounded(path: Path, size_bytes: int, *, max_hash_bytes: int) -> tuple[str | None, str]:
    if size_bytes > max_hash_bytes:
        return None, "not_computed_resource_bound"
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                assert_process_memory_budget(projected_bytes=HASH_CHUNK_BYTES)
                chunk = stream.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except (OSError, ResourceBoundError) as exc:
        if isinstance(exc, ResourceBoundError):
            raise RetentionManifestError(str(exc)) from exc
        raise RetentionManifestError("retention_hash_read_failed") from exc
    return digest.hexdigest(), "computed"


def build_retention_manifest(
    paths: Sequence[Path | str],
    *,
    project_root: Path | str,
    referenced_paths: Iterable[Path | str] = (),
    referenced_hashes: Iterable[str] = (),
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    now: float | None = None,
) -> dict[str, Any]:
    """Build an audit-only manifest from explicit files/directories.

    ``referenced_paths`` and ``referenced_hashes`` are the complete reference
    index for this invocation.  A path absent from that explicit index is
    ``unreferenced``; callers that cannot prove index completeness should pass
    no candidates or mark them ``unknown`` through :func:`classify_path`.
    """

    if int(max_files) <= 0 or int(max_bytes) < 0 or int(max_hash_bytes) < 0 or int(min_free_bytes) < 0:
        raise RetentionManifestError("retention_budget_invalid")
    root = _validate_root(project_root)
    explicit = [_absolute(path) for path in paths]
    if not explicit:
        raise RetentionManifestError("retention_explicit_paths_required")
    for path in explicit:
        if _is_external(path):
            raise RetentionManifestError("retention_external_path_forbidden")
        if not _inside(path, root):
            raise RetentionManifestError("retention_path_outside_project_root")
    reference_paths, reference_hashes = _validate_references(
        referenced_paths, referenced_hashes, project_root=root
    )
    free_bytes = _disk_free_bytes(root)
    if free_bytes < int(min_free_bytes):
        raise RetentionManifestError("retention_min_free_space_guard")
    try:
        assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    except ResourceBoundError as exc:
        raise RetentionManifestError(str(exc)) from exc

    current = float(datetime.now(tz=timezone.utc).timestamp() if now is None else now)
    entries: list[dict[str, Any]] = []
    discovered_bytes = 0
    bytes_read = 0
    seen: set[Path] = set()
    for path in _iter_explicit_paths(explicit, max_files=int(max_files)):
        path = _absolute(path)
        if path in seen:
            continue
        seen.add(path)
        try:
            stat = os.lstat(path)
            size_bytes = int(stat.st_size) if not os.path.isdir(path) else 0
            mtime_ns = int(stat.st_mtime_ns)
        except OSError as exc:
            raise RetentionManifestError("retention_path_stat_failed") from exc
        discovered_bytes += size_bytes

        digest: str | None = None
        hash_status = "not_computed_directory_or_symlink"
        reference_status = "referenced" if path in reference_paths else "unreferenced"
        if not os.path.isdir(path) and not os.path.islink(path):
            if size_bytes <= int(max_hash_bytes):
                bytes_read += size_bytes
                if bytes_read > int(max_bytes):
                    raise RetentionManifestError("retention_max_bytes_exceeded")
            digest, hash_status = _hash_if_bounded(
                path, size_bytes, max_hash_bytes=int(max_hash_bytes)
            )
            if digest in reference_hashes:
                reference_status = "referenced"
        classification = classify_path(
            path,
            project_root=root,
            size_bytes=size_bytes,
            reference_status=reference_status,
        )
        if os.path.islink(path):
            classification = {
                **classification,
                "category": KEEP,
                "reason": "symlink_not_followed_fail_closed",
            }
        try:
            mtime_utc = _utc_iso(float(stat.st_mtime))
        except (OverflowError, OSError, ValueError):
            raise RetentionManifestError("retention_mtime_probe_failed")
        entries.append({
            **classification,
            "size_bytes": size_bytes,
            "mtime_ns": mtime_ns,
            "mtime_utc": mtime_utc,
            "sha256": digest,
            "hash_status": hash_status,
        })
    counts = {category: sum(1 for item in entries if item["category"] == category) for category in sorted(CATEGORIES)}
    bytes_by_category = {
        category: sum(int(item["size_bytes"]) for item in entries if item["category"] == category)
        for category in sorted(CATEGORIES)
    }
    generated = _utc_iso(current)
    manifest = {
        "schema": SCHEMA,
        "state": "AUDIT_ONLY",
        "generated_at_utc": generated,
        "project_root": str(root),
        "explicit_input_count": len(explicit),
        "entry_count": len(entries),
        "entries": entries,
        "counts": counts,
        "bytes_by_category": bytes_by_category,
        "resource": {
            "ok": True,
            "free_bytes": free_bytes,
            "max_files": int(max_files),
            "max_bytes": int(max_bytes),
            "max_hash_bytes": int(max_hash_bytes),
            "min_free_bytes": int(min_free_bytes),
            "scanned_files": len(entries),
            "discovered_bytes": discovered_bytes,
            "bytes_read": bytes_read,
            "cleanup_performed": False,
            "writes_production_db": False,
            "external_archive_access": False,
        },
        "delete_enabled": False,
        "cleanup_performed": False,
    }
    # Validate the report itself against the existing bounded JSON contract.
    try:
        bounded_json_bytes(manifest)
    except ResourceBoundError as exc:
        raise RetentionManifestError(str(exc)) from exc
    return manifest


def audit_retention(**kwargs: Any) -> dict[str, Any]:
    """Named audit alias used by callers and the command-line wrapper."""

    return build_retention_manifest(**kwargs)


__all__ = [
    "CATEGORIES",
    "DELETE_CANDIDATE",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_HASH_BYTES",
    "DEFAULT_MIN_FREE_BYTES",
    "KEEP",
    "MAINTENANCE_REQUIRED",
    "RetentionManifestError",
    "SCHEMA",
    "audit_retention",
    "build_retention_manifest",
    "classify_path",
]
