"""Read-only: does 'did the forecast cloud/rain materialise' (METAR at P1 minus the latest TAF) predict P1's error?"""
import pickle, sys, json, math, statistics, sqlite3, collections, importlib.util, bisect
ROOT = "/Users/xiong/weather"; sys.path.insert(0, ROOT)
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
fitter = _load("_fit", f"{ROOT}/scripts/fit_taf_likelihood.py")
from bayes_basket_target.cross_consensus import distribution_sigma_c
from bayes_basket_target.materialization import _bucket_centers_c
cases = pickle.load(open(sys.argv[1], "rb"))
tafs = collections.defaultdict(list)
for c in cases: tafs[(c["station_id"], c["event_date"])].append((c["decided_at"], c["cloud"], c["precip"], c["wind"]))
for k in tafs: tafs[k].sort()
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
truth = fitter.load_truth_revisions(conn)
stations = sorted({c["station_id"] for c in cases})
rows = []; geo_cache = {}
for st in stations:
    fetched = conn.execute(
        "SELECT event_date, decision_at_utc, probabilities_json, bucket_ordinals_json,"
        " json_extract(lineage_json,'$.bucket_intervals_c_json'), json_extract(lineage_json,'$.p1_observation_likelihood_json')"
        " FROM basket_probability_envelope WHERE station_id=? AND stage='P1' AND state='COMMITTED' AND event_date>='2026-08-20'", (st,)).fetchall()
    for day, at, pj, oj, iv, lj in fetched:
        if (st, day) not in truth or not iv or not lj: continue
        tl = tafs.get((st, day))
        if not tl: continue
        t = fitter._epoch(at)
        i = bisect.bisect_right([x[0] for x in tl], t) - 1
        if i < 0: continue
        try: feats = (json.loads(lj) or {}).get("features") or {}
        except Exception: continue
        if feats.get("cloud_cover_fraction") is None or feats.get("precipitation_present") is None: continue
        probs = {str(k): float(v) for k, v in json.loads(pj).items()}; ords = {str(k): int(v) for k, v in json.loads(oj).items()}
        lin = {"bucket_intervals_c_json": iv}; axis = _bucket_centers_c(lin, bucket_ordinals=ords)
        labels = fitter._revision_labels(truth[(st, day)], lineage=lin, ordinals=ords)
        if not axis or not labels: continue
        order, centers = axis; mass = sum(probs.get(b, 0) for b in order)
        if mass <= 0: continue
        sig = distribution_sigma_c([probs.get(b, 0) for b in order], centers)
        if sig <= 0: continue
        mean = sum(probs.get(b, 0) * c for b, c in zip(order, centers)) / mass
        _, tc, tp, tw = tl[i]
        rows.append({"st": st, "day": day, "t": t, "resid": (labels[-1][2] - mean) / sig, "sig": sig,
                     "c_obs": float(feats["cloud_cover_fraction"]), "p_obs": float(feats["precipitation_present"]), "w_obs": float(feats.get("wind_speed_mps") or 0.0),
                     "c_taf": tc, "p_taf": tp, "w_taf": tw, "remaining_h": float(feats.get("remaining_hours") or 0.0), "clock_h": float(feats.get("clock_hours") or 0.0),
                     "probs": probs, "order": order, "centers": centers, "truth_bucket": labels[-1][3]})
conn.close()
per = collections.Counter((r["st"], r["day"]) for r in rows)
for r in rows: r["w"] = 1.0 / per[(r["st"], r["day"])]
print("P1 rows", len(rows), "station-days", len(per), "stations", len({r['st'] for r in rows}))
pickle.dump(rows, open(sys.argv[2], "wb"))
def wcorr(xs, ys, ws):
    W = sum(ws); mx = sum(w * x for w, x in zip(ws, xs)) / W; my = sum(w * y for w, y in zip(ws, ys)) / W
    sxy = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys)); sxx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs)); syy = sum(w * (y - my) ** 2 for w, y in zip(ws, ys))
    return sxy / math.sqrt(sxx * syy)
