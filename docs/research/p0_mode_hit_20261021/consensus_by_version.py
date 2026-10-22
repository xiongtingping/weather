#!/usr/bin/env python3
"""Charter 279 follow-up (user 10-22 「按版本段拆十家名单的读数」): the consensus shift's
walk-forward scores split by the P0 / P0-M serving version of the scored decision
(charter 255).  Parameters are still fitted on every earlier event day, whatever
its version; only the scoring is split.  Records = the fit's (first decision per
settled station-day x family, served vector, consensus knowable at it) plus the
envelope's posterior_version."""
import collections, json, math, os, pickle, sqlite3, statistics, sys, time
from datetime import datetime, timedelta, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target import deterministic_consensus_shift as DCS
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket
from bayes_basket_target.regional_point_lane import runs_for
from bayes_basket_target.settlement_time import hours_to_settlement
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "consensus_by_version.txt"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
PKL = os.path.join(HERE, "consensus_by_version_recs.pkl")
SQL = ("SELECT decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json,"
       " json_extract(lineage_json,'$.bucket_intervals_c_json'), posterior_version,"
       " json_extract(lineage_json,'$.observed_hard_excluded_buckets'), json_extract(lineage_json,'$.hard_excluded_buckets')"
       " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
       " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage=? ORDER BY decision_at_utc LIMIT ?")
LIMIT = {"P0": -1, "P0-M": 1, "P1": 1}
def _excl(t): return [b for b in str(t or "").split(",") if b and b != "NONE"]
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60

def collect():
    conn = sqlite3.connect(DB, uri=True, timeout=120); conn.execute("PRAGMA query_only=ON")
    today = datetime.now(timezone.utc).date(); start = (today - timedelta(days=DAYS)).isoformat(); end = (today - timedelta(days=1)).isoformat()
    recs = []; t0 = time.time()
    for (station,) in conn.execute("SELECT station_id FROM calibration_station_registry ORDER BY station_id").fetchall():
        truth = {}
        for day, _rev, tmax in conn.execute("SELECT event_date, truth_revision_no, settled_tmax_c FROM basket_station_day_settlement_label WHERE station_id=? AND event_date>=? AND event_date<=? ORDER BY event_date, truth_revision_no", (station, start, end)):
            if tmax is not None: truth[str(day)] = float(tmax)
        for day in sorted(truth):
            picked = []; seen = set()
            for stage in ("P0", "P0-M", "P1"):
                for row in conn.execute(SQL, (station, day, stage, LIMIT[stage])).fetchall():
                    try: hours = hours_to_settlement(day, as_of_utc=row[0], settlement_timezone=row[1])
                    except Exception: continue
                    fam = DCS.stage_family(stage, hours)
                    if fam is None or fam in seen: continue
                    seen.add(fam); picked.append((fam, row))
                    if stage != "P0" or len([f for f in seen if f.startswith("P0_")]) == 2: break
            if not picked: continue
            try: rows = runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            except Exception: rows = None
            for fam, (dec, tz, pj, oj, ivj, ver, x0m, x1) in picked:
                try: served = {k: float(v) for k, v in json.loads(pj).items()}; ordinals = json.loads(oj); intervals = json.loads(ivj)
                except (TypeError, ValueError): continue
                ordered = sorted(ordinals, key=lambda k: ordinals[k])
                if set(ordered) != set(served) or not isinstance(intervals, dict): continue
                winner = _winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                if winner is None or winner not in served: continue
                read = DCS.consensus_read(conn, station_id=station, event_date=day, as_of_utc=dec, settlement_timezone=tz, rows=rows)
                if read.get("state") != "ok" or all(v is None for v in read["f_c"].values()): continue
                recs.append({"station": station, "day": day, "family": fam, "decision_at_utc": dec, "version": ver,
                             "centre_c": DCS.served_mean_c(served, ordered, intervals), "f_c": dict(read["f_c"]), "n": dict(read["n"]),
                             "y_c": truth[day], "p": served, "ordered": ordered, "intervals": intervals, "winner": winner,
                             "excluded": _excl(x0m if fam == "P0M" else x1 if fam == "P1" else "")})
    conn.close(); P(f"collected {len(recs)} records in {time.time()-t0:.0f}s window {start}..{end}")
    return recs

if os.path.exists(PKL) and "--refresh" not in sys.argv:
    recs = pickle.load(open(PKL, "rb")); P(f"loaded {len(recs)} records from pickle")
else:
    recs = collect(); pickle.dump(recs, open(PKL, "wb"))

