"""Round 54 (熊先生 10-12, the station-calibration thread): the STATION VALUE itself.

Round 53 found no calibration recipe over our native members that reproduces
the cluster's bucket.  Round 39e found the research F built from Open-Meteo's
station values (seven models) matched the cluster 50-51% where the native F
matched 47%, with the same models -- the difference was the point value:
Open-Meteo interpolates the grid to the station and adjusts for elevation;
our native readers take the nearest land cell.  Here, same model, same run,
same station-day: Delta_m = Open-Meteo value - native value (bench
``first_runs.json``, single-runs fetched in round 33 for the listing runs).
(1) Delta per model: mean, sd, per-station stability (halves correlation),
against station elevation.  (2) F with the native raw median replaced by the
Open-Meteo raw median over the common members (same c, same lambda, same
biases): agreement with the cluster's pick and truth hits vs F.  (3) the
cluster's money centre - F regressed on (F_OM - F_native): a coefficient near
1 means its centre moves with the station value; near 0 means not.
"""
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
rows, bucket, prod_f = ns["rows"], ns["bucket"], ns["prod_f"]
conn = ns["ns"]["ns"]["conn"]
ROSTER = {s: (lat, lon, z) for s, lat, lon, z in ns["ns"]["ns"]["ROSTER"]}
ALL_TRUTHS = ns["ns"]["ns"]["ALL_TRUTHS"]
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import listing_forecast_f as F  # noqa: E402

OM = json.load(open(B / "first_runs.json"))
PAIR = {"NOAA_GFS_025_NATIVE_POINT": "gfs_global", "ECMWF_IFS_HRES_NATIVE_POINT": "ecmwf_ifs025",
        "ECMWF_AIFS_SINGLE_POINT": "ecmwf_aifs025_single", "NOAA_AIGFS_025_NATIVE_POINT": "ncep_aigfs025",
        "DWD_ICON_GLOBAL_13KM_NATIVE_POINT": "icon_global", "ECCC_GDPS_15KM_NATIVE_POINT": "gem_global"}


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


def pairs_for(st, ed, as_of, zone):
    """[(system, native tmax, open-meteo tmax)] for the same run."""
    native = F.system_maxima(conn, station_id=st, event_date=date.fromisoformat(ed), as_of_utc=as_of, settlement_timezone=zone)
    out = []
    for system, entry in native.items():
        model = PAIR.get(system)
        if not model:
            continue
        run = str(entry["run_init_utc"])[:16]
        om = OM.get(f"{st}|{run}", {}).get("tmax", {}).get(model, {}).get(ed)
        if om is not None:
            out.append((system, float(entry["tmax_c"]), float(om)))
    return out


# (1) Delta per model over the cluster's station-days (at the trade's as-of) and every roster station-day
print("(1) Open-Meteo station value - native nearest-land-cell value, same model, same run")
deltas = {}
per_station = {}
for r in rows:
    ev = r["ev"]
    as_of = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pr = pairs_for(ev["st"], ev["ed"], as_of, ROSTER[ev["st"]][2])
    r["pairs"] = pr
    for system, nat, om in pr:
        deltas.setdefault(system, []).append(om - nat)
        per_station.setdefault((ev["st"], system), []).append((ev["ed"], om - nat))
for system, d in sorted(deltas.items()):
    d = np.array(d)
    print(f"    {system:36s} n {len(d):3d}: mean {d.mean():+.2f} C, sd {d.std():.2f}, |delta| > 1 C {np.mean(np.abs(d) > 1):.0%}")
# station stability: split each station's deltas by date halves
corr_in = []
for (st, system), lst in per_station.items():
    lst = sorted(lst)
    if len(lst) >= 6:
        h = len(lst) // 2
        corr_in.append((statistics.mean(v for _d, v in lst[:h]), statistics.mean(v for _d, v in lst[h:])))
if len(corr_in) >= 5:
    a = np.array(corr_in)
    print(f"    per station x model, first-half vs second-half mean delta: corr {np.corrcoef(a[:, 0], a[:, 1])[0, 1]:+.2f} over {len(a)} pairs -> "
          + ("a stable station adjustment" if np.corrcoef(a[:, 0], a[:, 1])[0, 1] > 0.6 else "not a stable station constant"))

