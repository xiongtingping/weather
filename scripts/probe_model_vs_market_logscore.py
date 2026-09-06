"""Step one before any rule: does the model carry information the market lacks?

For every settled station-day, sample the served probability vector at fixed
lead-time bins (hours before the local midnight that closes the event day),
pair each sample with the FIRST order book observed at or after the decision
clock (honest pairing: the market has seen at least what the model saw), turn
the book into a market-implied distribution, and score both against the
bucket the truth settled in with the log score.  The difference, in nats per
event, is the model's information edge over the market.  No seat rule, no
window, no stake: the question is only whether the model is closer to the
truth than the price is.

Both distributions are clipped at the same floor and renormalised before
scoring, so a zero on the winning bucket costs the same on either side.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LEAD_BINS = (
    (36.0, math.inf, ">=36h"),
    (30.0, 36.0, "30-36h"),
    (24.0, 30.0, "24-30h"),
    (18.0, 24.0, "18-24h"),
    (15.0, 18.0, "15-18h"),
    (12.0, 15.0, "12-15h"),
    (10.0, 12.0, "10-12h"),
    (8.0, 10.0, "8-10h"),
    (6.0, 8.0, "6-8h"),
    (4.0, 6.0, "4-6h"),
    (2.0, 4.0, "2-4h"),
    (0.0, 2.0, "0-2h"),
)
STAGES = ("P0", "P0-M", "P1")


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _lead_bin(hours: float) -> str | None:
    for lo, hi, label in LEAD_BINS:
        if lo <= hours < hi:
            return label
    return None


def _clip_normalise(vec: dict[str, float], floor: float) -> dict[str, float]:
    clipped = {k: max(float(v), floor) for k, v in vec.items()}
    total = sum(clipped.values())
    return {k: v / total for k, v in clipped.items()}


def _market_vectors(buckets_json: str) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Per bucket: (ask-implied, mid-implied); None when the side is absent."""
    ask, mid = {}, {}
    for entry in json.loads(buckets_json):
        name = str(entry.get("bucket"))
        levels = entry.get("ask_levels") or []
        best_ask = float(levels[0]["price"]) if levels else None
        bid_levels = entry.get("bid_levels") or []
        best_bid = float(bid_levels[0]["price"]) if bid_levels else None
        if best_bid is None and float(entry.get("bid_price") or 0.0) > 0.0:
            best_bid = float(entry["bid_price"])
        ask[name] = best_ask
        if best_ask is not None and best_bid is not None:
            mid[name] = (best_ask + best_bid) / 2.0
        elif best_ask is not None:
            mid[name] = best_ask
        elif best_bid is not None:
            mid[name] = best_bid
        else:
            mid[name] = None
    return ask, mid


def _winner(intervals: list[tuple[str, float | None, float | None]], truth: float) -> str | None:
    for name, lo, hi in intervals:
        if (lo is None or truth >= lo) and (hi is None or truth < hi):
            return name
    return None


