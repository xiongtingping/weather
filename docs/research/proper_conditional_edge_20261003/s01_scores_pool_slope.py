"""T1-T3: information content. Model p (floored, as Proper lane reads it) vs market q (normalised mid)."""
import numpy as np, pandas as pd
from scipy.optimize import minimize_scalar
from common import frame, boot, fmt, HBL

df = frame()
df = df[df.q.notna()].copy()
df["lp"] = np.log(df.p); df["lq"] = np.log(df.q)
g = df.groupby("dec", sort=False)
dec = g.agg(st=("st","first"), d=("d","first"), stage=("stage","first"), hb=("hb","first"), h=("h","first"),
            first=("first","first"), ver=("ver","first"), era=("era","first"), unit=("unit","first"), nsys=("nsys","first"))
yrow = df[df.y == 1].set_index("dec")
dec["ls_p"] = yrow["lp"]; dec["ls_q"] = yrow["lq"]
dec["dls"] = dec.ls_p - dec.ls_q
df["bp"] = (df.p - df.y) ** 2; df["bq"] = (df.q - df.y) ** 2
dec["dbr"] = g["bp"].sum() - g["bq"].sum()
# ordinal means: centre error in bucket units
df["op"] = df.p * df.ord; df["oq"] = df.q * df.ord
dec["mu_p"] = g["op"].sum(); dec["mu_q"] = g["oq"].sum(); dec["y_ord"] = g["y_ord"].first()
dec["ae_p"] = (dec.mu_p - dec.y_ord).abs(); dec["ae_q"] = (dec.mu_q - dec.y_ord).abs()
dec["gap"] = dec.mu_p - dec.mu_q
# weight: each station-day counts once per (stage, lead bin)
dec["w"] = 1.0 / dec.groupby(["st","d","stage","hb"], observed=True)["dls"].transform("size")
dec.to_pickle("dec_scores.pkl")

print("=== T1 LogScore/Brier gap, model minus market (positive dLS = model better; negative dBrier = model better) ===")
print("stage  lead   era            n_sd   dLogScore [95% CI]                        dBrier [95% CI]                         |centre err| model vs market (buckets)")
for stage in ("P0", "P0-M", "P1"):
    for hb in HBL[::-1]:
        for era in ("all", "A:<09-12", "B:09-12..25", "C:09-26+"):
            s = dec[(dec.stage == stage) & (dec.hb == hb)]
            if era != "all":
                s = s[s.era == era]
            nsd = s.groupby(["st","d"]).ngroups
            if nsd < 40:
                continue
            a = boot(s.dls, s.w, s.d); b = boot(s.dbr, s.w, s.d)
            aep = np.average(s.ae_p, weights=s.w); aeq = np.average(s.ae_q, weights=s.w)
            print(f"{stage:5s} {hb:6s} {era:13s} {nsd:5d}   {fmt(a):42s} {fmt(b):42s} {aep:.3f} vs {aeq:.3f}")

def pool_ll(w, sub):
    z = (1 - w) * sub.lq.values + w * sub.lp.values
    m = np.exp(z)
    denom = np.bincount(sub.code.values, m)
    zy = z[sub.y.values == 1]
    return -(sub.wy.values * (zy - np.log(denom))).sum()

def fit_w(sub):
    return minimize_scalar(pool_ll, bounds=(-1.0, 2.0), args=(sub,), method="bounded").x

print()
print("=== T2 log-pool weight on the model: r ~ q^(1-w) p^w, w fitted by max likelihood (0 = market already contains the model; 1 = model alone) ===")
def prep(mask_dec):
    ids = dec.index[mask_dec]
    sub = df[df.dec.isin(ids)].copy()
    codes, _ = pd.factorize(sub.dec.values)
    sub["code"] = codes
    wy = dec.loc[sub[sub.y == 1].dec.values, "w"].values
    class S: pass
    s = S(); s.lq = sub.lq; s.lp = sub.lp; s.code = sub.code; s.y = sub.y; s.wy = pd.Series(wy); s.d = sub.d; s.sub = sub
    return s
