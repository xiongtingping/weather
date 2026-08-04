#!/usr/bin/env python3
"""Decode one official DWD ICON Global/EPS cohort into 45-station members.

The DWD ``icon-eps`` T_2M object is a *single* GRIB2.bz2 payload containing
40 perturbed members for one run/lead.  This decoder deliberately keeps that
shape: it streams one retained raw object, reads one GRIB message at a time,
and emits small station/member facts.  It never writes forty member raw files,
contacts DWD, opens SQLite, materializes P0, or changes activation state.

The production decoder binds DWD's official R03B06 ICON coordinate grid by
URL, SHA-256 and UUID, then uses its official cell coordinates to select the
nearest AWC station cells.  A missing grid, tool or validation receipt is a
hard failure, never a reason to interpolate from an arbitrary grid or fall
back to an aggregator.
"""

from __future__ import annotations

import argparse
from array import array
import bz2
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
    station_coordinates,
)


SCHEMA = "bayes_basket_target_icon_global_eps_station_member_cohort_v1"
CAPTURE_SCHEMA = "bayes_basket_target_icon_global_eps_forward_capture_v1"
PROVIDER_ID = "icon_global_eps_official"
PRODUCT_ID = "DWD_ICON_GLOBAL_EPS"
FAMILY_ID = "DWD_ICON_EPS"
EXPECTED_MEMBERS = tuple(range(1, 41))
EXPECTED_STATION_COUNT = 45
# The previous capture adapter permits a larger object.  Station-member
# decoding intentionally has a narrower retained-raw admission limit: one
# object may never exceed 64 MiB and its (one-object) cohort may never exceed
# 256 MiB.  No decompressed payload is retained on disk.
MAX_SINGLE_RAW_BYTES = 64 * 1024 * 1024
# The raw model payload has a 64-MiB bound.  The matching 737,280-cell DWD
# R03B06 coordinate grid is 216,240,634 bytes compressed, so it gets a
# separately bounded support-object lane.  The combined retained inputs and
# temporary expansion remain finite and are checked before decoding.
MAX_OFFICIAL_GRID_BYTES = 256 * 1024 * 1024
MAX_CUMULATIVE_RAW_BYTES = 512 * 1024 * 1024
MAX_GRID_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_TEMPORARY_DECODE_BYTES = 2 * 1024 * 1024 * 1024
MAX_NCDUMP_COORDINATE_OUTPUT_BYTES = 64 * 1024 * 1024
MIN_DECODE_FREE_BYTES = 2 * 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MAX_NCDUMP_OUTPUT_BYTES = 2 * 1024 * 1024
GRID_CAPTURE_SCHEMA = "bayes_basket_target_icon_eps_official_grid_capture_v1"
OFFICIAL_GRID_URL = "https://opendata.dwd.de/weather/lib/cdo/icon_grid_0036_R03B06_G.nc.bz2"
OFFICIAL_GRID_FILENAME = "icon_grid_0036_R03B06_G.nc.bz2"
EXPECTED_OFFICIAL_GRID_BYTES = 216_240_634
EXPECTED_OFFICIAL_GRID_UUID = "ae487d14fe2e11e4af85e50a2a56a360"
EXPECTED_OFFICIAL_GRID_CELL_COUNT = 737_280
GRIB_GET_BINARY = Path("/opt/homebrew/bin/grib_get")
NCDUMP_BINARY = Path("/opt/homebrew/bin/ncdump")
MAX_MEMBER_RECORDS = EXPECTED_STATION_COUNT * len(EXPECTED_MEMBERS)
MAX_CAPTURE_RECEIPT_BYTES = 4 * 1024 * 1024
DEFAULT_CAPTURE = ROOT / "output/audits/icon_global_eps_official_raw_capture_20260801.json"
DEFAULT_OUTPUT = ROOT / "output/audits/icon_global_eps_station_members_r03b06_20260801.json"
DEFAULT_GRID_ROOT = ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_eps_official/grid_r03b06"
DEFAULT_GRID_RECEIPT = ROOT / "output/audits/icon_global_eps_official_grid_r03b06_capture_latest.json"


