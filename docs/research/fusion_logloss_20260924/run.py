#!/usr/bin/env python3
"""Seat weights against LogLoss-fitted fusion weights on the historical replay.

Reuses the charter 199 historical study's pass 1 (per-update system
distributions and served tables) and its scorer; the only new thing is the
arm: production's fusion-layer structure with the linear pool's log loss as
the objective.  See PROTOCOL.md; writes results.json.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import pickle
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STUDY = ROOT / "docs" / "research" / "fusion_blend_historical_20260923"
for extra in (ROOT, ROOT / "scripts", STUDY):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

spec = importlib.util.spec_from_file_location("hist_score", STUDY / "score.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
FB = S.FB
replay = S.replay
oos = S.oos

WINDOW = FB.FUSION_BLEND_WINDOW_DAYS
FLEET_RIDGE = FB.FUSION_BLEND_FLEET_RIDGE
LOCAL_RIDGE_SCALE = FB.FUSION_BLEND_LOCAL_RIDGE_SCALE
P_FLOOR = 1e-9
FAMILY_CAP = S.FAMILY_CAP


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def training_rows(records):
    """Day-ahead roots as production TrainingRows, plus each system's p(truth)."""

    by_day = defaultdict(list)
    for r in records:
        by_day[(r["station"], r["event_date"])].append(r)
    rows, p_truth = [], {}
    for (station, day), recs in by_day.items():
        if station in FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS:
            continue
        zone = ZoneInfo(recs[0]["tz"])
        midnight = datetime.fromisoformat(day).replace(tzinfo=zone)
        roots = [r for r in recs if midnight - timedelta(days=1) <= _stamp(r["at"]) < midnight]
        if not roots:
            continue
        root = max(roots, key=lambda r: r["at"])
        pool = root["pool"]
        keys, intervals = S._intervals(set().union(*(root["day_rows"][s]["p"] for s in pool)))
        probs = {s: {k: float(root["day_rows"][s]["p"].get(k, 0.0)) for k in keys} for s in pool}
        stats = FB.row_statistics(
            intervals=intervals, truth_c=float(root["actual"]),
            centers_c={s: root["day_rows"][s]["c"] for s in pool},
            system_probabilities=probs, systems=pool,
        )
        record = {
            "station_id": station, "event_date": day, "truth_revision": 0,
            "root_decision_at_utc": root["at"], "truth_known_at_utc": root["truth_known_at"],
            "systems_json": json.dumps(pool),
            "lead_by_system_json": json.dumps({s: root["scopes"][s][0] for s in pool}),
            "center_error_json": json.dumps(stats["center_error"]),
            "crps_systems_json": json.dumps(stats["crps_systems"]),
            "crps_a_json": json.dumps(stats["crps_a"]),
            "crps_b_json": json.dumps(stats["crps_b"]),
        }
        record["content_sha256"] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        row = FB.TrainingRow(record)
        rows.append(row)
        hit = oos._temperature_bucket(float(root["actual"]))
        p_truth[row.identity] = {s: float(root["day_rows"][s]["p"].get(hit, 0.0)) for s in pool}
    return rows, p_truth


def fit_logloss(rows, target, p_truth, *, prior, ridge):
    """Production ``fit_stack``'s simplex and caps, with the log loss."""

    from scipy.optimize import minimize

    keys = target.keys
    start = np.array([prior[s] for s in keys])
    P = np.array([[p_truth[row.identity][s] for s in keys] for row in rows])

    def fun(w):
        q = np.maximum(P @ w, P_FLOOR)
        return float(-np.mean(np.log(q)) + ridge * np.sum((w - start) ** 2))

    def jac(w):
        q = np.maximum(P @ w, P_FLOOR)
        return -(P / q[:, None]).mean(axis=0) + 2 * ridge * (w - start)

    fit = minimize(fun, start, jac=jac, bounds=[(0, 1)] * len(keys),
                   constraints=FB._constraints(target, keys), method="SLSQP",
                   options={"maxiter": 100, "ftol": 1e-10})
    if not fit.success:
        raise ValueError("logloss_fit_failed:" + str(fit.message))
    weights = dict(zip(keys, map(float, fit.x)))
    FB._check(weights, target)
    return weights


