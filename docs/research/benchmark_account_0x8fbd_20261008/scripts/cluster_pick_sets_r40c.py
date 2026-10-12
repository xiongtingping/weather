"""Round 40c: covering the cluster's pre-formation first YES with more than one
bucket -- F plus the neighbour on the side F leans to (position in the bucket),
or on the side more corrected members sit; and F with both neighbours.
"""
import sys
from pathlib import Path

here = Path(__file__)
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events = ns["events"]
n = len(events)
one = sum(ev["choice"] == ev["fb"] for ev in events)
pos = vote = both = 0
up = sum(ev["choice"] == ev["fb"] + 1 for ev in events)
down = sum(ev["choice"] == ev["fb"] - 1 for ev in events)
for ev in events:
    lo, hi = ev["edges"][ev["fb"]]
    side = 1 if ev["f"] >= (lo + hi) / 2 else -1
    pos += ev["choice"] in (ev["fb"], ev["fb"] + side)
    above = sum(v for j, v in ev["cvotes"].items() if j > ev["fb"])
    below = sum(v for j, v in ev["cvotes"].items() if j < ev["fb"])
    vside = 1 if above >= below else -1
    vote += ev["choice"] in (ev["fb"], ev["fb"] + vside)
    both += abs(ev["choice"] - ev["fb"]) <= 1
print(f"{n} station-days: F alone {one / n:.1%}; cluster one bucket up {up / n:.1%}, down {down / n:.1%}")
print(f"   F + neighbour on F's side of its bucket   {pos / n:.1%}")
print(f"   F + neighbour on the side more members sit {vote / n:.1%}")
print(f"   F + both neighbours                        {both / n:.1%}")
