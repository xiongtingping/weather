"""Round 38 (熊先生 10-11 「查『挂牌后多久出 P0』」): the chain from Polymarket
listing to our first P0, segment by segment, for event days 2026-10-05..10-13
(charter 263 landed 10-09: listings from 10-09 on, i.e. event days >= 10-11, are
after it).  Listing = gamma's earliest acceptingOrdersTimestamp; registry = first
refreshed_at of the station-day in basket_target_market_registry; capture = the
first official NWP capture attempt for that event date started after the listing
(and its trigger); P0 = first committed P0 decision; frame = first committed WS
snapshot.  Read-only.
"""
import collections
import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

B = Path(sys.argv[1])
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
ts_ = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
hm = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")
G = json.load(open(B / "first_gamma.json"))
GX = B / "r38_gamma.json"
gx = json.load(open(GX)) if GX.exists() else {}
sds = sorted({(st, ed, slug) for st, ed, slug in conn.execute(
    "SELECT station_id, event_date, slug FROM basket_target_market_registry WHERE event_date BETWEEN '2026-10-05' AND '2026-10-13' AND station_id<>'' AND slug<>''")})
c = httpx.Client(timeout=30)
listing = {}
for st, ed, slug in sds:
    k = f"{st}|{ed}"
    if k in listing:
        continue
    g = G.get(k)
    if g and g.get("listed"):
        listing[k] = ts_(g["listed"])
        continue
    if k not in gx:
        for i in range(4):
            try:
                ev = c.get("https://gamma-api.polymarket.com/events", params={"slug": slug}).json()
                ts = [m.get("acceptingOrdersTimestamp") for m in (ev[0].get("markets", []) if ev else []) if m.get("acceptingOrdersTimestamp")]
                gx[k] = min(ts) if ts else None
                break
            except Exception:
                time.sleep(2)
    if gx.get(k):
        listing[k] = ts_(gx[k])
json.dump(gx, open(GX, "w"))
attempts = collections.defaultdict(list)
with open("/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/official_native_capture_attempts.jsonl") as f:
    for line in f:
        try:
            j = json.loads(line)
        except Exception:
            continue
        if j.get("event_date") and j.get("started_at_utc"):
            attempts[j["event_date"]].append((ts_(j["started_at_utc"]), str(j.get("trigger")), str(j.get("state"))))
for v in attempts.values():
    v.sort()
rows = []
for k, lt in listing.items():
    st, ed = k.split("|")
    reg = conn.execute("SELECT min(refreshed_at) FROM basket_target_market_registry WHERE station_id=? AND event_date=?", (st, ed)).fetchone()[0]
    p0 = conn.execute("SELECT min(decision_at_utc) FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND stage='P0' AND state='COMMITTED'", (st, ed)).fetchone()[0]
    fr = conn.execute("SELECT min(observed_at) FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'", (st, ed)).fetchone()[0]
    cap = next(((t, trig, s) for t, trig, s in attempts.get(ed, []) if t >= lt - 60), None)
    rows.append({"st": st, "ed": ed, "lt": lt, "reg": ts_(reg) if reg else None, "p0": ts_(p0) if p0 else None, "fr": ts_(fr) if fr else None,
                 "cap": cap, "batch": datetime.fromtimestamp(lt, timezone.utc).strftime("%H")})


def q(v, p):
    v = sorted(v)
    return v[int(p * (len(v) - 1))] if v else float("nan")


print("minutes from listing (median / p75 / p90 / max), by event day")
print("   event day  n   ->registry            ->first capture        ->first P0              ->first frame")
for ed in sorted({r["ed"] for r in rows}):
    sel = [r for r in rows if r["ed"] == ed]
    cells = []
    for key in ("reg", "cap", "p0", "fr"):
        v = [((r[key][0] if key == "cap" else r[key]) - r["lt"]) / 60 for r in sel if r[key] is not None]
        cells.append(f"{q(v, .5):5.1f}/{q(v, .75):5.1f}/{q(v, .9):5.1f}/{max(v) if v else float('nan'):6.1f}")
    print(f"   {ed}  {len(sel):2d}  " + "   ".join(cells))
print("\nafter charter 263 (event days >= 10-11), by listing batch (UTC hour): minutes listing -> first P0 (median / max), slowest stations")
for b in sorted({r["batch"] for r in rows if r["ed"] >= "2026-10-11"}):
    sel = [r for r in rows if r["ed"] >= "2026-10-11" and r["batch"] == b and r["p0"]]
    if not sel:
        continue
    v = sorted(((r["p0"] - r["lt"]) / 60, r["st"], r["ed"]) for r in sel)
    print(f"   {b}Z batch n={len(sel)}: median {q([x[0] for x in v], .5):.1f}  max {v[-1][0]:.1f}   slowest: " + ", ".join(f"{s}/{e[5:]} {m:.0f}m" for m, s, e in v[-4:]))
print("\nslowest 12 station-days since 10-11 with the segment that took the time")
late = sorted((r for r in rows if r["ed"] >= "2026-10-11" and r["p0"]), key=lambda r: r["p0"] - r["lt"])[-12:]
for r in late:
    cap = r["cap"]
    print(f"   {r['st']} {r['ed']}  listed {hm(r['lt'])}  registry +{(r['reg'] - r['lt']) / 60 if r['reg'] else float('nan'):.1f}m"
          f"  capture +{(cap[0] - r['lt']) / 60 if cap else float('nan'):.1f}m ({cap[1] if cap else '-'} {cap[2] if cap else ''})  P0 +{(r['p0'] - r['lt']) / 60:.1f}m")
missing = [r for r in rows if r["ed"] >= "2026-10-11" and not r["p0"]]
print(f"\nstation-days since 10-11 without any P0 yet: {len(missing)} " + ", ".join(f"{r['st']}/{r['ed'][5:]}" for r in missing[:20]))
