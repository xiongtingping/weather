"""Round 25 part 2: how much of the cluster's pick can each centre -- raw models,
model mode/mean, market mode/mean/median -- reproduce, alone and together?

A. Single-centre rules: the bucket a centre falls in vs their YES bucket
   (exact), and the side of the market mode a centre points to vs the side
   they took off the mode (agreement).
B. A ranker over every candidate leg (bucket x YES/NO) of each event,
   LightGBM lambdarank, cross-validated by event date (5 folds, no date in
   both train and test): top-1 / top-3 hit of their actual leg, adding
   feature families one at a time.
"""
import collections
import gzip
import json
import math
import random
import statistics
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np

B = Path(sys.argv[1])
ALL = json.load(gzip.open(B / "replicate_rows.json.gz", "rt"))
E = [e for e in ALL if e.get("set", "research") == "research"]
FWD = [e for e in ALL if e.get("set") == "forward"]
# Round 27: minute price paths (fetch_minute_paths.py), key "station|date|bucket".
import bisect  # noqa: E402
_MP = B / "minute_paths.json"
MINUTE = {k: v for k, v in (json.load(open(_MP)).items() if _MP.exists() else []) if "error" not in v}


def minute_feats(e, label):
    """Path features of one bucket's YES price strictly before the fill's minute."""
    path = MINUTE.get(f"{e['st']}|{e['ed']}|{label}")
    nan = float("nan")
    out = {k: nan for k in ("d1", "d5", "d15", "d30", "d60", "pos60", "vol60", "since", "p_now")}
    if not path or not path["t"]:
        return out
    t, pr = path["t"], path["p"]
    cut = int(e["ts"]) - 60
    i = bisect.bisect_right(t, cut) - 1
    if i < 0:
        return out
    now = pr[i]
    out["p_now"] = now
    for lag in (1, 5, 15, 30, 60):
        j = bisect.bisect_right(t, cut - 60 * lag) - 1
        if j >= 0:
            out[f"d{lag}"] = now - pr[j]
    j60 = bisect.bisect_left(t, cut - 3600)
    window = pr[j60:i + 1]
    if len(window) >= 2:
        hi, lo = max(window), min(window)
        out["pos60"] = (now - lo) / (hi - lo) if hi > lo else 0.5
        steps = [b - a for a, b in zip(window, window[1:])]
        out["vol60"] = statistics.pstdev(steps) if len(steps) > 1 else 0.0
    k = i
    while k > 0 and pr[k - 1] == now:
        k -= 1
    out["since"] = (cut - t[k]) / 60.0
    return out


def ladder_shift(e):
    """How far the ladder's mean (normalised minute prices, rungs) moved in the last 15 and 60 minutes."""
    out = {}
    for lag in (15, 60):
        now, then = {}, {}
        for o, label in e["labels"].items():
            path = MINUTE.get(f"{e['st']}|{e['ed']}|{label}")
            if not path or not path["t"]:
                continue
            t, pr = path["t"], path["p"]
            cut = int(e["ts"]) - 60
            i = bisect.bisect_right(t, cut) - 1
            j = bisect.bisect_right(t, cut - 60 * lag) - 1
            if i >= 0 and j >= 0:
                now[o], then[o] = pr[i], pr[j]
        if len(now) >= 3 and sum(now.values()) > 0 and sum(then.values()) > 0:
            mn = sum(o * v for o, v in now.items()) / sum(now.values())
            mt = sum(o * v for o, v in then.items()) / sum(then.values())
            out[lag] = mn - mt
        else:
            out[lag] = float("nan")
    return out
FILLS = collections.defaultdict(list)
_fills_src = B / "cluster_fills_all.json"
for f in (json.load(open(_fills_src)) if _fills_src.exists() else json.load(open(B / "cluster_events.json"))["fills"]):
    FILLS[(f["station"], f["event_date"])].append((int(f["ts"]), f["bucket"], f["outcome"], float(f["price"]), float(f["size"])))
for v in FILLS.values():
    v.sort()
random.seed(2525)
for e in ALL:
    for key in ("centres", "q", "ask", "no_price", "spread", "mom1h", "p"):
        e[key] = {int(k): v for k, v in e[key].items()}
    for s in e["raw"].values():
        s["probs"] = {int(k): v for k, v in s["probs"].items()}
    e["labels"] = {int(k): v for k, v in e["labels"].items()}


