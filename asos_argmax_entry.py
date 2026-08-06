"""Order-gateway-free ASOS/MADIS new-high entry fact materializer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from asos_argmax_window import LIVE_STATIONS, evaluate_observed_high_entry
from weather_basket_yes_runtime import (
    conservative_market_temperature,
    normalize_market_buckets,
)


SCHEMA = "asos_argmax_new_high_entry_fact_v1"
CANONICAL_TZ = "America/New_York"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc(value: str) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("entry_timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _event_bounds(event_date: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(CANONICAL_TZ)
    start = datetime.strptime(event_date, "%Y-%m-%d").replace(tzinfo=zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _madis_day_complete_through(
    conn: sqlite3.Connection, *, event_date: str, through_utc: datetime
) -> bool:
    start, end = _event_bounds(event_date)
    last_hour = min(through_utc, end - timedelta(microseconds=1)).replace(
        minute=0, second=0, microsecond=0
    )
    expected: set[str] = set()
    cursor = start
    while cursor <= last_hour:
        expected.add(_iso(cursor))
        cursor += timedelta(hours=1)
    actual = {
        str(row[0]) for row in conn.execute(
            """SELECT DISTINCT object_hour_utc
                 FROM reported_trajectory_madis_hfmetar_objects
                WHERE object_hour_utc>=? AND object_hour_utc<=?""",
            (_iso(start), _iso(last_hour)),
        )
    }
    return bool(expected) and expected.issubset(actual)


def _market_bucket(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, temperature_c: float
) -> dict[str, Any]:
    raw_rows = []
    seen = set()
    for row in conn.execute(
        """SELECT slug,outcome,yes_token,settlement_unit,registry_json
             FROM reported_trajectory_market_registry
            WHERE station_id=? AND event_date=? AND source_lane='awc'
              AND source_eligible=1 AND market_active=1 AND market_accepting_orders=1
            ORDER BY outcome""",
        (station_id, event_date),
    ):
        if str(row[1]) in seen:
            continue
        seen.add(str(row[1]))
        raw_rows.append({
            "slug": str(row[0]), "outcome": str(row[1]), "yes_token": str(row[2] or ""),
            "settlement_unit": str(row[3] or ""), "registry_json": str(row[4]),
        })
    buckets = normalize_market_buckets(raw_rows)
    unit = str(buckets[0]["unit"])
    scalar = conservative_market_temperature(temperature_c, unit)
    matches = []
    for bucket in buckets:
        lower, upper = bucket["lower"], bucket["upper"]
        if lower is not None and scalar < float(lower):
            continue
        if upper is not None and scalar > float(upper):
            continue
        matches.append(bucket)
    if len(matches) != 1 or not str(matches[0].get("yes_token") or ""):
        raise ValueError("observed_high_market_bucket_not_unique")
    return {**matches[0], "settlement_scalar": scalar, "settlement_unit": unit}


def _source_rows(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> tuple[str, list[dict[str, Any]]]:
    start, end = _event_bounds(event_date)
    # Exact one-minute ASOS wins only when its canonical coverage contract says
    # the elapsed event day is complete.  Otherwise the whole scope uses MADIS;
    # values are never mixed to manufacture a larger maximum.
    coverage = conn.execute(
        """SELECT coverage_complete FROM reported_trajectory_asos_station_day_coverage
            WHERE station_id=? AND event_date=? AND settlement_timezone=?""",
        (station_id, event_date, CANONICAL_TZ),
    ).fetchone()
    if coverage is not None and int(coverage[0] or 0) == 1:
        rows = [{
            "observation_id": str(row[0]), "observed_at_utc": str(row[1]),
            "temperature_c": float(row[2]), "raw_sha256": str(row[3]),
            "source_receipt_sha256": str(row[4]), "received_at_utc": str(row[5]),
        } for row in conn.execute(
            """SELECT observation_id,valid_at,temperature_c,raw_record_sha256,
                      source_receipt_sha256,canonical_committed_at
                 FROM reported_trajectory_asos_one_minute_observations
                WHERE station_id=? AND valid_at>=? AND valid_at<? ORDER BY valid_at""",
            (station_id, _iso(start), _iso(end)),
        )]
        return "asos_1min", rows
    rows = [{
        "observation_id": str(row[0]), "observed_at_utc": str(row[1]),
        "temperature_c": float(row[2]), "raw_sha256": str(row[3]),
        "source_receipt_sha256": str(row[4]), "received_at_utc": str(row[5]),
    } for row in conn.execute(
        """SELECT o.content_sha256,o.observed_at_utc,o.temperature_c,
                  o.raw_message_sha256,x.receipt_sha256,o.canonical_committed_at_utc
             FROM reported_trajectory_madis_hfmetar_observations o
             JOIN reported_trajectory_madis_hfmetar_objects x
               ON x.compressed_sha256=o.compressed_sha256
            WHERE o.station_id=? AND o.qc_eligible=1
              AND o.observed_at_utc>=? AND o.observed_at_utc<?
            ORDER BY o.observed_at_utc,o.content_sha256""",
        (station_id, _iso(start), _iso(end)),
    )]
    return "madis_hfmetar", rows


def materialize_argmax_entry_facts(
    conn: sqlite3.Connection,
    station_ids: Sequence[str],
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Arm complete scopes and append only future-arriving strict new highs."""

    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    event_date = now.astimezone(ZoneInfo(CANONICAL_TZ)).date().isoformat()
    inserted = 0
    scopes = []
    for station in sorted({str(x).upper() for x in station_ids}.intersection(LIVE_STATIONS)):
        window_row = conn.execute(
            """SELECT window_id,argmax_window_start_utc,argmax_window_end_utc,state,
                      argmax_periods_json
                 FROM reported_trajectory_asos_argmax_windows
                WHERE station_id=? AND event_date=?""",
            (station, event_date),
        ).fetchone()
        if window_row is None:
            scopes.append({"station_id": station, "state": "blocked", "reason": "ARGMAX_WINDOW_MISSING"})
            continue
        try:
            argmax_periods = json.loads(str(window_row[4]))
        except (TypeError, ValueError, json.JSONDecodeError):
            argmax_periods = None
        if not isinstance(argmax_periods, list) or not argmax_periods:
            scopes.append({
                "station_id": station, "state": "blocked",
                "reason": "ARGMAX_PERIODS_INVALID",
            })
            continue
        source_lane, rows = _source_rows(conn, station_id=station, event_date=event_date)
        if source_lane == "madis_hfmetar" and not _madis_day_complete_through(
            conn, event_date=event_date, through_utc=now
        ):
            scopes.append({"station_id": station, "state": "blocked", "reason": "MADIS_EVENT_DAY_GAP"})
            continue
        if not rows:
            scopes.append({"station_id": station, "state": "blocked", "reason": "ASOS_MADIS_OBSERVATION_MISSING"})
            continue
        state = conn.execute(
            """SELECT source_lane,armed_at_utc,running_high_c,last_observed_at_utc
                 FROM reported_trajectory_asos_argmax_lane_state
                WHERE station_id=? AND event_date=?""",
            (station, event_date),
        ).fetchone()
        if state is None or str(state[0]) != source_lane:
            running_high = max(float(row["temperature_c"]) for row in rows)
            last_observed = max(str(row["observed_at_utc"]) for row in rows)
            conn.execute(
                """INSERT INTO reported_trajectory_asos_argmax_lane_state(
                     station_id,event_date,source_lane,armed_at_utc,running_high_c,
                     last_observed_at_utc,last_block_reason,updated_at_utc
                   ) VALUES(?,?,?,?,?,?,NULL,?)
                   ON CONFLICT(station_id,event_date) DO UPDATE SET
                     source_lane=excluded.source_lane,armed_at_utc=excluded.armed_at_utc,
                     running_high_c=excluded.running_high_c,
                     last_observed_at_utc=excluded.last_observed_at_utc,
                     last_block_reason=NULL,updated_at_utc=excluded.updated_at_utc""",
                (station, event_date, source_lane, _iso(now), running_high, last_observed, _iso(now)),
            )
            conn.commit()
            scopes.append({"station_id": station, "state": "armed", "source_lane": source_lane})
            continue
        running_high = float(state[2])
        last_observed = str(state[3])
        block_reason = None
        for row in rows:
            if str(row["observed_at_utc"]) <= last_observed:
                continue
            last_observed = str(row["observed_at_utc"])
            if float(row["temperature_c"]) <= running_high:
                continue
            prior_high = running_high
            running_high = float(row["temperature_c"])
            eligibility = evaluate_observed_high_entry(
                {
                    "state": str(window_row[3]),
                    "argmax_window_start_utc": str(window_row[1]),
                    "argmax_window_end_utc": str(window_row[2]),
                    "argmax_periods": argmax_periods,
                },
                observation_valid_at_utc=str(row["observed_at_utc"]),
                observation_received_at_utc=str(row["received_at_utc"]),
                forms_new_station_day_high=True,
            )
            if not eligibility["eligible"]:
                block_reason = str(eligibility["reason"])
                continue
            try:
                bucket = _market_bucket(
                    conn, station_id=station, event_date=event_date,
                    temperature_c=running_high,
                )
            except Exception as exc:
                block_reason = f"{type(exc).__name__}:{exc}"
                continue
            fact = {
                "schema": SCHEMA, "station_id": station, "event_date": event_date,
                "source_lane": source_lane, "observation_id": row["observation_id"],
                "observed_at_utc": row["observed_at_utc"],
                "received_at_utc": row["received_at_utc"],
                "prior_high_c": prior_high, "new_high_c": running_high,
                "raw_sha256": row["raw_sha256"],
                "source_receipt_sha256": row["source_receipt_sha256"],
                "window_id": str(window_row[0]),
                "argmax_window_start_utc": eligibility["argmax_period_start_utc"],
                "market_slug": bucket["slug"], "outcome": bucket["outcome"],
                "yes_token": bucket["yes_token"],
                "settlement_scalar": bucket["settlement_scalar"],
                "settlement_unit": bucket["settlement_unit"],
                "reason": eligibility["reason"], "gateway_submitted": False,
            }
            content_sha = _sha(fact)
            cursor = conn.execute(
                """INSERT OR IGNORE INTO reported_trajectory_asos_argmax_entry_facts(
                     entry_fact_id,station_id,event_date,source_lane,observation_id,
                     observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
                     source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
                     outcome,yes_token,settlement_scalar,settlement_unit,reason,
                     content_sha256,gateway_submitted,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (
                    f"asosentry_{content_sha[:32]}", station, event_date, source_lane,
                    row["observation_id"], row["observed_at_utc"], row["received_at_utc"],
                    prior_high, running_high, row["raw_sha256"],
                    row["source_receipt_sha256"], window_row[0],
                    eligibility["argmax_period_start_utc"],
                    bucket["slug"], bucket["outcome"], bucket["yes_token"],
                    bucket["settlement_scalar"], bucket["settlement_unit"],
                    eligibility["reason"], content_sha, _iso(now),
                ),
            )
            inserted += int(cursor.rowcount or 0)
        conn.execute(
            """UPDATE reported_trajectory_asos_argmax_lane_state
                  SET running_high_c=?,last_observed_at_utc=?,last_block_reason=?,updated_at_utc=?
                WHERE station_id=? AND event_date=?""",
            (running_high, last_observed, block_reason, _iso(now), station, event_date),
        )
        conn.commit()
        scopes.append({
            "station_id": station, "state": "ready", "source_lane": source_lane,
            "running_high_c": running_high, "last_block_reason": block_reason,
        })
    return {
        "ok": True, "state": "materialized", "event_date": event_date,
        "inserted_entry_fact_count": inserted, "scopes": scopes,
        "calls_order_gateway": False, "orders_submitted": 0,
    }
