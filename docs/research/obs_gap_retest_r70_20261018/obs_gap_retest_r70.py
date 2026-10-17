"""Round 70 (PROTOCOL.md, committed 5751a257 before scoring): the five
same-day observed-minus-forecast readings retested on one sample, the D0
apparatus (09-01..10-14) joined to the resident D0 lane's as-of history
(10-15 on), up to the latest settled day.

    python3 -I docs/research/obs_gap_retest_r70_20261018/obs_gap_retest_r70.py > .../obs_gap_retest_r70.out
"""

from __future__ import annotations

import glob
import gzip
import importlib.util
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402

_spec = importlib.util.spec_from_file_location("r69", HERE.parent / "obs_gap_maker_r69_20261018" / "obs_gap_maker_r69.py")
r69 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r69)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
HOURS = range(9, 15)
MIN_TRAIN_DAYS = 10
# The day after which each original reading's sample ended (for the 新日子 column).
ORIGINAL_END = {"R1": "2026-10-10", "R2": "2026-10-06", "R3": "2026-10-06", "R4": "2026-10-14",
                "R5": "2026-10-14", "R6": "2026-10-14"}
GROUPS = {"temp": ("t",), "sun": ("sun",), "cloudrain": ("cloud", "rain"), "all": ("t", "sun", "cloud", "rain")}


def utc(text):
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def market(conn, st, day, at):
    lo = at - timedelta(minutes=30)
    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
        (st, day, r69.z(at), r69.z(lo))).fetchone()
    if row is None:
        return None
    buckets = json.loads(row[0] or "[]")
    ref = market_reference_v1(buckets)
    if ref.get("state") != "complete":
        return None
    quotes = {}
    for b in buckets:
        ask = b.get("best_ask") if b.get("ask_available") else None
        bid = b.get("bid_price") if b.get("bid_available") else None
        quotes[str(b.get("bucket"))] = [float(ask) if ask else None, float(bid) if bid else None]
    return {"q": {str(k): float(v) for k, v in ref["vector"].items()}, "quotes": quotes}


def served_p1(conn, st, day, at):
    row = conn.execute(
        "SELECT probabilities_json, bucket_ordinals_json FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' AND +stage='P1' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1",
        (st, day, r69.z(at))).fetchone()
    return (json.loads(row[0]), {str(k): int(v) for k, v in json.loads(row[1]).items()}) if row else (None, None)


def load(conn):
    labels = {(str(k[0]), str(k[1])): str(v["bucket"]) for k, v in knowable_labels(conn).items()}
    rows, tzs = [], {}
    for line in gzip.open(ROOT / "docs" / "research" / "d0_obs_model_20261015" / "rows_d0.jsonl.gz", "rt"):
        r = json.loads(line)
        tzs[r["station"]] = r["tz"]
        if r["hour"] not in HOURS or not r.get("market") or not r.get("p1"):
            continue
        settled = labels.get((r["station"], r["day"]))
        order = r69.ladder(r["intervals"])
        rows.append({"station": r["station"], "day": r["day"], "hour": r["hour"], "at": r["at"], "src": "apparatus",
                     "q": r["market"]["q"], "quotes": r["market"]["quotes"], "p1": r["p1"], "order": order,
                     "settled": settled, "t": r["t_minus_fc"], "sun": r["sun_pct"], "cloud": r["cloud_gap"],
                     "rain": r["rain_gap"]})
    last_app = max(r["day"] for r in rows)
    picked = {}
    for path in sorted(glob.glob(str(ROOT / "data" / "board_readings" / "d0_history" / "*.jsonl"))):
        for line in open(path):
            h = json.loads(line)
            st, day = h["station"], h["event_date"]
            if day <= last_app or st not in tzs or (st, day) not in labels:
                continue
            local = utc(h["as_of_utc"]).astimezone(ZoneInfo(tzs[st]))
            if local.date().isoformat() != day or local.hour not in HOURS or local.minute > 30:
                continue
            key = (st, day, local.hour)
            if key not in picked or h["as_of_utc"] > picked[key]["as_of_utc"]:
                picked[key] = h
    for (st, day, hour), h in sorted(picked.items()):
        at = utc(h["as_of_utc"])
        mk = market(conn, st, day, at)
        p1, order = served_p1(conn, st, day, at)
        if mk is None or p1 is None:
            continue
        x = h["inputs"]
        rows.append({"station": st, "day": day, "hour": hour, "at": r69.z(at), "src": "resident", "q": mk["q"],
                     "quotes": mk["quotes"], "p1": p1, "order": order, "settled": labels[(st, day)],
                     "t": x.get("t_minus_fc"), "sun": x.get("sun_pct"), "cloud": x.get("cloud_gap"),
                     "rain": x.get("rain_gap")})
    out = []
    for r in rows:
        keys = set(r["order"])
        if not r["settled"] or r["settled"] not in keys or set(r["q"]) != keys or set(r["p1"]) != keys:
            continue
        r["eq"] = sum(r["q"][b] * r["order"][b] for b in keys)
        tot = sum(r["p1"].values())
        r["p1"] = {b: v / tot for b, v in r["p1"].items()}
        r["ep"] = sum(r["p1"][b] * r["order"][b] for b in keys)
        r["k"] = r["order"][r["settled"]]
        r["unit"] = "F" if "°F" in r["settled"] else "C"
        out.append(r)
    return out


