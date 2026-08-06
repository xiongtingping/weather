#!/usr/bin/env python3
"""Build one atomic 45-station historical AWC truth contract bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.historical_awc_truth_contract import (  # noqa: E402
    build_historical_awc_truth_contract,
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


def _manifest_evidences(
    manifest_path: Path,
    *,
    expected_station_ids: Iterable[str] | None = None,
) -> tuple[list[Mapping[str, Any]], list[dict[str, str]], str]:
    manifest = load_bounded_json(manifest_path, limit=MAX_JSON_INPUT_BYTES)
    if manifest.get("schema") != "bayes_basket_target_historical_awc_replay_45station_manifest_v2":
        raise ContractError("historical_awc_truth_manifest_schema_invalid")
    supplied = manifest.get("content_sha256")
    identity = {key: value for key, value in manifest.items() if key != "content_sha256"}
    if not isinstance(supplied, str) or supplied != content_sha256(identity):
        raise ContractError("historical_awc_truth_manifest_sha256_mismatch")
    if manifest.get("settlement_timezone") != "America/New_York":
        raise ContractError("historical_awc_truth_manifest_timezone_invalid")
    authority_pointer = manifest.get("authority_scope_projection")
    if not isinstance(authority_pointer, Mapping) or set(authority_pointer) != {
        "path", "file_sha256", "content_sha256",
    }:
        raise ContractError("historical_awc_truth_authority_pointer_invalid")
    authority_path = Path(str(authority_pointer.get("path") or "")).expanduser()
    if authority_path.is_symlink():
        raise ContractError("historical_awc_truth_authority_symlink_forbidden")
    authority_path = authority_path.resolve(strict=True)
    if _file_sha256(authority_path) != authority_pointer.get("file_sha256"):
        raise ContractError("historical_awc_truth_authority_file_sha256_mismatch")
    projection = validate_pr12_authority_scope_projection(
        load_bounded_json(authority_path, limit=MAX_JSON_INPUT_BYTES)
    )
    if projection["content_sha256"] != authority_pointer.get("content_sha256"):
        raise ContractError("historical_awc_truth_authority_content_sha256_mismatch")
    source_pointer = manifest.get("source_locator_manifest")
    if not isinstance(source_pointer, Mapping) or set(source_pointer) != {
        "path", "file_sha256", "content_sha256",
    }:
        raise ContractError("historical_awc_truth_source_locator_pointer_invalid")
    source_path = Path(str(source_pointer.get("path") or "")).expanduser()
    if source_path.is_symlink():
        raise ContractError("historical_awc_truth_source_locator_symlink_forbidden")
    source_path = source_path.resolve(strict=True)
    if _file_sha256(source_path) != source_pointer.get("file_sha256"):
        raise ContractError("historical_awc_truth_source_locator_file_sha256_mismatch")
    source_manifest = load_bounded_json(source_path, limit=MAX_JSON_INPUT_BYTES)
    source_identity = {
        key: value for key, value in source_manifest.items() if key != "content_sha256"
    }
    if (
        source_manifest.get("content_sha256") != content_sha256(source_identity)
        or source_manifest.get("content_sha256") != source_pointer.get("content_sha256")
        or source_manifest.get("input_artifacts") != manifest.get("input_artifacts")
    ):
        raise ContractError("historical_awc_truth_source_locator_manifest_invalid")
    station_rows = manifest.get("stations")
    registry_stations = sorted(coordinate_registry_manifest()["station_ids"])
    expected = (
        registry_stations
        if expected_station_ids is None
        else sorted({str(item).strip().upper() for item in expected_station_ids})
    )
    if not expected or any(station not in registry_stations for station in expected):
        raise ContractError("historical_awc_truth_station_projection_invalid")
    if not isinstance(station_rows, list) or sorted(row.get("station_id") for row in station_rows if isinstance(row, Mapping)) != expected:
        raise ContractError("historical_awc_truth_manifest_station_projection_mismatch")
    authority_stations = sorted(str(row["station_id"]) for row in projection["rows"])
    if not set(expected).issubset(authority_stations):
        raise ContractError("historical_awc_truth_authority_station_scope_invalid")
    evidences: list[Mapping[str, Any]] = []
    pointers: list[dict[str, str]] = []
    for row in sorted(station_rows, key=lambda item: item["station_id"]):
        if row.get("settlement_timezone") != "America/New_York":
            raise ContractError("historical_awc_truth_station_timezone_invalid")
        path = Path(str(row.get("path") or "")).expanduser()
        if path.is_symlink():
            raise ContractError("historical_awc_truth_evidence_symlink_forbidden")
        path = path.resolve(strict=True)
        if _file_sha256(path) != row.get("file_sha256"):
            raise ContractError("historical_awc_truth_evidence_file_sha256_mismatch")
        evidence = load_bounded_json(path, limit=MAX_JSON_INPUT_BYTES)
        if (
            evidence.get("station_id") != row.get("station_id")
            or evidence.get("target_timezone") != "America/New_York"
        ):
            raise ContractError("historical_awc_truth_evidence_timezone_invalid")
        if evidence.get("evidence_sha256") != row.get("content_sha256"):
            raise ContractError("historical_awc_truth_evidence_content_sha256_mismatch")
        evidences.append(evidence)
        pointers.append(
            {
                "station_id": str(row["station_id"]),
                "path": str(path),
                "file_sha256": str(row["file_sha256"]),
                "content_sha256": str(row["content_sha256"]),
            }
        )
    return evidences, pointers, str(manifest["as_of_utc"])


def build(
    manifest_path: Path,
    *,
    contract_output: Path,
    receipt_output: Path,
    station_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_station_ids = [station_id.upper()] if station_id else None
    evidences, pointers, as_of_utc = _manifest_evidences(
        manifest_path,
        expected_station_ids=expected_station_ids,
    )
    contract, receipt = build_historical_awc_truth_contract(
        evidences,
        as_of_utc=as_of_utc,
    )
    receipt_identity = {key: value for key, value in receipt.items() if key != "content_sha256"}
    receipt_identity["source_manifest"] = {
        "path": str(manifest_path.resolve()),
        "file_sha256": _file_sha256(manifest_path),
        "content_sha256": str(
            load_bounded_json(manifest_path, limit=MAX_JSON_INPUT_BYTES)[
                "content_sha256"
            ]
        ),
        "authority_scope_projection": load_bounded_json(
            manifest_path, limit=MAX_JSON_INPUT_BYTES
        )["authority_scope_projection"],
        "source_locator_manifest": load_bounded_json(
            manifest_path, limit=MAX_JSON_INPUT_BYTES
        )["source_locator_manifest"],
        "station_evidence": pointers,
    }
    receipt = {**receipt_identity, "content_sha256": content_sha256(receipt_identity)}
    write_bounded_json(contract_output.resolve(), contract)
    write_bounded_json(receipt_output.resolve(), receipt)
    return contract, receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument(
        "--station-id",
        help="Optional exact station pilot projection; authority source remains global.",
    )
    args = parser.parse_args()
    contract, receipt = build(
        args.manifest.expanduser().resolve(strict=True),
        contract_output=args.contract_output.expanduser(),
        receipt_output=args.receipt_output.expanduser(),
        station_id=args.station_id,
    )
    print(json.dumps({
        "contract_content_sha256": contract["content_sha256"],
        "receipt_content_sha256": receipt["content_sha256"],
        "truth_fact_count": receipt["truth_fact_count"],
        "complete_station_count": receipt["complete_station_count"],
        "blocked_station_count": receipt["blocked_station_count"],
        "writes_target_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
