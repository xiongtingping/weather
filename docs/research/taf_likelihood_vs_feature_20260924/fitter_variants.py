"""Production's own fitter (scripts/fit_taf_likelihood.py), read-only, three TEMPERED_GROUPS variants, output to scratch.
Cases loaded once; line and gate identical across variants; only the tempering fit + its walk-forward differ."""
import json, sqlite3, sys, time
sys.path.insert(0, "/Users/xiong/weather"); sys.path.insert(0, "/Users/xiong/weather/scripts")
import fit_taf_likelihood as F
from bayes_basket_target import taf_likelihood as TL
DB = "/Users/xiong/weather/data/bayes_basket_target_ssot.db"; THROUGH = "2026-09-22"
t0 = time.time()
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
try:
    cases = F.load_cases(conn, since="2026-08-16", through=THROUGH)
    tcases = F.load_tempering_cases(conn, since="2026-08-16", through=THROUGH)
finally:
    conn.close()
print("cases", len(cases), "tempering cases", len(tcases), round(time.time() - t0, 1), flush=True)
line = F.fit(cases); served = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **line}
print("line pool", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in line["pool"].items()}, flush=True)
out = {}
for name, groups in (("prod_as_is", (TL.GROUP_UNREASONED_COLD,)),
                     ("plus_other", (TL.GROUP_UNREASONED_COLD, TL.GROUP_OTHER)),
                     ("all_four", tuple(TL.TEMPERING_GROUPS))):
    F.TEMPERED_GROUPS = groups
    lam, w = F.fit_tempering(served, tcases)
    wf = F.walk_forward_tempering(cases, tcases)
    out[name] = {"groups": groups, "lambda_by_group": lam, "weights": {k: round(v, 1) for k, v in w.items()},
                 "walk_forward": {k: v for k, v in wf.items() if k in ("state", "by_day_mean_gain_vs_lambda_one_nat", "ci_low", "ci_high", "days", "gain_by_group_nat", "station_days_by_group")}}
    print(name, json.dumps(out[name], default=str), round(time.time() - t0, 1), flush=True)
json.dump(out, open("/private/tmp/claude-501/-Users-xiong-weather/e0d3cbfe-8e26-47ae-b7d4-34df9179ede1/scratchpad/taf/fitter_variants.json", "w"), indent=1, default=str)
print("done")