def logloss_arm(records, rows, p_truth):
    tables_by_station = {}
    states = Counter()
    moved = []
    for i, r in enumerate(records):
        seat = {s: float(v) for s, v in r["seat"]["w"].items()}
        state = "APPLIED"
        weights = seat
        if r["station"] in FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS:
            state = "SEAT_LABELS_NOT_BOUND"
        else:
            family = r["family"]
            weighted = {s: v for s, v in seat.items() if v > 0 and s in family}
            if len({family[s] for s in weighted}) < 2:
                state = "SEAT_FEWER_THAN_TWO_FAMILIES"
            else:
                at = _stamp(r["at"])
                total = sum(weighted.values())
                target = FB.Target(
                    station=r["station"], event_date=r["event_date"], decision_at=at,
                    local_decision_day=at.astimezone(ZoneInfo(r["tz"])).date(),
                    weights={s: v / total for s, v in weighted.items()},
                    families={s: family[s] for s in weighted},
                    caps={f: FAMILY_CAP for f in set(family.values())},
                    lead={s: r["scopes"][s][0] for s in weighted},
                )
                known = FB.as_of_rows(rows, at)
                local = FB.eligible(known, target, WINDOW, probability=True)
                fleet = FB.eligible(known, target, WINDOW, fleet=True, lead=True, probability=True)
                if not fleet:
                    fleet = FB.eligible(known, target, WINDOW, fleet=True, probability=True)
                if not fleet:
                    state = "SEAT_NO_TRAINING_DAYS"
                else:
                    try:
                        prior = fit_logloss(fleet, target, p_truth, prior=target.weights, ridge=FLEET_RIDGE)
                        fitted = (fit_logloss(local, target, p_truth, prior=prior,
                                              ridge=LOCAL_RIDGE_SCALE / len(local))
                                  if local else prior)
                        served = FB.feasible(fitted, target.families, target.caps)
                        if min(served.values()) < FB.FUSION_BLEND_MIN_WEIGHT:
                            state = "SEAT_VOTED_OUT_A_MEMBER"
                        else:
                            weights = dict(served)
                            for s, v in seat.items():
                                weights.setdefault(s, v)
                    except (ValueError, KeyError, ZeroDivisionError, FloatingPointError):
                        state = "SEAT_FIT_FAILED"
        states[state] += 1
        r["ll_state"] = state
        if state != "APPLIED":
            r["blend"] = r["seat"]
            continue
        moved.append(0.5 * sum(abs(weights[s] - seat[s]) for s in seat))
        station = r["station"]
        if station not in tables_by_station:
            with open(S.PASS1 / f"{station}.tables.pkl", "rb") as handle:
                tables_by_station = {station: pickle.load(handle)}
        tables = tables_by_station[station][r["hour_key"]]
        day_rows = {s: {"probability": r["day_rows"][s]["p"], "calibrated_center_c": r["day_rows"][s]["c"]}
                    for s in r["pool"]}
        positive = {s: v for s, v in weights.items() if v > 0.0}
        scored = replay._blend_and_score(
            weights=positive, day_rows=day_rows, cooling_shift_sigma=r["cooling"],
            latitude=r["latitude"], tables=tables, actual=r["actual"],
        )
        r["blend"] = {"w": scored["w"], "mean": scored["mean"], "served": scored["served"], "vec": scored["vec"]}
        if i % 3000 == 0:
            print(f"  {i}/{len(records)}", flush=True)
    return states, moved


