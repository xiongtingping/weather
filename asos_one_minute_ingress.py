"""Bounded ASOS one-minute ingress and canonical source arbitration.

Iowa Environmental Mesonet republishes NOAA/NCEI ASOS one-minute records.
The republisher lineage is explicit; these rows are never labelled native or
METAR.  A station-day may outrank AWC only after its one-minute coverage gate
is complete.  Otherwise the existing AWC station-day bound remains selected.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from weather_basket_yes_runtime import identity


ENDPOINT = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
PROVIDER_ID = "IEM_ASOS1MIN"
SOURCE_LANE = "asos_1min"
SOURCE_CLASS = "aggregator_republisher"
UPSTREAM_AUTHORITY = "NOAA/NCEI ASOS"
MEASUREMENT_SEMANTICS = "ASOS_5_MINUTE_TEMPERATURE_PRODUCT_PUBLISHED_EACH_MINUTE_TMPF"
PRIORITY_CONTRACT_VERSION = "surface_observation_priority_v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ROWS = 50_000
MAX_INTERNAL_GAP_SECONDS = 5 * 60
MAX_EDGE_GAP_SECONDS = 10 * 60
MIN_COVERAGE_RATIO = 0.90


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("asos_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bounded(response: Any, *, deadline: float | None = None) -> bytes:
    """The body under the size bound and, when given, a wall-clock deadline:
    the socket timeout bounds one read, and a body trickling in under it
    never ends (2026-10-20, the AWC/MADIS lanes held 05:31-07:28Z)."""

    declared = int(str(response.headers.get("Content-Length") or "0"))
    if declared > MAX_RESPONSE_BYTES:
        raise RuntimeError("asos_response_too_large")
    chunks: list[bytes] = []
    total = 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("asos_fetch_deadline_exceeded")
        chunk = response.read(min(64 * 1024, MAX_RESPONSE_BYTES - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise RuntimeError("asos_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def icao_to_iem_station(station_id: str) -> str | None:
    """Return the unambiguous contiguous-US ASOS identifier."""

    station = str(station_id or "").strip().upper()
    if len(station) == 4 and station.startswith("K") and station[1:].isalnum():
        return station[1:]
    return None


def decode_asos_one_minute_temperature_payload(
    payload: bytes,
    station_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Decode retained NCEI/IEM CSV without performing another download."""

    mapping = {
        provider: station
        for station in sorted({str(x).strip().upper() for x in station_ids})
        if (provider := icao_to_iem_station(station)) is not None
    }
    reports: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8", "strict")))
    for row_no, row in enumerate(reader, start=1):
        if row_no > MAX_ROWS:
            raise RuntimeError("asos_row_limit_exceeded")
        provider_station = str(row.get("station") or "").strip().upper()
        station = mapping.get(provider_station)
        raw_tmpf = str(row.get("tmpf") or "").strip()
        raw_valid = str(row.get("valid(UTC)") or row.get("valid") or "").strip()
        if station is None or not raw_tmpf or raw_tmpf.upper() in {"M", "NULL", "NONE"}:
            continue
        try:
            tmpf = float(raw_tmpf)
            valid = datetime.strptime(raw_valid, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            continue
        tmpc = (tmpf - 32.0) * 5.0 / 9.0
        if not math.isfinite(tmpc) or not -100.0 <= tmpc <= 70.0:
            continue
        record = f"{provider_station},{raw_valid},{raw_tmpf}".encode()
        reports.append({
            "station_id": station,
            "provider_station_id": provider_station,
            "valid_at": _stamp(valid),
            "temperature_c": tmpc,
            "provider_temperature_f": tmpf,
            "raw_record_sha256": _sha(record),
        })
    return reports


def fetch_asos_one_minute_temperature(
    station_ids: Sequence[str],
    *,
    start_utc: datetime,
    end_utc: datetime,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Fetch a bounded CSV window with explicit IEM/NCEI lineage."""

    mapping = {
        provider: station
        for station in sorted({str(x).strip().upper() for x in station_ids})
        if (provider := icao_to_iem_station(station)) is not None
    }
    start = start_utc.astimezone(timezone.utc)
    end = end_utc.astimezone(timezone.utc)
    if not mapping:
        return {"reports": [], "station_ids": [], "state": "no_asos_station"}
    if end <= start or end - start > timedelta(hours=30):
        raise ValueError("asos_fetch_window_invalid")
    params: list[tuple[str, str]] = [("station", key) for key in sorted(mapping)]
    params.extend([
        ("year1", str(start.year)), ("month1", str(start.month)),
        ("day1", str(start.day)), ("hour1", str(start.hour)),
        ("minute1", str(start.minute)), ("year2", str(end.year)),
        ("month2", str(end.month)), ("day2", str(end.day)),
        ("hour2", str(end.hour)), ("minute2", str(end.minute)),
        ("tz", "UTC"), ("vars", "tmpf"), ("what", "download"),
        ("delim", "comma"), ("gis", "no"),
    ])
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/csv", "User-Agent": "weather-asos-one-minute/1.0"},
    )
    started = datetime.now(timezone.utc)
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            payload = _read_bounded(
                response, deadline=time.monotonic() + max(1.0, float(timeout)),
            )
            status = int(response.status)
    except Exception:
        seconds = max(1, int(math.ceil(float(timeout))))
        result = subprocess.run(
            [
                "/usr/bin/curl", "-fsSL", "--compressed", "--proto", "=https",
                "--tlsv1.2", "--connect-timeout", str(min(5, seconds)),
                "--max-time", str(seconds), "--retry", "0", "--max-filesize",
                str(MAX_RESPONSE_BYTES), "-H", "Accept: text/csv", "-H",
                "User-Agent: weather-asos-one-minute/1.0", url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=seconds + 2,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > MAX_RESPONSE_BYTES:
            raise RuntimeError(f"asos_transport_failed:curl_exit_{result.returncode}")
        payload = bytes(result.stdout)
        status = 200
    completed = datetime.now(timezone.utc)
    receipt_identity = {
        "provider_id": PROVIDER_ID,
        "source_class": SOURCE_CLASS,
        "upstream_authority": UPSTREAM_AUTHORITY,
        "url": url,
        "response_sha256": _sha(payload),
        "fetch_started_at": _stamp(started),
        "fetch_completed_at": _stamp(completed),
        "status": status,
    }
    receipt_sha = _sha(json.dumps(receipt_identity, sort_keys=True).encode())
    reports = decode_asos_one_minute_temperature_payload(payload, mapping.values())
    return {
        "source_lane": SOURCE_LANE,
        "provider_id": PROVIDER_ID,
        "source_class": SOURCE_CLASS,
        "upstream_authority": UPSTREAM_AUTHORITY,
        "origin_url": url,
        "source_receipt_sha256": receipt_sha,
        "fetch_started_at": _stamp(started),
        "fetch_completed_at": _stamp(completed),
        "station_ids": sorted(mapping.values()),
        "reports": reports,
        "payload": payload,
        "state": "available" if reports else "data_missing",
    }


class AsosOneMinuteIngress:
    """Caller-owned-transaction ASOS writer and source-priority selector."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def ingest(self, poll: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "source_lane": SOURCE_LANE,
            "provider_id": PROVIDER_ID,
            "source_class": SOURCE_CLASS,
            "upstream_authority": UPSTREAM_AUTHORITY,
        }
        if any(poll.get(key) != value for key, value in required.items()):
            raise ValueError("asos_lineage_invalid")
        receipt = str(poll.get("source_receipt_sha256") or "")
        if len(receipt) != 64:
            raise ValueError("asos_source_receipt_invalid")
        inserted = 0
        now = _stamp(datetime.now(timezone.utc))
        for raw in poll.get("reports") or ():
            station = str(raw["station_id"]).upper()
            valid_at = _stamp(_utc(str(raw["valid_at"])))
            observation_id = identity(
                "asos1m_", (station, valid_at, str(raw["raw_record_sha256"]))
            )
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO reported_trajectory_asos_one_minute_observations(
                     observation_id,source_lane,station_id,provider_station_id,valid_at,
                     temperature_c,provider_temperature_f,measurement_semantics,provider_id,
                     source_class,upstream_authority,origin_url,raw_record_sha256,
                     source_receipt_sha256,fetch_started_at,fetch_completed_at,canonical_committed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id, SOURCE_LANE, station, raw["provider_station_id"], valid_at,
                    float(raw["temperature_c"]), float(raw["provider_temperature_f"]),
                    MEASUREMENT_SEMANTICS, PROVIDER_ID, SOURCE_CLASS, UPSTREAM_AUTHORITY,
                    str(poll["origin_url"]), raw["raw_record_sha256"], receipt,
                    poll["fetch_started_at"], poll["fetch_completed_at"], now,
                ),
            )
            inserted += int(cursor.rowcount == 1)
        return {"new_observation_count": inserted, "source_receipt_sha256": receipt}

    def refresh_station_day(
        self,
        *,
        station_id: str,
        event_date: str,
        settlement_timezone: str,
        as_of_utc: str,
        source_receipt_sha256: str,
    ) -> dict[str, Any]:
        zone = ZoneInfo(settlement_timezone)
        local_start = datetime.fromisoformat(event_date).replace(tzinfo=zone)
        start = local_start.astimezone(timezone.utc)
        end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
        as_of = min(_utc(as_of_utc), end)
        rows = self.conn.execute(
            """SELECT observation_id,valid_at,temperature_c
                 FROM reported_trajectory_asos_one_minute_observations
                WHERE station_id=? AND valid_at>=? AND valid_at<? AND valid_at<=?
                ORDER BY valid_at,observation_id""",
            (station_id.upper(), _stamp(start), _stamp(end), _stamp(as_of)),
        ).fetchall()
        unique: dict[str, tuple[str, str, float]] = {}
        for row in rows:
            unique[str(row[1])] = (str(row[0]), str(row[1]), float(row[2]))
        values = list(unique.values())
        expected = max(1, int((as_of - start).total_seconds() // 60) + 1)
        gaps = [
            int((_utc(values[index][1]) - _utc(values[index - 1][1])).total_seconds())
            for index in range(1, len(values))
        ]
        maximum_gap = max(gaps, default=None)
        edge_ok = bool(values) and (
            _utc(values[0][1]) - start
        ).total_seconds() <= MAX_EDGE_GAP_SECONDS and (
            as_of - _utc(values[-1][1])
        ).total_seconds() <= MAX_EDGE_GAP_SECONDS
        ratio_ok = len(values) / expected >= MIN_COVERAGE_RATIO
        gap_ok = maximum_gap is None or maximum_gap <= MAX_INTERNAL_GAP_SECONDS
        complete = edge_ok and ratio_ok and gap_ok
        maximum = max(values, key=lambda item: (item[2], item[1], item[0])) if values else None
        failure = None if complete else (
            "ASOS_DATA_MISSING" if not values else "ASOS_COVERAGE_INCOMPLETE"
        )
        now = _stamp(datetime.now(timezone.utc))
        self.conn.execute(
            """INSERT INTO reported_trajectory_asos_station_day_coverage(
                 station_id,event_date,settlement_timezone,day_start_utc,day_end_utc,
                 covered_through_utc,sample_count,expected_minute_count,maximum_gap_seconds,
                 coverage_complete,reported_max_c,max_observation_id,max_valid_at,failure_code,
                 source_receipt_sha256,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(station_id,event_date) DO UPDATE SET
                 settlement_timezone=excluded.settlement_timezone,
                 day_start_utc=excluded.day_start_utc,day_end_utc=excluded.day_end_utc,
                 covered_through_utc=excluded.covered_through_utc,sample_count=excluded.sample_count,
                 expected_minute_count=excluded.expected_minute_count,
                 maximum_gap_seconds=excluded.maximum_gap_seconds,
                 coverage_complete=excluded.coverage_complete,reported_max_c=excluded.reported_max_c,
                 max_observation_id=excluded.max_observation_id,max_valid_at=excluded.max_valid_at,
                 failure_code=excluded.failure_code,source_receipt_sha256=excluded.source_receipt_sha256,
                 updated_at=excluded.updated_at""",
            (
                station_id.upper(), event_date, settlement_timezone, _stamp(start), _stamp(end),
                _stamp(as_of), len(values), expected, maximum_gap, int(complete),
                maximum[2] if maximum else None, maximum[0] if maximum else None,
                maximum[1] if maximum else None, failure, source_receipt_sha256, now,
            ),
        )
        return self.select_canonical_bound(
            station_id=station_id, event_date=event_date, as_of_utc=_stamp(as_of)
        )

    def select_canonical_bound(
        self, *, station_id: str, event_date: str, as_of_utc: str | None = None
    ) -> dict[str, Any]:
        station = station_id.upper()
        asos = self.conn.execute(
            """SELECT settlement_timezone,reported_max_c,max_observation_id,max_valid_at,
                      source_receipt_sha256,covered_through_utc,day_end_utc
                 FROM reported_trajectory_asos_station_day_coverage
                WHERE station_id=? AND event_date=? AND coverage_complete=1""",
            (station, event_date),
        ).fetchone()
        asos_fresh = False
        if asos is not None:
            as_of = _utc(as_of_utc) if as_of_utc else datetime.now(timezone.utc)
            covered = _utc(str(asos[5]))
            end = _utc(str(asos[6]))
            required_through = min(as_of, end) - timedelta(seconds=MAX_EDGE_GAP_SECONDS)
            asos_fresh = covered >= required_through
        if asos is not None and asos_fresh:
            selected = (asos[0], SOURCE_LANE, 220, asos[1], asos[2], asos[3],
                        "ASOS_COMPLETE_OUTRANKS_AWC", asos[4])
        else:
            awc = self.conn.execute(
                """SELECT settlement_timezone,reported_max_c,max_observation_id,max_valid_at,
                          updated_at,bound_generation
                     FROM reported_trajectory_station_day_bounds
                    WHERE source_lane='awc' AND station_id=? AND event_date=?""",
                (station, event_date),
            ).fetchone()
            madis = None
            if awc is not None and self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='reported_trajectory_madis_hfmetar_observations'"
            ).fetchone():
                zone = ZoneInfo(str(awc[0]))
                local_start = datetime.fromisoformat(event_date).replace(tzinfo=zone)
                day_start = local_start.astimezone(timezone.utc)
                day_end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
                as_of = _utc(as_of_utc) if as_of_utc else datetime.now(timezone.utc)
                through = min(as_of, day_end)
                madis_rows = self.conn.execute(
                    """SELECT content_sha256,observed_at_utc,temperature_c,compressed_sha256
                         FROM reported_trajectory_madis_hfmetar_observations
                        WHERE station_id=? AND qc_eligible=1 AND observed_at_utc>=?
                          AND observed_at_utc<=? ORDER BY observed_at_utc,content_sha256""",
                    (station, _stamp(day_start), _stamp(through)),
                ).fetchall()
                unique = {str(row[1]): row for row in madis_rows}
                values = list(unique.values())
                expected = max(1, int((through - day_start).total_seconds() // 300) + 1)
                gaps = [
                    int((_utc(str(values[i][1])) - _utc(str(values[i - 1][1]))).total_seconds())
                    for i in range(1, len(values))
                ]
                complete = bool(values) and (
                    (_utc(str(values[0][1])) - day_start).total_seconds() <= 600
                    and (through - _utc(str(values[-1][1]))).total_seconds() <= 600
                    and (max(gaps, default=0) <= 600)
                    and len(values) / expected >= 0.90
                )
                if complete:
                    maximum = max(values, key=lambda row: (float(row[2]), str(row[1]), str(row[0])))
                    receipt = _sha(json.dumps([
                        str(row[0]) for row in values
                    ], sort_keys=True).encode())
                    madis = (
                        str(awc[0]), "madis_hfmetar", 210, float(maximum[2]),
                        str(maximum[0]), str(maximum[1]),
                        "MADIS_HFMETAR_COMPLETE_OUTRANKS_AWC", receipt,
                    )
            if madis is not None:
                selected = madis
            elif awc is None:
                return {"state": "blocked", "reason": "NO_COMPLETE_ASOS_OR_AWC_BOUND"}
            else:
                receipt = _sha(
                    json.dumps({"station": station, "event_date": event_date,
                                "updated_at": awc[4], "generation": awc[5]}, sort_keys=True).encode()
                )
                selected = (awc[0], "awc", 100, awc[1], awc[2], awc[3],
                            "ASOS_UNAVAILABLE_AWC_FALLBACK", receipt)
        now = _stamp(datetime.now(timezone.utc))
        self.conn.execute(
            """INSERT INTO reported_trajectory_canonical_station_day_bounds(
                 station_id,event_date,settlement_timezone,selected_source_lane,source_priority,
                 observed_max_c,max_observation_id,max_valid_at,selection_reason,
                 source_receipt_sha256,priority_contract_version,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(station_id,event_date) DO UPDATE SET
                 settlement_timezone=excluded.settlement_timezone,
                 selected_source_lane=excluded.selected_source_lane,
                 source_priority=excluded.source_priority,observed_max_c=excluded.observed_max_c,
                 max_observation_id=excluded.max_observation_id,max_valid_at=excluded.max_valid_at,
                 selection_reason=excluded.selection_reason,
                 source_receipt_sha256=excluded.source_receipt_sha256,
                 priority_contract_version=excluded.priority_contract_version,updated_at=excluded.updated_at""",
            (station, event_date, *selected, PRIORITY_CONTRACT_VERSION, now),
        )
        return {"state": "selected", "selected_source_lane": selected[1],
                "observed_max_c": float(selected[3]), "selection_reason": selected[6]}


__all__ = [
    "AsosOneMinuteIngress", "fetch_asos_one_minute_temperature",
    "icao_to_iem_station", "PRIORITY_CONTRACT_VERSION",
]
