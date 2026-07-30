#!/usr/bin/env python3
"""Audit and optionally purge legacy future-day target model residue.

The target model-arrival collector now rejects a not-yet-started ET day before
network I/O or raw-file creation.  Older collectors did not have that guard
and left transport receipts and raw objects for future ET days.  This command
only considers those legacy objects, proves that their raw bodies are not
shared with any non-residue receipt, writes a compact immutable audit packet,
and optionally removes the proven-unreferenced bodies and receipts.

It never reads or writes SQLite, shared production storage, service state, or
an order gateway.  The default is read-only; ``--purge`` is intentionally
required for the narrowly scoped, recoverability-reducing operation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_future_model_residue_cleanup_v1"
DEFAULT_ROOT = ROOT / "data" / "research" / "bayes_basket_target" / "model_arrivals"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "bayes_basket_target_future_model_residue_cleanup.json"
PROVIDERS = (
    "ecmwf_ifs025_official",
    "gfs_0p25_official",
    "icon_global_official",
)


def _utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("future_residue_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("future_residue_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _receipt_candidate(
    *,
    provider: str,
    root: Path,
    receipt_path: Path,
    value: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    blockers: list[str] = []
    if value.get("schema") != "bayes_basket_target_native_model_transport_receipt_v1":
        return None, ["receipt_schema_invalid"]
    target_date = str(value.get("target_local_date") or "").strip()
    capture_started = str(value.get("capture_started_at_utc") or "").strip()
    if not target_date or not capture_started:
        return None, ["receipt_target_identity_missing"]
    try:
        state = event_day_window(target_date, as_of_utc=capture_started)["state"]
    except Exception:
        return None, ["receipt_event_day_window_invalid"]
    if state != "not_started":
        return None, []
    raw_items = value.get("raw_receipts")
    if not isinstance(raw_items, list) or not raw_items:
        return None, ["future_receipt_raw_receipts_missing"]
    raw_rows: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            blockers.append("future_receipt_raw_receipt_invalid")
            continue
        ref = Path(str(raw.get("raw_object_ref") or "")).expanduser().resolve()
        sha = str(raw.get("raw_sha256") or "").strip().lower()
        raw_rows.append({"path": str(ref), "sha256": sha})
        expected_root = (root / provider / "raw").resolve()
        if not _inside(ref, expected_root):
            blockers.append("future_raw_path_outside_provider_root")
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            blockers.append("future_raw_sha256_invalid")
        elif not ref.is_file():
            blockers.append("future_raw_object_missing")
        elif _sha256(ref) != sha:
            blockers.append("future_raw_sha256_mismatch")
    if blockers:
        return None, sorted(set(blockers))
    return {
        "provider_id": provider,
        "receipt_path": str(receipt_path.resolve()),
        "receipt_sha256": _sha256(receipt_path),
        "target_local_date": target_date,
        "run_initialization_utc": str(value.get("run_initialization_utc") or ""),
        "valid_time_utc": str(value.get("valid_time_utc") or ""),
        "station_id": str(value.get("station_id") or ""),
        "capture_started_at_utc": capture_started,
        "raw_objects": raw_rows,
    }, []


def build(*, root: Path, as_of_utc: str, purge: bool) -> dict[str, Any]:
    root = root.expanduser().resolve()
    _utc(as_of_utc)
    if not root.is_dir():
        raise ContractError("future_residue_root_missing")

    candidates: list[dict[str, Any]] = []
    all_raw_to_receipts: dict[str, list[tuple[Path, str]]] = {}
    receipt_values: list[tuple[str, Path, Mapping[str, Any]]] = []
    blockers: list[str] = []
    for provider in PROVIDERS:
        receipts_root = root / provider / "receipts"
        raw_root = (root / provider / "raw").resolve()
        if not receipts_root.is_dir() or not raw_root.is_dir():
            continue
        for receipt_path in sorted(receipts_root.glob("*.json")):
            try:
                value = load_bounded_json(receipt_path)
            except Exception as exc:
                blockers.append(f"{provider}:{receipt_path.name}:receipt_unreadable:{type(exc).__name__}")
                continue
            if not isinstance(value, Mapping):
                blockers.append(f"{provider}:{receipt_path.name}:receipt_not_mapping")
                continue
            receipt_values.append((provider, receipt_path, value))
            for raw in value.get("raw_receipts") or []:
                if isinstance(raw, Mapping):
                    sha = str(raw.get("raw_sha256") or "").strip().lower()
                    if sha:
                        all_raw_to_receipts.setdefault(sha, []).append((receipt_path, str(value.get("target_local_date") or "")))
            candidate, candidate_blockers = _receipt_candidate(
                provider=provider,
                root=root,
                receipt_path=receipt_path,
                value=value,
            )
            if candidate is not None:
                candidates.append(candidate)
            blockers.extend(f"{provider}:{receipt_path.name}:{reason}" for reason in candidate_blockers)

    candidate_receipt_paths = {str(item["receipt_path"]) for item in candidates}
    candidate_raw: dict[str, Path] = {}
    unsafe_raw: set[str] = set()
    for item in candidates:
        for raw in item["raw_objects"]:
            sha = str(raw["sha256"])
            candidate_raw[sha] = Path(str(raw["path"])).resolve()
    for sha in candidate_raw:
        owners = all_raw_to_receipts.get(sha, [])
        if any(str(path.resolve()) not in candidate_receipt_paths for path, _ in owners):
            unsafe_raw.add(sha)
            blockers.append(f"future_raw_shared_with_non_residue:{sha}")

    safe_candidates = [
        item for item in candidates
        if all(str(raw["sha256"]) not in unsafe_raw for raw in item["raw_objects"])
    ]
    safe_receipt_paths = [Path(str(item["receipt_path"])).resolve() for item in safe_candidates]
    safe_raw_paths = sorted({candidate_raw[str(raw["sha256"])] for item in safe_candidates for raw in item["raw_objects"]})

    body: dict[str, Any] = {
        "schema": SCHEMA,
        "as_of_utc": as_of_utc,
        "target_timezone": "America/New_York",
        "root": str(root),
        "mode": "purge" if purge else "audit_only",
        "definition": "receipt capture_started_at_utc was before target ET day start",
        "candidate_receipt_count": len(candidates),
        "safe_receipt_count": len(safe_receipt_paths),
        "candidate_raw_object_count": len(candidate_raw),
        "safe_raw_object_count": len(safe_raw_paths),
        "candidate_raw_bytes": sum(path.stat().st_size for path in candidate_raw.values() if path.is_file()),
        "safe_raw_bytes": sum(path.stat().st_size for path in safe_raw_paths if path.is_file()),
        "residue": [
            {
                "provider_id": item["provider_id"],
                "receipt_sha256": item["receipt_sha256"],
                "target_local_date": item["target_local_date"],
                "run_initialization_utc": item["run_initialization_utc"],
                "valid_time_utc": item["valid_time_utc"],
                "station_id": item["station_id"],
                "capture_started_at_utc": item["capture_started_at_utc"],
                "raw_sha256": sorted(str(raw["sha256"]) for raw in item["raw_objects"]),
            }
            for item in sorted(safe_candidates, key=lambda row: (row["provider_id"], row["target_local_date"], row["valid_time_utc"], row["station_id"], row["receipt_sha256"]))
        ],
        "checks": {
            "all_candidate_receipts_are_pre_start_residue": all(
                _utc(item["capture_started_at_utc"]) < _utc(event_day_window(item["target_local_date"], as_of_utc=item["capture_started_at_utc"])["target_day_start_utc"])
                for item in candidates
            ),
            "no_candidate_raw_shared_with_non_residue": not unsafe_raw,
            "protected_production_line_touched": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "purged": False,
        },
        "blockers": sorted(set(blockers)),
    }
    if blockers:
        body["checks"]["purged"] = False
        return {**body, "audit_sha256": content_sha256(body)}
    if not purge:
        return {**body, "audit_sha256": content_sha256(body)}

    for path in safe_receipt_paths + safe_raw_paths:
        if not path.is_file() or path.is_symlink():
            raise ContractError("future_residue_purge_target_changed")
    for path in safe_receipt_paths + safe_raw_paths:
        path.unlink()
    body["checks"]["purged"] = True
    body["purged_receipt_count"] = len(safe_receipt_paths)
    body["purged_raw_object_count"] = len(safe_raw_paths)
    body["purged_raw_bytes"] = int(body["safe_raw_bytes"])
    return {**body, "audit_sha256": content_sha256(body)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if args.purge:
        # Persist the exact planned scope before unlinking anything.  If the
        # final receipt write fails after the purge, the pending packet still
        # proves the complete set that was intentionally selected.
        planned = build(root=args.root, as_of_utc=args.as_of_utc, purge=False)
        planned["mode"] = "purge_plan"
        planned["audit_sha256"] = content_sha256(planned)
        write_bounded_json(output.with_name(output.name + ".pending"), planned)
    result = build(root=args.root, as_of_utc=args.as_of_utc, purge=args.purge)
    write_bounded_json(output, result)
    print(json.dumps({
        "schema": result["schema"],
        "mode": result["mode"],
        "candidate_receipt_count": result["candidate_receipt_count"],
        "safe_receipt_count": result["safe_receipt_count"],
        "safe_raw_object_count": result["safe_raw_object_count"],
        "safe_raw_bytes": result["safe_raw_bytes"],
        "purged": result["checks"]["purged"],
        "blockers": result["blockers"],
        "audit_sha256": result["audit_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not result["blockers"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
