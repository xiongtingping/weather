"""Round 48 data (熊先生 10-12 「拉」): multi-year daily highs per station, for a
same-season climatology.

Source: Iowa Environmental Mesonet daily summaries (``/api/1/daily.json``,
the station's local-day maximum computed by IEM from its METAR/ASOS reports --
the same reports our truth reads; IEM is ``aggregator_republisher``, cleared
as training data 2026-09-24, research only here, never P0/P1).  NOAA GSOD was
tried first and stops at 2025-08-27 (no overlap with our truth).  One request
per station x year, 2016..2026, polite pacing with back-off; progress is saved
after every request and a run stops within ``--budget`` seconds so the next
run resumes.  Writes ``<bench>/iem_daily_r48.json`` = {station: {date: max_c}}.
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

B = Path(sys.argv[1])
BUDGET = float(sys.argv[2]) if len(sys.argv) > 2 else 520.0
OUT = B / "iem_daily_r48.json"
YEARS = range(2016, 2027)
COUNTRY = {
    "EHAM": "NL", "LTAC": "TR", "LTFM": "TR", "ZBAA": "CN", "SAEZ": "AR", "RKPK": "KR", "RKSI": "KR", "FACT": "ZA",
    "ZUUU": "CN", "ZUCK": "CN", "ZGGG": "CN", "ZGSZ": "CN", "ZHHH": "CN", "ZSPD": "CN", "ZSQD": "CN", "EFHK": "FI",
    "OEJN": "SA", "OPKC": "PK", "WMKK": "MY", "EGLC": "GB", "VILK": "IN", "LEMD": "ES", "RPLL": "PH", "MMMX": "MX",
    "LIMC": "IT", "UUWW": "RU", "EDDM": "DE", "MPMG": "PA", "LFPB": "FR", "SBGR": "BR", "WSSS": "SG", "RCSS": "TW",
    "LLBG": "IL", "RJTT": "JP", "EPWA": "PL", "NZWN": "NZ",
}
US_STATE = {"KATL": "GA", "KAUS": "TX", "KBKF": "CO", "KDAL": "TX", "KHOU": "TX", "KLAX": "CA", "KLGA": "NY",
            "KMIA": "FL", "KORD": "IL", "KSEA": "WA", "KSFO": "CA"}


def network(st):
    if st in US_STATE:
        return st[1:], f"{US_STATE[st]}_ASOS"
    if st == "CYYZ":
        return st, "CA_ON_ASOS"
    return st, f"{COUNTRY[st]}__ASOS"


have = json.load(open(OUT)) if OUT.exists() else {}
done = set(have.get("_done", []))
t0 = time.time()
stations = sorted(set(US_STATE) | set(COUNTRY) | {"CYYZ"})
todo = [(st, year) for st in stations for year in YEARS if f"{st}|{year}" not in done]


def fetch(item):
    st, year = item
    sid, net = network(st)
    if time.time() - t0 > BUDGET:
        return item, None
    url = f"https://mesonet.agron.iastate.edu/api/1/daily.json?station={sid}&network={net}&year={year}"
    for attempt in range(6):
        r = subprocess.run(["curl", "-s", "-m", "60", "-A", "weather-research-climatology/1", url], capture_output=True, text=True)
        try:
            return item, json.loads(r.stdout).get("data")
        except (ValueError, AttributeError):
            time.sleep(5 * (attempt + 1))
    return item, None


# Three requests at a time: IEM answers slowly (~6 s) rather than refusing.
with ThreadPoolExecutor(max_workers=3) as pool:
    for (st, year), data in pool.map(fetch, todo):
        if data is None:
            continue
        rows = have.setdefault(st, {})
        for d in data:
            if d.get("max_tmpf") is not None and not d.get("tmpf_est"):
                rows[d["date"]] = round((float(d["max_tmpf"]) - 32.0) * 5.0 / 9.0, 2)
        done.add(f"{st}|{year}")
        have["_done"] = sorted(done)
        json.dump(have, open(OUT, "w"))
left = len([1 for st in stations for year in YEARS if f"{st}|{year}" not in done])
print(f"{len(done)} station-years done, {left} left", file=sys.stderr)
