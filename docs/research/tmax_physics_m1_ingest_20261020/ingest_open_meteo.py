"""Open-Meteo history for the M1 training store, charged to the shared quota
ledger before every call (``open_meteo_quota.charge``; memory 10-11: research
must never spend the address's day).  Resumable via ``fetch_log``; one unit =
one task x one 14-day window (a call of up to 10 variables and 14 days
weighs one per location).

Tasks (all ``aggregator_republisher`` rows, research only):
  * ``prev1`` -- the board's day-ahead basis, ``_previous_day1`` hourly
    temperature, shortwave, dew point, humidity, wind, direction,
    precipitation, total cloud (the day-ahead low cloud is not archived,
    COVERAGE.md), 48 stations -> ``forecast_d1_hourly``.
  * ``sat`` -- measured shortwave on the board's own product per station
    (``station_day_board._satellite_product``: DWD SIS, EUMETSAT IODC,
    EUMETSAT MSG; JAXA stations via ``jma_jaxa_himawari``, checked equal to
    the native L3) for every station GOES does not serve ->
    ``sw_measured_hourly`` ``source=<product>``, ``proxy=0``.
  * ``era5`` -- ERA5 shortwave as the stand-in for the GOES stations until
    the GOES backfill lands (user 2026-10-20 「美洲站标『代理』，不出读数」) ->
    ``source='ERA5_PROXY'``, ``proxy=1``.

Hourly radiation is the mean of the hour ending at the stamp, the board's
basis; stamps are stored in UTC.

    .venv/bin/python docs/research/tmax_physics_m1_ingest_20261020/ingest_open_meteo.py --task prev1 --max-weight 400
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store  # noqa: E402

from bayes_basket_target import open_meteo_quota as quota  # noqa: E402
from bayes_basket_target.station_day_board import _satellite_product  # noqa: E402
from bayes_basket_target.station_registry import _COORDINATES  # noqa: E402

_SSL = ssl.create_default_context(cafile=__import__("certifi").where())
WINDOW_DAYS = 14
PREV_VARS = ["temperature_2m", "shortwave_radiation", "dew_point_2m", "relative_humidity_2m",
             "wind_speed_10m", "wind_direction_10m", "precipitation", "cloud_cover"]
ENDPOINT = {
    "prev1": "https://previous-runs-api.open-meteo.com/v1/forecast",
    "sat": "https://satellite-api.open-meteo.com/v1/archive",
    "sarah3": "https://satellite-api.open-meteo.com/v1/archive",
    "era5": "https://archive-api.open-meteo.com/v1/archive",
}
# First archived day per satellite product, probed 2026-10-21 (single-station
# bisection, 3-day resolution): windows before it come back all null.
PRODUCT_START = {
    "jma_jaxa_himawari": "2024-01-01",
    "eumetsat_lsa_saf_msg": "2025-01-02",
    "eumetsat_lsa_saf_iodc": "2025-04-12",
    "dwd_sis_europe_africa_v4": "2026-03-12",
}


def stations_for(task: str) -> dict[str, str | None]:
    """station -> model parameter (None = the endpoint's default)."""

    if task == "prev1":
        return {s: None for s in sorted(_COORDINATES)}
    if task == "sat":
        # JAXA stations: the board reads native JAXA L3; Open-Meteo's
        # jma_jaxa_himawari matched it on 2026-10-17..19 (532 hours, r 0.9998,
        # mean +0.6 W/m2, MAE 3.7), so its archive stands in for the
        # 18,000-file native backfill.
        return {s: ("jma_jaxa_himawari" if p == "jaxa_ptree_himawari" else p) for s in sorted(_COORDINATES)
                if (p := _satellite_product(_COORDINATES[s][1]))}
    if task == "sarah3":
        # The board's European product (DWD SIS) is archived only from
        # 2026-03-12; EUMETSAT SARAH3 carries the history there (2026-10-15..17
        # against DWD SIS: r 0.986, MAE 25.8 W/m2, mean -5.7) and for the MSG
        # pair (r ~0.95, MAE ~55).  Kept under its own source; IODC stations
        # lie outside it.
        return {s: "eumetsat_sarah3" for s in sorted(_COORDINATES)
                if _satellite_product(_COORDINATES[s][1]) in ("dwd_sis_europe_africa_v4", "eumetsat_lsa_saf_msg")}
    if task == "era5":
        return {s: "era5" for s in sorted(_COORDINATES) if _satellite_product(_COORDINATES[s][1]) is None}
    raise SystemExit(f"unknown task {task}")


def windows(end: date) -> list[tuple[date, date]]:
    out, d = [], date.fromisoformat(store.START)
    while d <= end:
        out.append((d, min(end, d + timedelta(days=WINDOW_DAYS - 1))))
        d += timedelta(days=WINDOW_DAYS)
    return out


def call(task: str, lane: str, group: list[str], model: str | None, a: date, b: date) -> list[dict]:
    quota.charge(lane, len(group))
    hourly = [v + "_previous_day1" for v in PREV_VARS] if task == "prev1" else ["shortwave_radiation"]
    q = {"latitude": ",".join(str(_COORDINATES[s][0]) for s in group),
         "longitude": ",".join(str(_COORDINATES[s][1]) for s in group),
         "hourly": ",".join(hourly), "start_date": a.isoformat(), "end_date": b.isoformat(), "timezone": "UTC"}
    if model:
        q["models"] = model
    req = urllib.request.Request(ENDPOINT[task] + "?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "weather-research-m1"})
    try:
        got = json.loads(urllib.request.urlopen(req, timeout=180, context=_SSL).read(100 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        if quota.classify(body, exc.code) == "day":
            quota.mark_exhausted()
        raise RuntimeError(f"http {exc.code}: {body[:200]}") from exc
    return got if isinstance(got, list) else [got]


def store_rows(conn, task: str, group: list[str], model: str | None, got: list[dict]) -> int:
    n = 0
    for st, r in zip(group, got):
        h = r.get("hourly") or {}
        times = [t + ":00Z" if len(t) == 16 else t for t in h.get("time") or []]
        if task == "prev1":
            rows = [(st, t, v, val, "OPEN_METEO_PREV1_aggregator")
                    for v in PREV_VARS for t, val in zip(times, h.get(v + "_previous_day1") or [])]
            conn.executemany("INSERT OR REPLACE INTO forecast_d1_hourly VALUES (?,?,?,?,?)", rows)
        else:
            source, proxy = ("ERA5_PROXY", 1) if task == "era5" else (str(model), 0)
            rows = [(st, t, val, source, proxy, None) for t, val in zip(times, h.get("shortwave_radiation") or [])]
            conn.executemany("INSERT OR REPLACE INTO sw_measured_hourly VALUES (?,?,?,?,?,?)", rows)
        n += len(rows)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, choices=sorted(ENDPOINT))
    ap.add_argument("--lane", default="research")
    ap.add_argument("--max-weight", type=int, default=400, help="calls this run may charge")
    ap.add_argument("--budget", type=float, default=540.0)
    ap.add_argument("--refresh-days", type=int, default=0,
                    help="daily refresh: re-fetch every window reaching into the last N days (the newest window grows)")
    args = ap.parse_args()
    t0 = time.time()
    conn = store.connect()
    by_station = stations_for(args.task)
    groups: dict[str | None, list[str]] = {}
    for s, m in by_station.items():
        groups.setdefault(m, []).append(s)
    end = datetime.now(timezone.utc).date() - timedelta(days=3)   # the satellite archive lags ~2 days
    have = store.done(conn, args.task)
    spent = 0
    for model, group in groups.items():
        for a, b in windows(end):
            unit = f"{model}|{a}"
            fresh = args.refresh_days and b >= end - timedelta(days=args.refresh_days)
            if (unit in have and not fresh) or b.isoformat() < PRODUCT_START.get(str(model), "0000"):
                continue
            if spent + len(group) > args.max_weight or time.time() - t0 > args.budget or not store.disk_ok():
                break
            try:
                got = call(args.task, args.lane, group, model, a, b)
                n = store_rows(conn, args.task, group, model, got)
                store.mark(conn, args.task, unit, "ok", f"{n} values")
            except quota.QuotaRefused as exc:
                print(f"quota refused: {exc}")
                conn.commit()
                return 0
            except Exception as exc:  # noqa: BLE001 -- retried next run
                store.mark(conn, args.task, unit, "error", f"{type(exc).__name__}: {exc}")
            spent += len(group)
            conn.commit()
            time.sleep(1.0)
    total = sum(len(g) * len(windows(end)) for g in groups.values())
    done_w = sum(len(groups[m]) for u in store.done(conn, args.task) for m in [None if u.split("|")[0] == "None" else u.split("|")[0]] if m in groups)
    print(f"{args.task}: charged {spent} this run; {done_w}/{total} weighted units done; {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
