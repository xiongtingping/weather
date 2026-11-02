"""Index every post-pool (charter 134) P0 envelope of a settled station-day.

Read-only (mode=ro, query_only); one indexed query per station-day
(basket_probability_envelope_asof_idx).  Keeps the lineage fields the weight
arms and the jitter analysis need; the probability vector is the served one.
"""
import sqlite3, json, os, pickle, time, sys
DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
ARCH = "/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/forward_packet_archive"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw/index.pkl"
CUT = "2026-09-14T05:52:13Z"   # charter 134 declared instant
DAYS = ["2026-09-%02d" % d for d in range(14, 22)]
c = sqlite3.connect(DB, uri=True, timeout=60)
c.execute("PRAGMA query_only=ON")
st = [r[0] for r in c.execute("select station_id from calibration_station_registry order by station_id")]
t = time.time()
recs, truth = [], {}
for s in st:
    for d in DAYS:
        r = c.execute(
            "select official_tmax_c, revision_no, canonical_committed_at_utc from calibration_station_daily_high_truth "
            "where station_id=? and local_date=? and settlement_definition='official_daily_tmax_c' and qc_status='ACCEPTED' "
            "and source_lane='AWC' order by revision_no desc limit 1", (s, d)).fetchone()
        if r is None:
            continue
        truth[(s, d)] = dict(tmax=float(r[0]), rev=int(r[1]), committed=str(r[2]))
        rows = c.execute(
            "select envelope_id,decision_at_utc,created_at,probabilities_json,lineage_json,settlement_timezone "
            "from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED' "
            "and decision_at_utc>=? and stage='P0' order by decision_at_utc", (s, d, CUT)).fetchall()
        for eid, dec, cr, pj, lj, tz in rows:
            L = json.loads(lj)
            g = lambda k, dflt="{}": json.loads(L.get(k) or dflt) if isinstance(L.get(k), (str, type(None))) else L.get(k)
            snap = L.get("native_forward_snapshot_sha256") or ""
            path = os.path.join(ARCH, d, f"{s}-{snap[:16]}.json.gz")
            recs.append(dict(
                station=s, day=d, envelope_id=eid, decision=dec, created=cr, tz=tz,
                prob=json.loads(pj),
                weights=g("system_weights_json"),
                centers=g("native_calibrated_system_centers_c_json"),
                families=g("system_family_ids_json"),
                caps=g("family_weight_caps_json"),
                bridge=g("consensus_bridge_json", "null"),
                lead=L.get("training_lead_bucket"), season=L.get("season_bin"),
                hour_lead=g("hour_lead_bucket_by_system_json"),
                selection=L.get("native_selection_evidence_sha256"),
                cross_artifact=L.get("native_cross_ensemble_artifact_sha256"),
                run_cycle_by_system=g("run_cycle_by_system_json"),
                run_init=g("run_init_utc_by_system_json"),
                intervals=g("bucket_intervals_c_json"),
                provisional=g("provisional_uncalibrated_system_ids_json", "[]"),
                model_center_c=L.get("model_center_c"),
                packet_path=path, packet_exists=os.path.exists(path),
            ))
print("stations", len(st), "truth station-days", len(truth), "envelopes", len(recs), "sec", round(time.time() - t, 1))
pickle.dump({"recs": recs, "truth": truth}, open(OUT, "wb"))
