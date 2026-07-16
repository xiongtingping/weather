#!/usr/bin/env python3
"""Collect and remap the DWD ICON ZUUU weak-source surface.

DWD publishes ICON global data on its original unstructured triangular grid.
This collector downloads the official multi-file surface, uses DWD's official
CDO 0.25° world grid/weights package for remapping, then uses ecCodes only on
the resulting regular grid.  It runs outside the AMSC/posterior hot path.
"""

from __future__ import annotations

import argparse
import bz2
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_snapshot_runtime import (  # noqa: E402
    assemble_grib2_payload,
    build_and_persist_official_nwp_snapshot,
    decode_grib2_nearest_point,
)
from official_nwp_https_transport import open_official_https  # noqa: E402


DWD_ROOT = "https://opendata.dwd.de/weather/nwp/icon/grib"
DWD_CDO_ASSET_URL = "https://opendata.dwd.de/weather/lib/cdo/ICON_GLOBAL2WORLD_025_EASY.tar.bz2"
DWD_CDO_ASSET_DIR = "ICON_GLOBAL2WORLD_025_EASY"


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


def _fetch(url: str, *, timeout_sec: float) -> tuple[bytes, dict[str, str]]:
    requested_at = _now()
    request = Request(url, headers={"User-Agent": "weather-dwd-icon-collector/1"})
    with open_official_https(request, timeout_sec=timeout_sec) as response:
        response_started_at = _now()
        body = response.read()
    return body, {"requested_at": requested_at, "response_started_at": response_started_at, "response_completed_at": _now(), "response_headers_observed": True}


def _icon_urls(run: datetime, step: int) -> list[str]:
    stamp = run.strftime("%Y%m%d%H")
    cycle = run.strftime("%H")
    lead = f"{int(step):03d}"
    single = f"icon_global_icosahedral_single-level_{stamp}_{lead}_T_2M.grib2.bz2"
    pressure = lambda directory, level, parameter: f"icon_global_icosahedral_pressure-level_{stamp}_{lead}_{level}_{parameter}.grib2.bz2"
    rows = [("t_2m", single)]
    rows += [("t", pressure("t", level, "T")) for level in (850, 700)]
    rows += [("relhum", pressure("relhum", level, "RELHUM")) for level in (850, 700)]
    rows += [("fi", pressure("fi", level, "FI")) for level in (500, 850)]
    rows += [("u", pressure("u", level, "U")) for level in (850, 700)]
    rows += [("v", pressure("v", level, "V")) for level in (850, 700)]
    return [f"{DWD_ROOT}/{cycle}/{directory}/{filename}" for directory, filename in rows]


