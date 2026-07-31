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
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_arrival import (  # noqa: E402
    build_blocked_receipt,
    load_model_arrival_receipts,
)
from bayes_basket_target.official_nwp_capture_scheduler import (  # noqa: E402
    CANDIDATE_PREFETCH_SCHEMA,
)
from bayes_basket_target.resource_bounds import (
    MAX_INPUT_FILES_PER_BATCH,
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_model_arrival_evidence_v1"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_model_arrival_evidence.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    if path.is_symlink():
        raise ContractError("model_arrival_input_symlink_forbidden")
    size = int(path.stat().st_size)
    if size <= 0 or size > MAX_JSON_INPUT_BYTES:
        raise ContractError("model_arrival_input_size_invalid")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = size
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ContractError("model_arrival_input_short_read")
            digest.update(chunk)
            remaining -= len(chunk)
        if handle.read(1):
            raise ContractError("model_arrival_input_grew_during_hash")
    return digest.hexdigest()


def _candidate_receipt_arrivals(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Project only content-bound native arrivals from one candidate receipt.

    Aggregator rows remain candidate/calibration lineage and are deliberately
    excluded from the native arrival index.  Missing arrival fields are never
    synthesized from filenames, schedules, or the candidate receipt.
    """

    resolved = path.expanduser().resolve()
    receipt = load_bounded_json(resolved)
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema") != CANDIDATE_PREFETCH_SCHEMA
    ):
        raise ContractError("candidate_prefetch_receipt_schema_invalid")
    identity = {
        str(key): value
        for key, value in receipt.items()
        if key != "content_sha256"
    }
    if str(receipt.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("candidate_prefetch_receipt_hash_mismatch")
    if (
        receipt.get("candidate_only") is not True
        or receipt.get("p0_materialization_authorized") is not False
        or receipt.get("native_arrival_indexed") is not False
        or receipt.get("creates_activation") is not False
        or receipt.get("creates_p0") is not False
    ):
        raise ContractError("candidate_prefetch_receipt_boundary_invalid")
    providers = receipt.get("providers")
    if not isinstance(providers, list) or len(providers) > 8:
        raise ContractError("candidate_prefetch_provider_count_invalid")
    arrivals: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    inputs = [{
        "path": str(resolved),
        "sha256": _sha256(resolved),
    }]
    seen_paths: set[str] = set()
    for provider in providers:
        if not isinstance(provider, Mapping):
            raise ContractError("candidate_prefetch_provider_invalid")
        if provider.get("state") != "COMPLETE":
            continue
        provider_id = str(provider.get("provider_id") or "").strip().lower()
        model_id = str(provider.get("model_id") or "").strip().upper()
        if (
            provider.get("source_class") == "aggregator_republisher"
            or provider.get("aggregator_or_republisher") is True
        ):
            blocked.append({
                "path": str(resolved),
                "status": "CANDIDATE_ONLY_AGGREGATOR",
                "provider_id": provider_id,
                "model_id": model_id,
                "receipt": build_blocked_receipt(
                    reason_codes=[
                        "candidate_aggregator_native_arrival_forbidden"
                    ],
                    received_at_utc=str(receipt.get("as_of_utc") or ""),
                ),
            })
            continue
        if (
            provider.get("source_class") != "native_or_official"
            or provider.get("native_arrival_indexed") is not False
        ):
            raise ContractError(
                "candidate_native_provider_boundary_invalid"
            )
        bindings = provider.get("evidence_bindings")
        if (
            not isinstance(bindings, list)
            or not bindings
            or len(bindings) > MAX_INPUT_FILES_PER_BATCH
        ):
            raise ContractError(
                "candidate_native_evidence_bindings_missing"
            )
        for binding in bindings:
            if not isinstance(binding, Mapping):
                raise ContractError(
                    "candidate_native_evidence_binding_invalid"
                )
            binding_identity = {
                str(key): value
                for key, value in binding.items()
                if key != "binding_sha256"
            }
            if str(binding.get("binding_sha256") or "") != content_sha256(
                binding_identity
            ):
                raise ContractError(
                    "candidate_native_evidence_binding_hash_mismatch"
                )
            if (
                str(binding.get("provider_id") or "").strip().lower()
                != provider_id
                or str(binding.get("model_id") or "").strip().upper()
                != model_id
                or str(binding.get("station_id") or "").strip().upper()
                != str(receipt.get("station_id") or "").strip().upper()
                or str(binding.get("event_date") or "")
                != str(receipt.get("event_date") or "")
                or str(binding.get("run_initialization_utc") or "")
                != str(receipt.get("run_initialization_utc") or "")
            ):
                raise ContractError(
                    "candidate_native_evidence_binding_scope_mismatch"
                )
            evidence_path = Path(
                str(binding.get("evidence_path") or "")
            ).expanduser().resolve()
            path_text = str(evidence_path)
            if path_text in seen_paths:
                continue
            seen_paths.add(path_text)
            payload = load_bounded_json(evidence_path)
            if (
                not isinstance(payload, Mapping)
                or content_sha256(payload)
                != str(binding.get("evidence_payload_sha256") or "")
            ):
                raise ContractError(
                    "candidate_native_evidence_payload_hash_mismatch"
                )
            inputs.append({
                "path": path_text,
                "sha256": _sha256(evidence_path),
            })
            if len(inputs) > MAX_INPUT_FILES_PER_BATCH:
                raise ContractError(
                    "candidate_native_evidence_file_count_exceeded"
                )
            arrival = payload.get("arrival")
            if not isinstance(arrival, Mapping):
                blocked.append({
                    "path": path_text,
                    "status": "BLOCKED",
                    "receipt": build_blocked_receipt(
                        reason_codes=[
                            "candidate_native_arrival_payload_missing"
                        ],
                        received_at_utc=str(
                            binding.get("received_at_utc") or ""
                        ),
                    ),
                })
                continue
            arrivals.append(dict(arrival))
    return arrivals, blocked, inputs


def build(
    *,
    capture_paths: list[Path],
    as_of_utc: str,
    candidate_receipt_paths: list[Path] | None = None,
) -> dict[str, Any]:
    candidate_paths = list(candidate_receipt_paths or [])
    if not capture_paths and not candidate_paths:
        raise ContractError("target_capture_input_missing")
    if (
        len(capture_paths) + len(candidate_paths)
        > MAX_INPUT_FILES_PER_BATCH
    ):
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
    for raw_path in candidate_paths:
        try:
            projected, quarantined, inputs = (
                _candidate_receipt_arrivals(raw_path)
            )
            arrivals.extend(projected)
            blocked.extend(quarantined)
            input_artifacts.extend(inputs)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ContractError,
        ) as exc:
            blocked.append({
                "path": str(raw_path.expanduser().resolve()),
                "status": "BLOCKED",
                "receipt": build_blocked_receipt(
                    reason_codes=[type(exc).__name__ + ":" + str(exc)],
                    received_at_utc=_now(),
                ),
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
    parser.add_argument(
        "--capture",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument(
        "--candidate-receipt",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build(
        capture_paths=list(args.capture),
        candidate_receipt_paths=list(args.candidate_receipt),
        as_of_utc=args.as_of_utc,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(output, result)
    print(json.dumps({"schema": result["schema"], "output": str(output), "receipt_sha256": result["receipt_sha256"], "accepted_count": result["audit"]["accepted_count"], "blocked_count": len(result["blocked_captures"]) + result["audit"]["blocked_count"], "promotion_allowed": result["checks"]["promotion_allowed"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
