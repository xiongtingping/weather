"""JAXA Himawari history for the P1 re-test (user 2026-10-15 「重开重测」).

For every UTC hour of 2026-09-01..2026-10-14 in which any covered fleet
station (Himawari disk, east of 70E) has the sun up, two official JAXA
P-Tree products, read at each station and kept with the file's FTP
modification time (UTC) as its arrival receipt:

* ``swr``  Short Wave Radiation, Level 3 hourly mean of HH:00-HH:59
  (jaxa_ptree_radiation.read_file: the station's 5 km cell);
* ``clp``  Cloud Property, Level 2, the HH:00 scan
  (jaxa_ptree_radiation.read_clp_file: 5 x 5 box cover, commonest ISCCP
  type, median top height and optical thickness).

Resumable: one JSON line per (hour, kind) in ``jaxa/<YYYY-MM-DD>.jsonl``;
a line already there is not fetched again.  Bounded: ``--max-minutes`` per
call, ``--workers`` parallel downloads (each file deleted after reading),
and no new download while the machine's free memory is under the floor.

Read-only for production: the account is ~/.netrc, the files land in
data/runtime/jaxa_ptree_tmp and are deleted.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import jaxa_ptree_radiation as J  # noqa: E402

OUT = HERE / "jaxa"
FIRST, LAST = date(2026, 9, 1), date(2026, 10, 14)
MIN_FREE_MEMORY_PCT = 20
_LIST_RE = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3})\s+(\d+)\s+(\d{1,2}:\d{2}|\d{4})\s+(\S+)$")
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_LISTINGS: dict[str, dict[str, str]] = {}
_LOCK = threading.Lock()


def listing(directory: str, year: int) -> dict[str, str]:
    """name -> modification time (UTC ISO) for one FTP folder."""

    with _LOCK:
        if directory in _LISTINGS:
            return _LISTINGS[directory]
    out = J._curl(f"ftps://{J.HOST}{directory}/", timeout=60)
    names: dict[str, str] = {}
    if out.returncode == 0:
        for line in out.stdout.decode(errors="replace").splitlines():
            m = _LIST_RE.match(line.strip())
            if not m or ":" not in m.group(4):
                continue
            hh, mm = m.group(4).split(":")
            stamp = datetime(year, _MONTHS[m.group(2)], int(m.group(3)), int(hh), int(mm), tzinfo=timezone.utc)
            names[m.group(5)] = stamp.strftime("%Y-%m-%dT%H:%MZ")
    with _LOCK:
        _LISTINGS[directory] = names
    return names


def sunny_hours(day: date, stations: dict[str, tuple[float, float]]) -> list[datetime]:
    hours = []
    for h in range(24):
        middle = datetime(day.year, day.month, day.day, h, 30, tzinfo=timezone.utc)
        if any(_clear_sky(lat, lon, middle) > 0.0 for lat, lon in stations.values()):
            hours.append(middle.replace(minute=0))
    return hours


def _clear_sky(lat: float, lon: float, when: datetime) -> float:
    n = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60.0
    g = 2 * math.pi / 365 * (n - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    ha = math.radians((hour * 60 + eot + 4 * lon) / 4 - 180)
    phi = math.radians(lat)
    cosz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    return 0.0 if cosz <= 0.01 else 1098.0 * cosz * math.exp(-0.057 / cosz)


def free_memory_pct() -> int:
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=20).stdout
        return int(re.search(r"free percentage:\s*(\d+)", out).group(1))
    except Exception:  # noqa: BLE001 -- unknown reads as short
        return 0


def task(hour: datetime, kind: str, stations: dict[str, tuple[float, float]]) -> dict:
    if kind == "swr":
        directory = f"{J.PRODUCT_DIR}/{J._version()}/{hour:%Y%m}/{hour:%d}"
        names = listing(directory, hour.year)
        wanted = [n for n in names if (m := J._FILE_RE.match(n)) and m.group(1) == f"{hour:%Y%m%d}" and m.group(2) == f"{hour:%H}"]
        if not wanted:
            return {"hour": hour.strftime("%Y-%m-%dT%H:00Z"), "kind": kind, "state": "missing"}
        name = wanted[0]
        values = J.read_file(f"{directory}/{name}", stations)
    else:
        directory = f"{J.CLP_DIR}/{J._version(J.CLP_DIR)}/{hour:%Y%m}/{hour:%d}/{hour:%H}"
        names = listing(directory, hour.year)
        wanted = [n for n in names if (m := J._CLP_RE.match(n)) and m.group(1) == f"{hour:%Y%m%d}" and m.group(2) == f"{hour:%H}00"]
        if not wanted:
            return {"hour": hour.strftime("%Y-%m-%dT%H:00Z"), "kind": kind, "state": "missing"}
        name = wanted[0]
        values = J.read_clp_file(f"{directory}/{name}", stations)
    return {"hour": hour.strftime("%Y-%m-%dT%H:00Z"), "kind": kind, "state": "ok", "file": name,
            "mtime_utc": names[name], "values": values}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=8.5)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--kinds", default="clp,swr", help="which products, in this order")
    args = ap.parse_args()
    kinds = [k for k in args.kinds.split(",") if k in ("swr", "clp")]
    deadline = time.monotonic() + 60 * args.max_minutes
    stations = J._fleet()
    OUT.mkdir(exist_ok=True)
    todo = []
    day = FIRST
    while day <= LAST:
        path = OUT / f"{day.isoformat()}.jsonl"
        done = set()
        if path.exists():
            for line in path.read_text().splitlines():
                rec = json.loads(line)
                done.add((rec["hour"], rec["kind"]))
        for hour in sunny_hours(day, stations):
            for kind in kinds:
                if (hour.strftime("%Y-%m-%dT%H:00Z"), kind) not in done:
                    todo.append((hour, kind))
        day += timedelta(days=1)
    todo.sort(key=lambda item: (kinds.index(item[1]), item[0]))
    print(f"todo {len(todo)} (hour, kind) pairs", flush=True)
    written = errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        queue = list(todo)
        while queue or futures:
            while queue and len(futures) < args.workers and time.monotonic() < deadline:
                if free_memory_pct() < MIN_FREE_MEMORY_PCT:
                    print("memory short; stop starting", flush=True)
                    queue.clear()
                    break
                hour, kind = queue.pop(0)
                futures[pool.submit(task, hour, kind, stations)] = (hour, kind)
            if not futures:
                break
            if time.monotonic() >= deadline:
                queue.clear()
            done_one = next(as_completed(futures))
            hour, kind = futures.pop(done_one)
            try:
                rec = done_one.result()
            except Exception as exc:  # noqa: BLE001 -- left for the next call
                errors += 1
                print("error", hour, kind, type(exc).__name__, str(exc)[:120], flush=True)
                continue
            with (OUT / f"{hour.date().isoformat()}.jsonl").open("a") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
            written += 1
    print(f"written {written} errors {errors} remaining {len(todo) - written}", flush=True)


if __name__ == "__main__":
    main()
