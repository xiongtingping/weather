"""The consensus lane at its real execution (PROTOCOL_EXEC.md, 2026-09-24).

    python3 replay_exec.py > exec_results.json

Read-only.  Every statement is fetched to completion (autocommit reader), so
no read transaction outlives a query.  The lane's own functions judge the
rule and price the rung; nothing here restates them.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.p1_consensus_lane import evaluate_consensus  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
from bayes_basket_target.tiebreak_lane import _envelope, book_features, maker_rung  # noqa: E402

SSOT = f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro"
OVERRIDES = ROOT / "docs/research/bayes_kelly_replay_20260924/venue_truth_overrides.json"
FIRST, LAST = "2026-08-27", "2026-09-23"
LATENCY = timedelta(seconds=21.5)
NEAR = timedelta(seconds=90)
MIN_ASK, MAX_ASK, STAKE = 0.50, 0.70, 5.0
MIN_H, MAX_H = 6.0, 10.0
MAX_BUYS, GAP = 2, timedelta(minutes=20)
FRESH_FLOOR = timedelta(seconds=1200)
RUNG_LIFE_SEC = 270.0


def ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def row_of(snapshot, bucket):
    return next((r for r in snapshot["buckets"] if str(r.get("bucket")) == bucket), None)


def judge(env, snapshot, decided_at, tz, day):
    """Four same, window and band on this book; returns (bucket, book_row) or None."""
    h = hours_to_settlement(day, as_of_utc=decided_at, settlement_timezone=tz)
    if not (MIN_H <= h < MAX_H):
        return None
    market = market_reference_v1(snapshot["buckets"])
    if market.get("state") != "complete":
        return None
    reading = evaluate_consensus(env["probabilities"], env["ordinals"], market["vector"])
    if reading.get("state") != "read" or not reading["four_same"]:
        return None
    bucket = str(reading["market_mode"])
    book_row = row_of(snapshot, bucket)
    ask = book_features(book_row)["best_ask"]
    if ask is None or not (MIN_ASK <= float(ask) < MAX_ASK):
        return None
    return bucket, book_row


def tick_of(book_row):
    try:
        return float(book_row.get("price_tick_size") or 0.01)
    except (TypeError, ValueError):
        return 0.01


def main() -> None:
    ov = json.loads(OVERRIDES.read_text())["overrides"]
    db = sqlite3.connect(SSOT, uri=True, timeout=60, isolation_level=None)
    db.execute("PRAGMA query_only=1")
    labels = db.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE event_date BETWEEN ? AND ? AND truth_revision_no=(SELECT MAX(truth_revision_no)"
        " FROM basket_station_day_settlement_label l2 WHERE l2.station_id=l.station_id"
        " AND l2.event_date=l.event_date) ORDER BY event_date, station_id",
        (FIRST, LAST),
    ).fetchall()
    taker, maker_c, maker_o, triggered = [], [], [], []
    stats = defaultdict(int)
    for st, day, truth in labels:
        truth = ov.get(f"{st}|{day}", truth)
        facts = db.execute(
            "SELECT decision_at_utc, probability_envelope_id, settlement_timezone, created_at_utc, fact_id"
            " FROM basket_policy_training_fact WHERE station_id=? AND event_date=? AND stage='P1'"
            " ORDER BY decision_at_utc, created_at_utc",
            (st, day),
        ).fetchall()
        if not facts:
            continue
        last_per_second = {}
        for decided, env_id, tz, _created, _fid in facts:
            last_per_second[decided] = (env_id, tz)
        facts = sorted(last_per_second.items())
        stats["station_days_with_facts"] += 1
        snaps = db.execute(
            "SELECT observed_at, snapshot_id FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? ORDER BY observed_at",
            (st, day),
        ).fetchall()
        if not snaps:
            continue
        snap_t = [ts(s[0]) for s in snaps]
        cache = {}

        def snap(i):
            if i not in cache:
                bj = db.execute(
                    "SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?",
                    (snaps[i][1],),
                ).fetchall()[0][0]
                cache[i] = {"buckets": json.loads(bj or "[]") or [], "observed_at": snaps[i][0]}
            return cache[i]

        def nearest(t):
            j = bisect_left(snap_t, t)
            best = None
            for k in (j - 1, j):
                if 0 <= k < len(snap_t) and abs(snap_t[k] - t) <= NEAR:
                    if best is None or abs(snap_t[k] - t) < abs(snap_t[best] - t):
                        best = k
            return best

        # --- triggers at the lane's own cadence ---
        fired = []   # (judge_time, bucket, snapshot index, envelope, fresh_until)
        envs = {}
        for decided, (env_id, tz) in facts:
            try:
                h = hours_to_settlement(day, as_of_utc=decided, settlement_timezone=tz)
            except Exception:  # noqa: BLE001
                continue
            if not (MIN_H <= h < MAX_H):
                continue
            stats["facts_in_window"] += 1
            t_judge = ts(decided) + LATENCY
            k = nearest(t_judge)
            if k is None:
                stats["no_book_near"] += 1
                continue
            if env_id not in envs:
                envs[env_id] = _envelope(db, env_id)
            env = envs[env_id]
            if not env:
                continue
            got = judge(env, snap(k), decided, tz, day)
            if got is None:
                continue
            until = max(ts(env["fresh_until_utc"]) if env.get("fresh_until_utc") else ts(decided),
                        ts(decided) + FRESH_FLOOR)
            fired.append((t_judge, got[0], k, env, until, decided, tz))
        if not fired:
            continue
        stats["station_days_triggered"] += 1
        first_t, first_bucket, first_k = fired[0][0], fired[0][1], fired[0][2]
        first_ask = book_features(row_of(snap(first_k), first_bucket))["best_ask"]
        triggered.append({"st": st, "day": day, "bucket": first_bucket, "ask": first_ask,
                          "hit": int(first_bucket == truth)})

        # --- A: taker at the judging book ---
        bought, last_buy = set(), None
        for t_judge, bucket, k, _env, _until, _decided, _tz in fired:
            if len(bought) >= MAX_BUYS or bucket in bought or (last_buy and t_judge - last_buy < GAP):
                continue
            row = row_of(snap(k), bucket)
            feat = book_features(row)
            ask, depth = feat["best_ask"], feat["ask_depth_shares"] or 0.0
            if ask is None or depth * ask < STAKE:
                continue
            taker.append({"st": st, "day": day, "bucket": bucket, "price": float(ask),
                          "hit": int(bucket == truth), "at": t_judge.isoformat()})
            bought.add(bucket)
            last_buy = t_judge

        # --- B: maker, re-posted on every snapshot while a trigger is live ---
        for rule, sink in (("conservative", maker_c), ("optimistic", maker_o)):
            bought, last_buy = set(), None
            fi = 0
            live_until, live_env, live_tz, live_decided = None, None, None, None
            resting = None   # (price, bucket, best_bid at posting, posted at)
            start_k = fired[0][2]
            for k in range(start_k, len(snaps)):
                t_k = snap_t[k]
                while fi < len(fired) and fired[fi][0] <= t_k + timedelta(seconds=1):
                    live_until, live_env, live_decided, live_tz = fired[fi][4], fired[fi][3], fired[fi][5], fired[fi][6]
                    fi += 1
                s = snap(k)
                # 1) did the rung standing since the previous snapshot fill?
                if resting is not None:
                    price, bucket, bid_then, posted = resting
                    feat = book_features(row_of(s, bucket))
                    # A rung lives 270 s; a next book further out than that
                    # (plus a minute's slack) cannot vouch for a fill.
                    alive = (t_k - posted).total_seconds() <= RUNG_LIFE_SEC + 60
                    filled = alive and feat["best_ask"] is not None and feat["best_ask"] <= price + 1e-12
                    if not filled and alive and rule == "optimistic":
                        filled = feat["best_bid"] is None or feat["best_bid"] < bid_then - 1e-12
                    if filled:
                        sink.append({"st": st, "day": day, "bucket": bucket, "price": price,
                                     "hit": int(bucket == truth), "at": t_k.isoformat()})
                        bought.add(bucket)
                        last_buy = t_k
                    resting = None
                # 2) re-judge and re-post while the belief is fresh
                if live_until is None or t_k > live_until or len(bought) >= MAX_BUYS:
                    if fi >= len(fired) and (live_until is None or t_k > live_until):
                        break
                    continue
                try:
                    h = hours_to_settlement(day, as_of_utc=t_k.isoformat(), settlement_timezone=live_tz)
                except Exception:  # noqa: BLE001
                    break
                if h < MIN_H:
                    break
                got = judge(live_env, s, t_k.isoformat(), live_tz, day)
                if got is None:
                    continue
                bucket, row = got
                if bucket in bought or (last_buy and t_k - last_buy < GAP):
                    continue
                rung = maker_rung(row, STAKE, tick_size=tick_of(row))
                if rung["refusal"] or rung["limit_price"] > MAX_ASK + 1e-9:
                    continue
                resting = (float(rung["limit_price"]), bucket, float(rung["best_bid"]), t_k)

    rng = np.random.default_rng(203)

    def summ(rows):
        if not rows:
            return None
        days = sorted({r["day"] for r in rows})
        di = {d: i for i, d in enumerate(days)}
        r = np.array([row["hit"] / row["price"] - 1.0 for row in rows])
        idx = np.array([di[row["day"]] for row in rows])
        s = np.bincount(idx, weights=r, minlength=len(days))
        n = np.bincount(idx, minlength=len(days)).astype(float)
        w = rng.multinomial(len(days), np.full(len(days), 1.0 / len(days)), size=4000)
        boots = (w @ s) / np.maximum(w @ n, 1e-12)
        half = days[len(days) // 2]
        first = [x for x, row in zip(r, rows) if row["day"] < half]
        second = [x for x, row in zip(r, rows) if row["day"] >= half]
        return {
            "n": len(rows), "station_days": len({(x["st"], x["day"]) for x in rows}), "days": len(days),
            "hit_rate": float(np.mean([x["hit"] for x in rows])),
            "avg_price": float(np.mean([x["price"] for x in rows])),
            "roi": float(r.mean()), "ci90": [float(np.quantile(boots, 0.05)), float(np.quantile(boots, 0.95))],
            "p_le_0": float((boots <= 0).mean()),
            "first_half_roi": float(np.mean(first)) if first else None,
            "second_half_roi": float(np.mean(second)) if second else None, "split_day": half,
        }

    filled_c = {(x["st"], x["day"]) for x in maker_c}
    unfilled = [t for t in triggered if (t["st"], t["day"]) not in filled_c and t["ask"] is not None]
    filled_trig = [t for t in triggered if (t["st"], t["day"]) in filled_c and t["ask"] is not None]
    out = {
        "stats": dict(stats),
        "taker_at_judging_book": summ(taker),
        "maker_conservative": summ(maker_c),
        "maker_optimistic": summ(maker_o),
        "maker_fill_rate_conservative": len(filled_c) / max(len(triggered), 1),
        "maker_fill_rate_optimistic": len({(x["st"], x["day"]) for x in maker_o}) / max(len(triggered), 1),
        "triggered_station_days": len(triggered),
        "adverse_selection": {
            "filled_station_days_taker_at_trigger": summ([{**t, "price": t["ask"]} for t in filled_trig]),
            "unfilled_station_days_taker_at_trigger": summ([{**t, "price": t["ask"]} for t in unfilled]),
        },
        "rows": {"taker": taker, "maker_conservative": maker_c, "maker_optimistic": maker_o},
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
