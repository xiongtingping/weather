"""Round 33 part A -- buy the bias-corrected multi-model forecast bucket at
listing, as the protocol committed in README (0d45cf32) before scoring.

Universe: settled station-days with event day 2026-09-28..10-10 and a committed
WS snapshot of ours within 60 minutes after listing (gamma's earliest
acceptingOrdersTimestamp).  T = that first frame (secondary: the first frame
2-3 h and 6-8 h after listing).  F = bucket of the median of the ten models'
forecasts in hand at T, each with its station bias (truth - forecast over the
station's event days <= d-2, >=3, else 0; the forecast for a past day is the
one in hand an hour after its listing).  Legs, taker at the frame's best ask
(ask present, >= 5 shares at the best ask, price < 0.98), one share each:
L0 YES@F, L1 YES@F+1, L01 both, M YES@market mode at T.  Post-hoc, labelled:
C YES@bucket(raw median + 0.8 C + 0.3 x station correction), the cluster's
implied correction from part B.  Gross of fees; day-clustered 95% intervals.
"""
import collections
import gzip
import json
import math
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

B = Path(sys.argv[1])
G = json.load(open(B / "first_gamma.json"))
RUNS = {k: v for k, v in json.load(open(B / "first_runs.json")).items() if "error" not in v}
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
LAG = {"gfs_global": 4.0, "icon_global": 3.5, "ecmwf_ifs025": 7.0, "gem_global": 4.5, "ukmo_global_deterministic_10km": 5.5,
       "ecmwf_aifs025_single": 6.0, "meteofrance_arpege_world": 4.0, "jma_gsm": 4.0, "cma_grapes_global": 7.0, "ncep_aigfs025": 5.0}
TRUTH = {}
for st, ed, b, t in conn.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c FROM basket_station_day_settlement_label l WHERE event_date>='2026-09-20'"
        " AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    if b is not None:
        TRUTH[(st, ed)] = (str(b), None if t is None else float(t))
ts_ = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
sys.path.insert(0, str(Path(__file__).parent))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("fb", Path(__file__).with_name("first_trade_build.py"))
bounds = None
src = Path(__file__).with_name("first_trade_build.py").read_text()
ns = {"math": math, "re": __import__("re")}
exec(src[src.index("def bounds("):src.index("keep = []")], ns)
bounds = ns["bounds"]


def ladder(g):
    rows = []
    for b in g["buckets"]:
        bd, unit = bounds(b["label"])
        if bd is None:
            return None
        rows.append((bd, unit, b["label"]))
    rows.sort(key=lambda r: r[0][0])
    w = 1.0 if rows[0][1] == "C" else 10.0 / 9.0
    e = [list(r[0]) for r in rows]
    e[0][0] = e[0][1] - w
    e[-1][1] = e[-1][0] + w
    return {"labels": [r[2] for r in rows], "edges": e}


def bi(lad, x):
    e = lad["edges"]
    if x < e[0][1]:
        return 0
    for i, (lo, hi) in enumerate(e):
        if lo <= x < hi:
            return i
    return len(e) - 1


def fc(st, ed, m, at):
    d2 = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) - timedelta(days=2)
    best = None
    for run in (d2 - timedelta(hours=6), d2, d2 + timedelta(hours=6), d2 + timedelta(hours=12)):
        v = RUNS.get(f"{st}|{run.strftime('%Y-%m-%dT%H:%M')}", {}).get("tmax", {}).get(m, {}).get(ed)
        if v is not None and run.timestamp() + LAG[m] * 3600 <= at and (best is None or run > best[0]):
            best = (run, v)
    return None if best is None else best[1]


# bias history: the forecast in hand an hour after each station-day's listing
HIST = {}
for k, g in G.items():
    if "error" in g or not g.get("listed"):
        continue
    st, ed = k.split("|")
    for m in LAG:
        v = fc(st, ed, m, ts_(g["listed"]) + 3600)
        if v is not None:
            HIST[(st, ed, m)] = v


def bias(st, ed, m):
    cut = (date.fromisoformat(ed) - timedelta(days=2)).isoformat()
    v = [TRUTH[(s, d)][1] - x for (s, d, mm), x in HIST.items() if s == st and mm == m and d <= cut and (s, d) in TRUTH and TRUTH[(s, d)][1] is not None]
    return statistics.mean(v) if len(v) >= 3 else 0.0


