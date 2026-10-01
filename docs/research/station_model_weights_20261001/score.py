#!/usr/bin/env python3
"""Pass 2: station x model seating and weights (PROTOCOL.md).  Read-only."""
from __future__ import annotations
import gzip, json, math, sqlite3, sys, time
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    sys.path.insert(0, str(extra))
import replay_p0_stable_pool as replay
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import center_width_store as store
from bayes_basket_target.cross_consensus import bridged_cross_probability
from bayes_basket_target.served_shape import apply_served_table_layers
t0 = time.monotonic()
FLOOR = 1e-3
FAM = ["DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS", "ECMWF_AIFS_ENS_GLOBAL", "ECMWF_IFS_ENS_OPEN_PF50", "NOAA_AIGEFS_025", "NOAA_GEFS_05"]
FI = {f: i for i, f in enumerate(FAM)}
SCORE_FROM, SPLIT, SCORE_TO = "2026-08-10", "2026-09-14", "2026-09-30"
ETAS = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0); HALVES = (7.0, 14.0, 30.0, math.inf); TAUS = (5.0, 10.0, 20.0, 40.0, 80.0)
BINS = ((48, 1e9), (36, 48), (24, 36), (12, 24), (0, 12))
def utc(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def log(*a): print(f"[{time.monotonic()-t0:6.0f}s]", *a, flush=True)

# ---------------------------------------------------------------- load + representative decisions
decs = []
import os
ONLY = set(filter(None, os.environ.get("SMW_STATIONS", "").split(",")))
OUTTAG = os.environ.get("SMW_TAG", "")
for f in sorted((HERE / "pass1_smw").glob("????.jsonl.gz")):
    if ONLY and f.name[:4] not in ONLY: continue
    rows = [json.loads(l) for l in gzip.open(f, "rt")]
    by_day = defaultdict(list)
    for r in rows: by_day[r["event_date"]].append(r)
    for day, rs in by_day.items():
        end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(rs[0]["tz"])) + timedelta(days=1)
        for lo, hi in BINS:
            inbin = [r for r in rs if lo <= (end - utc(r["at"])).total_seconds() / 3600 < hi]
            if not inbin: continue
            r = max(inbin, key=lambda x: x["at"])
            y = oos._temperature_bucket(r["actual"])
            pool = sorted(r["sys"])
            d = {"station": r["station"], "day": day, "at": utc(r["at"]), "known": utc(r["truth_known_at"]), "bin": lo,
                 "lead": r["lead"], "actual": r["actual"], "lat": r["lat"], "cooling": r["cooling"], "family": r["family"],
                 "pool": pool, "w0": {s: r["seat"].get(s, 0.0) for s in pool}, "p": {s: r["sys"][s]["p"] for s in pool},
                 "c": {s: r["sys"][s]["c"] for s in pool}, "slead": {s: r["sys"][s]["lead"] for s in pool},
                 "py": {s: max(float(r["sys"][s]["p"].get(y, 0.0)), FLOOR) for s in pool}}
            tot = sum(d["w0"].values()); d["w0"] = {s: v / tot for s, v in d["w0"].items()}
            decs.append(d)
log("representative decisions", len(decs), "station-days", len({(d["station"], d["day"]) for d in decs}))
def proxy(d, w): return -math.log(max(sum(w[s] * d["py"][s] for s in w), FLOOR))
def norm(w):
    t = sum(w.values()); return {k: v / t for k, v in w.items()}

# ---------------------------------------------------------------- per station-day records r_jt (paired vs seat pool)
by_station = defaultdict(list)
for d in decs: by_station[d["station"]].append(d)
rec = defaultdict(dict)        # station -> day -> {"known", "r": {j: mean r}, "rl": {(j,lead): mean r}, "decs": [...]}
for st, ds in by_station.items():
    g = defaultdict(list)
    for d in ds: g[d["day"]].append(d)
    for day, dd in g.items():
        r = defaultdict(list); rl = defaultdict(list)
        for d in dd:
            l0 = proxy(d, d["w0"])
            for s in d["pool"]:
                v = l0 + math.log(d["py"][s]); r[s].append(v); rl[(s, d["slead"][s])].append(v)
        rec[st][day] = {"known": dd[0]["known"], "r": {s: float(np.mean(v)) for s, v in r.items()},
                        "rl": {k: float(np.mean(v)) for k, v in rl.items()}, "decs": dd}
known_sorted = {st: sorted(rec[st], key=lambda day: rec[st][day]["known"]) for st in rec}
def prefix_days(st, at):
    days = known_sorted[st]; keys = [rec[st][x]["known"] for x in days]
    return days[:bisect_left(keys, at)]
