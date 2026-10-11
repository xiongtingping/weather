"""Round 39 (熊先生 10-11 「挂牌前把f算好。接入确定性预报源」, charter 267).

Does the production listing-time F -- five NATIVE deterministic runs (AIFS
Single, IFS HRES, GFS, AIGFS, GDPS; ``listing_forecast_f.compute``) -- do
what the research F did?  Same 440 station-days as rounds 34/36/37 (event
days 09-28..10-10), at instants after listing.  Strict as-of: the runs are
those knowable then (backfilled rows are stamped at run init + the
publication lag); the truths are only days at least two calendar days
before the instant (so every station's day had ended; the research bias of
rounds 32-37 read up to event day -2, not yet settled at listing).  Compared
with the research C (ten Open-Meteo runs, raw median + 0.8 + 0.3 x station
bias), our served mode, the market mode at 06:00 local on the event day and
the cluster's pre-formation first YES.  Descriptive.
"""
import collections
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

B = Path(sys.argv[1])
here = Path(__file__)
src = here.with_name("forecast_then_mode_r36.py").read_text()
ns = {"__name__": "r36", "__file__": str(here.with_name("forecast_then_mode_r36.py"))}
argv = sys.argv
sys.argv = [argv[0], str(B)]
exec(compile(src.split("data = []")[0], "r36_head", "exec"), ns)
rows, market, conn, iso, tz, FR = ns["rows"], ns["market"], ns["conn"], ns["iso"], ns["tz"], ns["FR"]
src2 = here.with_name("listing_buy_r33.py").read_text()
ns2 = {"__name__": "r33", "__file__": str(here.with_name("listing_buy_r33.py")), "sys": sys}
exec(compile(src2.split("WINDOWS = {")[0], "r33_head", "exec"), ns2)
G, ladder, bi, fc, bias, LAG = (ns2[k] for k in ("G", "ladder", "bi", "fc", "bias", "LAG"))
src3 = here.with_name("f_vs_p0_r37.py").read_text()
served_src = src3[src3.index("def served("):src3.index("def det(")]
ns3 = {"conn": conn, "iso": iso, "ts_": ns2["ts_"]}
exec(served_src, ns3)
served = ns3["served"]
from zoneinfo import ZoneInfo  # noqa: E402

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

ALL_TRUTHS = F.formal_truths(conn, since=datetime(2026, 6, 1).date(), until=datetime(2026, 12, 31).date())
ROSTER = global_roster(conn)
_CACHE = {}


def prod_f(t):
    """Production F for every roster station x today..D+2 at instant ``t``."""

    key = int(t) // 600
    if key not in _CACHE:
        now = datetime.fromtimestamp(t, timezone.utc)
        cut = (now.date() - timedelta(days=2)).isoformat()
        F.formal_truths = lambda c, since, until: {k: v for k, v in ALL_TRUTHS.items()
                                                   if since.isoformat() <= k[1] <= min(until.isoformat(), cut)}
        _CACHE[key] = F.compute(conn, now=now, roster=ROSTER)
    return _CACHE[key]


def research_c(r, t):
    raw = {m: fc(r["st"], r["ed"], m, t) for m in LAG}
    raw = {m: v for m, v in raw.items() if v is not None}
    if len(raw) < 3:
        return None
    cor = {m: v + bias(r["st"], r["ed"], m) for m, v in raw.items()}
    med_c, med_r = statistics.median(cor.values()), statistics.median(raw.values())
    return med_r + 0.8 + 0.3 * (med_c - med_r)


