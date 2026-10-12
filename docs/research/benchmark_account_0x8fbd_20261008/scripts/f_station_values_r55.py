"""Round 55: F rebuilt on STATION values -- does it reproduce the cluster's bucket?

Round 54d: Open-Meteo's GFS station value = GFS's native ~13 km flux grid,
hourly TMAX, nearest land cell, lapse-rate downscaling to the station's
elevation (corr 0.94); for same-grid members the downscaling alone (GDPS
corr 0.94).  Here the same recipe natively for the cluster's event days:
  GFS       sflux TMAX at the D-2 00Z run (the run F had at the trade), nearest
            land cell on the Gaussian grid + lapse; cached per run in
            <bench>/sflux_gfs_r55.json (resume; one run per minute or so)
  others    the archived member value + the round 39h terrain offset
            (lapse x (cell height - station height)) -- the same downscaling
F_v3 = raw median of the station values + c + lambda x corr, biases over the
station's history on the same station values (GFS history: its archived
value + the station's sflux-minus-archive constant, 88% constant in round 54c).
Scored on the 227 picks against production F: agreement, truth hits, halves;
and the money centre regressed on (F_v3 - F) next to (research F - F).
Usage: python f_station_values_r55.py <bench> [--fetch-budget SECONDS]
"""
import ctypes
import json
import math
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import native_deterministic_point_archive as N  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, global_roster  # noqa: E402
from bayes_basket_target.dwd_icon_eps_archive import _eccodes  # noqa: E402
from bayes_basket_target.noaa_conus_archive import GridPointReader  # noqa: E402

B = Path(sys.argv[1])
BUDGET = float(sys.argv[sys.argv.index("--fetch-budget") + 1]) if "--fetch-budget" in sys.argv else 480.0
HERE = Path(__file__).parent.parent
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
ROSTER = {st: (lat, lon, zone) for st, lat, lon, zone in global_roster(conn)}
TERRAIN = json.load(open(HERE / "terrain_offsets_r39h.json"))["systems"]
LAPSE = 0.0065
STEPS = list(range(36, 79, 3))
CACHE = B / "sflux_gfs_r55.json"
cache = json.load(open(CACHE)) if CACHE.exists() else {}
GFS_ID = "NOAA_GFS_025_NATIVE_POINT"


def sflux_url(run, step):
    return f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.sfluxgrbf{step:03d}.grib2"


def read_sflux(run, step, field):
    idx = N._get(sflux_url(run, step) + ".idx", cap=N.MAX_INDEX_BYTES)
    if idx is None:
        return None
    span = N.parse_noaa_idx(idx.decode("utf-8", "replace"), field=field)
    if span is None:
        return None
    start, length = span
    rng = f"bytes={start}-" if length is None else f"bytes={start}-{start + length - 1}"
    return N._get(sflux_url(run, step), {"Range": rng}, cap=40 * 1024 * 1024)


def decode(message, with_coords=False):
    lib = _eccodes()
    buf = ctypes.create_string_buffer(message, len(message))
    h = lib.codes_handle_new_from_message(None, buf, ctypes.c_size_t(len(message)))
    try:
        out = {"v": np.array(GridPointReader._array(lib, h, b"values"), dtype=float)}
        if with_coords:
            out["lat"] = np.array(GridPointReader._array(lib, h, b"latitudes"), dtype=float)
            out["lon"] = np.array(GridPointReader._array(lib, h, b"longitudes"), dtype=float) % 360.0
        return out
    finally:
        lib.codes_handle_delete(h)


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    h = np.sin((lat2 - lat1) * p / 2) ** 2 + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * np.arcsin(np.sqrt(h))