# fleet records: per event day, mean over stations of r_jt
fleet_day = defaultdict(lambda: defaultdict(list))
for st in rec:
    for day, R in rec[st].items():
        for s, v in R["r"].items(): fleet_day[day][s].append((R["known"], v))

# fleet: per day, per system, records sorted by truth-known time with prefix sums
fleet_idx = {}
for day, per in fleet_day.items():
    for s_, vals in per.items():
        vals = sorted(vals); fleet_idx[(day, s_)] = ([k for k, _ in vals], np.cumsum([v for _, v in vals]))
_S_cache = {}
def cum_regret(d, half, mode):
    key = (d["id"], half, mode)
    if key in _S_cache: return _S_cache[key]
    delta = 0.5 ** (1.0 / half) if math.isfinite(half) else 1.0
    today = date.fromisoformat(d["day"]); S = {s: 0.0 for s in d["pool"]}
    if mode == "F":
        for day in fleet_day:
            age = (today - date.fromisoformat(day)).days
            if age < 0: continue   # same event day at other stations may already be settled (time zones)
            for s in S:
                ent = fleet_idx.get((day, s))
                if ent is None: continue
                n = bisect_left(ent[0], d["at"])
                if n: S[s] += delta ** age * float(ent[1][n - 1]) / n
    else:
        for day in prefix_days(d["station"], d["at"]):
            age = (today - date.fromisoformat(day)).days; R = rec[d["station"]][day]
            for s in S:
                v = R["rl"].get((s, d["slead"][s])) if mode == "L" else R["r"].get(s)
                if v is not None: S[s] += delta ** age * v
    _S_cache[key] = S; return S
def hedge(d, eta, half, mode):
    if eta == 0.0: return dict(d["w0"])
    S = cum_regret(d, half, mode); m = max(eta * v for v in S.values())
    return norm({s: d["w0"][s] * math.exp(eta * S[s] - m) for s in d["pool"]})

# ---------------------------------------------------------------- EB: station log-score-optimal mixture, shrunk to w0
_eb_cache = {}
def eb_fit(st, days):
    key = (st, len(days))
    if key in _eb_cache: return _eb_cache[key]
    recs = [d for day in days for d in rec[st][day]["decs"]]
    if not recs: _eb_cache[key] = None; return None
    M = np.zeros((len(recs), len(FAM))); P = np.zeros((len(recs), len(FAM)))
    for i, d in enumerate(recs):
        for s in d["pool"]: M[i, FI[s]] = 1.0; P[i, FI[s]] = d["py"][s]
    th = np.zeros(len(FAM))
    for _ in range(300):
        e = np.exp(th - th.max()) * M; den = e.sum(1, keepdims=True); q = e / den
        num = (q * P).sum(1, keepdims=True); resp = q * P / num
        grad = (resp - q).mean(0); th += 2.0 * grad; th -= th.mean()
        if np.abs(grad).max() < 1e-6: break
    w = np.exp(th - th.max()); _eb_cache[key] = (w / w.sum(), len(days)); return _eb_cache[key]
def eb(d, tau):
    fit = eb_fit(d["station"], prefix_days(d["station"], d["at"]))
    if fit is None: return dict(d["w0"])
    w, n = fit; a = n / (n + tau); hat = norm({s: float(w[FI[s]]) for s in d["pool"]})
    return {s: (1 - a) * d["w0"][s] + a * hat[s] for s in d["pool"]}

# ---------------------------------------------------------------- SEAT: station seat veto
_seat_cache = {}
rng = np.random.default_rng(20261001)
def seat(d):
    days = prefix_days(d["station"], d["at"]); key = (d["station"], len(days), tuple(d["pool"]))
    if key in _seat_cache: return _seat_cache[key]
    best = None
    if len(days) >= 10 and len(d["pool"]) >= 3:
        for j in d["pool"]:
            rest = [s for s in d["pool"] if s != j]
            if len({d["family"][s] for s in rest}) < 2: continue
            diffs = []
            for day in days:
                vals = []
                for x in rec[d["station"]][day]["decs"]:
                    if j not in x["pool"] or len(x["pool"]) < 2: continue
                    keep = [s for s in x["pool"] if s != j]
                    if not keep: continue
                    vals.append(proxy(x, x["w0"]) - proxy(x, norm({s: x["w0"][s] for s in keep})))
                if vals: diffs.append(float(np.mean(vals)))
            if len(diffs) < 10: continue
            arr = np.array(diffs); boots = arr[rng.integers(0, len(arr), (1000, len(arr)))].mean(1)
            lo = float(np.quantile(boots, 0.10))
            if arr.mean() > 0 and lo > 0 and (best is None or arr.mean() > best[1]): best = (j, float(arr.mean()))
    _seat_cache[key] = None if best is None else best[0]
    return _seat_cache[key]
