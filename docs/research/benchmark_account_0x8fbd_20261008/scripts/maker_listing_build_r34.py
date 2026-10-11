"""Round 34 data: for every settled station-day 2026-09-28..10-10, the bias-
corrected multi-model forecast bucket F and the cluster-implied bucket C five
minutes after listing (T0), each bucket's conditionId (gamma), and the public
trade tape of F, F+1 and C from listing to listing + 12 h 5 min (data-api,
paged back from the window's end).  Writes <bench>/r34_rows.json; resumable
tape cache <bench>/r34_tapes.json.
"""
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

B = Path(sys.argv[1])
src = Path(__file__).with_name("listing_buy_r33.py").read_text()
ns = {"__name__": "r33", "__file__": str(Path(__file__).with_name("listing_buy_r33.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("WINDOWS = {")[0], "listing_buy_r33_head", "exec"), ns)
G, TRUTH, ladder, bi, fc, bias, LAG, ts_ = (ns[k] for k in ("G", "TRUTH", "ladder", "bi", "fc", "bias", "LAG", "ts_"))
CLIENT = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=6, max_keepalive_connections=6))


def get_json(url, params, tries=6):
    for i in range(tries):
        try:
            r = CLIENT.get(url, params=params)
            j = r.json()
            if isinstance(j, dict) and ("Too Many" in str(j.get("error")) or r.status_code == 429):
                time.sleep(5 + 5 * i)
                continue
            return j
        except Exception:
            time.sleep(2 + 2 * i)
    return None


GP = B / "r34_gamma.json"
cond = json.load(open(GP)) if GP.exists() else {}
univ = []
for k, g in G.items():
    if "error" in g or not g.get("listed"):
        continue
    st, ed = k.split("|")
    if "2026-09-28" <= ed <= "2026-10-10" and (st, ed) in TRUTH:
        univ.append((k, g))


def gam(item):
    k, g = item
    ev = get_json("https://gamma-api.polymarket.com/events", {"slug": g["slug"]})
    if not ev:
        return k, None
    return k, {mk["groupItemTitle"]: mk.get("conditionId") for mk in ev[0].get("markets", [])}


with ThreadPoolExecutor(max_workers=6) as pool:
    for k, v in pool.map(gam, [u for u in univ if u[0] not in cond]):
        if v:
            cond[k] = v
json.dump(cond, open(GP, "w"))
print(f"universe {len(univ)}; with conditionIds {sum(1 for k, _ in univ if k in cond)}", file=sys.stderr)

rows = []
for k, g in univ:
    st, ed = k.split("|")
    lad = ladder(g)
    if not lad or k not in cond or TRUTH[(st, ed)][0] not in lad["labels"]:
        continue
    lt = ts_(g["listed"])
    T0 = lt + 300
    raw = {m: fc(st, ed, m, T0) for m in LAG}
    raw = {m: v for m, v in raw.items() if v is not None}
    if len(raw) < 3:
        continue
    cor = {m: v + bias(st, ed, m) for m, v in raw.items()}
    med_c, med_r = statistics.median(cor.values()), statistics.median(raw.values())
    F = bi(lad, med_c)
    C = bi(lad, med_r + 0.8 + 0.3 * (med_c - med_r))
    labs = lad["labels"]
    legs = {"F": labs[F], "F+1": labs[min(len(labs) - 1, F + 1)], "C": labs[C]}
    rows.append({"key": k, "st": st, "ed": ed, "listed": lt, "T0": T0, "labels": labs, "truth": TRUTH[(st, ed)][0],
                 "legs": legs, "cid": {lab: cond[k].get(lab) for lab in set(legs.values())}})
print(f"rows with a forecast at T0: {len(rows)}", file=sys.stderr)

TP = B / "r34_tapes.json"
tapes = json.load(open(TP)) if TP.exists() else {}
jobs = []
for r in rows:
    for lab, cid in r["cid"].items():
        if cid and cid not in tapes:
            jobs.append((cid, int(r["listed"]), int(r["listed"]) + 12 * 3600 + 300))
jobs = list({j[0]: j for j in jobs}.values())


def tape(job):
    cid, lo, hi = job
    out, end = [], hi
    for _ in range(40):
        page = get_json("https://data-api.polymarket.com/trades", {"market": cid, "limit": 500, "takerOnly": "false", "end": end})
        if not isinstance(page, list) or not page:
            break
        out += [{"t": int(x["timestamp"]), "outcome": x.get("outcome"), "price": float(x["price"]), "size": float(x["size"]),
                 "side": x.get("side"), "w": str(x.get("proxyWallet", ""))[:10]} for x in page]
        oldest = min(int(x["timestamp"]) for x in page)
        if oldest <= lo or len(page) < 500:
            break
        end = oldest
        time.sleep(0.2)
    seen, uniq = set(), []
    for x in out:
        key = (x["t"], x["outcome"], x["price"], x["size"], x["side"], x["w"])
        if key not in seen and lo <= x["t"] <= hi:
            seen.add(key)
            uniq.append(x)
    return cid, uniq


t0 = time.time()
with ThreadPoolExecutor(max_workers=4) as pool:
    for n, (cid, v) in enumerate(pool.map(tape, jobs)):
        tapes[cid] = v
        if (n + 1) % 200 == 0:
            json.dump(tapes, open(TP, "w"))
            print(n + 1, "tapes", round(time.time() - t0), "s", file=sys.stderr)
        if time.time() - t0 > 500:
            print("time budget reached; rerun to continue", file=sys.stderr)
            break
json.dump(tapes, open(TP, "w"))
json.dump(rows, open(B / "r34_rows.json", "w"))
print(f"tapes {len(tapes)} of {len(jobs) + len([1 for r in rows for c in r['cid'].values() if c in tapes]) and len(set(c for r in rows for c in r['cid'].values() if c))};"
      f" trades in hand {sum(len(v) for v in tapes.values())}", file=sys.stderr)
