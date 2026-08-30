#!/usr/bin/env python3
"""Refresh the manifest for the merged TAF exact-join quarantine directory.

The merged directory is assembled from already-retained local artifacts.  A
previous partial rebuild left its ``manifest.json`` describing only the first
15 files.  This bounded, read-only scanner rebuilds that pointer from the
actual 45-station audit and the files currently present; it never opens the
database, fetches data, or changes P0/P0-M.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "taf_native_exact_join_quarantine_manifest_v2"
MAX_STATIONS = expected_station_count()
MAX_ARTIFACTS = expected_station_count()
ARTIFACT_SUFFIX = "_taf_native_exact_join.json"


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise RuntimeError(f"mapping_required:{path}")
    return value


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _artifact_record(path: Path) -> dict[str, Any]:
    payload = _mapping(path)
    station = str(payload.get("station_id") or "").strip().upper()
    if not station:
        raise RuntimeError(f"artifact_station_missing:{path}")
    supplied = str(payload.get("content_sha256") or "").lower()
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    if len(supplied) != 64 or supplied != content_sha256(identity):
        raise RuntimeError(f"artifact_content_sha256_mismatch:{path}")
    run_cycle = str(payload.get("run_cycle") or "")
    expected_names = {
        f"{station}_{payload.get('season_bin')}_{payload.get('lead_bucket')}_"
        f"{run_cycle}{ARTIFACT_SUFFIX}",
        f"{station}_{payload.get('season_bin')}_{payload.get('lead_bucket')}_"
        f"{run_cycle.replace('_', '-')}{ARTIFACT_SUFFIX}",
    }
    if str(path.name) not in expected_names:
        raise RuntimeError(f"artifact_filename_scope_mismatch:{path}")
    return {
        "station_id": station,
        "path": _relative(path),
        "file_sha256": _sha_file(path),
        "content_sha256": supplied,
        "schema": str(payload.get("schema") or ""),
        "scope": {
            "season_bin": str(payload.get("season_bin") or ""),
            "lead_bucket": str(payload.get("lead_bucket") or ""),
            "run_cycle": str(payload.get("run_cycle") or ""),
            "settlement_definition": str(payload.get("settlement_definition") or ""),
            "settlement_timezone": str(payload.get("settlement_timezone") or ""),
        },
        "sample_count": int(payload.get("sample_count") or 0),
        "excluded_correction_sample_count": int(
            payload.get("excluded_correction_sample_count") or 0
        ),
        "observational_forward_event_count": int(
            payload.get("observational_forward_event_count") or 0
        ),
        "source_class": str(payload.get("source_class") or ""),
    }


def build_manifest(*, artifact_dir: Path, audit_path: Path) -> dict[str, Any]:
    canonical = sorted({
        str(value).strip().upper()
        for value in coordinate_registry_manifest()["station_ids"]
    })
    if len(canonical) != MAX_STATIONS:
        raise RuntimeError("canonical_station_count_invalid")
    audit = _mapping(audit_path)
    if audit.get("canonical_station_count") != MAX_STATIONS:
        raise RuntimeError("audit_canonical_station_count_invalid")
    audit_entries = audit.get("entries")
    if not isinstance(audit_entries, list) or len(audit_entries) != MAX_STATIONS:
        raise RuntimeError("audit_entries_invalid")
    audit_by_station: dict[str, Mapping[str, Any]] = {}
    for entry in audit_entries:
        if not isinstance(entry, Mapping):
            raise RuntimeError("audit_entry_invalid")
        station = str(entry.get("station_id") or "").strip().upper()
        if station in audit_by_station or station not in canonical:
            raise RuntimeError("audit_station_scope_invalid")
        audit_by_station[station] = entry
    files = sorted(artifact_dir.glob(f"*{ARTIFACT_SUFFIX}"))
    if len(files) > MAX_ARTIFACTS:
        raise RuntimeError("artifact_count_exceeded")
    artifacts = [_artifact_record(path) for path in files]
    by_station = {row["station_id"]: row for row in artifacts}
    if len(by_station) != len(artifacts):
        raise RuntimeError("artifact_station_duplicate")
    if set(by_station) - set(canonical):
        raise RuntimeError("artifact_station_not_canonical")
    missing = sorted(set(canonical) - set(by_station))
    missing_entries: list[dict[str, Any]] = []
    for station in missing:
        audit_entry = audit_by_station[station]
        if "exact_join_artifact_missing" not in list(audit_entry.get("blockers") or []):
            raise RuntimeError(f"missing_artifact_not_explained:{station}")
        missing_entries.append({
            "station_id": station,
            "reason": "exact_join_artifact_missing",
            "audit_blockers": sorted(str(value) for value in audit_entry.get("blockers") or []),
            "current_p0_scope": audit_entry.get("current_p0_scope"),
            "native_oos_sample_count": int(audit_entry.get("sample_count") or 0),
            "minimum_sample_n": int(audit_entry.get("minimum_sample_n") or 0),
            "data_fetch_performed": False,
            "synthetic_artifact_created": False,
        })
    identity = {
        "schema": SCHEMA,
        "version": 2,
        "output_dir": _relative(artifact_dir),
        "base_dir": _relative(artifact_dir),
        "source_audit": {
            "path": _relative(audit_path),
            "file_sha256": _sha_file(audit_path),
            "content_sha256": str(audit.get("content_sha256") or ""),
        },
        "canonical_station_count": MAX_STATIONS,
        "canonical_station_ids": canonical,
        "exact_join_artifact_count": len(artifacts),
        "artifact_count": len(artifacts),
        "error_count": 0,
        "artifact_station_ids": sorted(by_station),
        "missing_exact_join_station_ids": missing,
        "artifacts": sorted(artifacts, key=lambda row: row["station_id"]),
        "missing_entries": missing_entries,
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "writes_database": False,
            "writes_production_p0m": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "native_arrival_index_written": False,
            "p1_authority_written": False,
            "future_data_counted_as_oos": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.expanduser().resolve(strict=True)
    audit_path = args.audit.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    if output in {artifact_dir, audit_path}:
        raise RuntimeError("manifest_output_input_collision")
    manifest = build_manifest(artifact_dir=artifact_dir, audit_path=audit_path)
    wrote = write_bounded_json_if_changed(
        output, manifest, limit=MAX_JSON_OUTPUT_BYTES
    )
    print(json.dumps({
        "schema": SCHEMA,
        "state": "REFRESHED_READ_ONLY",
        "exact_join_artifact_count": manifest["exact_join_artifact_count"],
        "missing_exact_join_station_ids": manifest["missing_exact_join_station_ids"],
        "output": str(output),
        "wrote": wrote,
        "content_sha256": manifest["content_sha256"],
        "network_fetches": 0,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
