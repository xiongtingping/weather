"""Official ECMWF AIFS ENS open data: station daily highs for P0.

Charter L0 lists ``ECMWF_AIFS_ENS_GLOBAL`` among the formal P0 products, and
the official source was always on the same ``ecmwf-forecasts`` bucket as the
IFS PF50 this lane already reads -- but nothing ever captured it, so P0 served
four families' products and never this one.  Measured before this module was
written (2026-09-21..23, ``scripts/research_*aifs*``): through production's
own stable-pool replay, adding AIFS ENS as one more native member improved
the served P0 CRPS on every lead bucket and at 39/45 stations.

What differs from the IFS PF50 collector, and why:

* **Members.**  AIFS ENS publishes a control and fifty perturbed members in
  two objects per step (``-enfo-cf`` and ``-enfo-pf``); all 51 are read, as
  the system row ``ECMWF_AIFS_ENS_GLOBAL`` already registered in the SSOT
  says (expected_member_count 51).  That row is reused verbatim -- it is
  content-addressed, and restating it with different wording would conflict.
* **Daily high.**  There is no period maximum in AIFS open data (IFS has
  ``mx2t3``), only the instantaneous ``2t`` every six hours.  A member's
  daily high is the largest of the instants inside the station's local day:
  all four for a run issued before the day starts, whatever remains (at
  least one) for a run issued after it -- the same-day floor the PF50
  collector applies, restated for instants.  Fitting a diurnal harmonic
  through the instants was measured and rejected (the debiased CRPS did not
  improve), so the extraction is the plain maximum and its version says so.
* **No family temperature curve.**  The P0 root curve that P1 reads is a
  weighted mean of the families' curves on their common valid times; the
  other families carry 3-hourly period maxima, AIFS six-hourly instants, and
  intersecting them would coarsen every root curve to four points.  AIFS
  captures carry no curve and the root curve is formed from the systems that
  do (``native_ensemble_p0``).
* **No raw store.**  Receipts (object key, byte range, index and message
  digests) are kept; the GRIB bytes are not.  The raw bodies are a cache,
  not evidence, and this lane would otherwise add ~2 GB a day to a disk that
  is already the binding constraint.

Transport is the PF50 lane's own (``ecmwf_ifs_archive._get_bytes``: the
endpoint allowlist, retries, S3 fallback and the charter 195 transport
breaker) against the same base URL, so the two ECMWF products are always read
from the same place.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import ecmwf_ifs_archive as ifs
from .contracts import ContractError, content_sha256
from .native_archive_lead import (
    capture_lead_and_run,
    capture_lead_bucket,
    run_cycle_label,
)
from .native_ensemble_store import _insert_content_addressed
from .noaa_gefs_archive import decode_nearest_temperatures_c
from .p0_producer import bucket_intervals_from_registry_rows
from .resource_bounds import assert_process_memory_budget
from .ssot import native_ensemble_foundation_schema_status


SCHEMA = "ecmwf_aifs_ens_official_historical_station_members_v1"
MULTISTATION_SCHEMA = (
    "ecmwf_aifs_ens_official_historical_multistation_members_v1"
)
SYSTEM_ID = "ECMWF_AIFS_ENS_GLOBAL"
# Charter 217 (user ruling 2026-09-25, 「把 IFS 和 AIFS 拆成两个家族」): AIFS
# ENS is its own independence family.  Registered on 2026-07-31 under
# "ECMWF", it shared one family share with IFS PF50 in the stable pool's
# baseline (charter 134), one 0.75 cap, and could not make a two-family pool
# with it.  The provider stays ECMWF: transport and publication are PF50's.
INDEPENDENCE_FAMILY_ID = "ECMWF_AIFS"
PRE_SPLIT_INDEPENDENCE_FAMILY_ID = "ECMWF"
PRE_SPLIT_REGISTRY_CONTENT_SHA256 = (
    "d38cd419f5b9ffab939027fd32c33c95d6e471e130afa27e5c64a00410375c3c"
)
PROVIDER_ID = "ECMWF"
# The same official objects as PF50 through the same endpoints; the OOS
# reader's transport allowlist already admits it.
TRANSPORT_PROVIDER_ID = "ECMWF_OPEN_DATA_S3"
MEMBER_AVAILABILITY_CONTRACT = (
    "OFFICIAL_OPEN_DATA_CONTROL_PLUS_PERTURBED_1_TO_50"
)
DAILY_HIGH_ESTIMATOR = "max_of_in_day_2t_six_hourly_instants_v1"
EXPECTED_MEMBER_COUNT = 51
PERTURBED_MEMBER_COUNT = 50
STEP_HOURS = 6
# Three calendar leads from an 18Z run reach at most 18 + 24*3 hours; the
# stored steps never go past this.
MAX_STEP_HOURS = 96
INSTANTS_PER_DAY = 24 // STEP_HOURS
SUPPORTED_LEAD_DAYS = (0, 1, 2)
DEFAULT_LEAD_DAYS = 2
# Every cycle ECMWF publishes, at every lead: the capture follows the newest
# run, and the coverage check below still refuses a run that cannot describe
# a station's day.  The per-lead default mirrors the PF50 collector's.
SUPPORTED_RUN_CYCLE_HOURS = (0, 6, 12, 18)
RUN_CYCLE_HOUR_BY_LEAD = {2: 18, 1: 0, 0: 0}
MEMBER_KINDS = ("cf", "pf")
MAX_INDEX_BYTES = 3 * 1024 * 1024
# Measured 2026-09-23: a ``2t`` message is 614-619 KiB; the decoder refuses
# anything over 1 MiB, and so does this.
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 768 * 1024 * 1024
MAX_UNION_STEPS = 12
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_STATION_COUNT = 64


def resolve_run_cycle_hour(
    lead_days: int,
    run_cycle_hour: int | None = None,
) -> int:
    if int(lead_days) not in SUPPORTED_LEAD_DAYS:
        raise ContractError("aifs_ens_archive_lead_days_invalid")
    if run_cycle_hour is None:
        return RUN_CYCLE_HOUR_BY_LEAD[int(lead_days)]
    hour = int(run_cycle_hour)
    if hour not in SUPPORTED_RUN_CYCLE_HOURS:
        raise ContractError("aifs_ens_archive_run_cycle_hour_invalid")
    return hour


def extraction_version_for_lead(
    lead_days: int,
    run_cycle_hour: int | None = None,
) -> str:
    """Specific to the lead and the cycle, as for every native family.

    ``2t6`` names the field and its six-hour step; the daily-high rule is
    fixed by ``DAILY_HIGH_ESTIMATOR`` and a change to it is a new version.
    """

    cycle_hour = resolve_run_cycle_hour(lead_days, run_cycle_hour)
    return (
        "ecmwf_open_data_aifs_ens_0p25_2t6_"
        f"{cycle_hour:02d}z_d{int(lead_days)}_v1"
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ContractError("aifs_ens_archive_naive_datetime")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def day_instants(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int] | None:
    """Steps whose valid instant falls inside the station's local day.

    None when the run cannot describe the day: short of all four instants
    for a run issued before the day starts, or none left for one issued
    after it.  A run issued after local midnight is same-day for that station
    even when its UTC date is the day before (the PF50 collector's rule).
    """

    run_init = _utc(run_init_utc)
    target = date.fromisoformat(event_date)
    start = datetime(
        target.year, target.month, target.day,
        tzinfo=ZoneInfo(settlement_timezone),
    ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    steps = [
        step
        for step in range(0, MAX_STEP_HOURS + 1, STEP_HOURS)
        if start <= run_init + timedelta(hours=step) < end
    ]
    same_day = run_init >= start
    if not steps or (not same_day and len(steps) != INSTANTS_PER_DAY):
        return None
    return steps


def lead_days_for_run(*, event_date: str, run_init_utc: datetime) -> int:
    return (date.fromisoformat(event_date) - _utc(run_init_utc).date()).days


def _object_urls(
    *, run_init: datetime, forecast_step: int, kind: str
) -> tuple[str, str, str]:
    if kind not in MEMBER_KINDS:
        raise ContractError("aifs_ens_archive_member_kind_invalid")
    run_init = _utc(run_init)
    prefix = (
        f"{run_init.strftime('%Y%m%d')}/{run_init.strftime('%H')}z/"
        f"aifs-ens/0p25/enfo/{run_init.strftime('%Y%m%d%H%M%S')}-"
        f"{int(forecast_step)}h-enfo-{kind}"
    )
    base = ifs.BASE_URL
    return f"{base}/{prefix}.index", f"{base}/{prefix}.grib2", prefix


def member_id_for(kind: str, number: int) -> str:
    if kind == "cf":
        return "control"
    return f"perturbed_{int(number):02d}"


def _parse_index(
    raw: bytes, *, forecast_step: int, kind: str
) -> list[dict[str, Any]]:
    """The ``2t`` surface rows of one step's cf or pf index, validated."""

    if len(raw) > MAX_INDEX_BYTES:
        raise ContractError("aifs_ens_archive_index_too_large")
    rows: list[dict[str, Any]] = []
    try:
        lines = raw.decode("utf-8").splitlines()
        if len(lines) > 20_000:
            raise ContractError("aifs_ens_archive_index_row_limit")
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            if (
                isinstance(row, Mapping)
                and row.get("param") == "2t"
                and row.get("levtype") == "sfc"
                and row.get("type") == kind
                and int(row.get("step") or -1) == int(forecast_step)
            ):
                rows.append(dict(row))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("aifs_ens_archive_index_invalid") from exc
    if kind == "cf":
        if len(rows) != 1:
            raise ContractError("aifs_ens_archive_control_index_incomplete")
        rows[0]["number"] = 0
    else:
        numbers = sorted(int(row.get("number") or 0) for row in rows)
        if numbers != list(range(1, PERTURBED_MEMBER_COUNT + 1)):
            raise ContractError("aifs_ens_archive_member_index_incomplete")
    for row in rows:
        offset = int(row.get("_offset") if row.get("_offset") is not None else -1)
        length = int(row.get("_length") or 0)
        if offset < 0 or length <= 16 or length > MAX_MESSAGE_BYTES:
            raise ContractError("aifs_ens_archive_message_range_invalid")
    return sorted(rows, key=lambda row: int(row["number"]))


def _station_scopes(
    station_scopes: Sequence[Mapping[str, Any]],
    *,
    event_date: str,
    run_init: datetime,
    require_registry_rows: bool,
) -> dict[str, dict[str, Any]]:
    scopes: dict[str, dict[str, Any]] = {}
    for raw in station_scopes:
        station = str(raw.get("station_id") or "").strip().upper()
        timezone_name = str(raw.get("settlement_timezone") or "").strip()
        try:
            latitude = float(raw.get("latitude"))
            longitude = float(raw.get("longitude"))
        except (TypeError, ValueError) as exc:
            raise ContractError("aifs_ens_archive_station_scope_invalid") from exc
        registry_rows = raw.get("registry_rows")
        if (
            not station
            or station in scopes
            or not timezone_name
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or (
                require_registry_rows
                and (
                    not isinstance(registry_rows, Sequence)
                    or isinstance(registry_rows, (str, bytes))
                    or not registry_rows
                )
            )
        ):
            raise ContractError("aifs_ens_archive_station_scope_invalid")
        steps = day_instants(
            event_date=event_date,
            settlement_timezone=timezone_name,
            run_init_utc=run_init,
        )
        if steps is None:
            raise ContractError("aifs_ens_archive_local_day_grid_incomplete")
        scopes[station] = {
            "station_id": station,
            "settlement_timezone": timezone_name,
            "latitude": latitude,
            "longitude": longitude,
            "registry_rows": list(registry_rows or []),
            "forecast_steps": steps,
        }
    return scopes


def build_station_capture(
    *,
    station: str,
    event_date: str,
    scope: Mapping[str, Any],
    run_init: datetime,
    query_at: datetime,
    member_values: Mapping[str, Mapping[int, float]],
    receipts: Sequence[Mapping[str, Any]],
    source_prefix: str,
) -> dict[str, Any]:
    """One station-day's sealed capture from decoded member values.

    ``member_values`` maps member id to {step: temperature C} for exactly the
    scope's in-day steps; ``receipts`` are the field receipts of those steps
    (a superset is filtered here).
    """

    run_init = _utc(run_init)
    steps = [int(step) for step in scope["forecast_steps"]]
    step_set = set(steps)
    station_receipts = [
        dict(row) for row in receipts if int(row["forecast_step"]) in step_set
    ]
    if len(station_receipts) != EXPECTED_MEMBER_COUNT * len(steps):
        raise ContractError("aifs_ens_archive_station_receipt_incomplete")
    expected_members = ["control"] + [
        f"perturbed_{number:02d}"
        for number in range(1, PERTURBED_MEMBER_COUNT + 1)
    ]
    if sorted(member_values) != sorted(expected_members) or any(
        set(int(step) for step in member_values[member]) != step_set
        for member in expected_members
    ):
        raise ContractError("aifs_ens_archive_station_member_incomplete")
    members = [
        {
            "ensemble_system_id": SYSTEM_ID,
            "member_id": member_id,
            "member_role": "CONTROL" if member_id == "control" else "PERTURBED",
            "raw_tmax_c": max(
                float(member_values[member_id][step]) for step in steps
            ),
        }
        for member_id in sorted(expected_members)
    ]
    intervals = (
        bucket_intervals_from_registry_rows(scope["registry_rows"])
        if scope.get("registry_rows")
        else {}
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
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": scope["settlement_timezone"],
        "latitude": float(scope["latitude"]),
        "longitude": float(scope["longitude"]),
        "run_init_utc": _stamp(run_init),
        "run_cycle": run_cycle_label(run_init),
        "archive_lead_bucket": capture_lead_bucket(
            event_date=event_date,
            settlement_timezone=str(scope["settlement_timezone"]),
            run_init=run_init,
        ),
        "forecast_steps": steps,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "member_count": len(members),
        "member_availability_contract": MEMBER_AVAILABILITY_CONTRACT,
        "daily_high_estimator": DAILY_HIGH_ESTIMATOR,
        "members": members,
        "member_set_sha256": content_sha256(member_identity),
        "bucket_intervals": frozen_intervals,
        "bucket_contract_sha256": content_sha256(frozen_intervals),
        "raw_bucket_probabilities": {
            bucket: sum(
                (bounds[0] is None or row["raw_tmax_c"] >= float(bounds[0]))
                and (bounds[1] is None or row["raw_tmax_c"] < float(bounds[1]))
                for row in members
            ) / len(members)
            for bucket, bounds in frozen_intervals.items()
        },
        "field_receipt_count": len(station_receipts),
        "field_receipt_set_sha256": receipt_set_sha,
        "field_receipts": station_receipts,
        "archive_query_at_utc": _stamp(query_at),
        "source_prefix": source_prefix,
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


def capture_aifs_station_days(
    *,
    event_date: str,
    station_scopes: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 45.0,
    lead_days: int = DEFAULT_LEAD_DAYS,
    run_cycle_hour: int | None = None,
) -> dict[str, Any]:
    """Read one AIFS ENS run for every station's local day of ``event_date``.

    One download per (member, step) for all stations: each message is a
    global field and every station is a nearest-cell extraction from it.
    """

    if ifs.BASE_URL not in ifs.ALLOWED_TRANSPORT_BASE_URLS:
        raise ContractError("aifs_ens_archive_transport_base_url_invalid")
    cycle_hour = resolve_run_cycle_hour(lead_days, run_cycle_hour)
    run_init = datetime.combine(
        date.fromisoformat(event_date) - timedelta(days=int(lead_days)),
        time(hour=cycle_hour),
        tzinfo=timezone.utc,
    )
    query_at = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if (
        query_at <= run_init
        or not 1 <= len(station_scopes) <= MAX_STATION_COUNT
        or shutil.disk_usage(Path(tempfile.gettempdir())).free < MIN_FREE_BYTES
    ):
        raise ContractError("aifs_ens_archive_scope_invalid")
    scopes = _station_scopes(
        station_scopes,
        event_date=event_date,
        run_init=run_init,
        require_registry_rows=True,
    )
    union_steps = sorted({
        step for scope in scopes.values() for step in scope["forecast_steps"]
    })
    if not union_steps or len(union_steps) > MAX_UNION_STEPS:
        raise ContractError("aifs_ens_archive_union_grid_invalid")
    worker_cap = (
        ifs.MAX_GCS_DOWNLOAD_WORKERS
        if ifs.BASE_URL == ifs.GCS_OFFICIAL_REPLICA_BASE_URL
        else ifs.MAX_DOWNLOAD_WORKERS
    )
    try:
        requested = int(os.environ.get("ECMWF_ARCHIVE_MAX_WORKERS", str(worker_cap)))
    except ValueError as exc:
        raise ContractError("aifs_ens_archive_worker_count_invalid") from exc
    workers = max(1, min(worker_cap, requested))

    def fetch_index(item: tuple[int, str]) -> tuple[int, str, str, str, bytes]:
        step, kind = item
        index_url, grib_url, prefix = _object_urls(
            run_init=run_init, forecast_step=step, kind=kind
        )
        raw, _headers = ifs._get_bytes(
            index_url, timeout_seconds=timeout_seconds, max_bytes=MAX_INDEX_BYTES
        )
        return step, kind, grib_url, prefix, raw

    total_bytes = 0
    work: list[tuple[str, str, str, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aifs-index") as pool:
        indexes = list(pool.map(
            fetch_index, [(step, kind) for step in union_steps for kind in MEMBER_KINDS]
        ))
    for step, kind, grib_url, prefix, raw in sorted(indexes, key=lambda r: (r[0], r[1])):
        total_bytes += len(raw)
        index_sha = hashlib.sha256(raw).hexdigest()
        for row in _parse_index(raw, forecast_step=step, kind=kind):
            work.append((grib_url, prefix + ".grib2", index_sha, {**row, "_kind": kind}))

    def download(item: tuple[str, str, str, dict[str, Any]]):
        grib_url, object_key, index_sha, row = item
        offset, length = int(row["_offset"]), int(row["_length"])
        message, headers = ifs._get_bytes(
            grib_url,
            timeout_seconds=timeout_seconds,
            max_bytes=MAX_MESSAGE_BYTES,
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        )
        if len(message) != length or not message.startswith(b"GRIB"):
            raise ContractError("aifs_ens_archive_range_length_mismatch")
        kind = str(row["_kind"])
        number = int(row["number"])
        receipt = {
            "member_id": member_id_for(kind, number),
            "member_number": number,
            "member_kind": kind,
            "forecast_step": int(row["step"]),
            "object_key": object_key,
            "index_sha256": index_sha,
            "index_row_sha256": content_sha256(
                {k: v for k, v in row.items() if k != "_kind"}
            ),
            "range_start": offset,
            "range_end": offset + length - 1,
            "message_sha256": hashlib.sha256(message).hexdigest(),
            "last_modified": str(headers.get("last-modified") or ""),
        }
        return message, receipt

    member_values: dict[str, dict[str, dict[int, float]]] = {
        station: {} for station in scopes
    }
    receipts: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aifs-download") as pool:
        pending: dict[Any, None] = {}
        items = iter(work)

        def submit_next() -> None:
            item = next(items, None)
            if item is not None:
                pending[pool.submit(download, item)] = None

        for _ in range(workers):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                pending.pop(future)
                message, receipt = future.result()
                total_bytes += len(message)
                if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise ContractError("aifs_ens_archive_total_response_too_large")
                step = int(receipt["forecast_step"])
                active = [
                    (station, float(scope["latitude"]), float(scope["longitude"]))
                    for station, scope in scopes.items()
                    if step in scope["forecast_steps"]
                ]
                if active:
                    decoded = decode_nearest_temperatures_c(message, points=active)
                    for station, value in decoded.items():
                        member_values[station].setdefault(
                            str(receipt["member_id"]), {}
                        )[step] = float(value)
                receipts.append(receipt)
                assert_process_memory_budget(
                    projected_bytes=workers * MAX_MESSAGE_BYTES
                )
                submit_next()
    receipts.sort(key=lambda row: (int(row["member_number"]), int(row["forecast_step"])))
    source_prefix = (
        f"{ifs.BASE_URL}/{run_init.strftime('%Y%m%d')}/"
        f"{run_init.strftime('%H')}z/aifs-ens/0p25/enfo/"
    )
    captures = [
        build_station_capture(
            station=station,
            event_date=event_date,
            scope=scope,
            run_init=run_init,
            query_at=query_at,
            member_values=member_values[station],
            receipts=receipts,
            source_prefix=source_prefix,
        )
        for station, scope in sorted(scopes.items())
    ]
    identity = {
        "schema": MULTISTATION_SCHEMA,
        "event_date": event_date,
        "run_init_utc": _stamp(run_init),
        "run_cycle": run_cycle_label(run_init),
        "archive_lead_bucket": f"d{int(lead_days)}",
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "member_count": EXPECTED_MEMBER_COUNT,
        "member_availability_contract": MEMBER_AVAILABILITY_CONTRACT,
        "daily_high_estimator": DAILY_HIGH_ESTIMATOR,
        "station_count": len(captures),
        "station_ids": [capture["station_id"] for capture in captures],
        "union_forecast_steps": union_steps,
        "downloaded_field_receipt_count": len(receipts),
        "downloaded_field_receipt_set_sha256": content_sha256(receipts),
        "total_response_bytes": total_bytes,
        "network_response_bytes": total_bytes,
        "local_raw_store_hit_count": 0,
        "captures": captures,
        "archive_query_at_utc": _stamp(query_at),
        "single_download_per_global_member_field": True,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


# The SSOT row as registered on 2026-07-31 (content sha d38cd419...), with
# one field changed by charter 217: the independence family.  Its wording
# predates the official lane -- it names the republisher's member contract --
# but it describes the same 51 members of the same system, and apart from
# that one sanctioned relabel a content-addressed row is never reworded.
SYSTEM_IDENTITY = {
    "ensemble_system_id": SYSTEM_ID,
    "canonical_name": "ECMWF AIFS 0.25 Ensemble",
    "independence_family_id": INDEPENDENCE_FAMILY_ID,
    "provider_id": PROVIDER_ID,
    "source_class": "NATIVE_ENSEMBLE_PROVIDER",
    "aggregator_or_republisher": 0,
    "expected_member_count": EXPECTED_MEMBER_COUNT,
    "member_identity_contract_version": "open_meteo_control_plus_perturbed_v1",
    "publication_contract_version": "native_timestamps_required_for_formal_v1",
    "valid_from_utc": "2026-07-31T00:00:00Z",
    "valid_until_utc": "9999-12-31T23:59:59Z",
}


def relabel_pre_split_registration(conn: sqlite3.Connection) -> bool:
    """Move the registered row from the ECMWF family to AIFS's own (charter 217).

    The registry is append-only and keyed by system, so the family of a
    registered system has no insert-only path; this is the one sanctioned
    edit.  It acts only on the exact pre-split row (its content sha), changes
    the family and the content sha and nothing else, and puts the no-update
    guard back verbatim, all inside one savepoint.  True when it relabelled,
    False when there was nothing to do.

    Artifacts minted before it keep serving under the map they were certified
    under (``native_ensemble_p0.served_family_map``) until the OOS rotation
    re-mints them under the split.
    """

    row = conn.execute(
        "SELECT content_sha256 FROM calibration_native_ensemble_system "
        "WHERE ensemble_system_id=?",
        (SYSTEM_ID,),
    ).fetchone()
    if row is None or str(row[0]) != PRE_SPLIT_REGISTRY_CONTENT_SHA256:
        return False
    pre_split_identity = {
        **SYSTEM_IDENTITY,
        "independence_family_id": PRE_SPLIT_INDEPENDENCE_FAMILY_ID,
    }
    if content_sha256(pre_split_identity) != PRE_SPLIT_REGISTRY_CONTENT_SHA256:
        raise ContractError("aifs_ens_pre_split_registration_unreproducible")
    guard = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='calibration_native_system_no_update'"
    ).fetchone()
    if guard is None or not str(guard[0] or "").strip():
        raise ContractError("aifs_ens_registry_update_guard_missing")
    conn.execute("SAVEPOINT aifs_ens_family_split")
    try:
        conn.execute("DROP TRIGGER calibration_native_system_no_update")
        cursor = conn.execute(
            "UPDATE calibration_native_ensemble_system "
            "SET independence_family_id=?,content_sha256=? "
            "WHERE ensemble_system_id=? AND content_sha256=?",
            (
                INDEPENDENCE_FAMILY_ID,
                content_sha256(SYSTEM_IDENTITY),
                SYSTEM_ID,
                PRE_SPLIT_REGISTRY_CONTENT_SHA256,
            ),
        )
        conn.execute(str(guard[0]))
        if cursor.rowcount != 1:
            raise ContractError("aifs_ens_registry_relabel_failed")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT aifs_ens_family_split")
        conn.execute("RELEASE SAVEPOINT aifs_ens_family_split")
        raise
    conn.execute("RELEASE SAVEPOINT aifs_ens_family_split")
    return True


def ensure_system(conn: sqlite3.Connection) -> None:
    relabel_pre_split_registration(conn)
    _insert_content_addressed(
        conn,
        table="calibration_native_ensemble_system",
        primary_key="ensemble_system_id",
        row={**SYSTEM_IDENTITY, "content_sha256": content_sha256(SYSTEM_IDENTITY)},
    )
    actual = conn.execute(
        "SELECT provider_id,independence_family_id,expected_member_count "
        "FROM calibration_native_ensemble_system WHERE ensemble_system_id=?",
        (SYSTEM_ID,),
    ).fetchone()
    if (
        actual is None
        or str(actual[0]) != PROVIDER_ID
        or str(actual[1]) != INDEPENDENCE_FAMILY_ID
        or int(actual[2]) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("aifs_ens_archive_system_registration_invalid")


def member_run_rows(
    *,
    station: str,
    event_date: str,
    lead_bucket: str,
    run_cycle: str,
    run_init_utc: str,
    extraction_version: str,
    members: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    source_url: str,
    raw_object_ref: str,
    source_object_sha256: str,
) -> list[dict[str, Any]]:
    """The member-run rows for one station-day, in the table's own shape.

    Shared by the live ingest and the history backfill, so a backfilled row
    and a captured one differ only in what they say about their source.
    """

    rows = []
    for row in members:
        member_identity = {
            "ensemble_system_id": SYSTEM_ID,
            "station_id": station,
            "target_local_date": event_date,
            "archive_lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "member_id": str(row["member_id"]),
            "member_role": str(row["member_role"]),
            "raw_tmax_c": float(row["raw_tmax_c"]),
            "settlement_definition": "official_daily_tmax_c",
            "extraction_version": extraction_version,
            "provider_id": PROVIDER_ID,
            "transport_provider_id": TRANSPORT_PROVIDER_ID,
            "native_run_init_utc": run_init_utc,
            "offline_oos_eligible": 1,
            "archive_query_at_utc": archive_query_at_utc,
            "source_url": source_url,
            "raw_object_ref": raw_object_ref,
            "source_object_sha256": source_object_sha256,
            "qc_status": "ACCEPTED",
            "authority_scope": "CALIBRATION_TRAIN_ONLY",
            "holdout_eligible": 0,
        }
        row_sha = content_sha256(member_identity)
        rows.append({
            "historical_member_run_id": "aifs-ens-history-member-" + row_sha[:28],
            **member_identity,
            "content_sha256": row_sha,
        })
    return rows


def write_member_runs(
    conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
) -> str:
    """Insert one station-day's rows, or verify an identical earlier write.

    A station-day already present under the same (lead bucket, cycle,
    extraction version) must hold the same members to the value; anything
    else is a frozen-fact conflict, as in the PF50 ingest.
    """

    if len(rows) != EXPECTED_MEMBER_COUNT:
        raise ContractError("aifs_ens_archive_member_rows_incomplete")
    first = rows[0]
    existing = conn.execute(
        "SELECT member_id,member_role,raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND station_id=? AND target_local_date=? "
        "AND archive_lead_bucket=? AND run_cycle=? AND extraction_version=? "
        "AND native_run_init_utc=? ORDER BY member_id",
        (
            SYSTEM_ID, first["station_id"], first["target_local_date"],
            first["archive_lead_bucket"], first["run_cycle"],
            first["extraction_version"], first["native_run_init_utc"],
        ),
    ).fetchall()
    expected = sorted(
        (str(r["member_id"]), str(r["member_role"]), float(r["raw_tmax_c"]))
        for r in rows
    )
    if existing:
        if len(existing) != EXPECTED_MEMBER_COUNT or any(
            str(a[0]) != w[0] or str(a[1]) != w[1] or abs(float(a[2]) - w[2]) > 1e-4
            for a, w in zip(existing, expected)
        ):
            raise ContractError("aifs_ens_archive_frozen_fact_conflict")
        return "REPLAY"
    for row in rows:
        _insert_content_addressed(
            conn,
            table="calibration_native_ensemble_historical_member_run",
            primary_key="historical_member_run_id",
            row=dict(row),
        )
    return "COMMITTED"


def ingest_aifs_station_day(
    conn: sqlite3.Connection, capture: Mapping[str, Any]
) -> dict[str, Any]:
    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    identity = {k: v for k, v in capture.items() if k != "content_sha256"}
    members = capture.get("members")
    if (
        capture.get("schema") != SCHEMA
        or capture.get("source_class") != "OFFICIAL_NATIVE_ARCHIVE"
        or capture.get("member_availability_contract") != MEMBER_AVAILABILITY_CONTRACT
        or capture.get("daily_high_estimator") != DAILY_HIGH_ESTIMATOR
        or capture.get("training_only") is not True
        or capture.get("holdout_eligible") is not False
        or capture.get("content_sha256") != content_sha256(identity)
        or not isinstance(members, list)
        or len(members) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("aifs_ens_archive_capture_invalid")
    ensure_system(conn)
    lead_days, lead_bucket, run_init, run_cycle = capture_lead_and_run(
        capture,
        supported_lead_days=SUPPORTED_LEAD_DAYS,
        default_lead_days=DEFAULT_LEAD_DAYS,
        error_code="aifs_ens_archive_lead_days_invalid",
    )
    station = str(capture["station_id"])
    event_date = str(capture["event_date"])
    rows = member_run_rows(
        station=station,
        event_date=event_date,
        lead_bucket=lead_bucket,
        run_cycle=run_cycle,
        run_init_utc=str(capture["run_init_utc"]),
        extraction_version=extraction_version_for_lead(lead_days, run_init.hour),
        members=members,
        archive_query_at_utc=str(capture["archive_query_at_utc"]),
        source_url=str(capture["source_prefix"]),
        raw_object_ref=(
            "aifs_ens_field_receipt_set_sha256:"
            + str(capture["field_receipt_set_sha256"])
        ),
        source_object_sha256=str(capture["field_receipt_set_sha256"]),
    )
    state = write_member_runs(conn, rows)
    bucket_writes = 0
    if state == "COMMITTED" and capture.get("bucket_intervals"):
        bucket_identity = {
            "ensemble_system_id": SYSTEM_ID,
            "station_id": station,
            "target_local_date": event_date,
            "archive_lead_bucket": lead_bucket,
            "archive_query_at_utc": str(capture["archive_query_at_utc"]),
            "member_set_sha256": str(capture["member_set_sha256"]),
            "member_count": EXPECTED_MEMBER_COUNT,
            "bucket_contract_sha256": str(capture["bucket_contract_sha256"]),
            "bucket_intervals_json": json.dumps(
                capture["bucket_intervals"], sort_keys=True, separators=(",", ":")
            ),
            "raw_bucket_probabilities_json": json.dumps(
                capture["raw_bucket_probabilities"], sort_keys=True, separators=(",", ":")
            ),
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
                "historical_member_bucket_id": "aifs-ens-history-bucket-" + bucket_sha[:28],
                **bucket_identity,
                "content_sha256": bucket_sha,
            },
        )
    return {
        "state": state,
        "member_run_wrote_count": EXPECTED_MEMBER_COUNT if state == "COMMITTED" else 0,
        "member_bucket_wrote_count": int(bucket_writes),
    }


def ingest_aifs_station_days(
    conn: sqlite3.Connection, archive: Mapping[str, Any]
) -> dict[str, Any]:
    identity = {k: v for k, v in archive.items() if k != "content_sha256"}
    captures = archive.get("captures")
    if (
        archive.get("schema") != MULTISTATION_SCHEMA
        or archive.get("training_only") is not True
        or archive.get("holdout_eligible") is not False
        or archive.get("content_sha256") != content_sha256(identity)
        or not isinstance(captures, list)
        or len(captures) != int(archive.get("station_count") or 0)
    ):
        raise ContractError("aifs_ens_archive_multistation_invalid")
    results = [ingest_aifs_station_day(conn, capture) for capture in captures]
    return {
        "state": (
            "REPLAY" if all(row["state"] == "REPLAY" for row in results) else "COMMITTED"
        ),
        "station_count": len(results),
        "member_run_wrote_count": sum(int(r["member_run_wrote_count"]) for r in results),
        "member_bucket_wrote_count": sum(int(r["member_bucket_wrote_count"]) for r in results),
    }


__all__ = [
    "DAILY_HIGH_ESTIMATOR",
    "EXPECTED_MEMBER_COUNT",
    "MULTISTATION_SCHEMA",
    "SCHEMA",
    "SUPPORTED_LEAD_DAYS",
    "SUPPORTED_RUN_CYCLE_HOURS",
    "SYSTEM_ID",
    "build_station_capture",
    "capture_aifs_station_days",
    "day_instants",
    "extraction_version_for_lead",
    "ingest_aifs_station_days",
    "member_run_rows",
    "write_member_runs",
]