def seated(d):
    j = seat(d); return d["pool"] if j is None else [s for s in d["pool"] if s != j]

# ---------------------------------------------------------------- precompute proxy losses for tuning
combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],
          "HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS]}
W = defaultdict(dict)   # (arm, params) -> id(d) -> weights
for i, d in enumerate(decs):
    d["id"] = i
    for e, h in combos["H"]: W[("H", (e, h))][i] = hedge(d, e, h, "S")
    for e, h in combos["HL"]: W[("HL", (e, h))][i] = hedge(d, e, h, "L")
    for t in TAUS: W[("EB", (t,))][i] = eb(d, t)
    for e, h in combos["HF"]: W[("HF", (e, h))][i] = hedge(d, e, h, "F")
    if i % 2000 == 0: log("weights", i)
log("weights done")
by_day_decs = defaultdict(list)
for d in decs: by_day_decs[d["day"]].append(d)
alldays = sorted(by_day_decs)
def choose(arm, day):
    window = [x for x in alldays if (date.fromisoformat(day) - date.fromisoformat(x)).days in range(2, 23)]
    pool = [d for x in window for d in by_day_decs[x]]
    if not pool: return combos[arm][0]
    return min(combos[arm], key=lambda c: (sum(proxy(d, W[(arm, c)][d["id"]]) for d in pool) / len(pool), c))
chosen = {arm: {day: choose(arm, day) for day in alldays if SCORE_FROM <= day <= SCORE_TO} for arm in combos}
log("tuning done", {arm: sorted(set(v.values()))[:6] for arm, v in chosen.items()})

# ---------------------------------------------------------------- oracle upper bounds (in-sample, scored period)
def fit_fixed(recs):
    M = np.zeros((len(recs), len(FAM))); P = np.zeros((len(recs), len(FAM)))
    for i, d in enumerate(recs):
        for s in d["pool"]: M[i, FI[s]] = 1.0; P[i, FI[s]] = d["py"][s]
    th = np.zeros(len(FAM))
    for _ in range(500):
        e = np.exp(th - th.max()) * M; q = e / e.sum(1, keepdims=True); num = (q * P).sum(1, keepdims=True)
        grad = (q * P / num - q).mean(0); th += 2.0 * grad; th -= th.mean()
    w = np.exp(th - th.max()); return w / w.sum()
scored = [d for d in decs if SCORE_FROM <= d["day"] <= SCORE_TO]
oracle_s = {st: fit_fixed([d for d in scored if d["station"] == st]) for st in by_station}
oracle_sl = {}
for d in scored: oracle_sl.setdefault((d["station"], d["lead"]), []).append(d)
oracle_sl = {k: fit_fixed(v) for k, v in oracle_sl.items()}
log("oracles done")

# ---------------------------------------------------------------- arm weights at a scored decision
def arm_w(d, arm):
    if arm == "A0": return d["w0"]
    if arm in ("H*", "HL", "HF", "EB"):
        key = {"H*": "H", "HL": "HL", "HF": "HF", "EB": "EB"}[arm]; return W[(key, chosen[key][d["day"]])][d["id"]]
    if arm == "SEAT*": return norm({s: d["w0"][s] for s in seated(d)})
    if arm == "SEAT+H":
        keep = seated(d); w = W[("H", chosen["H"][d["day"]])][d["id"]]; return norm({s: w[s] for s in keep})
    if arm == "ORACLE-S": o = oracle_s[d["station"]]; return norm({s: float(o[FI[s]]) for s in d["pool"]})
    if arm == "ORACLE-SL": o = oracle_sl[(d["station"], d["lead"])]; return norm({s: float(o[FI[s]]) for s in d["pool"]})
ARMS = ["A0", "H*", "HL", "HF", "EB", "SEAT*", "SEAT+H", "ORACLE-S", "ORACLE-SL"]