def _canonical_timestamp(value: Any, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_station_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"icon_eps_station_{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _timestamp(value: Any, *, field: str) -> datetime:
    return datetime.fromisoformat(
        _canonical_timestamp(value, field=field).replace("Z", "+00:00")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _canonical_grid_uuid(value: Any, *, reason: str) -> str:
    candidate = str(value or "").strip().lower().replace("-", "")
    if len(candidate) != 32 or any(char not in "0123456789abcdef" for char in candidate):
        raise ContractError(reason)
    return candidate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _http_timestamp(value: Any, *, field: str) -> str:
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_station_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"icon_eps_station_{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _executable(path: Path, *, reason: str) -> str:
    candidate = path.expanduser().resolve()
    if not candidate.is_file() or candidate.is_symlink() or not os.access(candidate, os.X_OK):
        raise ContractError(reason)
    return str(candidate)


def _decompress_bz2(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
) -> int:
    """Decompress exactly one temporary support file under a hard byte cap."""

    total = 0
    try:
        with bz2.open(source, "rb") as reader, destination.open("xb") as writer:
            while True:
                block = reader.read(CHUNK_BYTES)
                if not block:
                    break
                total += len(block)
                if total > maximum_bytes:
                    raise ContractError("icon_eps_station_grid_decompressed_bound_exceeded")
                writer.write(block)
                assert_process_memory_budget()
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("icon_eps_station_grid_decompress_failed") from exc
    if total <= 0:
        raise ContractError("icon_eps_station_grid_decompressed_empty")
    return total


def _grid_metadata(grid_nc_path: Path) -> tuple[str, int]:
    binary = _executable(NCDUMP_BINARY, reason="icon_eps_station_ncdump_unavailable")
    try:
        completed = subprocess.run(
            [binary, "-h", str(grid_nc_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("icon_eps_station_grid_uuid_probe_failed") from exc
    header = bytes(completed.stdout or b"")
    if completed.returncode != 0 or len(header) > MAX_NCDUMP_OUTPUT_BYTES:
        raise ContractError("icon_eps_station_grid_uuid_probe_failed")
    try:
        text = header.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("icon_eps_station_grid_uuid_probe_failed") from exc
    # ICON grid distributions have used both a global ``uuid`` and a
    # ``uuidOfHGrid`` attribute.  The raw header remains the official source;
    # accept either exact attribute name, never a synthetic local UUID.
    import re

    uuid_match = re.search(r'(?m)^\s*:?(?:uuidOfHGrid|uuid)\s*=\s*"([^"]+)"\s*;', text)
    cell_match = re.search(r'(?m)^\s*cell\s*=\s*(\d+)\s*;', text)
    if not uuid_match or not uuid_match.group(1).strip() or not cell_match:
        raise ContractError("icon_eps_station_grid_uuid_missing")
    cell_count = int(cell_match.group(1))
    if cell_count <= 0:
        raise ContractError("icon_eps_station_grid_cell_count_invalid")
    return _canonical_grid_uuid(
        uuid_match.group(1), reason="icon_eps_station_grid_uuid_invalid"
    ), cell_count


def _grid_uuid(grid_nc_path: Path) -> str:
    return _grid_metadata(grid_nc_path)[0]


def _require_matching_grid_signature(
    *,
    grid_uuid: str,
    grid_cell_count: int,
    raw_grid_uuid: str,
    raw_cell_count: int,
) -> None:
    """Forbid silently applying a sibling ICON grid to a native raw field."""

    if (
        not grid_uuid
        or not raw_grid_uuid
        or grid_uuid.lower() != raw_grid_uuid.lower()
        or grid_cell_count <= 0
        or raw_cell_count <= 0
        or grid_cell_count != raw_cell_count
    ):
        raise ContractError("icon_eps_station_grid_signature_mismatch")


def _free_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return int(shutil.disk_usage(candidate).free)


def _curl_headers(path: Path) -> dict[str, str]:
    try:
        blocks = path.read_text(encoding="iso-8859-1").split("\n\n")
    except OSError as exc:
        raise ContractError("icon_eps_station_grid_curl_headers_missing") from exc
    for block in reversed(blocks):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("HTTP/"):
            continue
        result: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.strip().lower()] = value.strip()
        return result
    raise ContractError("icon_eps_station_grid_curl_headers_invalid")


def _curl_grid_metadata(*, timeout_sec: float) -> tuple[dict[str, str], int]:
    """Fetch only current official headers; never re-download an adopted raw."""

    with tempfile.TemporaryDirectory(prefix="icon-eps-grid-head-") as directory:
        header_path = Path(directory) / "headers.txt"
        try:
            completed = subprocess.run(
                [
                    "/usr/bin/curl", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--connect-timeout", "20", "--max-time",
                    str(max(1, int(timeout_sec))), "--head", "--dump-header", str(header_path),
                    "--output", "/dev/null", OFFICIAL_GRID_URL,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(2.0, float(timeout_sec) + 5.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContractError("icon_eps_station_grid_curl_head_failed") from exc
        exit_code = int(completed.returncode)
        if exit_code != 0:
            raise ContractError(f"icon_eps_station_grid_curl_head_exit_{exit_code}")
        return _curl_headers(header_path), exit_code


def _curl_grid_download(
    *,
    destination: Path,
    timeout_sec: float,
) -> tuple[dict[str, str], int]:
    """One bounded official GET with an auditable curl exit code."""

    header_path = destination.with_suffix(destination.suffix + ".headers")
    try:
        try:
            completed = subprocess.run(
                [
                    "/usr/bin/curl", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--connect-timeout", "20", "--max-time",
                    str(max(1, int(timeout_sec))), "--max-filesize", str(MAX_OFFICIAL_GRID_BYTES),
                    "--dump-header", str(header_path), "--output", str(destination), OFFICIAL_GRID_URL,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(2.0, float(timeout_sec) + 5.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContractError("icon_eps_station_grid_curl_failed") from exc
        exit_code = int(completed.returncode)
        if exit_code != 0:
            raise ContractError(f"icon_eps_station_grid_curl_exit_{exit_code}")
        return _curl_headers(header_path), exit_code
    finally:
        header_path.unlink(missing_ok=True)


def _validated_grid_receipt(grid_receipt_path: Path) -> dict[str, Any]:
    try:
        receipt = load_bounded_json(
            grid_receipt_path.expanduser().resolve(strict=True),
            limit=MAX_CAPTURE_RECEIPT_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise ContractError("icon_eps_station_grid_receipt_invalid") from exc
    if not isinstance(receipt, dict):
        raise ContractError("icon_eps_station_grid_receipt_invalid")
    identity = {key: value for key, value in receipt.items() if key != "content_sha256"}
    if (
        receipt.get("schema") != GRID_CAPTURE_SCHEMA
        or receipt.get("source_url") != OFFICIAL_GRID_URL
        or receipt.get("content_sha256") != content_sha256(identity)
        or not isinstance(receipt.get("grid_uuid"), str)
        or not str(receipt.get("grid_uuid")).strip()
        or not isinstance(receipt.get("grid_cell_count"), int)
        or int(receipt.get("grid_cell_count") or 0) != EXPECTED_OFFICIAL_GRID_CELL_COUNT
        or _canonical_grid_uuid(
            receipt.get("grid_uuid"), reason="icon_eps_station_grid_receipt_contract_invalid"
        ) != EXPECTED_OFFICIAL_GRID_UUID
    ):
        raise ContractError("icon_eps_station_grid_receipt_contract_invalid")
    grid_path = _safe_raw_path(receipt.get("raw_object_ref"))
    grid_size = int(grid_path.stat().st_size)
    if grid_size <= 0 or grid_size > MAX_OFFICIAL_GRID_BYTES:
        raise ContractError("icon_eps_station_grid_raw_bound_exceeded")
    if int(receipt.get("response_bytes") or -1) != grid_size:
        raise ContractError("icon_eps_station_grid_size_mismatch")
    grid_sha = str(receipt.get("raw_sha256") or "").lower()
    if len(grid_sha) != 64 or any(char not in "0123456789abcdef" for char in grid_sha):
        raise ContractError("icon_eps_station_grid_sha_invalid")
    if _sha256_file(grid_path) != grid_sha:
        raise ContractError("icon_eps_station_grid_sha_mismatch")
    return {**receipt, "raw_path": grid_path, "raw_size": grid_size}


def capture_official_grid(
    *,
    output_root: Path = DEFAULT_GRID_ROOT,
    receipt_path: Path = DEFAULT_GRID_RECEIPT,
    timeout_sec: float = 900.0,
) -> dict[str, Any]:
    """Capture one matching official grid once, content-addressed and bounded.

    A verified receipt is reused without another HTTP request.  The compressed
    object is the only retained grid source; decompressed forms are always
    temporary and removed by their context manager.
    """

    receipt_path = receipt_path.expanduser().resolve()
    receipt_to_rebuild: dict[str, Any] | None = None
    if receipt_path.is_file():
        existing_receipt = _validated_grid_receipt(receipt_path)
        object_count = existing_receipt.get("object_download_request_count")
        metadata_count = existing_receipt.get("metadata_request_count")
        total_count = existing_receipt.get("network_request_count")
        if (
            object_count == 1
            and metadata_count in {0, 1}
            and total_count == int(object_count) + int(metadata_count)
        ):
            return {
                key: value for key, value in existing_receipt.items()
                if key not in {"raw_path", "raw_size"}
            }
        # Upgrade a pre-count receipt only from its already verified raw and
        # already recorded official headers.  This is a local receipt rewrite,
        # not another GET or HEAD request.
        receipt_to_rebuild = existing_receipt
    root = output_root.expanduser().resolve()
    raw_dir = root / "raw"
    if _free_bytes(root) < MIN_DECODE_FREE_BYTES + MAX_OFFICIAL_GRID_BYTES:
        raise ContractError("icon_eps_station_grid_min_free_space_not_met")
    raw_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        path for path in raw_dir.glob("*.nc.bz2")
        if path.is_file() and not path.is_symlink()
    )
    if len(existing) > 1:
        raise ContractError("icon_eps_station_grid_multiple_retained_objects")
    temporary = raw_dir / f".grid.{os.getpid()}.{time.time_ns()}.nc.bz2"
    try:
        if existing:
            # The first bounded GET finished its content-addressed write but
            # did not finish receipt construction.  Do not download the 78
            # MiB object again: verify the sole retained object against a
            # fresh header-only official response and adopt it atomically.
            raw_path = existing[0]
            if receipt_to_rebuild is not None:
                headers = dict(receipt_to_rebuild.get("response_headers") or {})
                curl_exit_code = int(receipt_to_rebuild.get("curl_exit_code") or 0)
            else:
                headers, curl_exit_code = _curl_grid_metadata(timeout_sec=min(timeout_sec, 60.0))
            transport = "prior_bounded_get_adopted_after_official_head"
            http_method = "HEAD"
            object_download_request_count = 1
            metadata_request_count = 1
        else:
            headers, curl_exit_code = _curl_grid_download(
                destination=temporary, timeout_sec=timeout_sec
            )
            raw_size = int(temporary.stat().st_size) if temporary.is_file() else 0
            if raw_size <= 0 or raw_size > MAX_OFFICIAL_GRID_BYTES:
                raise ContractError("icon_eps_station_grid_response_bound_exceeded")
            raw_sha = _sha256_file(temporary)
            raw_path = raw_dir / f"{raw_sha}.nc.bz2"
            if raw_path.exists():
                raise ContractError("icon_eps_station_grid_duplicate_raw_refused")
            os.replace(temporary, raw_path)
            transport = "bounded_curl_https_get"
            http_method = "GET"
            object_download_request_count = 1
            metadata_request_count = 0
        if "content-length" not in headers or "last-modified" not in headers:
            raise ContractError("icon_eps_station_grid_http_metadata_missing")
        response_bytes = int(headers["content-length"])
        raw_size = int(raw_path.stat().st_size)
        if (
            response_bytes <= 0
            or response_bytes > MAX_OFFICIAL_GRID_BYTES
            or response_bytes != EXPECTED_OFFICIAL_GRID_BYTES
            or raw_size != response_bytes
        ):
            raise ContractError("icon_eps_station_grid_content_length_mismatch")
        raw_sha = _sha256_file(raw_path)
        with tempfile.TemporaryDirectory(prefix="icon-eps-grid-uuid-") as directory:
            grid_nc = Path(directory) / "grid.nc"
            decompressed = _decompress_bz2(
                raw_path, grid_nc, maximum_bytes=MAX_GRID_DECOMPRESSED_BYTES
            )
            grid_uuid, grid_cell_count = _grid_metadata(grid_nc)
            if (
                grid_uuid.lower() != EXPECTED_OFFICIAL_GRID_UUID
                or grid_cell_count != EXPECTED_OFFICIAL_GRID_CELL_COUNT
            ):
                raise ContractError("icon_eps_station_grid_expected_signature_mismatch")
        identity = {
            "schema": GRID_CAPTURE_SCHEMA,
            "state": "OFFICIAL_DWD_R03B06_GRID_CAPTURED",
            "source_url": OFFICIAL_GRID_URL,
            "grid_filename": OFFICIAL_GRID_FILENAME,
            "grid_uuid": grid_uuid,
            "grid_cell_count": grid_cell_count,
            "raw_object_ref": str(raw_path),
            "raw_sha256": raw_sha,
            "response_bytes": raw_size,
            "decompressed_bytes": decompressed,
            "transport": transport,
            "http_method": http_method,
            "curl_exit_code": curl_exit_code,
            "object_download_request_count": object_download_request_count,
            "metadata_request_count": metadata_request_count,
            "network_request_count": (
                object_download_request_count + metadata_request_count
            ),
            "provider_published_at_utc": _http_timestamp(headers["last-modified"], field="grid_last_modified"),
            "response_completed_at_utc": _utc_now(),
            "response_headers": {
                key: headers[key]
                for key in ("content-length", "last-modified", "etag", "date")
                if key in headers
            },
            "resource_guard": {
                "max_grid_response_bytes": MAX_OFFICIAL_GRID_BYTES,
                "max_grid_decompressed_bytes": MAX_GRID_DECOMPRESSED_BYTES,
                "min_free_bytes": MIN_DECODE_FREE_BYTES,
                "retained_compressed_grid_objects": 1,
            },
            "boundary": {
                "official_native_coordinate_asset": True,
                "network_request_count": (
                    object_download_request_count + metadata_request_count
                ),
                "writes_target_database": False,
                "activates_p0": False,
                "member_raw_fanout": False,
            },
        }
        result = {**identity, "content_sha256": content_sha256(identity)}
        write_bounded_json(receipt_path, result)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def _safe_raw_path(value: Any) -> Path:
    candidate = Path(str(value or "")).expanduser()
    if not candidate.is_absolute():
        raise ContractError("icon_eps_station_raw_path_not_absolute")
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError("icon_eps_station_raw_missing") from exc
    if not path.is_file() or path.is_symlink() or path.suffix.lower() != ".bz2":
        raise ContractError("icon_eps_station_raw_invalid")
    return path


def _capture_receipt(capture_path: Path) -> tuple[dict[str, Any], Path, int]:
    """Validate an official one-object capture receipt before decode starts."""

    try:
        capture = load_bounded_json(
            capture_path.expanduser().resolve(strict=True),
            limit=MAX_CAPTURE_RECEIPT_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise ContractError("icon_eps_station_capture_receipt_invalid") from exc
    if not isinstance(capture, dict):
        raise ContractError("icon_eps_station_capture_receipt_invalid")
    authority = capture.get("authority")
    boundary = capture.get("boundary")
    if (
        capture.get("schema") != CAPTURE_SCHEMA
        or capture.get("provider_id") != PROVIDER_ID
        or capture.get("product_id") != PRODUCT_ID
        or capture.get("independence_family_id") != FAMILY_ID
        or capture.get("state")
        not in {"OFFICIAL_RAW_CAPTURED_MEMBER_DECODE_PENDING", "REUSED_VERIFIED"}
        or not isinstance(authority, Mapping)
        or authority.get("source_role") != "official_native"
        or authority.get("transport_provider_id") != "DWD"
        or authority.get("member_decode_required") is not True
        or not isinstance(boundary, Mapping)
        or boundary.get("single_lead_only") is not True
        or capture.get("native_arrival_index_eligible") is not False
        or capture.get("formal_p0_eligible") is not False
    ):
        raise ContractError("icon_eps_station_capture_authority_invalid")
    raw_path = _safe_raw_path(capture.get("raw_object_ref"))
    raw_size = int(raw_path.stat().st_size)
    if raw_size <= 0 or raw_size > MAX_SINGLE_RAW_BYTES:
        raise ContractError("icon_eps_station_single_raw_bound_exceeded")
    # This cohort has exactly one DWD payload.  The explicit cumulative guard
    # prevents future callers from silently widening it to a raw fan-out.
    if raw_size > MAX_CUMULATIVE_RAW_BYTES:
        raise ContractError("icon_eps_station_cumulative_raw_bound_exceeded")
    response_bytes = capture.get("response_bytes")
    try:
        expected_response_bytes = int(response_bytes)
    except (TypeError, ValueError) as exc:
        raise ContractError("icon_eps_station_capture_size_invalid") from exc
    if expected_response_bytes != raw_size:
        raise ContractError("icon_eps_station_capture_size_mismatch")
    raw_sha = str(capture.get("raw_sha256") or "").lower()
    if len(raw_sha) != 64 or any(char not in "0123456789abcdef" for char in raw_sha):
        raise ContractError("icon_eps_station_capture_raw_sha_invalid")
    if _sha256_file(raw_path) != raw_sha:
        raise ContractError("icon_eps_station_capture_raw_sha_mismatch")
    capture_sha = str(capture.get("content_sha256") or "").lower()
    capture_identity = {key: value for key, value in capture.items() if key != "content_sha256"}
    if capture_sha != content_sha256(capture_identity):
        raise ContractError("icon_eps_station_capture_receipt_sha_mismatch")
    run = _timestamp(capture.get("run_initialization_utc"), field="run_initialization_utc")
    valid = _timestamp(capture.get("valid_time_utc"), field="valid_time_utc")
    published = _timestamp(capture.get("provider_published_at_utc"), field="provider_published_at_utc")
    received = _timestamp(capture.get("response_completed_at_utc"), field="response_completed_at_utc")
    try:
        lead = int(capture.get("forecast_hour"))
    except (TypeError, ValueError) as exc:
        raise ContractError("icon_eps_station_forecast_hour_invalid") from exc
    if not 0 <= lead <= 180 or valid != run + timedelta(hours=lead):
        raise ContractError("icon_eps_station_capture_time_contract_invalid")
    if received < published:
        raise ContractError("icon_eps_station_capture_arrival_order_invalid")
    return capture, raw_path, raw_size


class StationMemberAdapter(Protocol):
    """Narrow adapter boundary used by tests and the optional ecCodes runtime."""

    decoder_id: str
    remap_contract: str

    def iter_member_station_values(
        self,
        raw_path: Path,
        *,
        stations: Mapping[str, Mapping[str, Any]],
    ) -> Iterable[Mapping[str, Any]]:
        """Yield one official T_2M member at a time, without raw fan-out."""


class OfficialDwdIconEpsAdapter:
    """Official UUID-matched R03B06 cell-coordinate nearest-neighbour decoder.

    ecCodes' ``-l`` cannot locate a point on ICON's icosahedral grid, and CDO
    cannot consume the multi-entity R03B06 NetCDF directly as ``setgrid``.
    This adapter instead streams the DWD ``clon``/``clat`` cell coordinates,
    computes the exact nearest cell for each AWC station, then asks ecCodes
    ``grib_get -i`` for those fixed cells.  No model payload is remapped or
    copied into forty pseudo-member raws.
    """

    decoder_id = "dwd_icon_uuid_matched_cell_coordinate_nearest_grib_get_member_v2"
    remap_contract = "DWD_ICON_OFFICIAL_UUID_AND_CELL_COUNT_MATCHED_AWC45_V2"
    _NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")

    def __init__(
        self,
        *,
        grid_receipt_path: Path,
        precomputed_station_indices: Mapping[str, Any] | None = None,
    ) -> None:
        self.grid = _validated_grid_receipt(grid_receipt_path)
        self.station_nearest_cell_indices = {
            str(station_id).strip().upper(): int(index)
            for station_id, index in (precomputed_station_indices or {}).items()
        }

    def lineage(self) -> dict[str, Any]:
        return {
            "source_grid_url": self.grid["source_url"],
            "source_grid_uuid": self.grid["grid_uuid"],
            "source_grid_cell_count": int(self.grid["grid_cell_count"]),
            "source_grid_sha256": self.grid["raw_sha256"],
            "source_grid_receipt_sha256": self.grid["content_sha256"],
            "source_grid_bytes": int(self.grid["raw_size"]),
            "station_nearest_cell_indices": dict(self.station_nearest_cell_indices),
        }

    @staticmethod
    def _parse_point_rows(stdout: bytes) -> list[dict[str, Any]]:
        if len(stdout) > MAX_NCDUMP_OUTPUT_BYTES:
            raise ContractError("icon_eps_station_grib_get_output_bound_exceeded")
        try:
            lines = stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ContractError("icon_eps_station_grib_get_output_invalid") from exc
        rows: list[dict[str, Any]] = []
        for line in lines:
            tokens = line.split()
            # dataDate dataTime validityDate validityTime template member
            # ensemble_count selected_grid_value
            if len(tokens) < 8:
                continue
            try:
                date, data_time, validity_date, validity_time = tokens[0:4]
                template, member, count = (int(token) for token in tokens[4:7])
                value = float(tokens[-1])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                raise ContractError("icon_eps_station_temperature_invalid")
            try:
                run = _canonical_timestamp(
                    f"{date[:4]}-{date[4:6]}-{date[6:8]}T{int(data_time) // 100:02d}:{int(data_time) % 100:02d}:00Z",
                    field="grib_run",
                )
                valid = _canonical_timestamp(
                    f"{validity_date[:4]}-{validity_date[4:6]}-{validity_date[6:8]}T{int(validity_time) // 100:02d}:{int(validity_time) % 100:02d}:00Z",
                    field="grib_valid",
                )
            except (ValueError, IndexError) as exc:
                raise ContractError("icon_eps_station_grib_timestamp_invalid") from exc
            rows.append({
                "member_id": member,
                "number_of_forecasts_in_ensemble": count,
                "product_definition_template": "4.1" if template == 1 else str(template),
                "run_initialization_utc": run,
                "valid_time_utc": valid,
                "temperature_2m_c": round(value - 273.15 if value > 150.0 else value, 6),
            })
        if not rows:
            raise ContractError("icon_eps_station_grib_get_rows_missing")
        return rows

    @staticmethod
    def _command(*args: str, timeout: float, reason: str) -> bytes:
        try:
            completed = subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContractError(reason) from exc
        if completed.returncode != 0:
            raise ContractError(reason)
        return bytes(completed.stdout or b"")

    @classmethod
    def _raw_grid_signature(
        cls,
        raw_grib: Path,
        *,
        grib_get: str,
        selector: str,
    ) -> tuple[str, int]:
        """Read the native message grid identity before any station lookup."""

        stdout = cls._command(
            grib_get,
            "-w",
            selector,
            "-p",
            "uuidOfHGrid:s,numberOfDataPoints:i",
            str(raw_grib),
            timeout=30.0,
            reason="icon_eps_station_raw_grid_signature_unavailable",
        )
        if not stdout or len(stdout) > MAX_NCDUMP_OUTPUT_BYTES:
            raise ContractError("icon_eps_station_raw_grid_signature_invalid")
        try:
            lines = stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ContractError("icon_eps_station_raw_grid_signature_invalid") from exc
        signatures: set[tuple[str, int]] = set()
        for line in lines:
            parts = line.strip().split()
            if len(parts) != 2:
                raise ContractError("icon_eps_station_raw_grid_signature_invalid")
            uuid, count_text = parts
            try:
                count = int(count_text)
            except ValueError as exc:
                raise ContractError("icon_eps_station_raw_grid_signature_invalid") from exc
            if count <= 0:
                raise ContractError("icon_eps_station_raw_grid_signature_invalid")
            signatures.add((
                _canonical_grid_uuid(
                    uuid, reason="icon_eps_station_raw_grid_signature_invalid"
                ),
                count,
            ))
        if len(lines) != len(EXPECTED_MEMBERS) or len(signatures) != 1:
            raise ContractError("icon_eps_station_raw_grid_signature_inconsistent")
        return next(iter(signatures))

    def _nearest_cell_indices(
        self,
        grid_nc: Path,
        *,
        stations: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, int]:
        ncdump = _executable(NCDUMP_BINARY, reason="icon_eps_station_ncdump_unavailable")
        try:
            process = subprocess.Popen(
                [ncdump, "-v", "clon,clat", str(grid_nc)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
            )
        except OSError as exc:
            raise ContractError("icon_eps_station_grid_coordinate_stream_failed") from exc
        longitudes = array("d")
        latitudes = array("d")
        active: str | None = None
        emitted_bytes = 0
        try:
            assert process.stdout is not None
            for line in process.stdout:
                emitted_bytes += len(line.encode("utf-8"))
                if emitted_bytes > MAX_NCDUMP_COORDINATE_OUTPUT_BYTES:
                    raise ContractError("icon_eps_station_grid_coordinate_output_bound_exceeded")
                stripped = line.strip()
                if active is None:
                    if stripped.startswith("clon ="):
                        active, stripped = "lon", stripped.split("=", 1)[1]
                    elif stripped.startswith("clat ="):
                        active, stripped = "lat", stripped.split("=", 1)[1]
                    else:
                        continue
                finished = ";" in stripped
                payload = stripped.split(";", 1)[0]
                target = longitudes if active == "lon" else latitudes
                for token in self._NUMBER.findall(payload):
                    value = float(token)
                    if not math.isfinite(value):
                        raise ContractError("icon_eps_station_grid_coordinate_invalid")
                    target.append(value)
                if finished:
                    active = None
            if process.wait(timeout=60) != 0:
                raise ContractError("icon_eps_station_grid_coordinate_stream_failed")
        except ContractError:
            process.kill()
            process.wait()
            raise
        except (OSError, UnicodeError, subprocess.TimeoutExpired, ValueError) as exc:
            process.kill()
            process.wait()
            raise ContractError("icon_eps_station_grid_coordinate_stream_failed") from exc
        if len(longitudes) != len(latitudes) or not longitudes:
            raise ContractError("icon_eps_station_grid_coordinate_count_invalid")
        vectors = {
            station_id: (
                math.cos(math.radians(float(item["latitude"]))) * math.cos(math.radians(float(item["longitude"]))),
                math.cos(math.radians(float(item["latitude"]))) * math.sin(math.radians(float(item["longitude"]))),
                math.sin(math.radians(float(item["latitude"]))),
            )
            for station_id, item in stations.items()
        }
        nearest = {station_id: (-2.0, -1) for station_id in vectors}
        for index, (lon, lat) in enumerate(zip(longitudes, latitudes)):
            cosine_lat = math.cos(lat)
            cell_x, cell_y, cell_z = (
                cosine_lat * math.cos(lon),
                cosine_lat * math.sin(lon),
                math.sin(lat),
            )
            for station_id, (station_x, station_y, station_z) in vectors.items():
                dot = cell_x * station_x + cell_y * station_y + cell_z * station_z
                if dot > nearest[station_id][0]:
                    nearest[station_id] = (dot, index)
        result = {station_id: index for station_id, (_dot, index) in nearest.items()}
        if set(result) != set(stations) or any(index < 0 for index in result.values()):
            raise ContractError("icon_eps_station_nearest_cell_resolution_failed")
        return result

    def iter_member_station_values(
        self,
        raw_path: Path,
        *,
        stations: Mapping[str, Mapping[str, Any]],
    ) -> Iterator[Mapping[str, Any]]:
        grib_get = _executable(GRIB_GET_BINARY, reason="icon_eps_station_grib_get_unavailable")
        raw_size = int(raw_path.stat().st_size)
        grid_path = Path(self.grid["raw_path"])
        grid_size = int(self.grid["raw_size"])
        if raw_size + grid_size > MAX_CUMULATIVE_RAW_BYTES:
            raise ContractError("icon_eps_station_cumulative_raw_bound_exceeded")
        if _free_bytes(raw_path.parent) < MIN_DECODE_FREE_BYTES:
            raise ContractError("icon_eps_station_remap_min_free_space_not_met")
        with tempfile.TemporaryDirectory(prefix="icon-eps-r03b06-nearest-") as directory:
            temporary = Path(directory)
            grid_nc = temporary / "dwd-r03b06-grid.nc"
            grid_decompressed = 0
            if self.station_nearest_cell_indices:
                if (
                    set(self.station_nearest_cell_indices) != set(stations)
                    or any(
                        index < 0 or index >= int(self.grid["grid_cell_count"])
                        for index in self.station_nearest_cell_indices.values()
                    )
                ):
                    raise ContractError(
                        "icon_eps_station_precomputed_indices_invalid"
                    )
                grid_uuid = str(self.grid["grid_uuid"])
                grid_cell_count = int(self.grid["grid_cell_count"])
            else:
                grid_decompressed = _decompress_bz2(
                    grid_path,
                    grid_nc,
                    maximum_bytes=MAX_GRID_DECOMPRESSED_BYTES,
                )
                grid_uuid, grid_cell_count = _grid_metadata(grid_nc)
                if (
                    grid_uuid != self.grid["grid_uuid"]
                    or grid_cell_count != int(self.grid["grid_cell_count"])
                ):
                    raise ContractError("icon_eps_station_grid_uuid_mismatch")
            # ``grib_get -i`` is index-based and does not invoke ecCodes'
            # unsupported icosahedral ``-l`` nearest search.  It still reads
            # the exact native T_2M values for the DWD-derived indices.
            raw_budget = (
                MAX_TEMPORARY_DECODE_BYTES
                - grid_decompressed
            )
            if raw_budget <= 0:
                raise ContractError("icon_eps_station_temporary_budget_exhausted")
            raw_grib = temporary / "icon-eps-t2m.grib2"
            _decompress_bz2(raw_path, raw_grib, maximum_bytes=raw_budget)
            grouped: dict[int, dict[str, Any]] = {}
            selector = "shortName=2t,typeOfLevel=heightAboveGround,level=2"
            raw_grid_uuid, raw_cell_count = self._raw_grid_signature(
                raw_grib, grib_get=grib_get, selector=selector
            )
            _require_matching_grid_signature(
                grid_uuid=grid_uuid,
                grid_cell_count=grid_cell_count,
                raw_grid_uuid=raw_grid_uuid,
                raw_cell_count=raw_cell_count,
            )
            nearest_indices = (
                dict(self.station_nearest_cell_indices)
                if self.station_nearest_cell_indices
                else self._nearest_cell_indices(grid_nc, stations=stations)
            )
            self.station_nearest_cell_indices = dict(nearest_indices)
            fields = (
                "dataDate:i,dataTime:i,validityDate:i,validityTime:i,"
                "productDefinitionTemplateNumber:i,perturbationNumber:i,"
                "numberOfForecastsInEnsemble:i"
            )
            deadline = time.monotonic() + 180.0
            for station_id in sorted(stations):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ContractError("icon_eps_station_grib_get_total_timeout")
                stdout = self._command(
                    grib_get, "-w", selector, "-p", fields,
                    "-i", str(nearest_indices[station_id]), str(raw_grib),
                    timeout=min(20.0, max(1.0, remaining)),
                    reason="icon_eps_station_grib_get_failed",
                )
                for row in self._parse_point_rows(stdout):
                    member = int(row["member_id"])
                    entry = grouped.setdefault(member, {
                        key: row[key] for key in (
                            "member_id", "number_of_forecasts_in_ensemble",
                            "product_definition_template", "run_initialization_utc",
                            "valid_time_utc",
                        )
                    })
                    if any(entry[key] != row[key] for key in (
                        "number_of_forecasts_in_ensemble", "product_definition_template",
                        "run_initialization_utc", "valid_time_utc",
                    )) or station_id in entry.setdefault("station_values_c", {}):
                        raise ContractError("icon_eps_station_remap_member_metadata_conflict")
                    entry["station_values_c"][station_id] = row["temperature_2m_c"]
                assert_process_memory_budget()
            for member in sorted(grouped):
                yield grouped[member]


def _canonical_station_scope() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = coordinate_registry_manifest()
    station_ids = tuple(str(item) for item in manifest.get("station_ids") or ())
    if (
        manifest.get("station_count") != EXPECTED_STATION_COUNT
        or len(station_ids) != EXPECTED_STATION_COUNT
        or len(set(station_ids)) != EXPECTED_STATION_COUNT
    ):
        raise ContractError("icon_eps_station_coordinate_registry_scope_invalid")
    stations = {station_id: station_coordinates(station_id) for station_id in station_ids}
    if set(stations) != set(station_ids):
        raise ContractError("icon_eps_station_coordinate_registry_mismatch")
    return manifest, stations


def _member_records(
    rows: Iterable[Mapping[str, Any]],
    *,
    stations: Mapping[str, Mapping[str, Any]],
    capture_run: str,
    capture_valid: str,
    raw_sha256: str,
) -> list[dict[str, Any]]:
    members: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractError("icon_eps_station_adapter_row_invalid")
        try:
            member = int(row.get("member_id"))
            member_count = int(row.get("number_of_forecasts_in_ensemble"))
        except (TypeError, ValueError) as exc:
            raise ContractError("icon_eps_station_adapter_member_invalid") from exc
        if member not in EXPECTED_MEMBERS or member in members or member_count != len(EXPECTED_MEMBERS):
            raise ContractError("icon_eps_station_member_set_invalid")
        if str(row.get("product_definition_template") or "") != "4.1":
            raise ContractError("icon_eps_station_template_invalid")
        if _canonical_timestamp(row.get("run_initialization_utc"), field="grib_run") != capture_run:
            raise ContractError("icon_eps_station_run_mismatch")
        if _canonical_timestamp(row.get("valid_time_utc"), field="grib_valid") != capture_valid:
            raise ContractError("icon_eps_station_valid_time_mismatch")
        values = row.get("station_values_c")
        if not isinstance(values, Mapping) or set(values) != set(stations):
            raise ContractError("icon_eps_station_adapter_station_scope_invalid")
        members[member] = row
        if len(members) > len(EXPECTED_MEMBERS):
            raise ContractError("icon_eps_station_member_count_exceeded")
        assert_process_memory_budget()
    if tuple(sorted(members)) != EXPECTED_MEMBERS:
        raise ContractError("icon_eps_station_member_set_not_1_through_40")
    records: list[dict[str, Any]] = []
    for member in EXPECTED_MEMBERS:
        values = members[member]["station_values_c"]
        assert isinstance(values, Mapping)
        for station_id in sorted(stations):
            try:
                value = float(values[station_id])
            except (TypeError, ValueError) as exc:
                raise ContractError("icon_eps_station_temperature_invalid") from exc
            if not math.isfinite(value) or not -100.0 <= value <= 100.0:
                raise ContractError("icon_eps_station_temperature_invalid")
            records.append({
                "station_id": station_id,
                "member_id": member,
                "temperature_2m_c": round(value, 6),
                "raw_sha256": raw_sha256,
            })
    if len(records) != MAX_MEMBER_RECORDS:
        raise ContractError("icon_eps_station_record_count_invalid")
    return records


def decode_station_members(
    capture_path: Path,
    *,
    adapter: StationMemberAdapter | None = None,
    grid_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Return a content-addressed candidate-only cohort from one raw object."""

    capture, raw_path, raw_size = _capture_receipt(capture_path)
    coordinate_manifest, stations = _canonical_station_scope()
    if adapter is None and grid_receipt_path is None:
        raise ContractError("icon_eps_station_grid_receipt_required")
    active_adapter: StationMemberAdapter = adapter or OfficialDwdIconEpsAdapter(
        grid_receipt_path=grid_receipt_path  # type: ignore[arg-type]
    )
    decoder_id = str(getattr(active_adapter, "decoder_id", "") or "").strip()
    remap_contract = str(getattr(active_adapter, "remap_contract", "") or "").strip()
    if not decoder_id or not remap_contract:
        raise ContractError("icon_eps_station_adapter_lineage_missing")
    run = _canonical_timestamp(capture["run_initialization_utc"], field="run_initialization_utc")
    valid = _canonical_timestamp(capture["valid_time_utc"], field="valid_time_utc")
    records = _member_records(
        active_adapter.iter_member_station_values(raw_path, stations=stations),
        stations=stations,
        capture_run=run,
        capture_valid=valid,
        raw_sha256=str(capture["raw_sha256"]),
    )
    records_sha256 = _sha256_text(records)
    adapter_lineage = getattr(active_adapter, "lineage", None)
    source_grid_lineage = (
        adapter_lineage() if callable(adapter_lineage) else {
            "source_grid_binding": "adapter_injected_test_only"
        }
    )
    if not isinstance(source_grid_lineage, Mapping):
        raise ContractError("icon_eps_station_adapter_grid_lineage_invalid")
    cumulative_raw_bytes = raw_size + int(source_grid_lineage.get("source_grid_bytes") or 0)
    # The production adapter adds the verified grid byte count below.  Test
    # adapters intentionally keep it at zero because no synthetic grid may be
    # represented as native provider input.
    if cumulative_raw_bytes > MAX_CUMULATIVE_RAW_BYTES:
        raise ContractError("icon_eps_station_cumulative_raw_bound_exceeded")
    identity = {
        "schema": SCHEMA,
        "state": "OFFICIAL_40_MEMBER_45_STATION_DECODED_CANDIDATE_ONLY",
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": FAMILY_ID,
        "run_initialization_utc": run,
        "provider_published_at_utc": _canonical_timestamp(
            capture["provider_published_at_utc"], field="provider_published_at_utc"
        ),
        "response_completed_at_utc": _canonical_timestamp(
            capture["response_completed_at_utc"], field="response_completed_at_utc"
        ),
        "forecast_hour": int(capture["forecast_hour"]),
        "valid_time_utc": valid,
        "raw_object_ref": str(raw_path),
        "raw_sha256": str(capture["raw_sha256"]),
        "capture_receipt_sha256": str(capture["content_sha256"]),
        "raw_object_count": 1,
        "single_raw_bytes": raw_size,
        "cumulative_raw_bytes": cumulative_raw_bytes,
        "resource_guard": {
            "max_single_raw_bytes": MAX_SINGLE_RAW_BYTES,
            "max_cumulative_raw_bytes": MAX_CUMULATIVE_RAW_BYTES,
            "max_temporary_decode_bytes": MAX_TEMPORARY_DECODE_BYTES,
            "max_ncdump_coordinate_output_bytes": MAX_NCDUMP_COORDINATE_OUTPUT_BYTES,
            "streaming_grib_decode": True,
            "temporary_decompression_cleaned_after_decode": True,
            "raw_member_fanout_forbidden": True,
        },
        "member_count": len(EXPECTED_MEMBERS),
        "station_count": len(stations),
        "member_record_count": len(records),
        "station_coordinate_lineage": coordinate_manifest,
        "decoder": {
            "decoder_id": decoder_id,
            "remap_contract": remap_contract,
            "coordinate_source": "AWC_stationinfo_bound_to_DWD_R03B06_UUID_matched_nearest_cell",
        },
        "lineage": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
            "source_url": str(capture.get("source_url") or ""),
            "capture_plan_sha256": str(capture.get("plan_sha256") or ""),
            "capture_response_headers": dict(capture.get("response_headers") or {}),
            "official_grid": dict(source_grid_lineage),
        },
        "member_station_records": records,
        "member_station_records_sha256": records_sha256,
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "hard_no_eligible": False,
        "boundary": {
            "network_requests": 0,
            "writes_target_database": False,
            "writes_production_database": False,
            "activates_p0": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "single_official_raw_object_only": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def verified_precomputed_station_indices(
    reference_cohort_path: Path,
    *,
    grid_receipt_path: Path,
) -> dict[str, int]:
    """Reuse a content-addressed 45-station R03B06 nearest-cell map."""

    reference = load_bounded_json(
        reference_cohort_path.expanduser().resolve(strict=True),
        limit=MAX_CAPTURE_RECEIPT_BYTES,
    )
    grid_receipt = _validated_grid_receipt(grid_receipt_path)
    if not isinstance(reference, Mapping):
        raise ContractError("icon_eps_station_reference_cohort_invalid")
    identity = {
        key: value for key, value in reference.items() if key != "content_sha256"
    }
    lineage = reference.get("lineage")
    grid = lineage.get("official_grid") if isinstance(lineage, Mapping) else None
    indices = (
        grid.get("station_nearest_cell_indices")
        if isinstance(grid, Mapping)
        else None
    )
    _manifest, stations = _canonical_station_scope()
    if (
        reference.get("schema") != SCHEMA
        or reference.get("content_sha256") != content_sha256(identity)
        or not isinstance(indices, Mapping)
        or set(indices) != set(stations)
        or grid.get("source_grid_receipt_sha256") != grid_receipt["content_sha256"]
        or grid.get("source_grid_uuid") != grid_receipt["grid_uuid"]
        or grid.get("source_grid_cell_count") != grid_receipt["grid_cell_count"]
    ):
        raise ContractError("icon_eps_station_reference_cohort_invalid")
    result = {str(station): int(index) for station, index in indices.items()}
    if any(index < 0 or index >= int(grid_receipt["grid_cell_count"]) for index in result.values()):
        raise ContractError("icon_eps_station_reference_indices_invalid")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--grid-receipt", type=Path, default=DEFAULT_GRID_RECEIPT)
    parser.add_argument(
        "--reference-cohort",
        type=Path,
        help="Reuse its verified R03B06 45-station nearest-cell indices.",
    )
    parser.add_argument("--grid-output-root", type=Path, default=DEFAULT_GRID_ROOT)
    parser.add_argument("--fetch-official-grid", action="store_true")
    parser.add_argument("--grid-only", action="store_true")
    parser.add_argument("--grid-timeout-sec", type=float, default=900.0)
    args = parser.parse_args(argv)
    if args.grid_only and not args.fetch_official_grid:
        raise ContractError("icon_eps_station_grid_only_requires_fetch")
    if args.fetch_official_grid:
        grid = capture_official_grid(
            output_root=args.grid_output_root,
            receipt_path=args.grid_receipt,
            timeout_sec=args.grid_timeout_sec,
        )
        if args.grid_only:
            print(json.dumps(grid, ensure_ascii=False, sort_keys=True))
            return 0
    adapter = None
    if args.reference_cohort is not None:
        adapter = OfficialDwdIconEpsAdapter(
            grid_receipt_path=args.grid_receipt,
            precomputed_station_indices=verified_precomputed_station_indices(
                args.reference_cohort,
                grid_receipt_path=args.grid_receipt,
            ),
        )
    receipt = decode_station_members(
        args.capture,
        adapter=adapter,
        grid_receipt_path=None if adapter is not None else args.grid_receipt,
    )
    write_bounded_json(args.output.expanduser().resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
