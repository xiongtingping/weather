"""Round 25 (熊先生 10-10 「继续打目标账号的选桶（要100%复刻），尝试用原始模型、
模型众数、均值、市场众数、均值等」): one row per candidate leg (bucket x YES/NO) of
every first-taker event, with every centre we can state at the fill's instant.

Inputs: round 14's choice_rows.jsonl.gz (the ladder at the fill, our served p,
yesterday's truth, the running high), the cluster's fills (bench
cluster_events.json, for what the cluster had already bought on the
station-day), and the SSOT read-only: the six native ensembles' raw members
(each system's newest run in hand at the fill -- earliest archive_query_at_utc
<= fill, at least 10 members), the deterministic point runs (knowable_at <=
fill), ECMWF momentum (direction_signal.momentum).  Bias for a raw centre is
the station x system mean of (truth - centre) over the station's OTHER event
dates in this set (leave one date out).

Writes <bench>/replicate_rows.json.gz.
"""
import collections
import gzip
import json
import math
import re
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import run_change  # noqa: E402

B = Path(sys.argv[1])
HERE = Path(__file__).resolve().parents[1]
rows = [json.loads(line) for line in gzip.open(HERE / "choice_rows.jsonl.gz", "rt")]
conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
conn.execute("PRAGMA query_only=1")
NATIVE = ("ECMWF_IFS_ENS_OPEN_PF50", "ECMWF_AIFS_ENS_GLOBAL", "DWD_ICON_GLOBAL_EPS", "NOAA_GEFS_05",
          "ECCC_GEM_GLOBAL_ENS", "NOAA_AIGEFS_025")
SHORT = {"ECMWF_IFS_ENS_OPEN_PF50": "ifs", "ECMWF_AIFS_ENS_GLOBAL": "aifs", "DWD_ICON_GLOBAL_EPS": "icon",
         "NOAA_GEFS_05": "gefs", "ECCC_GEM_GLOBAL_ENS": "gem", "NOAA_AIGEFS_025": "aigefs",
         "ECMWF_IFS_9KM_RELAY_POINT": "hres", "ECMWF_AIFS_SINGLE_POINT": "aifs1", "UKMO_GLOBAL_10KM_RELAY_POINT": "ukmo",
         "CMA_GRAPES_GLOBAL_POINT": "cma"}
POINTS = ("ECMWF_IFS_9KM_RELAY_POINT", "ECMWF_AIFS_SINGLE_POINT", "UKMO_GLOBAL_10KM_RELAY_POINT", "CMA_GRAPES_GLOBAL_POINT")


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def bounds_c(label):
    """(lower, upper) in C of a bucket label; open ends as +-inf."""
    s = label.strip()
    m = re.match(r"^(-?\d+)°([CF]) or (below|higher)$", s)
    if m:
        v, unit, side = float(m.group(1)), m.group(2), m.group(3)
        conv = (lambda x: x) if unit == "C" else (lambda x: (x - 32) * 5 / 9)
        half = 0.5
        return (-math.inf, conv(v + half)) if side == "below" else (conv(v - half), math.inf)
    m = re.match(r"^(-?\d+)-(-?\d+)°F$", s)
    if m:
        lo, hi = float(m.group(1)) - 0.5, float(m.group(2)) + 0.5
        return (lo - 32) * 5 / 9, (hi - 32) * 5 / 9
    m = re.match(r"^(-?\d+)°([CF])$", s)
    if m:
        v, unit = float(m.group(1)), m.group(2)
        lo, hi = v - 0.5, v + 0.5
        return (lo, hi) if unit == "C" else ((lo - 32) * 5 / 9, (hi - 32) * 5 / 9)
    return None


# the cluster's own fills, per station-day
fills = json.load(open(B / "cluster_events.json"))["fills"]
own = collections.defaultdict(list)
for f in fills:
    own[(f["station"], f["event_date"])].append((int(f["ts"]), f["bucket"], f["outcome"], float(f["price"]) * float(f["size"])))
for v in own.values():
    v.sort()

native_cache = {}
point_cache = {}


def natives(st, ed):
    key = (st, ed)
    if key not in native_cache:
        out = collections.defaultdict(lambda: collections.defaultdict(list))
        for sysid, run, had, v in conn.execute(
                "SELECT ensemble_system_id, native_run_init_utc, archive_query_at_utc, raw_tmax_c"
                " FROM calibration_native_ensemble_historical_member_run WHERE station_id=? AND target_local_date=?"
                " AND qc_status='ACCEPTED' AND native_run_init_utc IS NOT NULL AND raw_tmax_c IS NOT NULL",
                (st, ed)):
            if sysid in NATIVE:
                out[sysid][run].append((str(had), float(v)))
        native_cache[key] = out
    return native_cache[key]