def bucket_of(e, c):
    if c is None:
        return None
    order = e["order"]
    best = min(order, key=lambda o: abs(e["centres"][o] - c))
    return best


def mean_c(e, vec):
    tot = sum(vec.get(o, 0.0) for o in e["order"])
    return sum(vec.get(o, 0.0) * e["centres"][o] for o in e["order"]) / tot if tot > 0 else None


def median_c(e, vec):
    tot = sum(vec.get(o, 0.0) for o in e["order"])
    acc = 0.0
    for o in e["order"]:
        acc += vec.get(o, 0.0) / tot
        if acc >= 0.5:
            return e["centres"][o]
    return None


def centres_of(e):
    """Every centre in C, keyed by name."""
    out = {}
    mode_c = e["centres"][e["mode_ord"]]
    out["market mode"] = mode_c
    out["market mean"] = mean_c(e, e["q"])
    out["market median"] = median_c(e, e["q"])
    out["our mode"] = e["centres"][e["our_mode_ord"]] if e["our_mode_ord"] in e["centres"] else None
    out["our mean"] = mean_c(e, e["p"])
    pooled = collections.defaultdict(float)
    for name, s in e["raw"].items():
        out[f"raw {name} mean"] = s["mean"]
        out[f"raw {name} mean+bias"] = s["mean"] + e["bias"].get(f"raw_{name}", 0.0)
        mo = max(s["probs"], key=lambda o: s["probs"][o])
        out[f"raw {name} mode"] = e["centres"][mo]
        for o, v in s["probs"].items():
            pooled[o] += v / len(e["raw"])
    if e["raw"]:
        out["raw 6-pool mean"] = statistics.mean(s["mean"] for s in e["raw"].values())
        out["raw 6-pool mean+bias"] = statistics.mean(s["mean"] + e["bias"].get(f"raw_{n}", 0.0) for n, s in e["raw"].items())
        out["raw 6-pool mode"] = e["centres"][max(pooled, key=pooled.get)]
    for name, v in e["points"].items():
        out[f"point {name}"] = v
        out[f"point {name}+bias"] = v + e["bias"].get(f"pt_{name}", 0.0)
    out["yesterday truth"] = e["ytruth_c"]
    if e["is_d0"]:
        out["running high"] = e["tmax_today"]
    return out


# ---------------------------------------------------------------- A. single centres
print("== A. single centres (their first-taker legs, 09-22..10-07)")
yes_events = [e for e in E if e["chosen"]["side"] == "Yes"]
off_events = [e for e in E if e["chosen"]["ord"] != e["mode_ord"]]
print(f"   YES legs {len(yes_events)}; legs off the market mode {len(off_events)}")
names = collections.Counter()
for e in E:
    names.update(centres_of(e).keys())
rows = []
for name, _n in names.most_common():
    exact = [1.0 if bucket_of(e, centres_of(e).get(name)) == e["chosen"]["ord"] else 0.0
             for e in yes_events if centres_of(e).get(name) is not None]
    agree = []
    for e in off_events:
        c = centres_of(e).get(name)
        if c is None:
            continue
        mode_c = e["centres"][e["mode_ord"]]
        d = e["chosen"]["ord"] - e["mode_ord"]
        their = (1 if d > 0 else -1) * (1 if e["chosen"]["side"] == "Yes" else -1)
        if abs(c - mode_c) < 0.15 * e["rung_c"]:
            continue
        agree.append(1.0 if (1 if c > mode_c else -1) == their else 0.0)
    if len(exact) >= 100:
        rows.append((name, len(exact), sum(exact) / len(exact), len(agree), (sum(agree) / len(agree)) if agree else float("nan")))
for name, n1, ex, n2, ag in sorted(rows, key=lambda x: -x[2]):
    print(f"   {name:26s} YES bucket = its bucket {ex:.3f} (n={n1})   side off the mode agrees {ag:.3f} (n={n2})")
print("   (a YES leg is a market-mode bucket 22% of the time; a random YES bucket within +-1 of the mode ~1/3)")

# ---------------------------------------------------------------- B. ranker
FAMILIES = ["market", "our", "raw", "points", "momentum", "weather_days", "own_history", "microstructure", "minute_path"]


