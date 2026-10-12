"""Round 50 (熊先生 10-12 「找到簇对标：f+1和f-1的显著特征」).

Every feature from rounds 40-49 in one table over the 227 pre-formation first
YES, F = the recalibrated production F; three classes by where the pick
falls: F-1, F, F+1 (picks further away left out of the contrasts).  For each
feature and each contrast (F+1 vs F, F-1 vs F): mean difference with a
day-block bootstrap interval (event days resampled -- station-days on a date
share weather), a permutation p-value with the labels shuffled within each
event day, AUC, and Benjamini-Hochberg false discovery control over all
tests.  Then a three-class logistic (F-1 / F / F+1) whose features are
selected inside each leave-one-event-day-out fold (training-fold p < 0.05),
scored as single-bucket agreement against always-F.
"""
import json
import math
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("skew_mean_median_mode_r46.py").read_text()
ns = {"__name__": "r46", "__file__": str(here.with_name("skew_mean_median_mode_r46.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("days = sorted({r[\"ev\"][\"ed\"] for r in rows})")[0], "r46_head", "exec"), ns)
rows, bucket, first_p0, recentre = (ns[k] for k in ("rows", "bucket", "first_p0", "recentre"))
r45 = ns["ns"]
prod_f, XN, labels_of = r45["prod_f"], r45["XN"], r45["labels_of"]
ALL_TRUTHS = r45["ns"]["ns"]["ALL_TRUTHS"]
TRUTH = {(st, date.fromisoformat(d)): v for (st, d), v in ALL_TRUTHS.items()}
IEM = {k: v for k, v in json.load(open(B / "iem_daily_r48.json")).items() if not k.startswith("_")}
src49 = here.with_name("physics_vs_cluster_r49.py").read_text()
ns49 = {"B": B, "json": json, "math": math, "statistics": statistics, "date": date, "timedelta": timedelta}
exec(compile(src49[src49.index("PH = json.load"):src49.index("def paired")] + src49[src49.index("def day_value"):src49.index("KEYS = [")],
             "r49_defs", "exec"), ns49)
phys = ns49["phys"]


def clim(st, D):
    vals = []
    for y in range(2016, 2026):
        c = D.replace(year=y)
        for k in range(-7, 8):
            v = IEM.get(st, {}).get((c + timedelta(days=k)).isoformat())
            if v is not None:
                vals.append(v)
    return (statistics.mean(vals), statistics.pstdev(vals)) if len(vals) >= 60 else (None, None)


picks_by_st = {}
for r in sorted(rows, key=lambda r: r["ev"]["ts"]):
    picks_by_st.setdefault(r["ev"]["st"], []).append(r)

feat = {}
for r in rows:
    ev = r["ev"]
    st, D = ev["st"], date.fromisoformat(ev["ed"])
    e = prod_f(ev["ts"])["station_days"][f"{st}|{ev['ed']}"]
    edges, fb, f = ev["edges"], ev["fb"], r["f"]
    centres = np.array([(a + b) / 2 for a, b in edges])
    x = dict(zip(XN, r["x"]))
    row = {k: x[k] for k in XN}
    row["obs->F change"] = e["recal_features"]["obs_to_f_change_c"]
    row["F_base - F"] = e["f_base_c"] - f
    row["L - F"] = (edges[1][0] + edges[-2][1]) / 2.0 - f
    row["log minutes"] = math.log1p(max(0.0, (ev["ts"] - ev["listed"]) / 60))
    row["listing hour UTC"] = (ev["listed"] % 86400) / 3600
    corrected = [v["tmax_c"] + v.get("bias_c", 0.0) for v in e["systems"].values()]
    shift = f - statistics.median(corrected)
    cen = [c + shift for c in corrected]
    row["members mass up - down"] = (sum(bucket(edges, c) > fb for c in cen) - sum(bucket(edges, c) < fb for c in cen)) / len(cen)
    row["members mean - F"] = statistics.mean(cen) - f
    row["momentum M"] = ev["M"]
    row["cluster station history"] = ev["hist"]
    for name in ("ENS@F", "P0first"):
        s = r["shape"].get(name)
        if not s:
            continue
        p = None
        if name == "ENS@F":
            praw = np.array([ev["ensp"].get(j, 0.0) for j in range(ev["n"])])
            p = recentre(praw / praw.sum(), edges, f)
            row["ENS raw mean - F"] = float((praw / praw.sum()) @ centres) - f
        else:
            p = first_p0(st, ev["ed"], labels_of[(st, ev["ed"])])
        order = np.argsort(-p, kind="stable")
        row[f"{name} p(F+1) - p(F-1)"] = float((p[fb + 1] if fb + 1 < len(p) else 0.0) - (p[fb - 1] if fb >= 1 else 0.0))
        row[f"{name} p2/p1"] = float(p[order[1]] / p[order[0]])
        row[f"{name} warm top2 at F"] = float(order[0] == fb and order[1] == fb + 1)
        row[f"{name} cold top2 at F"] = float(order[0] == fb and order[1] == fb - 1)
        row[f"{name} mean - mode"] = s["g_mm"]
        row[f"{name} mean - F (C)"] = float(p @ centres) - f
        row[f"{name} top1 = F"] = float(order[0] == fb)
    c, sd = clim(st, D)
    if c is not None:
        row["clim - F"] = c - f
        row["anomaly z"] = (f - c) / sd
    ph = phys(st, D, "previous_day3")
    if ph:
        for k in ("cloud", "sw", "precip", "cloud_anom", "sw_anom", "precip_anom", "cloud_chg", "sw_chg"):
            row[f"phys {k}"] = ph[k]
    hist = [TRUTH[(st, d)] for d in (D - timedelta(days=k) for k in range(3, 10)) if (st, d) in TRUTH]
    if len(hist) >= 5:
        row["recent truth - F"] = statistics.mean(hist) - f
    feat[id(r)] = row

cls = {id(r): (r["ev"]["choice"] - r["ev"]["fb"]) for r in rows}
days = sorted({r["ev"]["ed"] for r in rows})
names = sorted({k for v in feat.values() for k in v})
rng = np.random.default_rng(50)
print(f"{len(rows)} picks: F-1 {sum(c == -1 for c in cls.values())}, F {sum(c == 0 for c in cls.values())}, F+1 {sum(c == 1 for c in cls.values())},"
      f" further {sum(abs(c) > 1 for c in cls.values())}; {len(names)} features")


def auc(a, b):
    a, b = np.asarray(a), np.asarray(b)
    return float(((a[:, None] > b[None, :]).sum() + 0.5 * (a[:, None] == b[None, :]).sum()) / (len(a) * len(b)))


tests = []
for name in names:
    for target in (1, -1):
        sub = [r for r in rows if cls[id(r)] in (0, target) and name in feat[id(r)]]
        if sum(cls[id(r)] == target for r in sub) < 8:
            continue
        v = np.array([feat[id(r)][name] for r in sub], dtype=float)
        y = np.array([cls[id(r)] == target for r in sub])
        dd = np.array([r["ev"]["ed"] for r in sub])
        obs = v[y].mean() - v[~y].mean()
        sd_all = v.std() or 1.0
        # permutation within event day
        groups = [np.where(dd == d)[0] for d in set(dd)]
        cnt = 0
        for _ in range(2000):
            yp = y.copy()
            for g in groups:
                yp[g] = rng.permutation(y[g])
            if yp.sum() == 0 or (~yp).sum() == 0:
                continue
            cnt += abs(v[yp].mean() - v[~yp].mean()) >= abs(obs) - 1e-12
        p = (cnt + 1) / 2001
        # day-block bootstrap interval
        ud = sorted(set(dd))
        bs = []
        for _ in range(1000):
            idx = np.concatenate([np.where(dd == d)[0] for d in rng.choice(ud, size=len(ud), replace=True)])
            yy, vv = y[idx], v[idx]
            if yy.sum() and (~yy).sum():
                bs.append(vv[yy].mean() - vv[~yy].mean())
        tests.append({"name": name, "target": target, "n": int(y.sum()), "n0": int((~y).sum()), "diff": obs, "std_diff": obs / sd_all,
                      "lo": float(np.percentile(bs, 2.5)), "hi": float(np.percentile(bs, 97.5)), "p": p, "auc": auc(v[y], v[~y])})
# Benjamini-Hochberg
m = len(tests)
order = sorted(range(m), key=lambda i: tests[i]["p"])
q = [0.0] * m
prev = 1.0
for rank, i in reversed(list(enumerate(order, start=1))):
    prev = min(prev, tests[i]["p"] * m / rank)
    q[i] = prev
for i, t in enumerate(tests):
    t["q"] = q[i]
print(f"{m} tests (feature x contrast); sorted by permutation p.  q = Benjamini-Hochberg; * q < 0.10, + p < 0.05")
for t in sorted(tests, key=lambda t: t["p"])[:30]:
    flag = "*" if t["q"] < 0.10 else "+" if t["p"] < 0.05 else " "
    print(f"  {flag} {'F+1' if t['target'] == 1 else 'F-1'} vs F  {t['name']:30s} n {t['n']:3d}/{t['n0']:3d}: diff {t['diff']:+.3f} [{t['lo']:+.3f}, {t['hi']:+.3f}]"
          f" ({t['std_diff']:+.2f} sd), AUC {t['auc']:.2f}, p {t['p']:.4f}, q {t['q']:.3f}")

# Three-class model with in-fold selection, leave one event day out.
sub = [r for r in rows if abs(cls[id(r)]) <= 1]


def design(rs, cols, mu=None, sd=None):
    X = np.array([[feat[id(r)].get(c, np.nan) for c in cols] for r in rs], dtype=float)
    if mu is None:
        mu = np.nanmean(X, 0)
        sd = np.nanstd(X, 0)
        sd = np.where(sd > 1e-9, sd, 1.0)
    X = np.where(np.isnan(X), mu, X)
    return (X - mu) / sd, mu, sd


def fit_mlogit(X, y, l2=2.0, iters=800, lr=0.2):
    K = 3
    W = np.zeros((X.shape[1] + 1, K))
    Xb = np.c_[X, np.ones(len(X))]
    Y = np.eye(K)[y]
    for _ in range(iters):
        U = Xb @ W
        P = np.exp(U - U.max(1, keepdims=True))
        P /= P.sum(1, keepdims=True)
        G = Xb.T @ (P - Y) / len(X)
        G[:-1] += l2 * W[:-1] / len(X)
        W -= lr * G
    return W


hits, base, chosen_n = [], [], []
for d in days:
    tr = [r for r in sub if r["ev"]["ed"] != d]
    te = [r for r in sub if r["ev"]["ed"] == d]
    if not te:
        continue
    keep = []
    for name in names:
        for target in (1, -1):
            g = [r for r in tr if cls[id(r)] in (0, target) and name in feat[id(r)]]
            a = [feat[id(r)][name] for r in g if cls[id(r)] == target]
            b = [feat[id(r)][name] for r in g if cls[id(r)] == 0]
            if len(a) >= 8 and len(b) >= 8:
                se = math.sqrt(np.var(a) / len(a) + np.var(b) / len(b)) or 1e-9
                if abs(np.mean(a) - np.mean(b)) / se > 1.96 and name not in keep:
                    keep.append(name)
    chosen_n.append(len(keep))
    if not keep:
        for r in te:
            hits.append(r["ev"]["choice"] == r["ev"]["fb"])
            base.append(r["ev"]["choice"] == r["ev"]["fb"])
        continue
    Xtr, mu, sd = design(tr, keep)
    W = fit_mlogit(Xtr, np.array([cls[id(r)] + 1 for r in tr]))
    Xte, _, _ = design(te, keep, mu, sd)
    pred = np.argmax(np.c_[Xte, np.ones(len(Xte))] @ W, 1) - 1
    for r, pc in zip(te, pred):
        hits.append(r["ev"]["choice"] == max(0, min(r["ev"]["n"] - 1, r["ev"]["fb"] + int(pc))))
        base.append(r["ev"]["choice"] == r["ev"]["fb"])
w = sum(h and not b for h, b in zip(hits, base))
l_ = sum(b and not h for h, b in zip(hits, base))
n = len(hits)
dlt, hw = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
print(f"\nthree-class model, features selected inside each fold (median {statistics.median(chosen_n)} kept), leave one event day out, n {n}:"
      f" = cluster {statistics.mean(hits):.1%} vs always F {statistics.mean(base):.1%} ({dlt:+.3f} [{dlt - hw:+.3f}, {dlt + hw:+.3f}]); won {w}, lost {l_} against F")

# Parsimonious: keep only the k strongest features of the training fold (by |t| over both contrasts).
ALL_NAMES = list(names)
for label, pool in (("all features", ALL_NAMES),
                    ("as-of only (no P0first: our first P0 mostly lands after the trade)", [nm for nm in ALL_NAMES if not nm.startswith("P0first")])):
  names = pool
  print(f"\nthree-class model, top-k features chosen inside each fold, leave one event day out -- {label}")
  for k in (1, 2, 3, 5):
      hits, base, kept = [], [], {}
      for d in days:
          tr = [r for r in sub if r["ev"]["ed"] != d]
          te = [r for r in sub if r["ev"]["ed"] == d]
          if not te:
              continue
          score = {}
          for name in names:
              for target in (1, -1):
                  g = [r for r in tr if cls[id(r)] in (0, target) and name in feat[id(r)]]
                  a = [feat[id(r)][name] for r in g if cls[id(r)] == target]
                  b = [feat[id(r)][name] for r in g if cls[id(r)] == 0]
                  if len(a) >= 8 and len(b) >= 8:
                      se = math.sqrt(np.var(a) / len(a) + np.var(b) / len(b)) or 1e-9
                      score[name] = max(score.get(name, 0.0), abs(np.mean(a) - np.mean(b)) / se)
          keep = sorted(score, key=score.get, reverse=True)[:k]
          for nm in keep:
              kept[nm] = kept.get(nm, 0) + 1
          Xtr, mu, sd = design(tr, keep)
          W = fit_mlogit(Xtr, np.array([cls[id(r)] + 1 for r in tr]))
          Xte, _, _ = design(te, keep, mu, sd)
          pred = np.argmax(np.c_[Xte, np.ones(len(Xte))] @ W, 1) - 1
          for r, pc in zip(te, pred):
              hits.append((r, r["ev"]["choice"] == max(0, min(r["ev"]["n"] - 1, r["ev"]["fb"] + int(pc))), int(pc)))
              base.append(r["ev"]["choice"] == r["ev"]["fb"])
      hh = [h for _r, h, _p in hits]
      w = sum(h and not b for h, b in zip(hh, base))
      l_ = sum(b and not h for h, b in zip(hh, base))
      n = len(hh)
      dlt, hw = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
      mid = days[len(days) // 2]
      h1 = [h for r, h, _p in hits if r["ev"]["ed"] < mid]
      h2 = [h for r, h, _p in hits if r["ev"]["ed"] >= mid]
      b1 = [b for (r, _h, _p), b in zip(hits, base) if r["ev"]["ed"] < mid]
      b2 = [b for (r, _h, _p), b in zip(hits, base) if r["ev"]["ed"] >= mid]
      moved = sum(p != 0 for _r, _h, p in hits)
      tr_ = [(r, p) for r, _h, p in hits if r["ev"]["truth"] is not None]
      th = statistics.mean(r["ev"]["truth"] == r["ev"]["fb"] + p for r, p in tr_)
      tf = statistics.mean(r["ev"]["truth"] == r["ev"]["fb"] for r, p in tr_)
      print(f"  k {k}: = cluster {statistics.mean(hh):.1%} vs F {statistics.mean(base):.1%} ({dlt:+.3f} [{dlt - hw:+.3f}, {dlt + hw:+.3f}], won {w} lost {l_},"
            f" moved off F {moved}); halves {statistics.mean(h1):.1%} vs {statistics.mean(b1):.1%} / {statistics.mean(h2):.1%} vs {statistics.mean(b2):.1%};"
            f" truth hits {th:.1%} vs F {tf:.1%}; features kept {dict(sorted(kept.items(), key=lambda kv: -kv[1])[:6])}")

# Freeze the as-of one- and two-feature models (all picks within one bucket) for the forward read.
frozen = {"model": "cluster_f_plus_minus_mlogit_r50_v1", "classes": ["F-1", "F", "F+1"], "fitted_on_picks": len(sub),
          "event_days": [days[0], days[-1]], "forward_protocol": "pre-formation first YES on event days after 2026-10-11:"
          " argmax class -> bucket F + class, against bucket(f_c); read with S3 and r47 at >= 80 picks", "variants": {}}
for label, cols in (("one", ["ENS@F p(F+1) - p(F-1)"]), ("two", ["ENS@F p(F+1) - p(F-1)", "position"])):
    Xa, mu, sd = design(sub, cols)
    W = fit_mlogit(Xa, np.array([cls[id(r)] + 1 for r in sub]))
    frozen["variants"][label] = {"features": cols, "mean": mu.tolist(), "sd": sd.tolist(), "weights_by_class": W.T.tolist()}
    if label == "one":
        grid = np.linspace(-0.6, 0.6, 241)
        cl = [int(np.argmax(np.array([(g - mu[0]) / sd[0], 1.0]) @ W)) - 1 for g in grid]
        up = next((g for g, c in zip(grid, cl) if c == 1), None)
        dn = max((g for g, c in zip(grid, cl) if c == -1), default=None)
        frozen["variants"][label]["switch_points"] = {"F+1_when_at_least": None if up is None else round(float(up), 3),
                                                      "F-1_when_at_most": None if dn is None else round(float(dn), 3)}
        print(f"\nfrozen one-feature model: F+1 when ENS@F p(F+1) - p(F-1) >= {up}, F-1 when <= {dn}, else F")
json.dump(frozen, open(here.parent.parent / "cluster_f_plus_minus_r50_frozen.json", "w"), indent=1)
