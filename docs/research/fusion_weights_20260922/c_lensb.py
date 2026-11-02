"""Lens B: every weight arm on the walk-forward single-system forecasts.

Cell = (station, event day, hour lead bucket); members = each P0 system's
freshest run in that bucket (>=2 systems); decision instant = the latest
member's run init (conservative for knowability).  Pool = linear mixture of
the members' 1-C distributions; scored with production ``_metrics`` (CRPS on
the 1-C grid, NLL) against the final truth revision.

Tunable arms (EB shrink k, shrink-to-equal kappa) are chosen per cell on
earlier cells only: event day in [d-21, d) and truth committed by the cell's
own instant.  Output feeds d_lensb_report.py and Lens A's parameter choice.
"""
import math, pickle, sys, time
from collections import defaultdict
from datetime import date, timedelta
sys.path.insert(0, "/Users/xiong/weather/docs/research/fusion_weights_20260922")
import weights_lib as W

OUT = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw/lensb.pkl"
FAMILY = {"ECMWF_IFS_ENS_OPEN_PF50": "ECMWF", "NOAA_GEFS_05": "NOAA_GEFS",
          "DWD_ICON_GLOBAL_EPS": "DWD_ICON_EPS", "ECCC_GEM_GLOBAL_ENS": "ECCC_GEM",
          "NOAA_AIGEFS_025": "NOAA_AIGEFS"}
CAPS = {f: 0.75 for f in FAMILY.values()}
EVAL_FROM, EVAL_TO = "2026-08-24", "2026-09-21"
NS = (3, 5, 7, 14, 30, 60)
EB_N = (7, 14)
K_GRID = (0.0, 2.0, 5.0, 10.0, 20.0, 50.0, math.inf)
KAPPA_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
SEL_DAYS = 21

t0 = time.time()
S = W.Skill()
print("skill loaded", round(time.time() - t0), "s", flush=True)

groups = defaultdict(dict)
for c in S.cases:
    k = (c["station"], c["day"], c["lead"])
    prev = groups[k].get(c["system"])
    if prev is None or c["issued"] > prev["issued"]:
        groups[k][c["system"]] = c
cells = []
for (st, d, lead), mem in groups.items():
    if not (EVAL_FROM <= d <= EVAL_TO) or len(mem) < 2:
        continue
    fin = S.final_value(st, d)
    if fin is None:
        continue
    systems = sorted(mem)
    if len({FAMILY[s] for s in systems}) < 2:
        continue
    t = max(W._z(m["issued"]) for m in mem.values())
    cells.append(dict(st=st, d=d, lead=lead, t=t, systems=systems, truth=fin,
                      probs={s: mem[s]["prob"] for s in systems},
                      centres={s: mem[s]["centre"] for s in systems}))
cells.sort(key=lambda x: (x["d"], x["st"], x["lead"]))
print("cells", len(cells), "station-days", len({(c["st"], c["d"]) for c in cells}), flush=True)


def score(cell, w):
    crps, nll, pt = W.pool_crps_nll(cell["probs"], w, cell["truth"])
    return (crps, nll, pt)


