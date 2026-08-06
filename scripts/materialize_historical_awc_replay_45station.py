#!/usr/bin/env python3
"""Materialize exact 45-station canonical historical AWC replay evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.historical_awc_replay import (  # noqa: E402
    MAX_INPUT_PACKETS,
    build_historical_awc_replay_evidence,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.pr12_authority_scope_projection import (  # noqa: E402
    validate_pr12_authority_scope_projection,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authority_timezones(authority_projection: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Read the sole 45-station settlement authority for historical replay.

    Historical source packets only locate official AWC raw objects.  They do
    not define settlement timezones.  Replaying their rows through the legacy
    station-local binding would create a second semantic truth lane, so this
    entry point accepts only the current sealed PR12 authority projection.
    """

    expected = sorted(coordinate_registry_manifest()["station_ids"])
    resolved = authority_projection.expanduser()
    if resolved.is_symlink():
        raise ContractError("historical_awc_authority_projection_symlink_forbidden")
    projection = validate_pr12_authority_scope_projection(
        load_bounded_json(resolved.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    )
    result: dict[str, str] = {}
    for row in projection["rows"]:
        station = str(row["station_id"]).upper()
        timezone_value = str(row["settlement_timezone"])
        if station in result or timezone_value != "America/New_York":
            raise ContractError("historical_awc_authority_projection_timezone_invalid")
        result[station] = timezone_value
    if sorted(result) != expected or projection["settlement_timezone"] != "America/New_York":
        raise ContractError("historical_awc_authority_projection_45station_scope_invalid")
    return result, {
        "path": str(resolved.resolve(strict=True)),
        "file_sha256": _file_sha256(resolved.resolve(strict=True)),
        "content_sha256": str(projection["content_sha256"]),
    }


def _source_packets(
    source_manifest: Path,
) -> tuple[list[dict[str, object]], list[dict[str, str]], dict[str, str]]:
    """Load the sealed locator set from one prior replay manifest.

    Only ``input_artifacts`` are reused.  The prior station outputs and their
    legacy timezones grant no authority to the new replay; every locator row
    is independently reverified against retained official AWC raw below.
    """

    supplied = source_manifest.expanduser()
    if supplied.is_symlink():
        raise ContractError("historical_awc_source_manifest_symlink_forbidden")
    resolved_manifest = supplied.resolve(strict=True)
    manifest = load_bounded_json(resolved_manifest, limit=MAX_JSON_INPUT_BYTES)
    if manifest.get("schema") != "bayes_basket_target_historical_awc_replay_45station_manifest_v2":
        raise ContractError("historical_awc_source_manifest_schema_invalid")
    manifest_identity = {
        key: value for key, value in manifest.items() if key != "content_sha256"
    }
    if manifest.get("content_sha256") != content_sha256(manifest_identity):
        raise ContractError("historical_awc_source_manifest_content_sha256_mismatch")
    artifacts = manifest.get("input_artifacts")
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or len(artifacts) > MAX_INPUT_PACKETS
    ):
        raise ContractError("historical_awc_source_manifest_input_artifacts_invalid")
    packets: list[dict[str, object]] = []
    pointers: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    cumulative_bytes = 0
    for raw_pointer in artifacts:
        if not isinstance(raw_pointer, dict) or set(raw_pointer) != {"path", "file_sha256"}:
            raise ContractError("historical_awc_source_manifest_pointer_invalid")
        artifact_path = Path(str(raw_pointer.get("path") or "")).expanduser()
        if artifact_path.is_symlink():
            raise ContractError("historical_awc_source_artifact_symlink_forbidden")
        resolved_artifact = artifact_path.resolve(strict=True)
        path_text = str(resolved_artifact)
        if path_text in seen_paths or not resolved_artifact.is_file():
            raise ContractError("historical_awc_source_artifact_duplicate_or_invalid")
        cumulative_bytes += resolved_artifact.stat().st_size
        if cumulative_bytes > MAX_JSON_INPUT_BYTES:
            raise ContractError("historical_awc_source_artifact_cumulative_bytes_exceeded")
        actual_sha = _file_sha256(resolved_artifact)
        if actual_sha != raw_pointer.get("file_sha256"):
            raise ContractError("historical_awc_source_artifact_file_sha256_mismatch")
        packet = load_bounded_json(resolved_artifact, limit=MAX_JSON_INPUT_BYTES)
        if not isinstance(packet, dict):
            raise ContractError("historical_awc_source_artifact_mapping_required")
        seen_paths.add(path_text)
        packets.append(packet)
        pointers.append({"path": path_text, "file_sha256": actual_sha})
    return packets, pointers, {
        "path": str(resolved_manifest),
        "file_sha256": _file_sha256(resolved_manifest),
        "content_sha256": str(manifest["content_sha256"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--authority-projection", type=Path, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--station-id", action="append", default=[],
        help="Optional exact station projection; source authority remains 45-station.",
    )
    args = parser.parse_args()
    timezones, authority_pointer = _authority_timezones(args.authority_projection)
    requested_stations = {
        str(value or "").strip().upper()
        for value in args.station_id
        if str(value or "").strip()
    }
    if requested_stations:
        if not requested_stations.issubset(timezones):
            raise ContractError("historical_awc_station_projection_invalid")
        timezones = {
            station: timezone_name
            for station, timezone_name in timezones.items()
            if station in requested_stations
        }
    packets, input_artifacts, source_manifest_pointer = _source_packets(
        args.source_manifest
    )
    output_root = args.output_root.expanduser().resolve()
    station_rows = []
    for station, timezone_name in sorted(timezones.items()):
        evidence = build_historical_awc_replay_evidence(
            packets=packets,
            raw_root=args.raw_root,
            station_id=station,
            settlement_timezone=timezone_name,
            start_date=args.start_date,
            end_date=args.end_date,
            as_of_utc=args.as_of_utc,
        )
        path = output_root / station / "historical_awc_replay_evidence.json"
        write_bounded_json(path, evidence)
        station_rows.append({
            "station_id": station,
            "settlement_timezone": timezone_name,
            "path": str(path),
            "file_sha256": _file_sha256(path),
            "content_sha256": evidence["evidence_sha256"],
            "observation_count": len(evidence["observations"]),
            "observed_date_count": len(evidence["observed_dates"]),
            "truth_ready_date_count": len(evidence["truth_ready_dates"]),
            "truth_blocked_dates": evidence["truth_blocked_dates"],
        })
    identity = {
        "schema": "bayes_basket_target_historical_awc_replay_45station_manifest_v2",
        "as_of_utc": args.as_of_utc,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "settlement_timezone": "America/New_York",
        "authority_scope_projection": authority_pointer,
        "source_locator_manifest": source_manifest_pointer,
        "station_count": len(station_rows),
        "authority_station_count": 45,
        "station_projection": sorted(timezones),
        "input_artifacts": input_artifacts,
        "stations": station_rows,
        "summary": {
            "observation_count": sum(row["observation_count"] for row in station_rows),
            "observed_station_day_count": sum(row["observed_date_count"] for row in station_rows),
            "truth_ready_station_day_count": sum(row["truth_ready_date_count"] for row in station_rows),
            "complete_station_count": sum(not row["truth_blocked_dates"] for row in station_rows),
            "blocked_station_count": sum(bool(row["truth_blocked_dates"]) for row in station_rows),
        },
        "production_no_live": True,
        "writes_production_db": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    manifest = {**identity, "content_sha256": content_sha256(identity)}
    manifest_path = output_root / "manifest.json"
    write_bounded_json(manifest_path, manifest)
    print(json.dumps({
        "manifest": str(manifest_path),
        "content_sha256": manifest["content_sha256"],
        **manifest["summary"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
