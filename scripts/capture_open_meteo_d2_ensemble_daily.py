#!/usr/bin/env python3
"""Capture d2-lead ensemble members from Open-Meteo, once per day.

Three independence families -- UKMO, GOOGLE_WEATHERNEXT and NOAA_AIGEFS -- have
no official native archive at all, so this republisher is their only route into
the candidate set.  Two of them are AI models rather than physical ones, which
is what makes them worth the trouble: the three families already in production
(ECMWF/GEFS/ECCC) are all traditional NWP and their errors are correlated.

d2 is taken forward, never backward.  Open-Meteo retains ensemble members for
only about five days, and what a backward query returns for a past date is the
*current* run's hindcast rather than the d2 forecast that was actually issued
then -- right date, wrong lead.  Asking for ``forecast_days=3`` and keeping the
third day is the only way to obtain a genuine d2 member set, so history cannot
be backfilled and the archive can only grow from the day capture starts.

Written to the republisher tables and never to the official-native ones: these
values pass through an aggregator, and the distinction has to survive into any
later weighting decision.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)

BASE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
TRANSPORT_PROVIDER_ID = "OPEN_METEO"
SOURCE_ROLE = "aggregator_republisher"
ARCHIVE_LEAD_BUCKET = "d2"
LEAD_DAYS = 2
EXTRACTION_VERSION = "open_meteo_forward_d2_daily_tmax_member_v1"
# Open-Meteo reports no run initialisation time, so the cycle cannot be named.
RUN_CYCLE = "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 4
RETRY_SLEEP_SECONDS = 3.0
# Minimum gap between requests.  This pass is 45 stations x 3 models = 135
# separate calls issued back to back, and the provider prices per model-day, so
# an unpaced sweep arrives as one burst against a per-minute allowance.  The
# resulting 429s were being retried rather than avoided, which spends the quota
# that is refusing us.
#
# 0.25s spreads the sweep over ~34 seconds -- well inside the 15 minute cadence
# this pass runs at, so it costs nothing in practice while removing the burst.
MIN_REQUEST_INTERVAL_SECONDS = 0.25
# Retrying a rate limit sooner than the server said only earns another refusal.
MAX_RETRY_SLEEP_SECONDS = 30.0

# Open-Meteo model id -> (system id, independence family, expected members).
# Member counts are the published ensemble size including control; the API
# returns control as the unsuffixed field plus ``memberNN`` for perturbations.
TARGET_MODELS: dict[str, tuple[str, str, int]] = {
    "ukmo_global_ensemble_20km": ("UKMO_GLOBAL_ENS", "UKMO", 18),
    "google_weathernext2_ensemble": (
        "GOOGLE_WEATHERNEXT2_ENS", "GOOGLE_WEATHERNEXT", 64,
    ),
    "ncep_aigefs025": ("NOAA_AIGEFS_025", "NOAA_AIGEFS", 31),
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _proxy() -> str | None:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return None


_LAST_REQUEST_MONOTONIC: float = 0.0


def _pace_request() -> None:
    """Hold each request at least ``MIN_REQUEST_INTERVAL_SECONDS`` apart.

    Paced before the call rather than after: the point is to shape what this
    process sends, and a sweep that only slows down once it has already been
    refused has already spent the quota.
    """

    global _LAST_REQUEST_MONOTONIC
    elapsed = time.monotonic() - _LAST_REQUEST_MONOTONIC
    if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
    _LAST_REQUEST_MONOTONIC = time.monotonic()


def _retry_sleep_seconds(exc: Exception, *, attempt: int) -> float:
    """Prefer the provider's own Retry-After over a guess."""

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429 and response is not None:
        raw = str(response.headers.get("Retry-After") or "").strip()
        if raw:
            try:
                return max(0.0, min(float(raw), MAX_RETRY_SLEEP_SECONDS))
            except ValueError:
                pass
        # A rate limit needs materially longer than a socket retry.
        return min(
            RETRY_SLEEP_SECONDS * (2 ** (attempt + 1)), MAX_RETRY_SLEEP_SECONDS
        )
    return min(RETRY_SLEEP_SECONDS * (attempt + 1), MAX_RETRY_SLEEP_SECONDS)


def _fetch(client: httpx.Client, url: str) -> tuple[bytes, str]:
    """One bounded GET.  Returns the raw body and its digest.

    httpx rather than urllib, matching the four native collectors.  urllib on
    this host cannot verify TLS -- the python.org build ships no CA bundle --
    so every request failed with a bare URLError that looked like a proxy fault.
    """

    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        _pace_request()
        try:
            response = client.get(url)
            response.raise_for_status()
            payload = response.content
            if len(payload) > MAX_RESPONSE_BYTES:
                raise RuntimeError("open_meteo_d2_response_too_large")
            return payload, content_sha256(payload.decode("utf-8"))
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(_retry_sleep_seconds(exc, attempt=attempt))
    # Say which limit was reached, not merely that the transport failed.
    # Reducing model count, slowing the sweep and raising the quota are three
    # different fixes, and the provider names the window it refused on.
    detail = ""
    response = getattr(last, "response", None)
    if response is not None:
        try:
            decoded = json.loads(
                (response.content or b"").decode("utf-8", errors="replace")
            )
            detail = str(decoded.get("reason") or "")[:200]
        except Exception:
            detail = ""
        status = getattr(response, "status_code", None)
        raise RuntimeError(
            f"open_meteo_d2_http_{status}"
            + (f":{detail}" if detail else "")
        ) from last
    raise RuntimeError(
        f"open_meteo_d2_transport_failed:{type(last).__name__}"
    ) from last


