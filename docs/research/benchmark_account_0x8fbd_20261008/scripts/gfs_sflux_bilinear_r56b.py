"""Round 56b: bilinear interpolation on GFS's native 13 km flux grid (the rule the
user quoted: surrounding grid points, bilinear, lapse-rate height correction, valid-time
matching) against nearest-land + lapse, scored on Open-Meteo's value and on the truth."""
import json, statistics, sys
from datetime import date
from pathlib import Path
import numpy as np
here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("gfs_sflux_grid_r54d.py").read_text()
ns = {"__name__": "r54d", "__file__": str(here.with_name("gfs_sflux_grid_r54d.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
# run the whole 54d (re-downloads ~45 messages) but with a bilinear rule added before the loop
patched = src.replace('RULES = ("nearest", "nearest-land", "similar-elev")', '''
NI = 3072
row_lats = land["lat"][::NI]
_R = {st: (lat, lon, z) for st, lat, lon, z in ROSTER}
for s in stations:
    lat, lon, _z = _R[s["st"]]
    lon360 = lon % 360.0
    # two latitude rows bracketing the station, two columns bracketing its longitude
    above = np.flatnonzero(row_lats >= lat)
    below = np.flatnonzero(row_lats < lat)
    if len(above) == 0 or len(below) == 0:
        s["cells"]["bilinear"] = None
        continue
    r0, r1 = int(above[-1]), int(below[0])
    dlon = 360.0 / NI
    c0 = int(np.floor(lon360 / dlon)) % NI
    c1 = (c0 + 1) % NI
    tlon = (lon360 - c0 * dlon) / dlon
    tlat = (row_lats[r0] - lat) / (row_lats[r0] - row_lats[r1])
    s["cells"]["bilinear"] = [(r0 * NI + c0, (1 - tlat) * (1 - tlon)), (r0 * NI + c1, (1 - tlat) * tlon), (r1 * NI + c0, tlat * (1 - tlon)), (r1 * NI + c1, tlat * tlon)]
RULES = ("nearest-land", "bilinear")''')
patched = patched.replace('''            c = s["cells"][rule]
            if c is None:
                continue
            for lapse in (False, True):
                by_day = {}
                for step, v in fields.items():
                    # TMAX over the 3 h ending at the step: assign to the hour of the step's valid time
                    t = (run + timedelta(hours=step)).astimezone(tz)
                    val = float(v[c]) - 273.15 + (LAPSE * (float(hgt["v"][c]) - s["elev"]) if lapse else 0.0)''', '''            c = s["cells"][rule]
            if c is None:
                continue
            for lapse in (False, True):
                by_day = {}
                for step, v in fields.items():
                    t = (run + timedelta(hours=step)).astimezone(tz)
                    if rule == "bilinear":
                        tv = sum(float(v[i]) * w for i, w in c) - 273.15
                        th = sum(float(hgt["v"][i]) * w for i, w in c)
                    else:
                        tv, th = float(v[c]) - 273.15, float(hgt["v"][c])
                    val = tv + (LAPSE * (th - s["elev"]) if lapse else 0.0)''')
patched = patched.replace('json.dump(pairs, open(B / "gfs_sflux_grid_r54d.json", "w"))', 'json.dump(pairs, open(B / "gfs_sflux_bilinear_r56b.json", "w"))')
exec(compile(patched, "r54d_bilinear", "exec"), ns)
pairs = ns["pairs"]
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import listing_forecast_f as F
T = F.formal_truths(ns["conn"], since=date(2026, 9, 1), until=date(2026, 10, 12))
rows = [p for p in pairs if (p["st"], p["day"]) in T and all(k in p for k in ("nearest-land|1", "bilinear|1", "bilinear|0"))]
print(f"\nagainst the truth, {len(rows)} station-run-days (GFS alone, raw):")
base = np.array([abs(T[(p['st'], p['day'])] - p['native']) for p in rows])
for k in ("native", "nearest-land|0", "nearest-land|1", "bilinear|0", "bilinear|1"):
    e = np.array([T[(p['st'], p['day'])] - p[k] for p in rows]); d = np.abs(e) - base
    print(f"  {k:16s} MAE {np.abs(e).mean():.3f} bias {e.mean():+.3f} |err|<1 {np.mean(np.abs(e) < 1):.0%}  vs native {d.mean():+.3f} +-{1.96 * d.std() / len(d) ** 0.5:.3f}")
e_nl = np.array([abs(T[(p['st'], p['day'])] - p['nearest-land|1']) for p in rows]); e_bl = np.array([abs(T[(p['st'], p['day'])] - p['bilinear|1']) for p in rows])
d = e_bl - e_nl
print(f"  bilinear + lapse vs nearest-land + lapse: MAE {d.mean():+.3f} +-{1.96 * d.std() / len(d) ** 0.5:.3f}")
