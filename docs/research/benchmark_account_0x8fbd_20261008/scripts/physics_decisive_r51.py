"""Round 51 (熊先生 10-12 「测」): the decisive test of "the cluster picks F+1 / F-1
on physical evidence (cloud, rain, radiation, humidity, wind)".

If the cluster corrects F with physics it reads, then a physics-corrected F --
fitted on the TRUTH, nothing of the cluster in it -- must be both more
accurate than F and more cluster-like than F.  Either failing refutes it.
Physics, all already captured (no Open-Meteo quota spent):
  DP   charter 266's direction physics (``direction_physics_v1.json``): six
       models (ECMWF, GFS, ICON, JMA, ARPEGE, UKMO) daytime-mean shortwave,
       dew point, wind; previous_day2 (~48 h, around the listing; ``as-of``
       arm) and previous_day1 (~24 h, after the listing; ``ceiling`` arm).
  C208 charter 208 lane (``calibration_station_forecast_physics_version``,
       ``..._precip_hourly_version``): daily cloud mean, shortwave sum, wind
       max, precip sum, wet hours, rain before the peak hour -- the capture
       made before the listing instant only.
  R49  round 49's ECMWF/GFS day3 cloud / shortwave / precip.
  CURV the native runs F itself used (knowable at the listing): the hourly
       temperature curve's diurnal range, peak local hour, plateau width,
       morning rise, members' disagreement in peak hour.
Universe A = every first-rows station-day with a truth and F at listing+5 min
(the fit); universe B = the 227 pre-formation first YES (the cluster test).
(1) truth - F regressed on each family and on all, leave one event day out:
    MAE, bucket hits vs F.  (2) the same corrected F on the cluster's picks:
    agreement vs F; on the picks where the cluster left F, whether the
    correction moved toward the cluster's bucket, and who was right there.
(3) per-feature F+1 vs F / F-1 vs F contrasts (within-day permutation, BH).
(4) comparison arms on the same picks: construction leans (S3), the ensemble
    tilt (round 50), and the oracle bucket(truth).
"""
import collections
import json
import math
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("skew_mean_median_mode_r46.py").read_text()
ns = {"__name__": "r46", "__file__": str(here.with_name("skew_mean_median_mode_r46.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("days = sorted({r[\"ev\"][\"ed\"] for r in rows})")[0], "r46_head", "exec"), ns)
rows, bucket, recentre = ns["rows"], ns["bucket"], ns["recentre"]
r45 = ns["ns"]
prod_f, XN, FR_ALL = r45["prod_f"], r45["XN"], r45["FR_ALL"]
edges_of = r45["ns"]["edges_of"]
conn = r45["ns"]["ns"]["conn"]
ALL_TRUTHS = r45["ns"]["ns"]["ALL_TRUTHS"]
ROSTER = {s: (lat, lon, z) for s, lat, lon, z in r45["ns"]["ns"]["ROSTER"]}
src49 = here.with_name("physics_vs_cluster_r49.py").read_text()
ns49 = {"B": B, "json": json, "math": math, "statistics": statistics, "date": date, "timedelta": timedelta}
exec(compile(src49[src49.index("PH = json.load"):src49.index("def paired")] + src49[src49.index("def day_value"):src49.index("KEYS = [")],
             "r49_defs", "exec"), ns49)
phys49 = ns49["phys"]
DP = json.load(open("/Users/xiong/weather/data/runtime/direction_physics_v1.json"))["stations"]
DP_MODELS = ("ecmwf_ifs025", "gfs_global", "icon_global", "jma_gsm", "meteofrance_arpege_world", "ukmo_global_deterministic_10km")
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS  # noqa: E402


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- feature builders
def dp_features(st, D, lag):
    """lag 1 = previous_day2 (~48 h), lag 0 = previous_day1 (~24 h)."""
    daily = DP.get(st, {}).get("daily", {})
    out = {}
    for var, short in (("shortwave_radiation", "sw"), ("dew_point_2m", "dew"), ("wind_speed_10m", "wind")):
        vals, anoms = [], []
        for m in DP_MODELS:
            series = daily.get(f"{var}|{m}", {})
            v = (series.get(D.isoformat()) or [None, None])[lag]
            if v is None:
                continue
            vals.append(v)
            prev = [(series.get((D - timedelta(days=k)).isoformat()) or [None, None])[lag] for k in range(1, 8)]
            prev = [p for p in prev if p is not None]
            if len(prev) >= 4:
                anoms.append(v - statistics.mean(prev))
            if m == "icon_global" and short == "sw":
                out["DP icon sw"] = v
        if len(vals) < 3:
            return None
        out[f"DP {short}"] = statistics.mean(vals)
        out[f"DP {short} anom"] = statistics.mean(anoms) if anoms else 0.0
        out[f"DP {short} spread"] = statistics.pstdev(vals)
    return out


def c208_features(st, D, listing):
    row = conn.execute("SELECT cloudcover_mean, shortwave_radiation_sum, windspeed_10m_max, precipitation_sum FROM"
                       " calibration_station_forecast_physics_version WHERE station_id=? AND local_date=? AND captured_at_utc<=?"
                       " ORDER BY captured_at_utc DESC LIMIT 1", (st, D.isoformat(), iso(listing))).fetchone()
    if not row or any(v is None for v in row[:3]):
        return None
    out = {"C208 cloud": float(row[0]), "C208 sw sum": float(row[1]), "C208 wind max": float(row[2]), "C208 precip": math.log1p(float(row[3] or 0.0))}
    pr = conn.execute("SELECT wet_hours, precip_before_peak_mm, peak_hour FROM calibration_station_forecast_precip_hourly_version"
                      " WHERE station_id=? AND local_date=? AND captured_at_utc<=? ORDER BY captured_at_utc DESC LIMIT 1",
                      (st, D.isoformat(), iso(listing))).fetchone()
    if pr:
        out["C208 wet hours"] = float(pr[0] or 0)
        out["C208 rain before peak"] = math.log1p(float(pr[1] or 0.0))
    return out


def curve_features(st, D, zone, listing):
    tz = ZoneInfo(zone)
    feats = []
    for system in sorted(F_SYSTEM_IDS):
        row = conn.execute("SELECT hourly_c_json FROM calibration_regional_point_run WHERE station_id=? AND target_local_date=?"
                           " AND ensemble_system_id=? AND knowable_at_utc<=? ORDER BY run_init_utc DESC LIMIT 1",
                           (st, D.isoformat(), system, iso(listing))).fetchone()
        if not row:
            continue
        pts = []
        for stamp, v in json.loads(row[0]).items():
            t = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(tz)
            if t.date() == D and v is not None:
                pts.append((t.hour + t.minute / 60, float(v)))
        if len(pts) < 5:
            continue
        pts.sort()
        hours = np.array([h for h, _v in pts])
        temps = np.array([v for _h, v in pts])
        if hours.min() > 9 or hours.max() < 15:
            continue
        peak_i = int(np.argmax(temps))
        tmax = temps[peak_i]
        morning = temps[(hours >= 5) & (hours <= 9)]
        midday = temps[(hours >= 11) & (hours <= 13)]
        feats.append({"range": tmax - temps.min(), "peak hour": hours[peak_i],
                      "plateau h": float(hours[temps >= tmax - 0.5].max() - hours[temps >= tmax - 0.5].min()) if (temps >= tmax - 0.5).sum() > 1 else 0.0,
                      "rise": (midday.mean() - morning.mean()) if len(morning) and len(midday) else 0.0})
    if len(feats) < 3:
        return None
    out = {f"CURV {k}": statistics.mean(f[k] for f in feats) for k in ("range", "peak hour", "plateau h", "rise")}
    out["CURV peak hour spread"] = statistics.pstdev([f["peak hour"] for f in feats])
    return out


def all_features(st, D, zone, listing):
    out = {}
    fams = {}
    for fam, f in (("DP", dp_features(st, D, 1)), ("DP1", dp_features(st, D, 0)), ("C208", c208_features(st, D, listing)),
                   ("CURV", curve_features(st, D, zone, listing))):
        if f:
            fams[fam] = f
            out.update({(k if fam != "DP1" else k.replace("DP ", "DP1 ")): v for k, v in f.items()})
    p49 = phys49(st, D, "previous_day3")
    if p49:
        fams["R49"] = {f"R49 {k}": p49[k] for k in ("cloud", "sw", "precip", "cloud_anom", "sw_anom", "precip_anom", "cloud_chg", "sw_chg")}
        out.update(fams["R49"])
    return out, set(fams)


# ---------------------------------------------------------------- universe A: truth
A = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5 or r["st"] not in ROSTER:
        continue
    listing = datetime.fromtimestamp(int(r["listed"]), timezone.utc)
    e = prod_f(int(r["listed"]) + 300)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    D = date.fromisoformat(r["ed"])
    feats, fams = all_features(r["st"], D, ROSTER[r["st"]][2], listing)
    A.append({"st": r["st"], "ed": r["ed"], "f": e["f_c"], "y": float(r["truth_c"]), "edges": edges_of(r), "x": feats, "fams": fams})
daysA = sorted({a["ed"] for a in A})
FAMILIES = {
    "DP (6 models sw/dew/wind, ~48 h)": lambda k: k.startswith("DP "),
    "DP1 ceiling (~24 h, after the trade)": lambda k: k.startswith("DP1 "),
    "C208 (cloud/sw/wind/rain, as-of capture)": lambda k: k.startswith("C208 "),
    "R49 (ECMWF+GFS cloud/sw/rain day3)": lambda k: k.startswith("R49 "),
    "CURV (native curve shape)": lambda k: k.startswith("CURV "),
    "ALL as-of (DP + C208 + R49 + CURV)": lambda k: not k.startswith("DP1 "),
    "ALL + ceiling": lambda k: True,
}
all_keys = sorted({k for a in A for k in a["x"]})
print(f"universe A: {len(A)} station-days, {len(daysA)} event days; feature coverage: "
      + ", ".join(f"{fam} {sum(1 for a in A if any(sel(k) for k in a['x']))}" for fam, sel in FAMILIES.items() if "ALL" not in fam))


def fit_predict(train, test, keys, lam=5.0):
    """Ridge of truth - F on standardised features (missing -> training mean); returns predictions for test."""
    Xtr = np.array([[a["x"].get(k, np.nan) for k in keys] for a in train], dtype=float)
    mu = np.nanmean(Xtr, 0)
    mu = np.where(np.isnan(mu), 0.0, mu)
    Xtr = np.where(np.isnan(Xtr), mu, Xtr)
    sd = Xtr.std(0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    Atr = np.c_[(Xtr - mu) / sd, np.ones(len(Xtr))]
    y = np.array([a["y"] - a["f"] for a in train])
    P = lam * np.eye(Atr.shape[1])
    P[-1, -1] = 0
    b = np.linalg.solve(Atr.T @ Atr + P, Atr.T @ y)
    Xte = np.array([[a["x"].get(k, np.nan) for k in keys] for a in test], dtype=float)
    Xte = np.where(np.isnan(Xte), mu, Xte)
    return np.c_[(Xte - mu) / sd, np.ones(len(Xte))] @ b, b


print("\n(1) physics-corrected F on the truth, leave one event day out (universe A)")
base_mae = np.mean([abs(a["y"] - a["f"]) for a in A])
base_hit = [bucket(a["edges"], a["f"]) == bucket(a["edges"], a["y"]) for a in A]
rng = np.random.default_rng(51)
by_day = {d: [i for i, a in enumerate(A) if a["ed"] == d] for d in daysA}
corrected_B = {}
for fam, sel in FAMILIES.items():
    keys = [k for k in all_keys if sel(k)]
    pred = np.zeros(len(A))
    for d in daysA:
        tr = [a for a in A if a["ed"] != d]
        te = [a for a in A if a["ed"] == d]
        p, _b = fit_predict(tr, te, keys)
        pred[by_day[d]] = p
    f2 = [a["f"] + pred[i] for i, a in enumerate(A)]
    dm = np.array([abs(a["y"] - x) - abs(a["y"] - a["f"]) for a, x in zip(A, f2)])
    bs = [dm[np.concatenate([by_day[dd] for dd in rng.choice(daysA, size=len(daysA), replace=True)])].mean() for _ in range(600)]
    hit = [bucket(a["edges"], x) == bucket(a["edges"], a["y"]) for a, x in zip(A, f2)]
    print(f"  {fam:42s} ({len(keys):2d} features): MAE {np.mean([abs(a['y'] - x) for a, x in zip(A, f2)]):.4f} vs F {base_mae:.4f}"
          f" ({dm.mean():+.4f} [{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]); hits {statistics.mean(hit):.1%} vs F {statistics.mean(base_hit):.1%} {paired(hit, base_hit)};"
          f" |correction| mean {np.abs(pred).mean():.2f} C")

# ---------------------------------------------------------------- universe B: the cluster
Bp = []
for r in rows:
    ev = r["ev"]
    D = date.fromisoformat(ev["ed"])
    listing = datetime.fromtimestamp(ev["listed"], timezone.utc)
    feats, fams = all_features(ev["st"], D, ROSTER[ev["st"]][2], listing)
    Bp.append({"r": r, "x": feats, "ed": ev["ed"]})
print(f"\n(2) the same corrected F on the cluster's {len(Bp)} picks (fit on universe A without the pick's event day)")
left = [b for b in Bp if b["r"]["ev"]["choice"] != b["r"]["ev"]["fb"]]
print(f"  picks where the cluster left F: {len(left)}; there the truth = cluster's bucket {statistics.mean(b['r']['ev']['truth'] == b['r']['ev']['choice'] for b in left if b['r']['ev']['truth'] is not None):.1%},"
      f" = F's bucket {statistics.mean(b['r']['ev']['truth'] == b['r']['ev']['fb'] for b in left if b['r']['ev']['truth'] is not None):.1%}")
for fam, sel in FAMILIES.items():
    keys = [k for k in all_keys if sel(k)]
    pred = np.zeros(len(Bp))
    for d in sorted({b["ed"] for b in Bp}):
        tr = [a for a in A if a["ed"] != d]
        te = [{"x": b["x"], "f": b["r"]["f"]} for b in Bp if b["ed"] == d]
        p, _b = fit_predict(tr, te, keys)
        for i, j in enumerate([i for i, b in enumerate(Bp) if b["ed"] == d]):
            pred[j] = p[i]
    got = [bucket(b["r"]["ev"]["edges"], b["r"]["f"] + pred[i]) == b["r"]["ev"]["choice"] for i, b in enumerate(Bp)]
    base = [b["r"]["ev"]["fb"] == b["r"]["ev"]["choice"] for b in Bp]
    # on the picks where the cluster left F: did the correction move toward the cluster?
    toward = sum(1 for i, b in enumerate(Bp) if b["r"]["ev"]["choice"] != b["r"]["ev"]["fb"]
                 and np.sign(pred[i]) == np.sign(b["r"]["ev"]["choice"] - b["r"]["ev"]["fb"]) and abs(pred[i]) > 0.05)
    away = sum(1 for i, b in enumerate(Bp) if b["r"]["ev"]["choice"] != b["r"]["ev"]["fb"]
               and np.sign(pred[i]) == -np.sign(b["r"]["ev"]["choice"] - b["r"]["ev"]["fb"]) and abs(pred[i]) > 0.05)
    reached = sum(1 for i, b in enumerate(Bp) if b["r"]["ev"]["choice"] != b["r"]["ev"]["fb"]
                  and bucket(b["r"]["ev"]["edges"], b["r"]["f"] + pred[i]) == b["r"]["ev"]["choice"])
    print(f"  {fam:42s}: = cluster {statistics.mean(got):.1%} vs F {statistics.mean(base):.1%} {paired(got, base)};"
          f" on the {len(left)} departures: correction toward the cluster {toward}, away {away}, reached its bucket {reached}")

# ---------------------------------------------------------------- (3) per-feature contrasts
print("\n(3) F+1 vs F and F-1 vs F, per physics feature (within-day permutation p, BH q over all tests)")
cls = {id(b): b["r"]["ev"]["choice"] - b["r"]["ev"]["fb"] for b in Bp}
tests = []
for name in all_keys:
    for target in (1, -1):
        sub = [b for b in Bp if cls[id(b)] in (0, target) and name in b["x"]]
        if sum(cls[id(b)] == target for b in sub) < 8:
            continue
        v = np.array([b["x"][name] for b in sub], dtype=float)
        y = np.array([cls[id(b)] == target for b in sub])
        dd = np.array([b["ed"] for b in sub])
        obs = v[y].mean() - v[~y].mean()
        groups = [np.where(dd == d)[0] for d in set(dd)]
        cnt = 0
        for _ in range(2000):
            yp = y.copy()
            for g in groups:
                yp[g] = rng.permutation(y[g])
            if yp.sum() and (~yp).sum():
                cnt += abs(v[yp].mean() - v[~yp].mean()) >= abs(obs) - 1e-12
        a_, b_ = v[y], v[~y]
        auc = float(((a_[:, None] > b_[None, :]).sum() + 0.5 * (a_[:, None] == b_[None, :]).sum()) / (len(a_) * len(b_)))
        tests.append({"name": name, "target": target, "n": int(y.sum()), "n0": int((~y).sum()), "diff": obs / (v.std() or 1.0), "p": (cnt + 1) / 2001, "auc": auc})
m = len(tests)
order = sorted(range(m), key=lambda i: tests[i]["p"])
prev = 1.0
for rank, i in reversed(list(enumerate(order, start=1))):
    prev = min(prev, tests[i]["p"] * m / rank)
    tests[i]["q"] = prev
print(f"  {m} tests; the 15 smallest p (* q < 0.10, + p < 0.05):")
for t in sorted(tests, key=lambda t: t["p"])[:15]:
    flag = "*" if t["q"] < 0.10 else "+" if t["p"] < 0.05 else " "
    print(f"   {flag} {'F+1' if t['target'] == 1 else 'F-1'} vs F  {t['name']:24s} n {t['n']:3d}/{t['n0']:3d}: {t['diff']:+.2f} sd, AUC {t['auc']:.2f}, p {t['p']:.4f}, q {t['q']:.3f}")
print(f"  physics tests with q < 0.10: {sum(t['q'] < 0.10 for t in tests)} of {m}; with p < 0.05: {sum(t['p'] < 0.05 for t in tests)} (expected by chance ~{0.05 * m:.1f})")

# ---------------------------------------------------------------- (4) comparison arms
print("\n(4) comparison arms on the same picks")
base = [b["r"]["ev"]["fb"] == b["r"]["ev"]["choice"] for b in Bp]
tr_ = [b for b in Bp if b["r"]["ev"]["truth"] is not None]
oracle = [b["r"]["ev"]["truth"] == b["r"]["ev"]["choice"] for b in tr_]
basetr = [b["r"]["ev"]["fb"] == b["r"]["ev"]["choice"] for b in tr_]
print(f"  oracle bucket(truth) = cluster {statistics.mean(oracle):.1%} vs F {statistics.mean(basetr):.1%} {paired(oracle, basetr)} (n {len(tr_)}):"
      f" a perfect forecast matches the cluster less than F does")
# S3 construction leans and ENS@F tilt, LODO on the picks themselves (as in rounds 45f / 50)
s3 = [XN.index(k) for k in ("skew", "station corr", "recal", "F unit")]
daysB = sorted({b["ed"] for b in Bp})
for label, getx in (("S3 construction leans", lambda b: list(b["r"]["x"][s3])),
                    ("ENS@F tilt p(F+1)-p(F-1)", lambda b: [b["r"]["shape"]["ENS@F"]["mean"] - b["r"]["ev"]["fb"]] if "ENS@F" in b["r"]["shape"] else None)):
    sub = [b for b in Bp if getx(b) is not None and "1 h" in b["r"]["win"]]
    y = np.array([b["r"]["win"]["1 h"]["centre"] - b["r"]["f"] for b in sub])
    X = np.array([getx(b) for b in sub], dtype=float)
    pred = np.zeros(len(sub))
    for d in daysB:
        trm = np.array([b["ed"] != d for b in sub])
        if (~trm).sum() == 0:
            continue
        mu, sd = X[trm].mean(0), X[trm].std(0)
        sd = np.where(sd > 1e-9, sd, 1.0)
        Am = np.c_[(X - mu) / sd, np.ones(len(X))]
        P = 5.0 * np.eye(Am.shape[1])
        P[-1, -1] = 0
        bb = np.linalg.solve(Am[trm].T @ Am[trm] + P, Am[trm].T @ y[trm])
        pred[~trm] = Am[~trm] @ bb
    got = [bucket(b["r"]["ev"]["edges"], b["r"]["f"] + pred[i]) == b["r"]["ev"]["choice"] for i, b in enumerate(sub)]
    bs_ = [b["r"]["ev"]["fb"] == b["r"]["ev"]["choice"] for b in sub]
    lf = [(i, b) for i, b in enumerate(sub) if b["r"]["ev"]["choice"] != b["r"]["ev"]["fb"]]
    toward = sum(1 for i, b in lf if np.sign(pred[i]) == np.sign(b["r"]["ev"]["choice"] - b["r"]["ev"]["fb"]) and abs(pred[i]) > 0.05)
    away = sum(1 for i, b in lf if np.sign(pred[i]) == -np.sign(b["r"]["ev"]["choice"] - b["r"]["ev"]["fb"]) and abs(pred[i]) > 0.05)
    print(f"  {label:28s} (fitted on the cluster's own centre, LODO): = cluster {statistics.mean(got):.1%} vs F {statistics.mean(bs_):.1%} {paired(got, bs_)};"
          f" on {len(lf)} departures toward {toward}, away {away}")
