"""P0 centre source, step 1 (read-only): for every committed P0 envelope of a
settled station-day, rebuild each seated system's raw member mean from the
exact packet the envelope was compiled from (forward_packet_archive, keyed by
the lineage's native_forward_snapshot_sha256) and its served bias shift with
production's own resolver (native_ensemble_oos._resolved_bias_c) on the
artifact the lineage names.  The rebuilt centre is checked against the
lineage's native_calibrated_system_centers_c_json.

Market: the newest COMMITTED official WS snapshot at or before the decision,
<=30 min old, reduced with production's valuation_v1.market_reference_v1.
Truth: production label table (max truth revision), fallback ACCEPTED
official truth -- the audit's (vf_extract) rule.

Every read is an index seek by (station_id, event_date); SSOT opened
mode=ro + query_only.  Output: pickle (scratchpad), one row per envelope.
"""
import gzip, json, os, pickle, resource, sqlite3, sys, time
from datetime import datetime
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.native_ensemble_oos import _resolved_bias_c
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1
from bayes_basket_target.settlement_time import hours_to_settlement

DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
ARCH = "/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/forward_packet_archive"
POOL = "2026-09-14T05:52:13Z"          # charter 134 stable pool clock
D0 = sys.argv[1] if len(sys.argv) > 1 else "2026-09-07"   # bias-refusal fix 09-06
D1 = sys.argv[2] if len(sys.argv) > 2 else "2026-09-22"
OUT = sys.argv[3]
ONLY = sys.argv[4].split(",") if len(sys.argv) > 4 else None

c = sqlite3.connect(DB, uri=True, timeout=60)
c.execute("PRAGMA query_only=ON")
stations = ONLY or [r[0] for r in c.execute("select station_id from calibration_station_registry order by 1")]


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def tier(h):
    if h < 12: return "0-12"
    if h < 24: return "12-24"
    if h < 36: return "24-36"
    if h < 48: return "36-48"
    return "48+"


art_cache = {}


def artifact(sha):
    if sha not in art_cache:
        r = c.execute(
            "select bias_parameters_json, effective_sample_n, train_start_date, train_end_date, "
            "json_extract(reliability_parameters_json,'$.fit_case_count'), "
            "json_extract(reliability_parameters_json,'$.fit_through_date'), lead_bucket, run_cycle, season_bin, "
            "json_extract(spread_parameters_json,'$.train_residual_sigma_c') "
            "from calibration_native_ensemble_system_artifact where content_sha256=?", (sha,)).fetchone()
        art_cache[sha] = None if r is None else dict(
            bias=json.loads(r[0]), n_eff=r[1], train_start=r[2], train_end=r[3], fit_cases=r[4],
            fit_through=r[5], lead=r[6], cycle=r[7], season=r[8], sigma_tr=r[9])
    return art_cache[sha]