def candidate_rows(e):
    out = []
    mode = e["mode_ord"]
    qmean = mean_c(e, e["q"])
    qmed = median_c(e, e["q"])
    pmean = mean_c(e, e["p"])
    rung = e["rung_c"]
    pooled = collections.defaultdict(float)
    for s in e["raw"].values():
        for o, v in s["probs"].items():
            pooled[o] += v / max(1, len(e["raw"]))
    prior = e["prior"]
    last_ts = prior[-1][0] if prior else None
    shift = ladder_shift(e)
    for o in e["order"]:
        c = e["centres"][o]
        for side in ("Yes", "No"):
            if side == "No" and o not in e["no_price"]:
                continue
            y = 1 if side == "Yes" else -1
            f = {}
            # market
            f["is_yes"] = 1.0 if side == "Yes" else 0.0
            f["d_mode"] = float(o - mode)
            f["abs_d_mode"] = abs(o - mode)
            f["q"] = e["q"][o]
            f["q_mode"] = e["q"][mode]
            f["price"] = e["ask"][o] if side == "Yes" else e["no_price"][o]
            f["spread"] = e["spread"][o] or 0.0
            f["mom1h"] = (e["mom1h"][o] or 0.0) * y
            f["d_qmean"] = (c - qmean) / rung if qmean is not None else 0.0
            f["d_qmed"] = (c - qmed) / rung if qmed is not None else 0.0
            f["lead"] = e["lead"]
            f["is_f"] = 1.0 if e["unit"] == "F" else 0.0
            f["usdc"] = math.log1p(e["usdc"] or 0.0)
            # our served
            f["our_p"] = e["p"][o]
            f["our_logpq"] = math.log(max(e["p"][o], 1e-3) / max(e["q"][o], 1e-3))
            f["d_our_mode"] = float(o - e["our_mode_ord"])
            f["d_our_mean"] = (c - pmean) / rung if pmean is not None else 0.0
            f["stage_p1"] = 1.0 if e["stage"] == "P1" else 0.0
            f["stage_p0m"] = 1.0 if e["stage"] == "P0-M" else 0.0
            # raw models
            for name in ("ifs", "aifs", "icon", "gefs", "gem", "aigefs"):
                s = e["raw"].get(name)
                if s:
                    f[f"raw_{name}_p"] = s["probs"].get(o, 0.0)
                    f[f"raw_{name}_d"] = (c - s["mean"] - e["bias"].get(f"raw_{name}", 0.0)) / rung
                    f[f"raw_{name}_draw"] = (c - s["mean"]) / rung
                else:
                    f[f"raw_{name}_p"] = float("nan")
                    f[f"raw_{name}_d"] = float("nan")
                    f[f"raw_{name}_draw"] = float("nan")
            f["raw_pool_p"] = pooled.get(o, float("nan")) if e["raw"] else float("nan")
            f["raw_pool_logpq"] = math.log(max(pooled.get(o, 0.0), 1e-3) / max(e["q"][o], 1e-3)) if e["raw"] else float("nan")
            # points
            for name in ("hres", "aifs1", "ukmo", "cma"):
                v = e["points"].get(name)
                f[f"pt_{name}_d"] = (c - v - e["bias"].get(f"pt_{name}", 0.0)) / rung if v is not None else float("nan")
            # momentum
            M = e["M"] if e["M"] is not None else 0.0
            side_dir = (1 if o > mode else -1 if o < mode else 0) * y
            f["M_align"] = M * side_dir
            f["M"] = M
            for k in ("aifs_ens", "ifs_ens", "hres", "aifs_single"):
                f[f"chg_{k}"] = (e["M_parts"].get(k) or 0.0) * side_dir
            # yesterday / running high
            f["d_ytruth"] = (c - e["ytruth_c"]) / rung if e["ytruth_c"] is not None else float("nan")
            f["d_run"] = (c - e["tmax_today"]) / rung if (e["is_d0"] and e["tmax_today"] is not None) else float("nan")
            # own history on the station-day
            label = e["labels"][o]
            same = [u for t, b, s2, u in prior if b == label and s2 == side]
            opp = [u for t, b, s2, u in prior if b == label and s2 != side]
            nb = [u for t, b, s2, u in prior if s2 == "Yes" and b in (e["labels"].get(o - 1), e["labels"].get(o + 1))]
            f["own_same_n"] = float(len(same))
            f["own_same_usdc"] = math.log1p(sum(same))
            f["own_opp_n"] = float(len(opp))
            f["own_nb_yes_n"] = float(len(nb))
            f["own_prior_n"] = float(len(prior))
            f["own_since_h"] = (e["ts"] - last_ts) / 3600.0 if last_ts else 999.0
            # Round 25b: what a holder sees -- the cluster's net shares on this
            # bucket per side before the fill, its average entry, and how far the
            # YES mid has risen past it (the exit rule of rounds 6/9).
            hist = [x for x in FILLS.get((e["st"], e["ed"]), []) if x[0] < e["ts"] and x[1] == label]
            yes_sh = sum(sz for t, b, s2, pr, sz in hist if s2 == "Yes")
            no_sh = sum(sz for t, b, s2, pr, sz in hist if s2 == "No")
            yes_cost = sum(pr * sz for t, b, s2, pr, sz in hist if s2 == "Yes")
            f["own_yes_sh"] = math.log1p(yes_sh)
            f["own_no_sh"] = math.log1p(no_sh)
            f["own_yes_rise"] = (e["q"][o] - yes_cost / yes_sh) if yes_sh > 0 else float("nan")
            f["edge_mid"] = (e["q"][o] - e["ask"][o]) if side == "Yes" else ((1 - e["q"][o]) - e["no_price"][o])
            # Round 27: the bucket's minute path before the fill, read on the leg's own side.
            mf = minute_feats(e, label)
            for k in ("d1", "d5", "d15", "d30", "d60"):
                f[f"min_{k}"] = mf[k] * y if mf[k] == mf[k] else float("nan")
            f["min_pos60"] = (mf["pos60"] if y == 1 else 1 - mf["pos60"]) if mf["pos60"] == mf["pos60"] else float("nan")
            f["min_vol60"] = mf["vol60"]
            f["min_since"] = mf["since"]
            direction = (1 if o > mode else -1 if o < mode else 0) * y
            f["min_ladder15"] = shift[15] * direction if shift[15] == shift[15] else float("nan")
            f["min_ladder60"] = shift[60] * direction if shift[60] == shift[60] else float("nan")
            fam = {
                "market": ["is_yes", "d_mode", "abs_d_mode", "q", "q_mode", "price", "spread", "mom1h", "d_qmean", "d_qmed", "lead", "is_f", "usdc"],
                "our": ["our_p", "our_logpq", "d_our_mode", "d_our_mean", "stage_p1", "stage_p0m"],
                "raw": [k for k in f if k.startswith("raw_")],
                "points": [k for k in f if k.startswith("pt_")],
                "momentum": ["M_align", "M"] + [k for k in f if k.startswith("chg_")],
                "weather_days": ["d_ytruth", "d_run"],
                "own_history": [k for k in f if k.startswith("own_")],
                "microstructure": ["edge_mid"],
                "minute_path": [k for k in f if k.startswith("min_")],
            }
            out.append((o, side, f, fam))
    return out


