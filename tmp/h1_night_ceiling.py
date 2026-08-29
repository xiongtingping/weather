"""H1: nothing enforces a ceiling above the already-measured running high.

For P1 rows with lead>=0, compute the probability mass strictly ABOVE the
bucket that CONTAINS lineage.metar_trajectory_running_high_c, bin by LOCAL
hour of the decision, and compare claimed mass against the realised rate of
settled_ordinal > running_high_ordinal.
"""
import json, sqlite3, sys, math
from collections import defaultdict
sys.path.insert(0, "tmp")
from stage_pick import DB, load_chains, pick, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120)

# settled station-days that carry a paper position
settle = {}
for st, ed, sb, stmax in conn.execute(
    "SELECT s.station_id,s.event_date,s.settled_bucket,s.settled_tmax_c"
    " FROM paper_trade_settlement s JOIN paper_trade_position p"
    " ON p.position_id=s.position_id"):
    settle[(str(st), str(ed))] = (sb, stmax)
print("settled station-days with a position:", len(settle), flush=True)

keys = sorted(settle)

rows_by_key = defaultdict(list)
tz = {}
for k in keys:
    for st, ed, z, stage, at, env_blob, bi in conn.execute(
        "SELECT station_id,event_date,settlement_timezone,stage,decision_at_utc,"
        "envelope_json,bucket_intervals_json"
        " FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " ORDER BY decision_at_utc", k):
        tz[k] = z
        rows_by_key[k].append((str(stage), str(at), env_blob, bi))
print("chains loaded:", len(rows_by_key), "rows:",
      sum(len(v) for v in rows_by_key.values()), flush=True)


def intervals(env, bi_col):
    lin = env.get("lineage") or {}
    raw = lin.get("bucket_intervals_c_json")
    if raw:
        return json.loads(raw)
    if bi_col:
        return json.loads(bi_col)
    return None


def bucket_of_temp(iv, t):
    """The bucket whose [lo,hi) interval contains t. Production convention:
    lo is exclusive-below/inclusive, hi exclusive; open ends are None."""
    for b, (lo, hi) in iv.items():
        if (lo is None or t > lo) and (hi is None or t <= hi):
            return b
    return None


recs = []
skip = defaultdict(int)
for k, chain in rows_by_key.items():
    z = tz.get(k)
    if not z:
        skip["no_tz"] += 1
        continue
    close = local_close(k[1], z)
    sb, stmax = settle[k]
    for stage, at, blob, bi in chain:
        if stage != "P1":
            continue
        d = parse(at)
        lead = (close - d).total_seconds() / 3600.0
        if lead < 0:
            skip["lead_neg"] += 1
            continue
        env = json.loads(blob)
        lin = env.get("lineage") or {}
        rh = lin.get("metar_trajectory_running_high_c")
        if rh in (None, ""):
            skip["no_running_high"] += 1
            continue
        rh = float(rh)
        iv = intervals(env, bi)
        if not iv:
            skip["no_intervals"] += 1
            continue
        ords = env["bucket_ordinals"]
        rb = bucket_of_temp(iv, rh)
        if rb is None or rb not in ords:
            skip["rh_no_bucket"] += 1
            continue
        if sb not in ords:
            skip["settled_bucket_unknown"] += 1
            continue
        rho = ords[rb]
        probs = env["probabilities"]
        above = sum(p for b, p in probs.items() if ords.get(b, -1) > rho)
        tot = sum(probs.values())
        lh = d.astimezone(close.tzinfo)
        recs.append(dict(
            station=k[0], date=k[1], at=at, lead=lead,
            local_hour=lh.hour + lh.minute / 60.0,
            running_high_c=rh, rh_bucket=rb, rh_ord=rho,
            mass_above=above, mass_total=tot,
            settled_bucket=sb, settled_ord=ords[sb], settled_tmax_c=stmax,
            actual_above=1 if ords[sb] > rho else 0,
            excluded=lin.get("hard_excluded_buckets", ""),
            strength=lin.get("metar_trajectory_applied_strength"),
        ))

print("P1 rows usable:", len(recs), "skips:", dict(skip))
json.dump(recs, open("tmp/h1_night_ceiling_rows.json", "w"))

# --- per-row pooled, binned by local hour ---
BINS = [(0, 10), (10, 13), (13, 15), (15, 17), (17, 19), (19, 21), (21, 24)]
print("\n== ROW-LEVEL (all P1 recomputes, lead>=0) ==")
print(f"{'local hour':>12} {'n':>5} {'claimed>rh':>11} {'actual>rh':>10} {'ratio':>7}")
for lo, hi in BINS:
    sel = [r for r in recs if lo <= r["local_hour"] < hi]
    if not sel:
        continue
    cl = sum(r["mass_above"] for r in sel) / len(sel)
    ac = sum(r["actual_above"] for r in sel) / len(sel)
    print(f"{lo:>5}-{hi:<6} {len(sel):>5} {cl:>11.4f} {ac:>10.4f} {(cl/ac if ac else float('inf')):>7.2f}")

# --- one row per station-day per bin (last, complete prefix) ---
print("\n== STATION-DAY-LEVEL (last complete prefix inside each bin) ==")
print(f"{'local hour':>12} {'n':>5} {'claimed>rh':>11} {'actual>rh':>10} {'ratio':>7}")
for lo, hi in BINS:
    byk = {}
    for r in recs:
        if lo <= r["local_hour"] < hi:
            k = (r["station"], r["date"])
            if k not in byk or r["at"] > byk[k]["at"]:
                byk[k] = r
    sel = list(byk.values())
    if not sel:
        continue
    cl = sum(r["mass_above"] for r in sel) / len(sel)
    ac = sum(r["actual_above"] for r in sel) / len(sel)
    print(f"{lo:>5}-{hi:<6} {len(sel):>5} {cl:>11.4f} {ac:>10.4f} {(cl/ac if ac else float('inf')):>7.2f}")