def _members_for_target(
    payload: dict[str, object], *, target_date: str
) -> list[tuple[str, str, float]]:
    """Extract (member_id, role, tmax_c) for exactly the target local date."""

    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise RuntimeError("open_meteo_d2_daily_block_missing")
    times = daily.get("time")
    if not isinstance(times, list) or target_date not in times:
        raise RuntimeError("open_meteo_d2_target_date_absent")
    index = times.index(target_date)

    members: list[tuple[str, str, float]] = []
    control = daily.get("temperature_2m_max")
    if isinstance(control, list) and control[index] is not None:
        members.append(("control", "CONTROL", float(control[index])))
    for key in sorted(daily):
        if not key.startswith("temperature_2m_max_member"):
            continue
        values = daily[key]
        if not isinstance(values, list) or values[index] is None:
            continue
        members.append((
            key.replace("temperature_2m_max_", ""),
            "PERTURBED",
            float(values[index]),
        ))
    return members


def _local_target_date(settlement_timezone: str) -> str:
    """The d2 target date in the station's own settlement timezone.

    Deriving it in UTC is wrong for stations west of the meridian: at 06:00 UTC
    New York is still on the previous calendar day, so ``forecast_days=3`` from
    its local perspective ends one day short of the UTC-derived target and the
    capture fails with the date simply absent.
    """

    local_today = datetime.now(ZoneInfo(settlement_timezone)).date()
    return (local_today + timedelta(days=LEAD_DAYS)).isoformat()


def _capture_station_model(
    client: httpx.Client,
    *,
    station_id: str,
    latitude: float,
    longitude: float,
    settlement_timezone: str,
    model: str,
    target_date: str,
) -> dict[str, object]:
    query = urllib.parse.urlencode({
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "daily": "temperature_2m_max",
        "models": model,
        "forecast_days": LEAD_DAYS + 1,
        "timezone": settlement_timezone,
    })
    url = f"{BASE_URL}?{query}"
    raw, digest = _fetch(client, url)
    payload = json.loads(raw.decode("utf-8"))
    if "reason" in payload:
        raise RuntimeError(f"open_meteo_d2_api_error:{payload['reason'][:80]}")

    system_id, family_id, expected = TARGET_MODELS[model]
    members = _members_for_target(payload, target_date=target_date)
    if not members:
        raise RuntimeError("open_meteo_d2_no_members")
    return {
        "station_id": station_id,
        "target_local_date": target_date,
        "settlement_timezone": settlement_timezone,
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "open_meteo_model": model,
        "expected_member_count": expected,
        "member_count": len(members),
        "members": [
            {"member_id": mid, "member_role": role, "raw_tmax_c": value}
            for mid, role, value in members
        ],
        "source_url": url,
        "source_object_sha256": digest,
        "raw_object_ref": f"open_meteo_response_sha256:{digest}",
    }