def partial(xs, zs, ys, ws):
    # corr(x, y | z): residualise x and y on z (weighted), then correlate
    def res(a):
        W = sum(ws); ma = sum(w * v for w, v in zip(ws, a)) / W; mz = sum(w * v for w, v in zip(ws, zs)) / W
        b = sum(w * (v - ma) * (z - mz) for w, v, z in zip(ws, a, zs)) / sum(w * (z - mz) ** 2 for w, z in zip(ws, zs))
        return [v - ma - b * (z - mz) for v, z in zip(a, zs)]
    return wcorr(res(xs), res(ys), ws)
ys = [r["resid"] for r in rows]; ws = [r["w"] for r in rows]
print("P1 residual weighted mean", round(sum(w * y for w, y in zip(ws, ys)) / sum(ws), 3))
for name, sel in (("all", lambda r: True), ("in window (clock 6-15)", lambda r: 6 <= r["clock_h"] < 15), ("before window (clock<6)", lambda r: r["clock_h"] < 6)):
    R = [r for r in rows if sel(r)]
    if len(R) < 50: continue
    y = [r["resid"] for r in R]; w = [r["w"] for r in R]
    mc = [r["c_obs"] - r["c_taf"] for r in R]; mp = [r["p_obs"] - r["p_taf"] for r in R]; mw = [r["w_obs"] - r["w_taf"] for r in R]
    print(f"== {name}: n={len(R)} station-days={len({(r['st'], r['day']) for r in R})}")
    print(f"   corr(obs cloud, resid)={wcorr([r['c_obs'] for r in R], y, w):+.3f}  corr(taf cloud, resid)={wcorr([r['c_taf'] for r in R], y, w):+.3f}  corr(cloud obs-taf, resid)={wcorr(mc, y, w):+.3f}  partial|obs={partial(mc, [r['c_obs'] for r in R], y, w):+.3f}")
    print(f"   corr(obs precip, resid)={wcorr([r['p_obs'] for r in R], y, w):+.3f}  corr(taf precip, resid)={wcorr([r['p_taf'] for r in R], y, w):+.3f}  corr(precip obs-taf, resid)={wcorr(mp, y, w):+.3f}  partial|obs={partial(mp, [r['p_obs'] for r in R], y, w):+.3f}")
    print(f"   corr(wind obs-taf, resid)={wcorr(mw, y, w):+.3f}  partial|obs={partial(mw, [r['w_obs'] for r in R], y, w):+.3f}")
    # the confirmation question as groups: forecast cloudy/wet vs what came
    for label, g in (("forecast cloudy(>=0.6), came clear(<0.3)", lambda r: r["c_taf"] >= 0.6 and r["c_obs"] < 0.3),
                     ("forecast cloudy(>=0.6), came cloudy(>=0.6)", lambda r: r["c_taf"] >= 0.6 and r["c_obs"] >= 0.6),
                     ("forecast clear(<0.3), came cloudy(>=0.6)", lambda r: r["c_taf"] < 0.3 and r["c_obs"] >= 0.6),
                     ("forecast clear(<0.3), came clear(<0.3)", lambda r: r["c_taf"] < 0.3 and r["c_obs"] < 0.3),
                     ("forecast wet(>=0.3), no rain came", lambda r: r["p_taf"] >= 0.3 and r["p_obs"] == 0),
                     ("forecast wet(>=0.3), rain came", lambda r: r["p_taf"] >= 0.3 and r["p_obs"] > 0),
                     ("forecast dry(<0.05), rain came", lambda r: r["p_taf"] < 0.05 and r["p_obs"] > 0)):
        G = [r for r in R if g(r)]
        if G: print(f"     {label}: n={len(G)} sd={len({(r['st'], r['day']) for r in G})} mean resid {sum(r['w'] * r['resid'] for r in G) / sum(r['w'] for r in G):+.3f}σ")
