"""Station regional features, step 2 (read-only): every COMMITTED P0 envelope
of a settled station-day at the six gap stations, with its served vector,
bucket axis, served centre, decision time, hours to settlement, the
bias-refusal flag (08-14..09-06 incident: provisional_parameter_sources_json
notes 'bias_refused_*'), and the market vector at the decision.

Market: newest COMMITTED official WS snapshot at or before the decision,
<=30 min old, reduced with production's valuation_v1.market_reference_v1
(same rule as p0_center_source_20260922/collect.py).
Truth: production label table (max truth revision); fallback ACCEPTED
official truth.  Also dumps ACCEPTED truth from 07-01 for the regional
models' own as-of debias.

Every read is an index seek by (station_id, event_date); SSOT opened
mode=ro + query_only.  Output: pickle in the scratchpad.
"""
import json, pickle, sqlite3, sys, time
from datetime import datetime
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1
from bayes_basket_target.settlement_time import hours_to_settlement

DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/srf/"
STATIONS = ["SBGR", "ZUUU", "KSEA", "FACT", "KSFO", "LTAC"]
T0, D0, D1 = "2026-06-28", "2026-08-01", "2026-09-21"
c = sqlite3.connect(DB, uri=True, timeout=60)
c.execute("PRAGMA query_only=ON")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


Q = """select envelope_id, decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json, posterior_version,
 json_extract(lineage_json,'$.provisional_parameter_sources_json'),
 json_extract(lineage_json,'$.bucket_intervals_c_json'),
 json_extract(lineage_json,'$.model_center_c')
 from basket_probability_envelope indexed by basket_probability_envelope_asof_idx
 where station_id=? and event_date=? and state='COMMITTED' and stage='P0' order by decision_at_utc"""
truth, envs = {}, []
t0 = time.time()
for st in STATIONS:
    lab = {}
    for d, rev, tmax in c.execute("select event_date, truth_revision_no, settled_tmax_c from basket_station_day_settlement_label "
                                  "where station_id=? and event_date>=? and event_date<=?", (st, T0, D1)):
        if d not in lab or rev > lab[d][0]:
            lab[d] = (rev, float(tmax), "label")
    for d, rev, tmax, qc, sdef in c.execute("select local_date, revision_no, official_tmax_c, qc_status, settlement_definition "
                                            "from calibration_station_daily_high_truth where station_id=? and local_date>=? and local_date<=?",
                                            (st, T0, D1)):
        if qc != "ACCEPTED" or sdef != "official_daily_tmax_c" or (d in lab and lab[d][2] == "label"):
            continue
        if d not in lab or rev > lab[d][0]:
            lab[d] = (rev, float(tmax), "truth")
    truth[st] = {d: v[1] for d, v in lab.items()}
    n0 = len(envs)
    for day in sorted(truth[st]):
        if not (D0 <= day <= D1):
            continue
        for eid, dec, tz, pj, oj, pv, provj, ivj, mcc in c.execute(Q, (st, day)).fetchall():
            prov = json.loads(provj) if provj else {}
            refused = any(any("bias_refused" in n for n in (v.get("notes") or [])) for v in prov.values()) if isinstance(prov, dict) else False
            served = {k: float(v) for k, v in json.loads(pj).items()}
            ordn = json.loads(oj)
            order = sorted(ordn, key=lambda k: ordn[k])
            iv = json.loads(ivj) if ivj else None
            h = hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tz)
            snap = c.execute("select observed_at, buckets_json from basket_official_ws_snapshot "
                             "where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? "
                             "order by observed_at desc, sequence desc limit 1", (st, day, dec)).fetchone()
            mv = None
            if snap is not None and (ts(dec) - ts(snap[0])).total_seconds() <= 1800:
                m = market_reference_v1(json.loads(snap[1]))
                if m.get("state") == "complete":
                    mv = {k: float(v) for k, v in m["vector"].items()}
                    if set(mv) != set(served):
                        mv = None
            envs.append(dict(st=st, day=day, eid=eid, dec=dec, tz=tz, h=h, pv=pv, refused=refused, served=served,
                             order=order, intervals=iv, mcc=None if mcc is None else float(mcc), mv=mv, tmax=truth[st][day]))
    print(st, "truth days", len(truth[st]), "envelopes", len(envs) - n0, round(time.time() - t0, 1), flush=True)
pickle.dump(dict(envs=envs, truth=truth, D0=D0, D1=D1), open(SP + "envelopes.pkl", "wb"))
print("done", len(envs))