TRUTH_C = {k: v[1] for k, v in ns2["TRUTH"].items()}
# Variants of the same inputs (raw median, fleet shift c, station correction
# corr); corr already carries the fleet's common bias (b_sm = truth - raw_m).
ARMS = {
    "production raw+c+0.3corr": lambda e: e["raw_median_c"] + e["fleet_c"] + 0.3 * e["station_corr_c"],
    "raw+c (no station)": lambda e: e["raw_median_c"] + e["fleet_c"],
    "raw+c+0.3(corr-c) (station part only)": lambda e: e["raw_median_c"] + e["fleet_c"] + 0.3 * (e["station_corr_c"] - e["fleet_c"]),
    "raw+corr (station bias alone)": lambda e: e["raw_median_c"] + e["station_corr_c"],
    "raw+0.8+0.3corr (research constant)": lambda e: e["raw_median_c"] + 0.8 + 0.3 * e["station_corr_c"],
    "raw (uncorrected)": lambda e: e["raw_median_c"],
}
INSTANTS = [("listing +5 min", 300), ("+1 h", 3600), ("+3 h", 3 * 3600)]
for name, off in INSTANTS:
    st_ = collections.Counter()
    hits = collections.Counter()
    fleet = collections.Counter()
    nsys = collections.Counter()
    gaps = []
    arm_hit = collections.Counter()
    pair = collections.Counter()  # (arm, "win"/"loss") against production, per station-day
    arm_err = collections.defaultdict(list)
    for r in rows:
        t = int(r["listed"]) + off
        lad = ladder(G[r["key"]])
        if lad is None:
            continue
        payload = prod_f(t)
        fleet[payload["fleet"]["source"]] += 1
        entry = payload["station_days"].get(f"{r['st']}|{r['ed']}")
        st_["n"] += 1
        if not entry or entry.get("state") != "READY":
            st_["no production F"] += 1
            continue
        nsys[entry["n_systems"]] += 1
        Fb = bi(lad, entry["f_c"])
        c = research_c(r, t)
        s = served(r, t)
        y = r["labels"].index(r["truth"])
        st_["F"] += 1
        hits["F"] += Fb == y
        prod_hit = bi(lad, ARMS["production raw+c+0.3corr"](entry)) == y
        for arm, f in ARMS.items():
            x = f(entry)
            arm_hit[arm] += bi(lad, x) == y
            pair[(arm, "win")] += (bi(lad, x) == y) and not prod_hit
            pair[(arm, "loss")] += prod_hit and not (bi(lad, x) == y)
            if TRUTH_C.get((r["st"], r["ed"])) is not None:
                arm_err[arm].append(TRUTH_C[(r["st"], r["ed"])] - x)
        hits["F within 1"] += abs(Fb - y) <= 1
        if c is not None:
            Cb = bi(lad, c)
            pair[("research C", "win")] += (Cb == y) and not prod_hit
            pair[("research C", "loss")] += prod_hit and not (Cb == y)
            st_["F&C"] += 1
            hits["F (F&C)"] += Fb == y
            hits["C (F&C)"] += Cb == y
            hits["F=C"] += Fb == Cb
            gaps.append(entry["f_c"] - c)
        if s is not None:
            st_["F&ours"] += 1
            hits["F (F&ours)"] += Fb == y
            hits["ours (F&ours)"] += s["mode"] == y
            hits["F=ours"] += Fb == s["mode"]
        y_, m_, dd = (int(x) for x in r["ed"].split("-"))
        mk = market(r, int(datetime(y_, m_, dd, 6, tzinfo=ZoneInfo(tz[r["st"]])).timestamp()))
        if mk is not None:
            st_["F&mk"] += 1
            hits["F=market"] += Fb == mk
            hits["market hit (F&mk)"] += mk == y
    n = max(1, st_["F"])
    print(f"\n== {name}: station-days {st_['n']}, production F {st_['F']} (none {st_['no production F']}); systems {dict(sorted(nsys.items()))}; fleet source {dict(fleet)}")
    print(f"   production F hits the truth {hits['F'] / n:.3f}, within one bucket {hits['F within 1'] / n:.3f}")
    for arm in ARMS:
        e = arm_err[arm]
        w, l = pair[(arm, "win")], pair[(arm, "loss")]
        half = 1.96 * (w + l) ** 0.5 / n
        print(f"     arm {arm:40s} hit {arm_hit[arm] / n:.3f}   truth-F mean {statistics.mean(e):+.2f} C  MAE {statistics.mean(abs(x) for x in e):.2f} C"
              f"  vs production {(w - l) / n:+.3f} [{(w - l) / n - half:+.3f}, {(w - l) / n + half:+.3f}]")
    w, l = pair[("research C", "win")], pair[("research C", "loss")]
    half = 1.96 * (w + l) ** 0.5 / max(1, st_["F&C"])
    print(f"     research C vs production: {(w - l) / max(1, st_['F&C']):+.3f} [{(w - l) / max(1, st_['F&C']) - half:+.3f}, {(w - l) / max(1, st_['F&C']) + half:+.3f}]")
    nc = max(1, st_["F&C"])
    print(f"   vs research C on {st_['F&C']}: F {hits['F (F&C)'] / nc:.3f}  C {hits['C (F&C)'] / nc:.3f}  same bucket {hits['F=C'] / nc:.2f}"
          + (f"  F-C mean {statistics.mean(gaps):+.2f} C median {statistics.median(gaps):+.2f} C" if gaps else ""))
    no = max(1, st_["F&ours"])
    print(f"   vs our served mode on {st_['F&ours']}: F {hits['F (F&ours)'] / no:.3f}  ours {hits['ours (F&ours)'] / no:.3f}  same bucket {hits['F=ours'] / no:.2f}")
    nm = max(1, st_["F&mk"])
    print(f"   = market mode at 06:00 local on the event day ({st_['F&mk']}): {hits['F=market'] / nm:.3f}  (that market mode hits {hits['market hit (F&mk)'] / nm:.3f})")

print("\n== the cluster's pre-formation first YES: bucket = production F / = research C")
cnt = collections.Counter()
for r in rows:
    fr = FR.get((r["st"], r["ed"]))
    if not fr or fr["first"]["side"] != "Yes" or fr["first"]["bucket"] not in r["labels"]:
        continue
    v = list(fr["now"].values())
    if all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4:
        continue
    lad = ladder(G[r["key"]])
    entry = prod_f(fr["ts"])["station_days"].get(f"{r['st']}|{r['ed']}")
    c = research_c(r, fr["ts"])
    if lad is None or not entry or entry.get("state") != "READY" or c is None:
        continue
    ch = r["labels"].index(fr["first"]["bucket"])
    cnt["n"] += 1
    cnt["=F"] += ch == bi(lad, entry["f_c"])
    cnt["=C"] += ch == bi(lad, c)
n = max(1, cnt["n"])
print(f"   n={cnt['n']}: = production F {cnt['=F'] / n:.2f}   = research C {cnt['=C'] / n:.2f}")
