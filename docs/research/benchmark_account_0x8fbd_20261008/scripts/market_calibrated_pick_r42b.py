"""Round 42b: calibration toward the market instead of the truth.
(1) The cluster's own hits vs F's on its pre-formation first YES, paired, and
on the picks where they differ.  (2) A centre trained (strictly forward, on
every roster station-day) to land on the market's later mode -- the market's
mode 6 h after listing and at 06:00 local on the event day -- from the
bias-corrected members (weights free, multinomial-style via a normal over the
ladder), its single pick vs the cluster's.
"""
import collections
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

here = Path(__file__)
src = here.with_name("calibrated_pick_r42.py").read_text()
ns = {"__name__": "r42", "__file__": str(here.with_name("calibrated_pick_r42.py"))}
exec(compile(src.split("# 2) the cluster's picks")[0], "r42_head", "exec"), ns)
events, bucket, H, truth, row, conn, tz, SYSTEMS = (ns[k] for k in ("events", "bucket", "H", "truth", "row", "conn", "tz", "SYSTEMS"))
centres, arrays, cdf = ns["centres"], ns["arrays"], ns["cdf"]
r40 = ns["ns"]
G = r40["ns"]["G"] if "G" in r40["ns"] else None

# (1) the cluster's hits vs F's --------------------------------------------------
both = [e for e in events if e["truth"] is not None]
ch = sum(e["choice"] == e["truth"] for e in both)
fh = sum(e["fb"] == e["truth"] for e in both)
w = sum(1 for e in both if e["choice"] == e["truth"] and e["fb"] != e["truth"])
l = sum(1 for e in both if e["fb"] == e["truth"] and e["choice"] != e["truth"])
n = len(both)
d, h = (w - l) / n, 1.96 * (w + l) ** 0.5 / n
diff = [e for e in both if e["choice"] != e["fb"]]
print(f"(1) on {n} picks: the cluster hits {ch / n:.1%}, F {fh / n:.1%}; paired {d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]")
print(f"    where they differ ({len(diff)}): cluster right {sum(e['choice'] == e['truth'] for e in diff)}, F right {sum(e['fb'] == e['truth'] for e in diff)}")

# (2) market-calibrated centre ------------------------------------------------------
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")  # noqa: E731


def market_mode(st, d, at_ts):
    rowm = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                        " AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (st, d.isoformat(), iso(at_ts))).fetchone()
    if not rowm:
        return None
    mids = {}
    for b in json.loads(rowm[0]):
        asks = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
        bids = sorted((float(x["price"]) for x in (b.get("bid_levels") or []) if float(x.get("shares", 0)) >= 5), reverse=True)
        if asks and bids and 0 < bids[0] <= asks[0] < 1:
            mids[str(b.get("bucket"))] = (asks[0] + bids[0]) / 2
    tot = sum(mids.values())
    if len(mids) < 3 or not 0.7 <= tot <= 1.4:
        return None
    return max(mids, key=mids.get)


def ladder_of(st, d):
    """Labels and C edges from the registry (lower/upper)."""
    rows_ = conn.execute("SELECT outcome, registry_json, settlement_unit FROM basket_target_market_registry WHERE station_id=? AND event_date=?"
                         " ORDER BY refreshed_at DESC LIMIT 60", (st, d.isoformat())).fetchall()
    seen = {}
    for outcome, rj, unit in rows_:
        if outcome in seen:
            continue
        try:
            r = json.loads(rj)
        except ValueError:
            continue
        # Native-unit integers, inclusive: the bucket is [lower - 0.5, upper + 0.5).
        lo = None if r.get("lower") is None else float(r["lower"]) - 0.5
        hi = None if r.get("upper") is None else float(r["upper"]) + 0.5
        if str(unit).upper().startswith("F"):
            lo = None if lo is None else (lo - 32.0) * 5.0 / 9.0
            hi = None if hi is None else (hi - 32.0) * 5.0 / 9.0
        seen[outcome] = (lo, hi)
    if len(seen) < 3:
        return None
    items = sorted(seen.items(), key=lambda kv: (kv[1][0] if kv[1][0] is not None else -1e9))
    labels = [k for k, _ in items]
    edges = [[v[0], v[1]] for _, v in items]
    widths = [hi - lo for lo, hi in edges if lo is not None and hi is not None]
    if not widths:
        return None
    w_ = statistics.median(widths)
    if edges[0][0] is None:
        edges[0][0] = edges[0][1] - w_
    if edges[-1][1] is None:
        edges[-1][1] = edges[-1][0] + w_
    return labels, edges