# ---------------------------------------------------------------- full production downstream
conn = sqlite3.connect(f"file:{ROOT/'data/bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
layers = {st: oos.p0_gate_tables(conn, station_id=st)["center_width_layer"] for st in by_station}
conn.close()
log("width layers", {str(v) for v in layers.values()} if len({str(v) for v in layers.values()}) < 3 else "varied")
_ds = {}
def downstream(d, w):
    w = {s: v for s, v in w.items() if v > 1e-12}
    key = (d["id"], tuple(sorted((s, round(v, 9)) for s, v in w.items())))
    if key in _ds: return _ds[key]
    grid = replay._grid([d["p"][s] for s in w]); kc = [float(int(k)) for k in grid]
    probs = {s: {k: float(d["p"][s].get(k, 0.0)) for k in grid} for s in w}
    blended = bridged_cross_probability(system_probabilities=probs, system_weights=w, system_centers_c={s: d["c"][s] for s in w},
                                        ordered_keys=grid, key_centers_c=kc)[0] if len(w) > 1 else norm(probs[next(iter(w))])
    pre = oos._holdout_shape_stack(blended, grid=grid, cooling_shift_sigma=d["cooling"], station_latitude=d["lat"],
                                   neighbour_center_c=sum(w[s] * d["c"][s] for s in w), center_width_layer=None)
    served, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc, center_width=layers[d["station"]])
    crps, nll, brier, _b, _a = replay._score(served, d["actual"])
    _ds[key] = (nll, brier, crps); return _ds[key]
results = defaultdict(dict)   # arm -> (station, day) -> list of triples
for i, d in enumerate(scored):
    for arm in ARMS: results[arm].setdefault((d["station"], d["day"]), []).append(downstream(d, arm_w(d, arm)))
    if i % 2000 == 0: log("downstream", i, "/", len(scored))
sd = sorted(results["A0"])
tab = {arm: {k: tuple(float(np.mean([t[i] for t in results[arm][k]])) for i in range(3)) for k in sd} for arm in ARMS}

# ---------------------------------------------------------------- verdicts
def block_ci(arm, block=3, draws=2000):
    days = sorted({k[1] for k in sd}); byday = {x: [tab[arm][k][0] - tab["A0"][k][0] for k in sd if k[1] == x] for x in days}
    g = np.random.default_rng(7); out = []
    for _ in range(draws):
        ix = []
        while len(ix) < len(days):
            st0 = int(g.integers(len(days))); ix.extend((st0 + j) % len(days) for j in range(block))
        out.append(np.mean(np.concatenate([byday[days[i]] for i in ix[:len(days)]])))
    return [float(np.quantile(out, 0.0125)), float(np.quantile(out, 0.9875))]
res = {"decisions": len(decs), "scored_decisions": len(scored), "scored_station_days": len(sd),
       "chosen": {arm: {k: list(v) for k, v in chosen[arm].items()} for arm in chosen},
       "oracle_s": {k: v.tolist() for k, v in oracle_s.items()}, "arms": {}}
for arm in ARMS[1:]:
    entry = {}
    for conf in (0.975, 0.99):
        store.GATE_CONFIDENCE = conf
        entry[f"gate_{conf}"] = store._proper_score_gate([tab["A0"][k] for k in sd], [tab[arm][k] for k in sd], sd)
    store.GATE_CONFIDENCE = 0.95
    for name, pred in (("first", lambda k: k[1] < SPLIT), ("second", lambda k: k[1] >= SPLIT)):
        ks = [k for k in sd if pred(k)]
        entry[name] = [float(np.mean([tab[arm][k][i] - tab["A0"][k][i] for k in ks])) for i in range(3)] + [len(ks)]
    entry["date_block3_ci_975"] = block_ci(arm)
    entry["mean"] = [float(np.mean([tab[arm][k][i] for k in sd])) for i in range(3)]
    res["arms"][arm] = entry
    g = entry["gate_0.975"]
    log(f"{arm:9s} dLog={g['d_logscore']:+.4f} ci97.5={['%+.4f' % x for x in g['ci_logscore']]} dBrier={g['d_brier']:+.4f} "
        f"dCRPS={g['d_crps']:+.4f} lower={g['lower']} halves={entry['first'][0]:+.4f}/{entry['second'][0]:+.4f} block3={['%+.4f' % x for x in entry['date_block3_ci_975']]}")
res["A0_mean"] = [float(np.mean([tab["A0"][k][i] for k in sd])) for i in range(3)]
# seat veto census
drops = defaultdict(int)
for d in scored:
    j = seat(d)
    if j: drops[(d["station"], j)] += 1
res["seat_drops"] = {f"{k[0]}|{k[1]}": v for k, v in sorted(drops.items(), key=lambda x: -x[1])}
log("seat drops (station|system: decisions)", dict(list(res["seat_drops"].items())[:15]))
(HERE / f"results{OUTTAG}.json").write_text(json.dumps(res, indent=1, default=str))
with gzip.open(HERE / f"station_day_scores{OUTTAG}.jsonl.gz", "wt") as z:
    for k in sd: z.write(json.dumps({"station": k[0], "day": k[1], **{arm: tab[arm][k] for arm in ARMS}}) + "\n")
log("DONE")
