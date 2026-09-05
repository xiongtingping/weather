"""Is the market's move a signal for the probability layer, before any seat is taken?

Walks every station-day that has settled truth, samples the belief at fixed
clock slots, and for the max-mass contiguous three-bucket window of each
sampled checkpoint records the window's mass then and H hours earlier (Δp),
the window's best-ask sum on the books production held at those two clocks
(Δq), and whether the truth landed in the window.  No ledger rule is
re-implemented: the window is a plain max-mass three-run, not the seat rule,
and the question is only whether (Δp, Δq) carries information about the
outcome beyond the window's own claimed mass.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _sign(v, eps):
    return "up" if v > eps else ("down" if v < -eps else "flat")


def _best_ask(buckets_json):
    out = {}
    for e in json.loads(buckets_json):
        lv = e.get("ask_levels") or []
        out[str(e.get("bucket"))] = float(lv[0]["price"]) if lv else None
    return out


def _max_mass_run(buckets, width=3):
    # buckets: list of (ordinal, name, lower, upper, prob) sorted by ordinal
    best, best_i = -1.0, 0
    for i in range(0, len(buckets) - width + 1):
        m = sum(b[4] for b in buckets[i:i + width])
        if m > best:
            best, best_i = m, i
    return buckets[best_i:best_i + width]


def _contains(run, t):
    lo = run[0][2]
    hi = run[-1][3]
    return (lo is None or t >= lo) and (hi is None or t < hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--horizon", type=float, default=6.0)
    ap.add_argument("--slot-hours", type=float, default=3.0)
    ap.add_argument("--eps", type=float, default=0.02)
    ap.add_argument("--out")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")

    truth = {}
    for st, ld, t, rev in conn.execute(
        "SELECT station_id, local_date, official_tmax_c, revision_no"
        " FROM calibration_station_daily_high_truth"
        " WHERE official_tmax_c IS NOT NULL ORDER BY revision_no"
    ):
        truth[(st, ld)] = float(t)  # later revision wins
    print(f"truth station-days: {len(truth)}")

    rows = []
    for (st, ed), t_true in sorted(truth.items()):
        cps = conn.execute(
            "SELECT history_id, stage, decision_at_utc FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY decision_at_utc",
            (st, ed),
        ).fetchall()
        if len(cps) < 2:
            continue
        books = conn.execute(
            "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY observed_at",
            (st, ed),
        ).fetchall()
        if not books:
            continue
        book_times = [b[0] for b in books]

        def book_at_or_after(clock):
            import bisect
            i = bisect.bisect_left(book_times, clock)
            return books[i] if i < len(books) else None

        def checkpoint_at_or_before(clock):
            import bisect
            times = [c[2] for c in cps]
            i = bisect.bisect_right(times, clock) - 1
            return cps[i] if i >= 0 else None

        t0 = _iso(cps[0][2])
        t_end = _iso(cps[-1][2])
        slot = t0 + timedelta(hours=args.horizon)
        cache = {}

        def probs(history_id):
            if history_id not in cache:
                cache[history_id] = sorted(
                    conn.execute(
                        "SELECT ordinal, bucket, lower_c, upper_c, probability"
                        " FROM basket_probability_bridge_bucket_probability WHERE history_id=?",
                        (history_id,),
                    ).fetchall()
                )
            return cache[history_id]

        while slot <= t_end:
            clock = slot.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            now_cp = checkpoint_at_or_before(clock)
            prev_cp = checkpoint_at_or_before((slot - timedelta(hours=args.horizon)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            slot += timedelta(hours=args.slot_hours)
            if now_cp is None or prev_cp is None or now_cp[0] == prev_cp[0]:
                continue
            now_b = book_at_or_after(now_cp[2])
            prev_b = book_at_or_after(prev_cp[2])
            if now_b is None or prev_b is None or now_b[0] == prev_b[0]:
                continue
            pn = probs(now_cp[0])
            pp = {b[1]: b[4] for b in probs(prev_cp[0])}
            if not pn or not pp:
                continue
            run = _max_mass_run(pn)
            names = [b[1] for b in run]
            p_now = sum(b[4] for b in run)
            if any(n not in pp for n in names):
                continue
            p_prev = sum(pp[n] for n in names)
            qa_now, qa_prev = _best_ask(now_b[1]), _best_ask(prev_b[1])
            if any(qa_now.get(n) is None or qa_prev.get(n) is None for n in names):
                continue
            q_now = sum(qa_now[n] for n in names)
            q_prev = sum(qa_prev[n] for n in names)
            rows.append({
                "station_id": st, "event_date": ed, "clock": clock, "stage": now_cp[1],
                "prev_stage": prev_cp[1], "window": names, "p_now": p_now, "p_prev": p_prev,
                "q_now": q_now, "q_prev": q_prev, "dp": p_now - p_prev, "dq": q_now - q_prev,
                "hit": int(_contains(run, t_true)),
                "edge_now": p_now - q_now,
            })
    print(f"sampled checkpoints: {len(rows)} over {len({(r['station_id'], r['event_date']) for r in rows})} station-days")

    def cell(rs):
        n = len(rs)
        if not n:
            return None
        hits = sum(r["hit"] for r in rs)
        claimed = sum(r["p_now"] for r in rs)
        # honest unit return if one bought the whole window at best ask
        unit = sum(((1.0 / r["q_now"] - 1.0) if r["hit"] else -1.0) for r in rs if r["q_now"] > 0) / n
        return dict(n=n, hit=100 * hits / n, claimed=100 * claimed / n, ratio=hits / claimed if claimed else None,
                    unit=100 * unit, stations=len({r["station_id"] for r in rs}))

    def grid(title, rs):
        print(f"\n## {title} (n={len(rs)})")
        print("| Δp \\ Δq | down | flat | up |\n|---|---|---|---|")
        for dp in ("up", "flat", "down"):
            line = [f"**{dp}**"]
            for dq in ("down", "flat", "up"):
                c = cell([r for r in rs if _sign(r["dp"], args.eps) == dp and _sign(r["dq"], args.eps) == dq])
                line.append("—" if not c else f"n={c['n']} 命中{c['hit']:.0f}% 声称{c['claimed']:.0f}% 比{c['ratio']:.2f} 单位{c['unit']:+.0f}% 站{c['stations']}")
            print("| " + " | ".join(line) + " |")

    def split(title, rs, keyfn):
        print(f"\n## {title}")
        print("| 组 | n | 命中 | 声称 | 实现/声称 | 单位收益(全窗按卖一买) | 站数 |\n|---|---|---|---|---|---|---|")
        g = defaultdict(list)
        for r in rs:
            g[keyfn(r)].append(r)
        for k in sorted(g, key=str):
            c = cell(g[k])
            print(f"| {k} | {c['n']} | {c['hit']:.1f}% | {c['claimed']:.1f}% | {c['ratio']:.2f} | {c['unit']:+.1f}% | {c['stations']} |")

    print(f"\n# 概率层信息量：视野 {args.horizon:g}h，槽 {args.slot_hours:g}h，ε={args.eps}")
    grid("全部（最大质量三桶窗）", rows)
    for stg in ("P0", "P0-M", "P1"):
        sub = [r for r in rows if r["stage"] == stg]
        if sub:
            grid(stg, sub)
    split("市场是否向模型靠拢", rows, lambda r: (r["dq"] * (r["p_prev"] - r["q_prev"])) > 0)
    split("按 edge_now 分档 × Δq 方向", rows,
          lambda r: ("edge<0" if r["edge_now"] < 0 else "edge0-0.1" if r["edge_now"] < 0.1 else "edge>=0.1") + " / dq " + _sign(r["dq"], args.eps))
    split("按声称分档 × Δq 方向", rows,
          lambda r: ("p<0.5" if r["p_now"] < 0.5 else "p0.5-0.7" if r["p_now"] < 0.7 else "p>=0.7") + " / dq " + _sign(r["dq"], args.eps))
    split("按 |Δq| 幅度（仅 Δp up）", [r for r in rows if _sign(r["dp"], args.eps) == "up"],
          lambda r: ("|dq|<0.05" if abs(r["dq"]) < 0.05 else "|dq|0.05-0.15" if abs(r["dq"]) < 0.15 else "|dq|>=0.15") + " / " + _sign(r["dq"], args.eps))
    if args.out:
        json.dump(rows, open(args.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