def panel(records, label):
    base = S.compare(records, label)
    if base is None:
        return None
    # Charter 211 panel: bucket Brier and log score with block intervals,
    # Reliability and Resolution from the board's decomposition.
    copies = Counter((r["station"], r["event_date"]) for r in records)
    w = np.array([1.0 / copies[(r["station"], r["event_date"])] for r in records])
    b_seat = np.array([r["seat"]["served"][2] for r in records])
    b_arm = np.array([r["blend"]["served"][2] for r in records])
    n_seat = np.array([r["seat"]["served"][1] for r in records])
    n_arm = np.array([r["blend"]["served"][1] for r in records])
    days = sorted({r["event_date"] for r in records})
    idx_by_day = defaultdict(list)
    for i, r in enumerate(records):
        idx_by_day[r["event_date"]].append(i)
    rng = np.random.default_rng(20260924)
    boots = {"brier": [], "nll": []}
    n_blocks = math.ceil(len(days) / S.BLOCK)
    for _ in range(S.BOOT):
        idx = np.concatenate([idx_by_day[days[(s + j) % len(days)]]
                              for s in rng.integers(0, len(days), n_blocks) for j in range(S.BLOCK)])
        ww = w[idx]
        boots["brier"].append(float((ww * (b_arm[idx] - b_seat[idx])).sum() / ww.sum()))
        boots["nll"].append(float((ww * (n_arm[idx] - n_seat[idx])).sum() / ww.sum()))
    d = base["delta"]
    base["panel_ci95"] = {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in boots.items()}
    base["panel_passes"] = bool(d["brier"] < 0 and d["nll"] < 0 and d["rel"] < 0 and d["res"] > 0)
    base.pop("passes_switch_rule", None)
    return base


def main() -> int:
    started = time.time()
    records = S.load_pass1()
    records.sort(key=lambda r: (r["station"], r["at"]))
    rows, p_truth = training_rows(records)
    print("updates", len(records), "training rows", len(rows), flush=True)
    states, moved = logloss_arm(records, rows, p_truth)
    print("states", dict(states), f"{time.time() - started:.0f}s", flush=True)
    by_day = defaultdict(list)
    for r in records:
        by_day[(r["station"], r["event_date"])].append(r)
    roots = []
    for (station, day), recs in by_day.items():
        midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(recs[0]["tz"]))
        before = [r for r in recs if midnight - timedelta(days=1) <= _stamp(r["at"]) < midnight]
        if before:
            roots.append(max(before, key=lambda r: r["at"]))
    result = {
        "protocol": "PROTOCOL.md", "arm": "logloss_hier21", "training_rows": len(rows),
        "states": dict(states),
        "moved_half_l1_median": float(np.median(moved)) if moved else None,
        "moved_half_l1_p90": float(np.percentile(moved, 90)) if moved else None,
        "all_updates": panel(records, "全部更新（主读数）"),
        "day_ahead_roots": panel(roots, "日前根"),
        "applied_only": panel([r for r in records if r["ll_state"] == "APPLIED"], "只算真的改了权重的更新"),
        "stable_pool_era": panel([r for r in records if r["event_date"] >= S.STABLE_POOL_FROM], "宪章 134 之后"),
        "asos_excluded": panel([r for r in records if r["station"] not in FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS],
                               "去掉 ASOS 11 站"),
        "seconds": round(time.time() - started),
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key in ("all_updates", "day_ahead_roots", "applied_only", "stable_pool_era", "asos_excluded"):
        c = result[key]
        if not c:
            continue
        rc = c["relative_change"]
        print(f"{c['label']}: {c['station_days']} sd / {c['event_days']} d | Brier {rc['brier']:+.3%} "
              f"LogScore {rc['nll']:+.3%} REL {rc['rel']:+.1%} RES {rc['res']:+.2%} | RMSE {rc['rmse']:+.2%} "
              f"CRPS {rc['crps']:+.2%} | panel={c['panel_passes']} ci={c['panel_ci95']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