data = []
for e in E:
    cands = candidate_rows(e)
    label = [1 if (o == e["chosen"]["ord"] and s == e["chosen"]["side"]) else 0 for o, s, _f, _fam in cands]
    if sum(label) != 1:
        continue
    data.append((e, cands, label))
fam_cols = data[0][1][0][3]
dates = sorted({e["ed"] for e, _c, _l in data})
folds = {d: i % 5 for i, d in enumerate(dates)}


def run(families, params=None, report=True):
    cols = [c for fam in families for c in fam_cols[fam]]
    top1 = top3 = 0
    mrr = 0.0
    per_event = []
    for k in range(5):
        train = [x for x in data if folds[x[0]["ed"]] != k]
        test = [x for x in data if folds[x[0]["ed"]] == k]
        X = np.array([[f[c] for c in cols] for _e, cands, _l in train for _o, _s, f, _fam in cands], dtype=float)
        y = np.array([v for _e, _c, lab in train for v in lab])
        group = [len(c) for _e, c, _l in train]
        model = lgb.LGBMRanker(objective="lambdarank", n_estimators=300, learning_rate=0.05, num_leaves=15,
                               min_child_samples=20, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                               verbose=-1, **(params or {}))
        model.fit(X, y, group=group)
        for e, cands, lab in test:
            Xt = np.array([[f[c] for c in cols] for _o, _s, f, _fam in cands], dtype=float)
            score = model.predict(Xt)
            order = np.argsort(-score)
            rank = int(np.where(np.array(lab)[order] == 1)[0][0]) + 1
            top1 += rank == 1
            top3 += rank <= 3
            mrr += 1.0 / rank
            per_event.append((e, rank, cands[order[0]]))
    n = len(data)
    if report:
        print(f"   {'+'.join(families):60s} top-1 {top1/n:.3f}  top-3 {top3/n:.3f}  MRR {mrr/n:.3f}  (n={n}, {len(cols)} features)")
    return per_event, cols