for i, cell in enumerate(cells):
    systems = cell["systems"]
    ctx = dict(st=cell["st"], d=cell["d"], t=cell["t"], systems=systems,
               family={s: FAMILY[s] for s in systems}, caps=CAPS,
               leads={s: cell["lead"] for s in systems}, key=f'{cell["st"]}|{cell["d"]}|{cell["lead"]}')
    eq = W.equal(systems)
    arms, fb = {}, {}
    arms["equal"] = eq
    for n in NS:
        for scope, tag in (("fleet", "fleet"), ("station", "st")):
            stats, k = S.window_stats(scope, ctx["st"], ctx["d"], ctx["t"], systems, None, n)
            for metric in ("mse", "crps"):
                name = f"{tag}_{metric}_{n}"
                if stats is None:
                    arms[name] = eq; fb[name] = True
                else:
                    arms[name] = W.inv_weights(stats, systems, ctx["family"], CAPS, metric)
    for n in EB_N:
        cellst, nc = S.window_stats("station", ctx["st"], ctx["d"], ctx["t"], systems, ctx["leads"], n)
        prior, npr = S.window_stats("fleet", ctx["st"], ctx["d"], ctx["t"], systems, ctx["leads"], n)
        if prior is None:
            prior, npr = S.window_stats("fleet", ctx["st"], ctx["d"], ctx["t"], systems, None, n)
        for kk in K_GRID:
            name = f"stlead_eb{n}_k{kk}"
            m = {}
            for s in systems:
                if cellst is None or math.isinf(kk):
                    m[s] = None if prior is None else prior[s][0]
                elif prior is None or kk == 0:
                    m[s] = cellst[s][0]
                else:
                    m[s] = (nc * cellst[s][0] + kk * prior[s][0]) / (nc + kk)
            if any(v is None for v in m.values()):
                arms[name] = eq; fb[name] = True
            else:
                arms[name] = W.capped({s: 1.0 / max(m[s], 1e-6) for s in systems}, ctx["family"], CAPS)
    w, info = W.arm_prodrule(S, ctx)
    arms["prodrule60"] = w if w is not None else eq
    if w is None:
        fb["prodrule60"] = True
    arms["skillperm"] = W.rotate(arms["st_mse_7"], ctx["key"])
    ose = {s: (cell["centres"][s] - cell["truth"]) ** 2 for s in systems}
    arms["oracle"] = W.capped({s: 1.0 / max(ose[s], 0.01) for s in systems}, ctx["family"], CAPS)
    for kap in KAPPA_GRID:
        arms[f"st_mse7_shr{kap}"] = W.blend(arms["st_mse_7"], systems, kap)
    cell["weights"] = arms
    cell["fallback"] = fb
    cell["scores"] = {a: score(cell, w) for a, w in arms.items()}
    if i % 1000 == 0:
        print(i, round(time.time() - t0), flush=True)

# --- rolling selection on earlier cells only ---------------------------------
by_day = defaultdict(list)
for c in cells:
    by_day[c["d"]].append(c)
first_commit = S.first_commit


def select(prefix_options, d, t, key=lambda c, a: c["scores"][a][0]):
    lo = (date.fromisoformat(d) - timedelta(days=SEL_DAYS)).isoformat()
    tot = {a: 0.0 for a in prefix_options}
    n = 0
    for day in sorted(by_day):
        if day < lo or day >= d:
            continue
        for c in by_day[day]:
            if first_commit.get((c["st"], c["d"]), "9") > t:
                continue
            for a in prefix_options:
                tot[a] += key(c, a)
            n += 1
    if n == 0:
        return prefix_options[-1], 0
    best = min(prefix_options, key=lambda a: (round(tot[a] / n, 10), -prefix_options.index(a)))
    return best, n


for n in EB_N:
    opts = [f"stlead_eb{n}_k{kk}" for kk in K_GRID]
    for c in cells:
        a, m = select(opts, c["d"], c["t"])
        c["weights"][f"stlead_eb{n}"] = c["weights"][a]
        c["scores"][f"stlead_eb{n}"] = c["scores"][a]
        c.setdefault("chosen", {})[f"stlead_eb{n}"] = a
# shrink-to-equal on top of the rolled EB-14 arm and on st_mse_7
for c in cells:
    for kap in KAPPA_GRID:
        w = W.blend(c["weights"]["stlead_eb14"], c["systems"], kap)
        c["weights"][f"stlead_eb14_shr{kap}"] = w
        c["scores"][f"stlead_eb14_shr{kap}"] = score(c, w)
for base in ("st_mse7", "stlead_eb14"):
    opts = [f"{base}_shr{k}" for k in KAPPA_GRID]
    for c in cells:
        a, m = select(opts, c["d"], c["t"])
        c["weights"][f"{base}_shr"] = c["weights"][a]
        c["scores"][f"{base}_shr"] = c["scores"][a]
        c["chosen"][f"{base}_shr"] = a

pickle.dump(dict(cells=cells, K_GRID=K_GRID, KAPPA_GRID=KAPPA_GRID), open(OUT, "wb"))
print("done", len(cells), round(time.time() - t0), "s")
