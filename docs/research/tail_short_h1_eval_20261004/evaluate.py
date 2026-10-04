#!/usr/bin/env python3
"""Charter 234 (远尾做空线, H1 market-only far-tail NO): live ledger and replay.

Read-only against the production SSOT:

    python3 docs/research/tail_short_h1_eval_20261004/evaluate.py [--ssot PATH]

Replay: every P0 fact more than 36 h from the settlement clock, on the
snapshot that fact read, through production's own ``far_tail_legs`` and
``no_take`` (charter 234 v1 settings).  Two arms per (station-day, bucket):
``first_fire`` -- the first fact the rule fires on, whatever the order would
have been (the research arm of p0_differential_edge_20260927); and
``production`` -- the first fact on which the take is also sendable (NO price
on the cent grid, depth after headroom >= 5 shares), at most four buckets a
station-day; and ``production_v2`` -- charter 251's rule: as ``production``,
and the fact's own served P0 p of the bucket under the YES bid (production's
``model_filter``), judged where the leg is first sendable; a bucket refused
there stays out for the station-day, as the lane bars it.  Cash and the daily budget are not modelled: they decide how
many of these legs the lane can afford, not what a leg earns.

Labels: ``basket_station_day_settlement_label`` (newest revision), plus the
standing ACCEPTED truth for station-days not yet labelled, resolved with
production's ``standing_labels``.  ROI = sum(profit) / sum(cost), taker fee
0.05 p (1 - p) per share; 95% intervals from a three-day event-day block
bootstrap (2,000 draws, seed 20260927), as in the research protocol.
Writes results.json beside this file.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.candidate_window_evidence import standing_labels  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
from bayes_basket_target.strategy_lanes import TAIL_SHORT_DEFAULT_SETTINGS as CFG  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.tail_short_lane import MODEL_NOT_BELOW_BID, far_tail_legs, model_filter, no_take  # noqa: E402

SINCE = "2026-08-15"
RESEARCH_END = "2026-09-25"          # last event day of the study that chose the rule
FEE = float(CFG["taker_fee_rate"])
SEED, BOOT, BLOCK = 20260927, 2000, 3
MODEL_SPLIT_SINCE = "2026-10-04"     # first event day whose outcome was unseen when the split was registered
V2_FORWARD_SINCE = "2026-10-04"      # charter 251's verdict sample (PROTOCOL_FORWARD.md, rule v2)


def model_p(conn, envelope_id: str, bucket: str) -> float | None:
    """The served envelope's normalised probability of ``bucket`` (the lane itself never reads it)."""

    row = conn.execute("SELECT probabilities_json FROM basket_probability_envelope WHERE envelope_id=?",
                       (envelope_id,)).fetchone()
    probabilities = _decode(row[0]) if row else None
    if not probabilities or bucket not in probabilities:
        return None
    total = sum(float(v) for v in probabilities.values())
    return float(probabilities[bucket]) / total if total > 0 else None


def _decode(value):
    for _ in range(3):
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        if not isinstance(value, str):
            return value
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value


def labels(conn) -> dict[tuple[str, str], str]:
    best: dict[tuple[str, str], tuple[str, int]] = {}
    for station, day, bucket, revision in conn.execute(
        "SELECT station_id, event_date, settled_bucket, truth_revision_no FROM basket_station_day_settlement_label"
    ):
        key = (str(station), str(day))
        if key not in best or int(revision or 0) > best[key][1]:
            best[key] = (str(bucket), int(revision or 0))
    rows, _counts = standing_labels(conn, floor_event_date="2026-09-20", only_unlabelled=True)
    for station, day, revision, bucket, *_ in rows:
        key = (str(station), str(day))
        if key not in best or int(revision) > best[key][1]:
            best[key] = (str(bucket), int(revision))
    return {k: v[0] for k, v in best.items()}


