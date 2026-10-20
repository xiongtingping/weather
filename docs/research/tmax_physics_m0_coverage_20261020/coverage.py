"""M0 of the same-day Tmax increment model (ARCHITECTURE.md in
docs/research/tmax_physics_model_design_20261020): how many years of history
each station has in each source, and what is missing.

Probes (read-only; the SSOT and the local physics file are opened ``mode=ro``):
  * IEM daily summaries (``/api/1/daily.json``), one request per station x
    year 2016..2026: days with a local-day maximum.  IEM is an
    ``aggregator_republisher`` cleared as training data 2026-09-24.
  * Open-Meteo Previous Runs (the board's day-ahead basis): one batched call
    for 2024-01-02..04 with the ``_previous_day1`` variables -- non-null share
    per station and variable.  Charged to the research lane of the shared
    quota ledger first.
  * Open-Meteo satellite archive: one batched call for 2016-07-15 --
    which stations it serves that far back.  Charged likewise.
  * Native satellite archives, by region: JAXA P-Tree L3 PAR (listed
    2026-10-20: v021 2015-07..now), GOES ABI-L2-DSRF on AWS (GOES-16
    2019..2025, GOES-19 2024..now).
  * SSOT: formal truth days (charter 225 lanes) and observation facts.
  * Local native physics (data/deterministic_point_physics.db) and the
    terrain table.

Writes coverage.json and prints the table.  Run from the repo root.
"""

from __future__ import annotations

import json
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bayes_basket_target import open_meteo_quota as quota  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.station_registry import _COORDINATES  # noqa: E402

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
PREV_VARS = ["temperature_2m", "shortwave_radiation", "cloud_cover_low", "dew_point_2m", "relative_humidity_2m",
             "wind_speed_10m", "precipitation"]
UA = {"User-Agent": "weather-research-m0/1.0"}
_SSL = ssl.create_default_context(cafile=__import__("certifi").where())


def _get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return resp.read(50 * 1024 * 1024)


def _iem_network(st: str) -> tuple[str, str]:
    if st in US_STATE:
        return st[1:], f"{US_STATE[st]}_ASOS"
    if st == "CYYZ":
        return st, "CA_ON_ASOS"
    return st, f"{COUNTRY[st]}__ASOS"


def iem_days(stations: list[str]) -> dict[str, dict[str, int]]:
    def one(item):
        st, year = item
        sid, net = _iem_network(st)
        url = f"https://mesonet.agron.iastate.edu/api/1/daily.json?station={sid}&network={net}&year={year}"
        for attempt in range(3):
            try:
                rows = json.loads(_get(url)).get("data") or []
                return st, year, sum(1 for r in rows if r.get("max_tmpf") is not None)
            except Exception:  # noqa: BLE001 -- retried, then reported as unknown
                time.sleep(2.0 * (attempt + 1))
        return st, year, None

    out: dict[str, dict[str, int]] = {st: {} for st in stations}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for st, year, n in pool.map(one, [(s, y) for s in stations for y in YEARS]):
            out[st][str(year)] = n
    return out


def _open_meteo(base: str, params: dict, stations: list[str]) -> list[dict]:
    quota.charge("research", len(stations))
    q = dict(params)
    q["latitude"] = ",".join(str(_COORDINATES[s][0]) for s in stations)
    q["longitude"] = ",".join(str(_COORDINATES[s][1]) for s in stations)
    got = json.loads(_get(base + "?" + urllib.parse.urlencode(q), timeout=120.0))
    return got if isinstance(got, list) else [got]


def previous_runs(stations: list[str]) -> dict[str, dict[str, float]]:
    rows = _open_meteo("https://previous-runs-api.open-meteo.com/v1/forecast", {
        "hourly": ",".join(v + "_previous_day1" for v in PREV_VARS),
        "start_date": "2024-01-02", "end_date": "2024-01-04", "timezone": "UTC"}, stations)
    out = {}
    for st, r in zip(stations, rows):
        h = r.get("hourly") or {}
        out[st] = {v: (sum(x is not None for x in h.get(v + "_previous_day1") or [])
                       / max(1, len(h.get(v + "_previous_day1") or []))) for v in PREV_VARS}
    return out


def satellite_2016(stations: list[str]) -> dict[str, float]:
    try:
        rows = _open_meteo("https://satellite-api.open-meteo.com/v1/archive", {
            "hourly": "shortwave_radiation", "start_date": "2016-07-15", "end_date": "2016-07-15",
            "timezone": "UTC"}, stations)
    except Exception as exc:  # noqa: BLE001 -- the table says why
        return {"_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    out = {}
    for st, r in zip(stations, rows):
        vals = (r.get("hourly") or {}).get("shortwave_radiation") or []
        out[st] = sum(v is not None for v in vals) / max(1, len(vals))
    return out


def native_satellite(st: str) -> tuple[str, str]:
    lat, lon = _COORDINATES[st]
    if lon >= 90.0 or lon <= -150.0:
        return "JAXA L3 PAR", "2015-07"
    if lon <= -30.0:
        return "GOES ABI DSRF", "2019"
    return "SARAH3 (Open-Meteo)", "1983"


def ssot(stations: list[str]) -> dict[str, dict]:
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=60.0)
    try:
        truth = dict(((s, (a, b, n)) for s, a, b, n in conn.execute(
            "SELECT station_id, min(local_date), max(local_date), count(DISTINCT local_date)"
            " FROM calibration_station_daily_high_truth WHERE source_lane IN " + FORMAL_TRUTH_LANES_SQL
            + " GROUP BY station_id")))
        obs = dict(((s, (a, n)) for s, a, n in conn.execute(
            "SELECT station_id, substr(min(observed_at_utc),1,10), count(*) FROM basket_target_observation_facts"
            " GROUP BY station_id")))
    finally:
        conn.close()
    phys = {}
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'deterministic_point_physics.db'}?mode=ro", uri=True)
    try:
        for s, a, b, n in conn.execute(
                "SELECT station_id, min(target_local_date), max(target_local_date), count(DISTINCT target_local_date)"
                " FROM calibration_deterministic_point_physics_run GROUP BY station_id"):
            phys[s] = (a, b, n)
    finally:
        conn.close()
    terrain = json.loads((ROOT / "data" / "runtime" / "listing_forecast_f_terrain_v1.json").read_text())
    systems = terrain.get("systems") or {}
    with_terrain = {s for sys_rows in systems.values() if isinstance(sys_rows, dict) for s in sys_rows}
    return {s: {"truth": truth.get(s), "obs": obs.get(s), "native_physics": phys.get(s),
                "terrain": s in with_terrain} for s in stations}


def main() -> int:
    stations = sorted(_COORDINATES)
    t0 = time.time()
    result = {"stations": stations, "ssot": ssot(stations)}
    result["previous_runs_2024_01"] = previous_runs(stations)
    result["satellite_2016_07"] = satellite_2016(stations)
    result["iem_days_by_year"] = iem_days(stations)
    result["native_satellite"] = {s: native_satellite(s) for s in stations}
    result["elapsed_s"] = round(time.time() - t0, 1)
    (HERE / "coverage.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    print("done", result["elapsed_s"], "s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
