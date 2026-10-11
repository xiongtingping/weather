"""Round 32 (熊先生 10-11 「不是说延续，不是说已持加仓，我说的首次！」): the
cluster's FIRST trade on each station-day -- before it holds anything there.

Per station-day the cluster traded (bench cluster_fills_all.json, 8 wallets, buys,
09-21 14:13Z..10-10 13:41Z): the earliest fill f0 and the first burst (all
fills within 120 s of it).  Kept only where the market was listed after
2026-09-22 00:00Z, so the fill record covers the station-day from its listing.
At f0, from public reads and the SSOT read-only:
  listing time and ladder      gamma event (min acceptingOrdersTimestamp)
  the market                   CLOB prices-history, 1-minute, every bucket, f0-3h..f0+5min:
                               price 60 s before f0 and 61 min before f0 (book mids)
  our belief                   the served vector at f0 (P1 > P0-M > P0, latest committed <= f0)
  raw models                   six native ensembles (newest run in hand at f0), point runs
  momentum                     direction_signal.momentum at f0
  truth                        settled bucket (latest revision)
Writes <bench>/first_rows.json.gz.
"""
import collections
import gzip
import json
import math
import re
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target.copy_follow_lane import parse_event_slug  # noqa: E402

B = Path(sys.argv[1])
LISTED_AFTER = datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp()
conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
CLIENT = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=20, max_keepalive_connections=20))
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

fills = json.load(open(B / "cluster_fills_all.json"))
sd = collections.defaultdict(list)
for f in fills:
    sd[(f["station"], f["event_date"])].append(f)
for v in sd.values():
    v.sort(key=lambda f: (int(f["ts"]), f["w"]))
city = {}
for st, slug in conn.execute("SELECT station_id, slug FROM basket_target_market_registry WHERE slug<>''"):
    p = parse_event_slug(slug)
    if p:
        city.setdefault(st, p[0])
print(f"station-days traded {len(sd)}; stations with a city slug {len(city)}", file=sys.stderr)