def served_at_fact(conn, envelope_id: str, cache: dict) -> dict:
    """The fact's own P0 envelope in the shape of ``proper_bet_lane.served_p0_vector``:
    normalised and floored the same way, read as of the fact (the live lane reads
    the newest P0, which at the fact's own moment is this one)."""

    if envelope_id not in cache:
        row = conn.execute("SELECT probabilities_json FROM basket_probability_envelope WHERE envelope_id=?",
                           (envelope_id,)).fetchone()
        raw = _decode(row[0]) if row else None
        served = {"state": "envelope_unreadable", "probabilities": {}}
        if raw:
            total = sum(float(v) for v in raw.values())
            if total > 0 and math.isfinite(total):
                floored, _ = floor_probabilities({str(k): float(v) / total for k, v in raw.items()})
                served = {"state": "ok", "probabilities": {k: float(v) for k, v in floored.items()}}
        cache[envelope_id] = served
    return cache[envelope_id]


def replay(conn, truth) -> list[dict]:
    facts = defaultdict(list)
    for station, day, at, tz, snap, envelope in conn.execute(
        "SELECT station_id, event_date, decision_at_utc, settlement_timezone, ws_snapshot_id, probability_envelope_id"
        " FROM basket_policy_training_fact WHERE stage='P0' AND event_date>=?"
        " ORDER BY station_id, event_date, decision_at_utc, created_at_utc, fact_id", (SINCE,)
    ):
        facts[(str(station), str(day))].append((str(at), str(tz), str(snap), str(envelope)))
    legs, skipped, envelopes = [], Counter(), {}
    for (station, day), rows in facts.items():
        fired, sent, sent_v2, barred = set(), set(), set(), set()
        for at, tz, snap, envelope in rows:
            hours = float(hours_to_settlement(day, as_of_utc=at, settlement_timezone=tz))
            if not hours > float(CFG["min_hours_to_settlement"]):
                continue
            row = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?", (snap,)).fetchone()
            if row is None:
                skipped["snapshot_missing"] += 1
                continue
            reading = far_tail_legs(
                list(_decode(row[0]) or []), max_market_price=float(CFG["max_market_price"]),
                min_rungs_from_mode=int(CFG["min_rungs_from_mode"]), min_yes_bid=float(CFG["min_yes_bid"]),
            )
            if reading["state"] != "complete":
                skipped["market_incomplete"] += 1
                continue
            for leg in reading["legs"]:
                bucket = leg["bucket"]
                take = no_take(leg["bid"], leg["bid_size"], float(CFG["stake_usdc"]), tick_size=None, min_shares=5.0)
                base = {"s": station, "d": day, "b": bucket, "h": hours, "q": leg["q"], "rungs": leg["rungs_from_mode"],
                        "bid": leg["bid"], "bid_size": leg["bid_size"], "label": truth.get((station, day)),
                        "unit": "F" if "°F" in bucket else "C", "shares": take["shares"], "refusal": take["refusal"],
                        "envelope_id": envelope}
                if bucket not in fired:
                    fired.add(bucket)
                    legs.append({**base, "arm": "first_fire"})
                if bucket not in sent and take["refusal"] is None and len(sent) < int(CFG["max_legs_per_station_day"]):
                    sent.add(bucket)
                    legs.append({**base, "arm": "production"})
                if (bucket not in sent_v2 and bucket not in barred and take["refusal"] is None
                        and len(sent_v2) < int(CFG["max_legs_per_station_day"])):
                    model = model_filter(served_at_fact(conn, envelope, envelopes), bucket, leg["bid"])
                    if model["refusal"] is None:
                        sent_v2.add(bucket)
                        legs.append({**base, "arm": "production_v2", "p": model["p"]})
                    elif model["refusal"] == MODEL_NOT_BELOW_BID:
                        barred.add(bucket)   # judged once where first sendable
    print(f"replay: {len(facts)} P0 station-days; skipped {dict(skipped)}", flush=True)
    return legs


