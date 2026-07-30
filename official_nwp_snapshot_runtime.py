"""Decode and persist one direct official-NWP point snapshot.

Network acquisition is deliberately outside this module.  Callers provide an
already archived GRIB payload and a credential-safe request URL; this module
uses ecCodes' ``grib_get`` nearest-point mode, then validates and atomically
publishes the normalized provider snapshot.
"""

from __future__ import annotations

import bz2
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from official_nwp_source_catalog_runtime import (
    NORMALIZED_SNAPSHOT_SCHEMA,
    load_official_nwp_catalog,
    validate_official_nwp_catalog,
    validate_official_nwp_snapshot,
)


DECODER_ID = "eccodes_grib_get_nearest_point_v1"
STATION_LATITUDE = 30.576
STATION_LONGITUDE = 103.950
_SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "key", "token", "access_token", "signature", "password", "secret"}
_FIELD_REQUESTS = (
    ("temperature_2m_c", "shortName=2t,typeOfLevel=heightAboveGround,level=2", "kelvin_to_celsius"),
    ("temperature_850_c", "shortName=t,typeOfLevel=isobaricInhPa,level=850", "kelvin_to_celsius"),
    ("temperature_700_c", "shortName=t,typeOfLevel=isobaricInhPa,level=700", "kelvin_to_celsius"),
    ("relative_humidity_850_pct", "shortName=r,typeOfLevel=isobaricInhPa,level=850", "identity"),
    ("relative_humidity_700_pct", "shortName=r,typeOfLevel=isobaricInhPa,level=700", "identity"),
    ("geopotential_height_500_m", "shortName=gh,typeOfLevel=isobaricInhPa,level=500", "identity"),
    ("geopotential_height_850_m", "shortName=gh,typeOfLevel=isobaricInhPa,level=850", "identity"),
    ("u_wind_850_mps", "shortName=u,typeOfLevel=isobaricInhPa,level=850", "identity"),
    ("v_wind_850_mps", "shortName=v,typeOfLevel=isobaricInhPa,level=850", "identity"),
    ("u_wind_700_mps", "shortName=u,typeOfLevel=isobaricInhPa,level=700", "identity"),
    ("v_wind_700_mps", "shortName=v,typeOfLevel=isobaricInhPa,level=700", "identity"),
)


