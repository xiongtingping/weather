"""Round 40b: does the cluster follow one model?  Each native F member alone
(raw, + fleet shift, + its own full bias) and each of the research's ten
Open-Meteo models alone (strict-as-of bias), against the cluster's
pre-formation first YES; main account vs the other wallets.
"""
import collections
import gzip
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events, prod_f, bucket = ns["events"], ns["prod_f"], ns["bucket"]
r39 = ns["ns"]
fc, LAG, HIST, TRUTH = r39["fc"], r39["LAG"], r39["ns2"]["HIST"], r39["ns2"]["TRUTH"]
FR = {(r["st"], r["ed"]): r for r in ns["FR_ALL"]}


def om_bias(st, ed, m):
    cut = (date.fromisoformat(ed) - timedelta(days=4)).isoformat()
    v = [TRUTH[(s, d)][1] - x for (s, d, mm), x in HIST.items()
         if s == st and mm == m and d <= cut and (s, d) in TRUTH and TRUTH[(s, d)][1] is not None]
    return statistics.mean(v) if len(v) >= 3 else None


stat = collections.defaultdict(collections.Counter)
for ev in events:
    e = prod_f(ev["ts"])["station_days"].get(f"{ev['st']}|{ev['ed']}")
    w = FR[(ev["st"], ev["ed"])]["first"].get("w")
    who = "main" if w == "0x8fbd7cf5" else "others"
    for sysid, v in e["systems"].items():
        name = sysid.split("_NATIVE")[0].split("_POINT")[0]
        for label, x in (("raw", v["tmax_c"]), ("+c", v["tmax_c"] + e["fleet_c"]), ("+bias", v["tmax_c"] + v.get("bias_c", 0.0))):
            k = f"native {name:24s} {label}"
            stat[k]["n"] += 1
            stat[k]["hit"] += bucket(ev["edges"], x) == ev["choice"]
            stat[k]["hit_" + who] += bucket(ev["edges"], x) == ev["choice"]
            stat[k][who] += 1
    for m in LAG:
        x = fc(ev["st"], ev["ed"], m, ev["ts"])
        if x is None:
            continue
        b = om_bias(ev["st"], ev["ed"], m)
        for label, val in (("+0.8", x + 0.8), ("+bias", None if b is None else x + b)):
            if val is None:
                continue
            k = f"open-meteo {m:28s} {label}"
            stat[k]["n"] += 1
            stat[k]["hit"] += bucket(ev["edges"], val) == ev["choice"]
            stat[k]["hit_" + who] += bucket(ev["edges"], val) == ev["choice"]
            stat[k][who] += 1
    k = "production F"
    stat[k]["n"] += 1
    stat[k]["hit"] += ev["fb"] == ev["choice"]
    stat[k]["hit_" + who] += ev["fb"] == ev["choice"]
    stat[k][who] += 1
rows = sorted(((v["hit"] / max(1, v["n"]), k, v) for k, v in stat.items()), reverse=True)
for acc, k, v in rows:
    m_n, o_n = v["main"], v["others"]
    print(f"{k:58s} n={v['n']:3d}  = choice {acc:.1%}   main {v['hit_main'] / max(1, m_n):.1%} (n {m_n})"
          f"  others {v['hit_others'] / max(1, o_n):.1%} (n {o_n})")
