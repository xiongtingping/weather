#!/usr/bin/env python3
"""Collect a direct ECMWF Open Data IFS subset for the ZUUU weak source.

Run this script with the dedicated ``weather-official-nwp-venv`` interpreter.
It uses ECMWF's official request-based Open Data client, not a mirrored whole
file or an aggregator.  Two server-side subsets (2m temperature and pressure
levels) are immutable-receipted, concatenated only for local ecCodes decoding,
and published as one provider snapshot outside the posterior hot path.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_snapshot_runtime import (  # noqa: E402
    assemble_grib2_payload,
    build_and_persist_official_nwp_snapshot,
    decode_grib2_nearest_point,
)


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


def _as_url_ranges(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in getattr(result, "urls", []) or []:
        try:
            url, ranges = item
        except (TypeError, ValueError):
            continue
        rows.append({"url": str(url), "byte_ranges": [list(pair) for pair in ranges or []]})
    return rows


def _retrieve(client: Any, *, request: dict[str, Any], target: Path) -> tuple[Any, dict[str, Any]]:
    requested_at = _now()
    result = client.retrieve(request=request, target=str(target))
    completed_at = _now()
    return result, {"request": request, "requested_at": requested_at, "response_completed_at": completed_at, "response_headers_observed": False, "url_ranges": _as_url_ranges(result)}


def collect(
    *,
    run_at_utc: str,
    valid_time_utc: str,
    source_root: Path,
    catalog_path: Path,
) -> dict[str, Any]:
    try:
        from ecmwf.opendata import Client
    except ImportError:
        return {"ok": False, "state": "blocked", "blockers": ["ecmwf_opendata_runtime_unavailable"]}
    run = datetime.fromisoformat(run_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    valid = datetime.fromisoformat(valid_time_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    lead_seconds = (valid - run).total_seconds()
    step_hours = int(lead_seconds // 3600)
    if step_hours < 0 or lead_seconds != step_hours * 3600:
        return {"ok": False, "state": "blocked", "blockers": ["ecmwf_run_valid_time_invalid"]}
    temporary_root = Path(tempfile.mkdtemp(prefix="ecmwf-zuuu-"))
    try:
        client = Client(source="ecmwf", model="ifs", resol="0p25")
        # The workstation's development proxy can return a synthetic 503 for
        # PyPI/ECMWF even while direct HTTPS is healthy.  Official Open Data
        # transport must use its own direct, certificate-verified session;
        # it must never silently downgrade TLS verification or treat a proxy
        # error as an ECMWF availability signal.
        client.session.trust_env = False
        shared = {"date": run.date().isoformat(), "time": run.hour, "stream": "oper", "type": "fc", "step": step_hours}
        surface_path = temporary_root / "surface.grib2"
        pressure_path = temporary_root / "pressure.grib2"
        surface_result, surface_receipt = _retrieve(client, request={**shared, "param": ["2t"]}, target=surface_path)
        pressure_result, pressure_receipt = _retrieve(
            client,
            request={**shared, "levtype": "pl", "levelist": [500, 700, 850], "param": ["t", "r", "gh", "u", "v"]},
            target=pressure_path,
        )
        raw_paths: list[Path] = []
        raw_receipts: list[dict[str, Any]] = []
        for source_path, receipt in ((surface_path, surface_receipt), (pressure_path, pressure_receipt)):
            body = source_path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            stored = source_root / "ecmwf" / "raw" / f"{digest}.grib2"
            if stored.is_file() and stored.read_bytes() != body:
                return {"ok": False, "state": "blocked", "blockers": ["ecmwf_raw_sha256_identity_collision"]}
            if not stored.is_file():
                _atomic_bytes(stored, body)
            urls = [row["url"] for row in receipt["url_ranges"]]
            if not urls:
                return {"ok": False, "state": "blocked", "blockers": ["ecmwf_official_request_url_missing"]}
            raw_paths.append(stored)
            raw_receipts.append({**receipt, "raw_object_ref": str(stored.resolve()), "raw_sha256": digest, "raw_request_urls": urls})
        manifest = {"schema": "ecmwf_open_data_subset_receipt_v1", "provider_id": "ecmwf", "run_at_utc": run_at_utc, "valid_time_utc": valid_time_utc, "raw_receipts": raw_receipts, "open_meteo": False, "aggregator_or_republisher": False}
        manifest_digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        manifest_path = source_root / "ecmwf" / "receipts" / f"{manifest_digest}.json"
        if not manifest_path.is_file():
            _atomic_json(manifest_path, manifest)
        manifest_file_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        combined_path, temporary_combined = assemble_grib2_payload(raw_paths)
        try:
            decoded = decode_grib2_nearest_point(combined_path)
        finally:
            temporary_combined.unlink(missing_ok=True)
        raw_urls = [url for receipt in raw_receipts for url in receipt["raw_request_urls"]]
        completed_at = max(str(receipt["response_completed_at"]) for receipt in raw_receipts)
        persisted = build_and_persist_official_nwp_snapshot(
            catalog_path=catalog_path,
            provider_id="ecmwf",
            raw_path=raw_paths[0],
            raw_receipt_path=manifest_path,
            raw_request_url=raw_urls[0],
            raw_request_urls=raw_urls,
            raw_payload_sha256s=[str(receipt["raw_sha256"]) for receipt in raw_receipts],
            run_at_utc=run_at_utc,
            valid_time_utc=valid_time_utc,
            response_completed_at=completed_at,
            requested_at=min(str(receipt.get("requested_at") or "") for receipt in raw_receipts),
            response_headers_observed=False,
            decoded=decoded,
            current_path=source_root / "ecmwf" / "current.json",
        )
        return {**persisted, "raw_object_ref": str(manifest_path), "raw_sha256": manifest_file_sha256, "raw_receipts": raw_receipts, "boundary": {"writes_archive_db": False, "writes_posterior": False, "writes_task_queue": False, "places_orders": False}}
    except Exception as exc:
        return {"ok": False, "state": "blocked", "blockers": [f"ecmwf_official_subset_collect_failed:{type(exc).__name__}"]}
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-at-utc", required=True)
    parser.add_argument("--valid-time-utc", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_sources")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    args = parser.parse_args(argv)
    result = collect(run_at_utc=args.run_at_utc, valid_time_utc=args.valid_time_utc, source_root=args.source_root.expanduser().resolve(), catalog_path=args.catalog.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
