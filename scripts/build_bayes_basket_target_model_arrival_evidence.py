#!/usr/bin/env python3
"""Build the append-only target SSOT for prospective model arrivals.

The inputs are capture receipts produced by the target-only native-provider
collector.  Blocked captures remain visible in the audit, while only validated
arrivals enter the index consumed by calibration.  This command never reads
SQLite, writes production state, downloads data, or calls an order gateway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_arrival import (  # noqa: E402
    build_blocked_receipt,
    load_model_arrival_receipts,
)
from bayes_basket_target.resource_bounds import (
    MAX_INPUT_FILES_PER_BATCH,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_model_arrival_evidence_v1"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_model_arrival_evidence.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(*, capture_paths: list[Path], as_of_utc: str) -> dict[str, Any]:
    if len(capture_paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("target_capture_batch_size_exceeded")
    arrivals: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    input_artifacts: list[dict[str, str]] = []
    for raw_path in capture_paths:
        path = raw_path.expanduser().resolve()
        try:
            value = load_bounded_json(path)
            if not isinstance(value, dict):
                raise ContractError("capture_result_mapping_required")
            input_artifacts.append({"path": str(path), "sha256": _sha256(path)})
            if value.get("status") == "CAPTURED" and isinstance(value.get("arrival"), dict):
                arrivals.append(dict(value["arrival"]))
            else:
                blocked.append({
                    "path": str(path),
                    "status": value.get("status", "UNKNOWN"),
                    "receipt": value.get("receipt") or build_blocked_receipt(
                        reason_codes=["capture_result_not_captured"],
                        received_at_utc=_now(),
                    ),
                })
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ContractError) as exc:
            blocked.append({
                "path": str(path),
                "status": "BLOCKED",
                "receipt": build_blocked_receipt(reason_codes=[type(exc).__name__ + ":" + str(exc)], received_at_utc=_now()),
            })

    normalized, audit = load_model_arrival_receipts(arrivals, decision_at_utc=as_of_utc)
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of_utc,
        "arrivals": normalized,
        "blocked_captures": blocked,
        "input_artifacts": input_artifacts,
        "audit": audit,
        "checks": {
            "arrival_count_positive": bool(normalized),
            "all_arrivals_prospective": bool(normalized) and all(item["capture_mode"] == "prospective" for item in normalized),
            "all_publication_sources_native": bool(normalized) and all(item["publication_time_source"] == "provider_http_last_modified" for item in normalized),
            "duplicate_key_free": audit["accepted_count"] == len(normalized),
            "promotion_allowed": False,
            "production_authority": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "boundary": {
            "target_only": True,
            "offline_evidence_assembly": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    evidence_sha = content_sha256(identity)
    return {**identity, "evidence_sha256": evidence_sha, "receipt_sha256": content_sha256({**identity, "evidence_sha256": evidence_sha})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, action="append", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build(capture_paths=list(args.capture), as_of_utc=args.as_of_utc)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(output, result)
    print(json.dumps({"schema": result["schema"], "output": str(output), "receipt_sha256": result["receipt_sha256"], "accepted_count": result["audit"]["accepted_count"], "blocked_count": len(result["blocked_captures"]) + result["audit"]["blocked_count"], "promotion_allowed": result["checks"]["promotion_allowed"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
