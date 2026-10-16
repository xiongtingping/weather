"""Official NOAA AIGEFS, the operational NCEP run on NOMADS: station daily highs for P0.

Charter 270 (user ruling 2026-10-15 「换」): the lane reads NCEP's operational
AIGEFS from NOMADS (``com/aigefs/prod``) as a new system, ``NOAA_AIGEFS_OPS_025``,
in the same independence family.  The EAGLE experimental feed this lane read
until then (``EAGLE_ensemble/`` on the NODD bucket, below) stopped after the
2026-10-13 06Z run, and the operational run is a different model: the same
run, member and step read from both differ by up to 6C (control at SBGR
+12h: 17.95C on EAGLE, 24.25C operational), and the files differ (an APCP
message, no surface PRES, ``P-ENS=n`` in the index).  ``NOAA_AIGEFS_025``
therefore keeps its rows and certificates as history and is not followed any
more; the new system trains from its own captures only.  NOMADS keeps about
two days, so there is no operational history to backfill beyond that window,
and it bans clients past ~120 requests a minute: every request here goes
through one in-process limiter.  Measured 2026-10-13..14 on NOMADS: every
step of a run lands by init + 3h50m..4h05m (00Z 10-14: f072 at +11h53m).
Rows carry their own transport id, ``NCEP_NOMADS`` (user ruling 2026-10-15
「加」, charter 270 (五)): the member table's CHECK was widened in place.  The
first hours of captures were written as ``NOAA_GEFS_PDS_S3`` under extraction
``_v1``; ``_v2`` re-ingests the retained follow files under the right label,
and the ``_v1`` rows are read by no contract.

The rest of this docstring is the EAGLE-era record (charter 212).

Charter 212 (user ruling 2026-09-24 「接入」): ``NOAA_AIGEFS_025`` is the sixth
formal P0 product and the fifth independent family (``NOAA_AIGEFS``; it shares
nothing with GEFS in the pool's family-balanced baseline).  The SSOT system row
registered through the Open-Meteo era (31 members) is reused verbatim, as the
AIFS lane reuses its own.  Measured before this module was written
(``scripts/research_aigefs_ens_allruns.py`` + production's stable-pool replay,
2026-08-16..09-19, 1,574 station-days): served P0 CRPS -3.0% on its own and
-1.1% on top of AIFS ENS, 30/45 stations.

What the source is (verified by HTTP listing 2026-09-23):

* the public NODD bucket ``noaa-nws-graphcastgfs-pds``, prefix
  ``EAGLE_ensemble/aigefs.YYYYMMDD/HH/memNNN/model/atmos/grib2/`` with all
  four cycles; member directories ``mem000`` (``ENS=+0``, the control) to
  ``mem030``;
* files ``aigefs.tHHz.sfc.fFFF.grib2`` every six hours, each with an NCEP
  ``.idx``: the ``TMP:2 m above ground`` message (0.46-0.59 MB) is fetched
  alone by byte range, the rest of the 5 MB file never;
* a regular 0.25 degree lat-lon grid, values in kelvin, read at each
  station's nearest cell by production's eccodes decoder;
* every step of a run lands in the same minute, init + 6h21m..8h11m over the
  36 runs 2026-09-14..09-22 (35 of them within 6h58m).

What is shared with the AIFS ENS lane, and why: both publish six-hourly
instants and no period maximum, so the daily high is the largest in-day
instant under the same floor (``ecmwf_aifs_ens_archive.day_instants``), no
family temperature curve is carried (the P0 root curve is formed from the
systems that carry one), receipts are kept and raw bodies are not.

Transport id on the rows is ``NOAA_GEFS_PDS_S3``: the same NOAA open-data
programme and S3 transport the GEFS lane reads, and the only NOAA id the
member table's CHECK constraint admits.  A dedicated id would need that
5.5-million-row table rebuilt; ``source_url`` names the exact bucket, and the
system id and extraction version keep the lineage apart.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx

from .member_trajectory_store import record_member_trajectories
from . import noaa_gefs_archive as gefs
from .contracts import ContractError, content_sha256
from .ecmwf_aifs_ens_archive import (
    DAILY_HIGH_ESTIMATOR,
    _station_scopes,
    day_instants,
    lead_days_for_run,
)
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
from official_nwp_https_transport import official_nwp_proxy_url


SCHEMA = "noaa_aigefs_ops_station_members_v1"
MULTISTATION_SCHEMA = "noaa_aigefs_ops_multistation_members_v1"
SYSTEM_ID = "NOAA_AIGEFS_OPS_025"
INDEPENDENCE_FAMILY_ID = "NOAA_AIGEFS"
PROVIDER_ID = "NOAA"
TRANSPORT_PROVIDER_ID = "NCEP_NOMADS"
MEMBER_AVAILABILITY_CONTRACT = "OFFICIAL_NCEP_PROD_CONTROL_PLUS_PERTURBED_1_TO_30"
EXPECTED_MEMBER_COUNT = 31
PERTURBED_MEMBER_COUNT = 30
STEP_HOURS = 6
SUPPORTED_LEAD_DAYS = (0, 1, 2)
DEFAULT_LEAD_DAYS = 2
SUPPORTED_RUN_CYCLE_HOURS = (0, 6, 12, 18)
RUN_CYCLE_HOUR_BY_LEAD = {2: 18, 1: 0, 0: 0}
BASE_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigefs/prod"
MAX_INDEX_BYTES = 256 * 1024
# Measured 2026-09-23: 0.46-0.59 MB; the decoder refuses anything over 1 MiB.
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 512 * 1024 * 1024
MAX_UNION_STEPS = 12
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_STATION_COUNT = 64
# NOMADS blocks a client past ~120 requests a minute; two requests a field
# (index + range) at 90 a minute reads one event day's ~5 steps x 31 members
# in about 3.5 minutes.
MAX_DOWNLOAD_WORKERS = 4
MAX_REQUESTS_PER_MINUTE = 90
USER_AGENT = "weather-aigefs-ops/1"


def resolve_run_cycle_hour(lead_days: int, run_cycle_hour: int | None = None) -> int:
    if int(lead_days) not in SUPPORTED_LEAD_DAYS:
        raise ContractError("aigefs_archive_lead_days_invalid")
    if run_cycle_hour is None:
        return RUN_CYCLE_HOUR_BY_LEAD[int(lead_days)]
    hour = int(run_cycle_hour)
    if hour not in SUPPORTED_RUN_CYCLE_HOURS:
        raise ContractError("aigefs_archive_run_cycle_hour_invalid")
    return hour


def extraction_version_for_lead(lead_days: int, run_cycle_hour: int | None = None) -> str:
    cycle_hour = resolve_run_cycle_hour(lead_days, run_cycle_hour)
    return f"ncep_nomads_aigefs_ops_0p25_2t6_{cycle_hour:02d}z_d{int(lead_days)}_v2"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ContractError("aigefs_archive_naive_datetime")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def member_id_for(number: int) -> str:
    return "control" if int(number) == 0 else f"perturbed_{int(number):02d}"


def object_key(run_init: datetime, member_number: int, forecast_step: int) -> str:
    run_init = _utc(run_init)
    return (
        f"aigefs.{run_init:%Y%m%d}/{run_init:%H}/mem{int(member_number):03d}/"
        f"model/atmos/grib2/aigefs.t{run_init:%H}z.sfc.f{int(forecast_step):03d}.grib2"
    )


def _parse_index(
    raw: bytes, *, run_init: datetime, member_number: int, forecast_step: int
) -> tuple[int, int | None, str]:
    """(start, end or None, index line) of the 2 m temperature message.

    NOMADS index lines read ``n:offset:d=YYYYMMDDHH:TMP:2 m above ground:24
    hour fcst:P-ENS=7`` (the control is ``P-ENS=0``); the run, step and member the line names must be the ones
    asked for, or the index describes some other object.
    """

    if len(raw) > MAX_INDEX_BYTES:
        raise ContractError("aigefs_archive_index_too_large")
    try:
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise ContractError("aigefs_archive_index_invalid") from exc
    run_init = _utc(run_init)
    wanted = (
        f"d={run_init:%Y%m%d%H}",
        "anl" if int(forecast_step) == 0 else f"{int(forecast_step)} hour fcst",
        f"P-ENS={int(member_number)}",
    )
    for i, line in enumerate(lines):
        parts = line.split(":")
        if len(parts) < 7 or parts[3] != "TMP" or parts[4] != "2 m above ground":
            continue
        if (parts[2], parts[5], parts[6]) != wanted:
            raise ContractError("aigefs_archive_index_line_unexpected")
        try:
            start = int(parts[1])
            end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else None
        except (ValueError, IndexError) as exc:
            raise ContractError("aigefs_archive_index_invalid") from exc
        if start < 0 or (end is not None and not 16 < end - start + 1 <= MAX_MESSAGE_BYTES):
            raise ContractError("aigefs_archive_message_range_invalid")
        return start, end, line
    raise ContractError("aigefs_archive_index_2t_missing")


class _RateLimiter:
    """Request starts spaced so no minute holds more than ``per_minute``."""

    def __init__(self, per_minute: int) -> None:
        self._interval = 60.0 / float(per_minute)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
        if start > now:
            sleep(start - now)


# One per process: the follow job reads several runs in a row.
_LIMITER = _RateLimiter(MAX_REQUESTS_PER_MINUTE)


def _get(url: str, **kwargs: Any) -> tuple[bytes, Any]:
    _LIMITER.wait()
    return gefs._get_bytes(url, **kwargs)


def _client(workers: int) -> httpx.Client:
    # Official NWP transport is explicit, as the GEFS lane's is: the
    # configured proxy or none, never the ambient desktop environment.
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        proxy=official_nwp_proxy_url() or None,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=workers, max_keepalive_connections=workers),
    )


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
    run_init = _utc(run_init)
    steps = [int(step) for step in scope["forecast_steps"]]
    step_set = set(steps)
    station_receipts = [
        dict(row) for row in receipts if int(row["forecast_step"]) in step_set
    ]
    if len(station_receipts) != EXPECTED_MEMBER_COUNT * len(steps):
        raise ContractError("aigefs_archive_station_receipt_incomplete")
    expected_members = [member_id_for(n) for n in range(EXPECTED_MEMBER_COUNT)]
    if sorted(member_values) != sorted(expected_members) or any(
        set(int(step) for step in member_values[member]) != step_set
        for member in expected_members
    ):
        raise ContractError("aigefs_archive_station_member_incomplete")
    members = [
        {
            "ensemble_system_id": SYSTEM_ID,
            "member_id": member_id,
            "member_role": "CONTROL" if member_id == "control" else "PERTURBED",
            "raw_tmax_c": max(float(member_values[member_id][step]) for step in steps),
        }
        for member_id in sorted(expected_members)
    ]
    intervals = (
        bucket_intervals_from_registry_rows(scope["registry_rows"])
        if scope.get("registry_rows")
        else {}
    )
    frozen_intervals = {
        bucket: [None if lo is None else float(lo), None if hi is None else float(hi)]
        for bucket, (lo, hi) in intervals.items()
    }
    # User 2026-10-16: every member's step series, kept beside the capture
    # (member_trajectory_store; a record that never fails the capture).
    record_member_trajectories(
        ensemble_system_id=SYSTEM_ID, station_id=station, event_date=event_date,
        settlement_timezone=str(scope["settlement_timezone"]), run_init=run_init, archive_query_at=query_at,
        step_values_by_member=member_values,
        valid_time_of_step=lambda step: run_init + timedelta(hours=int(step)),
    )
    member_identity = [
        {"member_id": r["member_id"], "member_role": r["member_role"], "raw_tmax_c": r["raw_tmax_c"]}
        for r in members
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
                (bounds[0] is None or r["raw_tmax_c"] >= float(bounds[0]))
                and (bounds[1] is None or r["raw_tmax_c"] < float(bounds[1]))
                for r in members
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


def capture_aigefs_station_days(
    *,
    event_date: str,
    station_scopes: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 45.0,
    lead_days: int = DEFAULT_LEAD_DAYS,
    run_cycle_hour: int | None = None,
) -> dict[str, Any]:
    """Read one AIGEFS run for every station's local day of ``event_date``.

    One index read and one range read per (member, step) for all stations:
    each message is a global field and every station is a nearest-cell
    extraction from it.
    """

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
        raise ContractError("aigefs_archive_scope_invalid")
    scopes = _station_scopes(
        station_scopes, event_date=event_date, run_init=run_init, require_registry_rows=True,
    )
    union_steps = sorted({step for scope in scopes.values() for step in scope["forecast_steps"]})
    if not union_steps or len(union_steps) > MAX_UNION_STEPS:
        raise ContractError("aigefs_archive_union_grid_invalid")
    try:
        requested = int(os.environ.get("AIGEFS_ARCHIVE_MAX_WORKERS", str(MAX_DOWNLOAD_WORKERS)))
    except ValueError as exc:
        raise ContractError("aigefs_archive_worker_count_invalid") from exc
    workers = max(1, min(MAX_DOWNLOAD_WORKERS, requested))
    total_bytes = 0
    member_values: dict[str, dict[str, dict[int, float]]] = {station: {} for station in scopes}
    receipts: list[dict[str, Any]] = []

    def fetch(item: tuple[int, int], client: httpx.Client) -> tuple[bytes, dict[str, Any]]:
        number, step = item
        key = object_key(run_init, number, step)
        index_raw, _ = _get(
            f"{BASE_URL}/{key}.idx", client=client, timeout_seconds=timeout_seconds,
            max_bytes=MAX_INDEX_BYTES,
        )
        start, end, line = _parse_index(
            index_raw, run_init=run_init, member_number=number, forecast_step=step,
        )
        span = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
        message, headers = _get(
            f"{BASE_URL}/{key}", client=client, timeout_seconds=timeout_seconds,
            max_bytes=MAX_MESSAGE_BYTES, headers={"Range": span},
        )
        if (
            not message.startswith(b"GRIB")
            or not message.endswith(b"7777")
            or (end is not None and len(message) != end - start + 1)
        ):
            raise ContractError("aigefs_archive_range_length_mismatch")
        receipt = {
            "member_id": member_id_for(number),
            "member_number": int(number),
            "forecast_step": int(step),
            "object_key": key,
            "index_sha256": hashlib.sha256(index_raw).hexdigest(),
            "index_line_sha256": hashlib.sha256(line.encode()).hexdigest(),
            "range_start": int(start),
            "range_end": int(start) + len(message) - 1,
            "message_sha256": hashlib.sha256(message).hexdigest(),
            "last_modified": str(headers.get("last-modified") or ""),
        }
        return message, receipt

    work = [(number, step) for step in union_steps for number in range(EXPECTED_MEMBER_COUNT)]
    with _client(workers) as client, ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="aigefs-download"
    ) as pool:
        pending: dict[Any, None] = {}
        items = iter(work)

        def submit_next() -> None:
            item = next(items, None)
            if item is not None:
                pending[pool.submit(fetch, item, client)] = None

        for _ in range(workers):
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                pending.pop(future)
                message, receipt = future.result()
                total_bytes += len(message)
                if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise ContractError("aigefs_archive_total_response_too_large")
                step = int(receipt["forecast_step"])
                active = [
                    (station, float(scope["latitude"]), float(scope["longitude"]))
                    for station, scope in scopes.items()
                    if step in scope["forecast_steps"]
                ]
                if active:
                    for station, value in decode_nearest_temperatures_c(message, points=active).items():
                        member_values[station].setdefault(str(receipt["member_id"]), {})[step] = float(value)
                receipts.append(receipt)
                assert_process_memory_budget(projected_bytes=workers * MAX_MESSAGE_BYTES)
                submit_next()
    receipts.sort(key=lambda r: (int(r["member_number"]), int(r["forecast_step"])))
    source_prefix = f"{BASE_URL}/aigefs.{run_init:%Y%m%d}/{run_init:%H}/"
    captures = [
        build_station_capture(
            station=station, event_date=event_date, scope=scope, run_init=run_init,
            query_at=query_at, member_values=member_values[station], receipts=receipts,
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


# Charter 270: the operational system's own row.  ``NOAA_AIGEFS_025`` (the
# EAGLE feed, canonical name "NOAA AIGEFS 0.25") keeps the row registered on
# 2026-07-31; the two share the family, so the pool never counts both.
SYSTEM_IDENTITY = {
    "ensemble_system_id": SYSTEM_ID,
    "canonical_name": "NOAA AIGEFS 0.25 operational",
    "independence_family_id": INDEPENDENCE_FAMILY_ID,
    "provider_id": PROVIDER_ID,
    "source_class": "NATIVE_ENSEMBLE_PROVIDER",
    "aggregator_or_republisher": 0,
    "expected_member_count": EXPECTED_MEMBER_COUNT,
    "member_identity_contract_version": "ncep_nomads_aigefs_prod_p_ens_0_to_30_v1",
    "publication_contract_version": "native_timestamps_required_for_formal_v1",
    "valid_from_utc": "2026-10-15T00:00:00Z",
    "valid_until_utc": "9999-12-31T23:59:59Z",
}


def ensure_system(conn: sqlite3.Connection) -> None:
    _insert_content_addressed(
        conn, table="calibration_native_ensemble_system", primary_key="ensemble_system_id",
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
        raise ContractError("aigefs_archive_system_registration_invalid")


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
            "historical_member_run_id": "aigefs-history-member-" + row_sha[:28],
            **member_identity,
            "content_sha256": row_sha,
        })
    return rows


def write_member_runs(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> str:
    if len(rows) != EXPECTED_MEMBER_COUNT:
        raise ContractError("aigefs_archive_member_rows_incomplete")
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
        (str(r["member_id"]), str(r["member_role"]), float(r["raw_tmax_c"])) for r in rows
    )
    if existing:
        if len(existing) != EXPECTED_MEMBER_COUNT or any(
            str(a[0]) != w[0] or str(a[1]) != w[1] or abs(float(a[2]) - w[2]) > 1e-4
            for a, w in zip(existing, expected)
        ):
            raise ContractError("aigefs_archive_frozen_fact_conflict")
        return "REPLAY"
    for row in rows:
        _insert_content_addressed(
            conn, table="calibration_native_ensemble_historical_member_run",
            primary_key="historical_member_run_id", row=dict(row),
        )
    return "COMMITTED"


def ingest_aigefs_station_day(conn: sqlite3.Connection, capture: Mapping[str, Any]) -> dict[str, Any]:
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
        raise ContractError("aigefs_archive_capture_invalid")
    ensure_system(conn)
    lead_days, lead_bucket, run_init, run_cycle = capture_lead_and_run(
        capture, supported_lead_days=SUPPORTED_LEAD_DAYS,
        default_lead_days=DEFAULT_LEAD_DAYS, error_code="aigefs_archive_lead_days_invalid",
    )
    station = str(capture["station_id"])
    event_date = str(capture["event_date"])
    rows = member_run_rows(
        station=station, event_date=event_date, lead_bucket=lead_bucket, run_cycle=run_cycle,
        run_init_utc=str(capture["run_init_utc"]),
        extraction_version=extraction_version_for_lead(lead_days, run_init.hour),
        members=members, archive_query_at_utc=str(capture["archive_query_at_utc"]),
        source_url=str(capture["source_prefix"]),
        raw_object_ref="aigefs_field_receipt_set_sha256:" + str(capture["field_receipt_set_sha256"]),
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
            "bucket_intervals_json": json.dumps(capture["bucket_intervals"], sort_keys=True, separators=(",", ":")),
            "raw_bucket_probabilities_json": json.dumps(capture["raw_bucket_probabilities"], sort_keys=True, separators=(",", ":")),
            "source_role": "official_native_archive",
            "authority_scope": "CALIBRATION_TRAIN_ONLY",
            "holdout_eligible": 0,
        }
        bucket_sha = content_sha256(bucket_identity)
        bucket_writes = _insert_content_addressed(
            conn, table="calibration_native_ensemble_historical_member_bucket",
            primary_key="historical_member_bucket_id",
            row={"historical_member_bucket_id": "aigefs-history-bucket-" + bucket_sha[:28], **bucket_identity, "content_sha256": bucket_sha},
        )
    return {
        "state": state,
        "member_run_wrote_count": EXPECTED_MEMBER_COUNT if state == "COMMITTED" else 0,
        "member_bucket_wrote_count": int(bucket_writes),
    }


def ingest_aigefs_station_days(conn: sqlite3.Connection, archive: Mapping[str, Any]) -> dict[str, Any]:
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
        raise ContractError("aigefs_archive_multistation_invalid")
    results = [ingest_aigefs_station_day(conn, capture) for capture in captures]
    return {
        "state": "REPLAY" if all(r["state"] == "REPLAY" for r in results) else "COMMITTED",
        "station_count": len(results),
        "member_run_wrote_count": sum(int(r["member_run_wrote_count"]) for r in results),
        "member_bucket_wrote_count": sum(int(r["member_bucket_wrote_count"]) for r in results),
    }


__all__ = [
    "BASE_URL",
    "EXPECTED_MEMBER_COUNT",
    "MULTISTATION_SCHEMA",
    "SCHEMA",
    "SUPPORTED_LEAD_DAYS",
    "SUPPORTED_RUN_CYCLE_HOURS",
    "SYSTEM_ID",
    "build_station_capture",
    "capture_aigefs_station_days",
    "day_instants",
    "extraction_version_for_lead",
    "ingest_aigefs_station_days",
    "lead_days_for_run",
    "member_run_rows",
    "object_key",
    "write_member_runs",
]
