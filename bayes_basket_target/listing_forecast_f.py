"""Charter 267 (三): the listing-time forecast F, computed before listing.

User ruling 2026-10-11 「挂牌前把f算好」.  The benchmark cluster's first trade on a
station-day comes minutes after Polymarket lists it (two days ahead, ~04:56Z
for the Asia/Europe/Africa batch), before the market has a mode, and its YES
sits at the bucket of a calibrated centre of the deterministic global models:
the raw median + a fleet-wide warm shift (the models' mean cold bias, +0.83 C
measured) + ~30% of the station's own correction (benchmark research rounds
32-35: forward exact 47% against 36% for a station-only correction).  Our own
P0 is not there at listing (first P0 a median 10 min to 3 h later, round 38)
and its mode is the weaker bucket for this play, so F is built separately,
from native deterministic runs only (``deterministic_forecast_family.
F_SYSTEM_IDS``: AIFS Single, IFS HRES, GFS, AIGFS, GDPS), for every roster
station and every event day from today to D+2, and kept ready:

  raw    = median over systems of each system's newest knowable run's
           local-day maximum (``feature_at``: covers the afternoon)
  c      = fleet mean of (truth - raw) over the trailing ``FLEET_DAYS`` days,
           every past day read as of its own listing instant (D-2 05:00Z);
           the research value ``RESEARCH_FLEET_C`` until ``MIN_FLEET_SAMPLES``
           station-days over ``MIN_FLEET_DAYS`` days exist
  corr_s = median(raw_m + b_sm) - raw, b_sm the station x system mean of
           (truth - raw_m) over the station's trailing days (as of d-2, >= 3)
  F      = raw + c + LAMBDA x corr_s

Read-only on the SSOT; writes one runtime file (``F_PATH``), never the SSOT.
F is an execution-layer number: it is not a probability, never enters P0,
P0-M, P1, a certificate or any formal authority.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .canonical_contract import FORMAL_TRUTH_LANES_SQL
from .deterministic_forecast_family import F_SYSTEM_IDS, feature_at, global_roster

ROOT = Path(__file__).resolve().parents[1]
F_PATH = Path(os.environ.get("PM_LISTING_FORECAST_F_PATH") or ROOT / "data" / "runtime" / "listing_forecast_f_v1.json")
SCHEMA = "listing_forecast_f_v1"
RULE = "deterministic_median_fleet_shift_station_share_v2"
# 补记四 (user 「改」 10-12): 0.3 -> 0.5.  Our values are nearest-land-cell
# grid values, not point values adjusted to the station's elevation, so a
# larger share of each station's own bias is needed: on the 440 research
# station-days, agreement with the cluster's first YES +7.3 points
# [+0.8, +13.7] (+7.1 / +7.3 in each half of the dates), hits unchanged.
LAMBDA = 0.5
RESEARCH_FLEET_C = 0.8
FLEET_DAYS = 30
STATION_DAYS = 60
MIN_FLEET_SAMPLES = 200
MIN_FLEET_DAYS = 14
MIN_STATION_SYSTEM_DAYS = 3
MIN_SYSTEMS = 3
LISTING_HOUR_UTC = 5
# Past days' listing-time maxima change only when the run table grows (a
# backfill can add a run that was knowable then); the cache is dropped
# whenever the table's newest rowid moves.
_HISTORY_CACHE: dict[tuple[str, str], dict[str, float] | None] = {}
_HISTORY_GENERATION: list[int | None] = [None]


def listing_instant(event_date: date) -> str:
    """The batch listing instant F is for: two days ahead at ~05:00Z."""

    stamp = datetime.combine(event_date - timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
    return (stamp + timedelta(hours=LISTING_HOUR_UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def system_maxima(
    conn: sqlite3.Connection, *, station_id: str, event_date: date, as_of_utc: str, settlement_timezone: str,
) -> dict[str, dict[str, Any]]:
    """Each F system's newest knowable, afternoon-covering run at ``as_of_utc``."""

    features = feature_at(
        conn, station_id=station_id, event_date=event_date.isoformat(), as_of_utc=as_of_utc,
        settlement_timezone=settlement_timezone, systems=sorted(F_SYSTEM_IDS),
    )
    return {
        system: {
            "tmax_c": round(float(feature["day_max_c"]), 3),
            "run_init_utc": str(feature["run_init_utc"]),
            "knowable_at_utc": str(feature["knowable_at_utc"]),
        }
        for system, feature in features.items()
    }


def formal_truths(conn: sqlite3.Connection, *, since: date, until: date) -> dict[tuple[str, str], float]:
    """Settled maxima, both formal lanes, latest revision (settlement source on a tie)."""

    best: dict[tuple[str, str], tuple[int, int, float]] = {}
    for station, day, value, lane, revision in conn.execute(
        "SELECT station_id, local_date, official_tmax_c, source_lane, revision_no"
        " FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND local_date BETWEEN ? AND ?"
        " AND official_tmax_c IS NOT NULL",
        (since.isoformat(), until.isoformat()),
    ):
        key = (str(station).upper(), str(day))
        rank = (int(revision or 0), 1 if lane == "SETTLEMENT_SOURCE" else 0, float(value))
        if key not in best or rank[:2] > best[key][:2]:
            best[key] = rank
    return {key: value[2] for key, value in best.items()}


def _history_point(
    conn: sqlite3.Connection, *, station_id: str, event_date: date, zone: str,
) -> dict[str, float] | None:
    key = (station_id, event_date.isoformat())
    if key not in _HISTORY_CACHE:
        maxima = system_maxima(
            conn, station_id=station_id, event_date=event_date,
            as_of_utc=listing_instant(event_date), settlement_timezone=zone,
        )
        _HISTORY_CACHE[key] = {system: entry["tmax_c"] for system, entry in maxima.items()} or None
    return _HISTORY_CACHE[key]