# (2) F with the Open-Meteo raw median
print("\n(2) F with the native raw median swapped for the Open-Meteo raw median over the common members")
sub = [r for r in rows if len(r["pairs"]) >= 3]
days = sorted({r["ev"]["ed"] for r in sub})
mid = days[len(days) // 2]
for label, fn in (("swap raw median (F + med_OM - med_native)", lambda r: r["f"] + statistics.median(om for _s, _n, om in r["pairs"]) - statistics.median(n for _s, n, _o in r["pairs"])),
                  ("half swap", lambda r: r["f"] + 0.5 * (statistics.median(om for _s, _n, om in r["pairs"]) - statistics.median(n for _s, n, _o in r["pairs"]))),
                  ("Open-Meteo raw median + c only (no station bias)", lambda r: statistics.median(om for _s, _n, om in r["pairs"]) + prod_f(r["ev"]["ts"])["fleet"]["c"])):
    same = [bucket(r["ev"]["edges"], fn(r)) == r["ev"]["choice"] for r in sub]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
    tr = [(fn(r), r) for r in sub if r["ev"]["truth"] is not None]
    hits = statistics.mean(bucket(r["ev"]["edges"], v) == r["ev"]["truth"] for v, r in tr)
    fh = statistics.mean(r["ev"]["fb"] == r["ev"]["truth"] for _v, r in tr)
    h1 = statistics.mean(s for s, r in zip(same, sub) if r["ev"]["ed"] < mid) - statistics.mean(b for b, r in zip(base, sub) if r["ev"]["ed"] < mid)
    h2 = statistics.mean(s for s, r in zip(same, sub) if r["ev"]["ed"] >= mid) - statistics.mean(b for b, r in zip(base, sub) if r["ev"]["ed"] >= mid)
    print(f"    {label:50s} n {len(sub)}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} halves {h1:+.3f}/{h2:+.3f}; hits {hits:.1%} vs F {fh:.1%}")
d_med = np.array([statistics.median(om for _s, _n, om in r["pairs"]) - statistics.median(n for _s, n, _o in r["pairs"]) for r in sub])
print(f"    med_OM - med_native over the picks: mean {d_med.mean():+.2f} C, sd {d_med.std():.2f}")

# (3) the money centre on F_OM - F_native
print("\n(3) the cluster's money centre - F on (med_OM - med_native)")
s1 = [r for r in sub if "1 h" in r["win"]]
x = np.array([statistics.median(om for _s, _n, om in r["pairs"]) - statistics.median(n for _s, n, _o in r["pairs"]) for r in s1])
y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in s1])
A = np.c_[x, np.ones(len(x))]
b = np.linalg.lstsq(A, y, rcond=None)[0]
dd = np.array([r["ev"]["ed"] for r in s1])
rng = np.random.default_rng(54)
ud = sorted(set(dd))
bs = []
for _ in range(800):
    idx = np.concatenate([np.where(dd == d)[0] for d in rng.choice(ud, size=len(ud), replace=True)])
    bs.append(np.linalg.lstsq(A[idx], y[idx], rcond=None)[0][0])
res = y - A @ b
print(f"    n {len(s1)}: centre - F = {b[0]:+.2f} [{np.percentile(bs, 2.5):+.2f}, {np.percentile(bs, 97.5):+.2f}] x (med_OM - med_native) {b[1]:+.2f};"
      f" R^2 {1 - res.var() / y.var():.3f}; residual sd {res.std():.2f} C (was {y.std():.2f})")
# truth as well: does the Open-Meteo value know something about the truth beyond F?
yt = np.array([ALL_TRUTHS[(r["ev"]["st"], r["ev"]["ed"])] - r["f"] for r in s1 if (r["ev"]["st"], r["ev"]["ed"]) in ALL_TRUTHS])
xt = np.array([v for v, r in zip(x, s1) if (r["ev"]["st"], r["ev"]["ed"]) in ALL_TRUTHS])
bt = np.linalg.lstsq(np.c_[xt, np.ones(len(xt))], yt, rcond=None)[0]
print(f"    truth - F = {bt[0]:+.2f} x (med_OM - med_native) {bt[1]:+.2f} (n {len(yt)})")

# ---------------------------------------------------------------- (4) wider: per member, and the research F
print("\n(4) per member (larger n): the money centre - F and the truth - F on that member's (OM - native)")
for system in sorted(deltas):
    xs, ys, yt, dd = [], [], [], []
    for r in rows:
        if "1 h" not in r["win"]:
            continue
        d = [om - nat for s_, nat, om in r["pairs"] if s_ == system]
        if not d:
            continue
        xs.append(d[0])
        ys.append(r["win"]["1 h"]["centre"] - r["f"])
        yt.append(ALL_TRUTHS.get((r["ev"]["st"], r["ev"]["ed"])))
        dd.append(r["ev"]["ed"])
    x, y, dd = np.array(xs), np.array(ys), np.array(dd)
    A = np.c_[x, np.ones(len(x))]
    b = np.linalg.lstsq(A, y, rcond=None)[0]
    ud = sorted(set(dd))
    bs = []
    for _ in range(800):
        idx = np.concatenate([np.where(dd == d_)[0] for d_ in rng.choice(ud, size=len(ud), replace=True)])
        bs.append(np.linalg.lstsq(A[idx], y[idx], rcond=None)[0][0])
    m = np.array([t is not None for t in yt])
    ytt = np.array([t - r_f for t, r_f in zip([t for t in yt if t is not None], [r["f"] for r in rows if "1 h" in r["win"] and any(s_ == system for s_, _n, _o in r["pairs"]) and ALL_TRUTHS.get((r["ev"]["st"], r["ev"]["ed"])) is not None])])
    bt = np.linalg.lstsq(np.c_[x[m], np.ones(m.sum())], ytt, rcond=None)[0] if m.sum() > 5 else [float("nan"), float("nan")]
    print(f"    {system:36s} n {len(x):3d}: centre - F = {b[0]:+.2f} [{np.percentile(bs, 2.5):+.2f}, {np.percentile(bs, 97.5):+.2f}] x delta {b[1]:+.2f}, R^2 {1 - (y - A @ b).var() / y.var():.3f};"
          f"  truth - F = {bt[0]:+.2f} x delta {bt[1]:+.2f}")

