"""Scratch only: add the as-of TAF heating-window fields (new semantics) to a copy of the P1 sample table."""
import glob, json, sqlite3, sys, importlib.util, bisect, collections
ROOT = "/Users/xiong/weather"; sys.path.insert(0, ROOT)
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
fitter = _load("_fit", f"{ROOT}/scripts/fit_taf_likelihood.py")
cap = _load("_cap", f"{ROOT}/scripts/capture_bayes_basket_target_awc_taf_calibration.py")
from bayes_basket_target.native_ensemble_training_facts import taf_restatement_is_current
arch = {}
for f in sorted(glob.glob(f"{ROOT}/data/runtime/taf_raw_archive/*.jsonl")):
    for line in open(f):
        try: d = json.loads(line)
        except Exception: continue
        row = d.get("awc_row") or {}; sha = d.get("raw_taf_sha256")
        if sha not in arch or (not arch[sha].get("fcsts") and row.get("fcsts")): arch[sha] = row
ssot = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
fetched = ssot.execute("SELECT station_id,event_date,settlement_timezone,issued_at_utc,provider_available_at_utc,received_at_utc,source_object_sha256,revision_id"
                       " FROM calibration_forward_taf_capture WHERE event_date>='2026-08-09'").fetchall()
ssot.close()
ids = collections.defaultdict(list)
for r in fetched: ids[(r[0], r[1])].append(str(r[7]))
by_scope = collections.defaultdict(list); miss = collections.Counter()
for st, day, tz, iss, avail, recv, sha, rid in fetched:
    if not taf_restatement_is_current(rid, ids[(st, day)]): miss["not_current"] += 1; continue
    awc = arch.get(sha)
    if not awc or not awc.get("fcsts"): miss["no_archive"] += 1; continue
    try: fields = cap._forecast_features(awc, event_date=day, timezone_name=tz)
    except Exception: miss["no_features"] += 1; continue
    by_scope[(st, day)].append((fitter._epoch(str(avail or recv or iss)), fields))
for k in by_scope: by_scope[k].sort(key=lambda x: x[0])
db = sqlite3.connect(sys.argv[1])
for col in ("taf_cloud_cover_fraction", "taf_precipitation_probability", "taf_wind_speed_mps"):
    db.execute(f"ALTER TABLE metar_phase_hazard_sample ADD COLUMN {col} REAL")
rows = db.execute("SELECT sample_id, station_id, event_date, as_of_utc FROM metar_phase_hazard_sample").fetchall()
hit = 0; updates = []
for sid, st, day, at in rows:
    tl = by_scope.get((st, day))
    if not tl: continue
    t = fitter._epoch(at); i = bisect.bisect_right([x[0] for x in tl], t) - 1
    if i < 0: continue
    c, p, w = tl[i][1]; updates.append((c, p, w, sid)); hit += 1
db.executemany("UPDATE metar_phase_hazard_sample SET taf_cloud_cover_fraction=?, taf_precipitation_probability=?, taf_wind_speed_mps=? WHERE sample_id=?", updates)
db.commit()
print({"samples": len(rows), "with_taf": hit, **miss, "scopes_with_taf": len(by_scope)})
print(db.execute("SELECT min(event_date), count(distinct station_id||event_date) FROM metar_phase_hazard_sample WHERE taf_cloud_cover_fraction IS NOT NULL").fetchone())
