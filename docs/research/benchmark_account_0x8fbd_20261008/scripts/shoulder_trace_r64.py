"""Round 64 (PROTOCOL_R64_SHOULDER_TRACE.md, amended 5dd17c7e): where the deterministic chain loses the shoulder.

Every fleet station-day, event days 09-28..10-14 with settled truth.  At the listing instant L
(gamma: earliest acceptingOrdersTimestamp) the production listing F is recomputed as of L
(listing_forecast_f.compute, truths cut at L's date - 2).  Layers, each a point value:
  D0  each system's raw maximum            D0c  the same + its own station correction (bias_c)
  D1  raw median  ->  D2 + fleet shift c  ->  D3 + lambda x station correction (f_base)  ->  D4 + recal (f_c)
  D5  F_v3 station values (v3.f_c), D5r v3.f_recal_c
  DV  member votes: corrected members slid so their median sits at F; above minus below F's bucket
Views: common reference (side of F's bucket fb) and own bucket (each layer's own bucket b_s).
Usage: shoulder_trace_r64.py <bench> [--build]
"""
import collections
import json
import math
import re
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from lib import CITY2ST  # noqa: E402

B = Path(sys.argv[1])
B.mkdir(parents=True, exist_ok=True)
BUILD = "--build" in sys.argv
D_FROM, D_TO = date(2026, 9, 28), date(2026, 10, 14)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def bounds(label):
    def f2c(v):
        return (v - 32.0) * 5.0 / 9.0
    m = re.match(r"^(-?\d+)°([CF]) or below$", label)
    if m:
        v = float(m.group(1)) + 0.5
        return (None, v if m.group(2) == "C" else f2c(v)), m.group(2)
    m = re.match(r"^(-?\d+)°([CF]) or higher$", label)
    if m:
        v = float(m.group(1)) - 0.5
        return (v if m.group(2) == "C" else f2c(v), None), m.group(2)
    m = re.match(r"^(-?\d+)-(-?\d+)°F$", label)
    if m:
        return (f2c(float(m.group(1)) - 0.5), f2c(float(m.group(2)) + 0.5)), "F"
    m = re.match(r"^(-?\d+)°C$", label)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5), "C"
    return None, None


