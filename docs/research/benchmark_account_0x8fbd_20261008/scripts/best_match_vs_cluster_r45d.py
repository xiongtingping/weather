"""Round 45d: is the cluster reading Open-Meteo's default forecast?

``best_match_r45d.json`` (``fetch_best_match_r45d.py``): Open-Meteo best_match
local-day maxima at the airport station and at the geocoded city centre, from
the previous-runs API: day3 = the forecast made ~72 h before each hour
(strictly before the listing), day2 = ~48 h (around the listing; for western
stations the afternoon hours come from a run up to ~half a day after it),
day1 = ~24 h (after the listing; a ceiling, not a candidate).  Variants: raw;
+ a fleet shift (mean truth - raw over settled days <= D-4, all stations);
+ that shift + the station's own mean error (>= 3 days <= D-4, share 0.5 / 1).
Scored on the 227 pre-formation first YES (all, early < 15 min, halves) for
agreement with the cluster and truth hits, next to F; the money centre's
distance to each (sd, MAE) -- a source the cluster read would sit close.
"""
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
rows, bucket = ns["rows"], ns["bucket"]
ALL_TRUTHS = ns["ns"]["ns"]["ALL_TRUTHS"]
BM = json.load(open(B / "best_match_r45d.json"))
TRUTH = {(st, date.fromisoformat(d)): v for (st, d), v in ALL_TRUTHS.items()}


def raw(st, point, lead, d):
    return ((BM.get(st) or {}).get(point) or {}).get(lead, {}).get(d.isoformat())


def shift(point, lead, D):
    errs = [TRUTH[(st, d)] - raw(st, point, lead, d) for st in BM for d in (D - timedelta(days=k) for k in range(4, 30))
            if (st, d) in TRUTH and raw(st, point, lead, d) is not None]
    return statistics.mean(errs) if len(errs) >= 100 else 0.0


def station_bias(st, point, lead, D, c):
    errs = [TRUTH[(st, d)] - raw(st, point, lead, d) - c for d in (D - timedelta(days=k) for k in range(4, 30))
            if (st, d) in TRUTH and raw(st, point, lead, d) is not None]
    return statistics.mean(errs) if len(errs) >= 3 else 0.0


_C = {}


def value(st, D, point, lead, variant):
    x = raw(st, point, lead, D)
    if x is None:
        return None
    if variant == "raw":
        return x
    key = (point, lead, D)
    if key not in _C:
        _C[key] = shift(point, lead, D)
    c = _C[key]
    if variant == "+c":
        return x + c
    share = 0.5 if variant == "+c+0.5bias" else 1.0
    return x + c + share * station_bias(st, point, lead, D, c)


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]
print(f"{len(rows)} picks, event days {days[0]}..{days[-1]}")
base_same = {id(r): r["ev"]["fb"] == r["ev"]["choice"] for r in rows}
c1h = {id(r): r["win"]["1 h"]["centre"] for r in rows if "1 h" in r["win"]}
d_f = np.array([c1h[id(r)] - r["f"] for r in rows if id(r) in c1h])
print(f"F: = cluster {statistics.mean(base_same.values()):.1%}; money centre - F sd {d_f.std():.2f}, MAE {np.abs(d_f).mean():.2f} C")
for point in ("station", "city"):
    for lead in ("day3", "day2", "day1"):
        for variant in ("raw", "+c", "+c+0.5bias", "+c+bias"):
            sub, same, base, early, early_b, hits, fhits, gaps, h1, h2 = [], [], [], [], [], [], [], [], [], []
            for r in rows:
                ev = r["ev"]
                v = value(ev["st"], date.fromisoformat(ev["ed"]), point, lead, variant)
                if v is None:
                    continue
                b = bucket(ev["edges"], v)
                s = b == ev["choice"]
                sub.append(r)
                same.append(s)
                base.append(base_same[id(r)])
                (h1 if ev["ed"] < mid else h2).append(s)
                if ev["ts"] - ev["listed"] < 900:
                    early.append(s)
                    early_b.append(base_same[id(r)])
                if ev["truth"] is not None:
                    hits.append(b == ev["truth"])
                    fhits.append(ev["fb"] == ev["truth"])
                if id(r) in c1h:
                    gaps.append(c1h[id(r)] - v)
            if not sub:
                continue
            g = np.array(gaps)
            print(f"{point:7s} {lead} {variant:11s} n {len(sub):3d}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)};"
                  f" halves {statistics.mean(h1):.1%}/{statistics.mean(h2):.1%}; early {statistics.mean(early):.1%} vs F {statistics.mean(early_b):.1%} (n {len(early)});"
                  f" hits {statistics.mean(hits):.1%} vs F {statistics.mean(fhits):.1%}; centre gap mean {g.mean():+.2f} sd {g.std():.2f} MAE {np.abs(g).mean():.2f}")
