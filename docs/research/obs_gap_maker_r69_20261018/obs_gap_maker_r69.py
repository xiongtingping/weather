"""Round 69 (PROTOCOL.md, committed 46c64b87 before scoring): the same-day
observed-minus-forecast gap as a tilt on the market ladder, scored on the
bucket distribution and on a post-only maker ledger.

    python3 -I docs/research/obs_gap_maker_r69_20261018/obs_gap_maker_r69.py > .../obs_gap_maker_r69.out
"""

from __future__ import annotations

import gzip
import json
import math
import random
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
ROWS = ROOT / "docs" / "research" / "d0_obs_model_20261015" / "rows_d0.jsonl.gz"
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
HOURS = range(9, 15)
MIN_TRAIN_DAYS = 10
HORIZON = timedelta(minutes=15)
TICK = 0.01
FLOOR = 1e-3
DRAWS = 2000
SEED = 20261018
SLOTS = {"09–10": (9, 10), "11–12": (11, 12), "13–14": (13, 14)}


def utc(text):
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def z(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def ladder(intervals):
    keys = sorted(intervals, key=lambda b: -1e9 if intervals[b][0] is None else intervals[b][0])
    return {b: i for i, b in enumerate(keys)}


def bucket_of(intervals, value):
    for b, (lo, hi) in intervals.items():
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return b
    return None


def features(r):
    cloud, sun, rain = r.get("cloud_gap"), r.get("sun_pct"), r.get("rain_gap")
    x = [1.0, float(r["t_minus_fc"] or 0.0),
         -float(cloud) if cloud is not None else 0.0, 1.0 if cloud is None else 0.0,
         float(sun) if sun is not None else 0.0, 1.0 if sun is None else 0.0,
         -float(rain) if rain is not None else 0.0, 1.0 if rain is None else 0.0]
    x += [1.0 if r["hour"] == h else 0.0 for h in list(HOURS)[1:]]
    return x


def tilt(q, order, target):
    keys = list(q)
    ks = np.array([order[b] for b in keys], float)
    base = np.array([max(q[b], 1e-9) for b in keys])

    def mean(theta):
        w = base * np.exp(theta * (ks - ks.mean()))
        return float((w * ks).sum() / w.sum()), w
    lo, hi = -25.0, 25.0
    if not mean(lo)[0] <= target <= mean(hi)[0]:
        target = min(max(target, mean(lo)[0]), mean(hi)[0])
    for _ in range(60):
        mid = (lo + hi) / 2
        if mean(mid)[0] < target:
            lo = mid
        else:
            hi = mid
    w = mean((lo + hi) / 2)[1]
    w = w / w.sum()
    return {b: float(v) for b, v in zip(keys, w)}


def floored(v):
    f = {b: max(FLOOR, p) for b, p in v.items()}
    s = sum(f.values())
    return {b: p / s for b, p in f.items()}


def later_books(conn, station, day, at):
    """Best YES ask (min) and bid (max) per bucket over (at, at+15 min]."""

    rows = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at>? AND observed_at<=?", (station, day, z(at), z(at + HORIZON))).fetchall()
    ask, bid = {}, {}
    for (raw,) in rows:
        for b in json.loads(raw or "[]"):
            k = str(b.get("bucket"))
            a = b.get("best_ask") if b.get("ask_available") else None
            d = b.get("bid_price") if b.get("bid_available") else None
            if isinstance(a, (int, float)) and a > 0:
                ask[k] = min(ask.get(k, 9.0), float(a))
            if isinstance(d, (int, float)) and d > 0:
                bid[k] = max(bid.get(k, -9.0), float(d))
    return ask, bid, len(rows)


def ledger(belief, quotes, later, truth_bucket):
    """One share per qualifying side per bucket; returns [(side, price, filled, pnl)]."""

    ask_l, bid_l, _n = later
    out = []
    for b, p in belief.items():
        a, d = (quotes.get(b) or [None, None])
        if a is None:
            continue
        hit = 1.0 if b == truth_bucket else 0.0
        x = (d or 0.0) + TICK
        if x < a - 1e-9 and p > x:
            filled = ask_l.get(b, 9.0) <= x + 1e-9
            out.append(("YES", x, filled, (hit - x) if filled else 0.0))
        y = (1.0 - a) + TICK
        if d is not None and y < 1.0 - d - 1e-9 and (1.0 - p) > y:
            filled = bid_l.get(b, -9.0) >= 1.0 - y - 1e-9
            out.append(("NO", y, filled, ((1.0 - hit) - y) if filled else 0.0))
    return out


def boot(groups, stat, level=0.95):
    days = sorted(groups)
    rng = random.Random(SEED)
    draws = sorted(v for v in (stat([groups[days[rng.randrange(len(days))]] for _ in days]) for _ in range(DRAWS))
                   if v is not None)
    point = stat([groups[d] for d in days])
    return point, (draws[int((1 - level) / 2 * len(draws))], draws[int((1 + level) / 2 * len(draws)) - 1])


def main():
    rows = [json.loads(line) for line in gzip.open(ROWS, "rt")]
    rows = [r for r in rows if r.get("market") and r["hour"] in HOURS and r.get("truth") is not None]
    for r in rows:
        r["order"] = ladder(r["intervals"])
        r["settled"] = bucket_of(r["intervals"], float(r["truth"]))
        q = r["market"]["q"]
        r["eq"] = sum(q[b] * r["order"][b] for b in q if b in r["order"])
        r["y"] = None if r["settled"] is None else r["order"][r["settled"]] - r["eq"]
        r["x"] = features(r)
    rows = [r for r in rows if r["y"] is not None and set(r["market"]["q"]) == set(r["order"])]
    days = sorted({r["day"] for r in rows})
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    scored = []
    for day in days:
        cut = (date.fromisoformat(day) - timedelta(days=2)).isoformat()
        train = [r for r in rows if r["day"] <= cut]
        if len({r["day"] for r in train}) < MIN_TRAIN_DAYS:
            continue
        X = np.array([r["x"] for r in train])
        Y = np.array([r["y"] for r in train])
        beta = np.linalg.solve(X.T @ X + 1e-6 * np.eye(X.shape[1]), X.T @ Y)
        for r in [r for r in rows if r["day"] == day]:
            s_hat = float(np.array(r["x"]) @ beta)
            q = r["market"]["q"]
            later = later_books(conn, r["station"], r["day"], utc(r["at"]))
            arms = {"b0": q, "b1": tilt(q, r["order"], r["eq"] + s_hat), "b05": tilt(q, r["order"], r["eq"] + 0.5 * s_hat)}
            rec = {"day": day, "hour": r["hour"], "station": r["station"],
                   "unit": "F" if "°F" in r["settled"] else "C", "s_hat": s_hat, "books": later[2]}
            for name, v in arms.items():
                f = floored(v)
                rec[f"ls_{name}"] = -math.log(f[r["settled"]])
                # No snapshot in the 15 minutes after: the fill is unobservable
                # (pre-10-07 snapshots were thinned by retention); both arms skip it.
                rec[f"led_{name}"] = ledger(f, r["market"]["quotes"], later, r["settled"]) if later[2] else []
            scored.append(rec)
    sdays = sorted({s["day"] for s in scored})
    print(f"rows {len(rows)}; scored {len(scored)} over {len(sdays)} days {sdays[0]}..{sdays[-1]}; "
          f"median later books per anchor {sorted(s['books'] for s in scored)[len(scored)//2]}; "
          f"anchors with ≥1 later book {sum(1 for s in scored if s['books'])} "
          f"(days {len({s['day'] for s in scored if s['books']})})")
    print(f"Ŝ (rungs): mean {np.mean([s['s_hat'] for s in scored]):+.3f}, sd {np.std([s['s_hat'] for s in scored]):.3f}")

    def report(sub, title):
        by = defaultdict(list)
        for s in sub:
            by[s["day"]].append(s)
        if len(by) < 3:
            print(f"{title}: too few days"); return
        def dls(arm):
            return lambda gs: (lambda xs: sum(xs) / len(xs) if xs else None)(
                [s[f"ls_{arm}"] - s["ls_b0"] for g in gs for s in g])
        def per_share(arm, side=None):
            def f(gs):
                legs = [l for g in gs for s in g for l in s[f"led_{arm}"] if l[2] and (side is None or l[0] == side)]
                return sum(l[3] for l in legs) / len(legs) if legs else None
            return f
        def share_diff(gs):
            a, b = per_share("b1")(gs), per_share("b0")(gs)
            return None if a is None or b is None else a - b
        def total_diff(gs):
            xs = [sum(l[3] for l in s["led_b1"]) - sum(l[3] for l in s["led_b0"]) for g in gs for s in g]
            return sum(xs) / len(xs) if xs else None
        def counts(arm):
            legs = [l for s in sub for l in s[f"led_{arm}"]]
            fl = [l for l in legs if l[2]]
            return len(legs), len(fl), sum(l[3] for l in fl)
        m1, c1 = boot(by, dls("b1"))
        m2, c2 = boot(by, share_diff)
        m3, c3 = boot(by, per_share("b1"))
        m4, c4 = boot(by, total_diff)
        n0, f0, p0 = counts("b0")
        n1, f1, p1 = counts("b1")
        print(f"\n== {title}  (anchors {len(sub)}, days {len(by)})")
        print(f"  ΔLogScore b1−b0 {m1:+.4f} [{c1[0]:+.4f},{c1[1]:+.4f}]")
        print(f"  b0 挂 {n0} 成交 {f0} ({f0/max(n0,1):.0%}) 盈亏 {p0:+.2f} 每股 {p0/max(f0,1):+.4f}")
        print(f"  b1 挂 {n1} 成交 {f1} ({f1/max(n1,1):.0%}) 盈亏 {p1:+.2f} 每股 {m3:+.4f} [{c3[0]:+.4f},{c3[1]:+.4f}]")
        print(f"  每股 b1−b0 {m2:+.4f} [{c2[0]:+.4f},{c2[1]:+.4f}]  每时刻总盈亏 b1−b0 {m4:+.4f} [{c4[0]:+.4f},{c4[1]:+.4f}]")
        for side in ("YES", "NO"):
            ms, cs = boot(by, per_share("b1", side))
            print(f"    b1 {side} 每股 {ms:+.4f} [{cs[0]:+.4f},{cs[1]:+.4f}]" if ms is not None else f"    b1 {side} —")
        m5, c5 = boot(by, dls("b05"))
        print(f"  κ=0.5: ΔLogScore {m5:+.4f} [{c5[0]:+.4f},{c5[1]:+.4f}]")

    report(scored, "主检验：全部 09–14 时")
    for name, (a, b) in SLOTS.items():
        report([s for s in scored if a <= s["hour"] <= b], f"时段 {name}")
    for u in ("C", "F"):
        report([s for s in scored if s["unit"] == u], f"单位 {u}")


if __name__ == "__main__":
    main()
