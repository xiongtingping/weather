"""Round 57 (熊先生 10-12 「不进行未来测试，回测」): the PRODUCTION F_v3 replayed.

The production code itself (``listing_forecast_f.compute`` after charter 267
补记六, with the backfilled GFS_SFLUX runs and the production terrain table),
strictly as of each instant: runs knowable then, truths only days at least two
calendar days before the instant.  Two universes:
  picks   the 227 pre-formation first YES (rounds 40-56), F computed at the
          trade's own 10-minute bucket: f_c, v3.f_c (cluster-like) and
          v3.f_recal_c (re-centred) against the pick (agreement) and the truth
  all     every first-rows station-day with a truth, at listing + 5 min: the
          three numbers against the truth (MAE, bucket hits, bias)
Results are cached per instant (<bench>/f_v3_backtest_r57.json) so the run
resumes across foreground chunks; pass --budget SECONDS.
"""
import gzip
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, "/Users/xiong/weather")
import sqlite3  # noqa: E402

from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

B = Path(sys.argv[1])
BUDGET = float(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 480.0
CACHE = B / "f_v3_backtest_r57.json"
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
ROSTER = global_roster(conn)
ALL_TRUTHS = F.formal_truths(conn, since=date(2026, 6, 1), until=date(2026, 12, 31))
real_truths = F.formal_truths
FR_ALL = json.load(gzip.open(B / "first_rows.json.gz", "rt"))
cache = json.load(open(CACHE)) if CACHE.exists() else {}


def preformation(r):
    v = list((r.get("now") or {}).values())
    return not (v and all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4)


def edges_of(r):
    out = []
    for lab in r["labels"]:
        lo, hi = r["bounds"][lab]
        out.append([lo, hi])
    w = float(r.get("width") or 1.0)
    if out[0][0] is None:
        out[0][0] = out[0][1] - w
    if out[-1][1] is None:
        out[-1][1] = out[-1][0] + w
    return out


def bucket(edges, x):
    if x < edges[0][1]:
        return 0
    for i, (lo, hi) in enumerate(edges):
        if lo <= x < hi:
            return i
    return len(edges) - 1


def payload_at(t):
    """Production F at instant t (10-minute bucket), truths cut at t - 2 days; cached."""
    key = str(int(t) // 600 * 600)
    if key in cache:
        return cache[key]
    now = datetime.fromtimestamp(int(key), timezone.utc)
    cut = (now.date() - timedelta(days=2)).isoformat()
    F.formal_truths = lambda c, since, until: {k: v for k, v in ALL_TRUTHS.items()
                                               if since.isoformat() <= k[1] <= min(until.isoformat(), cut)}
    try:
        payload = F.compute(conn, now=now, roster=ROSTER)
    finally:
        F.formal_truths = real_truths
    slim = {}
    for k, e in payload["station_days"].items():
        if e.get("state") != "READY":
            continue
        v3 = e.get("v3") or {}
        slim[k] = {"f": e["f_c"], "v3": v3.get("f_c") if v3.get("state") == "READY" else None,
                   "v3r": v3.get("f_recal_c") if v3.get("state") == "READY" else None,
                   "gfs": v3.get("gfs_source")}
    cache[key] = {"v3_state": payload["v3"]["state"], "station_days": slim}
    json.dump(cache, open(CACHE, "w"))
    return cache[key]


picks = [r for r in FR_ALL if r["first"]["side"] == "Yes" and r["first"]["bucket"] in r["labels"] and preformation(r)]
instants = sorted({int(r["ts"]) // 600 * 600 for r in picks} | {(int(r["listed"]) + 300) // 600 * 600 for r in FR_ALL if r.get("truth_c") is not None})
t0 = time.time()
todo = [t for t in instants if str(t) not in cache]
print(f"{len(instants)} instants, {len(todo)} to compute", file=sys.stderr)
for t in todo:
    if time.time() - t0 > BUDGET:
        print(f"budget reached; {len([t for t in instants if str(t) not in cache])} instants left -- rerun", file=sys.stderr)
        sys.exit(0)
    payload_at(t)
print("all instants computed", file=sys.stderr)


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


# ---------------------------------------------------------------- picks
rows = []
for r in picks:
    p = payload_at(int(r["ts"]))["station_days"].get(f"{r['st']}|{r['ed']}")
    if not p or p["v3"] is None:
        continue
    edges = edges_of(r)
    rows.append({"ed": r["ed"], "st": r["st"], "edges": edges, "choice": r["labels"].index(r["first"]["bucket"]),
                 "truth": r["labels"].index(r["truth"]) if r.get("truth") in r["labels"] else None,
                 "f": p["f"], "v3": p["v3"], "v3r": p["v3r"], "gfs": p["gfs"], "early": int(r["ts"]) - float(r["listed"]) < 900})
days = sorted({x["ed"] for x in rows})
mid = days[len(days) // 2]
print(f"\n(1) the cluster's {len(rows)} pre-formation first YES ({days[0]}..{days[-1]}); GFS member from the flux grid on"
      f" {sum(x['gfs'] == 'NOAA_GFS_SFLUX_13KM_NATIVE_POINT' for x in rows)} picks")
base = [bucket(x["edges"], x["f"]) == x["choice"] for x in rows]
for name, key in (("F (production f_c)", "f"), ("F_v3 cluster-like (v3.f_c)", "v3"), ("F_v3 re-centred (v3.f_recal_c)", "v3r")):
    sub = [x for x in rows if x[key] is not None]
    same = [bucket(x["edges"], x[key]) == x["choice"] for x in sub]
    bs = [bucket(x["edges"], x["f"]) == x["choice"] for x in sub]
    tr = [x for x in sub if x["truth"] is not None]
    h1 = statistics.mean(s for s, x in zip(same, sub) if x["ed"] < mid) - statistics.mean(b for b, x in zip(bs, sub) if x["ed"] < mid)
    h2 = statistics.mean(s for s, x in zip(same, sub) if x["ed"] >= mid) - statistics.mean(b for b, x in zip(bs, sub) if x["ed"] >= mid)
    e_s = [s for s, x in zip(same, sub) if x["early"]]
    e_b = [b for b, x in zip(bs, sub) if x["early"]]
    print(f"  {name:32s} n {len(sub)}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(bs):.1%} {paired(same, bs)} halves {h1:+.3f}/{h2:+.3f};"
          f" early (<15 min) {statistics.mean(e_s):.1%} vs {statistics.mean(e_b):.1%} (n {len(e_s)});"
          f" hits {statistics.mean(bucket(x['edges'], x[key]) == x['truth'] for x in tr):.1%} vs F {statistics.mean(bucket(x['edges'], x['f']) == x['truth'] for x in tr):.1%};"
          f" mean (x - F) {statistics.mean(x[key] - x['f'] for x in sub):+.2f} C")
cl_hits = statistics.mean(x["choice"] == x["truth"] for x in rows if x["truth"] is not None)
print(f"  cluster itself hits the truth {cl_hits:.1%}")

# ---------------------------------------------------------------- all station-days
A = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5:
        continue
    p = payload_at(int(r["listed"]) + 300)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not p or p["v3"] is None or p["v3r"] is None:
        continue
    A.append({"ed": r["ed"], "edges": edges_of(r), "y": float(r["truth_c"]), "f": p["f"], "v3": p["v3"], "v3r": p["v3r"]})
daysA = sorted({a["ed"] for a in A})
by_day = {d: [i for i, a in enumerate(A) if a["ed"] == d] for d in daysA}
rng = np.random.default_rng(57)
print(f"\n(2) every first-rows station-day with a truth, F at listing + 5 min: n {len(A)}, {len(daysA)} event days")
fh = [bucket(a["edges"], a["f"]) == bucket(a["edges"], a["y"]) for a in A]
print(f"  {'F (production f_c)':32s} MAE {np.mean([abs(a['y'] - a['f']) for a in A]):.4f}; hits {statistics.mean(fh):.1%}; bias {np.mean([a['y'] - a['f'] for a in A]):+.3f}")
for name, key in (("F_v3 cluster-like (v3.f_c)", "v3"), ("F_v3 re-centred (v3.f_recal_c)", "v3r")):
    dm = np.array([abs(a["y"] - a[key]) - abs(a["y"] - a["f"]) for a in A])
    bs = [dm[np.concatenate([by_day[d] for d in rng.choice(daysA, size=len(daysA), replace=True)])].mean() for _ in range(600)]
    hit = [bucket(a["edges"], a[key]) == bucket(a["edges"], a["y"]) for a in A]
    print(f"  {name:32s} MAE {np.mean([abs(a['y'] - a[key]) for a in A]):.4f} ({dm.mean():+.4f} [{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]);"
          f" hits {statistics.mean(hit):.1%} vs F {statistics.mean(fh):.1%} {paired(hit, fh)}; bias {np.mean([a['y'] - a[key] for a in A]):+.3f}")
