"""Research arm: exclude runs issued >=12h into their target local day from Tmax training.

For every h00_12 scope whose case set changes, compare on the runs live seats
(the corrected set's last 8 closed days): certificate fitted on the current case
set vs on the corrected one, both strictly before the holdout, same production
fit rule (charter 216).  Integer-degree buckets.
"""
import json, math, sqlite3, sys, time
from datetime import date, timedelta
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.native_archive_lead import lead_hours_to_local_day
SP = "/private/tmp/claude-501/-Users-xiong-weather/c3f1916f-ae2f-483e-a670-3ec3fa769254/scratchpad/"
AS_OF = "2026-09-25T01:30:00Z"; HOLD = 8
NATIVE = {"ECMWF_IFS_ENS_OPEN_PF50", "ECMWF_AIFS_ENS_GLOBAL", "DWD_ICON_GLOBAL_EPS", "NOAA_GEFS_05", "ECCC_GEM_GLOBAL_ENS", "NOAA_AIGEFS_025"}
orig = oos.lead_hour_bucket_for_run
def patched(*, day_start_utc, run_init):
    if lead_hours_to_local_day(day_start_utc=day_start_utc, run_init=run_init) <= -12.0:
        return "POST_NOON_EXCLUDED"
    return orig(day_start_utc=day_start_utc, run_init=run_init)
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
oos.SINGLE_SYSTEM_FIT_RULE = oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS
def score(params, case):
    v = [float(x) for x in case["values"]]
    dist = oos._apply_parameters(v, params)
    k = oos._temperature_bucket(float(case["actual_tmax_c"]))
    pk = dist.get(k, 0.0)
    brier = sum((p - (1.0 if b == k else 0.0)) ** 2 for b, p in dist.items()) + (0.0 if k in dist else 1.0)
    return brier, -math.log(max(pk, 1e-6)), pk
def window(cases, before):
    start = (date.fromisoformat(before) - timedelta(days=oos.ROLLING_TRAIN_WINDOW_DAYS)).isoformat()
    return [c for c in cases if start <= str(c["event_date"])[:10] < before]
rows = []; scopes = []
for st in sys.argv[1].split(","):
    t = time.time()
    oos.lead_hour_bucket_for_run = orig
    cur = oos._load_cases(conn, station_id=st, as_of_utc=AS_OF)
    oos.lead_hour_bucket_for_run = patched
    fix = oos._load_cases(conn, station_id=st, as_of_utc=AS_OF)
    oos.lead_hour_bucket_for_run = orig
    for key, fcases in fix.items():
        if key[0] not in NATIVE or key[2] != "h00_12":
            continue
        ccases = cur.get(key) or []
        fsig = [(str(c["event_date"])[:10], tuple(c["member_native_run_init_utcs"][:1])) for c in fcases]
        csig = [(str(c["event_date"])[:10], tuple(c["member_native_run_init_utcs"][:1])) for c in ccases]
        if fsig == csig:
            continue
        changed = sum(1 for a in set(csig) - set(fsig))
        days = sorted({d for d, _ in fsig})
        if len(days) < HOLD + 10:
            scopes.append(dict(station=st, key=key, changed=changed, note="too_few_days", n_fix=len(fcases), n_cur=len(ccases)))
            continue
        hold_days = days[-HOLD:]; hs = hold_days[0]
        ftrain = window(fcases, hs); ctrain = window(ccases, hs)
        if len(ftrain) < 10 or len(ctrain) < 10:
            scopes.append(dict(station=st, key=key, changed=changed, note="thin_train", n_fix=len(ftrain), n_cur=len(ctrain)))
            continue
        pf = oos._fit_parameters(ftrain); pc = oos._fit_parameters(ctrain)
        for c in fcases:
            d = str(c["event_date"])[:10]
            if d not in hold_days:
                continue
            bc, lc, qc = score(pc, c); bf, lf, qf = score(pf, c)
            rows.append(dict(station=st, system=key[0], season=key[1], cycle=key[3], day=d, brier_cur=bc, brier_fix=bf, ll_cur=lc, ll_fix=lf, p_cur=qc, p_fix=qf))
        scopes.append(dict(station=st, key=key, changed=changed, n_fix=len(ftrain), n_cur=len(ctrain),
                           bias_cur=pc["bias_c"], bias_fix=pf["bias_c"], spread_cur=pc["spread_factor"], spread_fix=pf["spread_factor"]))
    print(st, round(time.time() - t, 1), "s", flush=True)
tag = sys.argv[2]
json.dump({"rows": rows, "scopes": scopes}, open(SP + f"postnoon_eval_{tag}.json", "w"), default=str)
print("rows", len(rows), "scopes", len(scopes))
