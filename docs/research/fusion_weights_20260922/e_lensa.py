"""Lens A: production P0 compile replayed on archived packets, weights swapped.

Sample: the first post-pool envelope of every (station, event day, training
lead bucket) whose packet is archived.  Each arm recompiles the same packet at
the same decision instant with only ``system_weights`` replaced (replay_lib
hook); everything downstream of the weights is production code.
"""
import pickle, sys, time
from collections import defaultdict
sys.path.insert(0, "/Users/xiong/weather/docs/research/fusion_weights_20260922")
import replay_lib as R
import weights_lib as W
import arms_lib as A

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw"
D = pickle.load(open(f"{SP}/index.pkl", "rb"))
recs, truth = D["recs"], D["truth"]
cells = {}
for r in recs:
    if not r["packet_exists"]:
        continue
    k = (r["station"], r["day"], r["lead"])
    if k not in cells or r["decision"] < cells[k]["decision"]:
        cells[k] = r
sample = sorted(cells.values(), key=lambda r: (r["day"], r["station"], r["decision"]))
first_sd = {}
for r in sample:
    k = (r["station"], r["day"])
    if k not in first_sd or r["decision"] < first_sd[k]["decision"]:
        first_sd[k] = r
K, N = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (0, 1)
print("Lens A envelopes", len(sample), "station-days", len(first_sd), "shard", K, "of", N, flush=True)
sample = sample[K::N]

t0 = time.time()
S = W.Skill()
sel = A.Selector(S.first_commit)
conn = R.CachingConn()
out, errs = [], defaultdict(int)
for i, r in enumerate(sample):
    packet = R.load_packet(r["packet_path"])
    conn.reset(r["tz"])
    try:
        base = R.compile_with_weights(conn, packet, r["decision"], None)
    except Exception as e:  # noqa: BLE001
        errs["prod:" + repr(e)[:80]] += 1
        continue
    cap = base["cap"]
    systems = sorted(cap["sp"])
    if set(cap["prod"]) != set(systems):
        errs["rider_or_mismatch"] += 1
        continue
    tv = truth[(r["station"], r["day"])]["tmax"]
    ctx = dict(st=r["station"], d=r["day"], t=r["decision"], systems=systems,
               family=cap["family"], caps=cap["caps"],
               leads={s: (r["hour_lead"] or {}).get(s) or r["lead"] for s in systems},
               key=f'{r["station"]}|{r["day"]}|{r["lead"]}')
    arms, fb = A.all_arms(S, sel, ctx, prod=cap["prod"], centres=cap["centers"], truth=tv)
    res = dict(station=r["station"], day=r["day"], lead=r["lead"], decision=r["decision"],
               intervals=r["intervals"], truth=tv, served=r["prob"], n=len(systems),
               first_of_day=first_sd[(r["station"], r["day"])] is r,
               centres=cap["centers"], sp=cap["sp"], weights={}, prob={}, fb=fb,
               k_eb=(arms.get("_k_eb7"), arms.get("_k_eb14")), kappa=arms.get("_kappa"),
               err={})
    res["prob"]["prod"] = base["prob"]
    res["weights"]["prod"] = cap["prod"]
    for a in A.ARMS_A:
        if a == "prod":
            continue
        w = arms[a]
        res["weights"][a] = w
        try:
            got = R.compile_with_weights(conn, packet, r["decision"], lambda cap, w=w: w)
            res["prob"][a] = got["prob"]
        except Exception as e:  # noqa: BLE001
            res["err"][a] = repr(e)[:160]
            errs[a + ":" + repr(e)[:60]] += 1
    out.append(res)
    if i % 100 == 0:
        print(i, round(time.time() - t0), "s", dict(errs), flush=True)
pickle.dump(dict(rows=out, errs=dict(errs), shimmed=conn.shimmed), open(f"{SP}/lensa_{K}of{N}.pkl", "wb"))
print("done", len(out), round(time.time() - t0), "s", dict(errs), "registry shimmed", conn.shimmed)
