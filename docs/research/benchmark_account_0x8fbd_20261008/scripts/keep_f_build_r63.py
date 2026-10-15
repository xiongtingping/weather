"""Round 63 data (PROTOCOL_R63_KEEP_F.md), self-contained, no Open-Meteo.

Stages, each cached in <bench> so a rerun resumes:
  fills      data-api /trades of the eight cluster wallets since 09-21 00Z (BUY only kept)
  gamma      ladder and listing time of every station-day the cluster traded
  paths      CLOB 1-minute prices of every bucket around the station-day's first fill
  f          production listing F (listing_forecast_f.compute) at each first fill, truths cut at date - 2
  rows       first fill + ladder + F + six native ensembles + ECMWF momentum + truth
Usage: keep_f_build_r63.py <bench> [stage ...]   (no stage = all)
"""
import collections
import gzip
import json
import math
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from lib import CITY2ST, parse_bucket, parse_event  # noqa: E402

B = Path(sys.argv[1])
B.mkdir(parents=True, exist_ok=True)
STAGES = sys.argv[2:] or ["fills", "gamma", "paths", "f", "rows"]
SINCE = int(datetime(2026, 9, 21, tzinfo=timezone.utc).timestamp())
LISTED_AFTER = datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp()
WALLETS = [
    "0x8fbd7cf5f806f563080864694415829f7229a959", "0x6fdd2bda5c031d551f588f176535f061a29a0b07",
    "0x5bae7577f470244ce8e678d20693eee2b8ffed25", "0x30200b893530920f253846199a153e6ac725df2b",
    "0x0e0e8174e213ff2c2ccb30c5652a7b7d77c72d75", "0x7076cb961e87bd22199445d02fe22973510cd0d9",
    "0x7d640ebd20cf136bee351100438bba44ca2e07f8", "0xf4d89f7e8c2fc8d2303e0f6b63cdff37fe304c91",
]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
NATIVE = {"ECMWF_IFS_ENS_OPEN_PF50": "ifs", "ECMWF_AIFS_ENS_GLOBAL": "aifs", "DWD_ICON_GLOBAL_EPS": "icon",
          "NOAA_GEFS_05": "gefs", "ECCC_GEM_GLOBAL_ENS": "gem", "NOAA_AIGEFS_025": "aigefs", "NOAA_AIGEFS_OPS_025": "aigefs"}