def x_of(r, group):
    x = [1.0]
    for name in GROUPS[group]:
        v = r[name]
        sign = -1.0 if name in ("cloud", "rain") else 1.0
        x += [sign * float(v) if v is not None else 0.0, 1.0 if v is None else 0.0]
    x += [1.0 if r["hour"] == h else 0.0 for h in list(HOURS)[1:]]
    return x


def fit(train, group, base):
    X = np.array([x_of(r, group) for r in train])
    Y = np.array([r["k"] - r[base] for r in train])
    return np.linalg.solve(X.T @ X + 1e-6 * np.eye(X.shape[1]), X.T @ Y)


def taker(belief, quotes, settled):
    legs = []
    for b, p in belief.items():
        a, d = quotes.get(b) or [None, None]
        hit = 1.0 if b == settled else 0.0
        if a is not None and p > a:
            legs.append(hit - a)
        if d is not None and p < d:
            legs.append((1.0 - hit) - (1.0 - d))
    return legs


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    rows = load(conn)
    days = sorted({r["day"] for r in rows})
    print(f"rows {len(rows)} (apparatus {sum(r['src']=='apparatus' for r in rows)}, resident "
          f"{sum(r['src']=='resident' for r in rows)}); days {days[0]}..{days[-1]} ({len(days)})")
    recs = []
    for day in days:
        cut = (date.fromisoformat(day) - timedelta(days=2)).isoformat()
        train = [r for r in rows if r["day"] <= cut]
        if len({r["day"] for r in train}) < MIN_TRAIN_DAYS:
            continue
        betas = {("q", "all"): fit(train, "all", "eq")}
        for g in GROUPS:
            betas[("p", g)] = fit(train, g, "ep")
        for r in [r for r in rows if r["day"] == day]:
            rec = {"day": day, "hour": r["hour"], "unit": r["unit"]}
            s_q = float(np.array(x_of(r, "all")) @ betas[("q", "all")])
            b1 = r69.floored(r69.tilt(r["q"], r["order"], r["eq"] + s_q))
            mids = {b: (a + d) / 2 for b, (a, d) in r["quotes"].items() if a is not None and d is not None}
            gap = max(((abs(b1[b] - m), b1[b] - m) for b, m in mids.items()), default=(None, None))[1]
            rec["edge"] = gap
            rec["R1"] = taker(b1, r["quotes"], r["settled"])
            p1 = r69.floored(r["p1"])
            rec["ls_p1"] = -math.log(p1[r["settled"]])
            for g in GROUPS:
                s_p = float(np.array(x_of(r, g)) @ betas[("p", g)])
                pt = r69.floored(r69.tilt(r["p1"], r["order"], r["ep"] + s_p))
                rec[f"ls_{g}"] = -math.log(pt[r["settled"]])
                if g == "all":
                    rec["R5_tilt"] = taker(pt, r["quotes"], r["settled"])
            rec["R5_p1"] = taker(p1, r["quotes"], r["settled"])
            later = r69.later_books(conn, r["station"], r["day"], utc(r["at"]))
            q = r69.floored(r["q"])
            rec["R6_b0"] = r69.ledger(q, r["quotes"], later, r["settled"]) if later[2] else []
            rec["R6_b1"] = r69.ledger(b1, r["quotes"], later, r["settled"]) if later[2] else []
            recs.append(rec)
    sdays = sorted({x["day"] for x in recs})
    print(f"scored {len(recs)} anchors over {len(sdays)} days {sdays[0]}..{sdays[-1]}")

    def by_day(sub):
        g = defaultdict(list)
        for x in sub:
            g[x["day"]].append(x)
        return g

    def mean_of(fn):
        def f(gs):
            xs = [v for g in gs for x in g for v in fn(x)]
            return sum(xs) / len(xs) if xs else None
        return f

    def show(label, sub, stat, n_fn):
        g = by_day(sub)
        if len(g) < 3:
            return f"{label}: {len(g)} 日，不足"
        m, ci = r69.boot(g, stat)
        return f"{label}: {m:+.4f} [{ci[0]:+.4f},{ci[1]:+.4f}]  n={n_fn(sub)} 日={len(g)}"

    def per(sub, after):
        return [x for x in sub if x["day"] > after]

    tests = [
        ("R1 吃单（市场梯子按实况差倾斜，每股）", "R1", mean_of(lambda x: x["R1"]), lambda s: sum(len(x["R1"]) for x in s)),
        ("R2 报文气温差 作 P1 似然 ΔLogScore", "R2", mean_of(lambda x: [x["ls_temp"] - x["ls_p1"]]), len),
        ("R3 卫星日照 作 P1 似然 ΔLogScore", "R3", mean_of(lambda x: [x["ls_sun"] - x["ls_p1"]]), len),
        ("R4 卫星云＋GSMaP 雨 作 P1 似然 ΔLogScore", "R4", mean_of(lambda x: [x["ls_cloudrain"] - x["ls_p1"]]), len),
        ("·  四项合并 作 P1 似然 ΔLogScore（描述）", "R4", mean_of(lambda x: [x["ls_all"] - x["ls_p1"]]), len),
    ]
    print("\n== 主检验（95%，按事件日整块自助）")
    for label, key, stat, n_fn in tests:
        print(show(label, recs, stat, n_fn))
        print("   " + show(f"新日子（> {ORIGINAL_END[key]}）", per(recs, ORIGINAL_END[key]), stat, n_fn))
    edges = sorted(x["edge"] for x in recs if x["edge"] is not None)
    print(f"R1 单桶优势（b₁−中价，偏离最大那一桶）：中位 |·| {statistics.median(abs(e) for e in edges):.4f}，"
          f"p90 {sorted(abs(e) for e in edges)[int(0.9*len(edges))]:.4f}")

    def diff_share(a, b):
        def f(gs):
            xa = [v for g in gs for x in g for v in x[a]]
            xb = [v for g in gs for x in g for v in x[b]]
            return None if not xa or not xb else sum(xa) / len(xa) - sum(xb) / len(xb)
        return f

    print(show("R5 吃单 P1′ 每股", recs, mean_of(lambda x: x["R5_tilt"]), lambda s: sum(len(x["R5_tilt"]) for x in s)))
    print(show("   服务 P1 自身 每股", recs, mean_of(lambda x: x["R5_p1"]), lambda s: sum(len(x["R5_p1"]) for x in s)))
    print(show("   P1′ − P1 每股", recs, diff_share("R5_tilt", "R5_p1"), lambda s: len(s)))
    print("   " + show(f"新日子 P1′ − P1（> {ORIGINAL_END['R5']}）", per(recs, ORIGINAL_END["R5"]), diff_share("R5_tilt", "R5_p1"), len))

    def filled(key):
        return lambda x: [l[3] for l in x[key] if l[2]]
    print(show("R6 挂单 b₁ 每股", recs, mean_of(filled("R6_b1")), lambda s: sum(len(filled("R6_b1")(x)) for x in s)))
    print(show("   市场 b₀ 每股", recs, mean_of(filled("R6_b0")), lambda s: sum(len(filled("R6_b0")(x)) for x in s)))

    def diff_fill(gs):
        a = [v for g in gs for x in g for v in filled("R6_b1")(x)]
        b = [v for g in gs for x in g for v in filled("R6_b0")(x)]
        return None if not a or not b else sum(a) / len(a) - sum(b) / len(b)
    print(show("   b₁ − b₀ 每股", recs, diff_fill, len))
    print("   " + show(f"新日子 b₁ 每股（> {ORIGINAL_END['R6']}）", per(recs, ORIGINAL_END["R6"]), mean_of(filled("R6_b1")), len))


if __name__ == "__main__":
    main()