print("\n== B. ranker over every candidate leg, 5 date folds (out of sample)")
print(f"   candidates per event {statistics.mean(len(c) for _e, c, _l in data):.1f}; uniform top-1 {statistics.mean(1/len(c) for _e, c, _l in data):.3f}")
steps = [["market"], ["market", "minute_path"], ["market", "our", "raw", "points", "momentum", "weather_days"],
         ["market", "microstructure", "own_history"],
         ["market", "microstructure", "own_history", "minute_path"],
         ["market", "microstructure", "our", "raw", "points", "momentum", "weather_days", "own_history"],
         ["market", "microstructure", "our", "raw", "points", "momentum", "weather_days", "own_history", "minute_path"]]
results = {}
for fams in steps:
    results[tuple(fams)] = run(fams)

def FILLS_BEFORE(e):
    return any(t < e["ts"] for t, *_rest in FILLS.get((e["st"], e["ed"]), []))


def direction_agreement(per_event):
    ok = []
    for e, rank, top in per_event:
        if e["chosen"]["ord"] == e["mode_ord"]:
            continue
        d = e["chosen"]["ord"] - e["mode_ord"]
        their = (1 if d > 0 else -1) * (1 if e["chosen"]["side"] == "Yes" else -1)
        td = top[0] - e["mode_ord"]
        if td == 0:
            continue
        ok.append(((1 if td > 0 else -1) * (1 if top[1] == "Yes" else -1)) == their)
    return sum(ok) / max(1, len(ok)), len(ok)


print("\n== B2. off-mode legs: does the top pick stand on their side of the mode? (out of sample)")
for fams in steps:
    rate, n = direction_agreement(results[tuple(fams)][0])
    print(f"   {'+'.join(fams):60s} {rate:.3f} (n={n})")

print("\n== B3. the station-day's first touch (no earlier cluster fill on it) vs later episodes")
for fams in (["market"], ["market", "our", "raw", "points", "momentum", "weather_days"],
             ["market", "microstructure", "own_history"],
             ["market", "microstructure", "our", "raw", "points", "momentum", "weather_days", "own_history"]):
    pe = results[tuple(fams)][0]
    for label, keep in (("first touch", lambda e: not FILLS_BEFORE(e)), ("later", lambda e: FILLS_BEFORE(e))):
        sub = [x for x in pe if keep(x[0])]
        if not sub:
            continue
        t1 = sum(1 for _e, r, _t in sub if r == 1) / len(sub)
        t3 = sum(1 for _e, r, _t in sub if r <= 3) / len(sub)
        rate, n = direction_agreement(sub)
        print(f"   {'+'.join(fams):60s} {label:11s} n={len(sub):4d} top-1 {t1:.3f} top-3 {t3:.3f} side-of-mode {rate:.3f} (n={n})")

per_event, cols = results[tuple(steps[-1])]
print("\n== C0. how many legs the cluster buys in the same burst (any fill, +-120 s, same station-day)")
burst_sizes = []
burst_hit = 0
for e, rank, top in per_event:
    legs = {(b, s2) for t, b, s2, pr, sz in FILLS.get((e["st"], e["ed"]), []) if abs(t - e["ts"]) <= 120}
    burst_sizes.append(len(legs))
    if (e["labels"][top[0]], top[1]) in legs:
        burst_hit += 1
cnt = collections.Counter(min(n, 4) for n in burst_sizes)
print("   legs in the burst: " + ", ".join(f"{k}{'+' if k == 4 else ''}: {v/len(burst_sizes):.2f}" for k, v in sorted(cnt.items())))
print(f"   the full ranker's top pick is one of the burst's legs {burst_hit/len(per_event):.3f}")
print("\n== C. where the full ranker misses (out of sample)")
by = collections.defaultdict(lambda: [0, 0])
for e, rank, top in per_event:
    kind = ("YES" if e["chosen"]["side"] == "Yes" else "NO") + (" mode" if e["chosen"]["ord"] == e["mode_ord"] else " off-mode")
    by[kind][0] += 1
    by[kind][1] += rank == 1