CLIENT = httpx.Client(timeout=30.0, limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def curl_json(url):
    for i in range(8):
        r = subprocess.run(["curl", "-s", "-m", "40", url], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
        except ValueError:
            time.sleep(2 + i)
            continue
        if isinstance(j, dict) and "Too Many" in str(j.get("error")):
            time.sleep(8 + 8 * i)
            continue
        return j
    return None


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


# ---------------------------------------------------------------- fills
FPATH = B / "fills_r63.json"
if "fills" in STAGES:
    out = []
    for w in WALLETS:
        rows, end = [], None
        while True:
            off, got = 0, []
            while off <= 9500:
                page = curl_json(f"https://data-api.polymarket.com/trades?user={w}&takerOnly=false&limit=500&offset={off}"
                                 + (f"&end={end}" if end else ""))
                if not isinstance(page, list):
                    break
                got += page
                if len(page) < 500 or min(t["timestamp"] for t in page) < SINCE:
                    break
                off += 500
                time.sleep(0.3)
            rows += got
            if not got:
                break
            oldest = min(t["timestamp"] for t in got)
            if oldest < SINCE or len(got) < 500 or (end is not None and oldest >= end):
                break
            end = oldest
        seen = set()
        n0 = len(out)
        for t in rows:
            if t["timestamp"] < SINCE or t.get("side") != "BUY":
                continue
            k = (t["transactionHash"], t["asset"], str(t["price"]), str(t["size"]), t["timestamp"])
            if k in seen:
                continue
            seen.add(k)
            city, ed = parse_event(t.get("eventSlug") or "")
            label = parse_bucket(t.get("title") or "")[0]
            st = CITY2ST.get(city)
            if not st or not ed or not label:
                continue
            out.append({"w": w, "ts": int(t["timestamp"]), "outcome": t["outcome"], "price": float(t["price"]),
                        "size": float(t["size"]), "station": st, "event_date": ed, "bucket": label})
        log(f"{w[:10]}: raw {len(rows)}, kept buys {len(out) - n0}, oldest {min((t['timestamp'] for t in rows), default=None)}")
    out.sort(key=lambda f: (f["ts"], f["w"]))
    json.dump({"pulled_at": int(time.time()), "since": SINCE, "fills": out}, open(FPATH, "w"))
    log(f"fills {len(out)}, newest {out[-1]['ts'] if out else None}")

FILLS = json.load(open(FPATH))
sd = collections.defaultdict(list)
for f in FILLS["fills"]:
    sd[(f["station"], f["event_date"])].append(f)
for v in sd.values():
    v.sort(key=lambda f: (f["ts"], f["w"]))

conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
city = {st: c for c, st in CITY2ST.items()}

# ---------------------------------------------------------------- gamma
GPATH = B / "gamma_r63.json"
gam = json.load(open(GPATH)) if GPATH.exists() else {}
if "gamma" in STAGES:
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
            rows.append({"label": mk["groupItemTitle"], "yes": toks[outs.index("Yes")]})
            if mk.get("acceptingOrdersTimestamp"):
                listed.append(mk["acceptingOrdersTimestamp"])
        return key, {"slug": slug, "buckets": rows, "listed": min(listed) if listed else ev[0].get("startDate")}

    todo = [f"{st}|{ed}" for st, ed in sd if f"{st}|{ed}" not in gam or "error" in gam[f"{st}|{ed}"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for k, v in pool.map(gamma, todo):
            gam[k] = v
    json.dump(gam, open(GPATH, "w"))
    log(f"gamma ok {sum(1 for v in gam.values() if 'error' not in v)} of {len(gam)}")


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
    if not g or "error" in g or not g.get("listed") or ts_of(g["listed"]) < LISTED_AFTER:
        continue
    if fl[0]["outcome"] != "Yes":
        continue
    keep.append((st, ed, fl, g, ts_of(g["listed"])))
log(f"station-days listed after 09-22 00Z whose first fill is a YES: {len(keep)}")

# ---------------------------------------------------------------- paths
PPATH = B / "paths_r63.json"
paths = json.load(open(PPATH)) if PPATH.exists() else {}
if "paths" in STAGES:
    jobs = [(f"{st}|{ed}|{b['label']}", b["yes"], fl[0]["ts"] - 900, fl[0]["ts"] + 60)
            for st, ed, fl, g, lt in keep for b in g["buckets"] if f"{st}|{ed}|{b['label']}" not in paths]

    def path(job):
        k, tok, s, e = job
        j = get_json("https://clob.polymarket.com/prices-history", {"market": tok, "startTs": s, "endTs": e, "fidelity": 1}, tries=3)
        if not j or "history" not in j:
            return k, {"error": "fetch_failed"}
        h = sorted(j["history"], key=lambda x: int(x["t"]))
        return k, {"t": [int(x["t"]) for x in h], "p": [float(x["p"]) for x in h]}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as pool:
        for k, v in pool.map(path, jobs):
            paths[k] = v
    json.dump(paths, open(PPATH, "w"))
    log(f"paths fetched {len(jobs)} in {time.time() - t0:.0f} s; errors {sum(1 for v in paths.values() if 'error' in v)}")


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


# ---------------------------------------------------------------- production F
from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

FFPATH = B / "prod_f_r63.json"
ff = json.load(open(FFPATH)) if FFPATH.exists() else {}
if "f" in STAGES:
    ALL_TRUTHS = F.formal_truths(conn, since=datetime(2026, 6, 1).date(), until=datetime(2026, 12, 31).date())
    ROSTER = global_roster(conn)
    need = collections.defaultdict(list)
    for st, ed, fl, g, lt in keep:
        v = [price_at(f"{st}|{ed}|{b['label']}", fl[0]["ts"] - 60) for b in g["buckets"]]
        if all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4:
            continue  # formed market: not in the sample, F not needed
        if f"{st}|{ed}" not in ff:
            need[fl[0]["ts"] // 600].append((st, ed, fl[0]["ts"]))
    t0 = time.time()
    deadline = t0 + float(__import__("os").environ.get("R63_F_BUDGET_SEC", "540"))
    done = 0
    for key, items in sorted(need.items()):
        if time.time() > deadline:
            break
        t = max(ts for _s, _e, ts in items)
        now = datetime.fromtimestamp(t, timezone.utc)
        cut = (now.date() - timedelta(days=2)).isoformat()
        F.formal_truths = lambda c, since, until, cut=cut: {k: v for k, v in ALL_TRUTHS.items()
                                                             if since.isoformat() <= k[1] <= min(until.isoformat(), cut)}
        payload = F.compute(conn, now=now, roster=ROSTER)
        for st, ed, _ts in items:
            e = payload["station_days"].get(f"{st}|{ed}")
            ff[f"{st}|{ed}"] = {k: v for k, v in (e or {"state": "ABSENT"}).items() if k != "v3"} | {"at": t}
        done += 1
        if done % 10 == 0:
            json.dump(ff, open(FFPATH, "w"))
    json.dump(ff, open(FFPATH, "w"))
    left = sum(len(v) for v in need.values()) - sum(1 for v in need.values() for st, ed, _t in v if f"{st}|{ed}" in ff)
    log(f"production F: {done} instants in {time.time() - t0:.0f} s; station-days with F {len(ff)}, still to do {left}")
    if left:
        sys.exit(3)

# ---------------------------------------------------------------- rows
if "rows" in STAGES:
    from bayes_basket_target import direction_signal as ds  # noqa: E402
    truth = {}
    for st, ed, b in conn.execute(
            "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
            " WHERE event_date>='2026-09-20' AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
            " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
        truth[(st, ed)] = b

    def iso(ts):
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def natives(st, ed, at, bnd):
        runs = collections.defaultdict(lambda: collections.defaultdict(list))
        for sysid, run, had, v in conn.execute(
                "SELECT ensemble_system_id, native_run_init_utc, archive_query_at_utc, raw_tmax_c FROM calibration_native_ensemble_historical_member_run"
                " WHERE station_id=? AND target_local_date=? AND qc_status='ACCEPTED' AND native_run_init_utc IS NOT NULL AND raw_tmax_c IS NOT NULL",
                (st, ed)):
            if sysid in NATIVE and str(had) <= at:
                runs[NATIVE[sysid]][str(run)].append(float(v))
        out = {}
        for fam, rr in runs.items():
            ok = {r: v for r, v in rr.items() if len(v) >= 10}
            if ok:
                run = max(ok)
                vals = ok[run]
                out[fam] = {"run": run, "mean": statistics.mean(vals),
                            "probs": {lab: sum(1 for x in vals if lo <= x < hi) / len(vals) for lab, (lo, hi) in bnd.items()}}
        return out

    rows = []
    for st, ed, fl, g, lt in keep:
        e = ff.get(f"{st}|{ed}")
        if not e or e.get("state") != "READY":
            continue
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
        f0 = fl[0]
        ts = f0["ts"]
        now = {lab: price_at(f"{st}|{ed}|{lab}", ts - 60) for lab in labels}
        v = list(now.values())
        formed = bool(v) and all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4
        mom = ds.momentum(conn, station_id=st, event_date=ed, at_utc=ts)
        rows.append({"st": st, "ed": ed, "ts": ts, "listed": lt, "unit": lad[0][1], "width": 1.0 if lad[0][1] == "C" else 10.0 / 9.0,
                     "labels": labels, "bounds": {r[2]: [r[0][0] if math.isfinite(r[0][0]) else None, r[0][1] if math.isfinite(r[0][1]) else None] for r in lad},
                     "first": {"bucket": f0["bucket"], "price": f0["price"], "w": f0["w"][:10]}, "formed": formed,
                     "f": e["f_c"], "f_systems": {k: v["tmax_c"] + v.get("bias_c", 0.0) for k, v in e["systems"].items()},
                     "raw": natives(st, ed, iso(ts), {r[2]: r[0] for r in lad}),
                     "M": mom["M"], "truth": truth.get((st, ed))})
    json.dump(rows, gzip.open(B / "rows_r63.json.gz", "wt"))
    pre = [r for r in rows if not r["formed"]]
    log(f"rows {len(rows)}; pre-formation {len(pre)}; with >=2 ensembles {sum(1 for r in pre if len(r['raw']) >= 2)};"
        f" settled {sum(1 for r in pre if r['truth'])}; event days {min(r['ed'] for r in rows)}..{max(r['ed'] for r in rows)}")
