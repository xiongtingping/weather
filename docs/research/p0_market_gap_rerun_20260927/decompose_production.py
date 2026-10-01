#!/usr/bin/env python3
"""PROTOCOL.md: split production P0's LogScore/Brier gap to the market into centre, width and shape.

Read-only against the production SSOT, so it runs on the machine that holds it:

    python3 docs/research/p0_market_gap_rerun_20260927/decompose_production.py [--ssot PATH] [--since 2026-09-01]

Every served P0 decision (the training-fact table) against the book bound to
it.  The shift and the scale are production's ``apply_center_width_calibration``
on production's served axis; the floor, the market vector and the hours to
settlement are production's too.  Writes results.json beside this file.
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
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    MAX_ABS_SHIFT_C,
    MAX_WIDTH_LAMBDA,
    MIN_WIDTH_LAMBDA,
    apply_center_width_calibration,
    served_bucket_axis,
)
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

DEFAULT_SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
FAR_H = 36.0
PERIODS = (("since_0901", "2026-09-01"), ("since_0914", "2026-09-14"), ("since_0924", "2026-09-24"))
H_BINS = (("0-24h", 0.0, 24.0), ("24-36h", 24.0, 36.0), ("36-48h", 36.0, 48.0), ("48-60h", 48.0, 60.0),
          ("60h+", 60.0, 1e9))
BLOCK, BOOT, SEED = 3, 2000, 20260927
PARTS = ("gap", "centre", "width", "shape")

FACTS_SQL = (
    "SELECT station_id, event_date, decision_at_utc, settlement_timezone, probability_envelope_id, ws_snapshot_id"
    " FROM basket_policy_training_fact WHERE stage='P0' AND event_date>=?"
    " ORDER BY station_id, event_date, decision_at_utc, created_at_utc, fact_id"
)
ENVELOPE_SQL = (
    "SELECT probabilities_json, json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.system_weights_json'), json_extract(lineage_json,'$.system_family_ids_json'),"
    " json_extract(lineage_json,'$.independence_family_ids_json'), posterior_version"
    " FROM basket_probability_envelope WHERE envelope_id=?"
)
SNAPSHOT_SQL = "SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?"
LABELS_SQL = ("SELECT station_id, event_date, settled_bucket, settled_tmax_c, truth_revision_no"
              " FROM basket_station_day_settlement_label")


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


# ---------------------------------------------------------------- the three transforms

def moments(vec, centers):
    mean = float(vec @ centers)
    return mean, math.sqrt(max(float(vec @ (centers - mean) ** 2), 0.0))


def _apply(vec, keys, centers, edges, *, delta=0.0, lam=1.0):
    out, _receipt = apply_center_width_calibration(
        {k: float(v) for k, v in zip(keys, vec)}, delta_c=delta, width_lambda=lam,
        ordered_keys=keys, key_centers_c=list(centers), open_edge_bounds_c=edges)
    total = sum(out.values())
    return np.array([out[k] / total for k in keys])


def shifted(vec, delta, keys, centers, edges):
    """A pure shift by ``delta``, in steps the transform accepts."""

    out, remaining = vec, float(delta)
    while abs(remaining) > 1e-12:
        step = max(-MAX_ABS_SHIFT_C, min(MAX_ABS_SHIFT_C, remaining))
        out = _apply(out, keys, centers, edges, delta=step)
        remaining -= step
    return out


def recentre(vec, target, keys, centers, edges):
    total, out = 0.0, vec
    for _ in range(3):
        mean, _sd = moments(out, centers)
        if abs(target - mean) < 0.01:
            break
        total += target - mean
        out = shifted(vec, total, keys, centers, edges)      # always from the original vector
    return out


def rescale(vec, target_sd, keys, centers, edges):
    _mean, sd = moments(vec, centers)
    if sd <= 1e-9:
        return vec
    lam = min(MAX_WIDTH_LAMBDA, max(MIN_WIDTH_LAMBDA, target_sd / sd))
    return vec if abs(lam - 1.0) < 1e-12 else _apply(vec, keys, centers, edges, lam=lam)


def score(vec, keys, hit, *, floor):
    if floor:
        floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, vec)})
        vec = np.array([floored[k] for k in keys])
    y = np.zeros(len(vec))
    y[hit] = 1.0
    return -math.log(max(float(vec[hit]), 1e-12)), float(np.sum((vec - y) ** 2))


def decompose(p, m, hit, keys, centers, edges):
    mean_m, sd_m = moments(m, centers)
    pc = recentre(p, mean_m, keys, centers, edges)
    pcw = rescale(pc, sd_m, keys, centers, edges)
    pw = rescale(p, sd_m, keys, centers, edges)
    pwc = recentre(pw, mean_m, keys, centers, edges)
    s = {name: score(v, keys, hit, floor=True) for name, v in (("p", p), ("pc", pc), ("pcw", pcw), ("pw", pw), ("pwc", pwc))}
    s["m"] = score(m, keys, hit, floor=False)
    out = {}
    for j, metric in enumerate(("ls", "br")):
        out[f"{metric}_gap"] = s["p"][j] - s["m"][j]
        out[f"{metric}_centre"] = 0.5 * ((s["p"][j] - s["pc"][j]) + (s["pw"][j] - s["pwc"][j]))
        out[f"{metric}_width"] = 0.5 * ((s["p"][j] - s["pw"][j]) + (s["pc"][j] - s["pcw"][j]))
        out[f"{metric}_shape"] = 0.5 * (s["pcw"][j] + s["pwc"][j]) - s["m"][j]
    return out


# ---------------------------------------------------------------- data

class Store:
    def __init__(self, conn):
        self.conn = conn
        self.envelopes, self.markets = {}, {}

    def envelope(self, envelope_id):
        if envelope_id in self.envelopes:
            return self.envelopes[envelope_id]
        row = self.conn.execute(ENVELOPE_SQL, (envelope_id,)).fetchone()
        out = None
        if row is not None:
            probabilities, intervals = _decode(row[0]) or {}, _decode(row[1]) or {}
            weights, family_map, family_list = _decode(row[2]) or {}, _decode(row[3]) or {}, _decode(row[4]) or []
            try:
                keys = sorted(intervals, key=lambda k: float("-inf") if intervals[k][0] is None else float(intervals[k][0]))
                centers, edges = served_bucket_axis(intervals, keys)
            except (ValueError, TypeError, KeyError, IndexError):
                keys = []
            if keys and set(keys) == set(probabilities):
                raw = np.array([float(probabilities[k]) for k in keys])
                if raw.sum() > 0 and math.isfinite(raw.sum()):
                    seated = [s for s, w in weights.items() if float(w or 0.0) > 0.0] if isinstance(weights, dict) else []
                    if seated and isinstance(family_map, dict) and all(s in family_map for s in seated):
                        families = len({family_map[s] for s in seated})
                    elif isinstance(family_list, list) and family_list:
                        families = len(set(family_list))
                    else:
                        families = None
                    out = {"keys": keys, "centers": np.array(centers, dtype=float), "edges": edges,
                           "p": raw / raw.sum(), "families": families, "version": str(row[5])}
        self.envelopes[envelope_id] = out
        return out

    def market(self, snapshot_id, keys):
        cache = (snapshot_id, tuple(keys))
        if cache in self.markets:
            return self.markets[cache]
        row = self.conn.execute(SNAPSHOT_SQL, (snapshot_id,)).fetchone()
        out = None
        if row is not None:
            reference = market_reference_v1(_decode(row[0]) or [])
            if reference.get("state") == "complete" and set(reference["vector"]) == set(keys):
                out = np.array([float(reference["vector"][k]) for k in keys])
        self.markets[cache] = out
        return out


def labels(conn):
    best = {}
    for station, day, bucket, tmax, revision in conn.execute(LABELS_SQL):
        key = (str(station), str(day))
        if key not in best or int(revision or 0) > best[key][2]:
            best[key] = (str(bucket), float(tmax), int(revision or 0))
    return best


# ---------------------------------------------------------------- summaries

def summarise(items):
    if not items:
        return None
    count = defaultdict(int)
    for d in items:
        count[(d["station"], d["day"])] += 1
    w = np.array([1.0 / count[(d["station"], d["day"])] for d in items])
    days = sorted({d["day"] for d in items})
    by_day = defaultdict(list)
    for i, d in enumerate(items):
        by_day[d["day"]].append(i)
    rng = np.random.default_rng(SEED)
    n_blocks = math.ceil(len(days) / BLOCK)
    draws = [np.concatenate([by_day[days[(s + b) % len(days)]]
                             for s in rng.integers(0, len(days), n_blocks) for b in range(BLOCK)])
             for _ in range(BOOT)]

    def wmean(values, idx=None):
        values = np.asarray(values, dtype=float)
        if idx is None:
            return float((w * values).sum() / w.sum())
        return float((w[idx] * values[idx]).sum() / w[idx].sum())

    out = {"decisions": len(items), "station_days": len(count), "event_days": len(days)}
    for metric in ("ls", "br"):
        for part in PARTS:
            values = np.array([d[f"{metric}_{part}"] for d in items])
            boots = [wmean(values, j) for j in draws]
            out[f"{metric}_{part}"] = [wmean(values), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]
    err_p = np.array([d["mean_p"] - d["tmax"] for d in items])
    err_m = np.array([d["mean_m"] - d["tmax"] for d in items])
    out["bias_p"], out["bias_m"] = wmean(err_p), wmean(err_m)
    out["rmse_p"], out["rmse_m"] = math.sqrt(wmean(err_p ** 2)), math.sqrt(wmean(err_m ** 2))
    out["abs_mean_gap"] = wmean(np.abs(np.array([d["mean_p"] - d["mean_m"] for d in items])))
    out["sd_ratio"] = wmean(np.array([d["sd_p"] / d["sd_m"] for d in items if d["sd_m"] > 0] or [float("nan")]))
    zp = np.array([(d["tmax"] - d["mean_p"]) / d["sd_p"] for d in items if d["sd_p"] > 0])
    zm = np.array([(d["tmax"] - d["mean_m"]) / d["sd_m"] for d in items if d["sd_m"] > 0])
    out["z_sd_p"] = float(np.sqrt(np.mean(zp ** 2))) if len(zp) else None
    out["z_sd_m"] = float(np.sqrt(np.mean(zm ** 2))) if len(zm) else None
    mass = {}
    for label in ("0", "1", "2", "3+"):
        mass[label] = {src: wmean(np.array([d["mass"][label][j] for d in items])) for j, src in enumerate(("model", "market", "truth"))}
    out["mass_by_distance_from_model_mode"] = mass
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssot", default=str(DEFAULT_SSOT))
    parser.add_argument("--since", default="2026-09-01")
    parser.add_argument("--out", default=str(HERE / "results.json"))
    args = parser.parse_args()
    started = time.time()
    conn = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    store, truth = Store(conn), labels(conn)
    skipped = Counter()
    items = []
    seen_first = set()
    for station, day, at, tz, env_id, snap_id in conn.execute(FACTS_SQL, (args.since,)).fetchall():
        station, day = str(station), str(day)
        first = (station, day) not in seen_first
        seen_first.add((station, day))
        label = truth.get((station, day))
        if label is None:
            skipped["no_label"] += 1
            continue
        env = store.envelope(str(env_id))
        if env is None:
            skipped["envelope_unusable"] += 1
            continue
        keys, centers, edges, p = env["keys"], env["centers"], env["edges"], env["p"]
        if label[0] not in keys:
            skipped["label_not_on_ladder"] += 1
            continue
        m = store.market(str(snap_id), keys)
        if m is None:
            skipped["market_incomplete_or_mismatch"] += 1
            continue
        hit = keys.index(label[0])
        try:
            parts = decompose(p, m, hit, keys, centers, edges)
        except ValueError as exc:
            skipped[f"decompose:{exc}"] += 1
            continue
        mean_p, sd_p = moments(p, centers)
        mean_m, sd_m = moments(m, centers)
        mode = int(np.argmax(p))
        mass = {label_: [0.0, 0.0, 0.0] for label_ in ("0", "1", "2", "3+")}
        for i in range(len(keys)):
            d = abs(i - mode)
            slot = mass["3+" if d >= 3 else str(d)]
            slot[0] += p[i]
            slot[1] += m[i]
            slot[2] += 1.0 if i == hit else 0.0
        items.append({"station": station, "day": day, "at": str(at), "first": first,
                      "h": float(hours_to_settlement(day, as_of_utc=str(at), settlement_timezone=str(tz))),
                      "families": env["families"], "version": env["version"], "tmax": label[1],
                      "mean_p": mean_p, "sd_p": sd_p, "mean_m": mean_m, "sd_m": sd_m, "mass": mass, **parts})
    print(f"decisions {len(items)}; skipped {dict(skipped)} ({time.time() - started:.0f}s)", flush=True)

    def fam(n):
        return "unknown" if n is None else ("2" if n <= 2 else "3" if n == 3 else "4+")

    result = {"ssot": str(args.ssot), "since": args.since, "skipped": dict(skipped), "slices": {}}
    for period, floor_day in PERIODS:
        rows = [d for d in items if d["day"] >= floor_day]
        slices = {"all": rows}
        for name, lo, hi in H_BINS:
            slices[name] = [d for d in rows if lo <= d["h"] < hi]
        far = [d for d in rows if d["h"] > FAR_H]
        slices["far_gt36"] = far
        for group in ("2", "3", "4+", "unknown"):
            slices[f"far_fam_{group}"] = [d for d in far if fam(d["families"]) == group]
        firsts = [d for d in far if d["first"]]
        slices["first_decisions_gt36"] = firsts
        for group in ("2", "3", "4+"):
            slices[f"first_fam_{group}"] = [d for d in firsts if fam(d["families"]) == group]
        result["slices"][period] = {name: summarise(rows_) for name, rows_ in slices.items()}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))

    for period, _floor in PERIODS:
        print(f"===== {period}")
        for name, s in result["slices"][period].items():
            if not s:
                continue
            print(f"{name:22s} n={s['decisions']:6d} sd={s['station_days']:5d} d={s['event_days']:3d} | LS gap {s['ls_gap'][0]:+.3f}"
                  f" = centre {s['ls_centre'][0]:+.3f} [{s['ls_centre'][1]:+.3f},{s['ls_centre'][2]:+.3f}]"
                  f" + width {s['ls_width'][0]:+.3f} [{s['ls_width'][1]:+.3f},{s['ls_width'][2]:+.3f}]"
                  f" + shape {s['ls_shape'][0]:+.3f} [{s['ls_shape'][1]:+.3f},{s['ls_shape'][2]:+.3f}]"
                  f" | Br gap {s['br_gap'][0]:+.4f} c {s['br_centre'][0]:+.4f} w {s['br_width'][0]:+.4f} s {s['br_shape'][0]:+.4f}"
                  f" | rmse p {s['rmse_p']:.2f} m {s['rmse_m']:.2f} bias p {s['bias_p']:+.2f} m {s['bias_m']:+.2f}"
                  f" |dmean| {s['abs_mean_gap']:.2f} sd p/m {s['sd_ratio']:.2f} zsd p {s['z_sd_p']:.2f} m {s['z_sd_m']:.2f}")
        s = result["slices"][period].get("far_gt36")
        if s:
            print("   mass by distance from model mode (far_gt36): "
                  + "; ".join(f"{k}: model {v['model']:.3f} market {v['market']:.3f} truth {v['truth']:.3f}"
                              for k, v in s["mass_by_distance_from_model_mode"].items()))
    print(f"done in {time.time() - started:.0f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
