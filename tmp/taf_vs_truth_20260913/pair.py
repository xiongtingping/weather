"""Does TAF TX carry information about the settled high beyond the P0 centre standing when it arrived?"""
import json, collections, random, statistics as st, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo
D = pathlib.Path(__file__).resolve().parent
tafs = json.load(open(D / "tafs.json")); truth = json.load(open(D / "truth.json")); cen = json.load(open(D / "centres_flag.json"))
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
print("truth non-integer share", round(sum(abs(v[0] - round(v[0])) > 1e-9 for v in truth.values()) / len(truth), 3))

def bucket(t):
    h = (ts(t["received"]) - datetime.fromisoformat(t["day"]).replace(tzinfo=ZoneInfo(t["tz"]))).total_seconds() / 3600
    return "D-1及更早" if h < 0 else ("当日上午" if h < 12 else "当日午后")

latest = {}
for t in tafs:
    if t["source"] != "forward" or f"{t['station']}|{t['day']}" not in truth: continue
    k = (t["station"], t["day"], bucket(t))
    if k not in latest or ts(t["received"]) > ts(latest[k]["received"]): latest[k] = t
pairs = []
for (stn, day, b), t in latest.items():
    at = ts(t["received"]); hit = None
    for dec, c, fu, refused in cen.get(f"{stn}|{day}", []):
        if ts(dec) <= at and (fu is None or at < ts(fu)): hit = (float(c), refused)
    if hit is None: continue
    y = truth[f"{stn}|{day}"][0]
    pairs.append(dict(stn=stn, day=day, b=b, tx=t["tx"], p0=hit[0], refused=hit[1], y=y))
print("pairs", len(pairs), collections.Counter((p["b"], p["refused"]) for p in pairs))

def boot(rows, stat, n=2000, seed=7):
    days = sorted({r["day"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["day"]].append(r)
    rng = random.Random(seed); vals = []
    for _ in range(n):
        sample = [r for d in (rng.choice(days) for _ in days) for r in by[d]]
        v = stat(sample)
        if v is not None: vals.append(v)
    vals.sort(); return [round(vals[int(0.05 * len(vals))], 3), round(vals[int(0.95 * len(vals)) - 1], 3)]

def beta(rows):
    g = [r["tx"] - r["p0"] for r in rows]; e = [r["y"] - r["p0"] for r in rows]
    if len(rows) < 10: return None
    mg, me = st.mean(g), st.mean(e); var = sum((x - mg) ** 2 for x in g)
    return None if var == 0 else sum((x - mg) * (z - me) for x, z in zip(g, e)) / var

def walk_forward(rows, min_past=60):
    """Blend weight fitted only on event dates strictly before the scored date (pooled fleet)."""
    out = []
    for r in sorted(rows, key=lambda r: r["day"]):
        past = [p for p in rows if p["day"] < r["day"]]
        if len(past) < min_past: continue
        w = min(1.0, max(0.0, beta(past) or 0.0))
        out.append(dict(r, w=w, blend=r["p0"] + w * (r["tx"] - r["p0"])))
    return out

report = {}
for label, sel in [("全部", lambda p: True), ("偏差正常", lambda p: not p["refused"]), ("偏差被拒", lambda p: p["refused"])]:
    for b in ("D-1及更早", "当日上午", "当日午后", "合并"):
        rows = [p for p in pairs if sel(p) and (b == "合并" or p["b"] == b)]
        if len(rows) < 20: continue
        mae = lambda rs, f: st.mean(abs(f(r) - r["y"]) for r in rs)
        wf = walk_forward(rows)
        dis = [r for r in rows if abs(r["tx"] - r["p0"]) >= 1.5]
        report[f"{label}|{b}"] = dict(
            n=len(rows), days=len({r["day"] for r in rows}),
            mae_taf=round(mae(rows, lambda r: r["tx"]), 3), mae_p0=round(mae(rows, lambda r: r["p0"]), 3),
            beta=round(beta(rows), 3), beta_90=boot(rows, beta),
            walk_forward=None if len(wf) < 20 else dict(
                n=len(wf), mean_w=round(st.mean(r["w"] for r in wf), 3),
                mae_p0=round(mae(wf, lambda r: r["p0"]), 3), mae_blend=round(mae(wf, lambda r: r["blend"]), 3),
                diff_90=boot(wf, lambda s: st.mean(abs(r["blend"] - r["y"]) - abs(r["p0"] - r["y"]) for r in s) if s else None)),
            disagree_ge_1_5c=dict(n=len(dis), taf_closer=round(sum(abs(r["tx"] - r["y"]) < abs(r["p0"] - r["y"]) for r in dis) / max(1, len(dis)), 3),
                                  truth_on_taf_side_of_p0=round(sum((r["y"] - r["p0"]) * (r["tx"] - r["p0"]) > 0 for r in dis) / max(1, len(dis)), 3)),
        )
(D / "pair_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
for k, v in report.items(): print(k, json.dumps(v, ensure_ascii=False))
