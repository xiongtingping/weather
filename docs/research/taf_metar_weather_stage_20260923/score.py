"""Read-only: walk-forward value of a TAF cloud/precip/wind centre shift at P0-M, by TAF lead."""
import pickle, sys, math, statistics, random, collections, importlib.util
from datetime import datetime, timezone
ROOT = "/Users/xiong/weather"; sys.path.insert(0, ROOT)
from bayes_basket_target.cross_consensus import shift_probability
from bayes_basket_target.materialization import PROBABILITY_MODEL_ZERO_FLOOR as FLOOR
cases = pickle.load(open(sys.argv[1], "rb"))
DGRID = [round(-4 + 0.05 * i, 2) for i in range(161)]
def flog(p, t):
    lifted = {b: max(float(v), FLOOR) for b, v in p.items()}
    return math.log(lifted[t] / sum(lifted.values()))
def lead_bucket(h): return "h00_12" if h < 12 else ("h12_24" if h < 24 else "h24_48")
for c in cases:
    base = flog(c["probabilities"], c["truth_bucket"])
    curve = []
    for d in DGRID:
        if d == 0: curve.append(0.0); continue
        s = shift_probability(c["probabilities"], delta_c=d, ordered_keys=c["order"], key_centers_c=c["centers_list"])
        curve.append(flog(s, c["truth_bucket"]) - base)
    c["curve"] = curve; c["bucket"] = lead_bucket(c["lead_h"])
def gain(c, delta):
    if delta <= DGRID[0]: return c["curve"][0]
    if delta >= DGRID[-1]: return c["curve"][-1]
    x = (delta - DGRID[0]) / 0.05; i = int(x); f = x - i
    return c["curve"][i] * (1 - f) + c["curve"][i + 1] * f
FIELDS = ("cloud", "precip", "wind")
def norm(train):
    out = {}
    for f in FIELDS:
        v = [c[f] for c in train]; m = statistics.mean(v); s = statistics.pstdev(v) or 1.0; out[f] = (m, s)
    return out
def index(c, nz): return sum((c[f] - nz[f][0]) / nz[f][1] for f in FIELDS) / len(FIELDS)
KGRID = [round(0.025 * i, 3) for i in range(-8, 25)]   # -0.2 .. 0.6
BUCKETS = ("h00_12", "h12_24", "h24_48")
def fit(train, monotone=True):
    nz = norm(train)
    S = {b: {k: 0.0 for k in KGRID} for b in BUCKETS}
    for c in train:
        I = index(c, nz)
        for k in KGRID: S[c["bucket"]][k] += c["weight"] * gain(c, -k * I * c["sigma"])
    if not monotone:
        return nz, {b: max(KGRID, key=lambda k: (round(S[b][k], 9), -abs(k))) for b in BUCKETS}, S
    pos = [k for k in KGRID if k >= 0]
    best = None
    for k1 in pos:
        for k2 in pos:
            if k2 > k1: continue
            for k3 in pos:
                if k3 > k2: continue
                v = S["h00_12"][k1] + S["h12_24"][k2] + S["h24_48"][k3]
                if best is None or v > best[0] + 1e-12: best = (v, k1, k2, k3)
    return nz, dict(zip(BUCKETS, best[1:])), S
# lead distribution
print("lead buckets:", collections.Counter(c["bucket"] for c in cases), "tx:", collections.Counter((c["bucket"], c["tx_c"] is not None) for c in cases))
# in-sample full optimum and residual correlation
nz_all, k_all, S_all = fit(cases, monotone=False)
print("in-sample unconstrained k:", k_all, "norm:", {f: tuple(round(x, 4) for x in v) for f, v in nz_all.items()})
_, k_mono, _ = fit(cases)
print("in-sample monotone k:", k_mono)
for b in BUCKETS:
    rows = [c for c in cases if c["bucket"] == b]
    xs = [index(c, nz_all) for c in rows]; ys = [(c["truth_c"] - c["root_mean_c"]) / c["sigma"] for c in rows]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)); r = cov / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    print(f"  {b}: n={len(rows)} corr(index, standardised residual)={r:+.3f}  in-sample gain/case at k_mono={S_all[b][k_mono[b]] / max(sum(c['weight'] for c in rows),1e-9):+.5f} nat")
# walk-forward: fit once per UTC day on what was settled and decided before that day started
def day_of(ts): return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
def day_start(ts): d = datetime.fromtimestamp(ts, tz=timezone.utc); return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
arms = {"monotone_fit": lambda tr: fit(tr), "free_fit": lambda tr: fit(tr, monotone=False),
        "fixed_0.15_like_P0": lambda tr: (norm(tr), {b: 0.15 for b in BUCKETS}, None)}
fits = {}
res = {a: {"by_day": collections.defaultdict(lambda: [0.0, 0.0]), "by_bucket": collections.defaultdict(lambda: [0.0, 0.0]), "ks": collections.Counter(), "tx": collections.defaultdict(lambda: [0.0, 0.0])} for a in arms}
scored = unscored_after_truth = 0
for c in sorted(cases, key=lambda c: c["decided_at"]):
    at = c["decided_at"]
    if any(l[0] <= at for l in c["truth_revisions"]): unscored_after_truth += 1; continue
    ds = day_start(at)
    if ds not in fits:
        train = []
        for o in cases:
            if o["decided_at"] > ds: continue
            standing = [l for l in o["truth_revisions"] if l[0] <= ds]
            if not standing: continue
            lab = max(standing, key=lambda l: l[1])
            train.append({**o, "truth_bucket": lab[3]} if lab[3] == o["truth_bucket"] else {**o, "truth_bucket": lab[3], "curve": o["curve"]})
        fits[ds] = {a: (f(train) if len({t["event_date"] for t in train}) >= 7 else None) for a, f in arms.items()}
    for a in arms:
        fr = fits[ds][a]
        if fr is None: continue
        nz, ks, _ = fr
        k = ks[c["bucket"]]; g = gain(c, -k * index(c, nz) * c["sigma"])
        R = res[a]; R["by_day"][c["event_date"]][0] += c["weight"] * g; R["by_day"][c["event_date"]][1] += c["weight"]
        R["by_bucket"][c["bucket"]][0] += c["weight"] * g; R["by_bucket"][c["bucket"]][1] += c["weight"]
        R["tx"][c["tx_c"] is not None][0] += c["weight"] * g; R["tx"][c["tx_c"] is not None][1] += c["weight"]
        R["ks"][tuple(ks[b] for b in BUCKETS)] += 1
    scored += 1
print("scored", scored, "unscored (truth known before the TAF)", unscored_after_truth)
for a, R in res.items():
    means = [v[0] / v[1] for v in R["by_day"].values() if v[1] > 0]
    rng = random.Random(20260923)
    boot = sorted(statistics.mean(rng.choice(means) for _ in means) for _ in range(4000))
    print(f"== {a}: days={len(means)} by-day mean gain {statistics.mean(means):+.5f} nat  CI [{boot[100]:+.5f}, {boot[3899]:+.5f}]  days>0 {sum(1 for m in means if m > 0)}/{len(means)}")
    print("   by lead:", {b: (round(v[0] / v[1], 5), round(v[1], 1)) for b, v in sorted(R["by_bucket"].items())}, " with TX / without:", {k: round(v[0] / v[1], 5) for k, v in R["tx"].items()})
    print("   fitted k (h00_12,h12_24,h24_48) most used:", R["ks"].most_common(4))