def frame(st, ed, lo, hi):
    row = conn.execute("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                       " AND observed_at>? AND observed_at<=? ORDER BY observed_at LIMIT 1", (st, ed, iso(lo), iso(hi))).fetchone()
    return None if not row else (ts_(row[0]), json.loads(row[1]))


def ask_of(bk):
    """The best ask = the lowest ask level (the frame's best_ask field is empty right after listing)."""
    lv = sorted(((float(x["price"]), float(x["shares"])) for x in bk.get("ask_levels") or []), key=lambda t: t[0])
    if not bk.get("ask_available") or not lv:
        return None
    a, sh = lv[0]
    if not 0 < a < 0.98 or sh < 5:
        return None
    return a


WINDOWS = {"T (first frame <=60 min after listing)": (0, 3600), "first frame 2-3 h after listing": (7200, 10800), "first frame 6-8 h after listing": (21600, 28800)}
legs_out = {w: collections.defaultdict(lambda: collections.defaultdict(list)) for w in WINDOWS}
counts = collections.Counter()
for k, g in G.items():
    if "error" in g or not g.get("listed"):
        continue
    st, ed = k.split("|")
    if not "2026-09-28" <= ed <= "2026-10-10" or (st, ed) not in TRUTH:
        continue
    lad = ladder(g)
    if not lad or TRUTH[(st, ed)][0] not in lad["labels"]:
        continue
    y = lad["labels"].index(TRUTH[(st, ed)][0])
    lt = ts_(g["listed"])
    for wname, (a, b) in WINDOWS.items():
        fr = frame(st, ed, lt + a, lt + b)
        if not fr:
            continue
        T, buckets = fr
        raw = {m: fc(st, ed, m, T) for m in LAG}
        raw = {m: v for m, v in raw.items() if v is not None}
        if len(raw) < 3:
            counts[f"{wname}: <3 models"] += 1
            continue
        cor = {m: v + bias(st, ed, m) for m, v in raw.items()}
        F = bi(lad, statistics.median(cor.values()))
        med_r = statistics.median(raw.values())
        Cb = bi(lad, med_r + 0.8 + 0.3 * (statistics.median(cor.values()) - med_r))
        bk = {str(x["bucket"]): x for x in buckets}
        ref = market_reference_v1(buckets)
        mode = None
        if ref.get("state") == "complete":
            vec = {str(kk): float(v) for kk, v in ref["vector"].items()}
            mode = lad["labels"].index(max(vec, key=lambda b_: vec[b_])) if max(vec, key=lambda b_: vec[b_]) in lad["labels"] else None
        counts[f"{wname}: station-days"] += 1

        def buy(i, tag):
            if i is None or not 0 <= i < len(lad["labels"]):
                return
            x = bk.get(lad["labels"][i])
            a_ = ask_of(x) if x else None
            if a_ is None:
                counts[f"{wname}: {tag} no executable ask"] += 1
                return
            legs_out[wname][tag][ed].append(((1.0 if y == i else 0.0) - a_, a_, y == i))
        buy(F, "L0 YES@F")
        buy(F + 1, "L1 YES@F+1")
        buy(F, "L01 YES@F and F+1")
        buy(F + 1, "L01 YES@F and F+1")
        buy(mode, "M YES@market mode")
        buy(Cb, "C YES@cluster-implied bucket (post-hoc)")


def summary(book):
    keys = list(book)
    if not keys:
        return None
    rng = np.random.default_rng(33)
    S = np.array([[sum(p for p, a, h in book[k]), sum(a for p, a, h in book[k])] for k in keys])
    idx = rng.integers(0, len(keys), size=(2000, len(keys)))
    T = S[idx].sum(axis=1)
    bs = np.sort(T[:, 0] / T[:, 1])
    allv = [x for k in keys for x in book[k]]
    return (len(allv), len(keys), 100 * S[:, 0].sum() / S[:, 1].sum(), 100 * bs[50], 100 * bs[1949],
            statistics.mean(a for p, a, h in allv), statistics.mean(1.0 if h else 0.0 for p, a, h in allv))


print("counts:", dict(counts))
for wname in WINDOWS:
    print(f"\n== {wname}")
    for tag in ("L0 YES@F", "L1 YES@F+1", "L01 YES@F and F+1", "M YES@market mode", "C YES@cluster-implied bucket (post-hoc)"):
        s = summary(legs_out[wname][tag])
        if not s:
            print(f"   {tag:40s} no trades")
            continue
        n, nd, roi, lo, hi, price, hit = s
        print(f"   {tag:40s} legs {n:4d} on {nd:2d} days  price {price:.3f}  hit {hit:.3f}  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}]")

# post-hoc (not in the protocol): the cluster paid 0.35 on average -- does a price cap turn L0 / C around?
print("\n== post-hoc: L0 and C at T only when the ask is at or below a cap")
for tag in ("L0 YES@F", "C YES@cluster-implied bucket (post-hoc)"):
    for cap in (0.25, 0.30, 0.35, 0.40):
        book = collections.defaultdict(list)
        for d, lst in legs_out["T (first frame <=60 min after listing)"][tag].items():
            for p, a, h in lst:
                if a <= cap:
                    book[d].append((p, a, h))
        s = summary(book)
        if s:
            n, nd, roi, lo, hi, price, hit = s
            print(f"   {tag:40s} ask <= {cap:.2f}: legs {n:4d} on {nd:2d} days  price {price:.3f}  hit {hit:.3f}  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}]")

# post-hoc price correction (10-11, 熊先生「我一直觉得你对盈亏的计算有误」): the cluster's real taker fills in the
# first 2 h after listing sat 2.7 c (median) / 5.2 c (mean) below our frame's lowest ask, even with a frame <=60 s old;
# other wallets' taker buys median 0 / mean 3.8 c below.  Re-price the legs at our ask minus 3 c and minus 6 c.
print("\n== post-hoc: the T legs re-priced at our lowest ask minus 3 c / minus 6 c (the observed fill-vs-frame gaps at listing)")
for tag in ("L0 YES@F", "L1 YES@F+1", "C YES@cluster-implied bucket (post-hoc)"):
    for adj in (0.0, 0.03, 0.06):
        book = collections.defaultdict(list)
        for d, lst in legs_out["T (first frame <=60 min after listing)"][tag].items():
            for p, a, h in lst:
                a2 = max(0.01, a - adj)
                book[d].append(((1.0 if h else 0.0) - a2, a2, h))
        n, nd, roi, lo, hi, price, hit = summary(book)
        print(f"   {tag:40s} ask - {adj:.2f}: price {price:.3f}  hit {hit:.3f}  per share {100 * (hit - price):+.1f} c  ROI {roi:+.1f}% [{lo:+.1f}, {hi:+.1f}]")

# same station-days, same accounting (per share = hit - price): the cluster's first YES placed before the market formed
# vs our L0 at T.  Where the cluster made money and our copy did not: which station-days, which bucket, what price.
print("\n== same station-days: the cluster's pre-formation first YES vs our L0 at T (per share = hit - price; settled)")
R1 = json.load(gzip.open(B / "first_rows.json.gz", "rt"))
early_yes = {}
for r in R1:
    v = list(r["now"].values())
    formed = all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4
    if not formed and r["first"]["side"] == "Yes" and r["truth"]:
        early_yes[(r["st"], r["ed"])] = r
ours = {}
for d, lst in legs_out["T (first frame <=60 min after listing)"]["L0 YES@F"].items():
    pass
# rebuild L0 per station-day (legs_out is keyed by day only)
L0 = {}
for k, g in G.items():
    if "error" in g or not g.get("listed"):
        continue
    st, ed = k.split("|")
    if (st, ed) not in early_yes or (st, ed) not in TRUTH or not "2026-09-28" <= ed <= "2026-10-10":
        continue
    lad = ladder(g)
    lt = ts_(g["listed"])
    fr = frame(st, ed, lt, lt + 3600)
    if not lad or not fr:
        continue
    T, buckets = fr
    raw = {m: fc(st, ed, m, T) for m in LAG}
    raw = {m: v for m, v in raw.items() if v is not None}
    if len(raw) < 3:
        continue
    F = bi(lad, statistics.median([v + bias(st, ed, m) for m, v in raw.items()]))
    bk = {str(x["bucket"]): x for x in buckets}.get(lad["labels"][F])
    a = ask_of(bk) if bk else None
    if a is None:
        continue
    L0[(st, ed)] = (lad["labels"][F], a, TRUTH[(st, ed)][0] == lad["labels"][F])
both = [k for k in L0 if k in early_yes]
cl_hit = [1.0 if early_yes[k]["truth"] == early_yes[k]["first"]["bucket"] else 0.0 for k in both]
cl_px = [early_yes[k]["first"]["price"] for k in both]
our_hit = [1.0 if L0[k][2] else 0.0 for k in both]
our_px = [L0[k][1] for k in both]
same = sum(1 for k in both if L0[k][0] == early_yes[k]["first"]["bucket"])
print(f"   station-days {len(both)}: cluster hit {statistics.mean(cl_hit):.3f} price {statistics.mean(cl_px):.3f} -> {100 * (statistics.mean(cl_hit) - statistics.mean(cl_px)):+.1f} c/share;"
      f"   our L0 hit {statistics.mean(our_hit):.3f} price {statistics.mean(our_px):.3f} -> {100 * (statistics.mean(our_hit) - statistics.mean(our_px)):+.1f} c/share")
same_k = [k for k in both if L0[k][0] == early_yes[k]["first"]["bucket"]]
if same_k:
    print(f"   same bucket on {len(same_k)} of them: cluster paid {statistics.mean(early_yes[k]['first']['price'] for k in same_k):.3f}, our frame ask {statistics.mean(L0[k][1] for k in same_k):.3f}")
allL0 = [v for k, v in L0.items()]
