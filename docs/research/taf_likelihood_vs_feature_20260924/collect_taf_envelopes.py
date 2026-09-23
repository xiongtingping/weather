"""TAF as likelihood vs feature, step 1 (read-only): every COMMITTED P0 envelope
of a settled station-day at the 17 TX stations (08-01..09-21), with served
vector, bucket axis, decision time, hours to settlement, bias-refusal flag;
plus every TAF capture with a TX for those stations (forward + historical,
deduped on issue time) with its availability time, TX, cloud and precip.
No market join (not needed for this question).  Index seeks only; SSOT ro.
"""
import json, pickle, sqlite3, sys, time
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.settlement_time import hours_to_settlement
DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
SP = "/private/tmp/claude-501/-Users-xiong-weather/e0d3cbfe-8e26-47ae-b7d4-34df9179ede1/scratchpad/taf/"
STATIONS = ["FACT","KBKF","LEMD","MMMX","RKPK","RKSI","RPLL","SAEZ","SBGR","ZBAA","ZGGG","ZGSZ","ZHHH","ZSPD","ZSQD","ZUCK","ZUUU"]
T0, D0, D1 = "2026-06-28", "2026-08-01", "2026-09-21"
c = sqlite3.connect(DB, uri=True, timeout=60); c.execute("PRAGMA query_only=ON")
Q = """select envelope_id, decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json, posterior_version,
 json_extract(lineage_json,'$.provisional_parameter_sources_json'), json_extract(lineage_json,'$.bucket_intervals_c_json'),
 json_extract(lineage_json,'$.model_center_c')
 from basket_probability_envelope indexed by basket_probability_envelope_asof_idx
 where station_id=? and event_date=? and state='COMMITTED' and stage='P0' order by decision_at_utc"""
truth, envs, tafs, tz = {}, [], [], {}
t0 = time.time()
for st in STATIONS:
    lab = {}
    for d, rev, tmax in c.execute("select event_date, truth_revision_no, settled_tmax_c from basket_station_day_settlement_label where station_id=? and event_date>=? and event_date<=?", (st, T0, D1)):
        if d not in lab or rev > lab[d][0]: lab[d] = (rev, float(tmax), "label")
    for d, rev, tmax, qc, sdef in c.execute("select local_date, revision_no, official_tmax_c, qc_status, settlement_definition from calibration_station_daily_high_truth where station_id=? and local_date>=? and local_date<=?", (st, T0, D1)):
        if qc != "ACCEPTED" or sdef != "official_daily_tmax_c" or (d in lab and lab[d][2] == "label"): continue
        if d not in lab or rev > lab[d][0]: lab[d] = (rev, float(tmax), "truth")
    truth[st] = {d: v[1] for d, v in lab.items()}
    n0 = len(envs)
    for day in sorted(truth[st]):
        if not (D0 <= day <= D1): continue
        for eid, dec, tzn, pj, oj, pv, provj, ivj, mcc in c.execute(Q, (st, day)).fetchall():
            tz[st] = tzn
            prov = json.loads(provj) if provj else {}
            refused = any(any("bias_refused" in n for n in (v.get("notes") or [])) for v in prov.values()) if isinstance(prov, dict) else False
            served = {k: float(v) for k, v in json.loads(pj).items()}
            ordn = json.loads(oj); order = sorted(ordn, key=lambda k: ordn[k])
            iv = json.loads(ivj) if ivj else None
            h = hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tzn)
            envs.append(dict(st=st, day=day, eid=eid, dec=dec, tz=tzn, h=h, pv=pv, refused=refused, served=served, order=order,
                             intervals=iv, mcc=None if mcc is None else float(mcc), mv=None, tmax=truth[st][day]))
    seen = set(); n1 = len(tafs)
    for day, kind, iss, rec, avail, tx, cloud, pp, src in c.execute(
            "select event_date, revision_kind, issued_at_utc, received_at_utc, provider_available_at_utc, tx_c, cloud_cover_fraction, precipitation_probability, source_class "
            "from calibration_forward_taf_capture where station_id=? and event_date>=? and event_date<=? and tx_c is not null", (st, T0, "2026-09-22")):
        key = (day, iss); 
        if key in seen: continue
        seen.add(key)
        tafs.append(dict(st=st, day=day, kind=kind, issued=iss, avail=avail or rec, tx=float(tx), cloud=cloud, precip=pp, src="forward"))
    for day, iss, avail, tx, cloud, pp in c.execute(
            "select event_date, issued_at_utc, provider_available_at_utc, tx_c, cloud_cover_fraction, precipitation_probability "
            "from calibration_historical_taf_capture where station_id=? and event_date>=? and event_date<=? and tx_c is not null", (st, T0, "2026-09-22")):
        key = (day, iss)
        if key in seen or not avail: continue
        seen.add(key)
        tafs.append(dict(st=st, day=day, kind="HIST", issued=iss, avail=avail, tx=float(tx), cloud=cloud, precip=pp, src="historical"))
    print(st, "truth", len(truth[st]), "envs", len(envs) - n0, "tafs", len(tafs) - n1, round(time.time() - t0, 1), flush=True)
pickle.dump(dict(envs=envs, truth=truth, tafs=tafs, tz=tz, D0=D0, D1=D1), open(SP + "envelopes.pkl", "wb"))
print("done envs", len(envs), "tafs", len(tafs))