# event days of the cluster's picks -> GFS D-2 00Z runs
src = (HERE / "scripts" / "cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(HERE / "scripts" / "cluster_money_target_r45.py"), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
rows, bucket, prod_f = ns["rows"], ns["bucket"], ns["prod_f"]
ALL_TRUTHS = ns["ns"]["ns"]["ALL_TRUTHS"]
research_c = ns["ns"]["ns"]["research_c"]
FRm = {(r_["st"], r_["ed"]): r_ for r_ in ns["ns"]["FR_ALL"]}
event_days = sorted({r["ev"]["ed"] for r in rows})

# station cells on the flux grid, once (land + terrain at one run)
if "_cells" not in cache:
    run0 = datetime.fromisoformat(event_days[0]).replace(tzinfo=timezone.utc) - timedelta(days=2)
    land = decode(read_sflux(run0, 0, "LAND:surface"), with_coords=True)
    hgt = decode(read_sflux(run0, 0, "HGT:surface"))
    cells = {}
    for st, (lat, lon, zone) in ROSTER.items():
        t = TERRAIN.get(GFS_ID, {}).get(st)
        if not t or t.get("station_elevation_m") is None:
            continue
        lon360 = lon % 360.0
        near = np.flatnonzero((np.abs(land["lat"] - lat) <= 0.35) & (np.minimum(np.abs(land["lon"] - lon360), 360 - np.abs(land["lon"] - lon360)) <= 0.35))
        d = km(lat, lon360, land["lat"][near], land["lon"][near])
        order = near[np.argsort(d)]
        dsort = np.sort(d)
        spacing = float(dsort[1]) if len(dsort) > 1 else 13.0
        reach = max(10.0, 1.5 * spacing)
        is_land = land["v"][order] >= 0.5
        nl = int(order[0]) if is_land[0] else next((int(i) for i, dd, l in zip(order, dsort, is_land) if l and dd <= reach), None)
        if nl is not None:
            cells[st] = {"cell": nl, "cell_height_m": float(hgt["v"][nl]), "station_elevation_m": float(t["station_elevation_m"])}
    cache["_cells"] = cells
    json.dump(cache, open(CACHE, "w"))
cells = cache["_cells"]

# fetch runs within the budget
t0 = time.time()
for ed in event_days:
    run = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)
    key = run.strftime("%Y-%m-%dT%H:%MZ")
    if key in cache:
        continue
    if time.time() - t0 > BUDGET:
        break
    fields = {}
    for step in STEPS:
        msg = read_sflux(run, step, "TMAX:2 m above ground")
        if msg is not None:
            fields[step] = decode(msg)["v"]
    per = {}
    for st, c in cells.items():
        tz = ZoneInfo(ROSTER[st][2])
        by_day = {}
        for step, v in fields.items():
            t = (run + timedelta(hours=step)).astimezone(tz)
            val = float(v[c["cell"]]) - 273.15 + LAPSE * (c["cell_height_m"] - c["station_elevation_m"])
            by_day.setdefault(t.date().isoformat(), []).append((t.hour, val))
        for day, pts in by_day.items():
            hours = [h for h, _v in pts]
            if min(hours) <= 9 and max(hours) >= 15:
                per.setdefault(st, {})[day] = round(max(v for _h, v in pts), 3)
    cache[key] = {"steps": len(fields), "values": per}
    json.dump(cache, open(CACHE, "w"))
    print(f"  {key}: {len(fields)} steps, {sum(len(v) for v in per.values())} station-days", file=sys.stderr)