def get_json(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = CLIENT.get(url, params=params)
            if len(r.content) > 4_000_000:
                raise ValueError("oversized")
            return r.json()
        except Exception:
            time.sleep(1.5 + 2 * i)
    return None


GPATH = B / "first_gamma.json"
gam = json.load(open(GPATH)) if GPATH.exists() else {}


def gamma(key):
    st, ed = key.split("|")
    if st not in city:
        return key, {"error": "no_city"}
    y, m, d = (int(x) for x in ed.split("-"))
    slug = f"highest-temperature-in-{city[st]}-on-{MONTHS[m - 1]}-{d}-{y}"
    ev = get_json("https://gamma-api.polymarket.com/events", {"slug": slug})
    if not ev:
        return key, {"error": "no_event", "slug": slug}
    rows, listed = [], []
    for mk in ev[0].get("markets", []):
        outs = json.loads(mk["outcomes"]) if isinstance(mk["outcomes"], str) else mk["outcomes"]
        toks = json.loads(mk["clobTokenIds"]) if isinstance(mk["clobTokenIds"], str) else mk["clobTokenIds"]
        rows.append({"label": mk["groupItemTitle"], "yes": toks[outs.index("Yes")], "no": toks[outs.index("No")]})
        if mk.get("acceptingOrdersTimestamp"):
            listed.append(mk["acceptingOrdersTimestamp"])
    return key, {"slug": slug, "buckets": rows, "listed": min(listed) if listed else ev[0].get("startDate")}


todo = [f"{st}|{ed}" for st, ed in sd if f"{st}|{ed}" not in gam or "error" in gam[f"{st}|{ed}"]]
with ThreadPoolExecutor(max_workers=8) as pool:
    for k, v in pool.map(gamma, todo):
        gam[k] = v
json.dump(gam, open(GPATH, "w"))
print(f"gamma events ok {sum(1 for v in gam.values() if 'error' not in v)} of {len(gam)}", file=sys.stderr)


def ts_of(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def bounds(label):
    def f2c(v):
        return (v - 32.0) * 5.0 / 9.0
    m = re.match(r"^(-?\d+)°([CF]) or below$", label)
    if m:
        v = float(m.group(1)) + 0.5
        return (-math.inf, v if m.group(2) == "C" else f2c(v)), m.group(2)
    m = re.match(r"^(-?\d+)°([CF]) or higher$", label)
    if m:
        v = float(m.group(1)) - 0.5
        return (v if m.group(2) == "C" else f2c(v), math.inf), m.group(2)
    m = re.match(r"^(-?\d+)-(-?\d+)°F$", label)
    if m:
        return (f2c(float(m.group(1)) - 0.5), f2c(float(m.group(2)) + 0.5)), "F"
    m = re.match(r"^(-?\d+)°C$", label)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5), "C"
    return None, None


keep = []
for (st, ed), fl in sd.items():
    g = gam.get(f"{st}|{ed}")
    if not g or "error" in g or not g.get("listed"):
        continue
    lt = ts_of(g["listed"])
    if lt < LISTED_AFTER:
        continue
    keep.append((st, ed, fl, g, lt))
print(f"station-days listed after 09-22 00Z with a ladder: {len(keep)}", file=sys.stderr)

# CLOB 1-minute paths around f0
PPATH = B / "first_paths.json"
paths = json.load(open(PPATH)) if PPATH.exists() else {}
jobs = []
for st, ed, fl, g, lt in keep:
    f0 = int(fl[0]["ts"])
    for b in g["buckets"]:
        k = f"{st}|{ed}|{b['label']}"
        if k not in paths:
            jobs.append((k, b["yes"], f0 - 3 * 3600, f0 + 300))


def path(job):
    k, tok, s, e = job
    j = get_json("https://clob.polymarket.com/prices-history", {"market": tok, "startTs": s, "endTs": e, "fidelity": 1}, tries=3)
    if not j or "history" not in j:
        return k, {"error": "fetch_failed"}
    h = sorted(j["history"], key=lambda x: int(x["t"]))
    return k, {"t": [int(x["t"]) for x in h], "p": [float(x["p"]) for x in h]}


t0 = time.time()
with ThreadPoolExecutor(max_workers=20) as pool:
    for n, (k, v) in enumerate(pool.map(path, jobs)):
        paths[k] = v
json.dump(paths, open(PPATH, "w"))
print(f"paths fetched {len(jobs)} in {time.time() - t0:.0f} s; errors {sum(1 for v in paths.values() if 'error' in v)}", file=sys.stderr)


def price_at(k, at, stale=3 * 3600):
    v = paths.get(k)
    if not v or "t" not in v:
        return None
    best = None
    for t, p in zip(v["t"], v["p"]):
        if t <= at:
            best = (t, p)
        else:
            break
    return best[1] if best and at - best[0] <= stale else None


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


labels_truth = {}
for st, ed, b, t in conn.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c FROM basket_station_day_settlement_label l"
        " WHERE event_date>='2026-09-20' AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
        " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    labels_truth[(st, ed)] = (b, t)
NATIVE = {"ECMWF_IFS_ENS_OPEN_PF50": "ifs", "ECMWF_AIFS_ENS_GLOBAL": "aifs", "DWD_ICON_GLOBAL_EPS": "icon",
          "NOAA_GEFS_05": "gefs", "ECCC_GEM_GLOBAL_ENS": "gem", "NOAA_AIGEFS_025": "aigefs"}


def served(st, ed, at):
    best = None
    for stage in ("P1", "P0-M", "P0"):
        row = conn.execute("SELECT history_id, decision_at_utc FROM basket_probability_bridge_history WHERE station_id=? AND event_date=?"
                           " AND stage=? AND state='COMMITTED' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1",
                           (st, ed, stage, at)).fetchone()
        if row:
            best = (stage, row[0], str(row[1]))
            break
    if not best:
        return None
    vec = {str(b): float(p) for b, p in conn.execute(
        "SELECT bucket, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?", (best[1],))}
    tot = sum(vec.values())
    return {"stage": best[0], "at": best[2], "p": {k: v / tot for k, v in vec.items()} if tot > 0 else {}}


def natives(st, ed, at, bnd):
    out = {}
    runs = collections.defaultdict(lambda: collections.defaultdict(list))
    for sysid, run, had, v in conn.execute(
            "SELECT ensemble_system_id, native_run_init_utc, archive_query_at_utc, raw_tmax_c FROM calibration_native_ensemble_historical_member_run"
            " WHERE station_id=? AND target_local_date=? AND qc_status='ACCEPTED' AND native_run_init_utc IS NOT NULL AND raw_tmax_c IS NOT NULL",
            (st, ed)):
        if sysid in NATIVE and str(had) <= at:
            runs[sysid][str(run)].append(float(v))
    for sysid, rr in runs.items():
        ok = {r: v for r, v in rr.items() if len(v) >= 10}
        if not ok:
            continue
        run = max(ok)
        vals = ok[run]
        out[NATIVE[sysid]] = {"mean": statistics.mean(vals), "sd": statistics.pstdev(vals), "run": run,
                              "probs": {lab: sum(1 for x in vals if lo <= x < hi) / len(vals) for lab, (lo, hi) in bnd.items()}}
    return out


rows = []
for n, (st, ed, fl, g, lt) in enumerate(keep):
    f0 = fl[0]
    ts = int(f0["ts"])
    lad = []
    for b in g["buckets"]:
        bd, unit = bounds(b["label"])
        if bd is None:
            lad = None
            break
        lad.append((bd, unit, b["label"]))
    if not lad:
        continue
    lad.sort(key=lambda r: r[0][0])
    labels = [r[2] for r in lad]
    unit = lad[0][1]
    width = 1.0 if unit == "C" else 10.0 / 9.0
    bnd = {r[2]: r[0] for r in lad}
    now = {lab: price_at(f"{st}|{ed}|{lab}", ts - 60) for lab in labels}
    ago = {lab: price_at(f"{st}|{ed}|{lab}", ts - 3660) for lab in labels}
    burst = [(int(f["ts"]) - ts, f["bucket"], f["outcome"], float(f["price"]), float(f["size"]), bool(f["taker"]), f["w"][:10])
             for f in fl if int(f["ts"]) - ts <= 120]
    at = iso(ts)
    mom = ds.momentum(conn, station_id=st, event_date=ed, at_utc=ts)
    truth = labels_truth.get((st, ed))
    rows.append({"st": st, "ed": ed, "ts": ts, "listed": lt, "unit": unit, "width": width, "labels": labels,
                 "bounds": {k: [v[0] if math.isfinite(v[0]) else None, v[1] if math.isfinite(v[1]) else None] for k, v in bnd.items()},
                 "first": {"bucket": f0["bucket"], "side": f0["outcome"], "price": float(f0["price"]), "size": float(f0["size"]),
                           "taker": bool(f0["taker"]), "w": f0["w"][:10]},
                 "burst": burst, "now": now, "ago": ago, "served": served(st, ed, at), "raw": natives(st, ed, at, bnd),
                 "M": mom["M"], "M_parts": {k: v["delta_c"] for k, v in mom["components"].items()},
                 "truth": truth[0] if truth else None, "truth_c": float(truth[1]) if truth and truth[1] is not None else None,
                 "n_fills_day": len(fl)})
    if (n + 1) % 100 == 0:
        print(n + 1, "station-days", file=sys.stderr)
json.dump(rows, gzip.open(B / "first_rows.json.gz", "wt"))
print(f"rows {len(rows)}; with a full ladder price at f0-60s {sum(1 for r in rows if all(v is not None for v in r['now'].values()))};"
      f" with served belief {sum(1 for r in rows if r['served'])}; with >=2 raw systems {sum(1 for r in rows if len(r['raw']) >= 2)};"
      f" settled {sum(1 for r in rows if r['truth'])}", file=sys.stderr)
