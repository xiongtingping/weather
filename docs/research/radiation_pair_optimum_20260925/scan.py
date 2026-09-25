#!/usr/bin/env python3
"""Charter 222's pair, scanned on production P0 in its served (centred) form.

PROTOCOL.md.  Read-only: one SSOT pass collects, per station-day, the served
market-ladder vector, its bucket intervals, the served radiation shift and
spread, the pre-shift centre and the station's radiation history; every pair
is then re-served offline with production's own ``forecast_clearness_shift_
sigma`` and scored by charter 211 (bucket LogScore and Brier).
"""
from __future__ import annotations

import collections
import json
import math
import random
import sqlite3
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.cross_consensus import forecast_clearness_shift_sigma  # noqa: E402
from bayes_basket_target.materialization import rehydrate_lineage_documents  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

GRID = sorted({round(0.02 * i, 2) for i in range(13)} | {0.07})  # the incumbent 0.07 included
FLOOR = 1e-12
START = date(2026, 8, 16)


def collect() -> list[dict]:
    c = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("pragma query_only=on")
    stations = [r[0] for r in c.execute(
        "select distinct station_id from calibration_station_market_timezone_binding") if len(r[0]) == 4]
    history = collections.defaultdict(list)
    for s, d, v in c.execute(
        "select station_id,local_date,shortwave_radiation_sum from calibration_station_forecast_physics"
        " where shortwave_radiation_sum is not null"):
        history[s].append((d, float(v)))
    for s in history:
        history[s].sort()
    truth = {}
    for s, d, t, rev in c.execute(
        "select station_id,local_date,official_tmax_c,revision_no from calibration_station_daily_high_truth"
        " where local_date>='2026-08-10' and official_tmax_c is not null"):
        if (s, d) not in truth or (rev or 0) > truth[(s, d)][1]:
            truth[(s, d)] = (float(t), rev or 0)
    last = max(d for _, d in truth)
    span = (date.fromisoformat(last) - START).days + 1
    rows = []
    for s in stations:
        try:
            lat = float(station_coordinates(s)["latitude"])
        except Exception:  # noqa: BLE001
            continue
        seen = set()
        for i in range(span):
            d = (START + timedelta(days=i)).isoformat()
            for hh in ("T00:00", "T12:00"):
                r = c.execute(
                    "select event_date,lineage_json,probabilities_json from basket_probability_envelope"
                    " indexed by basket_probability_envelope_station_stage_recency_idx"
                    " where station_id=? and stage='P0' and state='COMMITTED' and decision_at_utc>=?"
                    " order by decision_at_utc asc limit 1", (s, d + hh)).fetchone()
                if not r or r["event_date"] in seen:
                    continue
                lin = rehydrate_lineage_documents(c, json.loads(r["lineage_json"]))
                sc = lin.get("synoptic_cooling_json")
                sc = (json.loads(sc) if isinstance(sc, str) else sc) or {}
                if sc.get("state") != "RESOLVED" or not sc.get("shift_sigma") or sc.get("radiation_sum") is None:
                    continue
                tr = truth.get((s, r["event_date"]))
                intervals = lin.get("bucket_intervals_c_json")
                intervals = json.loads(intervals) if isinstance(intervals, str) else intervals
                if tr is None or lin.get("model_center_c") is None or not intervals:
                    continue
                seen.add(r["event_date"])
                rows.append({
                    "s": s, "ev": r["event_date"], "lat": lat, "rad": float(sc["radiation_sum"]),
                    "sig": float(sc["shift_c"]) / float(sc["shift_sigma"]),
                    "served_c": float(sc["shift_c"]), "truth": tr[0],
                    "c0": float(lin["model_center_c"]),
                    "hist": [(dd, v) for dd, v in history[s] if dd < r["event_date"]][-180:],
                    "p": json.loads(r["probabilities_json"]), "iv": intervals,
                })
    c.close()
    return rows


def ladder(row):
    keys = sorted(row["iv"], key=lambda k: (row["iv"][k][0] is not None, row["iv"][k][0] or 0.0))
    edges = [row["iv"][k][1] for k in keys[:-1]]
    widths = [b - a for a, b in zip(edges, edges[1:])] or [1.0]
    w = st.median(widths)
    probs = [max(0.0, float(row["p"].get(k, 0.0))) for k in keys]
    total = sum(probs)
    probs = [p / total for p in probs]
    # CDF knots: open tails spread over one median width.
    xs = [edges[0] - w] + edges + [edges[-1] + w]
    cdf = [0.0]
    acc = 0.0
    for p in probs:
        acc += p
        cdf.append(acc)
    return keys, edges, xs, cdf


def cdf_at(xs, cdf, x):
    if x <= xs[0]:
        return 0.0
    if x >= xs[-1]:
        return 1.0
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    t = (x - xs[lo]) / (xs[hi] - xs[lo])
    return cdf[lo] + t * (cdf[hi] - cdf[lo])


def shifted(xs, cdf, delta):
    """Knots of the distribution moved by ``delta`` degrees C."""
    return [x + delta for x in xs], cdf


