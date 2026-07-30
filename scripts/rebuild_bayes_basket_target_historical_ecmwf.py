#!/usr/bin/env python3
"""Rebuild settlement-time-compatible ECMWF native evidence from the official archive.

ECMWF's public files are date-addressable but do not expose a small GRIB
index.  This bounded adapter streams one forecast file at a time to a guarded
temporary file, extracts only the native 2-m-temperature message with
ecCodes, retains that small message plus source hashes, and deletes the full
temporary file.  The result is retrospective research evidence, never an
arrival receipt or a calibration-authorized input.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import subprocess
import tempfile
import time as time_module
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import assert_event_day_closed  # noqa: E402
from bayes_basket_target.native_model_decode import (  # noqa: E402
    decode_native_temperature_points,
    materialize_native_daily_curves,
    materialize_native_daily_curves_by_station_timezone,
)
from bayes_basket_target.model_curve_schedule import aligned_target_sample_offsets  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    MAX_TARGET_PROCESS_RSS_BYTES,
    MAX_TARGET_RESEARCH_RAW_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.settlement_time import historical_registry_scope_settlement_timezone  # noqa: E402
from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402


SCHEMA = "bayes_basket_target_historical_native_rebuild_evidence_v1"
PROVIDER = "ecmwf_ifs025_official"
HOST = "data.ecmwf.int"
ARCHIVE_HOST = "ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
MAX_FULL_BYTES = 256 * 1024 * 1024
MAX_INDEX_BYTES = 128 * 1024
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_DATES = 16
_DIRECT_TRANSPORT = False
MAX_STATIONS = 64
DEFAULT_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_ROOT = ROOT / "data/research/bayes_basket_target/historical_native"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_historical_ecmwf_rebuild_evidence.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _station_ids(path: Path) -> list[str]:
    value = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    stations = value.get("stations") if isinstance(value, Mapping) else None
    if not isinstance(stations, Mapping):
        raise ContractError("historical_ecmwf_station_lane_map_missing")
    result = sorted({str(key).strip().upper() for key in stations if str(key).strip()})
    if not result or len(result) > MAX_STATIONS:
        raise ContractError("historical_ecmwf_station_count_invalid")
    return result


def _registry_timezone(path: Path, *, station_ids: list[str], event_dates: list[str]) -> str:
    value = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    rows = value.get("rows") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("historical_ecmwf_timezone_registry_rows_missing")
    return historical_registry_scope_settlement_timezone(
        (row for row in rows if isinstance(row, Mapping)),
        station_ids=station_ids,
        event_dates=event_dates,
    )


def _registry_timezones(
    path: Path,
    *,
    station_ids: list[str],
) -> dict[str, str]:
    value = load_bounded_json(
        path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES
    )
    rows = value.get("rows") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("historical_ecmwf_timezone_registry_rows_missing")
    expected = set(station_ids)
    values: dict[str, set[str]] = {station: set() for station in expected}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station in values and timezone_name:
            values[station].add(timezone_name)
    result: dict[str, str] = {}
    for station in sorted(expected):
        if len(values[station]) != 1:
            raise ContractError(
                f"historical_ecmwf_station_timezone_invalid:{station}"
            )
        result[station] = next(iter(values[station]))
    return result


def _calibration_truth_timezone(
    path: Path,
    *,
    station_ids: list[str],
    event_dates: list[str],
) -> str:
    value = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    identity = {
        key: item for key, item in value.items() if key != "content_sha256"
    } if isinstance(value, Mapping) else {}
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != "bayes_basket_station_calibration_dataset_v1"
        or str(value.get("content_sha256") or "") != content_sha256(identity)
        or station_ids != [str(value.get("station_id") or "").strip().upper()]
    ):
        raise ContractError("historical_ecmwf_calibration_truth_authority_invalid")
    sample_dates = {
        str(row.get("event_date") or "")
        for row in value.get("samples") or []
        if isinstance(row, Mapping)
    }
    if not set(event_dates).issubset(sample_dates):
        raise ContractError("historical_ecmwf_calibration_truth_dates_missing")
    timezone_name = str(value.get("settlement_timezone") or "").strip()
    if not timezone_name:
        raise ContractError("historical_ecmwf_calibration_truth_timezone_missing")
    return timezone_name


def _scoped_station_ids(path: Path, explicit: list[str] | None) -> list[str]:
    allowed = set(_station_ids(path))
    if not explicit:
        return sorted(allowed)
    result = sorted({str(item).strip().upper() for item in explicit if str(item).strip()})
    if not result or set(result) - allowed:
        raise ContractError("historical_ecmwf_station_scope_invalid")
    return result


def _published(headers: Mapping[str, Any]) -> str:
    raw = str(headers.get("Last-Modified") or headers.get("last-modified") or "").strip()
    if not raw:
        raise ContractError("historical_ecmwf_publication_missing")
    parsed = parsedate_to_datetime(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("historical_ecmwf_publication_invalid")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_initialization(event_date: date, *, run_cycle: str, run_day_offset: int) -> datetime:
    cycle = str(run_cycle).strip().upper()
    if cycle not in {"00Z", "06Z", "12Z", "18Z"}:
        raise ContractError("historical_ecmwf_run_cycle_invalid")
    if int(run_day_offset) not in {1, 2}:
        raise ContractError("historical_ecmwf_run_day_offset_invalid")
    return datetime.combine(
        event_date - timedelta(days=int(run_day_offset)),
        time(hour=int(cycle[:2])),
        tzinfo=timezone.utc,
    )


def _url(run_initialization: datetime, lead_hours: int) -> str:
    token = run_initialization.strftime("%Y%m%d")
    hour = run_initialization.strftime("%H")
    return (
        f"https://{HOST}/forecasts/{token}/{hour}z/ifs/0p25/oper/"
        f"{token}{hour}0000-{lead_hours}h-oper-fc.grib2"
    )


def _candidate_urls(run_initialization: datetime, lead_hours: int) -> tuple[str, ...]:
    """Return only official ECMWF endpoints for one historical file.

    Historical files are requested from ECMWF's official S3 distribution
    first because the rolling ``data.ecmwf.int`` tree retires old objects and
    may hold a proxy connection open before returning that absence.  Both
    hosts carry the same date-addressed IFS open-data object.  The actual
    source URL remains part of retained lineage; no republisher is accepted.
    """

    token = run_initialization.strftime("%Y%m%d")
    hour = run_initialization.strftime("%H")
    suffix = f"{token}{hour}0000-{lead_hours}h-oper-fc.grib2"
    return (
        f"https://{ARCHIVE_HOST}/{token}/{hour}z/ifs/0p25/oper/{suffix}",
        _url(run_initialization, lead_hours),
    )


def _free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _raw_bytes(root: Path) -> int:
    total = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and "/raw/" in f"/{path.relative_to(root)}":
                total += path.stat().st_size
    return total


def _persist(path: Path, source: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.stat().st_size != source.stat().st_size or _sha256_file(path) != _sha256_file(source):
            raise ContractError("historical_ecmwf_content_address_collision")
        return
    os.replace(source, path)


def _fetch(url: str, *, headers: Mapping[str, str] | None = None, limit: int) -> tuple[bytes, dict[str, str], int]:
    """Fetch a small official object with curl's hard network timeout.

    The desktop Python TLS stack can remain in a proxy handshake poll despite
    a socket timeout.  Curl is used only as the bounded HTTPS transport here;
    certificate verification remains enabled, and the proxy is explicit.
    """

    last_error: Exception | None = None
    for attempt in range(3):
        temp_dir = Path(tempfile.mkdtemp(prefix="ecmwf-fetch-"))
        body_path = temp_dir / "body.bin"
        header_path = temp_dir / "headers.txt"
        try:
            curl = shutil.which("curl")
            if not curl:
                raise ContractError("historical_ecmwf_curl_unavailable")
            command = [
                curl,
                "--silent",
                "--show-error",
                "--location",
                "--connect-timeout",
                "10",
                "--max-time",
                "45",
                "--output",
                str(body_path),
                "--dump-header",
                str(header_path),
                "--write-out",
                "%{http_code}",
            ]
            proxy = "" if _DIRECT_TRANSPORT else official_nwp_proxy_url()
            command.extend(["--proxy", proxy] if proxy else ["--noproxy", "*"])
            for key, value in (headers or {}).items():
                command.extend(["--header", f"{key}: {value}"])
            command.append(url)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=55,
                check=False,
            )
            if completed.returncode != 0:
                raise URLError(completed.stderr.strip() or "historical_ecmwf_curl_failed")
            try:
                status = int(completed.stdout.strip()[-3:])
            except ValueError as exc:
                raise ContractError("historical_ecmwf_curl_status_invalid") from exc
            header_text = header_path.read_text(encoding="iso-8859-1") if header_path.is_file() else ""
            blocks = [block for block in re.split(r"\r?\n\r?\n", header_text) if block.strip()]
            response_headers: dict[str, str] = {}
            if blocks:
                for line in blocks[-1].splitlines()[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        response_headers[key.strip()] = value.strip()
            body = body_path.read_bytes() if body_path.is_file() else b""
            if len(body) > limit:
                raise ContractError("historical_ecmwf_response_too_large")
            if status not in {200, 206}:
                raise HTTPError(url, status, f"HTTP Error {status}: Not Found", response_headers, io.BytesIO(body))
            return body, response_headers, status
        except HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            detail = str(exc).lower()
            if code == 503 and "not found" in detail:
                raise
            if code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
            last_error = exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if attempt < 2:
            time_module.sleep(0.5 * (2**attempt))
    raise last_error or ContractError("historical_ecmwf_fetch_failed")


def _index_range(index_body: bytes, *, lead_hours: int) -> tuple[int, int, Mapping[str, Any]]:
    """Locate the official ECMWF surface 2-m temperature message."""

    matches: list[tuple[int, int, Mapping[str, Any]]] = []
    for raw_line in index_body.splitlines():
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("historical_ecmwf_index_json_invalid") from exc
        if not isinstance(item, Mapping):
            continue
        if str(item.get("param") or "").lower() != "2t":
            continue
        if str(item.get("levtype") or "").lower() != "sfc":
            continue
        if str(item.get("step") or "").strip() != str(lead_hours):
            continue
        try:
            start = int(item["_offset"])
            length = int(item["_length"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("historical_ecmwf_index_range_invalid") from exc
        end = start + length - 1
        if start < 0 or length <= 0 or length > MAX_MESSAGE_BYTES or end < start:
            raise ContractError("historical_ecmwf_2t_range_invalid")
        matches.append((start, end, item))
    if len(matches) != 1:
        raise ContractError("historical_ecmwf_2t_index_cardinality_invalid")
    return matches[0]


def _full_size(headers: Mapping[str, str]) -> int | None:
    raw = str(headers.get("Content-Range") or "").strip()
    if "/" in raw:
        try:
            total = int(raw.rsplit("/", 1)[1])
            return total if total > 0 else None
        except ValueError:
            pass
    raw = str(headers.get("Content-Length") or "").strip()
    try:
        total = int(raw)
        return total if total > 0 else None
    except ValueError:
        return None


def _stream_extract(*, url: str, output_root: Path, lead_hours: int) -> tuple[dict[str, Any], Path]:
    """Fetch only the official indexed 2-m-temperature GRIB message."""

    # Fail before network I/O when the shared target raw tree cannot accept
    # the maximum bounded message.
    assert_target_raw_retention_budget(
        output_root,
        bytes_needed=MAX_MESSAGE_BYTES,
    )
    index_url = url.removesuffix(".grib2") + ".index"
    index_body, index_headers, index_status = _fetch(index_url, limit=MAX_INDEX_BYTES)
    if index_status != 200:
        raise ContractError(f"historical_ecmwf_index_http_{index_status}")
    start, end, index_item = _index_range(index_body, lead_hours=lead_hours)
    index_sha = hashlib.sha256(index_body).hexdigest()
    range_body, headers, status = _fetch(
        url,
        headers={"Range": f"bytes={start}-{end}"},
        limit=MAX_MESSAGE_BYTES,
    )
    if status != 206 or len(range_body) != end - start + 1:
        raise ContractError("historical_ecmwf_range_not_honored")
    if not range_body.startswith(b"GRIB"):
        raise ContractError("historical_ecmwf_2t_message_not_grib")
    assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    if _free_bytes(output_root) < MIN_TARGET_FREE_BYTES + len(range_body):
        raise ContractError("historical_ecmwf_min_free_space_guard")
    assert_target_raw_retention_budget(
        output_root,
        bytes_needed=len(range_body),
    )
    raw_sha = hashlib.sha256(range_body).hexdigest()
    raw_path = output_root / PROVIDER / "raw" / f"{raw_sha}.grib2"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_path.is_file():
        if raw_path.stat().st_size != len(range_body) or _sha256_file(raw_path) != raw_sha:
            raise ContractError("historical_ecmwf_content_address_collision")
    else:
        temporary = raw_path.with_name(f".{raw_path.name}.tmp.{os.getpid()}")
        try:
            temporary.write_bytes(range_body)
            os.replace(temporary, raw_path)
        finally:
            temporary.unlink(missing_ok=True)
    full_size = _full_size(headers)
    if full_size is not None and full_size > MAX_FULL_BYTES:
        raise ContractError("historical_ecmwf_full_payload_too_large")
    published_headers = headers or index_headers
    return {
        "provider_id": PROVIDER,
        "source_url": url,
        "index_url": index_url,
        "index_sha256": index_sha,
        "index_selector": dict(index_item),
        "byte_range": {"start": start, "end": end},
        "full_payload_sha256": "",
        "full_payload_size_bytes": full_size,
        "raw_message_sha256": raw_sha,
        "raw_object_ref": str(raw_path.resolve()),
        "published_at_utc": _published(published_headers),
    }, raw_path


def _resume_rows(
    resume_path: Path | None,
    *,
    event_dates: set[str],
    output_root: Path,
    target_timezone: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[str, str]]]:
    """Reuse only verified local minimal objects from an earlier receipt."""

    if resume_path is None or not resume_path.expanduser().resolve().is_file():
        return [], [], set()
    value = load_bounded_json(resume_path.expanduser().resolve(), limit=MAX_JSON_OUTPUT_BYTES)
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise ContractError("historical_ecmwf_resume_schema_invalid")
    if str(value.get("target_timezone") or "").strip() != target_timezone:
        raise ContractError("historical_ecmwf_resume_settlement_timezone_mismatch")
    root = output_root.resolve()

    def safe_record(item: Mapping[str, Any]) -> bool:
        try:
            path = Path(str(item.get("raw_object_ref"))).expanduser().resolve()
            byte_range = item.get("byte_range")
            expected_size = (
                int(byte_range["end"]) - int(byte_range["start"]) + 1
                if isinstance(byte_range, Mapping)
                else 0
            )
            expected_sha = str(item.get("raw_message_sha256") or "")
            return (
                path.is_relative_to(root)
                and path.is_file()
                and expected_size > 0
                and path.stat().st_size == expected_size
                and len(expected_sha) == 64
                and _sha256_file(path) == expected_sha
            )
        except (KeyError, OSError, TypeError, ValueError):
            return False

    records: list[dict[str, Any]] = []
    for item in value.get("message_records") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("event_date") or "") not in event_dates or not safe_record(item):
            continue
        records.append(dict(item))
    record_times = {
        (str(item.get("event_date") or ""), str(item.get("valid_time_utc") or ""))
        for item in records
    }
    record_refs = {
        (
            str(item.get("event_date") or ""),
            str(item.get("valid_time_utc") or ""),
        ): str(Path(str(item.get("raw_object_ref"))).expanduser().resolve())
        for item in records
    }
    rows: list[dict[str, Any]] = []
    for item in value.get("model_points") or value.get("model_rows") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("event_date") or "") not in event_dates:
            continue
        key = (str(item.get("event_date") or ""), str(item.get("valid_time_utc") or ""))
        if (
            key in record_times
            and str(Path(str(item.get("raw_object_ref"))).expanduser().resolve())
            == record_refs.get(key)
        ):
            rows.append(dict(item))
    return rows, records, record_times


def _validate_resume_scope(
    resume_path: Path | None,
    *,
    station_ids: list[str],
    station_timezones: Mapping[str, str],
) -> None:
    if resume_path is None or not resume_path.expanduser().resolve().is_file():
        return
    value = load_bounded_json(
        resume_path.expanduser().resolve(),
        limit=MAX_JSON_OUTPUT_BYTES,
    )
    if sorted(value.get("station_ids") or []) != sorted(station_ids):
        raise ContractError("historical_ecmwf_resume_station_scope_mismatch")
    raw_timezones = value.get("station_timezones")
    if not isinstance(raw_timezones, Mapping) or {
        str(station).strip().upper(): str(timezone_name).strip()
        for station, timezone_name in raw_timezones.items()
    } != dict(station_timezones):
        raise ContractError(
            "historical_ecmwf_resume_station_timezone_mismatch"
        )


def _reuse_message_records(
    evidence_paths: list[Path] | None,
    *,
    event_dates: set[str],
    output_root: Path,
    run_cycle: str,
    run_day_offset: int,
) -> list[dict[str, Any]]:
    """Reuse verified global GRIB messages without inheriting station rows."""

    root = output_root.resolve()
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for evidence_path in evidence_paths or []:
        value = load_bounded_json(
            evidence_path.expanduser().resolve(),
            limit=MAX_JSON_OUTPUT_BYTES,
        )
        if (
            not isinstance(value, Mapping)
            or value.get("schema") != SCHEMA
            or value.get("provider_id") != PROVIDER
            or str(value.get("run_cycle") or "").upper()
            != str(run_cycle).upper()
            or int(value.get("run_day_offset") or 0) != int(run_day_offset)
        ):
            raise ContractError("historical_ecmwf_reuse_evidence_scope_invalid")
        for raw in value.get("message_records") or []:
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            event_date = str(item.get("event_date") or "")
            valid_time = str(item.get("valid_time_utc") or "")
            if event_date not in event_dates or not valid_time:
                continue
            try:
                expected_run = _run_initialization(
                    date.fromisoformat(event_date),
                    run_cycle=run_cycle,
                    run_day_offset=run_day_offset,
                ).isoformat().replace("+00:00", "Z")
                path = Path(str(item.get("raw_object_ref"))).expanduser().resolve()
                byte_range = item.get("byte_range")
                expected_size = (
                    int(byte_range["end"]) - int(byte_range["start"]) + 1
                    if isinstance(byte_range, Mapping)
                    else 0
                )
                expected_sha = str(item.get("raw_message_sha256") or "")
                if (
                    str(item.get("provider_id") or "") != PROVIDER
                    or str(item.get("run_initialization_utc") or "")
                    != expected_run
                    or not path.is_relative_to(root)
                    or not path.is_file()
                    or expected_size <= 0
                    or path.stat().st_size != expected_size
                    or len(expected_sha) != 64
                    or _sha256_file(path) != expected_sha
                ):
                    raise ContractError(
                        "historical_ecmwf_reuse_message_invalid"
                    )
            except (KeyError, OSError, TypeError, ValueError) as exc:
                raise ContractError(
                    "historical_ecmwf_reuse_message_invalid"
                ) from exc
            key = (event_date, valid_time)
            previous = records.get(key)
            if (
                previous is not None
                and previous.get("raw_message_sha256") != expected_sha
            ):
                raise ContractError(
                    "historical_ecmwf_reuse_message_conflict"
                )
            records[key] = item
    return [records[key] for key in sorted(records)]


def rebuild(
    *,
    event_dates: list[str],
    station_ids: list[str],
    output_root: Path,
    output_path: Path,
    target_timezone: str | None = None,
    station_timezones: Mapping[str, str] | None = None,
    resume_path: Path | None = None,
    reuse_message_evidence: list[Path] | None = None,
    as_of_utc: str | None = None,
    run_cycle: str = "00Z",
    run_day_offset: int = 1,
) -> dict[str, Any]:
    if not event_dates or len(event_dates) > MAX_DATES:
        raise ContractError("historical_ecmwf_event_dates_invalid")
    if not station_ids or len(station_ids) > MAX_STATIONS:
        raise ContractError("historical_ecmwf_station_ids_invalid")
    normalized_timezones = {
        str(station).strip().upper(): str(timezone_name).strip()
        for station, timezone_name in (station_timezones or {}).items()
    }
    if normalized_timezones:
        if set(normalized_timezones) != set(station_ids):
            raise ContractError(
                "historical_ecmwf_station_timezone_scope_mismatch"
            )
    elif target_timezone:
        normalized_timezones = {
            station: target_timezone for station in station_ids
        }
    else:
        raise ContractError("historical_ecmwf_target_timezone_missing")
    unique_timezones = sorted(set(normalized_timezones.values()))
    mixed_timezones = len(unique_timezones) > 1
    receipt_timezone = (
        "REGISTRY_BOUND_PER_STATION"
        if mixed_timezones
        else unique_timezones[0]
    )
    if mixed_timezones:
        _validate_resume_scope(
            resume_path,
            station_ids=station_ids,
            station_timezones=normalized_timezones,
        )
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    reconstructed_at = _now()
    reconstruction_clock = str(as_of_utc or reconstructed_at).strip()
    normalized_dates = sorted({date.fromisoformat(str(value).strip()).isoformat() for value in event_dates})
    for event_date in normalized_dates:
        for timezone_name in unique_timezones:
            assert_event_day_closed(
                event_date,
                as_of_utc=reconstruction_clock,
                target_timezone=timezone_name,
            )
    rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    resumed_rows, resumed_records, resumed_times = _resume_rows(
        resume_path,
        event_dates=set(normalized_dates),
        output_root=output_root,
        target_timezone=receipt_timezone,
    )
    rows.extend(resumed_rows)
    records.extend(resumed_records)
    existing_record_keys = {
        (
            str(item.get("event_date") or ""),
            str(item.get("valid_time_utc") or ""),
        )
        for item in records
    }
    records.extend(
        item
        for item in _reuse_message_records(
            reuse_message_evidence,
            event_dates=set(normalized_dates),
            output_root=output_root,
            run_cycle=run_cycle,
            run_day_offset=run_day_offset,
        )
        if (
            str(item.get("event_date") or ""),
            str(item.get("valid_time_utc") or ""),
        )
        not in existing_record_keys
    )
    resumed_records_by_time = {
        (
            str(item.get("event_date") or ""),
            str(item.get("valid_time_utc") or ""),
        ): item
        for item in records
    }
    resumed_point_keys = {
        (
            str(item.get("event_date") or ""),
            str(item.get("valid_time_utc") or ""),
            str(item.get("station_id") or "").strip().upper(),
        )
        for item in rows
    }
    for event_date in normalized_dates:
        day = date.fromisoformat(event_date)
        run_initialization = _run_initialization(
            day,
            run_cycle=run_cycle,
            run_day_offset=run_day_offset,
        )
        run_init = run_initialization.isoformat().replace("+00:00", "Z")
        stations_by_valid_time: dict[datetime, list[str]] = {}
        for station_id in station_ids:
            timezone_name = normalized_timezones[station_id]
            start = datetime.combine(
                day, time.min, tzinfo=ZoneInfo(timezone_name)
            ).astimezone(timezone.utc)
            sample_offsets = aligned_target_sample_offsets(
                target_day_start_utc=start,
                run_initialization=run_initialization,
            )
            for offset in sample_offsets:
                valid_at = start + timedelta(hours=offset)
                stations_by_valid_time.setdefault(valid_at, []).append(
                    station_id
                )
        for valid_at, scoped_stations in sorted(
            stations_by_valid_time.items()
        ):
            valid_time_text = valid_at.isoformat().replace("+00:00", "Z")
            lead = int((valid_at - run_initialization).total_seconds() // 3600)
            if lead < 0:
                raise ContractError("historical_ecmwf_negative_lead")
            source_url = _url(run_initialization, lead)
            try:
                resume_key = (event_date, valid_time_text)
                record = resumed_records_by_time.get(resume_key)
                if record is not None:
                    identity = record
                    raw_path = Path(
                        str(record["raw_object_ref"])
                    ).expanduser().resolve()
                    source_url = str(record.get("source_url") or source_url)
                else:
                    last_error: Exception | None = None
                    for candidate_url in _candidate_urls(run_initialization, lead):
                        try:
                            record, raw_path = _stream_extract(url=candidate_url, output_root=output_root, lead_hours=lead)
                            source_url = candidate_url
                            break
                        except Exception as exc:
                            last_error = exc
                    else:
                        raise last_error or ContractError("historical_ecmwf_all_official_endpoints_failed")
                    identity = {
                        **record,
                        "event_date": event_date,
                        "run_initialization_utc": run_init,
                        "valid_time_utc": valid_time_text,
                        "capture_mode": "historical_rebuild",
                    }
                    record = {**identity, "receipt_sha256": content_sha256(identity), "reconstructed_at_utc": reconstructed_at}
                    records.append(record)
                    resumed_records_by_time[resume_key] = record
                for station_id in scoped_stations:
                    point_key = (
                        event_date,
                        valid_time_text,
                        station_id,
                    )
                    if point_key in resumed_point_keys:
                        continue
                    try:
                        assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
                        decoded = decode_native_temperature_points(raw_path, provider_id=PROVIDER, station_id=station_id)
                        matches = [point for point in decoded["points"] if point["grib_valid_time_utc"] == identity["valid_time_utc"] and point["grib_run_initialization_utc"] == run_init]
                        if len(matches) != 1:
                            raise ContractError("historical_ecmwf_decoded_time_cardinality_invalid")
                        rows.append({
                            "station_id": station_id,
                            "event_date": event_date,
                            "target_timezone": normalized_timezones[station_id],
                            "provider_id": PROVIDER,
                            "model_run_initialization_utc": run_init,
                            "valid_time_utc": identity["valid_time_utc"],
                            "temperature_2m_c": matches[0]["temperature_2m_c"],
                            "raw_message_sha256": identity["raw_message_sha256"],
                            "receipt_sha256": record["receipt_sha256"],
                            "published_at_utc": identity["published_at_utc"],
                            "received_at_utc": "",
                            "reconstructed_at_utc": reconstructed_at,
                            "capture_mode": "historical_rebuild",
                            "production_calibration_eligible": False,
                            "source_url": source_url,
                            "raw_object_ref": identity["raw_object_ref"],
                        })
                        resumed_point_keys.add(point_key)
                    except Exception as exc:
                        blockers.append({"event_date": event_date, "valid_time_utc": identity["valid_time_utc"], "station_id": station_id, "reason": f"decode:{type(exc).__name__}:{exc}"})
            except Exception as exc:
                blockers.append({"event_date": event_date, "valid_time_utc": valid_at.isoformat().replace("+00:00", "Z"), "station_id": "*", "reason": f"fetch:{type(exc).__name__}:{exc}"})
    curves = (
        materialize_native_daily_curves_by_station_timezone(
            rows,
            station_timezones=normalized_timezones,
        )
        if rows and mixed_timezones
        else materialize_native_daily_curves(
            rows,
            target_timezone=receipt_timezone,
        )
        if rows
        else {
            "curve_count": 0,
            "complete_curve_count": 0,
            "eligible_model_rows": [],
            "all_complete": False,
        }
    )
    identity = {
        "schema": SCHEMA,
        "reconstructed_at_utc": reconstructed_at,
        "target_timezone": receipt_timezone,
        "station_timezones": dict(sorted(normalized_timezones.items())),
        "event_dates": normalized_dates,
        "as_of_utc": reconstruction_clock,
        "station_ids": sorted(station_ids),
        "provider_id": PROVIDER,
        "run_cycle": str(run_cycle).strip().upper(),
        "run_day_offset": int(run_day_offset),
        "capture_mode": "historical_rebuild",
        "model_points": rows,
        "model_point_count": len(rows),
        "model_rows": list(curves.get("eligible_model_rows") or []),
        "model_row_count": len(curves.get("eligible_model_rows") or []),
        "message_records": records,
        "message_count": len(records),
        "curves": curves,
        "blockers": blockers[:500],
        "checks": {
            "official_provider_only": True,
            "official_ecmwf_archive_only": True,
            "no_open_meteo": True,
            "registry_station_timezones_bound": True,
            "mixed_timezone_batch": mixed_timezones,
            "raw_objects_are_minimal_messages": True,
            "full_payloads_deleted_after_extraction": True,
            "all_rows_retrospective_not_arrival": True,
            "production_calibration_eligible": False,
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "resource_budget": {
            "max_full_temporary_bytes": MAX_FULL_BYTES,
            "max_message_bytes": MAX_MESSAGE_BYTES,
            "max_raw_bytes": MAX_TARGET_RESEARCH_RAW_BYTES,
            "min_free_bytes": MIN_TARGET_FREE_BYTES,
            "max_process_rss_bytes": MAX_TARGET_PROCESS_RSS_BYTES,
            "retained_raw_bytes_after_run": _raw_bytes(output_root),
        },
        "boundary": {"offline_research_only": True, "historical_rebuild": True, "production_calibration_eligible": False, "live_submit_authorized": False, "places_orders": False},
    }
    result = {**identity, "evidence_sha256": content_sha256(identity)}
    result["receipt_sha256"] = content_sha256({k: v for k, v in result.items() if k != "receipt_sha256"})
    write_bounded_json(output_path.expanduser().resolve(), result, limit=MAX_JSON_OUTPUT_BYTES)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", action="append", required=True)
    parser.add_argument("--station-lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument("--calibration-truth-dataset", type=Path)
    parser.add_argument("--station-id", action="append", default=[], help="Restrict this isolated rebuild to registered stations; repeatable.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume-evidence", type=Path, help="Reuse verified rows from a prior bounded receipt and fetch only missing times.")
    parser.add_argument("--reuse-message-evidence", type=Path, action="append", default=[], help="Reuse verified global GRIB messages from another station scope without inheriting decoded rows; repeatable.")
    parser.add_argument("--as-of-utc", default="", help="Historical cutoff; every requested settlement day must already be closed.")
    parser.add_argument("--run-cycle", default="00Z", choices=["00Z", "06Z", "12Z", "18Z"])
    parser.add_argument("--run-day-offset", type=int, default=1, choices=[1, 2])
    parser.add_argument(
        "--transport",
        choices=("configured_proxy", "direct"),
        default="configured_proxy",
    )
    args = parser.parse_args()
    global _DIRECT_TRANSPORT
    _DIRECT_TRANSPORT = args.transport == "direct"
    event_dates = sorted(set(args.event_date))
    station_ids = _scoped_station_ids(args.station_lane_map, args.station_id)
    registry_timezones = (
        None
        if args.calibration_truth_dataset is not None
        else _registry_timezones(
            args.timezone_registry,
            station_ids=station_ids,
        )
    )
    result = rebuild(
        event_dates=event_dates,
        station_ids=station_ids,
        output_root=args.output_root,
        output_path=args.output,
        target_timezone=(
            _calibration_truth_timezone(
                args.calibration_truth_dataset,
                station_ids=station_ids,
                event_dates=event_dates,
            )
            if args.calibration_truth_dataset is not None
            else None
        ),
        station_timezones=registry_timezones,
        resume_path=args.resume_evidence,
        reuse_message_evidence=args.reuse_message_evidence,
        as_of_utc=args.as_of_utc or None,
        run_cycle=args.run_cycle,
        run_day_offset=args.run_day_offset,
    )
    print(json.dumps({"schema": result["schema"], "output": str(args.output.expanduser().resolve()), "event_dates": result["event_dates"], "model_row_count": result["model_row_count"], "complete_curve_count": result["curves"].get("complete_curve_count"), "message_count": result["message_count"], "blocker_count": len(result["blockers"]), "production_calibration_eligible": False}, ensure_ascii=False, sort_keys=True))
    return 0 if result["model_point_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
