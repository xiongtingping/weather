"""Round 55c: what in the station-value F does the work, and is it still an accurate F?

(1) Ablations at lambda 0.9 (the leave-one-day-out choice of round 55b) on the
    227 picks: grid values; lapse-rate downscaling on every member but GFS on
    the 0.25 degree grid; GFS from the 13 km flux grid but no downscaling on
    the others; both (the station-value F).
(2) Accuracy on every first-rows station-day with a truth (universe A, F at
    listing + 5 min, ~717): MAE, bucket hits and bias of the station-value F
    at lambda 0.5 / 0.75 / 0.9 against production F, paired with day-block
    intervals -- the production question.
(3) Freeze the recipe for the forward read.
"""
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("f_station_values_lodo_r55b.py").read_text()
ns = {"__name__": "r55b", "__file__": str(here.with_name("f_station_values_lodo_r55b.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("days = sorted({r[\"ev\"][\"ed\"] for r in rows})")[0], "r55b_head", "exec"), ns)
rows, bucket, prod_f, ROSTER, ALL_TRUTHS, TERRAIN, conn = (ns[k] for k in ("rows", "bucket", "prod_f", "ROSTER", "ALL_TRUTHS", "TERRAIN", "conn"))
history_point, station_value, gfs_v3, const, paired, GFS_ID = (ns[k] for k in ("history_point", "station_value", "gfs_v3", "const", "paired", "GFS_ID"))
from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, feature_at  # noqa: E402

SIX = sorted(F_SYSTEM_IDS)
FR_ALL = ns["ns"]["ns"]["FR_ALL"]
edges_of = ns["ns"]["ns"]["ns"]["edges_of"]


def value(system, st, raw, *, sflux, lapse):
    if system == GFS_ID:
        return raw + (const.get(st, 0.0) if sflux else 0.0) + (0.0 if sflux else (lapse_offset(system, st) if lapse else 0.0))
    return raw + (lapse_offset(system, st) if lapse else 0.0)


def lapse_offset(system, st):
    t = TERRAIN.get(system, {}).get(st)
    return float(t["offset_c"]) if t and t.get("offset_c") is not None else 0.0


_C = {}


def fleet_c(D, mode):
    key = (D, tuple(sorted(mode.items())))
    if key not in _C:
        errs = []
        for k in range(2, 32):
            past = D - timedelta(days=k)
            for st, (_lat, _lon, zone) in ROSTER.items():
                tr = ALL_TRUTHS.get((st, past.isoformat()))
                if tr is None:
                    continue
                pt = history_point(st, past, zone, SIX)
                if len(pt) >= 3:
                    errs.append(tr - statistics.median(value(s, st, v, **mode) for s, v in pt.items()))
        _C[key] = statistics.mean(errs) if len(errs) >= 200 else 0.8
    return _C[key]


def centre(st, ed, as_of, lam, mode, exact_gfs=True):
    zone = ROSTER[st][2]
    feats = feature_at(conn, station_id=st, event_date=ed, as_of_utc=as_of, settlement_timezone=zone, systems=SIX)
    if len(feats) < 3:
        return None
    D = date.fromisoformat(ed)
    run_key = (D - timedelta(days=2)).strftime("%Y-%m-%dT%H")
    members = {}
    for s, f in feats.items():
        v = float(f["day_max_c"])
        if s == GFS_ID and mode["sflux"] and exact_gfs and gfs_v3(st, ed) is not None and str(f["run_init_utc"])[:13] == run_key:
            members[s] = gfs_v3(st, ed)
        else:
            members[s] = value(s, st, v, **mode)
    raw = statistics.median(members.values())
    biases = {}
    for s in members:
        errs = []
        for k in range(2, 61):
            past = D - timedelta(days=k)
            tr = ALL_TRUTHS.get((st, past.isoformat()))
            if tr is None:
                continue
            pt = history_point(st, past, zone, SIX)
            if s in pt:
                errs.append(tr - value(s, st, pt[s], **mode))
        biases[s] = statistics.mean(errs) if len(errs) >= 3 else 0.0
    corr = statistics.median(members[s] + biases[s] for s in members) - raw
    return raw + fleet_c(D, mode) + lam * corr


MODES = {"grid values": {"sflux": False, "lapse": False}, "lapse on all, GFS 0.25": {"sflux": False, "lapse": True},
         "GFS 13 km only, no lapse on others": {"sflux": True, "lapse": False}, "station values (GFS 13 km + lapse on all)": {"sflux": True, "lapse": True}}
days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]
print("(1) ablations on the 227 picks, lambda 0.9")
for name, mode in MODES.items():
    got = []
    for r in rows:
        ev = r["ev"]
        as_of = datetime.fromtimestamp(ev["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        v = centre(ev["st"], ev["ed"], as_of, 0.9, mode)
        if v is not None:
            got.append((r, v))
    same = [bucket(r["ev"]["edges"], v) == r["ev"]["choice"] for r, v in got]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r, _v in got]
    tr = [(r, v) for r, v in got if r["ev"]["truth"] is not None]
    h1 = statistics.mean(s for s, (r, _v) in zip(same, got) if r["ev"]["ed"] < mid) - statistics.mean(b for b, (r, _v) in zip(base, got) if r["ev"]["ed"] < mid)
    h2 = statistics.mean(s for s, (r, _v) in zip(same, got) if r["ev"]["ed"] >= mid) - statistics.mean(b for b, (r, _v) in zip(base, got) if r["ev"]["ed"] >= mid)
    print(f"   {name:42s} n {len(got)}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} halves {h1:+.3f}/{h2:+.3f};"
          f" hits {statistics.mean(bucket(r['ev']['edges'], v) == r['ev']['truth'] for r, v in tr):.1%} vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for r, _v in tr):.1%}")

# (2) accuracy on universe A
print("\n(2) every first-rows station-day with a truth, F at listing + 5 min")
A = []
mode = MODES["station values (GFS 13 km + lapse on all)"]
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5 or r["st"] not in ROSTER:
        continue
    t = int(r["listed"]) + 300
    e = prod_f(t)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    as_of = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    A.append({"st": r["st"], "ed": r["ed"], "f": e["f_c"], "y": float(r["truth_c"]), "edges": edges_of(r), "as_of": as_of})
daysA = sorted({a["ed"] for a in A})
by_day = {d: [i for i, a in enumerate(A) if a["ed"] == d] for d in daysA}
rng = np.random.default_rng(55)
base_hit = [bucket(a["edges"], a["f"]) == bucket(a["edges"], a["y"]) for a in A]
print(f"   n {len(A)}, {len(daysA)} event days; production F: MAE {np.mean([abs(a['y'] - a['f']) for a in A]):.4f}, hits {statistics.mean(base_hit):.1%}, bias {np.mean([a['y'] - a['f'] for a in A]):+.3f}")
for lam in (0.5, 0.75, 0.9):
    vals = [centre(a["st"], a["ed"], a["as_of"], lam, mode) for a in A]
    ok = [i for i, v in enumerate(vals) if v is not None]
    dm = np.array([abs(A[i]["y"] - vals[i]) - abs(A[i]["y"] - A[i]["f"]) for i in ok])
    bs = [np.mean([dm[j] for j, i in enumerate(ok) if A[i]["ed"] in set(rng.choice(daysA, size=len(daysA), replace=True).tolist())]) for _ in range(300)]
    hit = [bucket(A[i]["edges"], vals[i]) == bucket(A[i]["edges"], A[i]["y"]) for i in ok]
    bh = [base_hit[i] for i in ok]
    print(f"   station-value F lambda {lam:.2f}: n {len(ok)}, MAE {np.mean([abs(A[i]['y'] - vals[i]) for i in ok]):.4f} ({dm.mean():+.4f} [{np.percentile(bs, 2.5):+.4f}, {np.percentile(bs, 97.5):+.4f}]);"
          f" hits {statistics.mean(hit):.1%} vs F {statistics.mean(bh):.1%} {paired(hit, bh)}; bias {np.mean([A[i]['y'] - vals[i] for i in ok]):+.3f}")

# (3) freeze
frozen = {"model": "listing_f_station_values_v3_candidate", "members": SIX, "lambda": 0.9, "lambda_plateau": [0.7, 0.9],
          "station_value": {"GFS": "native ~13 km Gaussian flux grid (sflux), hourly TMAX, nearest land cell, + 0.0065 C/m x (cell height - station elevation)",
                            "others": "archived nearest-land value + 0.0065 C/m x (cell height - station elevation) (terrain_offsets_r39h.json)"},
          "fleet_shift": "re-estimated on station values (about +0.7 C vs +1.0 on grid values)",
          "fitted_on": {"picks": len(rows), "event_days": [days[0], days[-1]]},
          "forward_protocol": "pre-formation first YES on event days after 2026-10-11: bucket(F_v3) vs the first pick, paired against bucket(f_c); read with the other frozen candidates at >= 80 picks"}
json.dump(frozen, open(here.parent.parent / "f_station_values_r55_frozen.json", "w"), indent=1)
print("\nfrozen -> f_station_values_r55_frozen.json")