def _field_requests_for_provider(provider_id: str) -> tuple[tuple[str, str, str], ...]:
    """Return provider-specific GRIB aliases after any documented remap."""

    rows = list(_FIELD_REQUESTS)
    if provider_id == "icon":
        # DWD ICON's FI becomes GRIB shortName=z and retains geopotential
        # units after CDO remap; convert m²/s² to geometric height in metres.
        rows = [(field, selector.replace("shortName=gh,", "shortName=z,"), "geopotential_to_height" if field.startswith("geopotential_height_") else conversion) for field, selector, conversion in rows]
        rows += [
            ("total_cloud_cover_pct", "shortName=CLCT,typeOfLevel=surface,level=0", "identity"),
            ("low_cloud_cover_pct", "shortName=CLCL,level=800", "identity"),
            ("middle_cloud_cover_pct", "shortName=CLCM,typeOfLevel=isobaricLayer,level=400", "identity"),
            ("high_cloud_cover_pct", "shortName=CLCH,typeOfLevel=isobaricLayer,level=0", "identity"),
            ("total_precipitation_mm", "shortName=tp,typeOfLevel=surface,level=0", "identity"),
            ("convective_available_potential_energy_j_per_kg", "shortName=CAPE_ML,level=0", "identity"),
            # Preserve DWD local GRIB identities across CDO remap; ecCodes
            # may rename 6/27 to hcct and leave 6/26 without a shortName.
            ("convective_cloud_base_msl_m", "discipline=0,parameterCategory=6,parameterNumber=26,level=0", "identity"),
            ("convective_cloud_top_msl_m", "discipline=0,parameterCategory=6,parameterNumber=27,level=0", "identity"),
            ("boundary_layer_momentum_transfer_coefficient", "shortName=cd,typeOfLevel=surface,level=0", "identity"),
        ]
    elif provider_id == "ecmwf":
        rows += [
            ("total_cloud_cover_pct", "shortName=tcc,typeOfLevel=entireAtmosphere,level=0", "fraction_to_pct"),
            ("total_precipitation_mm", "shortName=tp,typeOfLevel=surface,level=0", "metres_to_mm"),
            ("convective_available_potential_energy_j_per_kg", "shortName=mucape,typeOfLevel=mostUnstableParcel,level=0", "identity"),
            ("boundary_layer_gust_mps", "shortName=10fg,typeOfLevel=heightAboveGround,level=10", "identity"),
            ("eastward_turbulent_surface_stress_n_m2_s", "shortName=ewss,typeOfLevel=surface,level=0", "identity"),
            ("northward_turbulent_surface_stress_n_m2_s", "shortName=nsss,typeOfLevel=surface,level=0", "identity"),
        ]
    elif provider_id == "gfs":
        rows += [
            ("total_cloud_cover_pct", "shortName=tcc,typeOfLevel=atmosphere,level=0,stepType=instant", "identity"),
            ("total_precipitation_mm", "shortName=tp,typeOfLevel=surface,level=0", "identity"),
            ("convective_precipitation_mm", "shortName=acpcp,typeOfLevel=surface,level=0", "identity"),
            ("convective_available_potential_energy_j_per_kg", "shortName=cape,typeOfLevel=surface,level=0", "identity"),
            # NCEP local table 4.2-0-3-196 is HPBL.  Some ecCodes builds do
            # not assign it a shortName, so bind the official GRIB identity.
            ("boundary_layer_height_m", "parameterCategory=3,parameterNumber=196,typeOfLevel=surface,level=0", "identity"),
        ]
    return tuple(rows)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    material = {key: value for key, value in dict(snapshot).items() if key != "snapshot_sha256"}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _persist_immutable_snapshot(path: Path, snapshot: Mapping[str, Any]) -> tuple[bool, str]:
    """Append a content-addressed snapshot; never overwrite a history object."""

    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False, "official_nwp_snapshot_history_unreadable"
        if existing != dict(snapshot):
            return False, "official_nwp_snapshot_history_sha256_collision"
        return True, ""
    _atomic_json(path, snapshot)
    return True, ""