def forward(records, family, *, keep):
    """walk_forward with parameters on all earlier days, scoring only records keep(r) admits."""
    fam_recs = [r for r in records if r["family"] == family]
    days = sorted({r["day"] for r in fam_recs})
    acc = {v: collections.defaultdict(list) for v in DCS.VARIANTS}; sums = {v: collections.Counter() for v in DCS.VARIANTS}; n = collections.Counter()
    for day in days:
        params = DCS.fit_parameters(fam_recs, family=family, before_day=day)
        if not params: continue
        for r in (x for x in fam_recs if x["day"] == day and keep(x)):
            for variant, p in params.items():
                form, roster = DCS.variant_parts(variant); f = r["f_c"].get(roster)
                if f is None: continue
                bias, _ = DCS.bias_for(p, form=form, station_id=r["station"])
                delta = max(-DCS.CAP_C, min(DCS.CAP_C, float(p["kappa"]) * (f + bias - r["centre_c"])))
                shadow = DCS.shift_served(r["p"], r["ordered"], r["intervals"], delta, excluded=r.get("excluded") or (), censor=DCS.CENSOR_BY_FAMILY.get(family, False))
                s = DCS.score_record(r, shadow)
                acc[variant][day].append(s["logscore_after"] - s["logscore_before"]); n[variant] += 1
                for k, v in s.items(): sums[variant][k] += v
    res = {}
    for variant in DCS.VARIANTS:
        if n[variant] < 30: res[variant] = None; continue
        lo, hi = DCS._bootstrap_ci(acc[variant], draws=300, seed=279)
        m = n[variant]
        res[variant] = dict(n=m, days=len(acc[variant]), d_ls=(sums[variant]["logscore_after"]-sums[variant]["logscore_before"])/m, ci=(lo, hi),
                            d_crps=(sums[variant]["crps_after"]-sums[variant]["crps_before"])/m, d_brier=(sums[variant]["brier_after"]-sums[variant]["brier_before"])/m,
                            ls0=sums[variant]["logscore_before"]/m, days_better=sum(1 for v in acc[variant].values() if statistics.mean(v) < 0))
    return res

def show(label, res):
    for variant, r in res.items():
        if r is None: P(f"   {label:34s} {variant:13s} n<30"); continue
        P(f"   {label:34s} {variant:13s} n={r['n']:5d} 天={r['days']:3d} LS0 {r['ls0']:.3f} ΔLS {r['d_ls']:+.4f} [{r['ci'][0]:+.4f},{r['ci'][1]:+.4f}] ΔCRPS {r['d_crps']:+.4f} ΔBrier {r['d_brier']:+.4f} 改善天 {r['days_better']}/{r['days']}")

for family in DCS.STAGE_FAMILIES:
    fam = [r for r in recs if r["family"] == family]
    vers = collections.OrderedDict()
    for r in sorted(fam, key=lambda r: r["decision_at_utc"]):
        v = vers.setdefault(r["version"], {"first": r["decision_at_utc"][:10], "last": r["decision_at_utc"][:10], "n": 0, "days": set()})
        v["last"] = r["decision_at_utc"][:10]; v["n"] += 1; v["days"].add(r["day"])
    current = list(vers)[-1]
    P(f"\n==================== {family}：{len(fam)} 条记录，服务版本 {len(vers)} 段（现行 {current}）====================")
    for ver, v in vers.items():
        P(f"   {ver:28s} 决策 {v['first']}…{v['last']}  n={v['n']:4d} 事件日 {len(v['days'])}")
    P("A. 全窗（与每日拟合同口径）")
    show("全窗", forward(recs, family, keep=lambda r: True))
    P("B. 只评现行版本段（参数仍用之前全部事件日）")
    show(f"现行 {current}", forward(recs, family, keep=lambda r, c=current: r["version"] == c))
    P("C. 只评旧版本段")
    show("旧版本", forward(recs, family, keep=lambda r, c=current: r["version"] != c))
    P("D. 逐段（≥30 条才报）")
    for ver in vers:
        show(f"  {ver}", forward(recs, family, keep=lambda r, v=ver: r["version"] == v))
    P("E. 按决策日历段（10-01 起 / 10-11 起）")
    show("决策 ≥10-01", forward(recs, family, keep=lambda r: r["decision_at_utc"] >= "2026-10-01"))
    show("决策 ≥10-11", forward(recs, family, keep=lambda r: r["decision_at_utc"] >= "2026-10-11"))
    P("F. 现行段内：四家 F 可得 vs 不可得的决策（十家名单的增益来自哪里）")
    show("现行·四家可得", forward(recs, family, keep=lambda r, c=current: r["version"] == c and r["f_c"].get("core") is not None))
    show("现行·四家不可得", forward(recs, family, keep=lambda r, c=current: r["version"] == c and r["f_c"].get("core") is None))
out.close()
