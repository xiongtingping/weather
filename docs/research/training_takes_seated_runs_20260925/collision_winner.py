import json, sqlite3, statistics, sys, time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import native_ensemble_oos as oos
SP = "/private/tmp/claude-501/-Users-xiong-weather/c3f1916f-ae2f-483e-a670-3ec3fa769254/scratchpad/"
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
tz = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"))
stations = sys.argv[1].split(",")
AI = {"ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025", "ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05", "DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"}
out = []
for st in stations:
    t = time.time()
    cases_by = oos._load_cases(conn, station_id=st, as_of_utc="2026-09-25T01:30:00Z")
    # live usage: latest P0 for 09-25 and 09-26
    live = {}
    for ev in ("2026-09-25", "2026-09-26"):
        env = conn.execute("SELECT lineage_json FROM basket_probability_envelope INDEXED BY basket_probability_envelope_station_stage_recency_idx "
                           "WHERE station_id=? AND stage='P0' AND state='COMMITTED' AND event_date=? ORDER BY decision_at_utc DESC LIMIT 1", (st, ev)).fetchone()
        if not env: continue
        lin = json.loads(env[0]); J = lambda k: (json.loads(lin[k]) if isinstance(lin.get(k), str) else lin.get(k)) or {}
        inits = J("run_init_utc_by_system_json"); leads = J("system_calibration_lead_by_system_json"); cyc = J("run_cycle_by_system_json")
        ds = datetime.fromisoformat(ev).replace(tzinfo=ZoneInfo(tz[st])).astimezone(timezone.utc)
        for s, init in inits.items():
            lh = (ds - datetime.fromisoformat(init.replace("Z", "+00:00"))).total_seconds() / 3600
            live[(s, leads.get(s), cyc.get(s), ev)] = round(lh, 1)
    for (s, season, lead, cyc), cases in cases_by.items():
        if s not in AI or lead != "h00_12": continue
        signs = Counter()
        for c in cases[-60:]:
            inits = {x for x in c.get("member_native_run_init_utcs") or [] if x}
            if len(inits) != 1: continue
            ds = datetime.fromisoformat(str(c["event_date"])[:10]).replace(tzinfo=ZoneInfo(tz[st])).astimezone(timezone.utc)
            lh = (ds - datetime.fromisoformat(next(iter(inits)).replace("Z", "+00:00"))).total_seconds() / 3600
            signs["same_day(<0)" if lh < 0 else "prev_day(>=0)"] += 1
        uses = sorted({v for (ls, ll, lc, ev), v in live.items() if ls == s and ll == lead and lc == cyc})
        out.append(dict(station=st, system=s, season=season, cycle=cyc, train=dict(signs), live_leads=uses))
    print(st, tz[st], round(time.time() - t, 1), "s", flush=True)
json.dump(out, open(SP + f"collision_winner_{stations[0]}.json", "w"))
for r in out:
    if r["live_leads"] or (r["train"].get("same_day(<0)") and r["train"].get("prev_day(>=0)") is None):
        mism = any(l >= 0 for l in r["live_leads"]) and r["train"].get("same_day(<0)", 0) > r["train"].get("prev_day(>=0)", 0)
        print(f"  {r['station']} {r['system']:24s} {r['season']:8s} h00_12 {r['cycle']}: train {r['train']} live_leads {r['live_leads']} {'<<< MISMATCH' if mism else ''}")
