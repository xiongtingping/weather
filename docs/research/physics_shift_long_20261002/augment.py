#!/usr/bin/env python3
"""Copy pass1_all with each decision's shift signal filled the way serving reads it:
production ``_holdout_conditioning_signals`` with the decision instant as the cutoff
(the replay never passed it, so every row had cooling=None)."""
import gzip, json, os, sqlite3, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.cross_consensus import radiation_shift_sigmas_for_lead
SRC = os.path.join(ROOT, "docs/research/station_model_weights_20261001/pass1_all")
c = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
filled = total = 0
for f in sorted(os.listdir(SRC)):
    st = f[:4]; out = []
    for line in gzip.open(os.path.join(SRC, f), "rt"):
        r = json.loads(line); total += 1
        if r["event_date"] >= "2026-08-01":
            preds = {s: {r["event_date"]: {"calibrated_center_c": v["c"], "decision_at_utc": r["at"]}} for s, v in r["sys"].items()}
            sig = oos._holdout_conditioning_signals(c, station_id=st, holdout_days=[r["event_date"]], predictions_by_system=preds,
                                                    radiation_shift_sigmas=radiation_shift_sigmas_for_lead(r["lead"]))
            r["cooling"] = (sig.get(r["event_date"]) or {}).get("synoptic_cooling_shift_sigma")
            filled += r["cooling"] is not None
        out.append(r)
    with gzip.open(os.path.join(HERE, "pass1_phys", f), "wt") as z:
        for r in out: z.write(json.dumps(r) + "\n")
c.close(); print("rows", total, "with shift signal", filled)
