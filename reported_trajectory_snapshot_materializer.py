"""Materialize AMSC TAF and official NWP station snapshots.

This module is intentionally a weak-source adapter.  It never writes a hard
observation, decision, intent, basket cycle, or order.  Network acquisition is
kept outside the database transaction; only immutable provider identities are
committed once the payload has parsed and lineage is complete.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from amsc_taf_revision_runtime import parse_taf_revision
from official_nwp_snapshot_runtime import (
    _convert_field_value,
    _field_requests_for_provider,
    _grib_get_binary,
    _parse_nearest_record,
    assemble_grib2_payload,
    decode_grib2_nearest_point,
)
from reported_trajectory_sources import utc_now
from reported_trajectory_taf_sources import fetch_amsc_taf_reports


SCHEMA_VERSION = "reported_trajectory_snapshot_materializer_v1"
CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loc_parts(loc_key: str) -> tuple[float, float]:
    parts = [part.strip() for part in _text(loc_key).split(",")]
    if len(parts) != 2:
        raise ValueError("station_loc_key_invalid")
    return float(parts[0]), float(parts[1])


def _event_date(valid_time_utc: str) -> str:
    value = datetime.fromisoformat(_text(valid_time_utc).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CHINA_TZ).date().isoformat()


def _raw_payload_paths(raw_object_ref: str) -> tuple[list[Path], Path]:
    """Resolve a normalized snapshot's receipt to its immutable GRIB payloads.

    The v2 collectors deliberately store the receipt manifest in
    ``raw_object_ref`` so transport identity is retained.  The manifest's
    ``raw_receipts[*].raw_object_ref`` entries are the actual GRIB objects.
    Passing the manifest JSON to ecCodes made every projection look empty and
    could spend the decoder timeout on each field.  Return both the payload
    list used for decoding and the receipt path retained for lineage.
    """

    receipt = Path(_text(raw_object_ref)).expanduser()
    if not receipt.is_file():
        return [], receipt
    if receipt.suffix.lower() not in {".json"}:
        return [receipt], receipt
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], receipt
    if not isinstance(payload, Mapping):
        return [], receipt
    candidates: list[Path] = []
    for item in payload.get("raw_receipts") or []:
        if not isinstance(item, Mapping):
            continue
        candidate = Path(_text(item.get("raw_object_ref"))).expanduser()
        if candidate.is_file():
            candidates.append(candidate)
    # A legacy receipt may nest range receipts rather than exposing one raw
    # object directly.  Keep the explicit payload identity if present.
    if not candidates:
        candidate = Path(_text(payload.get("raw_object_ref"))).expanduser()
        if candidate.is_file() and candidate.suffix.lower() != ".json":
            candidates.append(candidate)
    return candidates, receipt


def _icon_minimal_projection_paths(receipt_ref: Path, raw_paths: Sequence[Path]) -> list[Path]:
    """Keep only ICON fields consumed by the current production weak surface."""

    try:
        manifest = json.loads(receipt_ref.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return list(raw_paths)
    selected: list[Path] = []
    required_fragments = ("_T_2M", "_CLCT", "_TOT_PREC", "_850_RELHUM", "_700_RELHUM")
    for item in manifest.get("raw_receipts") or []:
        if not isinstance(item, Mapping):
            continue
        url = _text(item.get("raw_request_url")).upper()
        if not any(fragment in url for fragment in required_fragments):
            continue
        candidate = Path(_text(item.get("raw_object_ref"))).expanduser()
        if candidate.is_file():
            selected.append(candidate)
    # A malformed/legacy receipt must fail back to its explicit raw identity;
    # it must not silently publish a partial source under a complete label.
    return selected if len(selected) == len(required_fragments) else list(raw_paths)


def _station_in_declared_source_window(item: Mapping[str, Any], *, latitude: float, longitude: float) -> bool:
    """Avoid decoding a station from a deliberately regional GFS payload."""

    if _text(item.get("provider_id")).lower() != "gfs":
        return True
    url = _text(item.get("raw_request_url"))
    query = parse_qs(urlsplit(url).query)
    try:
        left = float((query.get("leftlon") or [""])[0])
        right = float((query.get("rightlon") or [""])[0])
        bottom = float((query.get("bottomlat") or [""])[0])
        top = float((query.get("toplat") or [""])[0])
    except (TypeError, ValueError):
        return True
    return left <= longitude <= right and bottom <= latitude <= top


def _prepare_decode_payload(
    provider_id: str,
    raw_paths: Sequence[Path],
    *,
    source_root: Path,
    station_items: Sequence[Mapping[str, Any]],
    projection_fields: Sequence[str] | None = None,
) -> tuple[Path, list[Path], bool]:
    """Build one decoder payload and compact regular-grid sources to stations."""

    del projection_fields
    decode_path, assembled_temporary = assemble_grib2_payload(list(raw_paths))
    temporaries = [assembled_temporary]
    provider = _text(provider_id).lower()
    if provider == "gfs":
        return decode_path, temporaries, False
    cdo = shutil.which("cdo") or ("/opt/homebrew/bin/cdo" if Path("/opt/homebrew/bin/cdo").is_file() else "/usr/local/bin/cdo")
    cdo_path = Path(cdo)
    if not cdo_path.is_file():
        raise ValueError("official_nwp_cdo_unavailable")
    source_for_points = decode_path
    if provider == "icon":
        support = Path(source_root) / "icon" / "support" / "ICON_GLOBAL2WORLD_025_EASY"
        grid = support / "target_grid_world_025.txt"
        weights = support / "weights_icogl2world_025.nc"
        if not grid.is_file() or not weights.is_file():
            raise ValueError("dwd_icon_cdo_projection_assets_unavailable")
        handle = tempfile.NamedTemporaryFile(prefix="official-nwp-icon-remapped-", suffix=".grib2", delete=False)
        source_for_points = Path(handle.name)
        handle.close()
        completed = subprocess.run(
            [str(cdo_path), "-s", "-f", "grb2", f"remap,{grid},{weights}", str(decode_path), str(source_for_points)],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0 or not source_for_points.is_file() or source_for_points.stat().st_size <= 0:
            source_for_points.unlink(missing_ok=True)
            raise ValueError("dwd_icon_cdo_remap_failed")
        temporaries.append(source_for_points)
    # Decode all configured stations from one tiny seven-point regularized
    # payload.  ``grib_get -l`` cannot query the original ICON unstructured
    # grid; the point order is retained as a deterministic station index.
    grid_handle = tempfile.NamedTemporaryFile(prefix="official-nwp-stations-", suffix=".txt", mode="w", delete=False)
    station_grid = Path(grid_handle.name)
    try:
        coordinates = [_loc_parts(_text(item.get("loc_key"))) for item in station_items]
        grid_handle.write("gridtype = unstructured\n")
        grid_handle.write(f"gridsize = {len(coordinates)}\n")
        grid_handle.write("xvals = " + " ".join(f"{lon:.6f}" for _lat, lon in coordinates) + "\n")
        grid_handle.write("yvals = " + " ".join(f"{lat:.6f}" for lat, _lon in coordinates) + "\n")
    finally:
        grid_handle.close()
    station_handle = tempfile.NamedTemporaryFile(prefix="official-nwp-station-points-", suffix=".nc", delete=False)
    station_points = Path(station_handle.name)
    station_handle.close()
    point_remap = subprocess.run(
        [str(cdo_path), "-s", "-f", "nc4", f"remapnn,{station_grid}", str(source_for_points), str(station_points)],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if point_remap.returncode != 0 or not station_points.is_file() or station_points.stat().st_size <= 0:
        station_points.unlink(missing_ok=True)
        station_grid.unlink(missing_ok=True)
        raise ValueError("official_nwp_station_point_remap_failed")
    temporaries.extend([station_grid, station_points])
    return station_points, temporaries, True


def _decode_cdo_station_points(
    source_path: Path,
    *,
    provider_id: str,
    station_items: Sequence[Mapping[str, Any]],
    projection_fields: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Read every field and station from one CDO table invocation."""

    cdo = shutil.which("cdo") or ("/opt/homebrew/bin/cdo" if Path("/opt/homebrew/bin/cdo").is_file() else "/usr/local/bin/cdo")
    if not Path(cdo).is_file():
        return [{"ok": False, "state": "blocked", "blockers": ["official_nwp_cdo_unavailable"]} for _ in station_items]
    provider = _text(provider_id).lower()
    requested_fields = {str(value).strip() for value in (projection_fields or ()) if str(value).strip()}
    requests = tuple(
        row for row in _field_requests_for_provider(provider)
        if not requested_fields or row[0] in requested_fields
    )
    # CDO names preserve the official shortName after remapping.  These ICON
    # local parameters have no shortName in the source selector, so bind their
    # documented CDO names explicitly.
    aliases = {
        "convective_cloud_base_msl_m": {"hbas_con", "param26.6.0"},
        "convective_cloud_top_msl_m": {"htop_con", "hcct", "param27.6.0"},
        "boundary_layer_momentum_transfer_coefficient": {"cd"},
    }
    expected: dict[str, tuple[set[str], str, str]] = {}
    for field, selector, conversion in requests:
        pieces = {part.split("=", 1)[0]: part.split("=", 1)[1] for part in selector.split(",") if "=" in part}
        names = {str(pieces["shortName"]).lower()} if pieces.get("shortName") else {name.lower() for name in aliases.get(field, set())}
        raw_level = str(pieces.get("level") or "")
        try:
            numeric_level = float(raw_level)
            # CDO writes pressure levels in Pa while the official selectors
            # and source contract use hPa.
            if provider in {"ecmwf", "icon"} and numeric_level >= 100.0:
                numeric_level *= 100.0
            expected_level = str(numeric_level)
        except ValueError:
            expected_level = raw_level
        expected[field] = (names, expected_level, conversion)
    points = [
        {
            "features": {},
            "traces": {},
            "lat": _loc_parts(_text(item.get("loc_key")))[0],
            "lon": _loc_parts(_text(item.get("loc_key")))[1],
        }
        for item in station_items
    ]
    try:
        completed = subprocess.run(
            [str(cdo), "-s", "outputtab,nohead,name,lev,lon,lat,value", str(source_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return [{"ok": False, "state": "blocked", "blockers": [f"official_nwp_cdo_table_failed:{type(exc).__name__}"]} for _ in station_items]
    if completed.returncode != 0:
        return [{"ok": False, "state": "blocked", "blockers": ["official_nwp_cdo_table_failed"]} for _ in station_items]
    for raw_line in completed.stdout.splitlines():
        tokens = raw_line.split()
        if len(tokens) < 5:
            continue
        name = tokens[0].strip()
        try:
            level = float(tokens[1])
            lon = float(tokens[2])
            lat = float(tokens[3])
            value = float(tokens[4])
        except (TypeError, ValueError):
            continue
        station_index = min(
            range(len(points)),
            key=lambda index: abs(points[index]["lat"] - lat) + abs(points[index]["lon"] - lon),
        )
        if abs(points[station_index]["lat"] - lat) > 0.01 or abs(points[station_index]["lon"] - lon) > 0.01:
            continue
        for field, (names, expected_level, conversion) in expected.items():
            if name.lower() not in names:
                continue
            if expected_level and abs(level - float(expected_level)) > 0.01:
                continue
            if field in points[station_index]["features"]:
                continue
            points[station_index]["features"][field] = round(float(_convert_field_value(value, conversion)), 6)
            points[station_index]["traces"][field] = {
                "cdo_variable_name": name,
                "cdo_level": level,
                "conversion": conversion,
                "station_index": station_index,
                "nearest_latitude": points[station_index]["lat"],
                "nearest_longitude": points[station_index]["lon"],
            }
            break
    output: list[dict[str, Any]] = []
    for point in points:
        missing = [field for field in expected if field not in point["features"]]
        output.append({
            "ok": not missing,
            "state": "available" if not missing else "blocked",
            "blockers": [f"official_nwp_grib_field_missing:{field}" for field in missing],
            "normalized_features": point["features"],
            "decoder_id": "cdo_station_point_outputtab_v1",
            "field_trace": point["traces"],
        })
    return output


def _decode_gfs_temperature_point(
    raw_path: Path,
    *,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    """Project the display-only GFS surface temperature without full fields.

    GFS is explicitly excluded from the production basket weak-source set.
    Its regional payload is retained for board comparison, so projecting the
    one UI-required temperature field is sufficient and avoids decoding
    unrelated pressure/accumulation fields for every retry of the same run.
    """

    binary = _grib_get_binary("")
    if not binary:
        return {"ok": False, "state": "blocked", "blockers": ["official_nwp_eccodes_grib_get_unavailable"]}
    command = [
        binary,
        "-w",
        "shortName=2t,typeOfLevel=heightAboveGround,level=2",
        "-p",
        "startStep,endStep,stepType,units",
        "-l",
        f"{float(latitude):.6f},{float(longitude):.6f},1",
        str(raw_path),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return {"ok": False, "state": "blocked", "blockers": [f"official_nwp_gfs_temperature_decode_failed:{type(exc).__name__}"]}
    value, metadata = _parse_nearest_record(completed.stdout) if completed.returncode == 0 else (None, {})
    if value is None:
        return {"ok": False, "state": "blocked", "blockers": ["official_nwp_gfs_temperature_point_missing"]}
    return {
        "ok": True,
        "state": "available",
        "blockers": [],
        "normalized_features": {"temperature_2m_c": round(float(_convert_field_value(value, "kelvin_to_celsius")), 6)},
        "decoder_id": "eccodes_grib_get_gfs_temperature_only_v1",
        "field_trace": {
            "temperature_2m_c": {
                "grib_selector": "shortName=2t,typeOfLevel=heightAboveGround,level=2",
                "conversion": "kelvin_to_celsius",
                "nearest_latitude": float(latitude),
                "nearest_longitude": float(longitude),
                **metadata,
            }
        },
    }


def materialize_amsc_taf(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str],
    target_event_date: str,
    count: int = 12,
    timeout: float = 8.0,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Fetch and persist official FC/FT TAF peaks for the requested day."""

    target = _text(target_event_date)[:10]
    checked_at = _text(now_utc) or utc_now()
    poll = fetch_amsc_taf_reports(station_ids, count=count, timeout=timeout)
    inserted = 0
    parsed = 0
    rejected: list[str] = []
    no_peak: list[str] = []
    for report in poll.get("reports") or []:
        if not isinstance(report, Mapping):
            continue
        station = _text(report.get("station_id")).upper()
        raw = _text(report.get("raw_text"))
        if not station or not raw:
            continue
        first_seen = _text(report.get("source_report_time"))
        if first_seen and "T" not in first_seen:
            first_seen = first_seen.replace(" ", "T") + "Z"
        if not first_seen:
            first_seen = checked_at
        try:
            parsed_revision = parse_taf_revision(
                raw,
                first_seen_at=first_seen,
                station_id=station,
                source_id="amsc_taf_forecast",
            )
        except (TypeError, ValueError) as exc:
            if str(exc) == "amsc_taf_tx_missing":
                no_peak.append(station)
                continue
            rejected.append(f"{station}:{type(exc).__name__}:{exc}")
            continue
        parsed += 1
        peaks = [peak for peak in parsed_revision.get("forecast_peaks") or [] if isinstance(peak, Mapping)]
        for peak in peaks:
            if _text(peak.get("event_date_local")) != target:
                continue
            issue = _text(parsed_revision.get("issue_time_utc"))
            revision_id = _text(parsed_revision.get("source_revision_id"))
            peak_time = _text(peak.get("valid_time_utc"))
            lineage = {
                "schema": "amsc_taf_snapshot_lineage_v1",
                "source": "AMSC",
                "endpoint": "https://www.amsc.net.cn/gateway/api/saas/rest/amc/ReportController/messageRetrievalSort",
                "transport": poll.get("transport") or "official_amsc_taf_batch_cccc",
                "report_type": "TAF",
                "source_revision_id": revision_id,
                "raw_sha256": parsed_revision.get("raw_sha256") or "",
                "retrieved_at": checked_at,
                "weak_forecast_only": True,
                "hard_observation_eligible": False,
            }
            conn.execute(
                """INSERT OR IGNORE INTO amsc_taf_snapshots(
                    source_revision_id,event_date,station_id,source_lane,
                    forecast_revision_kind,taf_modifier,issue_time_utc,
                    first_system_seen_at,validity_token,peak_c,peak_valid_time_utc,
                    forecast_weather_regime,forecast_peaks_json,raw_sha256,raw_text,
                    source_report_time,lineage_json,state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    revision_id,
                    target,
                    station,
                    "amsc_taf",
                    _text(parsed_revision.get("forecast_revision_kind")) or "original",
                    _text(parsed_revision.get("taf_modifier")),
                    issue,
                    _text(parsed_revision.get("first_system_seen_at")) or checked_at,
                    _text(parsed_revision.get("validity_token")),
                    float(peak.get("temperature_c")),
                    peak_time,
                    _text(peak.get("forecast_weather_regime")) or "unknown_forecast_regime",
                    _json(peaks),
                    _text(parsed_revision.get("raw_sha256")),
                    raw,
                    _text(report.get("source_report_time")),
                    _json(lineage),
                    "available",
                    checked_at,
                ),
            )
            inserted += int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    conn.commit()
    return {
        "schema": SCHEMA_VERSION,
        "ok": not rejected,
        "state": "available" if inserted or parsed else "empty",
        "target_event_date": target,
        "station_count": len({str(value).strip().upper() for value in station_ids if str(value).strip()}),
        "report_count": len(poll.get("reports") or []),
        "parsed_revision_count": parsed,
        "inserted_peak_count": inserted,
        "no_peak_revision_count": len(no_peak),
        "rejected": rejected,
        "transport": poll.get("transport") or "",
        "places_orders": False,
    }


def _iter_normalized_snapshots(source_root: Path, *, target_event_date: str) -> Iterable[dict[str, Any]]:
    target = _text(target_event_date)[:10]
    for provider_dir in sorted(source_root.iterdir() if source_root.is_dir() else []):
        if not provider_dir.is_dir() or provider_dir.name not in {"gfs", "icon", "ecmwf"}:
            continue
        for path in sorted((provider_dir / "history").glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(item, Mapping) or _text(item.get("schema")) != "official_nwp_normalized_snapshot_v1":
                continue
            valid = _text(item.get("valid_time_utc"))
            if not valid or _event_date(valid) != target:
                continue
            yield {**dict(item), "provider_id": _text(item.get("provider_id")) or provider_dir.name}


def _target_valid_times(*, target_event_date: str, provider_id: str) -> set[str]:
    """Return the valid-time contract for one local China station day.

    GFS and ICON are hourly products in the normalized archive.  ECMWF is a
    three-hourly product.  Selecting the newest file by ``run_at_utc`` alone
    is unsafe: a newly arrived run can contain only the first few target-day
    hours while an older run already contains the complete day.  A run is
    therefore eligible only after it covers this complete target-day grid.
    """

    provider = _text(provider_id).lower()
    step_hours = 3 if provider == "ecmwf" else 1
    local_start = datetime.fromisoformat(_text(target_event_date)[:10]).replace(tzinfo=CHINA_TZ)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=1)
    # ECMWF valid times are aligned to 00/03/06... UTC, not to the local-day
    # boundary (16Z in China).  Keep only those provider-native slots that
    # fall inside the target local day.
    phase_start = datetime.combine(start_utc.date(), datetime.min.time(), tzinfo=timezone.utc)
    return {
        (phase_start + timedelta(hours=offset)).isoformat().replace("+00:00", "Z")
        for offset in range(0, 48, step_hours)
        if start_utc <= phase_start + timedelta(hours=offset) < end_utc
    }


def _select_latest_complete_run(
    rows: Sequence[Mapping[str, Any]],
    *,
    target_event_date: str,
) -> tuple[str, set[str]]:
    """Select the newest run that fully covers the provider's target grid."""

    by_run: dict[str, set[str]] = {}
    for item in rows:
        provider = _text(item.get("provider_id"))
        run_at = _text(item.get("run_at_utc"))
        valid_at = _text(item.get("valid_time_utc"))
        if not provider or not run_at or not valid_at:
            continue
        by_run.setdefault(run_at, set()).add(valid_at)
    if not by_run:
        return "", set()
    expected = _target_valid_times(
        target_event_date=target_event_date,
        provider_id=_text(rows[0].get("provider_id")),
    )
    complete = [run for run, valid_times in by_run.items() if expected.issubset(valid_times)]
    if not complete:
        return "", expected
    return max(complete), expected


def materialize_official_nwp_station_snapshots(
    conn: sqlite3.Connection,
    *,
    stations: Sequence[Mapping[str, Any]],
    target_event_date: str,
    source_root: Path,
    max_snapshots: int = 96,
    decode_missing_station_points: bool = True,
    providers: Sequence[str] | None = None,
    valid_times: Sequence[str] | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Project immutable official NWP payloads onto configured station grids.

    Existing normalized snapshots carry the raw provider object reference.  If
    their field trace is for another station, the same immutable GRIB object is
    decoded at the configured station coordinate, so multi-city rows retain the
    original provider/run/valid identity rather than copying Chengdu values.
    """

    checked_at = _text(now_utc) or utc_now()
    provider_filter = {str(value).strip().lower() for value in (providers or ()) if str(value).strip()}
    valid_time_filter = {str(value).strip() for value in (valid_times or ()) if str(value).strip()}
    # Run completeness must be decided from the provider's complete target-day
    # inventory.  Applying ``valid_times`` before this check makes a bounded
    # batch (for example six ICON slots) look like an incomplete run and
    # silently materializes nothing.  Keep the batch filter for the final
    # projection selection only.
    source_rows = [
        item for item in _iter_normalized_snapshots(Path(source_root), target_event_date=target_event_date)
        if (not provider_filter or _text(item.get("provider_id")).lower() in provider_filter)
    ]
    # One representative per provider/run/valid/raw object keeps the projection
    # bounded when a source was retried or has multiple field envelopes.
    unique_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in source_rows:
        key = (_text(item.get("provider_id")), _text(item.get("run_at_utc")), _text(item.get("valid_time_utc")))
        previous = unique_by_identity.get(key)
        if previous is None:
            unique_by_identity[key] = item
            continue
        # Retries can publish multiple immutable receipts for the same
        # provider/run/valid identity.  Project one deterministic receipt;
        # retaining every retry multiplied decompression and produced false
        # duplicate station snapshots.
        score = (
            len(item.get("decoder_field_trace") or {}) if isinstance(item.get("decoder_field_trace"), Mapping) else 0,
            _text(item.get("response_completed_at")),
            _text(item.get("snapshot_sha256")),
        )
        previous_score = (
            len(previous.get("decoder_field_trace") or {}) if isinstance(previous.get("decoder_field_trace"), Mapping) else 0,
            _text(previous.get("response_completed_at")),
            _text(previous.get("snapshot_sha256")),
        )
        if score > previous_score:
            unique_by_identity[key] = item
    unique = {
        (provider, run_at, valid_at, _text(item.get("raw_object_ref"))): item
        for (provider, run_at, valid_at), item in unique_by_identity.items()
    }
    # A source directory contains several historical runs for the same target
    # day.  Pick the newest *complete* target-day run per provider.  The
    # newest run itself is often still a partial forecast and must not replace
    # a complete earlier run in either the board or the production weak curve.
    selected_run_by_provider: dict[str, str] = {}
    expected_by_provider: dict[str, set[str]] = {}
    for provider in sorted({_text(item.get("provider_id")) for item in unique.values()}):
        provider_rows = [item for item in unique.values() if _text(item.get("provider_id")) == provider]
        selected_run, expected = _select_latest_complete_run(provider_rows, target_event_date=target_event_date)
        if selected_run:
            selected_run_by_provider[provider] = selected_run
        expected_by_provider[provider] = expected
    rows = [
        item for item in unique.values()
        if _text(item.get("run_at_utc")) == selected_run_by_provider.get(_text(item.get("provider_id")), "")
        and (not valid_time_filter or _text(item.get("valid_time_utc")) in valid_time_filter)
    ]
    rows.sort(key=lambda item: (_text(item.get("provider_id")), _text(item.get("valid_time_utc")), _text(item.get("snapshot_sha256"))))
    rows = rows[: max(1, int(max_snapshots))]
    inserted = 0
    decoded = 0
    blocked = 0
    pending_rows: list[tuple[Any, ...]] = []
    station_items = [dict(item) for item in stations if isinstance(item, Mapping) and _text(item.get("station_id")) and _text(item.get("loc_key"))]
    for item in rows:
        provider = _text(item.get("provider_id"))
        raw_paths, receipt_ref = _raw_payload_paths(_text(item.get("raw_object_ref")))
        if not raw_paths:
            blocked += 1
            continue
        projection_fields: tuple[str, ...] | None = None
        if provider.lower() == "icon":
            raw_paths = _icon_minimal_projection_paths(receipt_ref, raw_paths)
            projection_fields = (
                "temperature_2m_c",
                "total_cloud_cover_pct",
                "total_precipitation_mm",
                "relative_humidity_850_pct",
                "relative_humidity_700_pct",
            )
        # Always normalize the receipt to one uncompressed payload once per
        # source snapshot.  Calling the decoder directly on .bz2/.gz for each
        # station used to decompress the same large ICON receipt once per
        # station and made a full-city projection look hung.
        decode_temporaries: list[Path] = []
        indexed_station_payload = False
        try:
            decode_path, decode_temporaries, indexed_station_payload = _prepare_decode_payload(
                provider,
                raw_paths,
                source_root=Path(source_root),
                station_items=station_items,
                projection_fields=projection_fields,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            blocked += len(station_items)
            continue
        trace = item.get("decoder_field_trace") if isinstance(item.get("decoder_field_trace"), Mapping) else {}
        indexed_results = (
            _decode_cdo_station_points(
                decode_path,
                provider_id=provider,
                station_items=station_items,
                projection_fields=projection_fields,
            )
            if indexed_station_payload
            else []
        )
        try:
            for station_index, station in enumerate(station_items):
                station_id = _text(station.get("station_id")).upper()
                try:
                    lat, lon = _loc_parts(_text(station.get("loc_key")))
                except ValueError:
                    blocked += 1
                    continue
                if not _station_in_declared_source_window(item, latitude=lat, longitude=lon):
                    blocked += 1
                    continue
                features: Mapping[str, Any] = {}
                # Reuse a normalized point only when its decoder trace explicitly
                # proves the same coordinate.  Otherwise decode the immutable raw.
                trace_row = trace.get("temperature_2m_c") if isinstance(trace, Mapping) else None
                same_point = isinstance(trace_row, Mapping) and abs(float(trace_row.get("nearest_latitude") or 1e9) - lat) < 1e-5 and abs(float(trace_row.get("nearest_longitude") or 1e9) - lon) < 1e-5
                if same_point:
                    features = item.get("normalized_features") if isinstance(item.get("normalized_features"), Mapping) else {}
                elif decode_missing_station_points and indexed_station_payload:
                    result = indexed_results[station_index] if station_index < len(indexed_results) else {"ok": False, "state": "blocked"}
                    decoded += 1
                    if result.get("ok") is not True:
                        blocked += 1
                        continue
                    features = result.get("normalized_features") if isinstance(result.get("normalized_features"), Mapping) else {}
                elif decode_missing_station_points:
                    result = (
                        _decode_gfs_temperature_point(decode_path, latitude=lat, longitude=lon)
                        if provider.lower() == "gfs"
                        else decode_grib2_nearest_point(decode_path, provider_id=provider, latitude=lat, longitude=lon)
                    )
                    decoded += 1
                    if result.get("ok") is not True:
                        blocked += 1
                        continue
                    features = result.get("normalized_features") if isinstance(result.get("normalized_features"), Mapping) else {}
                else:
                    continue
                try:
                    temp = float(features.get("temperature_2m_c"))
                except (TypeError, ValueError):
                    temp = None
                if temp is None:
                    blocked += 1
                    continue
                source_sha = _text(item.get("snapshot_sha256"))
                identity = {
                    "provider_id": provider,
                    "station_id": station_id,
                    "loc_key": _text(station.get("loc_key")),
                    "run_at_utc": _text(item.get("run_at_utc")),
                    "valid_time_utc": _text(item.get("valid_time_utc")),
                    "source_snapshot_sha256": source_sha,
                    "temperature_2m_c": round(temp, 6),
                }
                snapshot_id = "nwpst_" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()[:24]
                lineage = {
                    "schema": "official_nwp_station_snapshot_lineage_v1",
                    "provider_id": provider,
                    "station_id": station_id,
                    "loc_key": _text(station.get("loc_key")),
                    "source_snapshot_sha256": source_sha,
                    "receipt_manifest_ref": str(receipt_ref.resolve()),
                    "raw_payload_refs": [str(path.resolve()) for path in raw_paths],
                    "raw_sha256": _text(item.get("raw_sha256")),
                    "raw_payload_sha256s": list(item.get("raw_payload_sha256s") or []),
                    "decoder_id": _text(item.get("decoder_id")),
                    "projection_surface": (
                        "temperature_2m_only" if provider.lower() == "gfs"
                        else "temperature_cloud_precipitation_humidity_minimal" if provider.lower() == "icon"
                        else "full_official_station_features"
                    ),
                    "production_weak_source_role": "display_only_gfs_excluded_from_basket" if provider.lower() == "gfs" else "official_station_weak_source",
                    "decoded_at": checked_at,
                    "aggregator_or_republisher": False,
                    "open_meteo": False,
                }
                # Decode every station point before opening the SQLite write
                # transaction.  A multi-payload GRIB decode can take seconds;
                # inserting the first point here used to hold the shared
                # writer lock for the remainder of the decode loop and starve
                # the physical-probability ingress.
                pending_rows.append(
                    (
                        snapshot_id,
                        provider,
                        station_id,
                        _text(station.get("loc_key")),
                        _event_date(_text(item.get("valid_time_utc"))),
                        _text(item.get("run_at_utc")),
                        _text(item.get("valid_time_utc")),
                        temp,
                        _json(features),
                        str(receipt_ref.resolve()),
                        _text(item.get("raw_sha256")),
                        source_sha,
                        _json(lineage),
                        "available",
                        checked_at,
                    )
                )
        finally:
            for temporary in decode_temporaries:
                temporary.unlink(missing_ok=True)
    # Keep the write section bounded: all expensive source resolution and GRIB
    # decoding has completed before this point.
    for row in pending_rows:
        conn.execute(
            """INSERT OR IGNORE INTO official_nwp_station_snapshots(
                snapshot_id,provider_id,station_id,loc_key,event_date,run_at_utc,
                valid_time_utc,temperature_2m_c,normalized_features_json,
                raw_object_ref,raw_sha256,source_snapshot_sha256,lineage_json,
                state,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        inserted += int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    conn.commit()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available" if inserted else "empty",
        "target_event_date": _text(target_event_date)[:10],
        "source_snapshot_count": len(rows),
        "selected_run_by_provider": selected_run_by_provider,
        "complete_target_slots_by_provider": {
            provider: len(expected_by_provider.get(provider) or set())
            for provider in sorted(expected_by_provider)
        },
        "incomplete_provider_runs": sorted(
            provider for provider in expected_by_provider if provider not in selected_run_by_provider
        ),
        "decoded_station_points": decoded,
        "inserted_snapshot_count": inserted,
        "blocked_count": blocked,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "materialize_amsc_taf", "materialize_official_nwp_station_snapshots"]