def _ensure_cdo_assets(*, support_root: Path, timeout_sec: float) -> tuple[Path, Path, dict[str, Any]]:
    target = support_root / DWD_CDO_ASSET_DIR
    grid = target / "target_grid_world_025.txt"
    weights = target / "weights_icogl2world_025.nc"
    if grid.is_file() and weights.is_file():
        return grid, weights, {"state": "cached", "asset_url": DWD_CDO_ASSET_URL}
    body, timing = _fetch(DWD_CDO_ASSET_URL, timeout_sec=timeout_sec)
    archive = support_root / "raw" / f"{hashlib.sha256(body).hexdigest()}.tar.bz2"
    if not archive.is_file():
        _atomic_bytes(archive, body)
    subprocess.run(["tar", "-xjf", str(archive), "-C", str(support_root)], check=True, capture_output=True, text=True, timeout=60)
    if not grid.is_file() or not weights.is_file():
        raise RuntimeError("dwd_icon_cdo_assets_incomplete")
    return grid, weights, {"state": "downloaded", "asset_url": DWD_CDO_ASSET_URL, "asset_raw_object_ref": str(archive), "asset_raw_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), **timing}


def _decompress_bz2(source: Path, destination: Path) -> None:
    with bz2.open(source, "rb") as input_handle, destination.open("wb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle)


def _fetch_icon_payloads(urls: list[str], *, timeout_sec: float) -> list[tuple[str, bytes, dict[str, str]]]:
    """Fetch independent DWD parameter files concurrently, preserving URL order.

    The response receipt remains one per direct DWD body.  Concurrency changes
    wall-clock time only; it never merges transport timing or source identity.
    """

    completed: dict[int, tuple[str, bytes, dict[str, str]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(urls)))) as executor:
        futures = {executor.submit(_fetch, url, timeout_sec=timeout_sec): (index, url) for index, url in enumerate(urls)}
        for future in as_completed(futures):
            index, url = futures[future]
            body, timing = future.result()
            completed[index] = (url, body, timing)
    return [completed[index] for index in range(len(urls))]


def collect(*, run_at_utc: str, valid_time_utc: str, source_root: Path, catalog_path: Path, timeout_sec: float) -> dict[str, Any]:
    try:
        run = datetime.fromisoformat(run_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        valid = datetime.fromisoformat(valid_time_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        lead_seconds = (valid - run).total_seconds()
        if lead_seconds < 0 or lead_seconds % 3600:
            return {"ok": False, "state": "blocked", "blockers": ["dwd_icon_run_valid_time_invalid"]}
        cdo = shutil.which("cdo")
        if not cdo:
            return {"ok": False, "state": "blocked", "blockers": ["dwd_icon_cdo_unavailable"]}
        support_root = source_root / "icon" / "support"
        grid, weights, asset_receipt = _ensure_cdo_assets(support_root=support_root, timeout_sec=timeout_sec)
        raw_paths: list[Path] = []
        raw_receipts: list[dict[str, Any]] = []
        for url, body, timing in _fetch_icon_payloads(_icon_urls(run, int(lead_seconds // 3600)), timeout_sec=timeout_sec):
            if not body:
                return {"ok": False, "state": "blocked", "blockers": ["dwd_icon_direct_fetch_empty_body"]}
            digest = hashlib.sha256(body).hexdigest()
            raw_path = source_root / "icon" / "raw" / f"{digest}.grib2.bz2"
            if raw_path.is_file() and raw_path.read_bytes() != body:
                return {"ok": False, "state": "blocked", "blockers": ["dwd_icon_raw_sha256_identity_collision"]}
            if not raw_path.is_file():
                _atomic_bytes(raw_path, body)
            raw_paths.append(raw_path)
            raw_receipts.append({"raw_request_url": url, "raw_object_ref": str(raw_path.resolve()), "raw_sha256": digest, **timing})
        temporary_root = Path(tempfile.mkdtemp(prefix="dwd-icon-zuuu-"))
        try:
            combined_input = temporary_root / "source-combined.grib2"
            with combined_input.open("wb") as output_handle:
                for raw_path in raw_paths:
                    with bz2.open(raw_path, "rb") as input_handle:
                        shutil.copyfileobj(input_handle, output_handle)
            remapped_path = temporary_root / "remapped-combined.grib2"
            completed = subprocess.run([cdo, "-s", "-f", "grb2", f"remap,{grid},{weights}", str(combined_input), str(remapped_path)], check=False, capture_output=True, text=True, timeout=120)
            if completed.returncode != 0 or not remapped_path.is_file() or remapped_path.stat().st_size == 0:
                return {"ok": False, "state": "blocked", "blockers": ["dwd_icon_cdo_remap_failed"], "cdo_stderr_tail": completed.stderr[-1000:]}
            combined_path, temporary_combined = assemble_grib2_payload([remapped_path])
            try:
                decoded = decode_grib2_nearest_point(combined_path, provider_id="icon")
            finally:
                temporary_combined.unlink(missing_ok=True)
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)
        manifest = {"schema": "dwd_icon_multifile_cdo_receipt_v1", "provider_id": "icon", "run_at_utc": run_at_utc, "valid_time_utc": valid_time_utc, "raw_receipts": raw_receipts, "cdo_asset_receipt": asset_receipt, "cdo_command_contract": "remap,target_grid_world_025,weights_icogl2world_025", "open_meteo": False, "aggregator_or_republisher": False}
        manifest_digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        manifest_path = source_root / "icon" / "receipts" / f"{manifest_digest}.json"
        if not manifest_path.is_file():
            _atomic_json(manifest_path, manifest)
        completed_at = max(receipt["response_completed_at"] for receipt in raw_receipts)
        persisted = build_and_persist_official_nwp_snapshot(catalog_path=catalog_path, provider_id="icon", raw_path=raw_paths[0], raw_receipt_path=manifest_path, raw_request_url=raw_receipts[0]["raw_request_url"], raw_request_urls=[receipt["raw_request_url"] for receipt in raw_receipts], raw_payload_sha256s=[str(receipt["raw_sha256"]) for receipt in raw_receipts], run_at_utc=run_at_utc, valid_time_utc=valid_time_utc, response_completed_at=completed_at, requested_at=min(str(receipt.get("requested_at") or "") for receipt in raw_receipts), response_started_at=max(str(receipt.get("response_started_at") or "") for receipt in raw_receipts), response_headers_observed=all(receipt.get("response_headers_observed") is True for receipt in raw_receipts), decoded={**decoded, "decoder_id": "dwd_icon_cdo_025_eccodes_nearest_point_v1", "decoder_version": f"{decoded.get('decoder_version')};cdo"}, current_path=source_root / "icon" / "current.json")
        return {**persisted, "raw_object_ref": str(manifest_path), "raw_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "raw_receipts": raw_receipts, "boundary": {"writes_archive_db": False, "writes_posterior": False, "writes_task_queue": False, "places_orders": False}}
    except Exception as exc:
        return {"ok": False, "state": "blocked", "blockers": [f"dwd_icon_collect_failed:{type(exc).__name__}"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-at-utc", required=True)
    parser.add_argument("--valid-time-utc", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_sources")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    args = parser.parse_args(argv)
    result = collect(run_at_utc=args.run_at_utc, valid_time_utc=args.valid_time_utc, source_root=args.source_root.expanduser().resolve(), catalog_path=args.catalog.expanduser().resolve(), timeout_sec=args.timeout_sec)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
