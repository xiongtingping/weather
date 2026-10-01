#!/usr/bin/env python3
"""Post-verdict diagnostics (not gates): per-day paired delta of the geometric pool vs the refit linear pool, and the pre-pool floor sensitivity."""
import sys, json, runpy
from pathlib import Path
from collections import defaultdict
import numpy as np
HERE = Path(__file__).resolve().parent
out = {}
import os, io, contextlib
for eps in (1e-2, 1e-3, 1e-4):
    os.environ["POOL_EPS"] = str(eps)
    with contextlib.redirect_stdout(io.StringIO()):
        g = runpy.run_path(str(HERE / "run.py"))
    packets, fit, days, scored = g["packets"], g["fit"], g["days"], g["scored"]
    byday = defaultdict(lambda: defaultdict(list))
    for p in scored:
        byday[p["day"]][p["sd"]].append(p["scores"][("C", fit[("C", p["day"])])][0] - p["scores"][("A", fit[("A", p["day"])])][0])
    perday = {d: float(np.mean([np.mean(v) for v in sds.values()])) for d, sds in sorted(byday.items())}
    allsd = [np.mean(v) for sds in byday.values() for v in sds.values()]
    out[str(eps)] = {"per_day_dLog_C_minus_A1": perday, "mean": float(np.mean(allsd)), "days_negative": sum(1 for v in perday.values() if v < 0), "lambda_C": [fit[("C", d)] for d in days[3:]]}
    print(eps, {d: round(v, 4) for d, v in perday.items()}, "mean", round(float(np.mean(allsd)), 4), "lamC", out[str(eps)]["lambda_C"], flush=True)
(HERE / "diag.json").write_text(json.dumps(out, indent=1))
