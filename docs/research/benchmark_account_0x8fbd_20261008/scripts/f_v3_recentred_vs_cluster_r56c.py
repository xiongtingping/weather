"""Round 56c: does re-centring the station-value F (its own recalibration) keep the cluster agreement?
F_v3 lambda 0.9 as scored (warm, 56.4% / LODO 54.6%) against the same shifted by the training
days' mean (truth - F_v3) - (truth - F) (leave one event day out), on the 227 picks."""
import statistics, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("f_station_values_ablation_r55c.py").read_text()
ns = {"__name__": "r55c", "__file__": str(here.with_name("f_station_values_ablation_r55c.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("MODES = {")[0], "r55c_head", "exec"), ns)
rows, bucket, prod_f, ROSTER, ALL_TRUTHS, centre, FR_ALL, edges_of, paired = (ns[k] for k in ("rows", "bucket", "prod_f", "ROSTER", "ALL_TRUTHS", "centre", "FR_ALL", "edges_of", "paired"))
MODE = {"sflux": True, "lapse": True}
# universe A for the shift
A = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5 or r["st"] not in ROSTER:
        continue
    t = int(r["listed"]) + 300
    e = prod_f(t)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    v = centre(r["st"], r["ed"], datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), 0.9, MODE)
    if v is not None:
        A.append({"ed": r["ed"], "d": (float(r["truth_c"]) - v) - (float(r["truth_c"]) - e["f_c"])})
days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]
for label, shift_fn in (("as scored (warm)", lambda ed: 0.0), ("re-centred (LODO shift)", lambda ed: float(np.mean([a["d"] for a in A if a["ed"] != ed]))),
                        ("half re-centred", lambda ed: 0.5 * float(np.mean([a["d"] for a in A if a["ed"] != ed])))):
    got = []
    for r in rows:
        ev = r["ev"]
        v = centre(ev["st"], ev["ed"], datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), 0.9, MODE)
        if v is not None:
            got.append((r, v + shift_fn(ev["ed"])))
    same = [bucket(r["ev"]["edges"], v) == r["ev"]["choice"] for r, v in got]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r, _v in got]
    tr = [(r, v) for r, v in got if r["ev"]["truth"] is not None]
    h1 = statistics.mean(s for s, (r, _v) in zip(same, got) if r["ev"]["ed"] < mid) - statistics.mean(b for b, (r, _v) in zip(base, got) if r["ev"]["ed"] < mid)
    h2 = statistics.mean(s for s, (r, _v) in zip(same, got) if r["ev"]["ed"] >= mid) - statistics.mean(b for b, (r, _v) in zip(base, got) if r["ev"]["ed"] >= mid)
    print(f"{label:26s} n {len(got)}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} halves {h1:+.3f}/{h2:+.3f};"
          f" hits {statistics.mean(bucket(r['ev']['edges'], v) == r['ev']['truth'] for r, v in tr):.1%} vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for r, _v in tr):.1%};"
          f" mean shift {np.mean([shift_fn(r['ev']['ed']) for r, _v in got]):+.2f} C")
