"""GSMaP_NOW history for the P1 re-test's rain arm (user 2026-10-15 「云雨数据呢」).

Every HH:00 slot (rain of HH:00-HH:59 UTC) of 2026-09-01..2026-10-14, read
for every fleet station between 60N and 60S with
jaxa_gsmap.read_slot (cell and 3x3 mean, raw and gauge-calibrated), kept
with the file's FTP modification time (UTC, the real-time publication: one
minute after the hour) as its arrival receipt.  Resumable, bounded, files
deleted after reading; one line per slot in ``gsmap/<YYYY-MM-DD>.jsonl``.
"""

from __future__ import annotations

import argparse
import ftplib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from bayes_basket_target import jaxa_gsmap as G  # noqa: E402

OUT = HERE / "gsmap"
FIRST, LAST = date(2026, 9, 1), date(2026, 10, 14)
_LIST_RE = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3})\s+(\d+)\s+(\d{1,2}:\d{2})\s+(\S+)$")
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def day_mtimes(day: date) -> dict[str, str]:
    user, password = G._account()
    ftp = ftplib.FTP(G.HOST, timeout=60)
    ftp.login(user, password)
    lines: list[str] = []
    try:
        ftp.retrlines(f"LIST /now/netcdf/{day:%Y/%m/%d}", lines.append)
    finally:
        ftp.quit()
    out = {}
    for line in lines:
        m = _LIST_RE.match(line.strip())
        if m:
            hh, mm = m.group(4).split(":")
            out[m.group(5)] = datetime(day.year, _MONTHS[m.group(2)], int(m.group(3)), int(hh), int(mm),
                                       tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return out


def task(slot: datetime, mtimes: dict[str, str], stations) -> dict:
    name = f"gsmap_now_rain.{slot:%Y%m%d}.{slot:%H%M}.nc"
    if name not in mtimes:
        return {"slot": G.slot_key(slot), "state": "missing"}
    values = G.read_slot(slot, stations)
    if values is None:
        return {"slot": G.slot_key(slot), "state": "missing"}
    return {"slot": G.slot_key(slot), "state": "ok", "mtime_utc": mtimes[name], "values": values}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    deadline = time.monotonic() + 60 * args.max_minutes
    stations = G._fleet()
    OUT.mkdir(exist_ok=True)
    written = errors = 0
    day = FIRST
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while day <= LAST and time.monotonic() < deadline:
            path = OUT / f"{day.isoformat()}.jsonl"
            done = {json.loads(l)["slot"] for l in path.read_text().splitlines()} if path.exists() else set()
            slots = [datetime(day.year, day.month, day.day, h, tzinfo=timezone.utc) for h in range(24)]
            slots = [s for s in slots if G.slot_key(s) not in done]
            if slots:
                mtimes = day_mtimes(day)
                futures = {pool.submit(task, s, mtimes, stations): s for s in slots}
                for fut in as_completed(futures):
                    try:
                        rec = fut.result()
                    except Exception as exc:  # noqa: BLE001 -- left for the next call
                        errors += 1
                        print("error", futures[fut], type(exc).__name__, str(exc)[:100], flush=True)
                        continue
                    with path.open("a") as fh:
                        fh.write(json.dumps(rec, sort_keys=True) + "\n")
                    written += 1
            day += timedelta(days=1)
    total = sum(len(p.read_text().splitlines()) for p in OUT.glob("*.jsonl"))
    print(f"written {written} errors {errors} total {total}/{24 * ((LAST - FIRST).days + 1)}", flush=True)


if __name__ == "__main__":
    main()
