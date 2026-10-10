"""Round 24 (熊先生 10-10 「他在什么条件下选择众数桶？」): when does the cluster's
first taker buy land on the market's modal bucket -- YES on it, or NO on it --
and when do those legs pay?

Data: round 14's 1,491 first-taker events (choice_rows.jsonl.gz: the whole
two-sided ladder at the fill, our served p, yesterday's truth, today's running
high).  ECMWF momentum M at the fill from production's shared reader
(direction_signal.momentum, runs as we had them).  Descriptive; same 15 event
days as every earlier round.
"""
import collections
import gzip
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import direction_signal as ds  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
rows = [json.loads(line) for line in gzip.open(HERE / "choice_rows.jsonl.gz", "rt")]
conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
conn.execute("PRAGMA query_only=1")


def centre_c(label: str) -> float | None:
    m = re.match(r"^(-?\d+)(?:-(-?\d+))?°([CF])", label)
    if not m:
        m2 = re.match(r"^(-?\d+)°([CF]) or (below|higher)", label)
        if not m2:
            return None
        v, unit = float(m2.group(1)), m2.group(2)
        return v if unit == "C" else (v - 32) * 5 / 9
    lo, hi, unit = float(m.group(1)), float(m.group(2) or m.group(1)), m.group(3)
    c = (lo + hi) / 2 + (0.5 if unit == "F" and m.group(2) else 0.0)
    return c if unit == "C" else (c - 32) * 5 / 9


def region(st: str) -> str:
    if st[0] in "ZRVW" or st in ("OPKC", "OEJN"):
        return "Asia"
    if st[0] in "ELU":
        return "Europe/ME"
    if st[0] in "FN":
        return "Africa/Oceania"
    return "Americas"


E = []
for r in rows:
    yes = sorted((a for a in r["alts"] if a["side"] == "Yes"), key=lambda a: a["ord"])
    ch = next(a for a in r["alts"] if a["chosen"])
    mode = next(a for a in yes if a["d_mode"] == 0)
    mode_no = next(a for a in r["alts"] if a["side"] == "No" and a["d_mode"] == 0)
    qs = sorted((a["q"] for a in yes), reverse=True)
    width = (yes[1]["ord"] - yes[0]["ord"]) if len(yes) > 1 else 1
    unit_c = 1.0 if r["unit"] == "C" else 10.0 / 9.0
    mc = centre_c(mode["bucket"])
    hit_mode = 1.0 if r["truth_ord"] == r["mode_ord"] else 0.0
    m = ds.momentum(conn, station_id=r["station"], event_date=r["event_date"], at_utc=r["ts"])
    e = dict(
        st=r["station"], ed=r["event_date"], lead=r["lead_h"], unit=r["unit"], region=region(r["station"]),
        usdc=r["usdc"], side=ch["side"], is_mode=ch["d_mode"] == 0, q_mode=mode["q"], gap=qs[0] - (qs[1] if len(qs) > 1 else 0),
        ask_mode=mode["price"], no_mode=mode_no["price"], spread=mode["spread"], p_mode=mode["p"],
        our_agrees=r["our_mode_ord"] == r["mode_ord"], mom1h=mode.get("mom"),
        y_rungs=None if (r.get("ytruth_c") is None or mc is None) else (r["ytruth_c"] - mc) / unit_c,
        run_rungs=None if (r.get("tmax_today") is None or mc is None or not r.get("is_d0")) else (r["tmax_today"] - mc) / unit_c,
        M=m["M"], hit_mode=hit_mode, chosen_price=ch["price"],
        half="H1" if r["event_date"] < "2026-09-30" else "H2",
    )
    E.append(e)
print(f"events {len(E)}: YES-mode {sum(1 for e in E if e['side']=='Yes' and e['is_mode'])}, YES-off {sum(1 for e in E if e['side']=='Yes' and not e['is_mode'])},"
      f" NO-mode {sum(1 for e in E if e['side']=='No' and e['is_mode'])}, NO-off {sum(1 for e in E if e['side']=='No' and not e['is_mode'])}")


def bins(name, fn, cuts):
    def label(v):
        if v is None:
            return "n/a"
        for lo, hi, lab in cuts:
            if lo <= v < hi:
                return lab
        return "n/a"
    return name, lambda e: label(fn(e))


