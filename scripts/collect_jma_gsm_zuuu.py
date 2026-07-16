#!/usr/bin/env python3
"""Collect one JMA GSM official/authorised GRIB payload when provisioned.

No delivery URL, host, or credential is invented here.  Before an approved
delivery contract is configured the result is an explicit missing-source
blocker, allowing the unique weak model to use another source combination.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_snapshot_runtime import build_and_persist_official_nwp_snapshot, credential_safe_request_url, decode_grib2_nearest_point  # noqa: E402
from official_nwp_source_catalog_runtime import load_official_nwp_catalog, validate_official_nwp_catalog  # noqa: E402
from official_nwp_https_transport import open_official_https  # noqa: E402


CONTRACT_PATH = ROOT / "config" / "forecast_sources" / "jma_authorized_delivery_v1.json"


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


def _contract(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def collect(*, run_at_utc: str, valid_time_utc: str, source_root: Path, catalog_path: Path, contract_path: Path = CONTRACT_PATH, timeout_sec: float = 45.0) -> dict:
    contract = _contract(contract_path)
    if contract.get("schema") != "jma_authorized_delivery_contract_v1" or contract.get("state") != "approved":
        return {"ok": False, "state": "blocked", "blockers": ["jma_authorized_delivery_not_configured"]}
    endpoint = credential_safe_request_url(os.environ.get(str(contract.get("endpoint_env") or ""), ""))
    expected_host = str(contract.get("approved_delivery_host") or "").strip().lower()
    if not endpoint or not expected_host or (urlsplit(endpoint).hostname or "").lower() != expected_host:
        return {"ok": False, "state": "blocked", "blockers": ["jma_authorized_delivery_endpoint_or_host_invalid"]}
    catalog_result = validate_official_nwp_catalog(load_official_nwp_catalog(catalog_path))
    provider = ((catalog_result.get("catalog") or {}).get("providers") or {}).get("jma") or {}
    allowed_hosts = {str(item).strip().lower() for item in provider.get("allowed_hosts") or []}
    if expected_host not in allowed_hosts:
        return {"ok": False, "state": "blocked", "blockers": ["jma_authorized_delivery_host_not_in_official_catalog"]}
    headers = {"User-Agent": "weather-jma-gsm-collector/1"}
    token = os.environ.get(str(contract.get("bearer_token_env") or ""), "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    requested_at = _now()
    try:
        with open_official_https(Request(endpoint, headers=headers), timeout_sec=timeout_sec) as response:
            response_started_at = _now()
            body = response.read()
        response_completed_at = _now()
    except Exception as exc:
        return {"ok": False, "state": "blocked", "blockers": [f"jma_authorized_delivery_fetch_failed:{type(exc).__name__}"], "requested_at": requested_at}
    if not body:
        return {"ok": False, "state": "blocked", "blockers": ["jma_authorized_delivery_empty_body"]}
    digest = hashlib.sha256(body).hexdigest()
    raw_path = source_root / "jma" / "raw" / f"{digest}.grib2"
    if raw_path.is_file() and raw_path.read_bytes() != body:
        return {"ok": False, "state": "blocked", "blockers": ["jma_authorized_delivery_raw_identity_collision"]}
    if not raw_path.is_file():
        _atomic_bytes(raw_path, body)
    decoded = decode_grib2_nearest_point(raw_path, provider_id="jma")
    result = build_and_persist_official_nwp_snapshot(catalog_path=catalog_path, provider_id="jma", raw_path=raw_path, raw_request_url=endpoint, raw_request_urls=[endpoint], run_at_utc=run_at_utc, valid_time_utc=valid_time_utc, response_completed_at=response_completed_at, requested_at=requested_at, response_started_at=response_started_at, response_headers_observed=True, decoded=decoded, current_path=source_root / "jma" / "current.json")
    return {**result, "requested_at": requested_at, "response_started_at": response_started_at, "response_completed_at": response_completed_at, "raw_object_ref": str(raw_path), "raw_sha256": digest, "response_headers_observed": True, "boundary": {"writes_archive_db": False, "writes_posterior": False, "writes_task_queue": False, "places_orders": False}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-at-utc", required=True)
    parser.add_argument("--valid-time-utc", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_sources")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    args = parser.parse_args(argv)
    result = collect(run_at_utc=args.run_at_utc, valid_time_utc=args.valid_time_utc, source_root=args.source_root.expanduser().resolve(), catalog_path=args.catalog.expanduser().resolve(), contract_path=args.contract.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