have = [ed for ed in event_days if (datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ") in cache]
print(f"runs cached {len(have)}/{len(event_days)} event days", file=sys.stderr)
if len(have) < len(event_days):
    print("fetch incomplete; rerun to continue", file=sys.stderr)
    sys.exit(0)

# ---------------------------------------------------------------- F_v3
from bayes_basket_target import listing_forecast_f as F  # noqa: E402

def gfs_v3(st, ed):
    run = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)
    return cache.get(run.strftime("%Y-%m-%dT%H:%MZ"), {}).get("values", {}).get(st, {}).get(ed)


# the station's sflux-minus-archive constant for GFS history
const = {}
for ed in event_days:
    run = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)
    for st, days_ in cache[run.strftime("%Y-%m-%dT%H:%MZ")]["values"].items():
        for day, v in days_.items():
            nat = conn.execute("SELECT forecast_tmax_c FROM calibration_regional_point_run WHERE ensemble_system_id=? AND station_id=? AND target_local_date=?"
                               " AND run_init_utc=? ORDER BY archive_query_at_utc DESC LIMIT 1", (GFS_ID, st, day, run.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()
            if nat:
                const.setdefault(st, []).append(v - float(nat[0]))
const = {st: statistics.mean(v) for st, v in const.items() if len(v) >= 3}
print(f"GFS station constants (sflux + lapse - archived): {len(const)} stations, mean {statistics.mean(const.values()):+.2f}, sd {statistics.pstdev(list(const.values())):.2f}", file=sys.stderr)


def station_value(system, st, raw):
    """A member's archived value turned into a station value."""
    if system == GFS_ID:
        return raw + const.get(st, 0.0)
    t = TERRAIN.get(system, {}).get(st)
    return raw + (float(t["offset_c"]) if t and t.get("offset_c") is not None else 0.0)


_C3 = {}


def fleet_c_v3(D):
    """The fleet shift re-estimated on STATION values: mean(truth - median of station values) over the trailing 30 days (<= D-2)."""
    if D not in _C3:
        errs = []
        for k in range(2, 32):
            past = D - timedelta(days=k)
            for st, (_lat, _lon, zone) in ROSTER.items():
                tr = ALL_TRUTHS.get((st, past.isoformat()))
                pt = F._history_point(conn, station_id=st, event_date=past, zone=zone)
                if tr is None or not pt or len(pt) < 3:
                    continue
                errs.append(tr - statistics.median(station_value(sy, st, v) for sy, v in pt.items()))
        _C3[D] = statistics.mean(errs) if len(errs) >= 200 else None
    return _C3[D]


def f_v3(r, lam=0.5, exact_gfs=True, c_mode="station", recipe="production"):
    ev = r["ev"]
    st, ed = ev["st"], ev["ed"]
    as_of = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    zone = ROSTER[st][2]
    maxima = F.system_maxima(conn, station_id=st, event_date=date.fromisoformat(ed), as_of_utc=as_of, settlement_timezone=zone)
    if len(maxima) < 3:
        return None
    members = {}
    for system, e in maxima.items():
        v = float(e["tmax_c"])
        if system == GFS_ID and exact_gfs and gfs_v3(st, ed) is not None and str(e["run_init_utc"])[:13] == (datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H"):
            members[system] = gfs_v3(st, ed)
        else:
            members[system] = station_value(system, st, v)
    raw = statistics.median(members.values())
    D = date.fromisoformat(ed)
    # biases on station values over the station's history (listing-time points, settled by D-2 as production does)
    biases = {}
    for system in members:
        errs = []
        for k in range(2, 61):
            past = D - timedelta(days=k)
            pt = F._history_point(conn, station_id=st, event_date=past, zone=zone)
            tr = ALL_TRUTHS.get((st, past.isoformat()))
            if pt and system in pt and tr is not None and past <= D - timedelta(days=2):
                errs.append(tr - station_value(system, st, pt[system]))
        biases[system] = statistics.mean(errs) if len(errs) >= 3 else 0.0
    corr = statistics.median(members[m] + biases[m] for m in members) - raw
    c = fleet_c_v3(D) if c_mode == "station" else prod_f(ev["ts"])["fleet"]["c"]
    if recipe == "research":
        return raw + 0.8 + 0.3 * corr
    return raw + c + lam * corr


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]
print(f"\n{len(rows)} picks; production F = cluster {statistics.mean(r['ev']['fb'] == r['ev']['choice'] for r in rows):.1%}, hits {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for r in rows if r['ev']['truth'] is not None):.1%}")
arms = {"F_v3 (station values, lambda 0.5)": lambda r: f_v3(r, 0.5), "F_v3 lambda 0.3": lambda r: f_v3(r, 0.3),
        "F_v3 lambda 0.75": lambda r: f_v3(r, 0.75), "F_v3 GFS constant only (no exact sflux)": lambda r: f_v3(r, 0.5, exact_gfs=False),
        "F_v3 with the grid-value c (over-warm)": lambda r: f_v3(r, 0.5, c_mode="grid"),
        "F_v3 research recipe (0.8 + 0.3 bias)": lambda r: f_v3(r, recipe="research"),
        "research F (Open-Meteo ten models)": lambda r: research_c(FRm[(r["ev"]["st"], r["ev"]["ed"])], r["ev"]["ts"]) if (r["ev"]["st"], r["ev"]["ed"]) in FRm else None}
vals = {}
for name, fn in arms.items():
    got = []
    for r in rows:
        try:
            v = fn(r)
        except Exception:  # noqa: BLE001
            v = None
        if v is not None:
            got.append((r, v))
    vals[name] = got
    same = [bucket(r["ev"]["edges"], v) == r["ev"]["choice"] for r, v in got]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r, _v in got]
    tr = [(r, v) for r, v in got if r["ev"]["truth"] is not None]
    h1 = statistics.mean(s for s, (r, _v) in zip(same, got) if r["ev"]["ed"] < mid) - statistics.mean(b for b, (r, _v) in zip(base, got) if r["ev"]["ed"] < mid)
    h2 = statistics.mean(s for s, (r, _v) in zip(same, got) if r["ev"]["ed"] >= mid) - statistics.mean(b for b, (r, _v) in zip(base, got) if r["ev"]["ed"] >= mid)
    print(f"  {name:44s} n {len(got):3d}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} halves {h1:+.3f}/{h2:+.3f};"
          f" hits {statistics.mean(bucket(r['ev']['edges'], v) == r['ev']['truth'] for r, v in tr):.1%} vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for r, _v in tr):.1%};"
          f" mean (arm - F) {statistics.mean(v - r['f'] for r, v in got):+.2f} C")
# the money centre on (F_v3 - F) and (research F - F)
rng = np.random.default_rng(55)
for name in ("F_v3 (station values, lambda 0.5)", "research F (Open-Meteo ten models)"):
    s1 = [(r, v) for r, v in vals[name] if "1 h" in r["win"]]
    x = np.array([v - r["f"] for r, v in s1])
    y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r, _v in s1])
    A = np.c_[x, np.ones(len(x))]
    b = np.linalg.lstsq(A, y, rcond=None)[0]
    dd = np.array([r["ev"]["ed"] for r, _v in s1])
    ud = sorted(set(dd))
    bs = [np.linalg.lstsq(A[idx], y[idx], rcond=None)[0][0] for idx in (np.concatenate([np.where(dd == d_)[0] for d_ in rng.choice(ud, size=len(ud), replace=True)]) for _ in range(600))]
    print(f"  money centre - F = {b[0]:+.2f} [{np.percentile(bs, 2.5):+.2f}, {np.percentile(bs, 97.5):+.2f}] x ({name} - F) {b[1]:+.2f}; R^2 {1 - (y - A @ b).var() / y.var():.3f}; n {len(s1)}")
a = {(r["ev"]["st"], r["ev"]["ed"]): v for r, v in vals["F_v3 (station values, lambda 0.5)"]}
b_ = {(r["ev"]["st"], r["ev"]["ed"]): v for r, v in vals["research F (Open-Meteo ten models)"]}
fmap = {(r["ev"]["st"], r["ev"]["ed"]): r["f"] for r in rows}
both = [(a[k] - fmap[k], b_[k] - fmap[k]) for k in a if k in b_]
if both:
    x = np.array(both)
    print(f"  (F_v3 - F) vs (research F - F) on {len(both)} picks: corr {np.corrcoef(x[:, 0], x[:, 1])[0, 1]:.2f}, mean diff {np.mean(x[:, 0] - x[:, 1]):+.2f} C, sd of diff {np.std(x[:, 0] - x[:, 1]):.2f}")
print(f"  fleet c re-estimated on station values: {[(d.isoformat(), round(v, 2)) for d, v in sorted(_C3.items()) if v is not None][:3]} ... (production c on grid values {prod_f(rows[0]['ev']['ts'])['fleet']['c']:+.2f})")
