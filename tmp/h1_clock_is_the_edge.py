"""The edge ladder IS the clock: how far past the day's physical deadline the
claim is still standing.  Also: what the same rows claim ON the ceiling bucket."""
import json, sqlite3, statistics, sys
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse

rows = json.load(open("tmp/h1_edge_targets_rows.json"))
recs = json.load(open("tmp/h1_night_ceiling_rows.json"))

# when was the day's high actually reached?
arch = sqlite3.connect("file:data/archive.db?mode=ro", uri=True, timeout=120)
peak = {}
for st, ed, mx, va in arch.execute(
    "SELECT station_id,event_date,observed_max_c,max_valid_at"
    " FROM reported_trajectory_canonical_station_day_bounds"):
    peak[(str(st), str(ed))] = (mx, str(va))
print("archive peak rows:", len(peak), flush=True)

ssot = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                       uri=True, timeout=120)
tz = {(str(a), str(b)): str(c) for a, b, c in ssot.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}

for r in rows:
    k = (r["station"], r["date"])
    p = peak.get(k)
    r["h_since_peak"] = ((parse(r["at"]) - parse(p[1])).total_seconds() / 3600.0
                         if p else None)

print("\n=== A. edge bin -> clock ===")
print(f"{'bin':<16}{'n':>4}{'med local h':>13}{'med h since peak':>18}"
      f"{'after peak %':>14}{'hit%':>7}")
for name, sel in (("edge>0.25", [r for r in rows if r["edge"] > .25]),
                  ("0.10-0.25", [r for r in rows if .10 < r["edge"] <= .25]),
                  ("0-0.10", [r for r in rows if 0 < r["edge"] <= .10]),
                  ("edge<=0", [r for r in rows if r["edge"] <= 0])):
    hs = [r["h_since_peak"] for r in sel if r["h_since_peak"] is not None]
    print(f"{name:<16}{len(sel):>4}{statistics.median(r['local_hour'] for r in sel):>13.1f}"
          f"{(statistics.median(hs) if hs else float('nan')):>18.1f}"
          f"{100*sum(1 for h in hs if h>0)/max(len(hs),1):>13.0f}%"
          f"{100*sum(r['won'] for r in sel)/len(sel):>6.1f}%")

print("\n=== B. same 104 targets binned by CLOCK instead of by edge ===")
print(f"{'local hour':<14}{'n':>4}{'mean edge':>11}{'mean claim':>12}"
      f"{'mean ask':>10}{'hit%':>7}{'above ceiling%':>16}")
for lo, hi in ((0, 14), (14, 17), (17, 20), (20, 25)):
    sel = [r for r in rows if lo <= r["local_hour"] < hi]
    if not sel:
        continue
    ac = [r for r in sel if r["ceil_ord"] is not None]
    print(f"{lo}-{hi:<12}{len(sel):>4}{statistics.mean(r['edge'] for r in sel):>11.3f}"
          f"{statistics.mean(r['p_t'] for r in sel):>12.3f}"
          f"{statistics.mean(r['ask'] for r in sel):>10.3f}"
          f"{100*sum(r['won'] for r in sel)/len(sel):>6.1f}%"
          f"{100*sum(r['above_ceiling'] for r in ac)/max(len(ac),1):>15.0f}%")

print("\n=== C. the zero-sum: what the SAME night rows claim ON the ceiling bucket ===")
BINS = [(0, 10), (10, 13), (13, 15), (15, 17), (17, 19), (19, 21), (21, 24)]
print(f"{'local hour':<12}{'n':>4}{'claim on ceil':>15}{'settled==ceil%':>16}"
      f"{'claim above':>13}{'settled above%':>16}")
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
    # claim on ceiling bucket = 1 - mass_above - mass_below(hard-zeroed)
    print(f"{lo}-{hi:<9}{len(sel):>4}"
          f"{statistics.mean(r['claim_ceil'] for r in sel):>15.3f}"
          f"{100*sum(1 for r in sel if r['settled_ord']==r['rh_ord'])/len(sel):>15.1f}%"
          f"{statistics.mean(r['mass_above'] for r in sel):>13.3f}"
          f"{100*sum(r['actual_above'] for r in sel)/len(sel):>15.1f}%")