def _summarise(samples: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        groups[s[key]].append(s)
    out = {}
    for g, rows in groups.items():
        model = [r["ls_model"] for r in rows]
        market = [r["ls_market"] for r in rows]
        diff = [m - k for m, k in zip(model, market)]
        wins = sum(1 for d in diff if d > 0)
        out[g] = {
            "n": len(rows),
            "station_days": len({(r["station"], r["event_date"]) for r in rows}),
            "model_logscore": statistics.fmean(model),
            "market_logscore": statistics.fmean(market),
            "diff_nats": statistics.fmean(diff),
            "diff_median": statistics.median(diff),
            "model_better_share": wins / len(rows),
            "model_hit": statistics.fmean(r["model_hit"] for r in rows),
            "market_hit": statistics.fmean(r["market_hit"] for r in rows),
            "model_zero_winner": sum(1 for r in rows if r["model_zero_winner"]),
            "market_unquoted_winner": sum(1 for r in rows if r["market_unquoted_winner"]),
        }
    return out


def _bootstrap_ci(samples: list[dict], reps: int, seed: int) -> tuple[float, float]:
    """Block bootstrap over station-days of the mean model-minus-market diff."""
    blocks: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s in samples:
        blocks[(s["station"], s["event_date"])].append(s["ls_model"] - s["ls_market"])
    keys = list(blocks)
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        picked = [keys[rng.randrange(len(keys))] for _ in keys]
        vals = [v for k in picked for v in blocks[k]]
        means.append(statistics.fmean(vals))
    means.sort()
    return means[int(0.025 * reps)], means[int(0.975 * reps)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--floor", type=float, default=0.005)
    ap.add_argument("--book-tolerance-minutes", type=float, default=90.0)
    ap.add_argument("--market-side", choices=("ask", "mid"), default="mid")
    ap.add_argument("--pairing", choices=("after", "before"), default="after")
    ap.add_argument("--since", default="")
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--out")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")

    truth: dict[tuple[str, str], float] = {}
    for st, ld, t in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth"
        " WHERE qc_status='ACCEPTED' ORDER BY revision_no"
    ):
        truth[(st, ld)] = float(t)
    print(f"truth station-days: {len(truth)}")

    tolerance = timedelta(minutes=args.book_tolerance_minutes)
    samples: list[dict] = []
    skipped = defaultdict(int)
    for (st, ed), t_true in sorted(truth.items()):
        if args.since and ed < args.since:
            continue
        cps = conn.execute(
            "SELECT history_id, stage, decision_at_utc, settlement_timezone"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " ORDER BY decision_at_utc",
            (st, ed),
        ).fetchall()
        if not cps:
            skipped["no_checkpoints"] += 1
            continue
        tz = ZoneInfo(cps[0][3])
        close_local = datetime.fromisoformat(ed).replace(tzinfo=tz) + timedelta(days=1)
        close_utc = close_local.astimezone(timezone.utc)

        # Last checkpoint of each stage inside each lead bin.
        chosen: dict[tuple[str, str], tuple] = {}
        for hid, stage, decided, _ in cps:
            lead = (close_utc - _iso(decided)).total_seconds() / 3600.0
            label = _lead_bin(lead)
            if label is None:
                continue
            chosen[(label, stage)] = (hid, stage, decided, lead)
        if not chosen:
            skipped["no_binned_checkpoints"] += 1
            continue

        for (label, stage), (hid, _, decided, lead) in chosen.items():
            if args.pairing == "after":
                book = conn.execute(
                    "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
                    " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                    " AND observed_at>=? ORDER BY observed_at LIMIT 1",
                    (st, ed, decided),
                ).fetchone()
            else:
                book = conn.execute(
                    "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
                    " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                    " AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
                    (st, ed, decided),
                ).fetchone()
            if book is None:
                skipped["no_book"] += 1
                continue
            gap = abs((_iso(book[0]) - _iso(decided)).total_seconds())
            if gap > tolerance.total_seconds():
                skipped["book_too_far"] += 1
                continue
            rows = conn.execute(
                "SELECT bucket, lower_c, upper_c, probability"
                " FROM basket_probability_bridge_bucket_probability"
                " WHERE history_id=? ORDER BY ordinal",
                (hid,),
            ).fetchall()
            intervals = [(r[0], r[1], r[2]) for r in rows]
            model = {r[0]: float(r[3]) for r in rows}
            winner = _winner(intervals, t_true)
            if winner is None:
                skipped["truth_outside_ladder"] += 1
                continue
            ask, mid = _market_vectors(book[1])
            side = ask if args.market_side == "ask" else mid
            if set(side) != set(model):
                skipped["bucket_mismatch"] += 1
                continue
            if all(v is None for v in side.values()):
                skipped["empty_book"] += 1
                continue
            market_raw = {k: (v if v is not None else 0.0) for k, v in side.items()}
            model_n = _clip_normalise(model, args.floor)
            market_n = _clip_normalise(market_raw, args.floor)
            ask_sum = sum(v for v in ask.values() if v is not None)
            bid_sum = sum(
                (2.0 * m - (a if a is not None else m))
                for m, a in ((mid[b], ask[b]) for b in mid) if m is not None
            )
            samples.append({
                "ask_sum": ask_sum,
                "bid_sum": bid_sum,
                "model_vec": model_n,
                "market_vec": market_n,
                "station": st,
                "event_date": ed,
                "stage": stage,
                "lead_bin": label,
                "lead_hours": round(lead, 2),
                "decided": decided,
                "book_gap_s": round(gap),
                "winner": winner,
                "ls_model": math.log(model_n[winner]),
                "ls_market": math.log(market_n[winner]),
                "model_hit": float(max(model_n, key=model_n.get) == winner),
                "market_hit": float(max(market_n, key=market_n.get) == winner),
                "model_zero_winner": model.get(winner, 0.0) <= 0.0,
                "market_unquoted_winner": side.get(winner) is None,
            })

    print(f"samples: {len(samples)}  skipped: {dict(skipped)}")
    if not samples:
        return

    # The served vector at each lead: the highest-authority stage that has a
    # checkpoint in the bin (P1 over P0-M over P0), then its last checkpoint.
    # Not "latest by clock": one compile writes all three stages at the same
    # instant with different fractional-second formatting, and a string
    # comparison of those clocks picks the opening stage at the tie.
    authority = {stage: i for i, stage in enumerate(STAGES)}
    latest: dict[tuple[str, str, str], dict] = {}
    for s in samples:
        key = (s["station"], s["event_date"], s["lead_bin"])
        rank = (authority[s["stage"]], _iso(s["decided"]))
        if key not in latest or rank > latest[key]["_rank"]:
            latest[key] = dict(s, _rank=rank)
    served = list(latest.values())
    gaps = sorted(s["book_gap_s"] for s in samples)
    print(f"book gap seconds: median {gaps[len(gaps)//2]}, p90 {gaps[int(0.9*len(gaps))]}, max {gaps[-1]}")

    order = [label for _, _, label in LEAD_BINS]

    def show(title: str, table: dict[str, dict], keys: list[str]) -> None:
        print(f"\n== {title} ==")
        print(f"{'group':>10} {'n':>5} {'days':>5} {'model':>8} {'market':>8} {'diff':>8} {'median':>8} {'m>k':>6} {'hit_m':>6} {'hit_k':>6} {'m0':>4} {'kNA':>4}")
        for k in keys:
            if k not in table:
                continue
            r = table[k]
            print(
                f"{k:>10} {r['n']:>5} {r['station_days']:>5} {r['model_logscore']:>8.3f}"
                f" {r['market_logscore']:>8.3f} {r['diff_nats']:>+8.3f} {r['diff_median']:>+8.3f}"
                f" {r['model_better_share']:>6.2f} {r['model_hit']:>6.2f} {r['market_hit']:>6.2f}"
                f" {r['model_zero_winner']:>4} {r['market_unquoted_winner']:>4}"
            )

    all_served = _summarise(served, "stage")
    overall = {
        "n": len(served),
        "diff_nats": statistics.fmean(s["ls_model"] - s["ls_market"] for s in served),
    }
    lo, hi = _bootstrap_ci(served, args.bootstrap, seed=7)
    print(f"\nSERVED overall: n={overall['n']} diff={overall['diff_nats']:+.4f} nats"
          f"  95% block-bootstrap CI [{lo:+.4f}, {hi:+.4f}]")

    show("served vector by lead bin", _summarise(served, "lead_bin"), order)
    show("served vector by stage", all_served, list(STAGES))
    for stage in STAGES:
        rows = [s for s in samples if s["stage"] == stage]
        if rows:
            show(f"{stage} checkpoints by lead bin", _summarise(rows, "lead_bin"), order)

    by_station = _summarise(served, "station")
    ranked = sorted(by_station.items(), key=lambda kv: kv[1]["diff_nats"])
    show("served vector by station (worst 10)", dict(ranked[:10]), [k for k, _ in ranked[:10]])
    show("served vector by station (best 10)", dict(ranked[-10:]), [k for k, _ in ranked[-10:]])
    positive = sum(1 for _, r in ranked if r["diff_nats"] > 0)
    print(f"\nstations with positive diff: {positive}/{len(ranked)}")

    dates = sorted({s["event_date"] for s in served})
    mid_date = dates[len(dates) // 2]
    halves = {"early": [s for s in served if s["event_date"] < mid_date],
              "late": [s for s in served if s["event_date"] >= mid_date]}
    print(f"\n== halves split at {mid_date} ==")
    for name, rows in halves.items():
        if rows:
            d = statistics.fmean(s["ls_model"] - s["ls_market"] for s in rows)
            lo, hi = _bootstrap_ci(rows, args.bootstrap, seed=11)
            print(f"{name:>6} n={len(rows):>5} diff={d:+.4f} CI [{lo:+.4f}, {hi:+.4f}]")
        for label in order:
            sub = [s for s in rows if s["lead_bin"] == label]
            if len(sub) >= 20:
                d = statistics.fmean(s["ls_model"] - s["ls_market"] for s in sub)
                print(f"       {label:>7} n={len(sub):>4} diff={d:+.4f}")

    # Incremental information: geometric pooling p_w ∝ p^w · q^(1-w).  The
    # weight that maximises the log score on the early half, scored on the
    # late half, says whether the model adds anything ON TOP of the price;
    # w=0 means the market alone is the best forecast available here.
    grid = [round(0.05 * i, 2) for i in range(0, 21)]

    def pooled_logscore(rows: list[dict], w: float) -> float:
        total = 0.0
        for r in rows:
            m, k = r["model_vec"], r["market_vec"]
            unnorm = {b: (m[b] ** w) * (k[b] ** (1.0 - w)) for b in m}
            z = sum(unnorm.values())
            total += math.log(unnorm[r["winner"]] / z)
        return total / len(rows)

    def pooling_table(title: str, key: str, keys: list[str], rows_all: list[dict]) -> dict:
        print(f"\n== {title}: geometric pooling weight on model (fit early half, score late half) ==")
        print(f"{'group':>10} {'n_fit':>6} {'n_test':>6} {'w_fit':>6} {'test@w':>8} {'test@0':>8} {'test@1':>8} {'gain':>8} {'w_test':>6}")
        out = {}
        for k in keys:
            rows = [r for r in rows_all if r[key] == k]
            fit = [r for r in rows if r["event_date"] < mid_date]
            test = [r for r in rows if r["event_date"] >= mid_date]
            if len(fit) < 30 or len(test) < 30:
                continue
            w_fit = max(grid, key=lambda w: pooled_logscore(fit, w))
            w_test = max(grid, key=lambda w: pooled_logscore(test, w))
            at_w = pooled_logscore(test, w_fit)
            at_0 = pooled_logscore(test, 0.0)
            at_1 = pooled_logscore(test, 1.0)
            out[k] = {"n_fit": len(fit), "n_test": len(test), "w_fit": w_fit,
                      "test_at_w": at_w, "test_at_0": at_0, "test_at_1": at_1,
                      "gain_over_market": at_w - at_0, "w_test": w_test}
            print(f"{k:>10} {len(fit):>6} {len(test):>6} {w_fit:>6.2f} {at_w:>8.3f} {at_0:>8.3f} {at_1:>8.3f} {at_w - at_0:>+8.3f} {w_test:>6.2f}")
        return out

    pool_lead = pooling_table("served vector by lead bin", "lead_bin", order, served)
    pool_stage = pooling_table("served vector by stage", "stage", list(STAGES), served)
    pool_p1 = pooling_table("P1 checkpoints by lead bin", "lead_bin", order,
                            [s for s in samples if s["stage"] == "P1"])
    pool_p0 = pooling_table("P0 checkpoints by lead bin", "lead_bin", order,
                            [s for s in samples if s["stage"] == "P0"])

    print("\n== book overround at the served samples (sum of best asks / best bids) ==")
    for label in order:
        rows = [r for r in served if r["lead_bin"] == label]
        if len(rows) < 30:
            continue
        a = statistics.median(r["ask_sum"] for r in rows)
        b = statistics.median(r["bid_sum"] for r in rows)
        print(f"{label:>10} n={len(rows):>5} median ask_sum={a:.3f} bid_sum={b:.3f}")

    # Linear shrinkage: regress (outcome - q) on (p - q) over every bucket of
    # every served sample.  The slope is the fraction of the model's
    # disagreement with the price that is real.
    print("\n== linear shrinkage beta = cov(y-q, p-q)/var(p-q), served vector ==")
    for label in order:
        rows = [r for r in served if r["lead_bin"] == label]
        if len(rows) < 30:
            continue
        xs, ys = [], []
        for r in rows:
            for b, pm in r["model_vec"].items():
                q = r["market_vec"][b]
                xs.append(pm - q)
                ys.append((1.0 if b == r["winner"] else 0.0) - q)
        mx = statistics.fmean(xs); my = statistics.fmean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        var = sum((x - mx) ** 2 for x in xs)
        beta = cov / var if var > 0 else float("nan")
        print(f"{label:>10} n={len(rows):>5} beta={beta:>+6.3f}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({
                "args": vars(args),
                "samples": samples,
                "served_by_lead": _summarise(served, "lead_bin"),
                "served_by_stage": all_served,
                "served_by_station": by_station,
                "pooling_by_lead": pool_lead,
                "pooling_by_stage": pool_stage,
                "pooling_p1_by_lead": pool_p1,
                "pooling_p0_by_lead": pool_p0,
            }, fh, indent=1, sort_keys=True)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
