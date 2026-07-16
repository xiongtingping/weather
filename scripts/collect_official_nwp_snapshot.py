#!/usr/bin/env python3
"""Fetch one direct official GRIB payload, decode it, and publish its snapshot.

This collector is intentionally run outside the AMSC/posterior hot path.  It
accepts only a credential-safe HTTPS URL whose host is allowlisted in the
official source catalog; it never accepts an aggregator, stores credentials,
writes archive.db, creates a posterior, or places orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_snapshot_runtime import (  # noqa: E402
    assemble_grib2_payload,
    build_and_persist_official_nwp_snapshot,
    credential_safe_request_url,
    decode_grib2_nearest_point,
)
from official_nwp_source_catalog_runtime import (  # noqa: E402
    load_official_nwp_catalog,
    validate_official_nwp_catalog,
)
from official_nwp_https_transport import open_official_https  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _suffix_from_url(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    for suffix in (".grib2.bz2", ".grib2.gz", ".grib2", ".grb2.bz2", ".grb2.gz", ".grb2"):
        if path.endswith(suffix):
            return suffix
    return ".grib2"


def _fetch_failure_reason(exc: Exception) -> str:
    """Return a credential-safe, actionable direct-transport classification."""

    if isinstance(exc, HTTPError):
        return f"http_{int(exc.code)}"
    if isinstance(exc, URLError):
        return f"url_{type(exc.reason).__name__}"
    return type(exc).__name__


def collect(
    *,
    catalog_path: Path,
    provider_id: str,
    raw_urls: list[str],
    run_at_utc: str,
    valid_time_utc: str,
    source_root: Path,
    timeout_sec: float,
) -> dict[str, Any]:
    catalog_result = validate_official_nwp_catalog(load_official_nwp_catalog(catalog_path))
    if catalog_result.get("ok") is not True:
        return {"ok": False, "state": "blocked", "blockers": list(catalog_result.get("issues") or [])}
    safe_urls = [credential_safe_request_url(raw_url) for raw_url in raw_urls]
    if not safe_urls or any(not value for value in safe_urls):
        return {"ok": False, "state": "blocked", "blockers": ["official_nwp_raw_url_not_https_or_contains_credential"]}
    provider = (catalog_result.get("catalog") or {}).get("providers", {}).get(provider_id, {})
    if str(provider.get("collector_adapter") or "") != "noaa_nomads_filter_grib2_latlon_subset":
        return {
            "ok": False,
            "state": "blocked",
            "blockers": [f"official_nwp_provider_requires_dedicated_adapter:{provider.get('collector_adapter') or 'missing'}"],
        }
    allowed_hosts = {str(host).strip().lower() for host in provider.get("allowed_hosts", []) if str(host).strip()}
    if any((urlsplit(safe_url).hostname or "").lower() not in allowed_hosts for safe_url in safe_urls):
        return {"ok": False, "state": "blocked", "blockers": ["official_nwp_raw_request_host_not_allowlisted"]}
    receipts: list[dict[str, str]] = []
    raw_paths: list[Path] = []
    for safe_url in safe_urls:
        requested_at = _now()
        try:
            request = Request(safe_url, headers={"User-Agent": "weather-official-nwp-collector/1"})
            with open_official_https(request, timeout_sec=timeout_sec) as response:
                response_started_at = _now()
                body = response.read()
            response_completed_at = _now()
        except Exception as exc:
            return {
                "ok": False,
                "state": "blocked",
                "blockers": [f"official_nwp_direct_fetch_failed:{_fetch_failure_reason(exc)}"],
                "requested_at": requested_at,
            }
        if not body:
            return {"ok": False, "state": "blocked", "blockers": ["official_nwp_direct_fetch_empty_body"], "requested_at": requested_at}
        digest = hashlib.sha256(body).hexdigest()
        raw_path = source_root / provider_id / "raw" / f"{digest}{_suffix_from_url(safe_url)}"
        if raw_path.is_file() and raw_path.read_bytes() != body:
            return {"ok": False, "state": "blocked", "blockers": ["official_nwp_raw_sha256_identity_collision"]}
        if not raw_path.is_file():
            _atomic_bytes(raw_path, body)
        raw_paths.append(raw_path)
        receipts.append({"raw_request_url": safe_url, "raw_object_ref": str(raw_path.resolve()), "raw_sha256": digest, "requested_at": requested_at, "response_started_at": response_started_at, "response_completed_at": response_completed_at, "response_headers_observed": True})
    manifest = {"schema": "official_nwp_raw_receipt_manifest_v1", "provider_id": provider_id, "run_at_utc": run_at_utc, "valid_time_utc": valid_time_utc, "raw_receipts": receipts, "aggregator_or_republisher": False, "open_meteo": False}
    manifest_digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    manifest_path = source_root / provider_id / "receipts" / f"{manifest_digest}.json"
    if not manifest_path.is_file():
        _atomic_json(manifest_path, manifest)
    decode_path, temporary_decode_path = assemble_grib2_payload(raw_paths)
    try:
        decoded = decode_grib2_nearest_point(decode_path)
    finally:
        temporary_decode_path.unlink(missing_ok=True)
    response_completed_at = max(receipt["response_completed_at"] for receipt in receipts)
    persisted = build_and_persist_official_nwp_snapshot(
        catalog_path=catalog_path,
        provider_id=provider_id,
        raw_path=raw_paths[0],
        raw_request_url=safe_urls[0],
        raw_request_urls=safe_urls,
        raw_payload_sha256s=[receipt["raw_sha256"] for receipt in receipts],
        raw_receipt_path=manifest_path,
        run_at_utc=run_at_utc,
        valid_time_utc=valid_time_utc,
        response_completed_at=response_completed_at,
        requested_at=min(receipt["requested_at"] for receipt in receipts),
        response_started_at=max(receipt["response_started_at"] for receipt in receipts),
        response_headers_observed=all(receipt["response_headers_observed"] is True for receipt in receipts),
        decoded=decoded,
        current_path=source_root / provider_id / "current.json",
    )
    return {
        **persisted,
        "requested_at": receipts[0]["requested_at"],
        "response_started_at": receipts[0]["response_started_at"],
        "response_completed_at": response_completed_at,
        "raw_object_ref": str(manifest_path),
        "raw_sha256": manifest_digest,
        "raw_receipts": receipts,
        "boundary": {"writes_archive_db": False, "writes_posterior": False, "writes_task_queue": False, "places_orders": False},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("ecmwf", "icon", "gfs", "jma"))
    parser.add_argument("--raw-url", action="append", required=True, help="direct official/authorised GRIB URL; repeat for a multi-file source surface")
    parser.add_argument("--run-at-utc", required=True)
    parser.add_argument("--valid-time-utc", required=True)
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_sources")
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    args = parser.parse_args(argv)
    result = collect(
        catalog_path=args.catalog.expanduser().resolve(),
        provider_id=args.provider,
        raw_urls=list(args.raw_url),
        run_at_utc=args.run_at_utc,
        valid_time_utc=args.valid_time_utc,
        source_root=args.source_root.expanduser().resolve(),
        timeout_sec=args.timeout_sec,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