def jl(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


Q = """select envelope_id, decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json,
 json_extract(lineage_json,'$.system_weights_json'),
 json_extract(lineage_json,'$.native_calibrated_system_centers_c_json'),
 json_extract(lineage_json,'$.system_artifact_shas_json'),
 json_extract(lineage_json,'$.provisional_parameter_sources_json'),
 json_extract(lineage_json,'$.native_forward_snapshot_sha256'),
 json_extract(lineage_json,'$.bucket_intervals_c_json'),
 json_extract(lineage_json,'$.hour_lead_bucket_by_system_json'),
 json_extract(lineage_json,'$.run_init_utc_by_system_json'),
 json_extract(lineage_json,'$.model_center_c'),
 json_extract(lineage_json,'$.consensus_bridge_json')
 from basket_probability_envelope indexed by basket_probability_envelope_asof_idx
 where station_id=? and event_date=? and state='COMMITTED' and stage='P0' order by decision_at_utc"""

out = []
stats = dict(env=0, no_packet=0, no_artifact=0, centre_mismatch=0, sys_rows=0, paired=0)
t0 = time.time()
for st in stations:
    labels = {}
    for d, rev, b, tmax in c.execute(
            "select event_date, truth_revision_no, settled_bucket, settled_tmax_c from basket_station_day_settlement_label "
            "where station_id=? and event_date>=? and event_date<=?", (st, D0, D1)):
        if d not in labels or rev > labels[d][0]:
            labels[d] = (rev, b, float(tmax))
    fb = {}
    for d, rev, tmax, qc, sdef in c.execute(
            "select local_date, revision_no, official_tmax_c, qc_status, settlement_definition from calibration_station_daily_high_truth "
            "where station_id=? and local_date>=? and local_date<=?", (st, D0, D1)):
        if qc != "ACCEPTED" or sdef != "official_daily_tmax_c" or d in labels:
            continue
        if d not in fb or rev > fb[d][0]:
            fb[d] = (rev, None, float(tmax))
    labels.update(fb)
    for day, (rev, lab_b, tmax) in sorted(labels.items()):
        for (eid, dec, tz, pj, oj, wj, cj, shj, pvj, snap, ivj, hlbj, rij, mcc, brj) in c.execute(Q, (st, day)).fetchall():
            stats["env"] += 1
            w = jl(wj) or {}
            centres = jl(cj) or {}
            shas = jl(shj) or {}
            prov = jl(pvj) or {}
            hlb = jl(hlbj) or {}
            rinit = jl(rij) or {}
            path = os.path.join(ARCH, day, f"{st}-{(snap or '')[:16]}.json.gz")
            if not snap or not os.path.exists(path):
                stats["no_packet"] += 1
                continue
            pk = json.loads(gzip.open(path).read())
            members = {s["ensemble_system_id"]: [float(m["raw_tmax_c"]) for m in s["members"]] for s in pk["systems"]}
            systems = {}
            bad = False
            for sysid, wt in w.items():
                if float(wt) <= 1e-12:
                    continue
                vals = members.get(sysid)
                if not vals:
                    bad = True
                    break
                m = sum(vals) / len(vals)
                sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
                meta = {}
                if sysid in prov:
                    pv = prov[sysid]
                    params = {"bias_c": float(pv["bias_c"])}
                    kind = "provisional:" + str(pv.get("source"))
                    pooled = float(pv["bias_c"])
                else:
                    a = artifact(shas.get(sysid))
                    if a is None:
                        stats["no_artifact"] += 1
                        bad = True
                        break
                    params = a["bias"]
                    kind = "certified"
                    pooled = float(params.get("bias_c"))
                    meta = {("art_" + k): a[k] for k in ("n_eff", "train_start", "train_end", "fit_cases", "fit_through", "lead", "cycle", "season", "sigma_tr")}
                b = _resolved_bias_c(m, params)                     # production resolver
                b_const = _resolved_bias_c(m, {"bias_c": pooled})   # same resolver, constant (pooled) form
                if sysid in centres and abs(m + b - float(centres[sysid])) > 1e-6:
                    stats["centre_mismatch"] += 1
                run_age = None
                if rinit.get(sysid):
                    run_age = (ts(dec) - ts(rinit[sysid])).total_seconds() / 3600.0
                systems[sysid] = dict(w=float(wt), n=len(vals), raw=m, sd=sd, b=b, b_const=b_const, kind=kind,
                                      form=str(params.get("correction_form") or "constant"),
                                      lead=hlb.get(sysid), run_age_h=run_age, lineage_centre=centres.get(sysid), **meta)
                stats["sys_rows"] += 1
            if bad or not systems:
                continue
            h = hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tz)
            snaprow = c.execute(
                "select observed_at, buckets_json from basket_official_ws_snapshot "
                "where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? "
                "order by observed_at desc, sequence desc limit 1", (st, day, dec)).fetchone()
            mv = None
            if snaprow is not None and (ts(dec) - ts(snaprow[0])).total_seconds() <= 1800:
                mref = market_reference_v1(json.loads(snaprow[1]))
                if mref.get("state") == "complete":
                    mv = {k: float(v) for k, v in mref["vector"].items()}
            served = {k: float(v) for k, v in json.loads(pj).items()}
            if mv is not None and set(mv) != set(served):
                mv = None
            stats["paired"] += mv is not None
            ordn = json.loads(oj)
            out.append(dict(st=st, day=day, eid=eid, dec=dec, tz=tz, h=h, tier=tier(h), era="pool" if dec >= POOL else "pre",
                            tmax=tmax, lab_b=lab_b, served=served, order=sorted(ordn, key=lambda k: ordn[k]),
                            intervals=jl(ivj), mv=mv, systems=systems, model_center_c=mcc, bridge=jl(brj)))
    print(st, len(out), stats, round(time.time() - t0, 1), "rssMB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1_000_000, flush=True)
pickle.dump(dict(rows=out, stats=stats, D0=D0, D1=D1), open(OUT, "wb"))
print("done", len(out), stats)