GPATH, FPATH = B / "gamma_r64.json", B / "f_r64.json"
if BUILD:
    client = httpx.Client(timeout=30.0)
    gam = json.load(open(GPATH)) if GPATH.exists() else {}
    keys = [f"{st}|{(D_FROM + timedelta(days=k)).isoformat()}" for st in sorted(set(CITY2ST.values()))
            for k in range((D_TO - D_FROM).days + 1)]

    def gamma(key):
        st, ed = key.split("|")
        city = next(c for c, s in CITY2ST.items() if s == st)
        y, m, d = (int(x) for x in ed.split("-"))
        slug = f"highest-temperature-in-{city}-on-{MONTHS[m - 1]}-{d}-{y}"
        for i in range(4):
            try:
                ev = client.get("https://gamma-api.polymarket.com/events", params={"slug": slug}).json()
                break
            except Exception:
                time.sleep(2 + 2 * i)
        else:
            return key, {"error": "fetch"}
        if not ev:
            return key, {"error": "no_event"}
        labels, listed = [], []
        for mk in ev[0].get("markets", []):
            labels.append(mk["groupItemTitle"])
            if mk.get("acceptingOrdersTimestamp"):
                listed.append(mk["acceptingOrdersTimestamp"])
        return key, {"labels": labels, "listed": min(listed) if listed else None}

    with ThreadPoolExecutor(max_workers=8) as pool:
        for k, v in pool.map(gamma, [k for k in keys if k not in gam or "error" in gam[k]]):
            gam[k] = v
    json.dump(gam, open(GPATH, "w"))
    log(f"gamma ok {sum(1 for v in gam.values() if 'error' not in v and v.get('listed'))} of {len(gam)}")

    from bayes_basket_target import listing_forecast_f as F  # noqa: E402
    from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402
    ff = json.load(open(FPATH)) if FPATH.exists() else {}
    ALL_TRUTHS = F.formal_truths(conn, since=date(2026, 6, 1), until=date(2026, 12, 31))
    ROSTER = global_roster(conn)
    need = collections.defaultdict(list)
    for k, v in gam.items():
        if "error" in v or not v.get("listed") or k in ff:
            continue
        t = int(datetime.fromisoformat(v["listed"].replace("Z", "+00:00")).timestamp())
        need[t // 600].append((k, t))
    t0 = time.time()
    for _key, items in sorted(need.items()):
        if time.time() - t0 > 520:
            break
        t = max(tt for _k, tt in items)
        now = datetime.fromtimestamp(t, timezone.utc)
        cut = (now.date() - timedelta(days=2)).isoformat()
        F.formal_truths = lambda c, since, until, cut=cut: {kk: vv for kk, vv in ALL_TRUTHS.items()
                                                             if since.isoformat() <= kk[1] <= min(until.isoformat(), cut)}
        payload = F.compute(conn, now=now, roster=ROSTER)
        for k, _tt in items:
            ff[k] = (payload["station_days"].get(k) or {"state": "ABSENT"}) | {"at": t}
        json.dump(ff, open(FPATH, "w"))
    left = sum(len(v) for v in need.values()) - sum(1 for v in need.values() for k, _t in v if k in ff)
    log(f"F at listing: station-days {len(ff)}, still to do {left}, {time.time() - t0:.0f} s")
    if left:
        sys.exit(3)
    sys.exit(0)

# ------------------------------------------------------------------ analysis
gam, ff = json.load(open(GPATH)), json.load(open(FPATH))
truth = {}
for st, ed, b in conn.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE event_date BETWEEN ? AND ? AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
        " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)", (D_FROM.isoformat(), D_TO.isoformat())):
    truth[f"{st}|{ed}"] = b

SHORT = {"ECMWF_AIFS_SINGLE_POINT": "AIFS", "ECMWF_IFS_HRES_NATIVE_POINT": "HRES", "NOAA_GFS_025_NATIVE_POINT": "GFS",
         "NOAA_AIGFS_025_NATIVE_POINT": "AIGFS", "ECCC_GDPS_15KM_NATIVE_POINT": "GDPS"}


def short(sysid):
    return SHORT.get(sysid) or ("ICON" if "ICON" in sysid else sysid)


rows = []
for k, e in ff.items():
    g = gam.get(k)
    if e.get("state") != "READY" or not g or k not in truth:
        continue
    lad = []
    for lab in g["labels"]:
        bd, unit = bounds(lab)
        if bd is None:
            lad = None
            break
        lad.append((bd, lab, unit))
    if not lad or truth[k] not in [x[1] for x in lad]:
        continue
    lad.sort(key=lambda x: x[0][0] if x[0][0] is not None else -1e9)
    w = 1.0 if lad[0][2] == "C" else 10.0 / 9.0
    edges = [[lo, hi] for (lo, hi), _l, _u in lad]
    if edges[0][0] is None:
        edges[0][0] = edges[0][1] - w
    if edges[-1][1] is None:
        edges[-1][1] = edges[-1][0] + w
    y = [x[1] for x in lad].index(truth[k])
    layers = {}
    for sysid, v in e["systems"].items():
        layers[f"D0 {short(sysid)}"] = v["tmax_c"]
        layers[f"D0c {short(sysid)}"] = v["tmax_c"] + v.get("bias_c", 0.0)
    layers["D1 raw median"] = e["raw_median_c"]
    layers["D2 +fleet c"] = e["raw_median_c"] + e["fleet_c"]
    layers["D3 +lam*station (f_base)"] = e["f_base_c"]
    layers["D4 +recal (F)"] = e["f_c"]
    v3 = e.get("v3") or {}
    if v3.get("state") == "READY":
        layers["D5 v3 station values"] = v3["f_c"]
        if v3.get("f_recal_c") is not None:
            layers["D5r v3 recal"] = v3["f_recal_c"]
    cor = [v["tmax_c"] + v.get("bias_c", 0.0) for v in e["systems"].values()]
    rows.append({"k": k, "ed": k.split("|")[1], "edges": edges, "w": w, "y": y, "layers": layers, "cor": cor,
                 "n_sys": len(cor), "f": e["f_c"]})


