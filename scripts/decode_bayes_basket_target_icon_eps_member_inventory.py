#!/usr/bin/env python3
"""Stream-decode the member inventory of one official ICON Global EPS object.

The decoder never expands the whole GRIB container to disk.  It validates
GRIB2 product definition template 4.1 message-by-message and requires the
decoded perturbation-number set to be exactly 1..40.  This proves a native
member cohort but does not yet produce station curves or authorize P0.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, BinaryIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_icon_global_eps_member_inventory_v1"
CAPTURE_SCHEMA = "bayes_basket_target_icon_global_eps_forward_capture_v1"
EXPECTED_MEMBERS = tuple(range(1, 41))
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MESSAGE_COUNT = 64
DEFAULT_CAPTURE = ROOT / "output/audits/icon_global_eps_official_raw_capture_20260801.json"
DEFAULT_OUTPUT = ROOT / "output/audits/icon_global_eps_official_member_inventory_20260801.json"


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = handle.read(remaining)
        if not chunk:
            raise ContractError("icon_eps_grib_truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _section4(message: bytes) -> bytes:
    offset = 16
    end = len(message) - 4
    while offset < end:
        if offset + 5 > end:
            raise ContractError("icon_eps_grib_section_header_truncated")
        length = int.from_bytes(message[offset : offset + 4], "big")
        number = message[offset + 4]
        if length < 5 or offset + length > end:
            raise ContractError("icon_eps_grib_section_length_invalid")
        section = message[offset : offset + length]
        if number == 4:
            return section
        offset += length
    raise ContractError("icon_eps_grib_section4_missing")


def _member_metadata(message: bytes) -> dict[str, Any]:
    if len(message) < 20 or message[:4] != b"GRIB" or message[7] != 2:
        raise ContractError("icon_eps_grib2_signature_invalid")
    declared = int.from_bytes(message[8:16], "big")
    if declared != len(message) or message[-4:] != b"7777":
        raise ContractError("icon_eps_grib_message_envelope_invalid")
    section = _section4(message)
    if len(section) < 37:
        raise ContractError("icon_eps_grib_template4_1_truncated")
    template = int.from_bytes(section[7:9], "big")
    if template != 1:
        raise ContractError("icon_eps_grib_template_not_4_1")
    perturbation = int(section[35])
    forecast_count = int(section[36])
    if forecast_count != 40:
        raise ContractError("icon_eps_grib_forecast_count_not_40")
    if perturbation not in EXPECTED_MEMBERS:
        raise ContractError("icon_eps_grib_perturbation_out_of_scope")
    return {
        "perturbation_number": perturbation,
        "number_of_forecasts_in_ensemble": forecast_count,
        "type_of_ensemble_forecast": int(section[34]),
        "product_definition_template": "4.1",
        "parameter_category": int(section[9]),
        "parameter_number": int(section[10]),
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "message_bytes": len(message),
    }


def decode_member_inventory(raw_path: Path) -> dict[str, Any]:
    path = raw_path.expanduser().resolve(strict=True)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise ContractError("icon_eps_compressed_object_invalid")
    members: list[dict[str, Any]] = []
    decompressed = 0
    with bz2.open(path, "rb") as handle:
        while True:
            prefix = handle.read(16)
            if not prefix:
                break
            if len(prefix) != 16 or prefix[:4] != b"GRIB":
                raise ContractError("icon_eps_grib_stream_invalid")
            length = int.from_bytes(prefix[8:16], "big")
            if length < 20 or length > MAX_MESSAGE_BYTES:
                raise ContractError("icon_eps_grib_message_size_invalid")
            message = prefix + _read_exact(handle, length - 16)
            decompressed += len(message)
            if decompressed > MAX_DECOMPRESSED_BYTES:
                raise ContractError("icon_eps_decompressed_bound_exceeded")
            members.append(_member_metadata(message))
            if len(members) > MAX_MESSAGE_COUNT:
                raise ContractError("icon_eps_message_count_exceeded")
            assert_process_memory_budget()
    ids = [int(item["perturbation_number"]) for item in members]
    if sorted(ids) != list(EXPECTED_MEMBERS) or len(ids) != len(set(ids)):
        raise ContractError("icon_eps_member_set_not_1_through_40")
    if len({(item["parameter_category"], item["parameter_number"]) for item in members}) != 1:
        raise ContractError("icon_eps_parameter_scope_mixed")
    return {
        "member_ids": ids,
        "member_count": len(ids),
        "number_of_forecasts_in_ensemble": 40,
        "product_definition_template": "4.1",
        "decompressed_message_bytes": decompressed,
        "members": sorted(members, key=lambda item: item["perturbation_number"]),
    }


def build_receipt(capture_path: Path) -> dict[str, Any]:
    capture = load_bounded_json(capture_path.expanduser().resolve(strict=True))
    if (
        capture.get("schema") != CAPTURE_SCHEMA
        or capture.get("product_id") != "DWD_ICON_GLOBAL_EPS"
        or capture.get("state") not in {
            "OFFICIAL_RAW_CAPTURED_MEMBER_DECODE_PENDING",
            "REUSED_VERIFIED",
        }
        or capture.get("authority", {}).get("source_role") != "official_native"
        or capture.get("native_arrival_index_eligible") is not False
    ):
        raise ContractError("icon_eps_capture_receipt_invalid")
    raw_path = Path(str(capture.get("raw_object_ref") or ""))
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    if raw_sha != capture.get("raw_sha256"):
        raise ContractError("icon_eps_capture_raw_sha_mismatch")
    inventory = decode_member_inventory(raw_path)
    identity = {
        "schema": SCHEMA,
        "provider_id": "icon_global_eps_official",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "run_initialization_utc": capture["run_initialization_utc"],
        "valid_time_utc": capture["valid_time_utc"],
        "forecast_hour": capture["forecast_hour"],
        "provider_published_at_utc": capture["provider_published_at_utc"],
        "response_completed_at_utc": capture["response_completed_at_utc"],
        "raw_object_ref": str(raw_path),
        "raw_sha256": raw_sha,
        "capture_receipt_sha256": capture["content_sha256"],
        "member_inventory": inventory,
        "state": "OFFICIAL_40_MEMBER_INVENTORY_DECODED_STATION_CURVES_PENDING",
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "boundary": {
            "read_only_raw_decode": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_receipt(args.capture)
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
