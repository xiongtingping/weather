"""Fixed lambda tables scored by production's own walk-forward machinery: as-of lines, production pairing (TAF arrival x
current P0), weight 1/n per station-day, log score vs lambda = 1, pooled by event day, bootstrap by day.  No selection."""
import json, random, sqlite3, statistics, sys, time
from collections import defaultdict
sys.path.insert(0, "/Users/xiong/weather"); sys.path.insert(0, "/Users/xiong/weather/scripts")
import fit_taf_likelihood as F
from bayes_basket_target import taf_likelihood as TL
DB = "/Users/xiong/weather/data/bayes_basket_target_ssot.db"; THROUGH = "2026-09-22"
TABLES = {"prod_1_05_1_1": {"REASONED": 1.0, "UNREASONED_COLD": 0.5, "OTHER": 1.0, "SMALL": 1.0},
          "low_05_025_05_075": {"REASONED": 0.5, "UNREASONED_COLD": 0.25, "OTHER": 0.5, "SMALL": 0.75},
          "other_muted_1_05_05_1": {"REASONED": 1.0, "UNREASONED_COLD": 0.5, "OTHER": 0.5, "SMALL": 1.0},
          "cold025_other05_1_025_05_1": {"REASONED": 1.0, "UNREASONED_COLD": 0.25, "OTHER": 0.5, "SMALL": 1.0},
          "g050": {g: 0.5 for g in TL.TEMPERING_GROUPS}, "g075": {g: 0.75 for g in TL.TEMPERING_GROUPS}}
t0 = time.time()
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
try:
    cases = F.load_cases(conn, since="2026-08-16", through=THROUGH); tcases = F.load_tempering_cases(conn, since="2026-08-16", through=THROUGH)
finally:
    conn.close()
print("cases", len(cases), "tempering", len(tcases), round(time.time() - t0, 1), flush=True)
line_as_of = F.AsOf(cases); fits = {}
per_day = {name: defaultdict(lambda: [0.0, 0.0]) for name in TABLES}; by_group = {name: defaultdict(lambda: [0.0, 0.0]) for name in TABLES}
pair_day = defaultdict(lambda: [0.0, 0.0])  # low - prod
n_scored = 0
for case in sorted(tcases, key=lambda c: c["decided_at"]):
    at = case["decided_at"]
    if F.as_of_label(case, at) is not None: continue
    key = line_as_of.key(at)
    if key not in fits:
        tr = line_as_of.training(at)
        fits[key] = None if len({c["event_date"] for c in tr}) < F.MIN_TRAIN_DAYS else {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **F.fit(tr)}
    if fits[key] is None: continue
    ex = F._scored(fits[key], [case])[0][1]
    plain = F._tempered_gain(case, ex, 1.0)
    if plain is None: continue
    g = {name: F._tempered_gain(case, ex, tbl[case["group"]]) for name, tbl in TABLES.items()}
    if any(v is None for v in g.values()): continue
    n_scored += 1; w = case["weight"]; d = case["event_date"]
    for name in TABLES:
        per_day[name][d][0] += w * (g[name] - plain); per_day[name][d][1] += w
        by_group[name][case["group"]][0] += w * (g[name] - plain); by_group[name][case["group"]][1] += w
    pair_day[d][0] += w * (g["low_05_025_05_075"] - g["prod_1_05_1_1"]); pair_day[d][1] += w
def summarize(pd):
    means = [v[0] / v[1] for v in pd.values() if v[1] > 0]
    rng = random.Random(20260919)
    boot = sorted(statistics.mean(rng.choice(means) for _ in means) for _ in range(F.BOOTSTRAP_DRAWS))
    return dict(mean=round(statistics.mean(means), 4), ci_low=round(boot[int(0.025 * F.BOOTSTRAP_DRAWS)], 4), ci_high=round(boot[int(0.975 * F.BOOTSTRAP_DRAWS)], 4), days=len(means))
out = {"scored_tafs": n_scored, "as_of_line_fits": sum(1 for v in fits.values() if v)}
for name in TABLES:
    out[name] = {"vs_lambda_one_nat": summarize(per_day[name]), "gain_by_group_nat": {g_: round(v[0] / v[1], 4) for g_, v in by_group[name].items() if v[1] > 0}}
    print(name, json.dumps(out[name]), flush=True)
out["low - prod"] = summarize(pair_day); print("low - prod", json.dumps(out["low - prod"]))
json.dump(out, open("/private/tmp/claude-501/-Users-xiong-weather/e0d3cbfe-8e26-47ae-b7d4-34df9179ede1/scratchpad/taf/fitter_fixed_tables.json", "w"), indent=1)
print("done", round(time.time() - t0, 1))
