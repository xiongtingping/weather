"""Is the opening seat too early?  What the P0 window looks like at fixed
hours after a station-day's first belief.

Charter 48 opens P0 at listing: 195 of 203 P0 seats on the 08-15..09-04
book sit within four minutes of the station-day's first checkpoint.  That
leaves the ledger no variation to measure entry timing on, so this reads
the probability layer instead: at listing+H hours, the max-mass three-bucket
window's claim, its best-ask cost, and whether the truth landed in it.  The
window is a plain max-mass run, not the seat rule; the question is whether
the same kind of window is a better or worse buy H hours later, and why.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    EFFECTIVE_EDGE_LAMBDA,
    effective_edge_lambda,
)


def _iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _clock(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _asks(buckets_json):
    out, quoted, overround = {}, 0, 0.0
    for e in json.loads(buckets_json):
        lv = e.get("ask_levels") or []
        if lv:
            out[str(e.get("bucket"))] = float(lv[0]["price"])
            quoted += 1
            overround += float(lv[0]["price"])
    return out, quoted, overround


def _max_run(buckets, width=3):
    best, best_i = -1.0, 0
    for i in range(0, len(buckets) - width + 1):
        m = sum(b[4] for b in buckets[i:i + width])
        if m > best:
            best, best_i = m, i
    return buckets[best_i:best_i + width]


def _contains(run, t):
    lo, hi = run[0][2], run[-1][3]
    return (lo is None or t >= lo) and (hi is None or t < hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--offsets", default="0,1,3,6,12,18,24,36")
    ap.add_argument("--out")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")
    truth = {}
    for st, ld, t in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        " WHERE official_tmax_c IS NOT NULL ORDER BY revision_no"
    ):
        truth[(st, ld)] = float(t)
    offsets = [float(x) for x in args.offsets.split(",")]
    rows = []
    for (st, ed), t_true in sorted(truth.items()):
        cps = conn.execute(
            "SELECT history_id, stage, decision_at_utc FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY decision_at_utc",
            (st, ed),
        ).fetchall()
        books = conn.execute(
            "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY observed_at",
            (st, ed),
        ).fetchall()
        if not cps or not books:
            continue
        cp_times = [c[2] for c in cps]
        bk_times = [b[0] for b in books]
        t0 = _iso(cps[0][2])
        first_non_p0 = next((c[2] for c in cps if c[1] != "P0"), None)
        cache = {}
        for h in offsets:
            clock = _clock(t0 + timedelta(hours=h, seconds=1))
            i = bisect.bisect_right(cp_times, clock) - 1
            if i < 0:
                continue
            cp = cps[i]
            j = bisect.bisect_left(bk_times, cp[2])
            if j >= len(books):
                continue
            book = books[j]
            if _iso(book[0]) - _iso(clock) > timedelta(hours=2):
                continue  # no book near this clock
            if cp[0] not in cache:
                cache[cp[0]] = sorted(conn.execute(
                    "SELECT ordinal, bucket, lower_c, upper_c, probability"
                    " FROM basket_probability_bridge_bucket_probability WHERE history_id=?",
                    (cp[0],),
                ).fetchall())
            pb = cache[cp[0]]
            if not pb:
                continue
            run = _max_run(pb)
            asks, quoted, overround = _asks(book[1])
            names = [b[1] for b in run]
            if any(n not in asks for n in names):
                continue
            p = sum(b[4] for b in run)
            q = sum(asks[n] for n in names)
            if p <= 0 or q <= 0:
                continue
            a = q / p
            lam = effective_edge_lambda(a, EFFECTIVE_EDGE_LAMBDA)
            rows.append({
                "station_id": st, "event_date": ed, "offset_h": h, "stage": cp[1],
                "p0_still_governs": first_non_p0 is None or cp[2] < first_non_p0,
                "hours_since_listing": (_iso(cp[2]) - t0).total_seconds() / 3600,
                "p": p, "q": q, "a": a, "lam": lam,
                "nominal_edge": p - q, "effective_edge": lam * p - q,
                "quoted_buckets": quoted, "buckets": len(pb), "overround": overround,
                "hit": int(_contains(run, t_true)),
            })
    print(f"station-days with truth {len(truth)}; samples {len(rows)}")

    def cell(rs):
        n = len(rs)
        if not n:
            return None
        hits = sum(r["hit"] for r in rs)
        claim = sum(r["p"] for r in rs)
        unit = sum(((1 / r["q"] - 1) if r["hit"] else -1) for r in rs) / n
        pos = [r for r in rs if r["effective_edge"] > 0]
        pos_unit = (sum(((1 / r["q"] - 1) if r["hit"] else -1) for r in pos) / len(pos)) if pos else None
        return dict(
            n=n, sd=len({(r["station_id"], r["event_date"]) for r in rs}),
            hit=100 * hits / n, claimed=100 * claim / n, ratio=hits / claim,
            a=sum(r["a"] for r in rs) / n, q=sum(r["q"] for r in rs) / n,
            unit=100 * unit, pos_share=100 * len(pos) / n,
            pos_unit=None if pos_unit is None else 100 * pos_unit,
            pos_hit=(100 * sum(r["hit"] for r in pos) / len(pos)) if pos else None,
            quoted=sum(r["quoted_buckets"] / r["buckets"] for r in rs) / n * 100,
            overround=sum(r["overround"] for r in rs) / n,
        )

    def table(title, rs, keyfn):
        print(f"\n## {title}")
        print("| 挂牌后 | n | 站日 | 命中 | 声称 | 实现/声称 | 均 a | 均价 q | 全买单位收益 | 正有效边际占比 | 其中命中 | 其中单位收益 | 挂出桶比 | 卖一总和 |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        g = defaultdict(list)
        for r in rs:
            g[keyfn(r)].append(r)
        for k in sorted(g):
            c = cell(g[k])
            if not c:
                continue
            pu = "—" if c["pos_unit"] is None else f"{c['pos_unit']:+.0f}%"
            ph = "—" if c["pos_hit"] is None else f"{c['pos_hit']:.0f}%"
            print(f"| {k} | {c['n']} | {c['sd']} | {c['hit']:.0f}% | {c['claimed']:.0f}% | {c['ratio']:.2f} | {c['a']:.2f} | {c['q']:.2f} | {c['unit']:+.0f}% | {c['pos_share']:.0f}% | {ph} | {pu} | {c['quoted']:.0f}% | {c['overround']:.2f} |")

    table("全部（任何阶段在任）", rows, lambda r: f"{r['offset_h']:>4.0f}h")
    table("只看 P0 仍在任的样本", [r for r in rows if r["p0_still_governs"]], lambda r: f"{r['offset_h']:>4.0f}h")
    if args.out:
        json.dump(rows, open(args.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
