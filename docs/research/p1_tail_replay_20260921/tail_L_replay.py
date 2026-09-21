"""Tail-lane replay: the same rule, three likelihoods (read-only).

q = norm(norm(market_mid x alive) x L), fired where q - walked limit >= 0.08,
FOK 5 USDC via the lane's own taker_take, one bucket once per station-day,
at most two per station-day, held to settlement.

L variants
  mask  : L = 1 (hard bound only)             -- control
  S     : served exp f, root features from the root that sample had (v17 as served)
  M     : exp f, root features computed from the market mid instead
  g1    : charter-150 g, beta = 1               -- research-style
  ga    : g ** alpha(clock), served fallback

Samples, roots, settled buckets: the fitter's own load_samples / RootCache /
scorable_rows.  L: production compile_direct_update / evidence_by_bucket.
Books: first COMMITTED ws snapshot at or after report + k minutes.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/Users/xiong/weather")
sys.path.insert(0, str(ROOT))

import scripts.fit_p1_observation_likelihood as FIT  # noqa: E402
from bayes_basket_target import p1_direct_update as DU  # noqa: E402
from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402
from bayes_basket_target import p1_tail_lane as lane  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.tiebreak_lane import book_features  # noqa: E402

SINCE, THROUGH = sys.argv[1], sys.argv[2]
LAGS_MIN = (1, 8)
HURDLE, STAKE, WALK = 0.08, 5.0, 1
VARIANTS = ("mask", "S", "M", "g1", "ga")
OUT = Path(sys.argv[3])

target = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
artifact = json.loads(OL.ARTIFACT_PATH.read_text())
samples = FIT.load_samples(OL.SAMPLES_PATH, since=SINCE, through=THROUGH)
import os
RAWBIN = os.environ.get("BIN") == "raw"
ASKEXEC = os.environ.get("EXEC") == "ask"
pre = [
    s for s in samples
    if 6.0 <= (float(s["remaining_hours"]) if RAWBIN else OL.clock_hours(s["remaining_hours"], artifact, s["station_id"])) < 10.0
]
print(f"samples {len(samples)}  in [6,10) clock {len(pre)}", flush=True)
rows = FIT.scorable_rows(pre, FIT.RootCache(target))
print(f"scorable {len(rows)}", flush=True)


def utc(text: str) -> datetime:
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def book_after(station: str, day: str, at: datetime):
    row = target.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at>=?"
        " ORDER BY observed_at LIMIT 1",
        (station, day, iso(at)),
    ).fetchone()
    if row is None:
        return None
    return utc(row[0]), json.loads(row[1] or "[]")


def likelihoods(r, market):
    s, v = r["sample"], r["view"]
    feats = FIT.features_of(s)
    common = dict(
        bucket_ordinals=v["ordinals"], bucket_centers_c=v["centers"],
        bucket_upper_bounds_c=v["uppers"], running_high_c=r["high"],
        running_high_bucket=r["running"], hard_excluded_buckets=sorted(r["excluded"]),
        station_id=s["station_id"],
    )
    out = {}
    out["S"], _ = DU.compile_direct_update(feats, artifact, root_probabilities=v["probabilities"], **common)
    out["M"], rec_m = DU.compile_direct_update(feats, artifact, root_probabilities=market, **common)
    ev, _ = OL.evidence_by_bucket(
        feats, artifact, bucket_ordinals=v["ordinals"], bucket_centers_c=v["centers"],
        running_high_c=r["high"], running_high_bucket=r["running"], station_id=s["station_id"],
    )
    top = max(ev[b] for b in ev if b not in r["excluded"]) or 1.0
    out["g1"] = {b: ev[b] / top for b in ev}
    alpha = OL.alpha_for(OL.clock_hours(s["remaining_hours"], artifact, s["station_id"]), artifact)
    out["ga"] = {b: (ev[b] / top) ** alpha for b in ev}
    out["mask"] = {b: 1.0 for b in ev}
    return out


records = []
skipped = defaultdict(int)
for i, r in enumerate(rows):
    s = r["sample"]
    rep = utc(s["as_of_utc"])
    for lag in LAGS_MIN:
        got = book_after(s["station_id"], s["event_date"], rep + timedelta(minutes=lag))
        if got is None:
            skipped[f"no_book_lag{lag}"] += 1
            continue
        seen, buckets = got
        delay = (seen - rep).total_seconds() / 60.0
        if delay > lag + 14:
            skipped[f"book_too_late_lag{lag}"] += 1
            continue
        ref = market_reference_v1(buckets)
        if ref.get("state") != "complete":
            skipped[f"market_incomplete_lag{lag}"] += 1
            continue
        market = {str(k): float(v) for k, v in ref["vector"].items()}
        if r["truth"] not in market:
            skipped[f"truth_not_on_ladder_lag{lag}"] += 1
            continue
        try:
            Ls = likelihoods(r, market)
        except Exception as exc:  # noqa: BLE001
            skipped[f"L_{type(exc).__name__}_lag{lag}"] += 1
            continue
        asks = {str(b.get("bucket")): book_features(b)["best_ask"] for b in buckets}
        rowmap = {str(b.get("bucket")): b for b in buckets}
        tot = sum(market.values())
        rec = {
            "station": s["station_id"], "day": s["event_date"], "report": s["as_of_utc"],
            "lag": lag, "delay_min": round(delay, 2), "truth": r["truth"],
            "m_truth": market.get(r["truth"], 0.0) / tot if tot > 0 else 0.0,
            "clock": OL.clock_hours(s["remaining_hours"], artifact, s["station_id"]),
            "v": {},
        }
        for name in VARIANTS:
            reading = lane.evaluate_rule(market, Ls[name], sorted(r["excluded"]), asks, hurdle=HURDLE)
            if reading.get("state") != "read":
                rec["v"][name] = {"state": reading.get("state")}
                continue
            belief = reading["belief"]
            takes = []
            for bucket in reading["fired"]:
                if ASKEXEC:
                    ask = asks.get(bucket)
                    takes.append({"bucket": bucket, "price": ask, "shares": 1.0, "cost": ask,
                                  "gap": belief.get(bucket, 0.0) - ask, "hit": int(bucket == r["truth"])})
                    continue
                take = lane.taker_take(
                    rowmap.get(bucket), STAKE,
                    tick_size=rowmap.get(bucket, {}).get("price_tick_size") or 0.01,
                    min_shares=float(rowmap.get(bucket, {}).get("min_order_size") or 5.0),
                    walk_ticks=WALK,
                )
                if take["refusal"]:
                    continue
                walked = belief.get(bucket, 0.0) - float(take["limit_price"])
                if walked < HURDLE:
                    continue
                takes.append({"bucket": bucket, "price": take["limit_price"],
                              "shares": take["shares"], "cost": take["cost_usdc"],
                              "gap": walked, "hit": int(bucket == r["truth"])})
            rec["v"][name] = {"q_truth": belief.get(r["truth"], 0.0), "takes": takes}
        records.append(rec)
    if i % 500 == 0:
        print(f"  {i}/{len(rows)} records {len(records)}", flush=True)

OUT.write_text("\n".join(json.dumps(x) for x in records))
print("skipped", dict(skipped))
print("records", len(records))
