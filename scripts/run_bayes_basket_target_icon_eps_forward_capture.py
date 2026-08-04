#!/usr/bin/env python3
"""Capture one bounded official DWD ICON Global EPS T_2M cohort object.

One DWD ``icon-eps`` GRIB2 file contains every ensemble member for one lead.
This adapter deliberately captures exactly one lead at a time; it does not
pretend that the object is already a decoded 40-member set or authorize P0.
The default command is plan-only.  ``--execute`` performs one bounded HTTPS
GET and writes only content-addressed raw/receipt evidence.
"""

from __future__ import annotations

import argparse
import bz2
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from official_nwp_https_transport import (  # noqa: E402
    open_official_https,
    response_read_deadline,
)


SCHEMA = "bayes_basket_target_icon_global_eps_forward_capture_v1"
PROVIDER_ID = "icon_global_eps_official"
PRODUCT_ID = "DWD_ICON_GLOBAL_EPS"
FAMILY_ID = "DWD_ICON_EPS"
HOST = "opendata.dwd.de"
BASE = "https://opendata.dwd.de/weather/nwp/icon-eps/grib"
EXPECTED_MEMBER_COUNT = 40
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_BYTES = 256 * 1024 * 1024
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
DEFAULT_OUTPUT_ROOT = (
    ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_eps_official"
)
DEFAULT_RECEIPT = ROOT / "output/audits/icon_global_eps_official_capture_latest.json"


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("icon_eps_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("icon_eps_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _latest_mature_cycle(clock: datetime) -> datetime:
    cycle = clock.replace(minute=0, second=0, microsecond=0)
    cycle = cycle.replace(hour=(cycle.hour // 6) * 6)
    # Publication of lead zero is not immediate; use the preceding cycle.
    return cycle - timedelta(hours=6)


def _url(run: datetime, forecast_hour: int) -> str:
    return (
        f"{BASE}/{run:%H}/t_2m/"
        f"icon-eps_global_icosahedral_single-level_{run:%Y%m%d%H}_"
        f"{forecast_hour:03d}_t_2m.grib2.bz2"
    )


def build_plan(
    *,
    run_initialization_utc: str | None = None,
    forecast_hour: int = 0,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    now = _utc(as_of_utc or _iso(datetime.now(timezone.utc)))
    run = _utc(run_initialization_utc) if run_initialization_utc else _latest_mature_cycle(now)
    if run.minute or run.second or run.microsecond or run.hour not in {0, 6, 12, 18}:
        raise ContractError("icon_eps_run_cycle_invalid")
    lead = int(forecast_hour)
    if lead < 0 or lead > 180:
        raise ContractError("icon_eps_forecast_hour_invalid")
    if lead > 48 and (
        (lead <= 72 and lead % 3)
        or (72 < lead <= 120 and lead % 6)
        or (lead > 120 and lead % 12)
    ):
        raise ContractError("icon_eps_forecast_hour_not_published")
    valid = run + timedelta(hours=lead)
    url = _url(run, lead)
    identity = {
        "schema": SCHEMA,
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": FAMILY_ID,
        "expected_member_count": EXPECTED_MEMBER_COUNT,
        "run_initialization_utc": _iso(run),
        "forecast_hour": lead,
        "valid_time_utc": _iso(valid),
        "source_url": url,
        "as_of_utc": _iso(now),
        "resource_budget": {
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_capture_bytes": MAX_CAPTURE_BYTES,
            "min_free_bytes": MIN_FREE_BYTES,
        },
        "authority": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
            "member_decode_required": True,
            "native_arrival_index_eligible_before_decode": False,
            "formal_p0_eligible_without_strict_oos": False,
        },
        "boundary": {
            "single_lead_only": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "plan_sha256": content_sha256(identity)}


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != HOST
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(
            r"/weather/nwp/icon-eps/grib/(00|06|12|18)/t_2m/"
            r"icon-eps_global_icosahedral_single-level_\d{10}_\d{3}_t_2m\.grib2\.bz2",
            parsed.path,
        )
    ):
        raise ContractError("icon_eps_source_url_invalid")
    return value


def _published(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("icon_eps_last_modified_invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("icon_eps_last_modified_timezone_required")
    return _iso(parsed)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_bz2(path: Path) -> None:
    # Validate the container header and first decompressed bytes without
    # expanding a global ensemble object to disk.
    with bz2.open(path, "rb") as handle:
        if handle.read(4) != b"GRIB":
            raise ContractError("icon_eps_grib_signature_invalid")


def _parse_curl_headers(path: Path) -> dict[str, str]:
    blocks = re.split(r"\r?\n\r?\n", path.read_text(encoding="iso-8859-1"))
    for block in reversed(blocks):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("HTTP/"):
            continue
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        return headers
    raise ContractError("icon_eps_curl_headers_invalid")


def _curl_download(
    url: str,
    *,
    destination: Path,
    timeout_sec: float,
) -> tuple[dict[str, str], int, str]:
    """Use system TLS only after the exact DWD URL has been validated."""

    header_path = destination.with_suffix(destination.suffix + ".headers")
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--connect-timeout",
                "20",
                "--max-time",
                str(max(1, int(timeout_sec))),
                "--max-filesize",
                str(MAX_RESPONSE_BYTES),
                "--dump-header",
                str(header_path),
                "--output",
                str(destination),
                url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=max(2.0, float(timeout_sec) + 5.0),
        )
        if result.returncode != 0:
            raise ContractError("icon_eps_curl_transport_failed")
        headers = _parse_curl_headers(header_path)
        total = destination.stat().st_size
        if total <= 0 or total > MAX_RESPONSE_BYTES:
            raise ContractError("icon_eps_response_bound_exceeded")
        return headers, total, _hash_file(destination)
    finally:
        header_path.unlink(missing_ok=True)


def execute_plan(
    plan: Mapping[str, Any],
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    receipt_path: Path = DEFAULT_RECEIPT,
    timeout_sec: float = 90.0,
    prefer_curl: bool = False,
) -> dict[str, Any]:
    if plan.get("schema") != SCHEMA:
        raise ContractError("icon_eps_plan_schema_invalid")
    identity = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("plan_sha256") != content_sha256(identity):
        raise ContractError("icon_eps_plan_sha_mismatch")
    url = _safe_url(str(plan.get("source_url") or ""))
    destination_root = output_root.expanduser().resolve()
    raw_dir = destination_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_path.expanduser().resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if receipt.is_file():
        existing = load_bounded_json(receipt)
        raw_ref = Path(str(existing.get("raw_object_ref") or ""))
        if (
            existing.get("plan_sha256") == plan["plan_sha256"]
            and raw_ref.is_file()
            and raw_ref.stat().st_size == int(existing.get("response_bytes") or -1)
            and _hash_file(raw_ref) == existing.get("raw_sha256")
        ):
            return {**existing, "state": "REUSED_VERIFIED"}

    free = shutil.disk_usage(destination_root).free
    if free < MIN_FREE_BYTES + MAX_RESPONSE_BYTES:
        raise ContractError("icon_eps_min_free_space_not_met")
    assert_target_raw_retention_budget(destination_root, bytes_needed=MAX_RESPONSE_BYTES)
    request = Request(url, headers={"User-Agent": "weather-icon-eps-native/1"})
    temporary = raw_dir / f".capture.{os.getpid()}.{time.time_ns()}.bz2"
    total = 0
    digest = hashlib.sha256()
    headers: dict[str, str] = {}
    try:
        if prefer_curl:
            headers, total, _curl_sha = _curl_download(
                url,
                destination=temporary,
                timeout_sec=timeout_sec,
            )
        else:
            try:
                with open_official_https(request, timeout_sec=timeout_sec) as response:
                    headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                    if "content-length" not in headers or "last-modified" not in headers:
                        raise ContractError("icon_eps_native_http_metadata_missing")
                    advertised = int(headers["content-length"])
                    if advertised <= 0 or advertised > MAX_RESPONSE_BYTES:
                        raise ContractError("icon_eps_response_size_invalid")
                    with temporary.open("xb") as handle, response_read_deadline(timeout_sec):
                        while True:
                            chunk = response.read(CHUNK_BYTES)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_RESPONSE_BYTES:
                                raise ContractError("icon_eps_response_bound_exceeded")
                            handle.write(chunk)
                            digest.update(chunk)
                            assert_process_memory_budget()
                    # Some TLS/proxy paths can end the stream cleanly after a
                    # truncated body.  Treat that as a transport failure and
                    # retry the exact official URL through bounded curl.
                    if total != advertised:
                        raise OSError("icon_eps_native_http_truncated_body")
            except (OSError, TimeoutError, URLError):
                temporary.unlink(missing_ok=True)
                headers, total, _curl_sha = _curl_download(
                    url,
                    destination=temporary,
                    timeout_sec=timeout_sec,
                )
        if "content-length" not in headers or "last-modified" not in headers:
            raise ContractError("icon_eps_native_http_metadata_missing")
        advertised = int(headers["content-length"])
        if advertised <= 0 or advertised > MAX_RESPONSE_BYTES:
            raise ContractError("icon_eps_response_size_invalid")
        if total != advertised:
            raise ContractError("icon_eps_content_length_mismatch")
        _validate_bz2(temporary)
        raw_sha = _hash_file(temporary)
        raw_path = raw_dir / f"{raw_sha}.grib2.bz2"
        if raw_path.is_file():
            if raw_path.stat().st_size != total or _hash_file(raw_path) != raw_sha:
                raise ContractError("icon_eps_raw_collision")
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, raw_path)
        completed = _iso(datetime.now(timezone.utc))
        result_identity = {
            **identity,
            "state": "OFFICIAL_RAW_CAPTURED_MEMBER_DECODE_PENDING",
            "plan_sha256": plan["plan_sha256"],
            "provider_published_at_utc": _published(headers["last-modified"]),
            "response_completed_at_utc": completed,
            "response_bytes": total,
            "raw_sha256": raw_sha,
            "raw_object_ref": str(raw_path),
            "response_headers": {
                key: headers[key]
                for key in ("content-length", "last-modified", "etag", "date")
                if key in headers
            },
            "member_decode_state": "PENDING_FAIL_CLOSED",
            "native_arrival_index_eligible": False,
            "formal_p0_eligible": False,
        }
        result = {**result_identity, "content_sha256": content_sha256(result_identity)}
        write_bounded_json(receipt, result)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-initialization-utc", default="")
    parser.add_argument("--forecast-hour", type=int, default=0)
    parser.add_argument("--as-of-utc", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--prefer-curl", action="store_true")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    plan = build_plan(
        run_initialization_utc=args.run_initialization_utc or None,
        forecast_hour=args.forecast_hour,
        as_of_utc=args.as_of_utc or None,
    )
    result = (
        execute_plan(
            plan,
            output_root=args.output_root,
            receipt_path=args.receipt,
            prefer_curl=args.prefer_curl,
        )
        if args.execute
        else {**plan, "state": "PLAN_ONLY"}
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
