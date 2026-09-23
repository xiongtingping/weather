"""Read-only: every forward TAF on the P0 root current at its arrival, with new-semantics heating-window fields."""
import glob, json, sqlite3, sys, importlib.util, pickle, math, time
from datetime import datetime, date, timezone, time as wall_time
from zoneinfo import ZoneInfo
ROOT = "/Users/xiong/weather"; sys.path.insert(0, ROOT)
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
fitter = _load("_fit", f"{ROOT}/scripts/fit_taf_likelihood.py")
cap = _load("_cap", f"{ROOT}/scripts/capture_bayes_basket_target_awc_taf_calibration.py")
from bayes_basket_target.cross_consensus import distribution_sigma_c
from bayes_basket_target.materialization import _bucket_centers_c
from bayes_basket_target.native_ensemble_training_facts import taf_restatement_is_current
from bayes_basket_target.taf_rotation_state import TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL as H0
t0 = time.time()
arch = {}
for f in sorted(glob.glob(f"{ROOT}/data/runtime/taf_raw_archive/*.jsonl")):
    for line in open(f):
        try: d = json.loads(line)
        except Exception: continue
        row = d.get("awc_row") or {}; sha = d.get("raw_taf_sha256")
        if sha not in arch or (not arch[sha].get("fcsts") and row.get("fcsts")): arch[sha] = row
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
truth = fitter.load_truth_revisions(conn)
fetched = conn.execute(
    "SELECT station_id,event_date,settlement_timezone,issued_at_utc,provider_available_at_utc,received_at_utc,"
    " tx_c,source_object_sha256,revision_id,cloud_cover_fraction,precipitation_probability,wind_speed_mps"
    " FROM calibration_forward_taf_capture WHERE event_date>='2026-08-20'").fetchall()
ids = {}
for r in fetched: ids.setdefault((r[0], r[1]), []).append(str(r[8]))
raw = []; seen = set(); stats = {"rows": len(fetched), "not_current": 0, "no_truth": 0, "no_archive": 0, "no_features": 0}
for st, day, tz, iss, avail, recv, tx, sha, rid, c_old, p_old, w_old in fetched:
    if not taf_restatement_is_current(rid, ids[(st, day)]): stats["not_current"] += 1; continue
    if (st, day) not in truth: stats["no_truth"] += 1; continue
    key = (st, day, sha)
    if key in seen: continue
    seen.add(key)
    awc = arch.get(sha)
    if not awc or not awc.get("fcsts"): stats["no_archive"] += 1; continue
    try: cloud, precip, wind = cap._forecast_features(awc, event_date=day, timezone_name=tz)
    except Exception: stats["no_features"] += 1; continue
    when = str(avail or recv or iss)
    raw.append((st, day, tz, fitter._epoch(when), when, None if tx is None else float(tx), cloud, precip, wind))
roots = {}; geometry = {}; cases = []
for st, day, tz, moment, when, tx, cloud, precip, wind in sorted(raw):
    scope = (st, day)
    if scope not in roots:
        roots[scope] = sorted((fitter._epoch(a), str(c)) for a, c in conn.execute(
            "SELECT decision_at_utc, checkpoint_id FROM basket_probability_envelope"
            " WHERE station_id=? AND event_date=? AND stage='P0' AND state='COMMITTED'", scope).fetchall())
    current = [c for a, c in roots[scope] if a <= moment]
    if not current: stats.setdefault("no_root", 0); stats["no_root"] += 1; continue
    cid = current[-1]
    if cid not in geometry:
        row = conn.execute("SELECT json_extract(lineage_json,'$.model_center_c'), probabilities_json, bucket_ordinals_json,"
                           " json_extract(lineage_json,'$.bucket_intervals_c_json') FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1", (cid,)).fetchone()
        geometry[cid] = None
        if row and row[0] not in (None, "", "NONE") and row[3]:
            probs = {str(k): float(v) for k, v in json.loads(row[1]).items()}
            ords = {str(k): int(v) for k, v in json.loads(row[2]).items()}
            lin = {"bucket_intervals_c_json": row[3]}
            axis = _bucket_centers_c(lin, bucket_ordinals=ords)
            labels = fitter._revision_labels(truth[scope], lineage=lin, ordinals=ords)
            if axis and labels:
                order, centers = axis
                mass = sum(probs.get(b, 0.0) for b in order)
                if mass > 0:
                    sigma = distribution_sigma_c([probs.get(b, 0.0) for b in order], centers)
                    geometry[cid] = {"c0": float(row[0]), "order": list(order), "centers_list": list(centers),
                                     "probabilities": probs, "centers": dict(zip(order, centers)), "sigma": sigma,
                                     "root_mean_c": sum(probs.get(b, 0) * c for b, c in zip(order, centers)) / mass,
                                     "truth_c": labels[-1][2], "truth_bucket": labels[-1][3], "truth_revisions": labels}
    g = geometry[cid]
    if g is None: stats.setdefault("no_geometry", 0); stats["no_geometry"] += 1; continue
    d = date.fromisoformat(day)
    wstart = datetime.combine(d, wall_time(H0, 0), tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).timestamp()
    cases.append({"station_id": st, "event_date": day, "decided_at": moment, "decided_at_utc": when, "tx_c": tx,
                  "cloud": cloud, "precip": precip, "wind": wind, "lead_h": (wstart - moment) / 3600.0, "root_id": cid, **g})
conn.close()
per = {}
for c in cases: per[(c["station_id"], c["event_date"])] = per.get((c["station_id"], c["event_date"]), 0) + 1
for c in cases: c["weight"] = 1.0 / per[(c["station_id"], c["event_date"])]
stats.update(cases=len(cases), station_days=len(per), stations=len({c["station_id"] for c in cases}),
             with_tx=sum(1 for c in cases if c["tx_c"] is not None), secs=round(time.time() - t0, 1))
print(stats)
pickle.dump(cases, open(sys.argv[1], "wb"))
