"""Round 60b (熊先生 10-13 「还是开盘即挂：用 v3.f_c 选桶」): quote from the first book after
listing on the bucket of the production F_v3 (``v3.f_c``, round 57 cache at listing + 5 min).
Anchor: the first book's mid when both sides are quoted, else best ask - 0.05.  Same rungs,
fill models, windows and settlement as round 60; also the F bucket (``f_c``) for comparison."""
import collections, gzip, json, sqlite3, statistics, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
B = Path(sys.argv[1])
FR = json.load(gzip.open(B / "first_rows.json.gz", "rt"))
C57 = json.load(open(B / "f_v3_backtest_r57.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
def edges_of(r):
    out = [[*r["bounds"][l]] for l in r["labels"]]; w = float(r.get("width") or 1.0)
    if out[0][0] is None: out[0][0] = out[0][1] - w
    if out[-1][1] is None: out[-1][1] = out[-1][0] + w
    return out
def bucket(e, x):
    if x < e[0][1]: return 0
    for i, (lo, hi) in enumerate(e):
        if lo <= x < hi: return i
    return len(e) - 1
data = []
for r in FR:
    if r.get("truth") not in r["labels"]: continue
    p = C57.get(str((int(r["listed"]) + 300) // 600 * 600), {}).get("station_days", {}).get(f"{r['st']}|{r['ed']}")
    if not p or p.get("v3") is None: continue
    e = edges_of(r)
    tv, tf = r["labels"][bucket(e, p["v3"])], r["labels"][bucket(e, p["f"])]
    t0 = float(r["listed"])
    rows = conn.execute("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                        " AND observed_at>=? AND observed_at<=? GROUP BY substr(observed_at,1,15) ORDER BY observed_at",
                        (r["st"], r["ed"], datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                         datetime.fromtimestamp(t0 + 15 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))).fetchall()
    ser = []
    for obs, bj in rows:
        book = {}
        for b in json.loads(bj):
            lab = str(b.get("bucket"))
            if lab not in (tv, tf): continue
            asks = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
            bid = float(b["bid_price"]) if b.get("bid_available") and b.get("bid_price") else None
            book[lab] = (bid, asks[0] if asks else None)
        ser.append((datetime.fromisoformat(obs.replace("Z", "+00:00")).timestamp(), book))
    if len(ser) < 3 or ser[0][0] - t0 > 7200: continue
    data.append({"ed": r["ed"], "ser": ser, "truth": r["truth"], "v3": tv, "f": tf})
print(f"{len(data)} station-days with a book within 2 h of listing; v3 bucket verifies {statistics.mean(d['v3'] == d['truth'] for d in data):.1%}, F bucket {statistics.mean(d['f'] == d['truth'] for d in data):.1%}; v3 != F on {statistics.mean(d['v3'] != d['f'] for d in data):.0%}")
PC, OFFS, SH = 0.95, (0.0, 0.02, 0.05), 5.0
def run(d, key, dd, w, model):
    lab = d[key]; t0 = d["ser"][0][0]
    anchor = None
    for ts, book in d["ser"]:
        bid, ask = book.get(lab, (None, None))
        if ask is not None:
            anchor = (bid + ask) / 2 if bid is not None else ask - 0.05; break
    if anchor is None: return 0.0, 0.0, 0, 0, 0
    xs = [round(anchor - dd - o, 3) for o in OFFS]; ys = [round(PC - (anchor - dd) - o, 3) for o in OFFS]
    tick = 0.001 if model == "through" else 0.0
    yf, nf = [False] * 3, [False] * 3
    for ts, book in d["ser"]:
        if ts > t0 + w * 3600: break
        bid, ask = book.get(lab, (None, None))
        for k in range(3):
            if not yf[k] and xs[k] > 0 and ask is not None and ask <= xs[k] - tick: yf[k] = True
            if not nf[k] and ys[k] > 0 and bid is not None and bid >= 1 - ys[k] + tick: nf[k] = True
    ysh, nsh = SH * sum(yf), SH * sum(nf)
    yc = SH * sum(x for x, f in zip(xs, yf) if f); nc = SH * sum(y for y, f in zip(ys, nf) if f)
    hit = lab == d["truth"]
    return ysh * hit + nsh * (not hit) - yc - nc, yc + nc, min(ysh, nsh), ysh, nsh
days = sorted({d["ed"] for d in data}); mid = days[len(days) // 2]; rng = np.random.default_rng(61)
print(f"\n{'bucket':6s} {'fill':7s} {'W':>3s} {'d':>5s} {'P&L/sd':>8s} {'95% CI':>18s} {'ret/cap':>8s} {'halves':>14s} {'filled':>6s} {'matched':>7s} {'nakedY':>6s} {'nakedN':>6s}")
for key in ("v3", "f"):
    for model in ("touch", "through"):
        for w in (3, 6, 12):
            for dd in (0.01, 0.03, 0.05):
                res = [(d["ed"], run(d, key, dd, w, model)) for d in data]
                pn = np.array([r[0] for _e, r in res]); cp = np.array([r[1] for _e, r in res])
                by = collections.defaultdict(list)
                for e, r in res: by[e].append(r[0])
                bs = [np.mean([x for e in rng.choice(days, size=len(days)) for x in by[e]]) for _ in range(400)]
                hv = [sum(r[0] for e, r in res if (e < mid) == hf) / max(1e-9, sum(r[1] for e, r in res if (e < mid) == hf)) for hf in (True, False)]
                fl = [r for _e, r in res if r[1] > 0]; ys_ = sum(r[3] for r in fl); ns_ = sum(r[4] for r in fl); mt = sum(r[2] for r in fl)
                print(f"{key:6s} {model:7s} {w:3d} {dd:5.2f} {pn.mean():+8.3f} [{np.percentile(bs, 2.5):+.3f}, {np.percentile(bs, 97.5):+.3f}] {pn.sum() / max(1e-9, cp.sum()):+8.1%}"
                      f" {hv[0]:+6.1%}/{hv[1]:+6.1%} {len(fl) / len(res):6.0%} {2 * mt / max(1e-9, ys_ + ns_):7.0%} {(ys_ - mt) / max(1e-9, ys_ + ns_):6.0%} {(ns_ - mt) / max(1e-9, ys_ + ns_):6.0%}")