rng = np.random.default_rng(11)
for label, mask in [
    ("P0 >36h all", (dec.stage == "P0") & (dec.h > 36)),
    ("P0 >36h era C", (dec.stage == "P0") & (dec.h > 36) & (dec.era == "C:09-26+")),
    ("P0 >36h era B", (dec.stage == "P0") & (dec.h > 36) & (dec.era == "B:09-12..25")),
    ("P0 first decision >36h", (dec.stage == "P0") & (dec.h > 36) & (dec["first"] == 1)),
    ("P0 24-36h", (dec.stage == "P0") & (dec.h <= 36)),
    ("P0-M all", (dec.stage == "P0-M")),
    ("P0-M era C", (dec.stage == "P0-M") & (dec.era == "C:09-26+")),
    ("P1 12-24h", (dec.stage == "P1") & (dec.h > 12)),
    ("P1 6-12h", (dec.stage == "P1") & (dec.h > 6) & (dec.h <= 12)),
    ("P1 0-6h", (dec.stage == "P1") & (dec.h <= 6)),
    ("P1 all era C", (dec.stage == "P1") & (dec.era == "C:09-26+")),
]:
    s = prep(mask)
    w_hat = fit_w(s)
    days = np.array(sorted(set(s.sub.d)))
    sub = s.sub
    by_day = {d: sub[sub.d == d] for d in days}
    ws = []
    for _ in range(200):
        pick = rng.choice(days, len(days))
        parts = []
        off = 0
        for d in pick:
            part = by_day[d]
            parts.append(part.assign(code=part.code + off)); off += 10**7
        bs = pd.concat(parts)
        codes, _ = pd.factorize(bs.code.values)
        class S: pass
        t = S(); t.lq = bs.lq; t.lp = bs.lp; t.code = pd.Series(codes); t.y = bs.y
        t.wy = pd.Series(dec.loc[bs[bs.y == 1].dec.values, "w"].values)
        ws.append(fit_w(t))
    lo, hi = np.percentile(ws, [2.5, 97.5])
    print(f"{label:26s} w = {w_hat:+.3f} [{lo:+.3f},{hi:+.3f}]  days={len(days)} decisions={int(mask.sum())}")

print()
print("=== T3 disagreement slope: (Y - q) = beta * (p - q), bucket level (0 = market right where they differ; 1 = model right) ===")
df["w"] = df.dec.map(dec.w)
df["dx"] = df.p - df.q; df["dy"] = df.y - df.q
def slope(sub, B=1000, seed=5):
    codes, uniq = pd.factorize(sub.d.values); k = len(uniq)
    sxy = np.bincount(codes, sub.w * sub.dx * sub.dy, k); sxx = np.bincount(codes, sub.w * sub.dx ** 2, k)
    est = sxy.sum() / sxx.sum()
    idx = np.random.default_rng(seed).integers(0, k, size=(B, k))
    bs = sxy[idx].sum(1) / sxx[idx].sum(1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return f"{est:+.3f} [{lo:+.3f},{hi:+.3f}]{'*' if lo > 0 or hi < 0 else ' '} days={k} n={len(sub)}"
df["stage"] = df["stage"]
for label, m in [
    ("P0 >36h all", (df.stage == "P0") & (df.h > 36)),
    ("P0 >36h era A", (df.stage == "P0") & (df.h > 36) & (df.era == "A:<09-12")),
    ("P0 >36h era B", (df.stage == "P0") & (df.h > 36) & (df.era == "B:09-12..25")),
    ("P0 >36h era C", (df.stage == "P0") & (df.h > 36) & (df.era == "C:09-26+")),
    ("P0 first >36h", (df.stage == "P0") & (df.h > 36) & (df["first"] == 1)),
    ("P0 24-36h", (df.stage == "P0") & (df.h <= 36)),
    ("P0-M", (df.stage == "P0-M")),
    ("P0-M era C", (df.stage == "P0-M") & (df.era == "C:09-26+")),
    ("P1 12-24h", (df.stage == "P1") & (df.h > 12)),
    ("P1 6-12h", (df.stage == "P1") & (df.h > 6) & (df.h <= 12)),
    ("P1 0-6h", (df.stage == "P1") & (df.h <= 6)),
    ("P1 era C", (df.stage == "P1") & (df.era == "C:09-26+")),
]:
    print(f"{label:18s} beta = {slope(df[m])}")