def _ingest(
    conn: sqlite3.Connection, report: dict[str, object]
) -> dict[str, object]:
    """Append captured members; existing rows are left untouched.

    Rows land ineligible for OOS.  Admitting them requires a separately
    versioned historical contract bound to a closed station day and an AWC
    truth receipt, which cannot exist until the target date has actually
    settled -- so eligibility is a later, evidence-backed decision rather than
    something the capture can grant itself.
    """

    captured_at = str(report["captured_at_utc"])
    wrote = 0
    for capture in report["captures"]:  # type: ignore[index]
        station = str(capture["station_id"])
        system = str(capture["ensemble_system_id"])
        target = str(capture["target_local_date"])
        digest = str(capture["source_object_sha256"])
        for member in capture["members"]:  # type: ignore[index]
            identity = {
                "ensemble_system_id": system,
                "station_id": station,
                "target_local_date": target,
                "archive_lead_bucket": ARCHIVE_LEAD_BUCKET,
                "member_id": str(member["member_id"]),
                "extraction_version": EXTRACTION_VERSION,
                "source_object_sha256": digest,
                "raw_tmax_c": float(member["raw_tmax_c"]),
            }
            row_sha = content_sha256(identity)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO "
                "calibration_republisher_ensemble_historical_member_run("
                "historical_member_run_id,ensemble_system_id,station_id,"
                "target_local_date,archive_lead_bucket,run_cycle,member_id,"
                "member_role,raw_tmax_c,settlement_definition,"
                "extraction_version,provider_id,transport_provider_id,"
                "native_run_init_utc,offline_oos_eligible,archive_query_at_utc,"
                "source_url,raw_object_ref,source_object_sha256,qc_status,"
                "authority_scope,holdout_eligible,content_sha256,"
                "historical_oos_contract_id,historical_oos_eligible) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,?,?,?,?,"
                "'ACCEPTED','CALIBRATION_TRAIN_ONLY',0,?,NULL,0)",
                (
                    f"om-d2-{row_sha[:44]}", system, station, target,
                    ARCHIVE_LEAD_BUCKET, RUN_CYCLE,
                    str(member["member_id"]), str(member["member_role"]),
                    float(member["raw_tmax_c"]), "official_daily_tmax_c",
                    EXTRACTION_VERSION,
                    str(capture["independence_family_id"]),
                    TRANSPORT_PROVIDER_ID, captured_at,
                    str(capture["source_url"]),
                    str(capture["raw_object_ref"]), digest, row_sha,
                ),
            )
            wrote += int(cursor.rowcount or 0)
    return {"state": "COMMITTED", "member_run_wrote_count": wrote}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--target-date",
        help="Local event date to capture; defaults to today + 2 days UTC.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--station-id", action="append")
    args = parser.parse_args()

    target_date = args.target_date or (
        datetime.now(timezone.utc).date() + timedelta(days=LEAD_DAYS)
    ).isoformat()
    date.fromisoformat(target_date)

    database = args.db.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT DISTINCT station_id,settlement_timezone "
            "FROM basket_target_market_registry "
            "WHERE source_eligible=1 AND station_id IS NOT NULL "
            "  AND settlement_timezone IS NOT NULL "
            "ORDER BY station_id"
        ).fetchall()
    wanted = {s.strip().upper() for s in (args.station_id or [])}
    scopes = [
        (str(station), str(zone))
        for station, zone in rows
        if not wanted or str(station).strip().upper() in wanted
    ]
    if not scopes:
        raise SystemExit("open_meteo_d2_no_station_scope")

    captured_at = _utc_now()
    captures: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        proxy=_proxy(),
    ) as client:
      for station, zone in scopes:
        # The scope is the market registry; the model scope is the coordinate
        # registry, and listings run ahead of it.  A station the coordinate
        # registry does not carry is out of scope for this capture, not a
        # reason to abandon the pass: raising here cost UKMO, WeatherNext and
        # AI-GEFS every day from 2026-09-03 on, and forward-only members are
        # gone once the provider's five-day window closes.
        try:
            coordinates = station_coordinates(station)
        except Exception as exc:  # noqa: BLE001 - report, never abort
            failures.append({
                "station_id": station,
                "model": "*",
                "error": f"{type(exc).__name__}:{str(exc)[:120]}",
            })
            continue
        station_target = args.target_date or _local_target_date(zone)
        for model in sorted(TARGET_MODELS):
            try:
                captures.append(_capture_station_model(
                    client,
                    station_id=station,
                    latitude=float(coordinates["latitude"]),
                    longitude=float(coordinates["longitude"]),
                    settlement_timezone=zone,
                    model=model,
                    target_date=station_target,
                ))
            except Exception as exc:  # noqa: BLE001 - report, never abort
                failures.append({
                    "station_id": station,
                    "model": model,
                    "error": f"{type(exc).__name__}:{str(exc)[:120]}",
                })

    identity = {
        "schema": "open_meteo_forward_d2_ensemble_members_v1",
        "target_local_date": target_date,
        "archive_lead_bucket": ARCHIVE_LEAD_BUCKET,
        "run_cycle": RUN_CYCLE,
        "transport_provider_id": TRANSPORT_PROVIDER_ID,
        "source_role": SOURCE_ROLE,
        "extraction_version": EXTRACTION_VERSION,
        "captured_at_utc": captured_at,
        "station_count": len({c["station_id"] for c in captures}),
        "model_count": len({c["open_meteo_model"] for c in captures}),
        "capture_count": len(captures),
        "failure_count": len(failures),
        "failures": failures,
        "captures": captures,
        # This archive is republisher evidence.  It must not be promoted into
        # the official-native scope, and it never places an order.
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "official_native": False,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    report = {**identity, "content_sha256": content_sha256(identity)}

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    ingress = None
    if args.commit:
        with sqlite3.connect(database, timeout=120.0) as write_conn:
            write_conn.execute("PRAGMA busy_timeout=120000")
            write_conn.execute("BEGIN IMMEDIATE")
            try:
                ingress = _ingest(write_conn, report)
                write_conn.commit()
            except Exception:
                write_conn.rollback()
                raise

    summary = {
        "ingress": ingress,
        "target_local_date": target_date,
        "stations": identity["station_count"],
        "models": identity["model_count"],
        "captures": identity["capture_count"],
        "failures": identity["failure_count"],
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
