#!/usr/bin/env python3
"""Record one target-only prospective native-model arrival.

This command is intentionally explicit about provider publication time.  It
does not infer that time from a filename, filesystem mtime, run initialization,
or retrospective request time.  Existing historical receipts therefore emit a
BLOCKED receipt until their source collector has observed the required native
arrival metadata prospectively.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.model_arrival import (  # noqa: E402
    ALLOWED_PROVIDERS,
    build_blocked_receipt,
    validate_model_arrival_capture,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_payload_hashes(receipt: dict[str, Any]) -> list[str]:
    hashes: list[str] = []
    for item in receipt.get("raw_receipts") or []:
        if isinstance(item, dict) and item.get("raw_sha256"):
            hashes.append(str(item["raw_sha256"]).strip().lower())
    return sorted(set(hashes))


def _source_provider_matches(receipt_provider: str, target_provider: str) -> bool:
    aliases = {
        "ecmwf_ifs025_official": "ecmwf",
        "gfs_0p25_official": "gfs",
        "icon_global_official": "icon",
    }
    return receipt_provider == target_provider or receipt_provider == aliases.get(target_provider)


def record(
    *,
    receipt_path: Path,
    provider_id: str,
    station_id: str,
    target_local_date: str,
    run_initialization_utc: str,
    valid_time_utc: str,
    published_at_utc: str,
    received_at_utc: str,
    decision_at_utc: str,
    source_url: str,
    target_timezone: str,
) -> dict[str, Any]:
    try:
        source_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(source_receipt, dict):
            raise ContractError("source_receipt_mapping_required")
        source_provider = str(source_receipt.get("provider_id") or "").strip().lower()
        if not _source_provider_matches(source_provider, provider_id):
            raise ContractError("source_receipt_provider_mismatch")
        if source_receipt.get("capture_mode") != "prospective":
            raise ContractError("source_receipt_capture_not_prospective")
        if source_receipt.get("historical_arrival_time_observed") is not True:
            raise ContractError("source_receipt_historical_arrival_time_not_observed")
        if source_receipt.get("aggregator_or_republisher") is not False:
            raise ContractError("source_receipt_aggregator_forbidden")
        if source_receipt.get("open_meteo") is not False:
            raise ContractError("source_receipt_open_meteo_forbidden")
        if source_receipt.get("publication_time_source") != "provider_http_last_modified":
            raise ContractError("source_receipt_publication_time_source_invalid")
        payload_hashes = _raw_payload_hashes(source_receipt)
        if not payload_hashes:
            raise ContractError("source_receipt_raw_payload_sha256_missing")
        receipt_sha = _sha256(receipt_path)
        model_id = ALLOWED_PROVIDERS[provider_id]
        identity = {
            "arrival_id": "arrival-" + content_sha256({
                "provider_id": provider_id,
                "station_id": station_id,
                "target_local_date": target_local_date,
                "run_initialization_utc": run_initialization_utc,
                "receipt_sha256": receipt_sha,
            })[:24],
            "provider_id": provider_id,
            "model_id": model_id,
            "station_id": station_id,
            "target_local_date": target_local_date,
            "target_timezone": target_timezone,
            "run_initialization_utc": run_initialization_utc,
            "valid_time_utc": valid_time_utc,
            "published_at_utc": published_at_utc,
            "received_at_utc": received_at_utc,
            # Preserve the immutable capture decision separately from the
            # later formal-consumption clock. The transport receipt proves
            # response completion; fanout cannot rewrite that arrival time.
            "decision_at_utc": received_at_utc,
            "publication_time_source": "provider_http_last_modified",
            "source_url": source_url,
            "raw_payload_sha256s": payload_hashes,
            "receipt_sha256": receipt_sha,
            "capture_mode": "prospective",
            "historical_arrival_time_observed": True,
            "aggregator_or_republisher": False,
            "open_meteo": False,
        }
        return {
            "status": "CAPTURED",
            # Station fan-out is P0 preparation and may happen before the ET
            # event day. P1/formal fact consumption keeps its separate ET
            # gate; this record never grants production authority.
            "arrival": validate_model_arrival_capture(identity, decision_at_utc=decision_at_utc),
            "formal_consumed_at_utc": decision_at_utc,
            # Keep the transport receipt reachable from every station fan-out
            # row.  The native decoder verifies this exact receipt and its raw
            # object hashes; without the reference a valid arrival would be
            # unusable downstream and fail closed as an unreadable input.
            "transport_receipt_path": str(receipt_path.expanduser().resolve()),
            "boundary": {
                "target_only": True,
                "writes_production_db": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "p0_station_lineage_only": True,
                "p1_probability_input_authorized": False,
                "live_order_authorized": False,
            },
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ContractError) as exc:
        return {
            "status": "BLOCKED",
            "receipt": build_blocked_receipt(reason_codes=[str(exc)], received_at_utc=_now()),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--provider", choices=tuple(ALLOWED_PROVIDERS), required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--valid-time-utc", required=True)
    parser.add_argument("--published-at-utc", required=True)
    parser.add_argument("--received-at-utc", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--target-timezone", default="America/New_York")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = record(
        receipt_path=args.receipt.expanduser().resolve(),
        provider_id=args.provider,
        station_id=args.station_id,
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        valid_time_utc=args.valid_time_utc,
        published_at_utc=args.published_at_utc,
        received_at_utc=args.received_at_utc,
        decision_at_utc=args.decision_at_utc,
        source_url=args.source_url,
        target_timezone=args.target_timezone,
    )
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "CAPTURED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
