"""Read-only: which part of the TAF weather, if any, predicts P0's error -- per field, net of P0's own radiation reading, by lead."""
import pickle, sys, json, math, statistics, sqlite3, collections
ROOT = "/Users/xiong/weather"
cases = pickle.load(open(sys.argv[1], "rb"))
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
rad = {}
for rid in {c["root_id"] for c in cases}:
    row = conn.execute("SELECT json_extract(lineage_json,'$.synoptic_cooling_json') FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1", (rid,)).fetchone()
    r = json.loads(row[0]) if row and row[0] else {}
    r = r if isinstance(r, dict) else {}
    rad[rid] = r
conn.close()
for _c in cases: _c["bucket"] = "h00_12" if _c["lead_h"] < 12 else ("h12_24" if _c["lead_h"] < 24 else "h24_48")
def corr(xs, ys):
    if len(xs) < 20: return float("nan")
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys)); sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else float("nan")
def resid(c): return (c["truth_c"] - c["root_mean_c"]) / c["sigma"]
arm = collections.Counter()
for c in cases:
    r = rad[c["root_id"]]
    c["kt_std"] = r.get("standardised") if r.get("state") == "RESOLVED" else None
    arm["clearness" if "clearness_index" in r else ("legacy" if r.get("state") else "none")] += 1
print("P0 radiation arm on these roots:", dict(arm))
ys = [resid(c) for c in cases]
print("n", len(cases), "residual mean", round(statistics.mean(ys), 3), "sd", round(statistics.pstdev(ys), 3))
for f in ("cloud", "precip", "wind"):
    print(f"corr({f}, residual) all={corr([c[f] for c in cases], ys):+.3f}", {b: round(corr([c[f] for c in cases if c['bucket'] == b], [resid(c) for c in cases if c['bucket'] == b]), 3) for b in ("h00_12", "h12_24", "h24_48")})
kt = [c for c in cases if c["kt_std"] is not None]
print("with a resolved radiation reading:", len(kt))
print(f"corr(kt_std, residual)={corr([c['kt_std'] for c in kt], [resid(c) for c in kt]):+.3f}  corr(kt_std, cloud)={corr([c['kt_std'] for c in kt], [c['cloud'] for c in kt]):+.3f}  corr(kt_std, precip)={corr([c['kt_std'] for c in kt], [c['precip'] for c in kt]):+.3f}")
# forecaster's increment over the model's radiation: residualise each field on kt_std
for f in ("cloud", "precip", "wind"):
    xs = [c["kt_std"] for c in kt]; zs = [c[f] for c in kt]
    mx, mz = statistics.mean(xs), statistics.mean(zs); beta = sum((x - mx) * (z - mz) for x, z in zip(xs, zs)) / sum((x - mx) ** 2 for x in xs)
    inc = [z - mz - beta * (x - mx) for x, z in zip(xs, zs)]
    print(f"increment of TAF {f} over the model radiation: corr with residual {corr(inc, [resid(c) for c in kt]):+.3f}  (beta={beta:+.3f})",
          {b: round(corr([i for i, c in zip(inc, kt) if c['bucket'] == b], [resid(c) for c in kt if c['bucket'] == b]), 3) for b in ("h00_12", "h12_24", "h24_48")})
# with TX vs without, and within-day (lead < 0, the window has started)
for name, sel in (("with TX", lambda c: c["tx_c"] is not None), ("without TX", lambda c: c["tx_c"] is None), ("lead<0 (window started)", lambda c: c["lead_h"] < 0), ("0<=lead<6", lambda c: 0 <= c["lead_h"] < 6)):
    rows = [c for c in cases if sel(c)]
    print(f"{name}: n={len(rows)}", {f: round(corr([c[f] for c in rows], [resid(c) for c in rows]), 3) for f in ("cloud", "precip", "wind")})
# precipitation as an event: heavy forecast rain days
for thr in (0.3, 0.6):
    wet = [resid(c) for c in cases if c["precip"] >= thr]; dry = [resid(c) for c in cases if c["precip"] < 0.05]
    print(f"precip>={thr}: n={len(wet)} mean residual {statistics.mean(wet):+.3f}σ  vs dry(<0.05) n={len(dry)} {statistics.mean(dry):+.3f}σ")
for lo, hi in ((0, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 1.01)):
    rows = [resid(c) for c in cases if lo <= c["cloud"] < hi]
    print(f"cloud [{lo},{hi}): n={len(rows)} mean residual {statistics.mean(rows):+.3f}σ")
