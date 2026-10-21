"""IEM hourly METAR (routine + specials) per station, 2024-01-01 .. yesterday,
into ``metar_hourly``; then the local-day maximum into ``tmax_daily``.

IEM is ``aggregator_republisher``, cleared as training data 2026-09-24
(research only).  One request per station x calendar half-year; resumable
(``fetch_log``), stops within ``--budget`` seconds.  Run from the repo root:

    .venv/bin/python docs/research/tmax_physics_m1_ingest_20261020/ingest_iem.py --budget 500
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store  # noqa: E402

sys.path.insert(0, str(HERE.parent / "tmax_physics_m0_coverage_20261020"))
from coverage import _iem_network  # noqa: E402

URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
FIELDS = ["tmpf", "dwpf", "relh", "drct", "sknt", "vsby", "skyc1", "skyc2", "skyc3", "skyc4",
          "skyl1", "skyl2", "skyl3", "skyl4", "wxcodes"]
_SSL = ssl.create_default_context(cafile=__import__("certifi").where())
TASK = "iem_metar"


def _f(x: str) -> float | None:
    try:
        return None if x in ("", "M", "T") else float(x)
    except ValueError:
        return None


def halves(start: date, end: date) -> list[tuple[date, date]]:
    out, d = [], start
    while d <= end:
        stop = date(d.year, 7, 1) if d.month < 7 else date(d.year + 1, 1, 1)
        out.append((d, min(end, stop - timedelta(days=1))))
        d = stop
    return out


def fetch(station: str, a: date, b: date) -> list[tuple]:
    sid, _net = _iem_network(station)
    params = [("station", sid)] + [("data", f) for f in FIELDS] + [
        ("year1", a.year), ("month1", a.month), ("day1", a.day),
        ("year2", (b + timedelta(days=1)).year), ("month2", (b + timedelta(days=1)).month),
        ("day2", (b + timedelta(days=1)).day), ("tz", "Etc/UTC"), ("format", "onlycomma"),
        ("missing", "M"), ("trace", "T"), ("report_type", "3"), ("report_type", "4")]
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "weather-research-m1"})
    body = urllib.request.urlopen(req, timeout=300, context=_SSL).read(200 * 1024 * 1024).decode("utf-8", "replace")
    rows = []
    for r in csv.DictReader(io.StringIO(body)):
        t = _f(r.get("tmpf", ""))
        dw = _f(r.get("dwpf", ""))
        sky = [[r.get(f"skyc{i}"), _f(r.get(f"skyl{i}", ""))] for i in range(1, 5) if r.get(f"skyc{i}") not in ("", "M", None)]
        rows.append((station, r["valid"].replace(" ", "T") + "Z",
                     None if t is None else round((t - 32) / 1.8, 2), None if dw is None else round((dw - 32) / 1.8, 2),
                     _f(r.get("relh", "")), _f(r.get("drct", "")), _f(r.get("sknt", "")), _f(r.get("vsby", "")),
                     json.dumps(sky) if sky else None, None if r.get("wxcodes") in ("", "M") else r.get("wxcodes"),
                     "IEM_ASOS_aggregator"))
    return rows


def daily(conn, station: str, tz: str) -> int:
    zone = ZoneInfo(tz)
    best: dict[str, list[float]] = {}
    for valid, t in conn.execute("SELECT valid_utc, tmp_c FROM metar_hourly WHERE station=? AND tmp_c IS NOT NULL",
                                 (station,)):
        local = datetime.fromisoformat(valid.replace("Z", "+00:00")).astimezone(zone).date().isoformat()
        best.setdefault(local, []).append(t)
    conn.execute("DELETE FROM tmax_daily WHERE station=?", (station,))
    conn.executemany("INSERT INTO tmax_daily VALUES (?,?,?,?,?,?)",
                     [(station, d, max(v), len(v), tz, "IEM_ASOS_aggregator") for d, v in best.items() if len(v) >= 18])
    return len(best)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=500.0)
    ap.add_argument("--recent-days", type=int, default=0,
                    help="daily refresh: only the last N days, one request a station")
    args = ap.parse_args()
    t0 = time.time()
    tz = store.timezones()
    conn = store.connect()
    have = store.done(conn, TASK)
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    if args.recent_days:
        todo = [(s, end - timedelta(days=args.recent_days), end) for s in sorted(tz)]
        have = set()
    else:
        todo = [(s, a, b) for s in sorted(tz) for a, b in halves(date.fromisoformat(store.START), end)
                if f"{s}|{a}" not in have or b >= end - timedelta(days=3)]
    touched = set()
    for s, a, b in todo:
        if time.time() - t0 > args.budget or not store.disk_ok():
            break
        try:
            rows = fetch(s, a, b)
            conn.executemany("INSERT OR REPLACE INTO metar_hourly VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            store.mark(conn, TASK, f"{s}|{a}", "ok", f"{len(rows)} rows")
            touched.add(s)
        except Exception as exc:  # noqa: BLE001 -- retried next run
            store.mark(conn, TASK, f"{s}|{a}", "error", f"{type(exc).__name__}: {exc}")
        conn.commit()
        time.sleep(1.0)
    for s in sorted(touched):
        daily(conn, s, tz[s])
    conn.commit()
    left = len([1 for s, a, b in todo if f"{s}|{a}" not in store.done(conn, TASK)])
    print(f"iem: {len(touched)} stations touched, {left} units left, {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
