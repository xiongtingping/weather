"""Round 41 (熊先生 10-12 「继续打选桶，和对比的选桶逻辑要匹配，尝试我没提到的维度!」).

Single bucket, the cluster's pre-formation first YES (227 station-days).  New
dimensions, each read only from what was knowable at the cluster's trade,
judged leave-one-event-day-out against always-F (any parameter chosen inside
the training days):
  1 run timing      -- F from the runs knowable 3/6/12/18 h before the trade
                       (and 3 h after: does it hold runs before our stamps?)
  2 batch spillover -- the cluster's offsets from F on stations it already
                       traded earlier in the same listing batch
  3 next-door market-- the formed D+1 market's mean minus our F(D+1) at the
                       same station, carried to D+2 by a share alpha
  4 persistence     -- blend F with the latest settled day's observed maximum
  5 rounding/shift  -- a constant shift delta of F (e.g. truncation)
  6 station offset  -- a per-station shift in C learned from the cluster's own
                       earlier picks there, shrunk by count
  7 segments        -- F vs C stations, minutes after listing, main vs other
                       wallets, tail buckets
  8 quotes          -- among F-1..F+1, the bucket with a visible quote / the
                       cheapest visible quote
"""
import collections
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

here = Path(__file__)
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events, prod_f, bucket, FR_ALL = ns["events"], ns["prod_f"], ns["bucket"], ns["FR_ALL"]
r39 = ns["ns"]
conn, tz, TRUTH = r39["conn"], r39["tz"], r39["ns2"]["TRUTH"]
import bayes_basket_target.listing_forecast_f as mod  # noqa: E402

