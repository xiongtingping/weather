#!/usr/bin/env python3
"""Why bridge v2 failed: is the between-centre spread B a precision signal (centre error falls with B) or a hardness signal (it rises)?  Per-packet, by B quintile."""
import sys, json, runpy, gzip, glob, io, contextlib
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT))
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(str(HERE / "run.py"))
packets = g["packets"]
rows = {(r["station_id"], r["event_date"], r["hours_to_settlement"]): r for f in sorted(glob.glob(str(ROOT / "tmp/production_fusion_trace_20261001_codex/????.jsonl.gz"))) for r in map(json.loads, gzip.open(f, "rt"))}
# pooled centre error from lineage centres and truth
recs = []
for p in packets:
    r = rows[(p["sd"][0], p["sd"][1], p["h"])]; l = r["lineage"]
    w = {k: float(v) for k, v in l["system_weights_json"].items() if float(v) > 1e-12}
    cc = sum(w[s] * float(l["native_calibrated_system_centers_c_json"][s]) for s in w)
    recs.append((p["B"], p["A"], (cc - r["truth"]["official_tmax_c"]) ** 2, p["n"]))
arr = np.array(recs); q = np.quantile(arr[:, 0], [0.2, 0.4, 0.6, 0.8])
out = []
for i, (lo, hi) in enumerate(zip([-1] + list(q), list(q) + [1e9])):
    m = (arr[:, 0] > lo) & (arr[:, 0] <= hi); a = arr[m]
    out.append({"B_quintile": i + 1, "packets": int(m.sum()), "mean_B": float(a[:, 0].mean()), "mean_A": float(a[:, 1].mean()), "A_plus_B": float((a[:, 1] + a[:, 0]).mean()), "A_minus_B": float((a[:, 1] - a[:, 0]).mean()), "centre_MSE": float(a[:, 2].mean())})
    print(out[-1])
print("corr(B, centre err^2)", float(np.corrcoef(arr[:, 0], arr[:, 2])[0, 1]), " corr(A, centre err^2)", float(np.corrcoef(arr[:, 1], arr[:, 2])[0, 1]))
(HERE / "diag_b.json").write_text(json.dumps({"quintiles": out, "corr_B": float(np.corrcoef(arr[:, 0], arr[:, 2])[0, 1]), "corr_A": float(np.corrcoef(arr[:, 1], arr[:, 2])[0, 1])}, indent=1))
