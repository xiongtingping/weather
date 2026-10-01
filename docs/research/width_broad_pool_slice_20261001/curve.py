#!/usr/bin/env python3
"""Diagnostic after the pre-registered verdict: how flat is the in-sample
LogScore in the residual lambda for the broad cells?  Same cells, same
production scorer (_mean_nll on the floored vector).  Not a gate."""
from __future__ import annotations
import json, sqlite3, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(ROOT))
from bayes_basket_target import center_width_store as store
OUT = Path(__file__).resolve().parent; t0 = time.monotonic()
conn = sqlite3.connect(f"file:{ROOT/'data/bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
floor = store._evidence_floor(conn, stage="P0", caller_bound="2026-08-27T09:20:00Z")
by_day = store.serving_cells_by_day(conn, store._truth(conn), stage="P0", min_decision_at_utc=floor); conn.close()
cells = [c for d in sorted(by_day) for c in by_day[d]]
def seated(c): v = c.get("seated_system_count"); return -1 if v is None else int(v)
def hours(c): v = c.get("hours_to_settlement"); return -1.0 if v is None else float(v)
groups = {"n6": [c for c in cells if seated(c) == 6], "n4_5": [c for c in cells if seated(c) in (4, 5)],
          "n6_far": [c for c in cells if seated(c) == 6 and hours(c) > 36], "n6_near": [c for c in cells if seated(c) == 6 and 0 <= hours(c) <= 36],
          "thin_far": [c for c in cells if 0 <= seated(c) <= 3 and hours(c) > 36]}
lams = [0.6, 0.675, 0.75, 0.825, 0.9, 1.0, 1.1, 1.2]
out = {}
for name, cs in groups.items():
    row = {str(l): store._mean_nll(cs, delta_c=0.0, width_lambda=l) for l in lams}
    base = row["1.0"]
    out[name] = {"cells": len(cs), "station_days": len({(c['station_id'], c['event_date']) for c in cs}),
                 "mean_logscore_by_residual_lambda": row, "gain_vs_identity": {k: base - v for k, v in row.items()}}
    print(name, len(cs), {k: round(base - v, 4) for k, v in row.items()}, f"t={time.monotonic()-t0:.0f}s", flush=True)
(OUT / "curve.json").write_text(json.dumps(out, indent=1))
