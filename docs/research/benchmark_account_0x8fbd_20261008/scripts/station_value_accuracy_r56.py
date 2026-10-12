"""Round 56 (熊先生 10-12 「需要对站点进行验证配对吗？…双线性插值…递减率…有效时间」):
is the station-value F also the more ACCURATE F, and where does its warm offset come from?
(a) universe A (first-rows station-days with a truth, F at listing + 5 min): mean of
    F_v3(lambda 0.5) - F split into raw-median shift, fleet-shift change and the
    station-correction change; (b) F_v3 with its day-block bias re-centred (leave one
    event day out) -- MAE and hits against F; (c) the same at lambda 0.75 / 0.9.
"""
import statistics, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("f_station_values_ablation_r55c.py").read_text()
ns = {"__name__": "r55c", "__file__": str(here.with_name("f_station_values_ablation_r55c.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("MODES = {")[0], "r55c_head", "exec"), ns)
rows, bucket, prod_f, ROSTER, ALL_TRUTHS, conn, centre, fleet_c, value, history_point, SIX, FR_ALL, edges_of, paired = (
    ns[k] for k in ("rows", "bucket", "prod_f", "ROSTER", "ALL_TRUTHS", "conn", "centre", "fleet_c", "value", "history_point", "SIX", "FR_ALL", "edges_of", "paired"))
from bayes_basket_target.deterministic_forecast_family import feature_at  # noqa: E402
from datetime import date, timedelta  # noqa: E402
MODE = {"sflux": True, "lapse": True}
GRID = {"sflux": False, "lapse": False}
A = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5 or r["st"] not in ROSTER:
        continue
    t = int(r["listed"]) + 300
    e = prod_f(t)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    A.append({"st": r["st"], "ed": r["ed"], "f": e["f_c"], "fbase": e["f_base_c"], "raw": e["raw_median_c"], "c": e["fleet_c"], "corr": e["station_corr_c"],
              "y": float(r["truth_c"]), "edges": edges_of(r), "as_of": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
print(f"universe A: {len(A)} station-days")
# (a) decomposition at lambda 0.5 against F_base (no recalibration) and F
parts = []
for a in A:
    st, ed, zone = a["st"], a["ed"], ROSTER[a["st"]][2]
    feats = feature_at(conn, station_id=st, event_date=ed, as_of_utc=a["as_of"], settlement_timezone=zone, systems=SIX)
    if len(feats) < 3:
        continue
    D = date.fromisoformat(ed)
    run_key = (D - timedelta(days=2)).strftime("%Y-%m-%dT%H")
    members = {}
    for s, f in feats.items():
        v = float(f["day_max_c"])
        g3 = ns["gfs_v3"](st, ed)
        members[s] = g3 if (s == ns["GFS_ID"] and g3 is not None and str(f["run_init_utc"])[:13] == run_key) else value(s, st, v, **MODE)
    raw_v3 = statistics.median(members.values())
    raw_grid = statistics.median(float(f["day_max_c"]) for f in feats.values())
    c3 = fleet_c(D, MODE)
    v3 = centre(st, ed, a["as_of"], 0.5, MODE)
    corr3 = (v3 - raw_v3 - c3) / 0.5 if v3 is not None else None
    parts.append({"d_raw": raw_v3 - raw_grid, "d_c": c3 - a["c"], "d_corr": 0.5 * (corr3 - a["corr"]) if corr3 is not None else 0.0, "v3": v3, "a": a})
P = [p for p in parts if p["v3"] is not None]
print(f"(a) F_v3(0.5) - F_base: mean {np.mean([p['v3'] - p['a']['fbase'] for p in P]):+.3f} = raw median shift {np.mean([p['d_raw'] for p in P]):+.3f}"
      f" + fleet shift change {np.mean([p['d_c'] for p in P]):+.3f} + 0.5 x station-correction change {np.mean([p['d_corr'] for p in P]):+.3f};"
      f" F - F_base (recalibration) {np.mean([p['a']['f'] - p['a']['fbase'] for p in P]):+.3f}")
# (b)(c) re-centred F_v3: subtract the training days' mean (truth - F_v3) difference to F, leave one event day out
days = sorted({a["ed"] for a in A})
by_day = {d: [i for i, a in enumerate(A) if a["ed"] == d] for d in days}
rng = np.random.default_rng(56)
base_hit = [bucket(a["edges"], a["f"]) == bucket(a["edges"], a["y"]) for a in A]
for lam in (0.5, 0.75, 0.9):
    vals = [centre(a["st"], a["ed"], a["as_of"], lam, MODE) for a in A]
    ok = [i for i, v in enumerate(vals) if v is not None]
    for recentred in (False, True):
        adj = {}
        for i in ok:
            if recentred:
                tr = [j for j in ok if A[j]["ed"] != A[i]["ed"]]
                shift = np.mean([(A[j]["y"] - vals[j]) - (A[j]["y"] - A[j]["f"]) for j in tr])
            else:
                shift = 0.0
            adj[i] = vals[i] + shift
        dm = np.array([abs(A[i]["y"] - adj[i]) - abs(A[i]["y"] - A[i]["f"]) for i in ok])
        bs = []
        for _ in range(300):
            pick = set(rng.choice(days, size=len(days), replace=True).tolist())
            idx = [k for k, i in enumerate(ok) if A[i]["ed"] in pick]
            bs.append(dm[idx].mean())
        hit = [bucket(A[i]["edges"], adj[i]) == bucket(A[i]["edges"], A[i]["y"]) for i in ok]
        bh = [base_hit[i] for i in ok]
        print(f"({'c' if lam > 0.5 else 'b'}) lambda {lam:.2f} {'re-centred (LODO)' if recentred else 'as is          '}: MAE diff vs F {dm.mean():+.4f} [{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}];"
              f" hits {statistics.mean(hit):.1%} vs F {statistics.mean(bh):.1%} {paired(hit, bh)}; bias {np.mean([A[i]['y'] - adj[i] for i in ok]):+.3f} (F {np.mean([A[i]['y'] - A[i]['f'] for i in ok]):+.3f})")
