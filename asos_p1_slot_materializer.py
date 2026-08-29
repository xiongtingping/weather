"""Materialize intra-hour ASOS carrier rows as SOFT_PATH evidence for P1.

Soft in, hard out (user ruling 2026-08-29).  Every qc-eligible MADIS
five-minute row of an OOS-ready station's open settlement day is written to
``reported_trajectory_observations`` (lane ``madis_hfmetar``, report
``ASOS_HF``, id prefix ``asos_soft_``) so the resident P1 ingress serves it
to the path state -- momentum, drawdown, the physical conditioner's view of
the day -- through the same loader and compiler as every AWC report.

What these rows are NOT: hard evidence.  Settlement follows the formal
METAR series (measured 2026-08-29: METAR day-max minus official settle =
0.00 mean, never past +0.4C, while the MADIS intra-hour max overshoots it
on 35.7% of days), so carrier rows never move the running high, never close
a packet, and never root an atomic plan.  The trajectory layer and the P1
compiler enforce that by lane; this module only delivers the evidence.

The earlier racing/slot-substitute behaviour (minting FORMAL ``asos_p1_``
rows when a METAR went missing) is retired by the same ruling -- a formal
substitute was a hard-bound claim from a reading settlement never sees.
Its identity-fact machinery stays in place and simply idles at zero rows.

Ex-ante station gates, both required before a single row is minted:

1. the frozen pre-observation schedule marks the station
   ``asos_p1_oos_ready``;
2. an ``OOS_READY`` exact-match adapter artifact exists for the station
   (``reported_trajectory_asos_p1_oos_artifacts``).

Temperatures are quantized to the METAR tenths-Celsius grid, matching the
formal rows the path state mixes them with.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from asos_one_minute_ingress import icao_to_iem_station
from station_p1_preobservation_schedule import load_schedule
from bayes_basket_target.asos_substitute_observation_contract import (
    SUBSTITUTE_ORIGINS,
)
from bayes_basket_target.settlement_time import settlement_day_window


SOFT_CARRIER_ID_PREFIX = "asos_soft_"
SOFT_CARRIER_TRANSPORT = "asos_p1_soft_path_carrier"
# One settlement day of the five-minute product is 288 rows; the bound is a
# runaway-ingest backstop, not a policy.
MAX_SOFT_ROWS_PER_SCOPE_PASS = 400


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _tenths(value: float) -> float:
    """Quantize to the METAR T-group grid (0.1 C, half-up)."""

    return float(
        Decimal(str(float(value))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    )


def _oos_ready_stations(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]).upper()
        for row in conn.execute(
            "SELECT DISTINCT station_id"
            " FROM reported_trajectory_asos_p1_oos_artifacts"
            " WHERE state='OOS_READY'"
        )
    }


def _open_scopes(
    conn: sqlite3.Connection,
    stations: list[str],
    now: datetime,
) -> list[tuple[str, str, str, datetime, datetime]]:
    placeholders = ",".join("?" for _ in stations)
    scopes: list[tuple[str, str, str, datetime, datetime]] = []
    for station_id, event_date, timezone_name in conn.execute(
        f"""SELECT DISTINCT station_id,event_date,settlement_timezone
              FROM reported_trajectory_market_registry
             WHERE source_lane='awc' AND source_eligible=1 AND market_active=1
               AND station_id IN ({placeholders})
             ORDER BY station_id,event_date""",
        stations,
    ):
        window = settlement_day_window(str(event_date), str(timezone_name))
        start = _parse(str(window["target_day_start_utc"]))
        end = _parse(str(window["target_day_end_utc"]))
        if start <= now <= end:
            scopes.append(
                (str(station_id), str(event_date), str(timezone_name), start, end)
            )
    return scopes


def materialize_scheduled_asos_p1_observations(
    conn: sqlite3.Connection,
    station_ids: list[str],
    *,
    now_utc: datetime | None = None,
    schedule: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ingest the open day's qc-eligible carrier rows as SOFT_PATH evidence."""

    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stations = sorted({
        str(value).strip().upper()
        for value in station_ids
        if icao_to_iem_station(str(value)) is not None
    })
    if not stations:
        return {
            "ok": True,
            "state": "no_asos_station",
            "inserted_observation_count": 0,
            "scopes": [],
            "errors": [],
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    contract = dict(schedule) if schedule is not None else load_schedule()
    schedule_ready = {
        str(row["station_id"]).upper()
        for row in contract["stations"]
        if row.get("asos_p1_oos_ready")
    }
    oos_ready = _oos_ready_stations(conn)
    inserted = 0
    scope_results: list[dict[str, Any]] = []
    errors: list[str] = []
    origin = SUBSTITUTE_ORIGINS["MADIS_HFMETAR"]
    for station, event_date, timezone_name, start, end in _open_scopes(
        conn, stations, now
    ):
        if station not in schedule_ready:
            continue
        if station not in oos_ready:
            scope_results.append({
                "station_id": station,
                "event_date": event_date,
                "state": "oos_artifact_missing",
                "inserted_count": 0,
            })
            continue
        try:
            existing = {
                str(row[0])
                for row in conn.execute(
                    """SELECT valid_at FROM reported_trajectory_observations
                        WHERE station_id=?
                          AND source_lane IN ('asos_1min','madis_hfmetar')
                          AND valid_at>=? AND valid_at<?""",
                    (station, _iso(start), _iso(end)),
                )
            }
            candidates = conn.execute(
                """SELECT o.observed_at_utc, o.temperature_c,
                          o.raw_message_sha256, j.receipt_sha256
                     FROM reported_trajectory_madis_hfmetar_observations o
                     JOIN reported_trajectory_madis_hfmetar_objects j
                       ON j.compressed_sha256=o.compressed_sha256
                    WHERE o.station_id=? AND o.qc_eligible=1
                      AND o.observed_at_utc>=? AND o.observed_at_utc<?
                    ORDER BY o.observed_at_utc, o.canonical_committed_at_utc
                    LIMIT ?""",
                (station, _iso(start), _iso(end), MAX_SOFT_ROWS_PER_SCOPE_PASS),
            ).fetchall()
            scope_inserted = 0
            committed = _iso(now)
            for observed_at, temperature_c, raw_sha, receipt_sha in candidates:
                if str(observed_at) in existing:
                    continue
                identity = {
                    "station_id": station,
                    "event_date_local": event_date,
                    "settlement_timezone": timezone_name,
                    "source_lane": "madis_hfmetar",
                    "report_type": "ASOS_HF",
                    "valid_at_utc": str(observed_at),
                    "reported_temperature_c": _tenths(float(temperature_c)),
                    "raw_sha256": str(raw_sha).lower(),
                    "evidence_role": "SOFT_PATH",
                }
                observation_id = SOFT_CARRIER_ID_PREFIX + hashlib.sha256(
                    json.dumps(identity, sort_keys=True).encode("utf-8")
                ).hexdigest()[:32]
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO reported_trajectory_observations(
                         observation_id,source_lane,station_id,report_type,
                         valid_at,reported_temperature_c,raw_sha256,raw_text,
                         first_seen_at,parsed_at,canonical_committed_at,
                         fetch_started_at,fetch_completed_at,source_poll_id,
                         correction_suspected
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        observation_id,
                        "madis_hfmetar",
                        station,
                        "ASOS_HF",
                        str(observed_at),
                        identity["reported_temperature_c"],
                        identity["raw_sha256"],
                        "",
                        committed,
                        committed,
                        committed,
                        committed,
                        committed,
                        str(receipt_sha).lower(),
                        0,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                existing.add(str(observed_at))
                conn.execute(
                    """INSERT OR IGNORE INTO
                         reported_trajectory_observation_origins(
                         observation_id,source_lane,station_id,
                         origin_source_id,origin_country_code,
                         origin_authority,origin_url,origin_transport,
                         direct_country_official,fallback_used,
                         fallback_reason,preferred_origin_source_id,
                         preferred_origin_authority,
                         official_source_registry_sha256,origin_json,
                         committed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        observation_id,
                        "madis_hfmetar",
                        station,
                        origin["origin_source_id"],
                        "US",
                        origin["origin_authority"],
                        origin["origin_url"],
                        SOFT_CARRIER_TRANSPORT,
                        0,
                        0,
                        "",
                        origin["origin_source_id"],
                        origin["origin_authority"],
                        "",
                        json.dumps(identity, sort_keys=True, separators=(",", ":")),
                        committed,
                    ),
                )
                scope_inserted += 1
            inserted += scope_inserted
            scope_results.append({
                "station_id": station,
                "event_date": event_date,
                "state": "materialized" if scope_inserted else "complete",
                "inserted_count": scope_inserted,
            })
        except Exception as exc:  # fail one scope, keep the rest
            errors.append(f"{station}|{event_date}:{type(exc).__name__}:{exc}")
    if inserted:
        conn.commit()
    return {
        "ok": not errors,
        "state": "materialized" if inserted else "complete",
        "inserted_observation_count": inserted,
        "scopes": scope_results,
        "errors": errors,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


__all__ = [
    "MAX_SOFT_ROWS_PER_SCOPE_PASS",
    "SOFT_CARRIER_ID_PREFIX",
    "SOFT_CARRIER_TRANSPORT",
    "materialize_scheduled_asos_p1_observations",
]
