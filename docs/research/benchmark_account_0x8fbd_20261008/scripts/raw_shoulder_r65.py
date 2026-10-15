"""Round 65 (PROTOCOL_R65_RAW_SHOULDER.md): shoulder side information in the raw deterministic runs beyond F's position.

Usage: raw_shoulder_r65.py <bench> build   -- gamma ladders, F at each listing instant, per-run history (resumable, 520 s per call)
       raw_shoulder_r65.py <bench> score
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
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from lib import CITY2ST  # noqa: E402
from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import (  # noqa: E402
    FAMILY, F_SYSTEM_IDS, PRODUCT_BY_SYSTEM, day_maximum, global_roster)
from bayes_basket_target.regional_point_lane import runs_for  # noqa: E402

B = Path(sys.argv[1])
B.mkdir(parents=True, exist_ok=True)
MODE = sys.argv[2]
D_FROM, D_TO = date(2026, 9, 12), date(2026, 10, 14)
HIST_FROM = date(2026, 8, 13)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
ROSTER = global_roster(conn)
ZONE = {st: z for st, _la, _lo, z in ROSTER}
STATIONS = sorted(set(CITY2ST.values()) & set(ZONE))
ALL_TRUTHS = F.formal_truths(conn, since=date(2026, 6, 1), until=date(2026, 12, 31))
EXTRA = sorted(s for s in PRODUCT_BY_SYSTEM if s not in F_SYSTEM_IDS)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def days(a, b):
    return [a + timedelta(days=k) for k in range((b - a).days + 1)]


def step_of(sysid):
    return int(FAMILY[PRODUCT_BY_SYSTEM[sysid]]["step_hours"])


def run_series(st, d, as_of):
    """{system: [(run_init, day_max, curve_local[(hour, value)])...] newest first} for runs knowable at as_of."""
    out = collections.defaultdict(list)
    zone = ZoneInfo(ZONE[st])
    for row in runs_for(conn, station_id=st, event_date=d.isoformat(), as_of_utc=as_of):
        sysid = row["ensemble_system_id"]
        if sysid not in PRODUCT_BY_SYSTEM:
            continue
        v, ok = day_maximum(row["hourly_c"], event_date=d, settlement_timezone=ZONE[st], step_hours=step_of(sysid))
        if v is None or not ok or any(r[0] == row["run_init_utc"] for r in out[sysid]):
            continue
        curve = []
        for stamp, val in row["hourly_c"].items():
            t = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(zone)
            if t.date() == d:
                curve.append((t.hour + t.minute / 60, float(val)))
        out[sysid].append((row["run_init_utc"], float(v), sorted(curve)))
    for s in out:
        out[s].sort(key=lambda r: r[0], reverse=True)
    return out


# =============================================================== build
if MODE == "build":
    t0 = time.time()
    GP = B / "gamma_r65.json"
    gam = json.load(open(GP)) if GP.exists() else {}
    import httpx
    client = httpx.Client(timeout=30.0)

    def gamma(key):
        st, ed = key.split("|")
        city = next(c for c, s in CITY2ST.items() if s == st)
        y, m, d = (int(x) for x in ed.split("-"))
        for i in range(4):
            try:
                ev = client.get("https://gamma-api.polymarket.com/events",
                                params={"slug": f"highest-temperature-in-{city}-on-{MONTHS[m - 1]}-{d}-{y}"}).json()
                break
            except Exception:
                time.sleep(2 + 2 * i)
        else:
            return key, {"error": "fetch"}
        return key, ({"labels": [mk["groupItemTitle"] for mk in ev[0].get("markets", [])]} if ev else {"error": "no_event"})

    todo = [f"{st}|{d.isoformat()}" for st in STATIONS for d in days(D_FROM, D_TO) if f"{st}|{d.isoformat()}" not in gam]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for k, v in pool.map(gamma, todo):
            gam[k] = v
    json.dump(gam, open(GP, "w"))
    log(f"gamma {sum('labels' in v for v in gam.values())} of {len(gam)}")

    FP = B / "f_r65.json"
    ff = json.load(open(FP)) if FP.exists() else {}
    for d in days(D_FROM, D_TO):
        if d.isoformat() in ff or time.time() - t0 > 480:
            continue
        L = F.listing_instant(d)
        now = datetime.fromisoformat(L.replace("Z", "+00:00"))
        cut = (now.date() - timedelta(days=2)).isoformat()
        F.formal_truths = lambda c, since, until, cut=cut: {k: v for k, v in ALL_TRUTHS.items()
                                                             if since.isoformat() <= k[1] <= min(until.isoformat(), cut)}
        payload = F.compute(conn, now=now, roster=ROSTER)
        ff[d.isoformat()] = {k.split("|")[0]: {kk: vv for kk, vv in v.items() if kk != "v3"}
                             for k, v in payload["station_days"].items() if k.endswith(d.isoformat())}
        json.dump(ff, open(FP, "w"))
    log(f"F days {len(ff)} of {len(days(D_FROM, D_TO))}")

    # per station-day at its own listing: every run of every system (newest first), plus the day before (G4)
    HP = B / "runs_r65.json"
    hist = json.load(open(HP)) if HP.exists() else {}
    n0 = len(hist)
    for d in days(HIST_FROM, D_TO):
        L = F.listing_instant(d)
        for st in STATIONS:
            k = f"{st}|{d.isoformat()}"
            if k in hist:
                continue
            if time.time() - t0 > 520:
                break
            cur = run_series(st, d, L)
            prev = run_series(st, d - timedelta(days=1), L) if d >= D_FROM else {}
            hist[k] = {"cur": {s: [[r[0], r[1]] for r in v] for s, v in cur.items()},
                       "curve": {s: v[0][2] for s, v in cur.items() if v},
                       "prev": {s: {r[0]: r[1] for r in v} for s, v in prev.items()}}
    json.dump(hist, open(HP, "w"))
    left = sum(1 for d in days(HIST_FROM, D_TO) for st in STATIONS if f"{st}|{d.isoformat()}" not in hist)
    log(f"runs: +{len(hist) - n0}, total {len(hist)}, left {left}, {time.time() - t0:.0f} s")
    sys.exit(3 if left or len(ff) < len(days(D_FROM, D_TO)) else 0)

# =============================================================== features
gam = json.load(open(B / "gamma_r65.json"))
ff = json.load(open(B / "f_r65.json"))
hist = json.load(open(B / "runs_r65.json"))
truth = {}
for st, ed, b in conn.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE event_date BETWEEN ? AND ? AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
        " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)", (D_FROM.isoformat(), D_TO.isoformat())):
    truth[f"{st}|{ed}"] = b


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


def bucket(edges, x):
    if x < edges[0][1]:
        return 0
    for i, (lo, hi) in enumerate(edges):
        if lo <= x < hi:
            return i
    return len(edges) - 1


def listing_fc(st, p, sysid):
    h = hist.get(f"{st}|{p.isoformat()}")
    if not h or not h["cur"].get(sysid):
        return None
    return h["cur"][sysid][0][1]


def err_history(st, sysid, d):
    """(truth - listing forecast) for settled days p <= listing date - 2, newest last."""
    cut = date.fromisoformat(F.listing_instant(d)[:10]) - timedelta(days=2)
    out = []
    for p in days(HIST_FROM, cut):
        t = ALL_TRUTHS.get((st, p.isoformat()))
        fc = listing_fc(st, p, sysid)
        if t is not None and fc is not None:
            out.append(t - fc)
    return out


def peak_shape(curve):
    if len(curve) < 3:
        return None
    vals = [v for _h, v in curve]
    i = int(np.argmax(vals))
    hour = curve[i][0]
    if 0 < i < len(vals) - 1:
        a, b, c = vals[i - 1], vals[i], vals[i + 1]
        den = a - 2 * b + c
        miss = (-(a - c) ** 2 / (8 * den)) if den < 0 else 0.0
        drop = b - c
    else:
        miss, drop = 0.0, (vals[i] - vals[i - 1]) if i > 0 else 0.0
    return hour, miss, max(vals) - min(vals), drop


rows = []
for d in days(D_FROM, D_TO):
    for st in STATIONS:
        k = f"{st}|{d.isoformat()}"
        e = (ff.get(d.isoformat()) or {}).get(st)
        g = gam.get(k)
        if not e or e.get("state") != "READY" or not g or "labels" not in g or k not in truth or k not in hist:
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
        f = e["f_c"]
        fb = bucket(edges, f)
        y = [x[1] for x in lad].index(truth[k])
        fcen = sum(edges[fb]) / 2
        h = hist[k]
        fs = [s for s in e["systems"]]
        x = {"pos": (f - fcen) / w}
        # G1 run changes
        ch1 = [h["cur"][s][0][1] - h["cur"][s][1][1] for s in fs if len(h["cur"].get(s, [])) >= 2]
        ch2 = [h["cur"][s][0][1] - h["cur"][s][2][1] for s in fs if len(h["cur"].get(s, [])) >= 3]
        x["G1 change median"] = statistics.median(ch1) / w if ch1 else 0.0
        x["G1 change2 median"] = statistics.median(ch2) / w if ch2 else 0.0
        x["G1 warming minus cooling"] = (sum(c > 0.05 for c in ch1) - sum(c < -0.05 for c in ch1)) / max(1, len(ch1))
        # G2 member structure (corrected members)
        cor = {s: v["tmax_c"] + v.get("bias_c", 0.0) for s, v in e["systems"].items()}
        cv = list(cor.values())
        x["G2 skew mean-median"] = (statistics.mean(cv) - statistics.median(cv)) / w
        x["G2 spread"] = statistics.pstdev(cv) / w
        x["G2 max - F"] = (max(cv) - f) / w
        x["G2 min - F"] = (min(cv) - f) / w
        wts = {}
        for s in cor:
            eh = err_history(st, s, d)
            if len(eh) >= 5:
                wts[s] = 1.0 / max(0.3, statistics.mean(abs(v) for v in eh[-30:]))
        x["G2 skill-weighted - F"] = ((sum(wts[s] * cor[s] for s in wts) / sum(wts.values())) - f) / w if len(wts) >= 2 else 0.0
        # G3 curve shape (latest run of each F system)
        shp = [peak_shape(h["curve"][s]) for s in fs if s in h["curve"]]
        shp = [v for v in shp if v]
        x["G3 peak hour"] = statistics.median(v[0] for v in shp) - 14.0 if shp else 0.0
        x["G3 missed peak"] = statistics.median(v[1] for v in shp) / w if shp else 0.0
        x["G3 diurnal range"] = statistics.median(v[2] for v in shp) / w if shp else 0.0
        x["G3 post-peak drop"] = statistics.median(v[3] for v in shp) / w if shp else 0.0
        # G4 day-to-day trend within the same run
        tr = []
        for s in fs:
            if h["cur"].get(s):
                run0, v0 = h["cur"][s][0]
                pv = h["prev"].get(s, {}).get(run0)
                if pv is not None:
                    tr.append(v0 - pv)
        x["G4 trend vs day before"] = statistics.median(tr) / w if tr else 0.0
        # G5 recent error anomaly
        an1, an3 = [], []
        for s in fs:
            eh = err_history(st, s, d)
            if len(eh) >= 6:
                base = statistics.mean(eh[:-1])
                an1.append(eh[-1] - base)
                an3.append(statistics.mean(eh[-3:]) - statistics.mean(eh[:-3]))
        x["G5 last-day anomaly"] = statistics.median(an1) / w if an1 else 0.0
        x["G5 3-day anomaly"] = statistics.median(an3) / w if an3 else 0.0
        # G6 deterministic families outside F
        ex = []
        for s in EXTRA:
            if not h["cur"].get(s):
                continue
            eh = err_history(st, s, d)
            if len(eh) >= 5:
                ex.append(h["cur"][s][0][1] + statistics.mean(eh[-30:]) - f)
        x["G6 outside-F median - F"] = statistics.median(ex) / w if ex else 0.0
        x["G6 outside-F count"] = float(len(ex))
        rows.append({"k": k, "ed": d.isoformat(), "y": y, "fb": fb, "x": x,
                     "cls": "on" if y == fb else ("shoulder" if abs(y - fb) == 1 else "far"), "side": int(np.sign(y - fb))})

GROUPS = {
    "G1 run changes": ["G1 change median", "G1 change2 median", "G1 warming minus cooling"],
    "G2 member structure": ["G2 skew mean-median", "G2 spread", "G2 max - F", "G2 min - F", "G2 skill-weighted - F"],
    "G3 curve shape": ["G3 peak hour", "G3 missed peak", "G3 diurnal range", "G3 post-peak drop"],
    "G4 day-to-day trend": ["G4 trend vs day before"],
    "G5 recent errors": ["G5 last-day anomaly", "G5 3-day anomaly"],
    "G6 outside-F families": ["G6 outside-F median - F", "G6 outside-F count"],
}
GROUPS["all groups"] = [c for v in GROUPS.values() for c in v]
dl = sorted({r["ed"] for r in rows})
cc = collections.Counter(r["cls"] for r in rows)
n = len(rows)
print(f"station-days {n}, event days {dl[0]}..{dl[-1]} ({len(dl)}); truth on F {cc['on'] / n:.1%}, shoulder {cc['shoulder'] / n:.1%}, far {cc['far'] / n:.1%}")
cov = {c: sum(r["x"][c] != 0.0 for r in rows) / n for c in GROUPS["all groups"]}
print("feature coverage (non-zero share): " + ", ".join(f"{c} {v:.0%}" for c, v in cov.items()))

SH = [r for r in rows if r["cls"] == "shoulder"]
OFF = [r for r in rows if r["cls"] != "on"]
rng = np.random.default_rng(65)


def boot(vals, eds, draws=2000):
    vals, eds = np.asarray(vals, float), np.asarray(eds)
    ud = sorted(set(eds))
    sums = np.array([vals[eds == d].sum() for d in ud])
    cnts = np.array([(eds == d).sum() for d in ud])
    idx = rng.integers(0, len(ud), size=(draws, len(ud)))
    bs = sums[idx].sum(1) / cnts[idx].sum(1)
    return float(vals.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), bs


print("\n== (1) raw feature alone: sign(feature) calls the side (shoulder rows; rows where the feature is exactly 0 left out)")
for c in [c for c in GROUPS["all groups"] if c not in ("G2 spread", "G6 outside-F count", "G3 diurnal range")]:
    for sgn in (1,):
        g = [r for r in SH if r["x"][c] != 0.0]
        if len(g) < 30:
            continue
        ok = [float(np.sign(r["x"][c]) * sgn == r["side"]) for r in g]
        m, lo, hi, _ = boot(ok, [r["ed"] for r in g])
        okall = [float(np.sign(r["x"][c]) == r["side"]) for r in OFF if r["x"][c] != 0.0]
        print(f"   {c:28s} n {len(g):4d}: {m:.1%} [{lo:.1%}, {hi:.1%}]   all off-F {statistics.mean(okall):.1%}")
g = SH
print(f"   {'F position (baseline)':28s} n {len(g):4d}: {statistics.mean(float(np.sign(r['x']['pos']) == r['side']) for r in g):.1%}")


def fit_logit(X, yv, l2=1.0, iters=800, lr=0.3):
    wv = np.zeros(X.shape[1] + 1)
    Xb = np.c_[X, np.ones(len(X))]
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ wv))
        gr = Xb.T @ (p - yv) / len(X)
        gr[:-1] += l2 * wv[:-1] / len(X)
        wv -= lr * gr
    return wv


def walk_forward(cols, train_rows, test_rows):
    """P(warm) for each test row, fitted on off-F rows of strictly earlier event days (>= 7)."""
    out = {}
    for d in dl:
        tr = [r for r in train_rows if r["ed"] < d]
        if len({r["ed"] for r in tr}) < 7:
            continue
        X = np.array([[r["x"][c] for c in cols] for r in tr])
        mu, sd = X.mean(0), X.std(0)
        sd[sd < 1e-9] = 1.0
        wv = fit_logit((X - mu) / sd, np.array([r["side"] > 0 for r in tr], float))
        for r in test_rows:
            if r["ed"] == d:
                z = np.r_[(np.array([r["x"][c] for c in cols]) - mu) / sd, 1.0] @ wv
                out[r["k"]] = 1 / (1 + math.exp(-z))
    return out


print("\n== (2) increment over F's position, walk-forward (train on earlier days' off-F rows), scored on shoulder rows")
base = walk_forward(["pos"], OFF, SH)
keys = [r for r in SH if r["k"] in base]
print(f"   scored shoulder rows {len(keys)} on {len({r['ed'] for r in keys})} event days")


def ll(p, side):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -math.log(p if side > 0 else 1 - p)


bl = [ll(base[r["k"]], r["side"]) for r in keys]
ba = [float((base[r["k"]] > 0.5) == (r["side"] > 0)) for r in keys]
print(f"   baseline F position: logloss {statistics.mean(bl):.4f}, accuracy {statistics.mean(ba):.1%} (coin logloss 0.6931)")
res = {}
mid = dl[len(dl) // 2]
for gname, cols in GROUPS.items():
    pr = walk_forward(["pos"] + cols, OFF, SH)
    d_ll = [ll(pr[r["k"]], r["side"]) - ll(base[r["k"]], r["side"]) for r in keys]
    acc = [float((pr[r["k"]] > 0.5) == (r["side"] > 0)) for r in keys]
    m, lo, hi, bs = boot(d_ll, [r["ed"] for r in keys])
    p_one = float((bs >= 0).mean())
    h1 = statistics.mean(v for v, r in zip(d_ll, keys) if r["ed"] < mid)
    h2 = statistics.mean(v for v, r in zip(d_ll, keys) if r["ed"] >= mid)
    res[gname] = (m, lo, hi, p_one, h1, h2)
    print(f"   + {gname:24s} dLogLoss {m:+.4f} [{lo:+.4f}, {hi:+.4f}] p {p_one:.3f}  halves {h1:+.4f}/{h2:+.4f}  accuracy {statistics.mean(acc):.1%}")
names = [g for g in GROUPS if g != "all groups"]
order = sorted(names, key=lambda g: res[g][3])
passed, mtests = set(), len(names)
for i, g in enumerate(order, 1):
    if res[g][3] <= 0.10 * i / mtests:
        passed = set(order[:i])
print(f"   BH q=0.10 over six groups: passed {sorted(passed) or 'none'};"
      f" with both halves negative: {sorted(g for g in passed if res[g][4] < 0 and res[g][5] < 0) or 'none'}")

print("\n== (3) post-hoc (after reading (2), not part of the verdict): which piece of G2 carries it, and the reversed-sign singles")
for c in GROUPS["G2 member structure"] + ["G3 missed peak", "G1 change median"]:
    pr = walk_forward(["pos", c], OFF, SH)
    d_ll = [ll(pr[r["k"]], r["side"]) - ll(base[r["k"]], r["side"]) for r in keys]
    m, lo, hi, _bs = boot(d_ll, [r["ed"] for r in keys])
    h1 = statistics.mean(v for v, r in zip(d_ll, keys) if r["ed"] < mid)
    h2 = statistics.mean(v for v, r in zip(d_ll, keys) if r["ed"] >= mid)
    acc = statistics.mean(float((pr[r["k"]] > 0.5) == (r["side"] > 0)) for r in keys)
    print(f"   pos + {c:26s} dLogLoss {m:+.4f} [{lo:+.4f}, {hi:+.4f}] halves {h1:+.4f}/{h2:+.4f} accuracy {acc:.1%}")
sk = [r for r in SH if abs(r["x"]["G2 skill-weighted - F"]) > 0 and np.sign(r["x"]["G2 skill-weighted - F"]) != np.sign(r["x"]["pos"])]
print(f"   skill-weighted members disagree with F's position on {len(sk)} shoulder rows: skill-weighted side right"
      f" {statistics.mean(float(np.sign(r['x']['G2 skill-weighted - F']) == r['side']) for r in sk):.1%}")
for cols in (["pos"] + GROUPS["G2 member structure"], ["pos", "G1 change median"], ["pos", "G3 missed peak"]):
    X = np.array([[r["x"][c] for c in cols] for r in OFF])
    mu, sd = X.mean(0), X.std(0)
    sd[sd < 1e-9] = 1.0
    wv = fit_logit((X - mu) / sd, np.array([r["side"] > 0 for r in OFF], float))
    print("   full-sample standardized coefficients (P(warm)): " + ", ".join(f"{c} {v:+.2f}" for c, v in zip(cols, wv[:-1])))