FR = {(r["st"], r["ed"]): r for r in FR_ALL}
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
days = sorted({ev["ed"] for ev in events})
mid_day = days[len(days) // 2]


def centre(ev, j):
    lo, hi = ev["edges"][j]
    return (lo + hi) / 2.0


def report(name, chooser, fitted=None):
    """chooser(ev, params) -> bucket; ``fitted``: (grid, ...) chosen per fold."""
    picks = {}
    for d in days:
        train = [e for e in events if e["ed"] != d]
        params = None
        if fitted is not None:
            params = max(fitted, key=lambda p: sum(chooser(e, p) == e["choice"] for e in train))
        for e in events:
            if e["ed"] == d:
                picks[id(e)] = chooser(e, params)
    out = []
    for label, sub in (("all", events), ("first half", [e for e in events if e["ed"] < mid_day]),
                       ("second half", [e for e in events if e["ed"] >= mid_day])):
        n = max(1, len(sub))
        base = sum(e["choice"] == e["fb"] for e in sub)
        got = sum(e["choice"] == picks[id(e)] for e in sub)
        w = sum(1 for e in sub if e["choice"] == picks[id(e)] and e["choice"] != e["fb"])
        l = sum(1 for e in sub if e["choice"] == e["fb"] and picks[id(e)] != e["fb"])
        out.append(f"{label} {got / n:.1%} (F {base / n:.1%}, +{w}/-{l})")
    moved = sum(1 for e in events if picks[id(e)] != e["fb"])
    print(f"{name:52s} " + "   ".join(out) + f"   moved {moved}")


print(f"{len(events)} picks over {len(days)} event days; always F = {sum(e['choice'] == e['fb'] for e in events) / len(events):.1%}")

# 1 run timing -------------------------------------------------------------
fasof = {}


def f_from_runs(ev, shift_h):
    key = (id(ev), shift_h)
    if key not in fasof:
        entry = prod_f(ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
        maxima = mod.system_maxima(conn, station_id=ev["st"], event_date=date.fromisoformat(ev["ed"]),
                                   as_of_utc=iso(ev["ts"] + shift_h * 3600), settlement_timezone=tz[ev["st"]])
        if len(maxima) < 3:
            fasof[key] = None
        else:
            raw = [v["tmax_c"] for v in maxima.values()]
            cor = [v["tmax_c"] + entry["systems"].get(s, {}).get("bias_c", 0.0) for s, v in maxima.items()]
            fasof[key] = statistics.median(raw) + entry["fleet_c"] + 0.5 * (statistics.median(cor) - statistics.median(raw))
    return fasof[key]


for h in (-18, -12, -6, -3, 3):
    def ch(ev, p, h=h):
        f = f_from_runs(ev, h)
        return ev["fb"] if f is None else bucket(ev["edges"], f)
    report(f"1 F from runs knowable {h:+d} h from the trade", ch)

# 2 batch spillover ----------------------------------------------------------
def batch_offset(ev):
    prior = [e for e in events if abs(e["listed"] - ev["listed"]) < 1800 and e["ts"] < ev["ts"]]
    if len(prior) < 2:
        return None
    return statistics.mean((centre(e, e["choice"]) - e["f"]) / e["width"] for e in prior)


def ch_batch(ev, a):
    o = batch_offset(ev)
    return ev["fb"] if o is None else bucket(ev["edges"], ev["f"] + a * o * ev["width"])


report("2 batch spillover, share a of earlier offsets", ch_batch, fitted=(0.0, 0.25, 0.5, 0.75, 1.0))

# 3 next-door market (D+1) ---------------------------------------------------
def label_centre(label):
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", label)]
    if not nums:
        return None
    if "°F" in label:
        v = (nums[0] + nums[1]) / 2 if len(nums) >= 2 else (nums[0] - 1.0 if "below" in label else nums[0] + 1.0)
        return (v - 32.0) * 5.0 / 9.0
    v = nums[0]
    return v - 0.5 if "below" in label else (v + 0.5 if "higher" in label else v)


market_cache = {}


def next_door_offset(ev):
    if id(ev) in market_cache:
        return market_cache[id(ev)]
    prev = (date.fromisoformat(ev["ed"]) - timedelta(days=1)).isoformat()
    entry = prod_f(ev["ts"])["station_days"].get(f"{ev['st']}|{prev}")
    row = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                       " AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (ev["st"], prev, iso(ev["ts"]) + "")).fetchone()
    out = None
    if entry and entry.get("state") == "READY" and row:
        probs = {}
        for b in json.loads(row[0]):
            asks = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
            bids = sorted((float(x["price"]) for x in (b.get("bid_levels") or []) if float(x.get("shares", 0)) >= 5), reverse=True)
            if asks and bids and 0 < bids[0] <= asks[0] < 1:
                c = label_centre(str(b.get("bucket")))
                if c is not None:
                    probs[c] = (asks[0] + bids[0]) / 2
        tot = sum(probs.values())
        if len(probs) >= 3 and 0.7 <= tot <= 1.4:
            out = sum(c * p for c, p in probs.items()) / tot - entry["f_c"]
    market_cache[id(ev)] = out
    return out


def ch_market(ev, a):
    o = next_door_offset(ev)
    return ev["fb"] if o is None else bucket(ev["edges"], ev["f"] + a * o)


have = sum(1 for e in events if next_door_offset(e) is not None)
report(f"3 D+1 market mean - F(D+1), share a (have {have})", ch_market, fitted=(0.0, 0.25, 0.5, 0.75, 1.0))

# 4 persistence --------------------------------------------------------------
def latest_obs(ev):
    for back in (3, 4):
        d = date.fromisoformat(ev["ed"]) - timedelta(days=back)
        end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(tz[ev["st"]])).timestamp()
        if end <= ev["ts"] and (ev["st"], d.isoformat()) in TRUTH and TRUTH[(ev["st"], d.isoformat())][1] is not None:
            return TRUTH[(ev["st"], d.isoformat())][1]
    return None


def ch_persist(ev, w):
    y = latest_obs(ev)
    return ev["fb"] if y is None else bucket(ev["edges"], (1 - w) * ev["f"] + w * y)


report("4 blend with latest settled maximum, weight w", ch_persist, fitted=(0.0, 0.1, 0.2, 0.3, 0.5))

# 5 rounding / constant shift -------------------------------------------------
report("5 constant shift delta (bucket widths)", lambda ev, d: bucket(ev["edges"], ev["f"] + d * ev["width"]),
       fitted=tuple(x / 20 for x in range(-10, 11)))
report("5 truncation (floor F to the unit, then bucket)",
       lambda ev, p: bucket(ev["edges"], float(int(ev["f"])) + (0.0 if ev["unit"] == "C" else 0.0)))

# 6 per-station offset learned from the cluster's earlier picks ----------------
def station_offset(ev, k):
    past = [(centre(e, e["choice"]) - e["f"]) / e["width"] for e in events
            if e["st"] == ev["st"] and e["ts"] < ev["listed"]]
    past = [max(-1.5, min(1.5, x)) for x in past]
    if not past:
        return 0.0
    return statistics.mean(past) * len(past) / (len(past) + k)


report("6 per-station offset from its earlier picks, shrink k",
       lambda ev, k: bucket(ev["edges"], ev["f"] + station_offset(ev, k) * ev["width"]), fitted=(1, 2, 4, 8, 1000))

# 7 segments (descriptive) -----------------------------------------------------
seg = collections.defaultdict(collections.Counter)
for e in events:
    mins = (e["ts"] - e["listed"]) / 60
    keys = [f"unit {e['unit']}", "minutes after listing " + ("<15" if mins < 15 else "15-60" if mins < 60 else ">60"),
            "main wallet" if FR[(e['st'], e['ed'])]['first'].get('w') == "0x8fbd7cf5" else "other wallets",
            "F in a tail bucket" if e["fb"] in (0, e["n"] - 1) else "F in an inner bucket"]
    for k in keys:
        seg[k]["n"] += 1
        seg[k]["hit"] += e["choice"] == e["fb"]
for k in sorted(seg):
    print(f"7 segment {k:28s} n={seg[k]['n']:3d}  = F {seg[k]['hit'] / seg[k]['n']:.1%}")

# 8 visible quotes ---------------------------------------------------------------
def ch_quoted(ev, p):
    cands = [j for j in (ev["fb"], ev["fb"] - 1, ev["fb"] + 1) if 0 <= j < ev["n"]]
    quoted = [j for j in cands if ev["now"].get(j) is not None]
    if p == "has quote":
        return ev["fb"] if ev["fb"] in quoted or not quoted else quoted[0]
    if p == "cheapest":
        return min(quoted, key=lambda j: ev["now"][j]) if quoted else ev["fb"]
    return max(quoted, key=lambda j: ev["now"][j]) if quoted else ev["fb"]


for p in ("has quote", "cheapest", "dearest"):
    report(f"8 among F-1..F+1: {p}", lambda ev, q, p=p: ch_quoted(ev, p))