SPLITS = [
    bins("market q of the mode", lambda e: e["q_mode"], [(0, .35, "<0.35"), (.35, .45, "0.35-0.45"), (.45, .55, "0.45-0.55"), (.55, 2, ">=0.55")]),
    bins("mode lead over 2nd bucket", lambda e: e["gap"], [(0, .1, "<0.10"), (.1, .2, "0.10-0.20"), (.2, .35, "0.20-0.35"), (.35, 2, ">=0.35")]),
    bins("lead to local noon (h)", lambda e: e["lead"], [(-99, 12, "<12"), (12, 24, "12-24"), (24, 48, "24-48"), (48, 999, ">=48")]),
    bins("ECMWF momentum |M|", lambda e: None if e["M"] is None else abs(e["M"]), [(0, .5, "quiet <0.5"), (.5, 1.5, "0.5-1.5"), (1.5, 99, ">=1.5")]),
    bins("our p(mode) - market q(mode)", lambda e: e["p_mode"] - e["q_mode"], [(-9, -.15, "<-0.15"), (-.15, -.05, "-0.15..-0.05"), (-.05, .05, "+-0.05"), (.05, 9, ">0.05")]),
    ("our mode = market mode", lambda e: "same" if e["our_agrees"] else "differs"),
    bins("mode mid 1h change", lambda e: e["mom1h"], [(-9, -.02, "fell >2c"), (-.02, .02, "flat"), (.02, 9, "rose >2c")]),
    bins("yesterday's truth vs mode (rungs)", lambda e: e["y_rungs"], [(-99, -1.5, "<=-2 (yday colder)"), (-1.5, -.5, "-1"), (-.5, .5, "on mode"), (.5, 1.5, "+1"), (1.5, 99, ">=+2 (yday warmer)")]),
    bins("D0 running high vs mode (rungs)", lambda e: e["run_rungs"], [(-99, -2.5, "<=-3 below"), (-2.5, -.5, "1-2 below"), (-.5, .5, "in mode"), (.5, 99, "above")]),
    bins("first fill size (USDC)", lambda e: e["usdc"], [(0, 10, "<10"), (10, 30, "10-30"), (30, 100, "30-100"), (100, 1e9, ">=100")]),
    ("unit", lambda e: e["unit"]),
    ("region", lambda e: e["region"]),
]


def share_table(side):
    pool = [e for e in E if e["side"] == side]
    base = sum(e["is_mode"] for e in pool) / len(pool)
    print(f"\n== {side}: share of {side} first buys that land ON the mode (base {base:.3f}, n={len(pool)})")
    for name, fn in SPLITS:
        groups = collections.defaultdict(list)
        for e in pool:
            groups[fn(e)].append(e)
        cells = []
        for lab in sorted(groups, key=str):
            g = groups[lab]
            if len(g) < 15 or lab == "n/a":
                continue
            cells.append(f"{lab}: {sum(x['is_mode'] for x in g)/len(g):.2f} (n={len(g)})")
        print(f"   {name:36s} " + " | ".join(cells))


def payoff(e):
    if e["side"] == "Yes":
        return e["hit_mode"] - e["ask_mode"], e["ask_mode"], e["hit_mode"]
    return (1 - e["hit_mode"]) - e["no_mode"], e["no_mode"], 1 - e["hit_mode"]


def payoff_table(side):
    pool = [e for e in E if e["side"] == side and e["is_mode"]]
    print(f"\n== {side} on the mode: hit minus price (cents/share at the price they paid), n={len(pool)}")
    def line(lab, g):
        if len(g) < 15:
            return f"{lab}: n={len(g)}"
        v = [payoff(e) for e in g]
        h = {hh: [payoff(e)[0] for e in g if e["half"] == hh] for hh in ("H1", "H2")}
        hs = "/".join(f"{100*sum(x)/len(x):+.1f}" if x else "-" for x in h.values())
        return f"{lab}: {100*sum(x[0] for x in v)/len(v):+.1f}c (price {sum(x[1] for x in v)/len(v):.2f} hit {sum(x[2] for x in v)/len(v):.2f}, H1/H2 {hs}, n={len(g)})"
    print("   all: " + line("all", pool))
    for name, fn in SPLITS:
        groups = collections.defaultdict(list)
        for e in pool:
            groups[fn(e)].append(e)
        cells = [line(lab, groups[lab]) for lab in sorted(groups, key=str) if lab != "n/a" and len(groups[lab]) >= 15]
        if cells:
            print(f"   {name:36s} " + " | ".join(cells))


for side in ("Yes", "No"):
    share_table(side)
for side in ("Yes", "No"):
    payoff_table(side)

# the market's own mode, every event (what the market said vs what landed), for reference
print("\n-- reference: market mode on all 1,491 events: said {:.3f}, landed {:.3f}".format(
    sum(e["q_mode"] for e in E) / len(E), sum(e["hit_mode"] for e in E) / len(E)))
