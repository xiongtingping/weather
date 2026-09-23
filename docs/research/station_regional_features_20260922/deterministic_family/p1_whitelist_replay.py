"""Charter 201 step 1: P1's served features are unchanged by the frozen pair list.

Read-only replay.  For every COMMITTED P1 envelope in the window, re-read the
regional runs knowable at its decision (the production reader, runs_for) and
compute the two covariates three ways:

  served   the receipt the envelope carries (features.regional_headroom_c,
           regional_system_id, running_high_c)
  head     regional_features_at as committed at HEAD (no pair list)
  new      regional_features_at in the working tree (charter 201 pair list)

new must equal head exactly (same dict) on the same inputs, and both must
reproduce the served headroom (receipt rounds to 4 decimals) and system.
Also: every (system, station) pair stored in the lane today is on the list,
so the list can remove nothing that exists yet.
"""
import importlib.util, json, sqlite3, sys
from collections import Counter
from datetime import datetime, timedelta, timezone
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import regional_features as NEW
from bayes_basket_target.regional_point_lane import runs_for

HEAD_PATH = sys.argv[1]
SINCE = sys.argv[2] if len(sys.argv) > 2 else (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
OUT = sys.argv[3] if len(sys.argv) > 3 else None
# Loaded as a sibling module of the package so its relative imports resolve.
spec = importlib.util.spec_from_file_location("bayes_basket_target.regional_features_head", HEAD_PATH)
HEAD = importlib.util.module_from_spec(spec); sys.modules[spec.name] = HEAD; spec.loader.exec_module(HEAD)

c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
c.execute("PRAGMA query_only=ON")
pairs = set(c.execute("select distinct ensemble_system_id, station_id from calibration_regional_point_run"))
off_list = sorted(p for p in pairs if p not in NEW.P1_FEATURE_PAIRS)
stations = [r[0] for r in c.execute("select station_id from calibration_station_registry order by 1")]
stats = Counter(); mism = []
cache = {}
for st in stations:
    for eid, dec, day, receipt in c.execute(
            "select envelope_id, decision_at_utc, event_date, json_extract(lineage_json,'$.p1_observation_likelihood_json') "
            "from basket_probability_envelope indexed by basket_probability_envelope_station_stage_recency_idx "
            "where station_id=? and stage='P1' and state='COMMITTED' and decision_at_utc>=?", (st, SINCE)).fetchall():
        stats["envelopes"] += 1
        try:
            r = json.loads(receipt or "{}")
        except ValueError:
            r = {}
        feats = r.get("features") or {}
        if "running_high_c" not in r:
            stats["no_receipt"] += 1
            continue
        key = (st, day)
        if key not in cache:
            cache.clear()
            cache[key] = runs_for(c, station_id=st, event_date=day, as_of_utc="9999")
        rows = [x for x in cache[key] if x["knowable_at_utc"] <= dec]
        kw = dict(as_of_utc=dec, running_high_c=r["running_high_c"], latest_observed_c=20.0, station_id=st)
        a = HEAD.regional_features_at(rows, **kw)
        b = NEW.regional_features_at(rows, **kw)
        if a != b:
            stats["new_vs_head_differs"] += 1; mism.append((eid, "head", a, b)); continue
        stats["new_equals_head"] += 1
        served_h = feats.get("regional_headroom_c"); served_sys = r.get("regional_system_id")
        got_h = b["regional_headroom_c"]; got_sys = b["regional_system_id"]
        same_sys = (None if served_sys in (None, "", "NONE") else served_sys) == (got_sys or None)
        same_h = (served_h is None and got_h is None) or (served_h is not None and got_h is not None and abs(round(got_h, 4) - served_h) < 1e-9)
        if same_sys and same_h:
            stats["reproduces_served"] += 1
        else:
            stats["differs_from_served"] += 1
            if len(mism) < 20:
                mism.append((eid, dec, "served", served_sys, served_h, got_sys, got_h))
out = dict(since=SINCE, pairs_stored=len(pairs), pairs_off_list=off_list, stats=dict(stats), mismatches=mism[:20])
print(json.dumps(out, indent=1, default=str))
if OUT:
    json.dump(out, open(OUT, "w"), indent=1, default=str)
