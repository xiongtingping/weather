#!/usr/bin/env python3
"""Plan and safely prune official-NWP raw objects.

The curve root is an isolated source-evidence store.  Raw objects referenced by
current/history/receipt/revision JSON are immutable replay inputs, regardless
of run age, and are never deleted by retention.  Only genuinely unreferenced
objects may be discarded after their content hash is written to a local JSONL
manifest.  The immutable receipts and normalized snapshots are never edited.
That manifest is not a production dependency and is deliberately excluded
from the production readiness, posterior, and trading paths.
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--curve-source-root", type=Path, default=DEFAULT_CURVE_ROOT)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--retain-latest-runs", type=int, default=DEFAULT_RETAIN_LATEST_RUNS)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--purge-unreferenced", action="store_true")
    parser.add_argument("--acknowledge-missing", action="store_true", help="record pre-existing missing historical bodies in the non-production manifest")
    args = parser.parse_args()
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