def credential_safe_request_url(value: str) -> str:
    """Accept public filtering parameters but reject credential-shaped query keys."""

    parsed = urlsplit(_text(value))
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    if any(key.lower() in _SENSITIVE_QUERY_KEYS for key, _item in parse_qsl(parsed.query, keep_blank_values=True)):
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _grib_get_binary(value: str = "") -> str:
    candidates = [_text(value), shutil.which("grib_get") or "", "/opt/homebrew/opt/eccodes/bin/grib_get"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _decompressed_grib_path(raw_path: Path) -> tuple[Path, Path | None]:
    suffix = raw_path.suffix.lower()
    if suffix not in {".gz", ".bz2"}:
        return raw_path, None
    handle = tempfile.NamedTemporaryFile(prefix="official-nwp-", suffix=".grib2", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            opener = gzip.open if suffix == ".gz" else bz2.open
            with opener(raw_path, "rb") as source:
                shutil.copyfileobj(source, handle)
        return temporary, temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def assemble_grib2_payload(raw_paths: list[Path]) -> tuple[Path, Path]:
    """Concatenate decompressed direct GRIB messages for one decoder call."""

    handle = tempfile.NamedTemporaryFile(prefix="official-nwp-joint-", suffix=".grib2", delete=False)
    output = Path(handle.name)
    temporary_inputs: list[Path] = []
    try:
        with handle:
            for raw_path in raw_paths:
                payload_path, temporary = _decompressed_grib_path(raw_path)
                if temporary is not None:
                    temporary_inputs.append(temporary)
                with payload_path.open("rb") as source:
                    shutil.copyfileobj(source, handle)
        return output, output
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        for temporary in temporary_inputs:
            temporary.unlink(missing_ok=True)


def _parse_nearest_record(stdout: str) -> tuple[float | None, dict[str, Any]]:
    for line in reversed(str(stdout or "").splitlines()):
        tokens = line.split()
        if not tokens:
            continue
        try:
            value = float(tokens[-1])
        except (TypeError, ValueError):
            continue
        metadata: dict[str, Any] = {}
        if len(tokens) >= 5:
            try:
                metadata["start_step"] = int(tokens[0])
                metadata["end_step"] = int(tokens[1])
            except ValueError:
                pass
            metadata["step_type"] = tokens[2]
            metadata["units"] = " ".join(tokens[3:-1])
            if "start_step" in metadata and "end_step" in metadata:
                metadata["accumulation_hours"] = metadata["end_step"] - metadata["start_step"]
        return value, metadata
    return None, {}


def _convert_field_value(value: float, conversion: str) -> float:
    if conversion == "kelvin_to_celsius":
        return value - 273.15 if value > 150.0 else value
    if conversion == "geopotential_to_height":
        return value / 9.80665
    if conversion == "metres_to_mm":
        return value * 1000.0
    if conversion == "fraction_to_pct":
        return value * 100.0 if abs(value) <= 1.000001 else value
    return value


def decode_grib2_nearest_point(
    raw_path: Path,
    *,
    provider_id: str = "",
    latitude: float = STATION_LATITUDE,
    longitude: float = STATION_LONGITUDE,
    grib_get_binary: str = "",
) -> dict[str, Any]:
    """Extract the fixed ZUUU weak-source feature surface from one GRIB payload."""

    binary = _grib_get_binary(grib_get_binary)
    if not binary:
        return {"ok": False, "state": "blocked", "blockers": ["official_nwp_eccodes_grib_get_unavailable"]}
    if not raw_path.is_file():
        return {"ok": False, "state": "blocked", "blockers": ["official_nwp_raw_grib_missing"]}
    temporary: Path | None = None
    try:
        grib_path, temporary = _decompressed_grib_path(raw_path)
        features: dict[str, float] = {}
        traces: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for field, selector, conversion in _field_requests_for_provider(provider_id):
            command = [
                binary,
                "-w",
                selector,
                "-p",
                "startStep,endStep,stepType,units",
                "-l",
                f"{float(latitude):.6f},{float(longitude):.6f},1",
                str(grib_path),
            ]
            completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
            value, metadata = _parse_nearest_record(completed.stdout) if completed.returncode == 0 else (None, {})
            if value is None:
                missing.append(field)
                continue
            converted = _convert_field_value(value, conversion)
            features[field] = round(float(converted), 6)
            traces[field] = {"grib_selector": selector, "conversion": conversion, "nearest_latitude": latitude, "nearest_longitude": longitude, **metadata}
            if field == "total_precipitation_mm" and metadata.get("step_type") == "accum":
                features["total_precipitation_accumulation_hours"] = float(metadata.get("accumulation_hours") or 0)
            if field == "convective_precipitation_mm" and metadata.get("step_type") == "accum":
                features["convective_precipitation_accumulation_hours"] = float(metadata.get("accumulation_hours") or 0)
    except (OSError, subprocess.SubprocessError, TimeoutError, EOFError) as exc:
        return {"ok": False, "state": "blocked", "blockers": [f"official_nwp_grib_decode_failed:{type(exc).__name__}"]}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "ok": not missing,
        "state": "available" if not missing else "blocked",
        "blockers": [f"official_nwp_grib_field_missing:{field}" for field in missing],
        "normalized_features": features,
        "decoder_id": DECODER_ID,
        "decoder_version": _decoder_version(binary),
        "field_trace": traces,
    }


def _decoder_version(binary: str) -> str:
    try:
        completed = subprocess.run([binary, "-V"], check=False, capture_output=True, text=True, timeout=5)
        first = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
        return first or "unknown"
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return "unknown"


def build_and_persist_official_nwp_snapshot(
    *,
    catalog_path: Path,
    provider_id: str,
    raw_path: Path,
    raw_request_url: str,
    raw_request_urls: list[str] | None = None,
    raw_payload_sha256s: list[str] | None = None,
    raw_receipt_path: Path | None = None,
    run_at_utc: str,
    valid_time_utc: str,
    response_completed_at: str,
    requested_at: str = "",
    response_started_at: str = "",
    response_headers_observed: bool | None = None,
    decoded: Mapping[str, Any],
    current_path: Path,
) -> dict[str, Any]:
    """Validate and atomically publish a decoded direct-provider snapshot."""

    catalog_result = validate_official_nwp_catalog(load_official_nwp_catalog(catalog_path))
    catalog = _mapping(catalog_result.get("catalog"))
    provider = _mapping(_mapping(catalog.get("providers")).get(provider_id))
    safe_url = credential_safe_request_url(raw_request_url)
    receipt_path = raw_receipt_path or raw_path
    raw_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest() if receipt_path.is_file() else ""
    safe_urls = [credential_safe_request_url(item) for item in (raw_request_urls or [raw_request_url])]
    payload_hashes = [str(item or "").strip() for item in (raw_payload_sha256s or []) if str(item or "").strip()]
    snapshot = {
        "schema": NORMALIZED_SNAPSHOT_SCHEMA,
        "provider_id": provider_id,
        "source_id": _text(provider.get("source_id")),
        "model_id": _text(provider.get("model_id")),
        "run_at_utc": _text(run_at_utc),
        "valid_time_utc": _text(valid_time_utc),
        "requested_at": _text(requested_at),
        "response_started_at": _text(response_started_at),
        "response_headers_observed": response_headers_observed,
        "response_completed_at": _text(response_completed_at),
        "raw_object_ref": str(receipt_path.resolve()),
        "raw_sha256": raw_sha256,
        # This is the direct provider-body identity.  ``raw_sha256`` above
        # may be a transport receipt manifest hash (and therefore includes
        # arrival timestamps), so it cannot by itself distinguish a retry
        # from a changed provider payload.
        "raw_payload_sha256s": payload_hashes,
        "raw_request_url": safe_url,
        "raw_request_urls": safe_urls,
        "decoder_id": _text(_mapping(decoded).get("decoder_id")),
        "decoder_version": _text(_mapping(decoded).get("decoder_version")),
        "normalized_features": _mapping(_mapping(decoded).get("normalized_features")),
        "decoder_field_trace": _mapping(_mapping(decoded).get("field_trace")),
        "aggregator_or_republisher": False,
        "open_meteo": False,
        "captured_at": _iso_now(),
    }
    snapshot["snapshot_sha256"] = _snapshot_sha256(snapshot)
    validation = validate_official_nwp_snapshot(snapshot, provider_id=provider_id, catalog=catalog)
    if catalog_result.get("ok") is not True or decoded.get("ok") is not True or validation.get("ok") is not True:
        return {
            "schema": "official_nwp_snapshot_persist_result_v1",
            "ok": False,
            "state": "blocked",
            "blockers": sorted(set(list(catalog_result.get("issues") or []) + list(decoded.get("blockers") or []) + list(validation.get("issues") or []))),
            "snapshot": {},
        }
    history_path = current_path.parent / "history" / f"{snapshot['snapshot_sha256']}.json"
    history_ok, history_error = _persist_immutable_snapshot(history_path, snapshot)
    if not history_ok:
        return {"schema": "official_nwp_snapshot_persist_result_v1", "ok": False, "state": "blocked", "blockers": [history_error], "snapshot": {}}
    _atomic_json(current_path, snapshot)
    return {"schema": "official_nwp_snapshot_persist_result_v1", "ok": True, "state": "available", "snapshot": snapshot, "history_path": str(history_path.resolve())}


__all__ = [
    "DECODER_ID",
    "build_and_persist_official_nwp_snapshot",
    "assemble_grib2_payload",
    "credential_safe_request_url",
    "decode_grib2_nearest_point",
]