def points(st, ed):
    key = (st, ed)
    if key not in point_cache:
        out = collections.defaultdict(list)
        for sysid, run, known, v in conn.execute(
                "SELECT ensemble_system_id, run_init_utc, knowable_at_utc, forecast_tmax_c FROM calibration_regional_point_run"
                " WHERE station_id=? AND target_local_date=? AND remaining_peak_complete=1 AND knowable_at_utc IS NOT NULL",
                (st, ed)):
            if sysid in POINTS and v is not None:
                out[sysid].append((str(known), str(run), float(v)))
        point_cache[key] = out
    return point_cache[key]


events = []
for n, r in enumerate(rows):
    st, ed, ts = r["station"], r["event_date"], int(r["ts"])
    at = iso(ts)
    yes = sorted((a for a in r["alts"] if a["side"] == "Yes"), key=lambda a: a["ord"])
    no = {a["ord"]: a for a in r["alts"] if a["side"] == "No"}
    order = [a["ord"] for a in yes]
    bnd = {a["ord"]: bounds_c(a["bucket"]) for a in yes}
    if any(v is None for v in bnd.values()):
        continue
    rung = 1.0 if r["unit"] == "C" else 10.0 / 9.0
    centres = {}
    for o, (lo, hi) in bnd.items():
        if math.isinf(lo):
            centres[o] = hi - rung / 2
        elif math.isinf(hi):
            centres[o] = lo + rung / 2
        else:
            centres[o] = (lo + hi) / 2
    raw = {}
    nat = natives(st, ed)
    for sysid in NATIVE:
        best = None
        for run, members in nat.get(sysid, {}).items():
            held = [v for had, v in members if had <= at]
            if len(held) >= 10 and (best is None or run > best[0]):
                best = (run, held)
        if best:
            vals = best[1]
            probs = {o: sum(1 for v in vals if bnd[o][0] <= v < bnd[o][1]) / len(vals) for o in order}
            raw[SHORT[sysid]] = {"mean": statistics.mean(vals), "sd": statistics.pstdev(vals), "probs": probs, "run": best[0]}
    pts = {}
    for sysid, lst in points(st, ed).items():
        seen = {}
        for known, run, v in sorted(lst):
            if known <= at:
                seen[run] = v
        if seen:
            pts[SHORT[sysid]] = seen[max(seen)]
    mom = ds.momentum(conn, station_id=st, event_date=ed, at_utc=ts)
    prior = [x for x in own.get((st, ed), []) if x[0] < ts]
    chosen = next(a for a in r["alts"] if a["chosen"])
    events.append({
        "st": st, "ed": ed, "ts": ts, "lead": r["lead_h"], "unit": r["unit"], "stage": r["stage"], "usdc": r["usdc"],
        "rung_c": rung, "order": order, "centres": centres, "labels": {a["ord"]: a["bucket"] for a in yes},
        "q": {a["ord"]: a["q"] for a in yes}, "ask": {a["ord"]: a["price"] for a in yes},
        "no_price": {o: no[o]["price"] for o in order if o in no}, "spread": {a["ord"]: a["spread"] for a in yes},
        "mom1h": {a["ord"]: a.get("mom") for a in yes}, "p": {a["ord"]: a["p"] for a in yes},
        "mode_ord": r["mode_ord"], "our_mode_ord": r["our_mode_ord"], "truth_ord": r["truth_ord"], "truth_c": r["truth_c"],
        "ytruth_c": r.get("ytruth_c"), "tmax_today": r.get("tmax_today"), "cur_obs": r.get("cur_obs"), "is_d0": r.get("is_d0"),
        "raw": raw, "points": pts, "M": mom["M"], "M_parts": {k: v["delta_c"] for k, v in mom["components"].items()},
        "prior": [(t, b, o, u) for t, b, o, u in prior[-200:]],
        "chosen": {"ord": chosen["ord"], "side": chosen["side"]},
        "half": "H1" if ed < "2026-09-30" else "H2",
    })
    if (n + 1) % 300 == 0:
        print(n + 1, "events", file=sys.stderr)

# leave-one-date-out bias per station x centre (truth - centre), over the station's other dates
by_station = collections.defaultdict(list)
for e in events:
    by_station[e["st"]].append(e)
for st, evs in by_station.items():
    keys = set()
    for e in evs:
        keys |= {f"raw_{k}" for k in e["raw"]} | {f"pt_{k}" for k in e["points"]}
    for e in evs:
        e["bias"] = {}
        for key in keys:
            kind, name = key.split("_", 1)
            res = []
            for x in evs:
                if x["ed"] == e["ed"]:
                    continue
                c = (x["raw"].get(name) or {}).get("mean") if kind == "raw" else x["points"].get(name)
                if c is not None and x["truth_c"] is not None:
                    res.append(x["truth_c"] - c)
            e["bias"][key] = statistics.mean(res) if len(res) >= 3 else 0.0
json.dump(events, gzip.open(B / "replicate_rows.json.gz", "wt"))
print("events", len(events), "with raw systems:", collections.Counter(len(e["raw"]) for e in events).most_common(),
      "with points:", collections.Counter(len(e["points"]) for e in events).most_common(), file=sys.stderr)
