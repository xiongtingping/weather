"""Official ECMWF IFS ensemble open-data history for calibration OOS."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
import time as time_module
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .contracts import ContractError, content_sha256
from .ecmwf_pf50_raw_reuse import (
    RetainedMemberField,
    find_content_addressed_message,
    find_range_addressed_message,
    load_retained_field_index,
    store_content_addressed_message,
    store_range_alias,
)
from .future_temperature_curve import (
    MIN_CURVE_POINTS,
    MIN_SAME_DAY_CURVE_POINTS,
    build_native_family_temperature_curve,
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


SCHEMA = "ecmwf_ifs_open_pf50_historical_station_members_v1"
MULTISTATION_SCHEMA = (
    "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
)
SYSTEM_ID = "ECMWF_IFS_ENS_OPEN_PF50"
# The cycle this collector reads at its default lead.  Every capture and every
# row carries the cycle of the run it was actually drawn from instead of this
# constant; it survives only as the d2 answer and as external documentation of
# what "the ECMWF archive" means without further qualification.
RUN_CYCLE = "18Z"
# P0 follows the freshest run, so the same collector is asked for a different
# lead as the event day approaches: d2 two days out, d1 the day before, d0 on
# the day.  ``ARCHIVE_LEAD_BUCKET`` remains the default for callers that do not
# ask, so existing d2 behaviour is unchanged.
SUPPORTED_LEAD_DAYS = (0, 1, 2)
DEFAULT_LEAD_DAYS = 2
# The cycle is not free to choose: it must leave enough forecast steps to cover
# the target local day everywhere on earth.  18Z works at d2 because the run
# precedes the day by a full night, but at d1 it starts after the local day has
# already begun in the far east -- Auckland gets five 3-hour periods instead of
# eight -- so the shorter leads take the day's first cycle instead.
RUN_CYCLE_HOUR_BY_LEAD = {2: 18, 1: 0, 0: 0}
# ECMWF open data publishes four cycles; these are the two this lane reads.
# All four published cycles.  06Z and 12Z were excluded while the run
# ladder only asked for 00Z/18Z; the ladder now follows every cycle the
# publisher issues, and the coverage check below still rejects any cycle
# that cannot cover a station's local day.
SUPPORTED_RUN_CYCLE_HOURS = (0, 6, 12, 18)
ARCHIVE_LEAD_BUCKET = f"d{DEFAULT_LEAD_DAYS}"
# Charter 193 amendment (2026-09-21, user ruling "A 维持现状 / C 先用 ECMWF 前一天
# 18Z"): a D+2 day lists about D-2 05Z, when the newest ECMWF run that covers it
# is D-3 18Z -- D-2 00Z is complete on the replica only around 08:10Z.  That
# run is captured for training on the follow lane (``follow_run``) at a lead
# the ladder never asks for, under its own identity; it is not a ladder lead,
# so ``SUPPORTED_LEAD_DAYS`` -- which the OOS contract and every lead loop read
# -- stays as it is and only capture and ingest accept it.
FOLLOW_ONLY_LEAD_CYCLES = {3: (18,)}
CAPTURE_LEAD_DAYS = tuple(sorted({*SUPPORTED_LEAD_DAYS, *FOLLOW_ONLY_LEAD_CYCLES}))


def _capture_lead_allowed(lead_days: int, cycle_hour: int) -> bool:
    lead = int(lead_days)
    if lead in SUPPORTED_LEAD_DAYS:
        return True
    return int(cycle_hour) in FOLLOW_ONLY_LEAD_CYCLES.get(lead, ())


def resolve_run_cycle_hour(
    lead_days: int,
    run_cycle_hour: int | None = None,
) -> int:
    """The cycle this capture reads, defaulting to the lead's own mapping.

    See ``noaa_gefs_archive.resolve_run_cycle_hour``: the default exists to
    guarantee the run covers the target local day, but for eastern stations
    the previous day's 18Z covers more of that day than the day's own 00Z and
    is published earlier, and no lead maps to it.  The grid coverage check is
    unchanged and still rejects a cycle that genuinely comes up short.
    """

    if run_cycle_hour is None:
        return RUN_CYCLE_HOUR_BY_LEAD[int(lead_days)]
    hour = int(run_cycle_hour)
    if hour not in SUPPORTED_RUN_CYCLE_HOURS:
        raise ContractError("ecmwf_archive_run_cycle_hour_invalid")
    return hour


def extraction_version_for_lead(
    lead_days: int,
    run_cycle_hour: int | None = None,
) -> str:
    """Extraction identity is specific to the lead *and* to its cycle.

    A d1 row read the same way as a d2 row is still different evidence: it
    comes from a different run and carries different skill.  Sharing one
    version string across leads would let the OOS scope check accept a d1 row
    against d2's contract and train the two together.

    The cycle is spelled out rather than fixed at ``18z`` because it moves with
    the lead -- see ``RUN_CYCLE_HOUR_BY_LEAD``.  Writing ``18z`` on a capture
    drawn from the 00Z run would restate the very mislabelling the per-lead
    identity exists to prevent, one dimension over, and nothing downstream
    could detect it: the label and the contract would agree with each other
    while both disagreed with the object that was downloaded.
    """

    cycle_hour = resolve_run_cycle_hour(lead_days, run_cycle_hour)
    return (
        "ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_"
        f"{cycle_hour:02d}z_d{int(lead_days)}_v1"
    )


EXTRACTION_VERSION = extraction_version_for_lead(DEFAULT_LEAD_DAYS)
# Same official ``ecmwf-forecasts`` S3 bucket through two AWS endpoints.
# Direct regional transport is measurably faster; the global endpoint remains
# a same-object fallback if AWS throttles or rejects the regional request.
PRIMARY_BASE_URL = (
    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
)
FALLBACK_BASE_URL = "https://ecmwf-forecasts.s3.amazonaws.com"
GCS_OFFICIAL_REPLICA_BASE_URL = (
    "https://storage.googleapis.com/ecmwf-open-data"
)
ALLOWED_TRANSPORT_BASE_URLS = frozenset(
    {PRIMARY_BASE_URL, FALLBACK_BASE_URL, GCS_OFFICIAL_REPLICA_BASE_URL}
)
# The configured value selects transport only.  Every allowed endpoint serves
# ECMWF's date-addressed Open Data namespace; it does not change provider,
# model, member, or native lineage.  A process uses one base for an entire
# packet so ``source_prefix`` remains truthful and cannot hide mixed hosts.
BASE_URL = os.environ.get(
    "ECMWF_ARCHIVE_TRANSPORT_BASE_URL", PRIMARY_BASE_URL
).rstrip("/")
MAX_INDEX_BYTES = 3 * 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 768 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_STATION_COUNT = 64
DEFAULT_RETAINED_PACKET_DIR = Path(
    "output/audits/native_ensemble_history"
)
DEFAULT_RAW_OBJECT_DIR = Path(
    "data/runtime/official_nwp_sources/ecmwf_pf50/raw"
)
DEFAULT_RAW_SEARCH_ROOTS = (
    Path("data/runtime/official_nwp_sources/ecmwf/raw"),
    Path(
        "output/audits/bayes_basket_target_45station_native_history/"
        "ecmwf_18z_d2_objects/ecmwf_ifs025_official/raw"
    ),
)
# ECMWF's public archive throttles sustained parallel range requests with
# ``503 Slow Down``.  Historical recovery is deliberately conservative: one
# process with three bounded workers avoids both the repeated eight-way retry
# storms and the reproducible four-worker ``503 Slow Down`` failure.
#
# Raising this to 24 to shorten capture wall time reproduced exactly that
# failure -- ``503 Slow Down`` on the enfo index roughly 90s into a capture --
# so the limit is empirical, not cautionary.  ECMWF dominates a capture (51
# members, ~816 latency-bound requests) and is therefore slow at three workers;
# the answer is the GCS replica below, which is not rate-limited the same way,
# not more parallelism against S3.
MAX_DOWNLOAD_WORKERS = 3
# Measured against the replica with real GRIB range requests (~649 KiB each):
# 3 workers sustain 0.23 MB/s, 16 sustain 0.80, 24 sustain 0.93, and no level up
# to 32 returned a single 503 -- the limit is the local proxy's round trip, not
# the endpoint.  Small-object probes did show throughput rolling off past 24, so
# this ceiling is above the measured optimum on purpose: it is a cap, and
# ``ECMWF_ARCHIVE_MAX_WORKERS`` can dial it back without a code change.
MAX_GCS_DOWNLOAD_WORKERS = 32
EXPECTED_MEMBER_COUNT = 50
# A failed GET may be retried only for an explicitly transient network
# failure.  The cap applies independently to each official S3 endpoint; with
# the regional endpoint plus its global same-object fallback this bounds every
# request to at most ten attempts.  Four parallel workers repeatedly tripped
# the public archive's sustained-request limiter; the production recovery
# path is fixed at three workers and gives a throttled immutable object enough
# bounded time to become readable without restarting the whole settled day.
MAX_TRANSPORT_ATTEMPTS = 5
MAX_RETRY_BACKOFF_SECONDS = 1.0
RETRY_BACKOFF_BASE_SECONDS = 0.25
# S3 ``Slow Down`` needs materially longer than a socket retry.  The scope is
# deliberately narrow: only HTTP 503 takes the bounded 2s/4s/8s/8s waits.
S3_SLOW_DOWN_BACKOFF_BASE_SECONDS = 2.0
MAX_S3_SLOW_DOWN_BACKOFF_SECONDS = 8.0
S3_FALLBACK_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
TRANSIENT_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.WriteTimeout,
)
_HTTP_CLIENT_LOCAL = threading.local()


def _configured_archive_proxy() -> str | None:
    """Return only the explicit operator proxy for official NWP capture.

    The ECMWF client keeps ``trust_env=False`` so unrelated ambient proxy
    settings cannot silently route a bounded S3 capture.  Production
    operators must opt in with ``PM_OFFICIAL_NWP_PROXY`` (the weather local
    default is ``http://127.0.0.1:1082``).
    """

    try:
        return official_nwp_proxy_url() or None
    except ValueError as exc:
        raise ContractError("ecmwf_archive_proxy_invalid") from exc


def _thread_http_client() -> httpx.Client:
    """Reuse one bounded connection per download thread.

    A 45-station day needs roughly 850 small byte-range requests. Creating a
    fresh client for every range forced a fresh proxy/TLS handshake each
    time and made one settled day take many minutes. Thread-local clients
    preserve the existing four-connection ceiling while allowing HTTP
    keep-alive within each worker. The short-lived backfill process owns and
    releases all sockets at process exit.
    """

    client = getattr(_HTTP_CLIENT_LOCAL, "client", None)
    if client is None:
        client = httpx.Client(
            follow_redirects=False,
            # Keep one bounded connection per worker.  The explicit proxy is
            # opt-in so operators can enforce the local NWP egress policy;
            # otherwise the client remains direct and ignores ambient env.
            trust_env=False,
            proxy=_configured_archive_proxy(),
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=30.0,
            ),
        )
        _HTTP_CLIENT_LOCAL.client = client
    return client


def _discard_thread_http_client() -> None:
    """Drop a poisoned proxy connection pool before the bounded retry."""

    client = getattr(_HTTP_CLIENT_LOCAL, "client", None)
    if client is None:
        return
    try:
        client.close()
    finally:
        try:
            delattr(_HTTP_CLIENT_LOCAL, "client")
        except AttributeError:
            pass


def _get_bytes(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
) -> tuple[bytes, Mapping[str, str]]:
    endpoint_urls = [url]
    if url.startswith(PRIMARY_BASE_URL):
        endpoint_urls.append(
            url.replace(PRIMARY_BASE_URL, FALLBACK_BASE_URL, 1)
        )

    last_transport_error: Exception | None = None
    last_http_error: httpx.HTTPStatusError | None = None
    for request_url in endpoint_urls:
        for attempt in range(MAX_TRANSPORT_ATTEMPTS):
            try:
                request_headers = {
                    "User-Agent": "weather-ecmwf-history/1",
                    **dict(headers or {}),
                }
                client = _thread_http_client()
                with client.stream(
                    "GET",
                    request_url,
                    headers=request_headers,
                    timeout=timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    length = response.headers.get("Content-Length")
                    if length and int(length) > max_bytes:
                        raise ContractError(
                            "ecmwf_archive_response_too_large"
                        )
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ContractError(
                                "ecmwf_archive_response_too_large"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), dict(response.headers)
            except TRANSIENT_TRANSPORT_ERRORS as exc:
                last_transport_error = exc
                # A proxy pool timeout can leave a half-open connection in
                # httpcore.  Recreate only this worker's client; no global
                # session or other worker is touched.
                _discard_thread_http_client()
                if attempt + 1 >= MAX_TRANSPORT_ATTEMPTS:
                    break
                time_module.sleep(
                    min(
                        RETRY_BACKOFF_BASE_SECONDS * (2**attempt),
                        MAX_RETRY_BACKOFF_SECONDS,
                    )
                )
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code
                    not in S3_FALLBACK_HTTP_STATUS_CODES
                ):
                    raise
                # S3's ``503 Slow Down`` is a per-endpoint throttling signal,
                # not proof that the immutable object is absent.  Retry the
                # same official object with the existing bounded exponential
                # schedule before trying the regional/global same-object
                # fallback.  Both dimensions are capped at five attempts;
                # a day capture therefore remains resumable by rerunning its
                # unchanged content-addressed output after a final failure.
                last_http_error = exc
                if attempt + 1 >= MAX_TRANSPORT_ATTEMPTS:
                    break
                base_seconds = (
                    S3_SLOW_DOWN_BACKOFF_BASE_SECONDS
                    if exc.response.status_code == 503
                    else RETRY_BACKOFF_BASE_SECONDS
                )
                cap_seconds = (
                    MAX_S3_SLOW_DOWN_BACKOFF_SECONDS
                    if exc.response.status_code == 503
                    else MAX_RETRY_BACKOFF_SECONDS
                )
                time_module.sleep(
                    min(
                        base_seconds * (2**attempt),
                        cap_seconds,
                    )
                )

    if last_http_error is not None:
        raise last_http_error
    if last_transport_error is not None:
        raise ContractError(
            "ecmwf_archive_transport_failed:"
            + type(last_transport_error).__name__
        ) from last_transport_error
    raise ContractError("ecmwf_archive_transport_failed:unknown")


def _run_after_local_day_start(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> bool:
    """Whether this run is a same-day run *for this station*.

    A run issued on the event date in UTC is same-day everywhere; a run
    issued after the station's local midnight is same-day for that station
    even when its UTC date is the day before -- a d1 18Z run is 03:00 in
    Tokyo on the event day.  Either way it cannot describe hours that have
    already happened and its grid is legitimately shorter.  Judging by UTC
    date alone held those eastern stations to the full-day bar and failed
    the whole batch on ``*_local_day_grid_incomplete``.
    """

    target = date.fromisoformat(event_date)
    run_start = run_init_utc.astimezone(timezone.utc)
    if run_start.date() == target:
        return True
    day_start = datetime(
        target.year, target.month, target.day,
        tzinfo=ZoneInfo(settlement_timezone),
    ).astimezone(timezone.utc)
    return run_start >= day_start


def _period_end_steps(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int]:
    target = date.fromisoformat(event_date)
    zone = ZoneInfo(settlement_timezone)
    result = [
        step
        for step in range(3, 73, 3)
        if (
            run_init_utc + timedelta(hours=step)
        ).astimezone(zone).date() == target
    ]
    # A full local day is eight 3-hour periods, and that is the right bar for
    # d2 and d1: their run precedes the day, so anything short means the grid
    # is broken.
    #
    # It is the wrong bar for d0.  That run is issued after the local day has
    # begun, so it cannot describe hours that have already happened -- and does
    # not need to: those are settled observation by then.  What d0 must still
    # cover is the part of the day where the maximum can still occur.  Demanding
    # eight periods here rejected every d0 capture outright, which is why the
    # freshest same-day run was unusable.
    same_day = _run_after_local_day_start(
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        run_init_utc=run_init_utc,
    )
    minimum = 1 if same_day else 7
    if not minimum <= len(result) <= 9:
        raise ContractError("ecmwf_archive_local_day_grid_incomplete")
    return result


def follow_period_end_steps(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int] | None:
    """Charter 193: the steps a follow run seals for one station, or None.

    ``_period_end_steps`` raises for the whole batch; a follow capture asks per
    station first and leaves out the ones the run cannot seal (D-3 18Z reaches
    +72h, so the far west is left out), plus those the curve seal would refuse.
    """

    try:
        steps = _period_end_steps(
            event_date=event_date,
            settlement_timezone=settlement_timezone,
            run_init_utc=run_init_utc,
        )
    except ContractError:
        return None
    same_day = _run_after_local_day_start(
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        run_init_utc=run_init_utc,
    )
    floor = MIN_SAME_DAY_CURVE_POINTS if same_day else MIN_CURVE_POINTS
    return steps if len(steps) >= floor else None


def _object_urls(
    *,
    run_init: datetime,
    forecast_step: int,
) -> tuple[str, str, str]:
    run_day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    timestamp = run_init.strftime("%Y%m%d%H%M%S")
    prefix = (
        f"{run_day}/{cycle}z/ifs/0p25/enfo/"
        f"{timestamp}-{forecast_step}h-enfo-ef"
    )
    return (
        f"{BASE_URL}/{prefix}.index",
        f"{BASE_URL}/{prefix}.grib2",
        prefix,
    )


def _parse_index(
    raw: bytes,
    *,
    forecast_step: int,
) -> list[dict[str, Any]]:
    if len(raw) > MAX_INDEX_BYTES:
        raise ContractError("ecmwf_archive_index_too_large")
    rows = []
    try:
        lines = raw.decode("utf-8").splitlines()
        if len(lines) > 20_000:
            raise ContractError("ecmwf_archive_index_row_limit")
        for line in lines:
            row = json.loads(line)
            if (
                isinstance(row, Mapping)
                and row.get("param") == "mx2t3"
                and row.get("type") == "pf"
                and row.get("levtype") == "sfc"
                and int(row.get("step") or -1) == forecast_step
            ):
                rows.append(dict(row))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("ecmwf_archive_index_invalid") from exc
    numbers = {int(row.get("number") or 0) for row in rows}
    if (
        len(rows) != EXPECTED_MEMBER_COUNT
        or numbers != set(range(1, EXPECTED_MEMBER_COUNT + 1))
    ):
        raise ContractError("ecmwf_archive_member_index_incomplete")
    for row in rows:
        offset = int(row.get("_offset") or -1)
        length = int(row.get("_length") or 0)
        if (
            offset < 0
            or length <= 16
            or length > MAX_MESSAGE_BYTES
        ):
            raise ContractError("ecmwf_archive_message_range_invalid")
    return sorted(rows, key=lambda row: int(row["number"]))


def _download_message(
    *,
    grib_url: str,
    index_sha256: str,
    object_key: str,
    row: Mapping[str, Any],
    timeout_seconds: float,
    retained: RetainedMemberField | None = None,
    raw_roots: Sequence[Path] = (),
    raw_store: Path | None = None,
) -> tuple[bytes, dict[str, Any], bool]:
    if retained is not None:
        local = find_content_addressed_message(
            retained.message_sha256, raw_roots
        )
        if local is not None:
            return local.read_bytes(), dict(retained.receipt), True
    offset = int(row["_offset"])
    length = int(row["_length"])
    member_number = int(row["number"])
    # No retained receipt, but this range may still have been fetched before:
    # captures for two event dates served by the same run share roughly half
    # their forecast steps, and each step is the same immutable object range.
    # The digest is only known after a download, so the store is asked by
    # source coordinate instead; the bytes it returns are verified against
    # that digest before they are used.
    if retained is None and raw_roots:
        cached = find_range_addressed_message(
            object_key=object_key,
            range_start=offset,
            range_end=offset + length - 1,
            roots=raw_roots,
        )
        if cached is not None:
            path, record = cached
            message = path.read_bytes()
            if len(message) == length:
                return message, {
                    "member_id": f"perturbed_{member_number:02d}",
                    "member_number": member_number,
                    "forecast_step": int(row["step"]),
                    "object_key": object_key,
                    "index_sha256": index_sha256,
                    "index_row_sha256": content_sha256(dict(row)),
                    "range_start": offset,
                    "range_end": offset + length - 1,
                    "message_sha256": str(record["message_sha256"]),
                    # The object's own header, recorded when these bytes were
                    # first fetched.  ECMWF objects are immutable, so it still
                    # describes them; inventing a fresh value here would claim
                    # a response that was never made.
                    "last_modified": str(record.get("last_modified") or ""),
                }, True
    message, headers = _get_bytes(
        grib_url,
        timeout_seconds=timeout_seconds,
        max_bytes=MAX_MESSAGE_BYTES,
        headers={"Range": f"bytes={offset}-{offset + length - 1}"},
    )
    if len(message) != length:
        raise ContractError("ecmwf_archive_range_length_mismatch")
    receipt = {
        "member_id": f"perturbed_{member_number:02d}",
        "member_number": member_number,
        "forecast_step": int(row["step"]),
        "object_key": object_key,
        "index_sha256": index_sha256,
        "index_row_sha256": content_sha256(dict(row)),
        "range_start": offset,
        "range_end": offset + length - 1,
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "last_modified": str(headers.get("last-modified") or ""),
    }
    if retained is not None:
        if hashlib.sha256(message).hexdigest() != retained.message_sha256:
            raise ContractError("ecmwf_archive_retained_message_sha_mismatch")
        # Availability evidence belongs to the immutable ECMWF object, not
        # to the AWS/GCS mirror used for this replay.  Preserve the retained
        # receipt after byte identity has been proved.
        receipt = dict(retained.receipt)
    if raw_store is not None:
        store_content_addressed_message(
            message,
            root=raw_store,
            expected_sha256=str(receipt["message_sha256"]),
        )
        store_range_alias(
            object_key=object_key,
            range_start=offset,
            range_end=offset + length - 1,
            message_sha256=str(receipt["message_sha256"]),
            root=raw_store,
            last_modified=str(receipt.get("last_modified") or ""),
        )
    return message, receipt, False


def _retained_packet_dir() -> Path:
    return Path(
        os.environ.get(
            "ECMWF_PF50_RETAINED_PACKET_DIR",
            str(DEFAULT_RETAINED_PACKET_DIR),
        )
    ).expanduser().resolve()


def _raw_object_dir() -> Path:
    return Path(
        os.environ.get(
            "ECMWF_PF50_RAW_OBJECT_DIR", str(DEFAULT_RAW_OBJECT_DIR)
        )
    ).expanduser().resolve()


def _raw_search_roots(raw_store: Path) -> tuple[Path, ...]:
    configured = os.environ.get("ECMWF_PF50_RAW_SEARCH_ROOTS")
    candidates = (
        [Path(value) for value in configured.split(os.pathsep) if value]
        if configured is not None
        else list(DEFAULT_RAW_SEARCH_ROOTS)
    )
    roots: list[Path] = [raw_store]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


@lru_cache(maxsize=4)
def _retained_fields_for_directory(
    directory_text: str,
) -> dict[tuple[str, str, str, int], RetainedMemberField]:
    directory = Path(directory_text)
    if not directory.is_dir():
        return {}
    return load_retained_field_index(
        [
            *directory.glob("ECMWF_PF50_45station_*_official_d2.json"),
            *directory.glob("official_ecmwf_ifs_forward_*.json"),
        ]
    )


def _retained_index_rows(
    *, run_init_utc: str, forecast_steps: Sequence[int]
) -> list[tuple[str, str, str, dict[str, Any], RetainedMemberField]] | None:
    index = _retained_fields_for_directory(str(_retained_packet_dir()))
    rows: list[tuple[str, str, str, dict[str, Any], RetainedMemberField]] = []
    for number in range(1, EXPECTED_MEMBER_COUNT + 1):
        member_id = f"perturbed_{number:02d}"
        for step in forecast_steps:
            field = index.get((SYSTEM_ID, run_init_utc, member_id, int(step)))
            if field is None:
                return None
            receipt = field.receipt
            try:
                start = int(receipt["range_start"])
                end = int(receipt["range_end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ContractError("ecmwf_archive_retained_range_invalid") from exc
            object_key = str(receipt.get("object_key") or "")
            index_sha = str(receipt.get("index_sha256") or "")
            if not object_key or len(index_sha) != 64 or end < start:
                raise ContractError("ecmwf_archive_retained_receipt_invalid")
            row = {
                "number": number,
                "step": int(step),
                "_offset": start,
                "_length": end - start + 1,
            }
            rows.append((
                f"{BASE_URL}/{object_key}",
                index_sha,
                object_key,
                row,
                field,
            ))
    return rows


def _lead_bucket_for_run(*, event_date: str, run_init: datetime) -> str:
    """Lead implied by the gap between the run day and the target day."""

    delta = (
        date.fromisoformat(event_date) - run_init.astimezone(timezone.utc).date()
    ).days
    if not _capture_lead_allowed(delta, run_init.astimezone(timezone.utc).hour):
        raise ContractError("ecmwf_archive_lead_days_invalid")
    return f"d{delta}"


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
    steps = list(scope["forecast_steps"])
    station_receipts = [
        dict(row)
        for row in receipts
        if int(row["forecast_step"]) in set(steps)
    ]
    if len(station_receipts) != EXPECTED_MEMBER_COUNT * len(steps):
        raise ContractError("ecmwf_archive_station_receipt_incomplete")
    values_by_member = {
        member_id: (
            {int(step): float(value) for step, value in values.items()}
            if isinstance(values, Mapping)
            else {
                int(step): float(value)
                for step, value in zip(steps, values)
            }
        )
        for member_id, values in member_values.items()
    }
    members = [
        {
            "ensemble_system_id": SYSTEM_ID,
            "member_id": member_id,
            "member_role": "PERTURBED",
            "raw_tmax_c": max(values.values()),
        }
        for member_id, values in sorted(values_by_member.items())
    ]
    if (
        len(members) != EXPECTED_MEMBER_COUNT
        or any(
            set(values_by_member[row["member_id"]]) != set(steps)
            for row in members
        )
    ):
        raise ContractError("ecmwf_archive_station_member_incomplete")
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
            if _run_after_local_day_start(
                event_date=event_date,
                settlement_timezone=str(scope["settlement_timezone"]),
                run_init_utc=run_init,
            )
            else MIN_CURVE_POINTS
        ),
        payload={
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": scope["settlement_timezone"],
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "ECMWF",
        "available_at_utc": query_at.isoformat().replace("+00:00", "Z"),
        "source_field_receipt_set_sha256": receipt_set_sha,
        "points": [
            {
                "valid_time_utc": (run_init + timedelta(hours=step)).isoformat().replace("+00:00", "Z"),
                "temperature_c": sum(
                    values_by_member[member_id][step]
                    for member_id in values_by_member
                ) / len(values_by_member),
            }
            for step in steps
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
        # The run's own cycle.  It moves with the lead, so the module
        # default would label a 00Z d1 capture as 18Z.
        "run_cycle": run_cycle_label(run_init),
        # Derived from the run actually used rather than from the module
        # default, so a capture can never advertise a lead its run does not
        # carry -- and stated in hours ahead of this station's own local day,
        # not as a calendar ``d`` label.  One run reaches a station at UTC+8
        # and one at UTC-4 with lead hours half a day apart, so the label has
        # to be per station; the calendar label called all of them the same
        # thing and put them in one training group.
        "archive_lead_bucket": capture_lead_bucket(
            event_date=event_date,
            settlement_timezone=str(scope["settlement_timezone"]),
            run_init=run_init,
        ),
        "forecast_steps": steps,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "ECMWF",
        "member_count": len(members),
        "member_availability_contract": (
            "OFFICIAL_OPEN_DATA_PERTURBED_1_TO_50_NO_CONTROL"
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
            ) / len(members)
            for bucket, bounds in frozen_intervals.items()
        },
        "field_receipt_count": len(station_receipts),
        "field_receipt_set_sha256": receipt_set_sha,
        "field_receipts": station_receipts,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "source_prefix": (
            f"{BASE_URL}/{run_init.strftime('%Y%m%d')}/"
            f"{run_init.strftime('%H')}z/ifs/0p25/enfo/"
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


def capture_ecmwf_station_days(
    *,
    event_date: str,
    station_scopes: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 45.0,
    lead_days: int = DEFAULT_LEAD_DAYS,
    run_cycle_hour: int | None = None,
) -> dict[str, Any]:
    if BASE_URL not in ALLOWED_TRANSPORT_BASE_URLS:
        raise ContractError("ecmwf_archive_transport_base_url_invalid")
    if lead_days not in CAPTURE_LEAD_DAYS or (
        lead_days not in SUPPORTED_LEAD_DAYS and run_cycle_hour is None
    ):
        raise ContractError("ecmwf_archive_lead_days_invalid")
    lead_bucket = f"d{lead_days}"
    cycle_hour = resolve_run_cycle_hour(lead_days, run_cycle_hour)
    if not _capture_lead_allowed(lead_days, cycle_hour):
        raise ContractError("ecmwf_archive_lead_days_invalid")
    target = date.fromisoformat(event_date)
    run_init = datetime.combine(
        target - timedelta(days=lead_days),
        time(hour=cycle_hour),
        tzinfo=timezone.utc,
    )
    query_at = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    projection_generated_at = query_at
    if (
        query_at <= run_init
        or not 1 <= len(station_scopes) <= MAX_STATION_COUNT
        or shutil.disk_usage(Path(tempfile.gettempdir())).free
        < MIN_FREE_BYTES
    ):
        raise ContractError("ecmwf_archive_scope_invalid")
    worker_cap = (
        MAX_GCS_DOWNLOAD_WORKERS
        if BASE_URL == GCS_OFFICIAL_REPLICA_BASE_URL
        else MAX_DOWNLOAD_WORKERS
    )
    # Default to the cap of whichever endpoint is actually in use.  Defaulting
    # to the conservative ECMWF figure meant ``min(16, 3)`` still yielded three
    # workers on the replica: the higher ceiling existed but nothing ever
    # requested it.  Measured against the replica with real GRIB range requests
    # (~649 KiB each), three workers sustain 0.23 MB/s and sixteen sustain
    # 0.80 MB/s with no 503 at any level up to twenty-four -- the wall is the
    # local proxy's round trip, not the endpoint.
    #
    # The three-worker figure remains right for ``data.ecmwf.int``, where four
    # workers reproducibly returned `503 Slow Down`.
    try:
        requested_workers = int(
            os.environ.get("ECMWF_ARCHIVE_MAX_WORKERS", str(worker_cap))
        )
    except ValueError as exc:
        raise ContractError("ecmwf_archive_worker_count_invalid") from exc
    workers = max(1, min(worker_cap, requested_workers))
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
            raise ContractError("ecmwf_archive_station_scope_invalid")
        scopes[station] = {
            "station_id": station,
            "settlement_timezone": timezone_name,
            "latitude": latitude,
            "longitude": longitude,
            "registry_rows": list(registry_rows),
            "forecast_steps": _period_end_steps(
                event_date=event_date,
                settlement_timezone=timezone_name,
                run_init_utc=run_init,
            ),
        }
    union_steps = sorted({
        step
        for scope in scopes.values()
        for step in scope["forecast_steps"]
    })
    if not union_steps or len(union_steps) > 17:
        raise ContractError("ecmwf_archive_union_grid_invalid")
    member_values: dict[str, dict[str, dict[int, float]]] = {
        station: {
            f"perturbed_{number:02d}": {}
            for number in range(1, EXPECTED_MEMBER_COUNT + 1)
        }
        for station in scopes
    }
    receipts = []
    total_bytes = 0
    # Fields served from the local raw store rather than fetched.  Counted
    # apart from ``total_bytes`` so the archive cannot claim a network response
    # it never made: the field count is what the capture read, the network
    # figure is what it actually cost.
    raw_cache_hits = 0
    network_bytes = 0
    index_rows: list[
        tuple[str, str, str, Mapping[str, Any], RetainedMemberField | None]
    ] = []

    def fetch_index(
        step: int,
    ) -> tuple[int, str, str, bytes]:
        index_url, grib_url, object_prefix = _object_urls(
            run_init=run_init,
            forecast_step=step,
        )
        index_raw, _headers = _get_bytes(
            index_url,
            timeout_seconds=timeout_seconds,
            max_bytes=MAX_INDEX_BYTES,
        )
        return step, grib_url, object_prefix, index_raw

    run_init_text = run_init.isoformat().replace("+00:00", "Z")
    retained_rows = _retained_index_rows(
        run_init_utc=run_init_text,
        forecast_steps=union_steps,
    )
    retained_arrival_evidence: dict[str, Any] | None = None
    if retained_rows is not None:
        # The old 45-station packet is a receipt manifest for the global
        # member fields.  Reuse its exact range coordinates and hashes; the
        # absence of a new pilot artifact is not a reason to fetch indexes.
        index_rows.extend(retained_rows)
        retained_fields = [row[4] for row in retained_rows]
        retained_at = max(
            datetime.fromisoformat(
                field.retained_at_utc.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            for field in retained_fields
        )
        if retained_at > projection_generated_at:
            raise ContractError(
                "ecmwf_archive_retained_arrival_after_projection"
            )
        query_at = retained_at
        retained_arrival_evidence = {
            "schema": "official_native_retained_arrival_projection_v1",
            "ensemble_system_id": SYSTEM_ID,
            "retained_at_utc": query_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "projection_generated_at_utc": projection_generated_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "source_packet_content_sha256s": sorted({
                field.source_packet_content_sha256
                for field in retained_fields
            }),
            "source_packet_refs": sorted({
                field.source_packet_ref for field in retained_fields
            }),
            "field_count": len(retained_fields),
            "network_download_count": 0,
        }
        retained_arrival_evidence["content_sha256"] = content_sha256(
            retained_arrival_evidence
        )
    else:
        # Only a genuinely absent receipt scope may query the index service.
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ecmwf-index",
        ) as index_executor:
            index_futures = [
                index_executor.submit(fetch_index, step)
                for step in union_steps
            ]
            fetched_indexes = [future.result() for future in index_futures]
        for step, grib_url, object_prefix, index_raw in sorted(
            fetched_indexes,
            key=lambda row: row[0],
        ):
            total_bytes += len(index_raw)
            index_sha = hashlib.sha256(index_raw).hexdigest()
            for row in _parse_index(index_raw, forecast_step=step):
                index_rows.append((
                    grib_url,
                    index_sha,
                    object_prefix + ".grib2",
                    row,
                    None,
                ))
    raw_store = _raw_object_dir()
    raw_roots = _raw_search_roots(raw_store)
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="ecmwf-download",
    ) as executor:
        pending: dict[Any, int] = {}
        row_iter = iter(index_rows)

        def submit_next() -> bool:
            try:
                grib_url, index_sha, object_key, row, retained = next(
                    row_iter
                )
            except StopIteration:
                return False
            future = executor.submit(
                _download_message,
                grib_url=grib_url,
                index_sha256=index_sha,
                object_key=object_key,
                row=row,
                timeout_seconds=timeout_seconds,
                retained=retained,
                raw_roots=raw_roots,
                raw_store=raw_store,
            )
            pending[future] = int(row["step"])
            return True

        for _index in range(workers):
            submit_next()
        while pending:
            done, _not_done = wait(
                pending,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                step = pending.pop(future)
                message, receipt, from_local_store = future.result()
                if from_local_store:
                    raw_cache_hits += 1
                else:
                    network_bytes += len(message)
                total_bytes += len(message)
                if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise ContractError(
                        "ecmwf_archive_total_response_too_large"
                    )
                active = [
                    (
                        station,
                        float(scope["latitude"]),
                        float(scope["longitude"]),
                    )
                    for station, scope in scopes.items()
                    if step in scope["forecast_steps"]
                ]
                decoded = decode_nearest_temperatures_c(
                    message,
                    points=active,
                )
                member_id = str(receipt["member_id"])
                for station, value in decoded.items():
                    member_values[station][member_id][step] = value
                receipts.append(receipt)
                assert_process_memory_budget(
                    projected_bytes=(
                        workers * MAX_MESSAGE_BYTES
                    )
                )
                submit_next()
    receipts.sort(
        key=lambda row: (
            int(row["member_number"]),
            int(row["forecast_step"]),
        )
    )
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
        "run_cycle": run_cycle_label(run_init),
        "archive_lead_bucket": lead_bucket,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "ECMWF",
        "member_count": EXPECTED_MEMBER_COUNT,
        "member_availability_contract": (
            "OFFICIAL_OPEN_DATA_PERTURBED_1_TO_50_NO_CONTROL"
        ),
        "station_count": len(captures),
        "station_ids": [
            capture["station_id"] for capture in captures
        ],
        "union_forecast_steps": union_steps,
        "downloaded_field_receipt_count": len(receipts),
        "downloaded_field_receipt_set_sha256": content_sha256(
            receipts
        ),
        "total_response_bytes": total_bytes,
        "network_response_bytes": network_bytes,
        "local_raw_store_hit_count": raw_cache_hits,
        "captures": captures,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "projection_generated_at_utc": projection_generated_at.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "retained_arrival_evidence": retained_arrival_evidence,
        "single_download_per_global_member_field": True,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _ensure_system(conn: sqlite3.Connection) -> tuple[str, str]:
    identity = {
        "ensemble_system_id": SYSTEM_ID,
        "canonical_name": "ECMWF IFS Open Data PF50",
        "independence_family_id": "ECMWF",
        "provider_id": "ECMWF",
        "source_class": "NATIVE_ENSEMBLE_PROVIDER",
        "aggregator_or_republisher": 0,
        "expected_member_count": EXPECTED_MEMBER_COUNT,
        "member_identity_contract_version": (
            "ecmwf_open_data_perturbed_1_to_50_no_control_v1"
        ),
        "publication_contract_version": (
            "native_run_init_known_archive_publication_unknown_v1"
        ),
        "valid_from_utc": "2026-06-01T00:00:00Z",
        "valid_until_utc": "9999-12-31T23:59:59Z",
    }
    row = {
        **identity,
        "content_sha256": content_sha256(identity),
    }
    _insert_content_addressed(
        conn,
        table="calibration_native_ensemble_system",
        primary_key="ensemble_system_id",
        row=row,
    )
    actual = conn.execute(
        "SELECT provider_id,independence_family_id,"
        "expected_member_count FROM calibration_native_ensemble_system "
        "WHERE ensemble_system_id=?",
        (SYSTEM_ID,),
    ).fetchone()
    if (
        actual is None
        or str(actual[0]) != "ECMWF"
        or str(actual[1]) != "ECMWF"
        or int(actual[2]) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("ecmwf_archive_system_registration_invalid")
    return str(actual[0]), str(actual[1])


def ingest_ecmwf_station_day(
    conn: sqlite3.Connection,
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    identity = {
        key: value
        for key, value in capture.items()
        if key != "content_sha256"
    }
    members = capture.get("members")
    if (
        capture.get("schema") != SCHEMA
        or capture.get("source_class") != "OFFICIAL_NATIVE_ARCHIVE"
        or capture.get("member_availability_contract")
        != "OFFICIAL_OPEN_DATA_PERTURBED_1_TO_50_NO_CONTROL"
        or capture.get("training_only") is not True
        or capture.get("holdout_eligible") is not False
        or capture.get("content_sha256") != content_sha256(identity)
        or not isinstance(members, list)
        or len(members) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("ecmwf_archive_capture_invalid")
    provider_id, _family = _ensure_system(conn)
    # The capture states its own lead, cycle and run; the module constants are
    # only a fallback for archives written before leads other than d2 existed.
    # Reading the constants here would relabel a d1 capture as d2 at the moment
    # it lands, which is exactly where a mislabelling becomes permanent.
    capture_lead_days, capture_lead_bucket, capture_run_init, capture_run_cycle = (
        capture_lead_and_run(
            capture,
            supported_lead_days=CAPTURE_LEAD_DAYS,
            default_lead_days=DEFAULT_LEAD_DAYS,
            error_code="ecmwf_archive_lead_days_invalid",
        )
    )
    if not _capture_lead_allowed(capture_lead_days, capture_run_init.hour):
        raise ContractError("ecmwf_archive_lead_days_invalid")
    # Rebuilt from the run the capture carries rather than the lead's default
    # cycle, so a non-default cycle is not rejected for disagreeing with
    # itself; the lead/run agreement was already enforced above.
    capture_extraction_version = extraction_version_for_lead(
        capture_lead_days,
        capture_run_init.hour,
    )
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
            (
                str(actual[0]) != wanted[0]
                or str(actual[1]) != wanted[1]
                or abs(float(actual[2]) - wanted[2]) > 1e-9
            )
            for actual, wanted in zip(existing, expected)
        ):
            raise ContractError("ecmwf_archive_frozen_fact_conflict")
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
            "member_role": "PERTURBED",
            "raw_tmax_c": float(row["raw_tmax_c"]),
            "settlement_definition": "official_daily_tmax_c",
            "extraction_version": capture_extraction_version,
            "provider_id": provider_id,
            "transport_provider_id": "ECMWF_OPEN_DATA_S3",
            "native_run_init_utc": str(capture["run_init_utc"]),
            "offline_oos_eligible": 1,
            "archive_query_at_utc": str(
                capture["archive_query_at_utc"]
            ),
            "source_url": str(capture["source_prefix"]),
            "raw_object_ref": (
                "ecmwf_field_receipt_set_sha256:"
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
                    "ecmwf-history-member-" + row_sha[:28]
                ),
                **member_identity,
                "content_sha256": row_sha,
            },
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
        "bucket_intervals_json": json.dumps(
            capture["bucket_intervals"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "raw_bucket_probabilities_json": json.dumps(
            capture["raw_bucket_probabilities"],
            sort_keys=True,
            separators=(",", ":"),
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
            "historical_member_bucket_id": (
                "ecmwf-history-bucket-" + bucket_sha[:28]
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


def ingest_ecmwf_station_days(
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
        or archive.get("content_sha256") != content_sha256(identity)
        or not isinstance(captures, list)
        or len(captures) != int(archive.get("station_count") or 0)
    ):
        raise ContractError("ecmwf_archive_multistation_invalid")
    results = [
        ingest_ecmwf_station_day(conn, capture)
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
    }


__all__ = [
    "ARCHIVE_LEAD_BUCKET",
    "EXTRACTION_VERSION",
    "MULTISTATION_SCHEMA",
    "SCHEMA",
    "SYSTEM_ID",
    "capture_ecmwf_station_days",
    "ingest_ecmwf_station_days",
]
