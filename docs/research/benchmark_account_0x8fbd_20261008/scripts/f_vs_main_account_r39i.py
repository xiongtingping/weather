"""Round 39i (熊先生 10-12 「目前首单有多少和簇匹配的，和主对标账号呢？」): production
F (six native systems, lambda 0.5) against (a) the cluster's pre-formation first
YES (any of its eight wallets), (b) those placed by the main account 0x8fbd7cf5,
(c) the main account's own first YES on the station-day whenever it came.  F
is read strictly as of each trade's own time.  Same 440 station-days.
"""
import collections
import json
import sys
from pathlib import Path

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
rows, ladder, G, bi, FR = ns["rows"], ns["ladder"], ns["G"], ns["bi"], ns["FR"]
MAIN = "0x8fbd7cf5"
fills = json.load(open(B / "cluster_fills_all.json"))
main_first = {}
for f in sorted(fills, key=lambda x: x["ts"]):
    if f["w"].startswith(MAIN) and f["outcome"] == "Yes":
        main_first.setdefault((f["station"], f["event_date"]), f)


def f_bucket(r, ts, lad):
    e = ns["prod_f"](int(ts))["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        return None
    return bi(lad, e["f_c"])


groups = collections.defaultdict(collections.Counter)
for r in rows:
    lad = ladder(G[r["key"]])
    if lad is None:
        continue
    y = r["labels"].index(r["truth"])
    fr = FR.get((r["st"], r["ed"]))
    if fr and fr["first"]["side"] == "Yes" and fr["first"]["bucket"] in r["labels"]:
        v = list(fr["now"].values())
        if not (all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4):
            ch = r["labels"].index(fr["first"]["bucket"])
            fb = f_bucket(r, fr["ts"], lad)
            if fb is not None:
                for name in ["cluster first YES (pre-formation)"] + (
                        ["  of which by the main account"] if fr["first"].get("w") == MAIN else []):
                    g = groups[name]
                    g["n"] += 1
                    g["same"] += fb == ch
                    g["within1"] += abs(fb - ch) <= 1
                    g["F up"] += fb > ch
                    g["F down"] += fb < ch
                    g["trade hit"] += ch == y
                    g["F hit"] += fb == y
    m = main_first.get((r["st"], r["ed"]))
    if m and m["bucket"] in r["labels"]:
        ch = r["labels"].index(m["bucket"])
        fb = f_bucket(r, m["ts"], lad)
        if fb is not None:
            g = groups["main account's own first YES (any time)"]
            g["n"] += 1
            g["same"] += fb == ch
            g["within1"] += abs(fb - ch) <= 1
            g["F up"] += fb > ch
            g["F down"] += fb < ch
            g["trade hit"] += ch == y
            g["F hit"] += fb == y
            g["mins after listing"] += (m["ts"] - int(r["listed"])) / 60.0
for name in ("cluster first YES (pre-formation)", "  of which by the main account", "main account's own first YES (any time)"):
    g = groups[name]
    n = max(1, g["n"])
    extra = f"  mean {g['mins after listing'] / n:.0f} min after listing" if g["mins after listing"] else ""
    print(f"{name:42s} n={g['n']:3d}  F = their bucket {g['same'] / n:.1%}  within one {g['within1'] / n:.1%}"
          f"  (F a bucket higher {g['F up'] / n:.0%}, lower {g['F down'] / n:.0%})"
          f"  hit: theirs {g['trade hit'] / n:.1%}, F {g['F hit'] / n:.1%}{extra}")