def roi(trades, *, sized=False, fee=True) -> dict | None:
    days = sorted({t["d"] for t in trades})
    if not days:
        return None
    index = {d: i for i, d in enumerate(days)}
    num, den = np.zeros(len(days)), np.zeros(len(days))
    losses = 0  # the far-tail bucket settled: the NO leg lost
    for t in trades:
        y = 1.0 if t["label"] == t["b"] else 0.0
        losses += int(y)
        price = 1.0 - t["bid"]
        n = float(t["shares"]) if sized else 1.0
        num[index[t["d"]]] += n * (t["bid"] - y) - (FEE * price * (1 - price) * n if fee else 0.0)
        den[index[t["d"]]] += n * price
    rng = np.random.default_rng(SEED)
    blocks = math.ceil(len(days) / BLOCK)
    boots = []
    for _ in range(BOOT):
        pick = [(s + b) % len(days) for s in rng.integers(0, len(days), blocks) for b in range(BLOCK)]
        boots.append(num[pick].sum() / den[pick].sum())
    half = len(days) // 2
    return {"legs": len(trades), "station_days": len({(t["s"], t["d"]) for t in trades}), "event_days": len(days),
            "roi": float(num.sum() / den.sum()), "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
            "halves": [float(num[:half].sum() / den[:half].sum()) if half else None, float(num[half:].sum() / den[half:].sum())],
            "losses": losses, "loss_rate": losses / len(trades), "mean_bid": float(np.mean([t["bid"] for t in trades]))}


