"""Official ECCC GEPS historical members for calibration training."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import subprocess
import tempfile
import time as time_module
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .contracts import ContractError, content_sha256
from .native_archive_lead import (
    capture_lead_and_run,
    capture_lead_bucket,
    lead_bucket_for_run,
)
from .future_temperature_curve import (
    MIN_CURVE_POINTS,
    MIN_SAME_DAY_CURVE_POINTS,
    build_native_family_temperature_curve,
)
from .native_ensemble_store import _insert_content_addressed
from .noaa_gefs_archive import (
    ECCODES_GRIB_GET,
    decode_nearest_temperatures_c,
)
from .p0_producer import bucket_intervals_from_registry_rows
from .resource_bounds import assert_process_memory_budget
from .ssot import native_ensemble_foundation_schema_status
from official_nwp_https_transport import official_nwp_proxy_url


SCHEMA = "eccc_geps_official_historical_station_members_v1"
MULTISTATION_SCHEMA = (
    "eccc_geps_official_historical_multistation_members_v1"
)
SYSTEM_ID = "ECCC_GEM_GLOBAL_ENS"
INDEPENDENCE_FAMILY_ID = "ECCC_GEM"
# ECCC is the only native family without an 18Z run, so its d2 cycle is a real
# choice rather than a given.  00Z fires 18 hours before the 18Z the other
# families use; 12Z narrows that information deficit to 6 hours.  Both are
# admitted here so the two can be measured against each other instead of one
# being assumed.  A capture's cycle is derived from its own ``run_init`` -- see
# ``_run_cycle_label`` -- rather than from a module constant, so that a row can
# never claim a cycle the object it came from does not carry.
SUPPORTED_RUN_CYCLE_HOURS = (0, 12)
# P0 follows the freshest run, so this collector is asked for a shorter lead as
# the event day approaches.  The default keeps existing d2 callers unchanged.
SUPPORTED_LEAD_DAYS = (0, 1, 2)
DEFAULT_LEAD_DAYS = 2
# Which run production reads at each lead.  The others read a late cycle at d2
# and fall back to the day's first cycle at d1/d0, because a late run no longer
# precedes the local day in the far east.  ECCC has no 18Z, so d2 takes 12Z --
# the latest run that still precedes the day everywhere -- for the lead-hour
# reason spelled out under ``ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD`` below: at 00Z the
# d2 capture lands in a bucket no other family occupies and can never be paired.
#
# d1 and d0 stay on 00Z.  Their 00Z history is already the length that admits
# them (30 and 33 days), and moving a lead's cycle restarts its accumulation
# from zero -- a cost worth paying only where the existing history is unusable,
# which is d2's case and not theirs.
RUN_CYCLE_HOUR_BY_LEAD = {2: 12, 1: 0, 0: 0}
# Which cycles the OOS archive *admits* at each lead, as against the single
# cycle the mapping above tells the collector to take.  The two differ because
# they answer different questions: that one asks which run production reads,
# this one asks which retained runs may train.
#
# Reading 00Z at every lead put ECCC in lead-hour buckets the other families do
# not occupy.  A 00Z run two days out is 48h ahead of a UTC day start and more
# for every station west of the meridian, so measured over the 45-station
# registry its d2 lands in ``h48_plus`` at 36 stations while ECMWF and GEFS read
# 18Z into ``h24_48``.  That is exactly where ECCC's evidence is thickest -- 39
# days against 30 at d1 -- and a cross-native cohort needs two independent
# families in the same bucket, so the thickest scope this family has had nobody
# to pair with.  At 12Z the same lead is 36h ahead of a UTC day start, which
# lands in ``h24_48`` at all 45 stations.
#
# Both cycles stay admitted rather than 00Z being replaced.  The A/B in
# ``scripts/eccc_geps_run_cycle_ab_experiment.py`` measured 12Z at 0.032C worse
# MAE than 00Z over 18 paired days -- real but small, and measured on raw skill
# at a fixed label, which is not the question a cohort asks.  Keeping both lets
# the cohort selection weigh an in-bucket partner against that 0.032C on the
# evidence, instead of this table deciding it in advance.
ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD = {2: (0, 12), 1: (0, 12), 0: (0, 12)}
ARCHIVE_LEAD_BUCKET = f"d{DEFAULT_LEAD_DAYS}"


def extraction_version_for_lead(lead_days: int, cycle_hour: int = 0) -> str:
    """Extraction identity is specific to both lead and cycle.

    ECCC is the one family without an 18Z run, so its cycle is already part of
    the identity; a shorter lead adds a second dimension.  Sharing a version
    string across either would let the OOS scope check accept one against the
    other's contract and train them together.
    """

    return (
        f"eccc_geps_0p5_{int(cycle_hour):02d}z_"
        f"d{int(lead_days)}_station_tmax_v1"
    )


EXTRACTION_VERSION = extraction_version_for_lead(DEFAULT_LEAD_DAYS, 0)
BASE_URL = "https://dd.weather.gc.ca"
EXPECTED_MEMBER_COUNT = 21
MAX_STATION_COUNT = 64
MAX_DOWNLOAD_WORKERS = 4
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_MESSAGE_BYTES = 512 * 1024
MAX_TOTAL_RESPONSE_BYTES = 100 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024


def _get_bytes(
    url: str,
    *,
    timeout_seconds: float,
) -> tuple[bytes, Mapping[str, str]]:
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with httpx.Client(
                proxy=official_nwp_proxy_url() or None,
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=1,
                    max_keepalive_connections=0,
                ),
            ) as client:
                with client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": "weather-eccc-geps-history/1"},
                    timeout=timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    length = response.headers.get("Content-Length")
                    if length and int(length) > MAX_FILE_BYTES:
                        raise ContractError(
                            "eccc_geps_archive_response_too_large"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ContractError(
                                "eccc_geps_archive_response_too_large"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), dict(response.headers)
        except (
            httpx.TransportError,
            httpx.TimeoutException,
            httpx.HTTPStatusError,
        ) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code
                not in {429, 500, 502, 503, 504}
            ):
                raise
            last_error = exc
            if attempt < 5:
                time_module.sleep(min(0.5 * (2**attempt), 8.0))
    raise ContractError(
        "eccc_geps_archive_transport_failed:"
        + type(last_error).__name__
    ) from last_error


def _run_cycle_label(run_init: datetime) -> str:
    """Derive the cycle label from the run itself, never from a constant.

    ECCC publishes GEPS on 00Z and 12Z only.  Deriving the label here means a
    capture can never claim a cycle its ``run_init`` does not actually carry,
    which is what makes the 00Z/12Z comparison auditable.
    """

    hour = run_init.astimezone(timezone.utc).hour
    if hour not in SUPPORTED_RUN_CYCLE_HOURS:
        raise ContractError("eccc_geps_archive_run_cycle_unsupported")
    return f"{hour:02d}Z"


def _forecast_hours(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int]:
    target = date.fromisoformat(event_date)
    zone = ZoneInfo(settlement_timezone)
    result = [
        forecast_hour
        for forecast_hour in range(0, 97, 3)
        if (
            run_init_utc + timedelta(hours=forecast_hour)
        ).astimezone(zone).date()
        == target
    ]
    # A full local day is eight periods, and that is the right bar for d2 and
    # d1: their run precedes the day, so a short grid means it is broken.
    #
    # It is the wrong bar for d0.  That run is issued after the local day has
    # begun and cannot describe hours that already happened -- nor does it need
    # to, since those are settled observation by then.  Its value is the fresher
    # initial state, not sampling density: a same-day run has assimilated that
    # morning's observations and beats a two-day-old d2 even with few points.
    run_day = run_init_utc.astimezone(timezone.utc).date()
    minimum = 1 if (target - run_day).days == 0 else 7
    if not minimum <= len(result) <= 9:
        raise ContractError("eccc_geps_archive_local_day_grid_incomplete")
    return result


def follow_forecast_hours(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int] | None:
    """Charter 192: the steps a followed 12Z run seals for one station, or None.

    The capture above raises for the whole batch when one station falls
    short, and so does the curve seal behind it (three points on the event's
    UTC date, four otherwise).  A follow capture asks per station first and
    leaves out the ones its run cannot cover; those keep the run they have.
    """

    try:
        hours = _forecast_hours(
            event_date=event_date,
            settlement_timezone=settlement_timezone,
            run_init_utc=run_init_utc,
        )
    except ContractError:
        return None
    same_day = (
        date.fromisoformat(event_date)
        - run_init_utc.astimezone(timezone.utc).date()
    ).days == 0
    floor = MIN_SAME_DAY_CURVE_POINTS if same_day else MIN_CURVE_POINTS
    return hours if len(hours) >= floor else None


def _object_url(
    *,
    run_init: datetime,
    forecast_hour: int,
) -> tuple[str, str]:
    run_day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    filename = (
        "CMC_geps-raw_TMP_TGL_2m_latlon0p5x0p5_"
        f"{run_day}{cycle}_P{forecast_hour:03d}_allmbrs.grib2"
    )
    object_key = (
        f"{run_day}/WXO-DD/ensemble/geps/grib2/raw/"
        f"{cycle}/{forecast_hour:03d}/{filename}"
    )
    return f"{BASE_URL}/{object_key}", object_key


def _split_grib_messages(raw: bytes) -> list[bytes]:
    messages: list[bytes] = []
    offset = 0
    while offset < len(raw):
        if (
            offset + 16 > len(raw)
            or raw[offset : offset + 4] != b"GRIB"
            or raw[offset + 7] != 2
        ):
            raise ContractError("eccc_geps_archive_grib_framing_invalid")
        length = int.from_bytes(
            raw[offset + 8 : offset + 16],
            byteorder="big",
        )
        end = offset + length
        if (
            length <= 20
            or length > MAX_MESSAGE_BYTES
            or end > len(raw)
            or raw[end - 4 : end] != b"7777"
        ):
            raise ContractError("eccc_geps_archive_grib_length_invalid")
        messages.append(raw[offset:end])
        offset = end
    if len(messages) != EXPECTED_MEMBER_COUNT:
        raise ContractError("eccc_geps_archive_member_count_invalid")
    return messages


def _member_metadata(raw: bytes) -> list[dict[str, int]]:
    if not ECCODES_GRIB_GET.is_file() or len(raw) > MAX_FILE_BYTES:
        raise ContractError("eccc_geps_archive_decoder_unavailable")
    with tempfile.NamedTemporaryFile(
        prefix="eccc-geps-allmbrs-",
        suffix=".grib2",
        delete=True,
    ) as handle:
        handle.write(raw)
        handle.flush()
        result = subprocess.run(
            [
                str(ECCODES_GRIB_GET),
                "-p",
                (
                    "number,typeOfEnsembleForecast,"
                    "perturbationNumber,step"
                ),
                handle.name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    if (
        result.returncode != 0
        or len(result.stdout) > 8192
        or len(result.stderr) > 8192
    ):
        raise ContractError("eccc_geps_archive_metadata_decode_failed")
    rows: list[dict[str, int]] = []
    try:
        for line in result.stdout.splitlines():
            number, ensemble_type, perturbation, step = (
                int(value) for value in line.split()
            )
            rows.append({
                "number": number,
                "type_of_ensemble_forecast": ensemble_type,
                "perturbation_number": perturbation,
                "forecast_hour": step,
            })
    except ValueError as exc:
        raise ContractError(
            "eccc_geps_archive_metadata_invalid"
        ) from exc
    if (
        len(rows) != EXPECTED_MEMBER_COUNT
        or {row["number"] for row in rows} != set(range(21))
        or sum(
            row["number"] == 0
            and row["perturbation_number"] == 0
            and row["type_of_ensemble_forecast"] == 1
            for row in rows
        )
        != 1
        or any(
            row["type_of_ensemble_forecast"] != 4
            or row["perturbation_number"] != row["number"]
            for row in rows
            if row["number"] != 0
        )
    ):
        raise ContractError("eccc_geps_archive_member_semantics_invalid")
    return rows


def _download_field(
    *,
    run_init: datetime,
    forecast_hour: int,
    timeout_seconds: float,
) -> tuple[bytes, list[bytes], list[dict[str, int]], dict[str, Any]]:
    url, object_key = _object_url(
        run_init=run_init,
        forecast_hour=forecast_hour,
    )
    raw, headers = _get_bytes(url, timeout_seconds=timeout_seconds)
    messages = _split_grib_messages(raw)
    metadata = _member_metadata(raw)
    if any(row["forecast_hour"] != forecast_hour for row in metadata):
        raise ContractError("eccc_geps_archive_forecast_hour_mismatch")
    receipt = {
        "forecast_hour": forecast_hour,
        "object_key": object_key,
        "response_bytes": len(raw),
        "object_sha256": hashlib.sha256(raw).hexdigest(),
        "member_metadata_sha256": content_sha256(metadata),
        "last_modified": str(headers.get("last-modified") or ""),
    }
    return raw, messages, metadata, receipt


def _build_station_capture(
    *,
    station: str,
    event_date: str,
    scope: Mapping[str, Any],
    run_init: datetime,
    query_at: datetime,
    member_values: Mapping[str, Sequence[float] | Mapping[int, float]],
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    hours = list(scope["forecast_hours"])
    station_receipts = [
        dict(row)
        for row in receipts
        if int(row["forecast_hour"]) in set(hours)
    ]
    if len(station_receipts) != len(hours):
        raise ContractError("eccc_geps_archive_station_receipt_incomplete")
    values_by_member = {
        member_id: (
            {int(hour): float(value) for hour, value in values.items()}
            if isinstance(values, Mapping)
            else {
                int(hour): float(value)
                for hour, value in zip(hours, values)
            }
        )
        for member_id, values in member_values.items()
    }
    members = [
        {
            "ensemble_system_id": SYSTEM_ID,
            "member_id": member_id,
            "member_role": (
                "CONTROL" if member_id == "control" else "PERTURBED"
            ),
            "raw_tmax_c": max(values.values()),
        }
        for member_id, values in sorted(values_by_member.items())
    ]
    if (
        len(members) != EXPECTED_MEMBER_COUNT
        or any(
            set(values_by_member[row["member_id"]]) != set(hours)
            for row in members
        )
    ):
        raise ContractError("eccc_geps_archive_station_member_incomplete")
    intervals = bucket_intervals_from_registry_rows(
        scope["registry_rows"]
    )
    frozen_intervals = {
        bucket: [
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        ]
        for bucket, (lower, upper) in intervals.items()
    }
    member_identity = [
        {
            "member_id": row["member_id"],
            "member_role": row["member_role"],
            "raw_tmax_c": row["raw_tmax_c"],
        }
        for row in members
    ]
    receipt_set_sha = content_sha256(station_receipts)
    family_curve = build_native_family_temperature_curve(
        # A same-day run cannot describe hours that have already happened, so
        # its curve is legitimately shorter than a run that precedes the day.
        min_points=(
            MIN_SAME_DAY_CURVE_POINTS
            if (
                date.fromisoformat(event_date)
                - run_init.astimezone(timezone.utc).date()
            ).days == 0
            else MIN_CURVE_POINTS
        ),
        payload={
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": scope["settlement_timezone"],
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "available_at_utc": query_at.isoformat().replace("+00:00", "Z"),
        "source_field_receipt_set_sha256": receipt_set_sha,
        "points": [
            {
                "valid_time_utc": (run_init + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
                "temperature_c": sum(
                    values_by_member[member_id][hour]
                    for member_id in values_by_member
                ) / len(values_by_member),
            }
            for hour in hours
        ],
    })
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": scope["settlement_timezone"],
        "latitude": float(scope["latitude"]),
        "longitude": float(scope["longitude"]),
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": _run_cycle_label(run_init),
        # Hours ahead of this station's own local day.  The calendar ``d``
        # label this used to carry is a property of the run's UTC date, so it
        # grouped runs issued 12 hours apart together and split runs issued the
        # same number of hours ahead -- ``lead_bucket_for_run`` is still used
        # below to choose *which run to fetch*, which is a different question.
        "archive_lead_bucket": capture_lead_bucket(
            event_date=event_date,
            settlement_timezone=str(scope["settlement_timezone"]),
            run_init=run_init,
        ),
        "forecast_hours": hours,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "member_count": len(members),
        "member_availability_contract": (
            "OFFICIAL_ALLMBRS_CONTROL_0_PERTURBED_1_TO_20"
        ),
        "members": members,
        "member_set_sha256": content_sha256(member_identity),
        "family_temperature_curve": family_curve,
        "bucket_intervals": frozen_intervals,
        "bucket_contract_sha256": content_sha256(frozen_intervals),
        "raw_bucket_probabilities": {
            bucket: sum(
                (
                    bounds[0] is None
                    or row["raw_tmax_c"] >= float(bounds[0])
                )
                and (
                    bounds[1] is None
                    or row["raw_tmax_c"] < float(bounds[1])
                )
                for row in members
            )
            / len(members)
            for bucket, bounds in frozen_intervals.items()
        },
        "field_receipt_count": len(station_receipts),
        "field_receipt_set_sha256": receipt_set_sha,
        "field_receipts": station_receipts,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "source_prefix": (
            f"{BASE_URL}/{run_init.strftime('%Y%m%d')}/WXO-DD/"
            "ensemble/geps/grib2/raw/00/"
        ),
        "source_class": "OFFICIAL_NATIVE_ARCHIVE",
        "native_run_init_known": True,
        "native_publication_at_known": False,
        "offline_chronological_oos_eligible": True,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def capture_geps_station_days(
    *,
    event_date: str,
    station_scopes: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 45.0,
    run_cycle_hour: int = 0,
    lead_days: int = DEFAULT_LEAD_DAYS,
) -> dict[str, Any]:
    if run_cycle_hour not in SUPPORTED_RUN_CYCLE_HOURS:
        raise ContractError("eccc_geps_archive_run_cycle_unsupported")
    if lead_days not in SUPPORTED_LEAD_DAYS:
        raise ContractError("eccc_geps_archive_lead_days_invalid")
    target = date.fromisoformat(event_date)
    run_init = datetime.combine(
        target - timedelta(days=lead_days),
        time(hour=run_cycle_hour),
        tzinfo=timezone.utc,
    )
    query_at = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if (
        query_at <= run_init
        or not 1 <= len(station_scopes) <= MAX_STATION_COUNT
        or shutil.disk_usage(Path(tempfile.gettempdir())).free
        < MIN_FREE_BYTES
    ):
        raise ContractError("eccc_geps_archive_scope_invalid")
    scopes: dict[str, dict[str, Any]] = {}
    for raw in station_scopes:
        station = str(raw.get("station_id") or "").strip().upper()
        timezone_name = str(
            raw.get("settlement_timezone") or ""
        ).strip()
        latitude = float(raw.get("latitude"))
        longitude = float(raw.get("longitude"))
        registry_rows = raw.get("registry_rows")
        if (
            not station
            or station in scopes
            or not timezone_name
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not isinstance(registry_rows, Sequence)
            or isinstance(registry_rows, (str, bytes))
            or not registry_rows
        ):
            raise ContractError("eccc_geps_archive_station_scope_invalid")
        scopes[station] = {
            "settlement_timezone": timezone_name,
            "latitude": latitude,
            "longitude": longitude,
            "registry_rows": list(registry_rows),
            "forecast_hours": _forecast_hours(
                event_date=event_date,
                settlement_timezone=timezone_name,
                run_init_utc=run_init,
            ),
        }
    union_hours = sorted({
        hour
        for scope in scopes.values()
        for hour in scope["forecast_hours"]
    })
    if not union_hours or len(union_hours) > 20:
        raise ContractError("eccc_geps_archive_union_grid_invalid")
    member_ids = [
        "control",
        *(f"perturbed_{number:02d}" for number in range(1, 21)),
    ]
    member_values: dict[str, dict[str, dict[int, float]]] = {
        station: {member_id: {} for member_id in member_ids}
        for station in scopes
    }
    receipts: list[dict[str, Any]] = []
    total_bytes = 0
    with ThreadPoolExecutor(
        max_workers=MAX_DOWNLOAD_WORKERS,
        thread_name_prefix="eccc-geps-download",
    ) as executor:
        pending: dict[Any, int] = {}
        hours_iter = iter(union_hours)

        def submit_next() -> bool:
            try:
                hour = next(hours_iter)
            except StopIteration:
                return False
            future = executor.submit(
                _download_field,
                run_init=run_init,
                forecast_hour=hour,
                timeout_seconds=timeout_seconds,
            )
            pending[future] = hour
            return True

        for _index in range(MAX_DOWNLOAD_WORKERS):
            submit_next()
        while pending:
            done, _not_done = wait(
                pending,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                hour = pending.pop(future)
                raw, messages, metadata, receipt = future.result()
                total_bytes += len(raw)
                if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise ContractError(
                        "eccc_geps_archive_total_response_too_large"
                    )
                active = [
                    (
                        station,
                        float(scope["latitude"]),
                        float(scope["longitude"]),
                    )
                    for station, scope in scopes.items()
                    if hour in scope["forecast_hours"]
                ]
                for message, member_meta in zip(messages, metadata):
                    number = int(member_meta["number"])
                    member_id = (
                        "control"
                        if number == 0
                        else f"perturbed_{number:02d}"
                    )
                    decoded = decode_nearest_temperatures_c(
                        message,
                        points=active,
                    )
                    for station, value in decoded.items():
                        member_values[station][member_id][hour] = value
                receipts.append(receipt)
                assert_process_memory_budget(
                    projected_bytes=(
                        MAX_DOWNLOAD_WORKERS * MAX_FILE_BYTES
                    )
                )
                submit_next()
    receipts.sort(key=lambda row: int(row["forecast_hour"]))
    if len(receipts) != len(union_hours):
        raise ContractError("eccc_geps_archive_receipt_set_incomplete")
    captures = [
        _build_station_capture(
            station=station,
            event_date=event_date,
            scope=scope,
            run_init=run_init,
            query_at=query_at,
            member_values=member_values[station],
            receipts=receipts,
        )
        for station, scope in sorted(scopes.items())
    ]
    identity = {
        "schema": MULTISTATION_SCHEMA,
        "event_date": event_date,
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": _run_cycle_label(run_init),
        "archive_lead_bucket": lead_bucket_for_run(
            event_date=event_date,
            run_init=run_init,
            supported_lead_days=SUPPORTED_LEAD_DAYS,
            error_code="eccc_geps_archive_lead_days_invalid",
        ),
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "member_count": EXPECTED_MEMBER_COUNT,
        "member_availability_contract": (
            "OFFICIAL_ALLMBRS_CONTROL_0_PERTURBED_1_TO_20"
        ),
        "station_count": len(captures),
        "station_ids": [
            capture["station_id"] for capture in captures
        ],
        "union_forecast_hours": union_hours,
        "downloaded_file_receipt_count": len(receipts),
        "downloaded_file_receipt_set_sha256": content_sha256(receipts),
        "total_response_bytes": total_bytes,
        "captures": captures,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "single_download_per_global_field": True,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def ingest_geps_station_day(
    conn: sqlite3.Connection,
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    # Lead, cycle and run all come from the capture.  ECCC is the one family
    # read at two cycles, so its identity depends on both dimensions, and the
    # module constants answer neither for a capture that was not the default.
    capture_lead_days, capture_lead_bucket, _run_init, capture_run_cycle = (
        capture_lead_and_run(
            capture,
            supported_lead_days=SUPPORTED_LEAD_DAYS,
            default_lead_days=DEFAULT_LEAD_DAYS,
            error_code="eccc_geps_archive_lead_days_invalid",
        )
    )
    capture_extraction_version = extraction_version_for_lead(
        capture_lead_days, int(capture_run_cycle[:2])
    )
    identity = {
        key: value
        for key, value in capture.items()
        if key != "content_sha256"
    }
    members = capture.get("members")
    if (
        capture.get("schema") != SCHEMA
        or capture.get("source_class") != "OFFICIAL_NATIVE_ARCHIVE"
        or capture.get("training_only") is not True
        or capture.get("holdout_eligible") is not False
        or capture.get("content_sha256") != content_sha256(identity)
        or not isinstance(members, list)
        or len(members) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("eccc_geps_archive_capture_invalid")
    system = conn.execute(
        "SELECT provider_id,independence_family_id,"
        "expected_member_count "
        "FROM calibration_native_ensemble_system "
        "WHERE ensemble_system_id=?",
        (SYSTEM_ID,),
    ).fetchone()
    if (
        system is None
        or str(system[0]) != "ECCC"
        or str(system[1]) != INDEPENDENCE_FAMILY_ID
        or int(system[2]) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("eccc_geps_archive_system_registration_invalid")
    station = str(capture["station_id"])
    event_date = str(capture["event_date"])
    existing = conn.execute(
        "SELECT member_id,member_role,raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND station_id=? "
        "AND target_local_date=? AND archive_lead_bucket=? "
        "AND run_cycle=? AND extraction_version=? ORDER BY member_id",
        (
            SYSTEM_ID,
            station,
            event_date,
            capture_lead_bucket,
            capture_run_cycle,
            capture_extraction_version,
        ),
    ).fetchall()
    expected = sorted(
        (
            str(row["member_id"]),
            str(row["member_role"]),
            float(row["raw_tmax_c"]),
        )
        for row in members
    )
    if existing:
        if len(existing) != EXPECTED_MEMBER_COUNT or any(
            str(actual[0]) != wanted[0]
            or str(actual[1]) != wanted[1]
            or abs(float(actual[2]) - wanted[2]) > 1e-9
            for actual, wanted in zip(existing, expected)
        ):
            raise ContractError("eccc_geps_archive_frozen_fact_conflict")
        return {
            "state": "REPLAY",
            "member_run_wrote_count": 0,
            "member_bucket_wrote_count": 0,
        }
    member_writes = 0
    for row in members:
        member_identity = {
            "ensemble_system_id": SYSTEM_ID,
            "station_id": station,
            "target_local_date": event_date,
            "archive_lead_bucket": capture_lead_bucket,
            "run_cycle": capture_run_cycle,
            "member_id": str(row["member_id"]),
            "member_role": str(row["member_role"]),
            "raw_tmax_c": float(row["raw_tmax_c"]),
            "settlement_definition": "official_daily_tmax_c",
            "extraction_version": capture_extraction_version,
            "provider_id": str(system[0]),
            "transport_provider_id": "ECCC_DD_WEATHER",
            "native_run_init_utc": str(capture["run_init_utc"]),
            "offline_oos_eligible": 1,
            "archive_query_at_utc": str(
                capture["archive_query_at_utc"]
            ),
            "source_url": str(capture["source_prefix"]),
            "raw_object_ref": (
                "eccc_geps_file_receipt_set_sha256:"
                + str(capture["field_receipt_set_sha256"])
            ),
            "source_object_sha256": str(
                capture["field_receipt_set_sha256"]
            ),
            "qc_status": "ACCEPTED",
            "authority_scope": "CALIBRATION_TRAIN_ONLY",
            "holdout_eligible": 0,
        }
        row_sha = content_sha256(member_identity)
        member_writes += _insert_content_addressed(
            conn,
            table="calibration_native_ensemble_historical_member_run",
            primary_key="historical_member_run_id",
            row={
                "historical_member_run_id": (
                    "eccc-geps-history-member-" + row_sha[:28]
                ),
                **member_identity,
                "content_sha256": row_sha,
            },
        )
    bucket_intervals_json = json.dumps(
        capture["bucket_intervals"],
        sort_keys=True,
        separators=(",", ":"),
    )
    raw_probabilities_json = json.dumps(
        capture["raw_bucket_probabilities"],
        sort_keys=True,
        separators=(",", ":"),
    )
    bucket_identity = {
        "ensemble_system_id": SYSTEM_ID,
        "station_id": station,
        "target_local_date": event_date,
        "archive_lead_bucket": capture_lead_bucket,
        "archive_query_at_utc": str(capture["archive_query_at_utc"]),
        "member_set_sha256": str(capture["member_set_sha256"]),
        "member_count": EXPECTED_MEMBER_COUNT,
        "bucket_contract_sha256": str(
            capture["bucket_contract_sha256"]
        ),
        "bucket_intervals_json": bucket_intervals_json,
        "raw_bucket_probabilities_json": raw_probabilities_json,
        "source_role": "official_native_archive",
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": 0,
    }
    bucket_sha = content_sha256(bucket_identity)
    bucket_writes = _insert_content_addressed(
        conn,
        table="calibration_native_ensemble_historical_member_bucket",
        primary_key="historical_member_bucket_id",
        row={
            "historical_member_bucket_id": (
                "eccc-geps-history-bucket-" + bucket_sha[:28]
            ),
            **bucket_identity,
            "content_sha256": bucket_sha,
        },
    )
    return {
        "state": "COMMITTED",
        "member_run_wrote_count": member_writes,
        "member_bucket_wrote_count": bucket_writes,
    }


def ingest_geps_station_days(
    conn: sqlite3.Connection,
    archive: Mapping[str, Any],
) -> dict[str, Any]:
    identity = {
        key: value
        for key, value in archive.items()
        if key != "content_sha256"
    }
    captures = archive.get("captures")
    if (
        archive.get("schema") != MULTISTATION_SCHEMA
        or archive.get("training_only") is not True
        or archive.get("holdout_eligible") is not False
        or archive.get("single_download_per_global_field") is not True
        or archive.get("content_sha256") != content_sha256(identity)
        or not isinstance(captures, list)
        or len(captures) != int(archive.get("station_count") or 0)
    ):
        raise ContractError("eccc_geps_archive_multistation_invalid")
    results = [
        {
            "station_id": str(capture["station_id"]),
            **ingest_geps_station_day(conn, capture),
        }
        for capture in captures
    ]
    return {
        "state": (
            "REPLAY"
            if all(row["state"] == "REPLAY" for row in results)
            else "COMMITTED"
        ),
        "station_count": len(results),
        "member_run_wrote_count": sum(
            int(row["member_run_wrote_count"]) for row in results
        ),
        "member_bucket_wrote_count": sum(
            int(row["member_bucket_wrote_count"]) for row in results
        ),
        "station_results": results,
    }


__all__ = [
    "ARCHIVE_LEAD_BUCKET",
    "EXTRACTION_VERSION",
    "MULTISTATION_SCHEMA",
    "SCHEMA",
    "capture_geps_station_days",
    "ingest_geps_station_day",
    "ingest_geps_station_days",
]