def bucket(edges, x):
    if x < edges[0][1]:
        return 0
    for i, (lo, hi) in enumerate(edges):
        if lo <= x < hi:
            return i
    return len(edges) - 1


rng = np.random.default_rng(64)


def boot_mean(vals, eds, draws=2000):
    vals, eds = np.asarray(vals, float), np.asarray(eds)
    ud = sorted(set(eds))
    sums = np.array([vals[eds == d].sum() for d in ud])
    cnts = np.array([(eds == d).sum() for d in ud])
    idx = rng.integers(0, len(ud), size=(draws, len(ud)))
    bs = sums[idx].sum(1) / cnts[idx].sum(1)
    return float(vals.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def ci(t, pct=True):
    p, lo, hi = t
    return f"{p:.1%} [{lo:.1%}, {hi:.1%}]" if pct else f"{p:+.3f} [{lo:+.3f}, {hi:+.3f}]"


for r in rows:
    r["fb"] = bucket(r["edges"], r["f"])
    lo, hi = r["edges"][r["fb"]]
    r["fc"] = (lo + hi) / 2
    r["cls"] = "on" if r["y"] == r["fb"] else ("shoulder" if abs(r["y"] - r["fb"]) == 1 else "far")
    r["tside"] = int(np.sign(r["y"] - r["fb"]))
    shift = r["f"] - statistics.median(r["cor"])
    cen = [c + shift for c in r["cor"]]
    up = sum(bucket(r["edges"], c) > r["fb"] for c in cen)
    dn = sum(bucket(r["edges"], c) < r["fb"] for c in cen)
    r["votes"] = (up, dn)

n = len(rows)
cc = collections.Counter(r["cls"] for r in rows)
print(f"station-days {n} (event days {min(r['ed'] for r in rows)}..{max(r['ed'] for r in rows)}, {len({r['ed'] for r in rows})} days);"
      f" systems per F {dict(sorted(collections.Counter(r['n_sys'] for r in rows).items()))}")
print(f"truth vs pre-listing F bucket: on F {cc['on']} ({cc['on'] / n:.1%}), shoulder {cc['shoulder']} ({cc['shoulder'] / n:.1%}),"
      f" far {cc['far']} ({cc['far'] / n:.1%});  shoulder warm {sum(r['cls'] == 'shoulder' and r['tside'] > 0 for r in rows)}"
      f" / cold {sum(r['cls'] == 'shoulder' and r['tside'] < 0 for r in rows)}")

SH = [r for r in rows if r["cls"] == "shoulder"]
OFF = [r for r in rows if r["cls"] != "on"]
names = sorted({k for r in rows for k in r["layers"]}, key=lambda s: (s.split()[0], s))

print("\n== (1) common reference: does the layer put the truth on the right side of F's bucket?  (shoulder rows; then all off-F rows)")
print("   ties (x exactly at the bucket centre) count as wrong; 50% = coin")
acc_by = {}
for nm in names:
    have = [r for r in SH if nm in r["layers"]]
    ok = [float(np.sign(r["layers"][nm] - r["fc"]) == r["tside"]) for r in have]
    have2 = [r for r in OFF if nm in r["layers"]]
    ok2 = [float(np.sign(r["layers"][nm] - r["fc"]) == r["tside"]) for r in have2]
    acc_by[nm] = {r["k"]: o for r, o in zip(have, ok)}
    print(f"   {nm:28s} shoulder n {len(have):3d}: {ci(boot_mean(ok, [r['ed'] for r in have]))}   all off-F n {len(have2):3d}: {ci(boot_mean(ok2, [r['ed'] for r in have2]))}")
vote_ok, vote_ls, vote_n = [], [], []
for r in SH:
    up, dn = r["votes"]
    s = ((up if r["tside"] > 0 else dn) + 0.5) / (up + dn + 1.0)
    vote_ls.append(-math.log(s))
    vote_ok.append(float(np.sign(up - dn) == r["tside"]))
print(f"   {'DV member votes':28s} shoulder n {len(SH):3d}: {ci(boot_mean(vote_ok, [r['ed'] for r in SH]))}  (ties up=down {sum(r['votes'][0] == r['votes'][1] for r in SH)});"
      f" shoulder split -ln s {statistics.mean(vote_ls):.3f} vs coin 0.693")

print("\n== (2) paired steps along the chain (shoulder rows, common reference): accuracy change, event-day block bootstrap")
CHAIN = ["D1 raw median", "D2 +fleet c", "D3 +lam*station (f_base)", "D4 +recal (F)", "D5 v3 station values", "D5r v3 recal"]
for a, b in zip(CHAIN, CHAIN[1:]):
    ks = [r for r in SH if r["k"] in acc_by[a] and r["k"] in acc_by[b]]
    d = [acc_by[b][r["k"]] - acc_by[a][r["k"]] for r in ks]
    print(f"   {a:26s} -> {b:26s} n {len(ks)}: {ci(boot_mean(d, [r['ed'] for r in ks]), pct=False)}")
for nm in [x for x in names if x.startswith("D0 ")]:
    ks = [r for r in SH if r["k"] in acc_by[nm] and r["k"] in acc_by.get(nm.replace("D0 ", "D0c "), {})]
    d = [acc_by[nm.replace("D0 ", "D0c ")][r["k"]] - acc_by[nm][r["k"]] for r in ks]
    if ks:
        print(f"   {nm:26s} -> its station correction  n {len(ks)}: {ci(boot_mean(d, [r['ed'] for r in ks]), pct=False)}")

print("\n== (3) F (D4) side accuracy on shoulder rows by distance from F's bucket centre (|F - centre| / width)")
for lo, hi in ((0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.51)):
    g = [r for r in SH if lo <= abs(r["f"] - r["fc"]) / r["w"] < hi]
    if g:
        print(f"   [{lo:.1f}, {hi:.1f}) n {len(g):3d}: {statistics.mean(float(np.sign(r['f'] - r['fc']) == r['tside']) for r in g):.1%}"
              f"   (share of shoulder rows {len(g) / len(SH):.0%}; truth on F among all rows in this band"
              f" {statistics.mean(r['cls'] == 'on' for r in rows if lo <= abs(r['f'] - r['fc']) / r['w'] < hi):.1%})")

print("\n== (4) own bucket per layer: hit, within one, and side accuracy when truth is on the layer's own shoulder")
for nm in names:
    have = [r for r in rows if nm in r["layers"]]
    bs = [bucket(r["edges"], r["layers"][nm]) for r in have]
    hit = [float(b == r["y"]) for b, r in zip(bs, have)]
    w1 = [float(abs(b - r["y"]) <= 1) for b, r in zip(bs, have)]
    sh = [(r, b) for b, r in zip(bs, have) if abs(r["y"] - b) == 1]
    side = [float(np.sign(r["layers"][nm] - sum(r["edges"][b]) / 2) == np.sign(r["y"] - b)) for r, b in sh]
    eds = [r["ed"] for r in have]
    print(f"   {nm:28s} n {len(have):3d}: hit {ci(boot_mean(hit, eds))}  within one {statistics.mean(w1):.1%}"
          f"  own-shoulder rows {len(sh) / len(have):.0%}, side right {statistics.mean(side) if side else float('nan'):.1%}")
