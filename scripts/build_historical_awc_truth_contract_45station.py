#!/usr/bin/env python3
"""Build one atomic 45-station historical AWC truth contract bundle."""

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

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.historical_awc_truth_contract import (  # noqa: E402
    build_historical_awc_truth_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_evidences(manifest_path: Path) -> tuple[list[Mapping[str, Any]], list[dict[str, str]], str]:
    manifest = load_bounded_json(manifest_path, limit=MAX_JSON_INPUT_BYTES)
    if manifest.get("schema") != "bayes_basket_target_historical_awc_replay_45station_manifest_v2":
        raise ContractError("historical_awc_truth_manifest_schema_invalid")
    supplied = manifest.get("content_sha256")
    identity = {key: value for key, value in manifest.items() if key != "content_sha256"}
    if not isinstance(supplied, str) or supplied != content_sha256(identity):
        raise ContractError("historical_awc_truth_manifest_sha256_mismatch")
    station_rows = manifest.get("stations")
    expected = sorted(coordinate_registry_manifest()["station_ids"])
    if not isinstance(station_rows, list) or sorted(row.get("station_id") for row in station_rows if isinstance(row, Mapping)) != expected:
        raise ContractError("historical_awc_truth_manifest_exact_45_required")
    evidences: list[Mapping[str, Any]] = []
    pointers: list[dict[str, str]] = []
    for row in sorted(station_rows, key=lambda item: item["station_id"]):
        path = Path(str(row.get("path") or "")).expanduser()
        if path.is_symlink():
            raise ContractError("historical_awc_truth_evidence_symlink_forbidden")
        path = path.resolve(strict=True)
        if _file_sha256(path) != row.get("file_sha256"):
            raise ContractError("historical_awc_truth_evidence_file_sha256_mismatch")
        evidence = load_bounded_json(path, limit=MAX_JSON_INPUT_BYTES)
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


def build(manifest_path: Path, *, contract_output: Path, receipt_output: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    evidences, pointers, as_of_utc = _manifest_evidences(manifest_path)
    contract, receipt = build_historical_awc_truth_contract(
        evidences,
        as_of_utc=as_of_utc,
    )
    receipt_identity = {key: value for key, value in receipt.items() if key != "content_sha256"}
    receipt_identity["source_manifest"] = {
        "path": str(manifest_path.resolve()),
        "file_sha256": _file_sha256(manifest_path),
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
    args = parser.parse_args()
    contract, receipt = build(
        args.manifest.expanduser().resolve(strict=True),
        contract_output=args.contract_output.expanduser(),
        receipt_output=args.receipt_output.expanduser(),
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
