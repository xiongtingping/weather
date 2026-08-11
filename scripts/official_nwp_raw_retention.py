#!/usr/bin/env python3
"""Plan and safely prune official-NWP raw objects.

Two independent lanes share one rule: a raw body may only be discarded once
its content hash has been written to a local JSONL manifest, and the immutable
receipts and normalized snapshots are never edited.  That manifest is not a
production dependency and is deliberately excluded from the production
readiness, posterior, and trading paths.

``curve`` lane
    The curve root is an isolated source-evidence store.  Raw objects
    referenced by current/history/receipt/revision JSON are immutable replay
    inputs, regardless of run age, and are never deleted by retention.  Only
    genuinely unreferenced objects may be discarded.

``native`` lane
    The content-addressed object stores written by
    ``native_raw_object_store.store_content_addressed_message`` -- every
    ``<provider>/raw`` directory under ``data/runtime/official_nwp_sources``
    and ``data/research/bayes_basket_target/model_arrivals``.  These stores
    have no eviction of their own and grow by gigabytes per day.  Their
    references do not live beside them: the packet JSON under ``output/audits``
    names a body only through ``field_receipts[].message_sha256``, so the
    reference index is built over explicit reference roots instead of the
    store's own directory.  A body is deleted only when it is unreferenced
    everywhere *and* older than the in-flight-capture age floor.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_CURVE_ROOT = ROOT / "data/runtime/official_nwp_curve_sources_v2"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETAIN_LATEST_RUNS = 2
PROVIDERS = ("ecmwf", "icon", "gfs")
RAW_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
ARCHIVE_MANIFEST_NAME = "raw_cold_archive_manifest.jsonl"
ACTIVE_CAPTURE_MARKERS = (
    "run_official_nwp_daily_curve_monitor_job.py",
    "collect_dwd_icon_zuuu.py",
    "collect_ecmwf_ifs_zuuu.py",
    "collect_official_nwp_snapshot.py",
)

# --- native content-addressed object-store lane -------------------------
DEFAULT_NATIVE_SOURCE_ROOTS = (
    ROOT / "data/runtime/official_nwp_sources",
    ROOT / "data/research/bayes_basket_target/model_arrivals",
)
# A packet under ``output/audits`` is the only thing that names a native body,
# and ``data/research`` / ``data/runtime`` hold the per-provider receipt and
# curve documents.  Dropping any of these three roots silently reclassifies
# live bodies as unreferenced: scanning ``output/audits`` alone reports every
# ``official_nwp_sources/{ecmwf,icon,gfs}`` body as garbage.
DEFAULT_NATIVE_REFERENCE_ROOTS = (
    ROOT / "output/audits",
    ROOT / "data/research",
    ROOT / "data/runtime",
)
RAW_STORE_DIR_NAME = "raw"
REFERENCE_DOCUMENT_SUFFIXES = (".json", ".jsonl")
# A capture stores the body first and writes its packet at the end of the
# cycle, so a body younger than this floor may simply be mid-flight.  The
# resident worker is up continuously and therefore cannot be used as an
# "is a capture running" signal the way the curve lane's collectors can; this
# floor is what makes the native lane safe against an in-flight capture.
DEFAULT_NATIVE_MIN_AGE_DAYS = 2
NATIVE_BODY_SCAN_LIMIT = 500_000
NATIVE_REFERENCE_DOCUMENT_LIMIT = 200_000
NATIVE_HEX64 = re.compile(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
# This worker's own audit output lands under output/audits, which is also a
# reference root, and it lists the hash of every body it plans to discard.
# Reading it back would mark those bodies referenced and quietly disable
# retention from the second run onwards.  Recognise the plan by its schema so
# a renamed or wrapped copy is skipped too.
SELF_PLAN_MARKERS = (
    b'"official_nwp_native_raw_retention_plan_v1"',
    b'"official_nwp_raw_retention_plan_v2"',
)
# Historical backfills store bodies across a whole multi-hour run and can
# therefore straddle the age floor, so an active one blocks the pass.
#
# The continuous producers are deliberately absent.  The resident worker never
# stops, and it spawns capture_official_native_ensemble_forward.py per station
# around the clock; treating either as an active capture would block retention
# permanently.  Both write a body and its packet inside one short process, so
# the age floor is what covers them.
NATIVE_ACTIVE_CAPTURE_MARKERS = (
    "backfill_ecmwf_ifs_multistation_archive.py",
    "backfill_noaa_gefs_archive.py",
    "backfill_noaa_gefs_multistation_archive.py",
    "capture_icon_eps_daily_all_stations.py",
    "capture_native_ensemble_pilot_et.py",
)


def _hash_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidates = [Path(text).name.split(".", 1)[0], text]
    for candidate in candidates:
        if RAW_HASH.fullmatch(candidate):
            return candidate.lower()
    return ""


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, str):
        yield value


def _json_documents(curve_root: Path) -> Iterable[Path]:
    for path in curve_root.rglob("*.json"):
        if "raw" in path.parts:
            continue
        if path.name.startswith("."):
            continue
        yield path


def _scan_references(curve_root: Path) -> tuple[set[str], list[dict[str, Any]]]:
    referenced: set[str] = set()
    missing: dict[str, set[str]] = {}
    root_text = str(curve_root.resolve())
    for path in _json_documents(curve_root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        for value in _walk_strings(payload):
            token = _hash_token(value)
            if token:
                referenced.add(token)
            if root_text in value and "/raw/" in value:
                raw_path = Path(value)
                if not raw_path.is_file():
                    missing.setdefault(str(raw_path), set()).add(str(path))
    return referenced, [
        {
            "path": path,
            "referencer_count": len(referencers),
            "referencers": sorted(referencers)[:20],
        }
        for path, referencers in sorted(missing.items())
    ]


def _scan_raw_lineage(curve_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Collect run/valid-time lineage for each direct raw body hash.

    Receipt JSON is immutable evidence.  This reader is intentionally
    conservative: it only assigns a body to a run when its object reference
    is under ``<provider>/raw`` and a receipt supplies that run identity.
    Unknown-lineage bodies are therefore protected from budget cleanup.
    """

    root = curve_root.resolve()
    lineage: dict[str, list[dict[str, Any]]] = {}

    def visit(value: Any, context: Mapping[str, Any], source: Path) -> None:
        if isinstance(value, Mapping):
            next_context = dict(context)
            for key in ("provider_id", "run_at_utc", "valid_time_utc"):
                if str(value.get(key) or "").strip():
                    next_context[key] = str(value[key]).strip()
            raw_ref = str(value.get("raw_object_ref") or "").strip()
            if raw_ref:
                try:
                    resolved = Path(raw_ref).expanduser().resolve()
                except (OSError, RuntimeError):
                    resolved = Path(raw_ref).expanduser()
                provider = str(next_context.get("provider_id") or "").strip()
                provider_raw = (root / provider / "raw").resolve() if provider in PROVIDERS else None
                if provider_raw is not None:
                    try:
                        in_provider_raw = resolved.parent == provider_raw
                    except OSError:
                        in_provider_raw = False
                    if in_provider_raw:
                        token = _hash_token(value.get("raw_sha256") or raw_ref)
                        if token:
                            row = {
                                "provider": provider,
                                "run_at_utc": str(next_context.get("run_at_utc") or "").strip(),
                                "valid_time_utc": str(next_context.get("valid_time_utc") or "").strip(),
                                "requested_at": str(value.get("requested_at") or "").strip(),
                                "response_completed_at": str(value.get("response_completed_at") or "").strip(),
                                "source": str(source),
                                "raw_request_url": str(value.get("raw_request_url") or "").strip(),
                            }
                            lineage.setdefault(token, []).append(row)
            for item in value.values():
                visit(item, next_context, source)
        elif isinstance(value, list):
            for item in value:
                visit(item, context, source)

    for path in _json_documents(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        visit(payload, {}, path)
    return lineage


def _archive_manifest_path(curve_root: Path) -> Path:
    return curve_root.resolve() / ARCHIVE_MANIFEST_NAME


def _load_archived_paths(curve_root: Path) -> dict[str, dict[str, Any]]:
    """Load acknowledged missing/discarded bodies without making them live."""

    path = _archive_manifest_path(curve_root)
    rows: dict[str, dict[str, Any]] = {}
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return rows
    with handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(item, Mapping):
                continue
            raw_path = str(item.get("path") or item.get("original_path") or "").strip()
            if raw_path:
                rows[raw_path] = dict(item)
    return rows


def _append_archive_manifest(curve_root: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    items = [dict(row) for row in rows]
    if not items:
        return 0
    path = _archive_manifest_path(curve_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(items)


def acknowledge_missing_references(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Record pre-existing missing historical bodies without editing receipts."""

    curve_root = Path(str(plan.get("curve_root") or "")).expanduser().resolve()
    already = _load_archived_paths(curve_root)
    rows: list[dict[str, Any]] = []
    now_value = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for item in plan.get("missing_raw_references") or []:
        raw_path = str(item.get("path") or "").strip()
        if not raw_path or raw_path in already:
            continue
        token = Path(raw_path).name.split(".", 1)[0].lower()
        rows.append(
            {
                "schema": "official_nwp_raw_cold_archive_entry_v1",
                "state": "preexisting_missing_body",
                "recorded_at": now_value,
                "path": raw_path,
                "sha256": token if RAW_HASH.fullmatch(token) else "",
                "size_bytes": None,
                "provider": Path(raw_path).parts[-3] if len(Path(raw_path).parts) >= 3 else "",
                "referencer_count": int(item.get("referencer_count") or 0),
                "referencers": list(item.get("referencers") or []),
                "production_dependency": False,
                "strict_replay_body_available": False,
            }
        )
    count = _append_archive_manifest(curve_root, rows)
    return {"acknowledged_count": count, "archive_manifest_path": str(_archive_manifest_path(curve_root))}


def _referenced_hashes(curve_root: Path) -> set[str]:
    """Compatibility helper returning only the conservative hash set."""

    referenced, _missing = _scan_references(curve_root)
    return referenced


def _active_capture_processes(curve_root: Path) -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["ps", "axww", "-o", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return [{"reason": "process_list_unavailable"}]
    root_text = str(curve_root.resolve())
    rows: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text or str(os.getpid()) in text:
            continue
        if "official_nwp_raw_retention.py" in text or "grep -E" in text:
            continue
        if root_text not in text or not any(marker in text for marker in ACTIVE_CAPTURE_MARKERS):
            continue
        parts = text.split(None, 1)
        rows.append(
            {
                "pid": int(parts[0]) if parts and parts[0].isdigit() else None,
                "command": parts[1] if len(parts) > 1 else text,
            }
        )
    return rows


def _capture_lease_path(curve_root: Path) -> Path:
    configured = str(os.environ.get("PM_OFFICIAL_NWP_CAPTURE_LEASE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return curve_root.resolve().parent / "official_nwp_capture.lock"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_plan(
    curve_root: Path,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    retain_latest_runs: int = DEFAULT_RETAIN_LATEST_RUNS,
    now: datetime | None = None,
    providers: Iterable[str] = PROVIDERS,
) -> dict[str, Any]:
    root = curve_root.expanduser().resolve()
    now_value = now or datetime.now(timezone.utc)
    cutoff_epoch = now_value.timestamp() - max(0, int(retention_days)) * 86400.0
    referenced, missing_references = _scan_references(root)
    archived_paths = _load_archived_paths(root)
    missing_references = [
        item for item in missing_references if str(item.get("path") or "") not in archived_paths
    ]
    lineage = _scan_raw_lineage(root)
    keep_run_count = max(0, int(retain_latest_runs))
    latest_runs_by_provider: dict[str, list[str]] = {}
    for provider in providers:
        runs: set[str] = set()
        for rows in lineage.values():
            for item in rows:
                if str(item.get("provider") or "") == provider and str(item.get("run_at_utc") or "").strip():
                    runs.add(str(item["run_at_utc"]).strip())
        latest_runs_by_provider[provider] = sorted(runs, reverse=True)[:keep_run_count]
    provider_rows: list[dict[str, Any]] = []
    unreferenced: list[dict[str, Any]] = []
    old_referenced: list[dict[str, Any]] = []
    hot_files: list[dict[str, Any]] = []
    for provider in providers:
        raw_root = root / provider / "raw"
        files = sorted(path for path in raw_root.iterdir() if path.is_file()) if raw_root.is_dir() else []
        provider_unreferenced: list[dict[str, Any]] = []
        provider_old_referenced: list[dict[str, Any]] = []
        provider_hot: list[dict[str, Any]] = []
        for path in files:
            token = path.name.split(".", 1)[0].lower()
            evidence_rows = [row for row in lineage.get(token, []) if str(row.get("provider") or "") == provider]
            run_ids = sorted({str(row.get("run_at_utc") or "").strip() for row in evidence_rows if str(row.get("run_at_utc") or "").strip()})
            hot = bool(set(run_ids).intersection(latest_runs_by_provider.get(provider, [])))
            row = {
                "provider": provider,
                "path": str(path),
                "name": path.name,
                "sha256_name": token,
                "size_bytes": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "older_than_retention": path.stat().st_mtime < cutoff_epoch,
                "referenced": token in referenced,
                "run_at_utc": run_ids[-1] if run_ids else "",
                "run_ids": run_ids,
                "hot_run": hot,
                "lineage_known": bool(run_ids),
            }
            if token not in referenced:
                provider_unreferenced.append(row)
                unreferenced.append(row)
            elif hot:
                provider_hot.append(row)
                hot_files.append(row)
            elif run_ids:
                provider_old_referenced.append(row)
                old_referenced.append(row)
        provider_rows.append(
            {
                "provider": provider,
                "raw_file_count": len(files),
                "raw_bytes": sum(path.stat().st_size for path in files),
                "unreferenced_count": len(provider_unreferenced),
                "unreferenced_bytes": sum(item["size_bytes"] for item in provider_unreferenced),
                "old_referenced_count": len(provider_old_referenced),
                "old_referenced_bytes": sum(item["size_bytes"] for item in provider_old_referenced),
                "hot_run_count": len(latest_runs_by_provider.get(provider, [])),
                "hot_runs": latest_runs_by_provider.get(provider, []),
                "hot_file_count": len(provider_hot),
                "hot_bytes": sum(item["size_bytes"] for item in provider_hot),
                "unknown_lineage_count": sum(
                    1 for path in files if not any(
                        str(row.get("provider") or "") == provider
                        for row in lineage.get(path.name.split(".", 1)[0].lower(), [])
                    )
                ),
            }
        )
    active_processes = _active_capture_processes(root)
    return {
        "schema": "official_nwp_raw_retention_plan_v2",
        "state": "retention_plan_ready",
        "curve_root": str(root),
        "generated_at": now_value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "retention_days": int(retention_days),
        "retain_latest_runs": keep_run_count,
        "retention_cutoff": datetime.fromtimestamp(cutoff_epoch, timezone.utc).isoformat(),
        "providers": list(providers),
        "referenced_hash_count": len(referenced),
        "missing_raw_references": missing_references,
        "missing_raw_reference_count": len(missing_references),
        "acknowledged_missing_raw_reference_count": sum(
            1 for path in archived_paths.values() if path.get("state") == "preexisting_missing_body"
        ),
        "archived_raw_body_count": sum(
            1 for path in archived_paths.values() if path.get("state") == "body_discarded"
        ),
        "archive_manifest_path": str(_archive_manifest_path(root)),
        "provider_rows": provider_rows,
        "unreferenced_files": unreferenced,
        "old_referenced_files": old_referenced,
        "hot_files": hot_files,
        "active_capture_processes": active_processes,
        "capture_lease_path": str(_capture_lease_path(root)),
        "apply_allowed": not active_processes and not missing_references,
        "production_dependency": False,
        "external_archive_dependency": False,
        "raw_body_required_for_strict_replay": True,
        "cold_archive_is_nonproduction_only": True,
        "unreferenced_delete_requires_explicit_apply": True,
        "referenced_body_delete_forbidden": True,
    }


def apply_unreferenced(plan: Mapping[str, Any]) -> dict[str, Any]:
    if plan.get("active_capture_processes"):
        raise RuntimeError("official_nwp_raw_retention_capture_active")
    if plan.get("missing_raw_references"):
        raise RuntimeError("official_nwp_raw_retention_missing_raw_reference")
    # Referenced bodies are strict replay inputs.  Do not add an escape hatch
    # here: an old run can still be needed for ET reconstruction, calibration,
    # decoder replays, or audit.  Disk pressure must fail closed rather than
    # silently destroy the only official payload.
    candidates = [*list(plan.get("unreferenced_files") or [])]
    deleted: list[dict[str, Any]] = []
    seen: set[str] = set()
    manifest_rows: list[dict[str, Any]] = []
    for raw in candidates:
        path = Path(str(raw.get("path") or ""))
        if str(path) in seen:
            continue
        seen.add(str(path))
        if not path.is_file():
            continue
        expected = str(raw.get("sha256_name") or "").lower()
        actual = _sha256(path)
        if expected != actual:
            raise RuntimeError(f"official_nwp_raw_retention_hash_mismatch:{path}")
        size = path.stat().st_size
        manifest_rows.append(
            {
                "schema": "official_nwp_raw_cold_archive_entry_v1",
                "state": "body_discarded",
                "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "path": str(path),
                "sha256": actual,
                "size_bytes": size,
                "provider": str(raw.get("provider") or ""),
                "run_at_utc": str(raw.get("run_at_utc") or ""),
                "run_ids": list(raw.get("run_ids") or []),
                "deletion_reason": "unreferenced",
                "production_dependency": False,
                "strict_replay_body_available": False,
            }
        )
    _append_archive_manifest(Path(str(plan["curve_root"])), manifest_rows)
    for raw in candidates:
        path = Path(str(raw.get("path") or ""))
        if str(path) in {str(item.get("path") or "") for item in manifest_rows} and path.is_file():
            size = path.stat().st_size
            actual = str(raw.get("sha256_name") or "").lower()
            path.unlink()
            deleted.append({"path": str(path), "sha256": actual, "size_bytes": size})
    return {"deleted": deleted, "deleted_count": len(deleted), "deleted_bytes": sum(item["size_bytes"] for item in deleted)}


def _native_active_capture_processes() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["ps", "axww", "-o", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return [{"reason": "process_list_unavailable"}]
    rows: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text or str(os.getpid()) in text:
            continue
        if "official_nwp_raw_retention.py" in text or "grep -E" in text:
            continue
        if not any(marker in text for marker in NATIVE_ACTIVE_CAPTURE_MARKERS):
            continue
        parts = text.split(None, 1)
        rows.append(
            {
                "pid": int(parts[0]) if parts and parts[0].isdigit() else None,
                "command": parts[1] if len(parts) > 1 else text,
            }
        )
    return rows


def discover_raw_stores(source_root: Path) -> list[Path]:
    """Return every ``raw`` object-store directory under a source root.

    The stores are nested at different depths per provider -- ``gefs/raw`` but
    also ``icon_global_eps_official/grid_r03b06/raw`` -- so the directory name
    is the contract rather than a fixed depth.  Symlinked directories are not
    followed: a store must live inside the source root it is attributed to.
    """

    root = source_root.expanduser().resolve()
    if not root.is_dir():
        return []
    stores: list[Path] = []
    for current, directories, _files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not (Path(current) / name).is_symlink()
        )
        if Path(current).name == RAW_STORE_DIR_NAME and Path(current) != root:
            stores.append(Path(current))
    return stores


def _native_bodies(stores: Iterable[Path]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Index content-addressed bodies by hash, and list in-store evidence.

    A ``*.json``/``*.jsonl`` file inside a store is a receipt that happens to
    be filed next to the bodies (``gem_gdps_official/raw/<sha>.receipt.json``)
    or a raw API response that is the only copy of its payload.  Neither is a
    re-fetchable GRIB body, so both are excluded from deletion candidacy and
    reported separately.
    """

    bodies: dict[str, list[dict[str, Any]]] = {}
    evidence: list[dict[str, Any]] = []
    scanned = 0
    for store in stores:
        for path in sorted(store.iterdir()):
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            scanned += 1
            if scanned > NATIVE_BODY_SCAN_LIMIT:
                raise RuntimeError("official_nwp_native_retention_body_scan_limit")
            token = path.name.split(".", 1)[0].lower()
            if not RAW_HASH.fullmatch(token):
                evidence.append({"path": str(path), "reason": "not_content_addressed"})
                continue
            stat = path.stat()
            row = {
                "store": str(store),
                "path": str(path),
                "sha256_name": token,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "mtime_epoch": stat.st_mtime,
            }
            if path.name[len(token):].lower().endswith(REFERENCE_DOCUMENT_SUFFIXES):
                evidence.append({**row, "reason": "in_store_json_evidence_or_payload"})
                continue
            bodies.setdefault(token, []).append(row)
    return bodies, evidence


def build_native_reference_index(
    reference_roots: Iterable[Path],
    candidate_hashes: set[str],
    *,
    store_paths: Iterable[Path] = (),
    allow_unreadable: bool = False,
) -> dict[str, Any]:
    """Find which candidate body hashes any reference document still names.

    Only hashes that belong to a discovered body are retained, which keeps the
    index proportional to the object stores rather than to the 3.6 GiB of audit
    JSON it is built from.  Matching is done on raw bytes so a hash counts as a
    reference wherever it appears -- ``message_sha256``, ``raw_sha256``, a
    ``raw_object_ref`` path, or a field this script does not model.
    """

    stores = {Path(value).resolve() for value in store_paths}
    referenced: set[str] = set()
    referenced_outside_store: set[str] = set()
    unreadable: list[dict[str, str]] = []
    self_plans: list[str] = []
    documents = 0
    document_bytes = 0
    scanned_roots: list[str] = []
    wanted = {value.encode("ascii") for value in candidate_hashes}
    for reference_root in reference_roots:
        root = Path(reference_root).expanduser().resolve()
        if not root.is_dir():
            continue
        scanned_roots.append(str(root))
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                name for name in directories if not (Path(current) / name).is_symlink()
            ]
            inside_store = Path(current).resolve() in stores
            for name in sorted(files):
                if not name.lower().endswith(REFERENCE_DOCUMENT_SUFFIXES):
                    continue
                if name == ARCHIVE_MANIFEST_NAME:
                    # The cold-archive manifest is a tombstone, not a
                    # reference.  Reading it back would make every already
                    # discarded hash look live again, so a body that is later
                    # re-downloaded could never be collected a second time.
                    continue
                path = Path(current) / name
                if path.is_symlink():
                    continue
                documents += 1
                if documents > NATIVE_REFERENCE_DOCUMENT_LIMIT:
                    raise RuntimeError(
                        "official_nwp_native_retention_reference_document_limit"
                    )
                try:
                    payload = path.read_bytes()
                except OSError as exc:
                    # An unreadable document cannot prove a body is garbage,
                    # so the default is to refuse the whole pass.  The opt-out
                    # exists because a handful of root-owned 0600 leftovers
                    # would otherwise block disk hygiene forever; it records
                    # every skipped document in the plan.
                    unreadable.append({"path": str(path), "error": str(exc)})
                    if not allow_unreadable:
                        raise RuntimeError(
                            f"official_nwp_native_retention_reference_unreadable:{path}"
                        ) from exc
                    continue
                if any(marker in payload for marker in SELF_PLAN_MARKERS):
                    self_plans.append(str(path))
                    continue
                document_bytes += len(payload)
                found = {
                    match.group(0) for match in NATIVE_HEX64.finditer(payload)
                } & wanted
                if not found:
                    continue
                decoded = {value.decode("ascii") for value in found}
                referenced |= decoded
                if not inside_store:
                    referenced_outside_store |= decoded
    return {
        "referenced": referenced,
        "referenced_outside_store": referenced_outside_store,
        "reference_roots": scanned_roots,
        "reference_document_count": documents,
        "reference_document_bytes": document_bytes,
        "unreadable_reference_documents": unreadable,
        "skipped_self_plan_documents": sorted(self_plans),
    }


def build_native_plan(
    source_roots: Iterable[Path] = DEFAULT_NATIVE_SOURCE_ROOTS,
    *,
    reference_roots: Iterable[Path] = DEFAULT_NATIVE_REFERENCE_ROOTS,
    min_age_days: int = DEFAULT_NATIVE_MIN_AGE_DAYS,
    allow_unreadable_references: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    roots = [Path(value).expanduser().resolve() for value in source_roots]
    now_value = now or datetime.now(timezone.utc)
    age_cutoff = now_value.timestamp() - max(0, int(min_age_days)) * 86400.0
    stores_by_root = {root: discover_raw_stores(root) for root in roots}
    all_stores = [store for stores in stores_by_root.values() for store in stores]
    bodies, evidence = _native_bodies(all_stores)
    index = build_native_reference_index(
        reference_roots,
        set(bodies),
        store_paths=all_stores,
        allow_unreadable=allow_unreadable_references,
    )
    referenced: set[str] = index["referenced"]

    unreferenced: list[dict[str, Any]] = []
    withheld_young: list[dict[str, Any]] = []
    store_rows: list[dict[str, Any]] = []
    root_rows: list[dict[str, Any]] = []
    for root in roots:
        root_bytes = 0
        root_unreferenced_bytes = 0
        for store in stores_by_root[root]:
            store_text = str(store)
            provider = _native_provider(root, store)
            rows = [
                row
                for token_rows in bodies.values()
                for row in token_rows
                if row["store"] == store_text
            ]
            for row in rows:
                row["provider"] = provider
                row["source_root"] = str(root)
            store_unreferenced = [
                row for row in rows if row["sha256_name"] not in referenced
            ]
            eligible = [
                row for row in store_unreferenced if row["mtime_epoch"] < age_cutoff
            ]
            young = [
                row for row in store_unreferenced if row["mtime_epoch"] >= age_cutoff
            ]
            unreferenced.extend(eligible)
            withheld_young.extend(young)
            root_bytes += sum(row["size_bytes"] for row in rows)
            root_unreferenced_bytes += sum(row["size_bytes"] for row in eligible)
            store_rows.append(
                {
                    "source_root": str(root),
                    "store": store_text,
                    "provider": _native_provider(root, store),
                    "body_count": len(rows),
                    "body_bytes": sum(row["size_bytes"] for row in rows),
                    "unreferenced_count": len(eligible),
                    "unreferenced_bytes": sum(row["size_bytes"] for row in eligible),
                    "withheld_young_count": len(young),
                    "withheld_young_bytes": sum(row["size_bytes"] for row in young),
                }
            )
        root_rows.append(
            {
                "source_root": str(root),
                "exists": root.is_dir(),
                "store_count": len(stores_by_root[root]),
                "body_bytes": root_bytes,
                "unreferenced_bytes": root_unreferenced_bytes,
                "archive_manifest_path": str(_archive_manifest_path(root)),
            }
        )

    active_processes = _native_active_capture_processes()
    unreferenced.sort(key=lambda row: (row["mtime_epoch"], row["path"]))
    withheld_young.sort(key=lambda row: (row["mtime_epoch"], row["path"]))
    return {
        "schema": "official_nwp_native_raw_retention_plan_v1",
        "state": "retention_plan_ready",
        "lane": "native",
        "generated_at": now_value.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_roots": [str(root) for root in roots],
        "source_root_rows": root_rows,
        "store_rows": sorted(store_rows, key=lambda row: -int(row["unreferenced_bytes"])),
        "min_age_days": max(0, int(min_age_days)),
        "min_age_cutoff": datetime.fromtimestamp(age_cutoff, timezone.utc).isoformat(),
        "reference_roots": index["reference_roots"],
        "reference_document_count": index["reference_document_count"],
        "reference_document_bytes": index["reference_document_bytes"],
        "unreadable_reference_documents": index["unreadable_reference_documents"],
        "unreadable_reference_document_count": len(
            index["unreadable_reference_documents"]
        ),
        "skipped_self_plan_documents": index["skipped_self_plan_documents"],
        "body_hash_count": len(bodies),
        "referenced_hash_count": len(referenced),
        "unreferenced_files": unreferenced,
        "unreferenced_count": len(unreferenced),
        "unreferenced_bytes": sum(row["size_bytes"] for row in unreferenced),
        "withheld_young_files": withheld_young,
        "withheld_young_count": len(withheld_young),
        "withheld_young_bytes": sum(row["size_bytes"] for row in withheld_young),
        "in_store_evidence_count": len(evidence),
        "in_store_evidence_files": evidence[:200],
        "active_capture_processes": active_processes,
        "apply_allowed": not active_processes,
        "production_dependency": False,
        "external_archive_dependency": False,
        "raw_body_required_for_strict_replay": True,
        "cold_archive_is_nonproduction_only": True,
        "unreferenced_delete_requires_explicit_apply": True,
        "referenced_body_delete_forbidden": True,
        "in_store_json_delete_forbidden": True,
    }


def _native_provider(source_root: Path, store: Path) -> str:
    try:
        return store.relative_to(source_root).parts[0]
    except (ValueError, IndexError):
        return ""


def _native_manifest_root(plan: Mapping[str, Any], path: Path) -> Path:
    for root_text in plan.get("source_roots") or []:
        root = Path(str(root_text))
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return root
    raise RuntimeError(f"official_nwp_native_retention_root_unresolved:{path}")


def apply_native_unreferenced(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Discard the planned unreferenced bodies, manifest row written first.

    Referenced bodies are strict replay inputs and are never candidates here,
    exactly as in the curve lane.  The only additional protection this lane
    needs is the age floor already applied by :func:`build_native_plan`: an
    unreferenced body younger than that floor may still belong to a capture
    that has not written its packet yet.
    """

    if plan.get("active_capture_processes"):
        raise RuntimeError("official_nwp_native_retention_capture_active")
    deleted: list[dict[str, Any]] = []
    seen: set[str] = set()
    by_root: dict[Path, list[dict[str, Any]]] = {}
    recorded_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for raw in plan.get("unreferenced_files") or []:
        path = Path(str(raw.get("path") or ""))
        if str(path) in seen:
            continue
        seen.add(str(path))
        if not path.is_file() or path.is_symlink():
            continue
        expected = str(raw.get("sha256_name") or "").lower()
        actual = _sha256(path)
        if expected != actual:
            raise RuntimeError(f"official_nwp_native_retention_hash_mismatch:{path}")
        by_root.setdefault(_native_manifest_root(plan, path), []).append(
            {
                "schema": "official_nwp_raw_cold_archive_entry_v1",
                "state": "body_discarded",
                "recorded_at": recorded_at,
                "path": str(path),
                "sha256": actual,
                "size_bytes": path.stat().st_size,
                "provider": str(raw.get("provider") or Path(raw.get("store") or "").parent.name),
                "store": str(raw.get("store") or ""),
                "mtime": str(raw.get("mtime") or ""),
                "deletion_reason": "unreferenced_native_object_store_body",
                "lane": "native",
                "production_dependency": False,
                "strict_replay_body_available": False,
            }
        )
    for root, rows in by_root.items():
        _append_archive_manifest(root, rows)
    for rows in by_root.values():
        for row in rows:
            path = Path(str(row["path"]))
            if not path.is_file():
                continue
            path.unlink()
            deleted.append(
                {
                    "path": str(path),
                    "sha256": str(row["sha256"]),
                    "size_bytes": int(row["size_bytes"]),
                }
            )
    return {
        "deleted": deleted[:500],
        "deleted_count": len(deleted),
        "deleted_bytes": sum(item["size_bytes"] for item in deleted),
        "archive_manifest_paths": sorted(
            str(_archive_manifest_path(root)) for root in by_root
        ),
    }


def _run_native_lane(args: argparse.Namespace) -> dict[str, Any]:
    plan = build_native_plan(
        args.native_source_root or list(DEFAULT_NATIVE_SOURCE_ROOTS),
        reference_roots=args.reference_root or list(DEFAULT_NATIVE_REFERENCE_ROOTS),
        min_age_days=max(0, args.min_age_days),
        allow_unreadable_references=args.allow_unreadable_reference_documents,
    )
    if not args.apply:
        return plan
    if not args.purge_unreferenced:
        return {**plan, "state": "blocked_apply_confirmation_missing", "apply_allowed": False}
    if plan.get("active_capture_processes"):
        return {**plan, "state": "blocked_capture_in_progress", "apply_allowed": False}
    plan = {
        **plan,
        "state": "retention_apply_started",
        **apply_native_unreferenced(plan),
    }
    plan["state"] = "retention_apply_complete"
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lane",
        choices=("curve", "native"),
        default="curve",
        help="curve: the isolated curve-source evidence store. "
        "native: the content-addressed object stores under "
        "official_nwp_sources and model_arrivals.",
    )
    parser.add_argument("--curve-source-root", type=Path, default=DEFAULT_CURVE_ROOT)
    parser.add_argument(
        "--native-source-root",
        type=Path,
        action="append",
        help="native lane; repeatable, defaults to official_nwp_sources and model_arrivals",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        action="append",
        help="native lane; repeatable root scanned for hashes that keep a body alive",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=DEFAULT_NATIVE_MIN_AGE_DAYS,
        help="native lane; never discard a body younger than this in-flight-capture floor",
    )
    parser.add_argument(
        "--allow-unreadable-reference-documents",
        action="store_true",
        help="native lane; continue past reference documents this user cannot "
        "read and record them in the plan, instead of refusing the pass",
    )
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--retain-latest-runs", type=int, default=DEFAULT_RETAIN_LATEST_RUNS)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--purge-unreferenced", action="store_true")
    parser.add_argument("--acknowledge-missing", action="store_true", help="record pre-existing missing historical bodies in the non-production manifest")
    args = parser.parse_args()
    if args.lane == "native":
        plan = _run_native_lane(args)
        if args.audit_output:
            output = args.audit_output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0 if plan.get("state") in {"retention_plan_ready", "retention_apply_complete"} else 2
    plan = build_plan(
        args.curve_source_root,
        retention_days=max(0, args.retention_days),
        retain_latest_runs=max(0, args.retain_latest_runs),
    )
    if args.acknowledge_missing and plan.get("missing_raw_references"):
        acknowledge_missing_references(plan)
        plan = build_plan(
            args.curve_source_root,
            retention_days=max(0, args.retention_days),
            retain_latest_runs=max(0, args.retain_latest_runs),
        )
    if args.apply:
        if not args.purge_unreferenced:
            plan = {**plan, "state": "blocked_apply_confirmation_missing", "apply_allowed": False}
        elif plan.get("active_capture_processes"):
            plan = {**plan, "state": "blocked_capture_in_progress", "apply_allowed": False}
        elif plan.get("missing_raw_references"):
            plan = {**plan, "state": "blocked_missing_raw_reference", "apply_allowed": False}
        else:
            from official_nwp_request_control import try_acquire_capture_lease

            lease = try_acquire_capture_lease(Path(str(plan["curve_root"])))
            if lease is None:
                plan = {**plan, "state": "blocked_capture_lease_busy", "apply_allowed": False}
            else:
                try:
                    plan = {
                        **plan,
                        "state": "retention_apply_started",
                        **apply_unreferenced(plan),
                    }
                    plan["state"] = "retention_apply_complete"
                finally:
                    lease.close()
    if args.audit_output:
        output = args.audit_output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0 if plan.get("state") in {"retention_plan_ready", "retention_apply_complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
