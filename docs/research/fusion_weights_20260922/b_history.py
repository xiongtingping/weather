"""Per-system walk-forward single-system forecasts: the skill source.

For every station, production's own case loader (``native_ensemble_oos._load_cases``)
supplies each system's member values per (season, hour lead bucket, run cycle,
event day).  Each case is forecast out of sample the way the half-life
counterfactual on the dashboard does it: ``_fit_parameters`` on the same scope's
cases in the rolling window strictly before the day, then ``_corrected_center`` /
``_apply_parameters``.  Stricter than that section in one respect: a training
case enters only if its truth was committed before this case's own issue time
(the as-of gate on the fit, both sides).

Stored per case: forecast centre and 1-C probability (``_metrics`` scores it
later against the truth revision knowable at each decision).
"""
import sqlite3, sys, time, pickle
from datetime import date, timedelta
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import native_ensemble_oos as OOS
from bayes_basket_target.contracts import ContractError

DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
OUT = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw/history.pkl"
AS_OF = "2026-09-22T10:00:00Z"
P0_SYSTEMS = {"ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05", "DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS", "NOAA_AIGEFS_025"}
FROM = "2026-08-10"
MIN_TRAIN = OOS.FORWARD_ACCUMULATION_MIN_TRAIN_DAYS   # production's live forward floor (5)

c = sqlite3.connect(DB, uri=True, timeout=60)
c.execute("PRAGMA query_only=ON")
stations = [r[0] for r in c.execute("select station_id from calibration_station_registry order by station_id")]
out = {"cases": [], "truth": {}, "skipped": {}}
t0 = time.time()
for st in stations:
    # every accepted AWC revision with its commit instant (the as-of reader picks per decision)
    for d, v, rev, com in c.execute(
            "select local_date, official_tmax_c, revision_no, canonical_committed_at_utc "
            "from calibration_station_daily_high_truth where station_id=? and local_date>=? "
            "and source_lane='AWC' and qc_status='ACCEPTED' and settlement_definition='official_daily_tmax_c' "
            "and authority_scope in ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') order by local_date, revision_no",
            (st, "2026-06-01")):
        out["truth"].setdefault((st, d), []).append((str(com), float(v), int(rev)))
    try:
        scopes = OOS._load_cases(c, station_id=st, as_of_utc=AS_OF)
    except (ContractError, sqlite3.Error, ValueError, KeyError) as e:
        out["skipped"][st] = repr(e)[:200]
        continue
    commit = {d: min(x[0] for x in revs) for (s2, d), revs in out["truth"].items() if s2 == st}
    n = 0
    for (system, season, lead, cycle), cases in scopes.items():
        if system not in P0_SYSTEMS or cycle not in OOS.VALID_NATIVE_RUN_CYCLES or not lead.startswith("h"):
            continue
        cases = sorted(cases, key=lambda x: x["event_date"])
        for case in cases:
            day = case["event_date"][:10]
            if day < FROM:
                continue
            issued = str(case["aggregator_snapshot_at_utc"])
            floor = (date.fromisoformat(day) - timedelta(days=OOS.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
            hist = [p for p in cases
                    if floor <= p["event_date"][:10] < day
                    and p["event_date"][:10] in commit
                    and commit[p["event_date"][:10]].replace("Z", "") <= issued.replace("Z", "")]
            if len({p["event_date"][:10] for p in hist}) < MIN_TRAIN:
                continue
            try:
                params = OOS._fit_parameters(hist)
                vals = [float(v) for v in case["values"]]
                mean = sum(vals) / len(vals)
                centre = OOS._corrected_center(mean, params)
                prob = OOS._apply_parameters(vals, params)
            except (ContractError, KeyError, TypeError, ValueError):
                continue
            out["cases"].append(dict(station=st, system=system, lead=lead, cycle=cycle, day=day, issued=issued,
                                     centre=centre, raw_mean=mean, prob=prob, n_train=len(hist)))
            n += 1
    print(f"{st}: {n} cases  t={time.time()-t0:.0f}s", flush=True)
pickle.dump(out, open(OUT, "wb"))
print("total cases", len(out["cases"]), "skipped", out["skipped"], "sec", round(time.time() - t0))