def probs_on(edges, xs, cdf):
    cuts = [0.0] + [cdf_at(xs, cdf, e) for e in edges] + [1.0]
    return [max(0.0, b - a) for a, b in zip(cuts, cuts[1:])]


def prepare(rows):
    out = []
    for row in rows:
        keys, edges, xs, cdf = ladder(row)
        # un-serve: the pre-shift distribution, shared by every arm
        bx, bc = shifted(xs, cdf, -row["served_c"])
        hit = None
        for i, k in enumerate(keys):
            lo, hi = row["iv"][k]
            if (lo is None or row["truth"] >= lo) and (hi is None or row["truth"] < hi):
                hit = i
                break
        if hit is None:
            continue
        shifts = {}
        for kw in GRID:
            for kc in GRID:
                s, rec = forecast_clearness_shift_sigma(
                    radiation_sum=row["rad"], event_date=row["ev"], latitude_deg=row["lat"],
                    station_history=row["hist"], shift_sigma_per_unit=kw,
                    cold_shift_sigma_per_unit=kc)
                if s is None:
                    break
                shifts[(kw, kc)] = s * row["sig"]
            if len(shifts) != (GRID.index(kw) + 1) * len(GRID):
                break
        if len(shifts) != len(GRID) ** 2:
            continue
        scores = {}
        for pair, sc in shifts.items():
            px, pc = shifted(bx, bc, sc)
            p = probs_on(edges, px, pc)
            brier = sum((q - (1.0 if i == hit else 0.0)) ** 2 for i, q in enumerate(p))
            nll = -math.log(max(p[hit], FLOOR))
            sq = (row["truth"] - row["c0"] - sc) ** 2
            scores[pair] = (nll, brier, sq)
        out.append({"s": row["s"], "ev": row["ev"], "scores": scores})
    return out


def mean_scores(items, pair):
    n = len(items)
    return tuple(sum(it["scores"][pair][j] for it in items) / n for j in range(3))


def best(items, j):
    return min(((kw, kc) for kw in GRID for kc in GRID), key=lambda pr: mean_scores(items, pr)[j])


def paired_ci(items, a, b, j, seed=7):
    by_day = collections.defaultdict(list)
    for it in items:
        by_day[it["ev"]].append(it)
    days = sorted(by_day)
    rng = random.Random(seed)
    diffs = []
    for _ in range(1000):
        sample = [it for d in rng.choices(days, k=len(days)) for it in by_day[d]]
        diffs.append(mean_scores(sample, b)[j] - mean_scores(sample, a)[j])
    diffs.sort()
    point = mean_scores(items, b)[j] - mean_scores(items, a)[j]
    return [round(point, 5), round(diffs[25], 5), round(diffs[974], 5)]


def main() -> int:
    rows = collect()
    items = prepare(rows)
    days = sorted({it["ev"] for it in items})
    print("station-days", len(items), "days", len(days), days[0], days[-1], flush=True)
    serv, sym = (0.12, 0.04), (0.07, 0.07)
    b_nll, b_brier, b_sq = best(items, 0), best(items, 1), best(items, 2)
    table = {f"{kw:.2f}/{kc:.2f}": [round(x, 5) for x in mean_scores(items, (kw, kc))] for kw in GRID for kc in GRID}
    half = days[len(days) // 2]
    first = [it for it in items if it["ev"] < half]
    second = [it for it in items if it["ev"] >= half]
    oos = {}
    for name, fit_on, test_on in (("fit_first_test_second", first, second), ("fit_second_test_first", second, first)):
        pick = best(fit_on, 0)
        oos[name] = {
            "picked": pick,
            "test_nll": {f"{p[0]:.2f}/{p[1]:.2f}": round(mean_scores(test_on, p)[0], 5) for p in (pick, serv, sym)},
            "test_brier": {f"{p[0]:.2f}/{p[1]:.2f}": round(mean_scores(test_on, p)[1], 5) for p in (pick, serv, sym)},
        }
    result = {
        "protocol": "PROTOCOL.md", "station_days": len(items), "days": len(days), "span": [days[0], days[-1]],
        "best_by_logscore": b_nll, "best_by_brier": b_brier, "best_by_squared_residual": b_sq,
        "scores_nll_brier_sq": {k: table[k] for k in (f"{b_nll[0]:.2f}/{b_nll[1]:.2f}", f"{b_brier[0]:.2f}/{b_brier[1]:.2f}", "0.12/0.04", "0.07/0.07", "0.00/0.00")},
        "served_minus_symmetric": {"nll": paired_ci(items, sym, serv, 0), "brier": paired_ci(items, sym, serv, 1)},
        "best_minus_served": {"nll": paired_ci(items, serv, b_nll, 0), "brier": paired_ci(items, serv, b_nll, 1)},
        "best_minus_symmetric": {"nll": paired_ci(items, sym, b_nll, 0), "brier": paired_ci(items, sym, b_nll, 1)},
        "out_of_sample_halves": oos, "grid": table,
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "grid"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