for kind, (n, h) in sorted(by.items()):
    print(f"   their leg {kind:13s} n={n:4d} top-1 {h/n:.3f}")
side_ok = sum(1 for e, rank, top in per_event if top[1] == e["chosen"]["side"]) / len(per_event)
bucket_ok = sum(1 for e, rank, top in per_event if top[0] == e["chosen"]["ord"]) / len(per_event)
print(f"   top pick has their side {side_ok:.3f}; their bucket (either side) {bucket_ok:.3f}")
off = [(e, top) for e, rank, top in per_event if e["chosen"]["ord"] != e["mode_ord"]]
dir_ok = []
for e, top in off:
    d = e["chosen"]["ord"] - e["mode_ord"]
    their = (1 if d > 0 else -1) * (1 if e["chosen"]["side"] == "Yes" else -1)
    td = top[0] - e["mode_ord"]
    if td == 0:
        continue
    mine = (1 if td > 0 else -1) * (1 if top[1] == "Yes" else -1)
    dir_ok.append(mine == their)
print(f"   off-mode legs: the top pick's side of the mode matches theirs {sum(dir_ok)/max(1,len(dir_ok)):.3f} (n={len(dir_ok)})")

# feature importance of the full model on all data
X = np.array([[f[c] for c in cols] for _e, cands, _l in data for _o, _s, f, _fam in cands], dtype=float)
y = np.array([v for _e, _c, lab in data for v in lab])
model = lgb.LGBMRanker(objective="lambdarank", n_estimators=300, learning_rate=0.05, num_leaves=15, min_child_samples=20,
                       subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1, importance_type="gain")
model.fit(X, y, group=[len(c) for _e, c, _l in data])
imp = sorted(zip(cols, model.feature_importances_), key=lambda x: -x[1])
tot = sum(v for _c, v in imp)
print("\n== D. gain share of the full ranker (all data)")
print("   " + ", ".join(f"{c} {100*v/tot:.1f}%" for c, v in imp[:25]))


# ---------------------------------------------------------------- F. forward (round 26)
print("\n== F. forward: train on the whole research window, test on the cluster's first takers after it")
fwd_data = []
for e in FWD:
    cands = candidate_rows(e)
    label = [1 if (o == e["chosen"]["ord"] and s == e["chosen"]["side"]) else 0 for o, s, _f, _fam in cands]
    if sum(label) == 1:
        fwd_data.append((e, cands, label))
print(f"   forward events {len(fwd_data)} ({min(e['ed'] for e, _c, _l in fwd_data)}..{max(e['ed'] for e, _c, _l in fwd_data)}),"
      f" candidates per event {statistics.mean(len(c) for _e, c, _l in fwd_data):.1f}")


def forward(families):
    cols = [c for fam in families for c in fam_cols[fam]]
    X = np.array([[f[c] for c in cols] for _e, cands, _l in data for _o, _s, f, _fam in cands], dtype=float)
    y = np.array([v for _e, _c, lab in data for v in lab])
    model = lgb.LGBMRanker(objective="lambdarank", n_estimators=300, learning_rate=0.05, num_leaves=15,
                           min_child_samples=20, subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1)
    model.fit(X, y, group=[len(c) for _e, c, _l in data])
    pe = []
    for e, cands, lab in fwd_data:
        Xt = np.array([[f[c] for c in cols] for _o, _s, f, _fam in cands], dtype=float)
        order = np.argsort(-model.predict(Xt))
        rank = int(np.where(np.array(lab)[order] == 1)[0][0]) + 1
        pe.append((e, rank, cands[order[0]]))
    n = len(pe)
    t1 = sum(1 for _e, r, _t in pe if r == 1) / n
    t3 = sum(1 for _e, r, _t in pe if r <= 3) / n
    rate, nd = direction_agreement(pe)
    print(f"   {'+'.join(families):60s} top-1 {t1:.3f} top-3 {t3:.3f} side-of-mode {rate:.3f} (n={n}, dir n={nd})")
    return pe


for fams in (["market"], ["market", "minute_path"], ["market", "our", "raw", "points", "momentum", "weather_days"],
             ["market", "microstructure", "own_history"], ["market", "microstructure", "own_history", "minute_path"],
             ["market", "microstructure", "our", "raw", "points", "momentum", "weather_days", "own_history"],
             ["market", "microstructure", "our", "raw", "points", "momentum", "weather_days", "own_history", "minute_path"]):
    pe = forward(fams)