# why so few matched members: the native runs at the trade vs the Open-Meteo run keys in hand
import collections  # noqa: E402

miss = collections.Counter()
for r in rows[:80]:
    ev = r["ev"]
    as_of = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    native = F.system_maxima(conn, station_id=ev["st"], event_date=date.fromisoformat(ev["ed"]), as_of_utc=as_of, settlement_timezone=ROSTER[ev["st"]][2])
    for system, entry in native.items():
        if system in PAIR:
            run = str(entry["run_init_utc"])[:16]
            have = f"{ev['st']}|{run}" in OM and PAIR[system] in OM[f"{ev['st']}|{run}"].get("tmax", {})
            miss[(system.split("_")[1], run[11:16], "matched" if have else "no OM run")] += 1
print("    native run hour at the trade x Open-Meteo availability (first 80 picks):", dict(sorted(miss.items())))

# the research F (ten Open-Meteo models, +0.8 +0.3 bias, round 33) on all picks, and the centre on (research - F)
research_c = ns["ns"]["ns"]["research_c"]
FRm = {(r_["st"], r_["ed"]): r_ for r_ in ns["ns"]["FR_ALL"]}
got = []
for r in rows:
    fr = FRm.get((r["ev"]["st"], r["ev"]["ed"]))
    if fr is None:
        continue
    try:
        c = research_c(fr, r["ev"]["ts"])
    except Exception:  # noqa: BLE001
        c = None
    if c is not None:
        got.append((r, c))
print(f"\n(5) the research F (Open-Meteo ten models, raw median + 0.8 + 0.3 x station bias) on {len(got)} picks")
same = [bucket(r["ev"]["edges"], c) == r["ev"]["choice"] for r, c in got]
base = [r["ev"]["fb"] == r["ev"]["choice"] for r, _c in got]
tr = [(r, c) for r, c in got if r["ev"]["truth"] is not None]
daysg = sorted({r["ev"]["ed"] for r, _c in got})
midg = daysg[len(daysg) // 2]
h1 = statistics.mean(s for s, (r, _c) in zip(same, got) if r["ev"]["ed"] < midg) - statistics.mean(b_ for b_, (r, _c) in zip(base, got) if r["ev"]["ed"] < midg)
h2 = statistics.mean(s for s, (r, _c) in zip(same, got) if r["ev"]["ed"] >= midg) - statistics.mean(b_ for b_, (r, _c) in zip(base, got) if r["ev"]["ed"] >= midg)
print(f"    = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} halves {h1:+.3f}/{h2:+.3f};"
      f" hits {statistics.mean(bucket(r['ev']['edges'], c) == r['ev']['truth'] for r, c in tr):.1%} vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for r, _c in tr):.1%}")
s1 = [(r, c) for r, c in got if "1 h" in r["win"]]
x = np.array([c - r["f"] for r, c in s1])
y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r, _c in s1])
A = np.c_[x, np.ones(len(x))]
b = np.linalg.lstsq(A, y, rcond=None)[0]
dd = np.array([r["ev"]["ed"] for r, _c in s1])
ud = sorted(set(dd))
bs = [np.linalg.lstsq(A[idx], y[idx], rcond=None)[0][0] for idx in (np.concatenate([np.where(dd == d_)[0] for d_ in rng.choice(ud, size=len(ud), replace=True)]) for _ in range(800))]
yt = np.array([ALL_TRUTHS[(r["ev"]["st"], r["ev"]["ed"])] - r["f"] for r, _c in s1 if (r["ev"]["st"], r["ev"]["ed"]) in ALL_TRUTHS])
xt = np.array([v for v, (r, _c) in zip(x, s1) if (r["ev"]["st"], r["ev"]["ed"]) in ALL_TRUTHS])
bt = np.linalg.lstsq(np.c_[xt, np.ones(len(xt))], yt, rcond=None)[0]
print(f"    centre - F = {b[0]:+.2f} [{np.percentile(bs, 2.5):+.2f}, {np.percentile(bs, 97.5):+.2f}] x (research F - F) {b[1]:+.2f}, R^2 {1 - (y - A @ b).var() / y.var():.3f}, n {len(s1)};"
      f" research F - F mean {x.mean():+.2f} sd {x.std():.2f};  truth - F = {bt[0]:+.2f} x (research F - F) {bt[1]:+.2f}")