def live(conn, truth) -> dict:
    rows = conn.execute(
        "SELECT station_id, event_date, bucket, order_intent_id FROM tail_short_lane_evaluation WHERE filled_shares>0"
    ).fetchall()
    out = {"filled": [], "settled": {}, "open": {}}
    for station, day, bucket, intent in rows:
        size, notional = conn.execute(
            "SELECT SUM(matched_size), SUM(matched_notional_usdc) FROM basket_target_order_events"
            " WHERE order_intent_id=? AND event_type='FILL'", (intent,)).fetchone()
        price = notional / size
        fee = FEE * price * (1 - price) * size
        label = truth.get((str(station), str(day)))
        y = None if label is None else float(label == bucket)
        out["filled"].append({"s": station, "d": day, "b": bucket, "shares": size, "cost": notional, "fee": fee, "y": y,
                              "pnl": None if y is None else size * (1 - y) - notional - fee})
    settled = [f for f in out["filled"] if f["y"] is not None]
    pending = [f for f in out["filled"] if f["y"] is None]
    out["settled"] = {"legs": len(settled), "losses": int(sum(f["y"] for f in settled)),
                      "cost": sum(f["cost"] for f in settled), "pnl": sum(f["pnl"] for f in settled),
                      "event_days": sorted({f["d"] for f in settled}),
                      "lost_legs": [(f["s"], f["d"], f["b"]) for f in settled if f["y"]]}
    out["settled"]["roi"] = out["settled"]["pnl"] / out["settled"]["cost"] if settled else None
    out["open"] = {"legs": len(pending), "cost": sum(f["cost"] for f in pending),
                   "by_day": dict(Counter(f["d"] for f in pending))}
    refusals = Counter()
    for (reason,) in conn.execute("SELECT refusal FROM tail_short_lane_evaluation"):
        refusals[(reason or "sent").split(":")[0]] += 1
    out["evaluations"] = dict(refusals)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssot", default=str(ROOT / "data" / "bayes_basket_target_ssot.db"))
    args = parser.parse_args()
    started = time.time()
    conn = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    truth = labels(conn)
    legs = [t for t in replay(conn, truth) if t["label"] is not None]
    windows = {"in_sample": (SINCE, RESEARCH_END), "forward": ("2026-09-26", "9999"), "full": (SINCE, "9999"),
               "forward_v2": (V2_FORWARD_SINCE, "9999")}
    result = {"windows": {}, "slices_report_only": {}, "live": live(conn, truth)}
    for name, (lo, hi) in windows.items():
        inside = [t for t in legs if lo <= t["d"] <= hi]
        ff = [t for t in inside if t["arm"] == "first_fire"]
        pr = [t for t in inside if t["arm"] == "production"]
        result["windows"][name] = {
            "first_fire_1share_nofee": roi(ff, fee=False), "first_fire_1share_fee": roi(ff),
            "production_1share_fee": roi(pr), "production_sized_fee": roi(pr, sized=True),
            "production_v2_1share_fee": roi([t for t in inside if t["arm"] == "production_v2"]),
            "production_v2_sized_fee": roi([t for t in inside if t["arm"] == "production_v2"], sized=True)}
    slices = {"rungs=3": lambda t: t["rungs"] == 3, "rungs>=4": lambda t: t["rungs"] >= 4,
              "bid<2.5c": lambda t: t["bid"] < 0.025, "bid 2.5-3.5c": lambda t: 0.025 <= t["bid"] < 0.035,
              "bid>=3.5c": lambda t: t["bid"] >= 0.035, "h 36-48": lambda t: t["h"] <= 48,
              "h 48-60": lambda t: 48 < t["h"] <= 60, "h >60": lambda t: t["h"] > 60,
              "C": lambda t: t["unit"] == "C", "F": lambda t: t["unit"] == "F"}
    for name, keep in slices.items():
        for window in ("full", "forward"):
            lo, hi = windows[window]
            result["slices_report_only"][f"{name} [{window}]"] = roi(
                [t for t in legs if t["arm"] == "production" and lo <= t["d"] <= hi and keep(t)])
    forward = sorted({t["d"] for t in legs if t["arm"] == "production" and t["d"] >= "2026-09-26"})
    result["forward_by_day"] = {d: roi([t for t in legs if t["arm"] == "production" and t["d"] == d]) for d in forward}
    # PROTOCOL_FORWARD.md, secondary reading added 2026-10-04 after the six forward losses were read: the
    # served P0 p of the triggering fact against the YES bid, on event days from MODEL_SPLIT_SINCE only.
    split = [t for t in legs if t["arm"] == "production" and t["d"] >= MODEL_SPLIT_SINCE]
    for t in split:
        t["p"] = model_p(conn, t["envelope_id"], t["b"])
    result["model_split_report_only"] = {
        "since": MODEL_SPLIT_SINCE,
        "p_below_bid": roi([t for t in split if t["p"] is not None and t["p"] < t["bid"]]),
        "p_at_or_above_bid": roi([t for t in split if t["p"] is not None and t["p"] >= t["bid"]]),
        "p_missing": sum(1 for t in split if t["p"] is None)}
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))

    def line(tag, s):
        if not s:
            return f"  {tag}: —"
        return (f"  {tag:34s} legs {s['legs']:5d} ({s['station_days']} sd / {s['event_days']} d)  ROI {s['roi']*100:+.2f}% "
                f"[{s['ci95'][0]*100:+.2f}, {s['ci95'][1]*100:+.2f}]  halves "
                f"{'/'.join('—' if h is None else f'{h*100:+.2f}' for h in s['halves'])}  losses {s['losses']} "
                f"({s['loss_rate']*100:.2f}%)  bid {s['mean_bid']*100:.2f}c")
    for name, arms in result["windows"].items():
        print(f"== {name}")
        for arm, s in arms.items():
            print(line(arm, s))
    ms = result["model_split_report_only"]
    print(f"== model split since {ms['since']} (report only; p missing {ms['p_missing']})")
    print(line("p < YES bid", ms["p_below_bid"]))
    print(line("p >= YES bid", ms["p_at_or_above_bid"]))
    print("== slices (production, 1 share, fee; report only)")
    for name, s in result["slices_report_only"].items():
        print(line(name, s))
    lv = result["live"]
    print(f"== live: settled {lv['settled']['legs']} legs, {lv['settled']['losses']} lost, cost {lv['settled']['cost']:.2f}, "
          f"pnl {lv['settled']['pnl']:+.2f} ({(lv['settled']['roi'] or 0)*100:+.2f}%), days {lv['settled']['event_days']}; "
          f"lost {lv['settled']['lost_legs']}; open {lv['open']}")
    print(f"   evaluations by outcome {lv['evaluations']}")
    print(f"done in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
