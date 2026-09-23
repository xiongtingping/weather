"""CONUS cell audit, part 2 (read-only): the production P1 lane's own stored
day maxima against settled truth, per station x product -- a wrong cell shows
as a station whose offset sits far outside the fleet's and whose spread is
wide.  Rows: calibration_regional_point_run, whole local day ahead of the run
(lead >= 0, every wanted hour covered, peak window complete); truth: settlement label (max rev)."""
import json, os, sqlite3, collections
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
c.execute("PRAGMA query_only=ON")
ST = ["KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO", "CYYZ"]
out = {}
for st in ST:
    truth = {}
    for d, rev, t in c.execute("select event_date, truth_revision_no, settled_tmax_c from basket_station_day_settlement_label where station_id=?", (st,)):
        if d not in truth or rev > truth[d][0]:
            truth[d] = (rev, float(t))
    for sysid in ("NCEP_HRRR_POINT", "NCEP_NBM_POINT"):
        e = collections.defaultdict(list)
        for d, tm, hw, hc in c.execute("select target_local_date, forecast_tmax_c, hours_wanted, hours_covered from calibration_regional_point_run "
                                       "indexed by calibration_regional_point_run_scope_idx where station_id=? and ensemble_system_id=? "
                                       "and lead_hours_to_local_day_start>=0 and remaining_peak_complete=1", (st, sysid)):
            if hw == hc and d in truth:
                e[d].append(truth[d][1] - float(tm))
        if not e:
            continue
        per_day = np.array([np.mean(v) for v in e.values()])
        out[f"{st}|{sysid}"] = dict(days=len(per_day), rows=sum(len(v) for v in e.values()),
                                    mean_truth_minus_lane=round(float(per_day.mean()), 2), sd=round(float(per_day.std()), 2))
        print(st, sysid, out[f"{st}|{sysid}"])
json.dump(out, open(os.path.join(HERE, "lane_error_audit.json"), "w"), indent=1)