targets = {}
for (st, d) in list(H):
    if not (date(2026, 8, 20) <= d <= date(2026, 10, 12)) or st not in tz:
        continue
    lad = ladder_of(st, d)
    if lad is None:
        continue
    labels, edges = lad
    six_local = datetime.combine(d, datetime.min.time(), tzinfo=ZoneInfo(tz[st])).timestamp() + 6 * 3600
    m = market_mode(st, d, six_local)
    if m is None or m not in labels:
        continue
    k = labels.index(m)
    lo, hi = edges[k]
    targets[(st, d)] = ((lo + hi) / 2.0, edges)
print(f"(2) station-days with a later market mode (06:00 local on the day): {len(targets)}")

fits = {}


def fit_market(D):
    if D in fits:
        return fits[D]
    cut = D - timedelta(days=2)
    rows_, ys = [], []
    for (st, d), (y, _edges) in targets.items():
        if d <= cut:
            rows_.append(row(st, d, H[(st, d)], d - timedelta(days=4)))
            ys.append(y)
    if len(rows_) < 200:
        fits[D] = None
        return None
    A = arrays(rows_, ys)
    obj = lambda p, kind: float(np.mean((A["y"] - centres(p, A, kind)) ** 2)) + 1e-3 * float(p @ p)  # noqa: E731
    pF = minimize(obj, np.array([0.0, 0.0, 0.0]), args=("F",), method="L-BFGS-B").x
    pW = minimize(obj, np.r_[np.zeros(len(SYSTEMS)), 0.0, 0.0, 0.0], args=("W",), method="L-BFGS-B").x
    fits[D] = {"F": pF, "W": pW, "n": len(rows_)}
    return fits[D]


stat = collections.defaultdict(collections.Counter)
for ev in events:
    D = date.fromisoformat(ev["ed"])
    m = fit_market(D)
    if m is None:
        continue
    e = r40["prod_f"](ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    members = {s: v["tmax_c"] for s, v in e["systems"].items()}
    r = row(ev["st"], D, members, D - timedelta(days=4))
    A = arrays([r], [0.0])
    for name, val in (("F", r["f"]), ("market-shift F", float(centres(m["F"], A, "F")[0])),
                      ("market-weighted members", float(centres(m["W"], A, "W")[0]))):
        b = bucket(ev["edges"], val)
        stat[name]["n"] += 1
        stat[name]["same"] += b == ev["choice"]
        stat[name]["early_n"] += ev["ts"] - ev["listed"] < 900
        stat[name]["early"] += (ev["ts"] - ev["listed"] < 900) and b == ev["choice"]
        stat[name]["w"] += b == ev["choice"] and bucket(ev["edges"], r["f"]) != ev["choice"]
        stat[name]["l"] += bucket(ev["edges"], r["f"]) == ev["choice"] and b != ev["choice"]
for name, s in stat.items():
    n = max(1, s["n"])
    d_, h_ = (s["w"] - s["l"]) / n, 1.96 * (s["w"] + s["l"]) ** 0.5 / n
    print(f"   {name:26s} n={s['n']:3d} = cluster {s['same'] / n:.1%} ({d_:+.3f} [{d_ - h_:+.3f}, {d_ + h_:+.3f}])"
          f"   early {s['early'] / max(1, s['early_n']):.1%}")
last = fits[max(k for k, v in fits.items() if v)]
print("   latest market fit: shift", np.round(last["F"][0], 2), "weights", np.round(np.exp(last["W"][:len(SYSTEMS)]), 2).tolist(), [s.split("_")[1] for s in SYSTEMS])