by_day = collections.defaultdict(list)
for e, r, _t in pe:
    by_day[e["ed"]].append(r)
print("   full model by event day: " + ", ".join(f"{d} n={len(v)} top-1 {sum(1 for x in v if x == 1)/len(v):.2f}" for d, v in sorted(by_day.items())))
kinds = collections.defaultdict(lambda: [0, 0])
for e, r, _t in pe:
    kind = ("YES" if e["chosen"]["side"] == "Yes" else "NO") + (" mode" if e["chosen"]["ord"] == e["mode_ord"] else " off-mode")
    kinds[kind][0] += 1
    kinds[kind][1] += r == 1
print("   full model by leg type: " + ", ".join(f"{k} n={n} top-1 {h/n:.2f}" for k, (n, h) in sorted(kinds.items())))
mix_r = collections.Counter(("YES" if e["chosen"]["side"] == "Yes" else "NO") + (" mode" if e["chosen"]["ord"] == e["mode_ord"] else " off") for e in FWD)
mix_o = collections.Counter(("YES" if e["chosen"]["side"] == "Yes" else "NO") + (" mode" if e["chosen"]["ord"] == e["mode_ord"] else " off") for e in E)
print("   leg mix research: " + ", ".join(f"{k} {v/len(E):.2f}" for k, v in sorted(mix_o.items())) +
      " | forward: " + ", ".join(f"{k} {v/len(FWD):.2f}" for k, v in sorted(mix_r.items())))

# ---------------------------------------------------------------- G. near misses (forward, full model)
print("\n== G. forward misses of the full model: how far was the top pick from their leg")
miss = collections.Counter()
for e, r, top in pe:
    if r == 1:
        miss["exact"] += 1
        continue
    o, s = top[0], top[1]
    co, cs = e["chosen"]["ord"], e["chosen"]["side"]
    if o == co and s != cs:
        miss["same bucket, other side"] += 1
    elif abs(o - co) == 1 and s == cs:
        miss["next bucket, same side"] += 1
    elif s == cs:
        miss["same side, 2+ buckets away"] += 1
    else:
        miss["other side, other bucket"] += 1
print("   " + ", ".join(f"{k} {v/len(pe):.3f}" for k, v in miss.most_common()))

# ---------------------------------------------------------------- H. more data: date-fold CV on research + forward
print("\n== H. date-fold CV on all events (research + forward), does more data lift the ceiling?")
union = data + fwd_data
dates_u = sorted({e["ed"] for e, _c, _l in union})
folds_u = {d: i % 5 for i, d in enumerate(dates_u)}
for fams in (["market"], ["market", "minute_path"], ["market", "microstructure", "own_history"],
             ["market", "microstructure", "own_history", "minute_path"],
             ["market", "microstructure", "our", "raw", "points", "momentum", "weather_days", "own_history", "minute_path"]):
    cols = [c for fam in fams for c in fam_cols[fam]]
    hits = t3 = 0
    pe_u = []
    for k in range(5):
        tr = [x for x in union if folds_u[x[0]["ed"]] != k]
        te = [x for x in union if folds_u[x[0]["ed"]] == k]
        X = np.array([[f[c] for c in cols] for _e, cands, _l in tr for _o, _s, f, _fam in cands], dtype=float)
        y = np.array([v for _e, _c, lab in tr for v in lab])
        m = lgb.LGBMRanker(objective="lambdarank", n_estimators=300, learning_rate=0.05, num_leaves=15, min_child_samples=20,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1)
        m.fit(X, y, group=[len(c) for _e, c, _l in tr])
        for e, cands, lab in te:
            Xt = np.array([[f[c] for c in cols] for _o, _s, f, _fam in cands], dtype=float)
            order = np.argsort(-m.predict(Xt))
            rank = int(np.where(np.array(lab)[order] == 1)[0][0]) + 1
            hits += rank == 1
            t3 += rank <= 3
            pe_u.append((e, rank, cands[order[0]]))
    rate, nd = direction_agreement(pe_u)
    print(f"   {'+'.join(fams):60s} top-1 {hits/len(union):.3f} top-3 {t3/len(union):.3f} side-of-mode {rate:.3f} (n={len(union)})")