def compute(
    conn: sqlite3.Connection, *, now: datetime | None = None,
    roster: Sequence[tuple[str, float, float, str]] | None = None,
) -> dict[str, Any]:
    """F for every roster station x event day today..D+2 (UTC calendar)."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    as_of = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stations = list(roster if roster is not None else global_roster(conn))
    today = now.date()
    generation = conn.execute("SELECT max(rowid) FROM calibration_regional_point_run").fetchone()[0]
    if generation != _HISTORY_GENERATION[0]:
        _HISTORY_CACHE.clear()
        _HISTORY_GENERATION[0] = generation
    history_from = today - timedelta(days=max(FLEET_DAYS, STATION_DAYS) + 2)
    truths = formal_truths(conn, since=history_from, until=today)
    # History: each past settled day's listing-time maxima, per station.
    history: dict[str, list[tuple[date, dict[str, float], float]]] = {}
    for station, _lat, _lon, zone in stations:
        rows = []
        for (st, day), truth in truths.items():
            if st != station:
                continue
            past = date.fromisoformat(day)
            point = _history_point(conn, station_id=station, event_date=past, zone=zone)
            if point:
                rows.append((past, point, truth))
        history[station] = rows
    fleet_errors: list[tuple[date, float]] = []
    for rows in history.values():
        for past, point, truth in rows:
            if past >= today - timedelta(days=FLEET_DAYS) and len(point) >= MIN_SYSTEMS:
                fleet_errors.append((past, truth - statistics.median(point.values())))
    fleet_days = len({past for past, _err in fleet_errors})
    if len(fleet_errors) >= MIN_FLEET_SAMPLES and fleet_days >= MIN_FLEET_DAYS:
        fleet_c = statistics.mean(err for _past, err in fleet_errors)
        fleet_source = "native_history"
    else:
        fleet_c = RESEARCH_FLEET_C
        fleet_source = "research_fallback"
    out: dict[str, Any] = {}
    for station, _lat, _lon, zone in stations:
        for offset in (0, 1, 2):
            event_date = today + timedelta(days=offset)
            maxima = system_maxima(
                conn, station_id=station, event_date=event_date, as_of_utc=as_of, settlement_timezone=zone,
            )
            entry: dict[str, Any] = {"systems": maxima, "n_systems": len(maxima)}
            if len(maxima) < MIN_SYSTEMS:
                entry["state"] = "TOO_FEW_SYSTEMS"
                out[f"{station}|{event_date.isoformat()}"] = entry
                continue
            raw = statistics.median(v["tmax_c"] for v in maxima.values())
            cut = event_date - timedelta(days=2)
            corrected = []
            for system, value in maxima.items():
                errors = [
                    truth - point[system] for past, point, truth in history.get(station, [])
                    if system in point and past <= cut and past >= event_date - timedelta(days=STATION_DAYS)
                ]
                bias = statistics.mean(errors) if len(errors) >= MIN_STATION_SYSTEM_DAYS else 0.0
                corrected.append(value["tmax_c"] + bias)
                # Each member's own correction, for readers that look past the median.
                value["bias_c"] = round(bias, 3)
            corr = statistics.median(corrected) - raw
            entry.update({
                "state": "READY",
                "raw_median_c": round(raw, 3),
                "fleet_c": round(fleet_c, 3),
                "station_corr_c": round(corr, 3),
                "lambda": LAMBDA,
                "f_c": round(raw + fleet_c + LAMBDA * corr, 3),
                "listing_instant_utc": listing_instant(event_date),
            })
            out[f"{station}|{event_date.isoformat()}"] = entry
    return {
        "schema": SCHEMA, "rule": RULE, "computed_at_utc": as_of,
        "systems": sorted(F_SYSTEM_IDS),
        "fleet": {"c": round(fleet_c, 3), "source": fleet_source, "samples": len(fleet_errors), "days": fleet_days},
        "station_days": out,
        "authority": "EXECUTION_LAYER_ONLY",
        "calls_order_gateway": False,
    }


def write(payload: Mapping[str, Any], path: Path = F_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    os.replace(tmp, path)


def refresh(db_path: str | Path) -> dict[str, Any]:
    """The resident's ten-minute job: compute from the SSOT ``mode=ro``, write the file."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        conn.execute("PRAGMA query_only=1")
        payload = compute(conn)
    finally:
        conn.close()
    write(payload)
    ready = sum(1 for v in payload["station_days"].values() if v.get("state") == "READY")
    return {"state": "ok", "ready": ready, "station_days": len(payload["station_days"]),
            "fleet_c": payload["fleet"]["c"], "fleet_source": payload["fleet"]["source"]}


def read(path: Path = F_PATH) -> dict[str, Any] | None:
    """The file as written, or None (missing/unreadable)."""

    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def bucket_for(f_c: float, ladder: Sequence[Mapping[str, Any]]) -> str | None:
    """The ladder bucket (``lower_c``/``upper_c``, None = open) containing F."""

    for bucket in ladder:
        lower, upper = bucket.get("lower_c"), bucket.get("upper_c")
        if (lower is None or f_c >= float(lower)) and (upper is None or f_c < float(upper)):
            return str(bucket.get("label") or bucket.get("outcome") or "")
    return None


__all__ = [
    "F_PATH", "LAMBDA", "RESEARCH_FLEET_C", "RULE", "SCHEMA", "bucket_for", "compute",
    "formal_truths", "listing_instant", "read", "refresh", "system_maxima", "write",
]
