#!/usr/bin/env python3
"""PROTOCOL.md: differential edges when the model is not the better forecaster overall.

Read-only against the production SSOT, so it runs on the machine that holds it:

    python3 docs/research/p0_differential_edge_20260927/differential_edges.py [--ssot PATH] [--since 2026-08-15]

H1 shorts a tail bucket only where the bid leaves room (gamma's executable
part), H2 takes the Proper legs only on days the model and the market agree
on the centre and the model is the narrower, H3 asks whether the market
follows a model update within hours.  Decision points are the P0 facts of the
training-fact table; p, the market vector, the floor, the served axis and the
hours to settlement are production's.  Writes results.json beside this file.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.center_width_calibration import served_bucket_axis  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

DEFAULT_SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
FAR_H = 36.0
H1_RULE = (0.01, 0.02, 2.0)                       # p <= 0.01, bid >= 0.02, p <= bid / 2
H1_GRID = [(0.01, b, d) for b in (0.01, 0.02, 0.03, 0.05) for d in (2.0, 3.0)]
H1_MARKET_ONLY = (0.05, 3, 0.02)                  # q <= 0.05, >= 3 rungs from the market mode, bid >= 0.02
H2_CENTRE_C, H2_SD_RATIO = 0.3, 0.9
H3_MIN_MOVE_C, H3_MIN_H, H3_HORIZONS = 0.5, 12.0, (3, 6, 12)
H_BINS = ((36, 48), (48, 60), (60, 1e9))
BLOCK, BOOT, SEED = 3, 2000, 20260927
LEVELS = (2.5, 97.5, 0.85, 99.15)                 # 95% and Bonferroni-over-three 98.3%

FACTS_SQL = (
    "SELECT station_id, event_date, decision_at_utc, settlement_timezone, probability_envelope_id, ws_snapshot_id"
    " FROM basket_policy_training_fact WHERE stage='P0' AND event_date>=?"
    " ORDER BY station_id, event_date, decision_at_utc, created_at_utc, fact_id"
)
ENVELOPE_SQL = (
    "SELECT probabilities_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
    " FROM basket_probability_envelope WHERE envelope_id=?"
)
SNAPSHOT_SQL = "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot WHERE snapshot_id=?"
# Times are compared parsed, not as text: stored stamps mix precisions, and a
# text bound one digit longer than the stamp would drop a snapshot on the edge.
SNAPSHOT_TIMES_SQL = (
    "SELECT snapshot_id, observed_at, sequence FROM basket_official_ws_snapshot"
    " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
)
LABELS_SQL = "SELECT station_id, event_date, settled_bucket, truth_revision_no FROM basket_station_day_settlement_label"


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


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _levels(raw) -> list[tuple[float, float]]:
    out = []
    for level in raw or ():
        try:
            price = float(level.get("price"))
            shares = float(level.get("shares") or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0.0 and shares > 0.0:
            out.append((price, shares))
    return out


def book_sides(buckets) -> dict[str, dict]:
    """Best YES bid and ask with their sizes, read as ``market_reference_v1`` reads them."""

    sides = {}
    for row in buckets or ():
        if not isinstance(row, dict):
            continue
        asks = _levels(row.get("ask_levels"))
        bids = _levels(row.get("bid_levels"))
        ask = min(asks) if asks and row.get("ask_available", True) else None
        bid = max(bids) if bids else None
        if bid is None:
            try:
                raw_bid = float(row.get("bid_price") or 0.0)
            except (TypeError, ValueError):
                raw_bid = 0.0
            if row.get("bid_available", raw_bid > 0.0) and raw_bid > 0.0:
                bid = (raw_bid, float("nan"))
        sides[str(row.get("bucket"))] = {
            "ask": ask[0] if ask else None, "ask_size": ask[1] if ask else None,
            "bid": bid[0] if bid else None, "bid_size": bid[1] if bid else None,
            "unmaterialized": bool(row.get("unmaterialized_book", False)),
        }
    return sides


class Store:
    def __init__(self, conn):
        self.conn = conn
        self.envelopes, self.snapshots, self.times = {}, {}, {}

    def envelope(self, envelope_id):
        if envelope_id in self.envelopes:
            return self.envelopes[envelope_id]
        row = self.conn.execute(ENVELOPE_SQL, (envelope_id,)).fetchone()
        out = None
        if row is not None:
            probabilities = _decode(row[0]) or {}
            intervals = _decode(row[1]) or {}
            try:
                keys = sorted(intervals, key=lambda k: float("-inf") if intervals[k][0] is None else float(intervals[k][0]))
                centers, _edges = served_bucket_axis(intervals, keys)
            except (ValueError, TypeError, KeyError, IndexError):
                keys, centers = [], []
            if keys and set(keys) == set(probabilities):
                total = sum(float(probabilities[k]) for k in keys)
                if total > 0.0 and math.isfinite(total):
                    floored, _ = floor_probabilities({k: float(probabilities[k]) / total for k in keys})
                    p = np.array([floored[k] for k in keys])
                    c = np.array(centers, dtype=float)
                    mean = float(p @ c)
                    out = {"keys": keys, "centers": c, "p": p, "mean": mean,
                           "sd": math.sqrt(max(float(p @ (c - mean) ** 2), 0.0))}
        self.envelopes[envelope_id] = out
        return out

    def _market(self, buckets, observed_at, keys, centers):
        sides = book_sides(buckets)
        reference = market_reference_v1(buckets)
        q = None
        if reference.get("state") == "complete" and set(reference["vector"]) == set(keys):
            q = np.array([float(reference["vector"][k]) for k in keys])
        out = {"sides": sides, "q": q, "observed_at": observed_at}
        if q is not None:
            mean = float(q @ centers)
            out.update(mean=mean, sd=math.sqrt(max(float(q @ (centers - mean) ** 2), 0.0)))
        return out

    def snapshot(self, snapshot_id, keys, centers):
        cache = (snapshot_id, tuple(keys))
        if cache in self.snapshots:
            return self.snapshots[cache]
        row = self.conn.execute(SNAPSHOT_SQL, (snapshot_id,)).fetchone()
        out = None if row is None else self._market(_decode(row[0]) or [], str(row[1]), keys, centers)
        self.snapshots[cache] = out
        return out

    def snapshot_asof(self, station, day, after: datetime, until: datetime, keys, centers):
        """The last committed snapshot strictly after ``after`` and at or before ``until``."""

        if (station, day) not in self.times:
            rows = [(_utc(observed), int(sequence or 0), str(sid))
                    for sid, observed, sequence in self.conn.execute(SNAPSHOT_TIMES_SQL, (station, day))]
            self.times[(station, day)] = sorted(rows)
        best = None
        for observed, _sequence, sid in self.times[(station, day)]:
            if observed <= after:
                continue
            if observed > until:
                break
            best = sid
        return None if best is None else self.snapshot(best, keys, centers)


def labels(conn):
    best = {}
    for station, day, bucket, revision in conn.execute(LABELS_SQL):
        key = (str(station), str(day))
        if key not in best or int(revision or 0) > best[key][1]:
            best[key] = (str(bucket), int(revision or 0))
    return {k: v[0] for k, v in best.items()}


# ---------------------------------------------------------------- statistics

def _blocks(days_sorted, rng):
    n_blocks = math.ceil(len(days_sorted) / BLOCK)
    return [(s + b) % len(days_sorted) for s in rng.integers(0, len(days_sorted), n_blocks) for b in range(BLOCK)]


def roi_summary(trades):
    """ROI = sum(w * profit) / sum(w * cost), event-day block bootstrap, halves."""

    if not trades:
        return {"trades": 0}
    days = sorted({t["day"] for t in trades})
    index = {d: i for i, d in enumerate(days)}
    num = np.zeros(len(days))
    den = np.zeros(len(days))
    for t in trades:
        num[index[t["day"]]] += t["w"] * t["profit"]
        den[index[t["day"]]] += t["w"] * t["cost"]
    rng = np.random.default_rng(SEED)
    boots = []
    for _ in range(BOOT):
        pick = _blocks(days, rng)
        boots.append(num[pick].sum() / den[pick].sum() if den[pick].sum() > 0 else 0.0)
    half = len(days) // 2
    halves = [float(num[:half].sum() / den[:half].sum()) if den[:half].sum() > 0 else None,
              float(num[half:].sum() / den[half:].sum()) if den[half:].sum() > 0 else None]
    return {
        "trades": len(trades), "station_days": len({(t["station"], t["day"]) for t in trades}), "event_days": len(days),
        "roi": float(num.sum() / den.sum()), "ci95": [float(np.percentile(boots, LEVELS[0])), float(np.percentile(boots, LEVELS[1]))],
        "ci983": [float(np.percentile(boots, LEVELS[2])), float(np.percentile(boots, LEVELS[3]))],
        "halves": halves, "hit_rate": float(np.mean([t.get("y", 0.0) for t in trades])),
        "mean_price": float(np.mean([t.get("price", 0.0) for t in trades])),
    }


def holds(summary):
    return bool(summary.get("trades")) and summary["ci95"][0] > 0.0 and all(h is not None and h > 0.0 for h in summary["halves"])


def ols_summary(events, horizon):
    rows = [e for e in events if e.get(f"dq_{horizon}") is not None]
    if len(rows) < 10:
        return {"events": len(rows)}
    days = sorted({e["day"] for e in rows})
    by_day = defaultdict(list)
    for i, e in enumerate(rows):
        by_day[e["day"]].append(i)
    X = np.array([[1.0, e["dp"], e["gap_prev"]] for e in rows])
    y = np.array([e[f"dq_{horizon}"] for e in rows])

    def fit(idx):
        beta, *_ = np.linalg.lstsq(X[idx], y[idx], rcond=None)
        return beta

    beta = fit(np.arange(len(rows)))
    rng = np.random.default_rng(SEED)
    boots = []
    for _ in range(BOOT):
        idx = np.concatenate([by_day[days[i]] for i in _blocks(days, rng)])
        boots.append(fit(idx)[1])
    half = len(days) // 2
    first = np.concatenate([by_day[d] for d in days[:half]]) if half else np.array([], dtype=int)
    second = np.concatenate([by_day[d] for d in days[half:]])
    moved = [e for e in rows if abs(e[f"dq_{horizon}"]) >= 0.1]
    return {
        "events": len(rows), "event_days": len(days),
        "beta_dp": float(beta[1]), "beta_gap_prev": float(beta[2]), "intercept": float(beta[0]),
        "ci95_beta_dp": [float(np.percentile(boots, LEVELS[0])), float(np.percentile(boots, LEVELS[1]))],
        "ci983_beta_dp": [float(np.percentile(boots, LEVELS[2])), float(np.percentile(boots, LEVELS[3]))],
        "halves_beta_dp": [float(fit(first)[1]) if len(first) > 3 else None, float(fit(second)[1]) if len(second) > 3 else None],
        "same_sign_share": (float(np.mean([np.sign(e[f"dq_{horizon}"]) == np.sign(e["dp"]) for e in moved])) if moved else None),
        "moved_events": len(moved),
    }


# ---------------------------------------------------------------- main pass

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssot", default=str(DEFAULT_SSOT))
    parser.add_argument("--since", default="2026-08-15")
    parser.add_argument("--out", default=str(HERE / "results.json"))
    args = parser.parse_args()
    started = time.time()
    conn = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    store = Store(conn)
    truth = labels(conn)

    by_sd = defaultdict(list)
    for station, day, at, tz, env, snap in conn.execute(FACTS_SQL, (args.since,)):
        by_sd[(str(station), str(day))].append({
            "at": str(at), "h": float(hours_to_settlement(str(day), as_of_utc=str(at), settlement_timezone=str(tz))),
            "envelope_id": str(env), "snapshot_id": str(snap)})
    print(f"P0 station-days {len(by_sd)}, facts {sum(len(v) for v in by_sd.values())} ({time.time() - started:.0f}s)", flush=True)

    skipped = Counter()
    h1 = defaultdict(list)          # variant -> trades
    h2 = {"conditioned": [], "unconditioned": [], "conditioned_1share": [], "unconditioned_1share": []}
    h3 = []
    for (station, day), facts in by_sd.items():
        label = truth.get((station, day))
        traded = defaultdict(set)
        h2_done = {"conditioned": False, "unconditioned": False}
        previous = None
        for position, fact in enumerate(facts):
            env = store.envelope(fact["envelope_id"])
            if env is None:
                skipped["envelope_unusable"] += 1
                continue
            keys, centers, p = env["keys"], env["centers"], env["p"]
            market = store.snapshot(fact["snapshot_id"], keys, centers)
            if market is None:
                skipped["snapshot_missing"] += 1
                continue
            # ---- H3: a model update, and the market over the next hours
            if previous is not None and previous["envelope_id"] != fact["envelope_id"] and market["q"] is not None:
                dp = env["mean"] - previous["mean"]
                if abs(dp) >= H3_MIN_MOVE_C and fact["h"] > H3_MIN_H:
                    event = {"station": station, "day": day, "dp": dp, "gap_prev": previous["mean"] - market["mean"]}
                    t0 = _utc(market["observed_at"])
                    for horizon in H3_HORIZONS:
                        later = store.snapshot_asof(station, day, t0, t0 + timedelta(hours=horizon), keys, centers)
                        event[f"dq_{horizon}"] = (later["mean"] - market["mean"]) if later and later.get("q") is not None else None
                    h3.append(event)
            previous = {"envelope_id": fact["envelope_id"], "mean": env["mean"]}
            if fact["h"] <= FAR_H or label is None:
                continue
            if label not in keys:
                skipped["label_not_on_ladder"] += 1
                continue
            y = np.array([1.0 if k == label else 0.0 for k in keys])
            sides = market["sides"]
            # ---- H1: tail NO where the bid leaves room
            variants = {"primary": H1_RULE, **{f"grid_p{a}_bid{b}_div{d:g}": (a, b, d) for a, b, d in H1_GRID}}
            if position == 0:
                variants["first_decision_only"] = H1_RULE
            for name, (p_max, bid_min, div) in variants.items():
                for i, k in enumerate(keys):
                    side = sides.get(k) or {}
                    bid = side.get("bid")
                    if bid is None or k in traded[name]:
                        continue
                    if p[i] <= p_max and bid >= bid_min and p[i] <= bid / div:
                        traded[name].add(k)
                        h1[name].append({"station": station, "day": day, "h": fact["h"], "w": 1.0, "y": y[i],
                                         "price": 1.0 - bid, "profit": bid - y[i], "cost": 1.0 - bid,
                                         "bid_size": side.get("bid_size")})
            if market["q"] is not None:
                q = market["q"]
                mode = int(np.argmax(q))
                q_max, rungs, bid_min = H1_MARKET_ONLY
                for i, k in enumerate(keys):
                    bid = (sides.get(k) or {}).get("bid")
                    if bid is None or k in traded["market_only"]:
                        continue
                    if q[i] <= q_max and abs(i - mode) >= rungs and bid >= bid_min:
                        traded["market_only"].add(k)
                        h1["market_only"].append({"station": station, "day": day, "h": fact["h"], "w": 1.0, "y": y[i],
                                                  "price": 1.0 - bid, "profit": bid - y[i], "cost": 1.0 - bid})
                # ---- H2: the Proper legs, on centre-agreement days where the model is the narrower
                agree = abs(env["mean"] - market["mean"]) < H2_CENTRE_C and env["sd"] < H2_SD_RATIO * market["sd"]
                for name, condition in (("conditioned", agree), ("unconditioned", True)):
                    if h2_done[name] or not condition:
                        continue
                    h2_done[name] = True
                    for i, k in enumerate(keys):
                        side = sides.get(k) or {}
                        s = math.log(p[i] / q[i]) if q[i] > 0 else 0.0
                        ask, bid = side.get("ask"), side.get("bid")
                        leg = None
                        if ask is not None and p[i] > ask:
                            leg = {"side": "YES", "price": ask, "profit": y[i] - ask, "cost": ask}
                        elif bid is not None and p[i] < bid:
                            leg = {"side": "NO", "price": 1.0 - bid, "profit": bid - y[i], "cost": 1.0 - bid}
                        if leg is None:
                            continue
                        base = {"station": station, "day": day, "h": fact["h"], "y": y[i], **leg}
                        h2[name].append({**base, "w": abs(s)})
                        h2[f"{name}_1share"].append({**base, "w": 1.0})
    print(f"pass done ({time.time() - started:.0f}s); skipped {dict(skipped)}", flush=True)

    result = {"ssot": str(args.ssot), "since": args.since, "skipped": dict(skipped), "H1": {}, "H2": {}, "H3": {}}
    for name, trades in sorted(h1.items()):
        result["H1"][name] = roi_summary(trades)
    primary = h1.get("primary", [])
    result["H1"]["primary_by_h"] = {f"{lo}-{hi if hi < 1e8 else 'inf'}h": roi_summary([t for t in primary if lo < t["h"] <= hi])
                                    for lo, hi in H_BINS}
    sizes = [t["bid_size"] for t in primary if t.get("bid_size") is not None and math.isfinite(t["bid_size"])]
    result["H1"]["primary_bid_size_p25_p50_p75"] = [float(np.percentile(sizes, q)) for q in (25, 50, 75)] if sizes else None
    result["H1"]["verdict"] = "HOLDS" if holds(result["H1"].get("primary", {})) else "DOES_NOT_HOLD"
    for name, legs in h2.items():
        result["H2"][name] = roi_summary(legs)
        for side in ("YES", "NO"):
            result["H2"][f"{name}_{side}"] = roi_summary([leg for leg in legs if leg["side"] == side])
    result["H2"]["verdict"] = "HOLDS" if holds(result["H2"].get("conditioned", {})) else "DOES_NOT_HOLD"
    for horizon in H3_HORIZONS:
        result["H3"][f"{horizon}h"] = ols_summary(h3, horizon)
    six = result["H3"].get("6h", {})
    result["H3"]["verdict"] = ("HOLDS" if six.get("ci95_beta_dp") and six["ci95_beta_dp"][0] > 0.0
                               and all(b is not None and b > 0.0 for b in six.get("halves_beta_dp", [None]))
                               else "DOES_NOT_HOLD")
    result["H3"]["events_total"] = len(h3)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))

    def line(tag, s):
        if not s.get("trades"):
            return f"{tag}: no trades"
        return (f"{tag}: trades {s['trades']} ({s['station_days']} sd, {s['event_days']} d) ROI {s['roi']:+.4f} "
                f"[{s['ci95'][0]:+.4f},{s['ci95'][1]:+.4f}] halves {[None if h is None else round(h, 4) for h in s['halves']]} "
                f"hit {s['hit_rate']:.4f} price {s['mean_price']:.4f}")
    print("== H1 tail NO where the bid leaves room")
    for name in ["primary", "first_decision_only", "market_only"] + sorted(k for k in result["H1"] if k.startswith("grid_")):
        if name in result["H1"]:
            print("  " + line(name, result["H1"][name]))
    for name, s in result["H1"]["primary_by_h"].items():
        print("  " + line(f"primary {name}", s))
    print("  bid size p25/p50/p75:", result["H1"]["primary_bid_size_p25_p50_p75"])
    print("  VERDICT H1:", result["H1"]["verdict"])
    print("== H2 Proper legs on centre-agreement days, model narrower")
    for name in ("conditioned", "conditioned_YES", "conditioned_NO", "unconditioned", "unconditioned_YES",
                 "unconditioned_NO", "conditioned_1share", "unconditioned_1share"):
        print("  " + line(name, result["H2"][name]))
    print("  VERDICT H2:", result["H2"]["verdict"])
    print(f"== H3 does the market follow a model update ({len(h3)} events)")
    for horizon in H3_HORIZONS:
        s = result["H3"][f"{horizon}h"]
        if "beta_dp" in s:
            print(f"  {horizon}h: events {s['events']} beta_dp {s['beta_dp']:+.3f} [{s['ci95_beta_dp'][0]:+.3f},{s['ci95_beta_dp'][1]:+.3f}] "
                  f"halves {s['halves_beta_dp']} beta_gap_prev {s['beta_gap_prev']:+.3f} same-sign {s['same_sign_share']}")
        else:
            print(f"  {horizon}h: events {s['events']} (too few)")
    print("  VERDICT H3:", result["H3"]["verdict"])
    print(f"done in {time.time() - started:.0f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
