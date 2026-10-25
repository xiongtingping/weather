#!/usr/bin/env python3
"""TX as an input of P0's regression against the new root times the TAF likelihood (PROTOCOL.md).

    python3 docs/research/taf_in_regression_20261025/run.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_taf_likelihood as TAFFIT  # noqa: E402
from bayes_basket_target import taf_likelihood as TL  # noqa: E402
from bayes_basket_target.materialization import _bucket_centers_c  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
REPLAY = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "p0_ngr_root_replay.db"
FLOOR = 1e-3
SCORE_FROM = "2026-10-01"
MIN_TRAIN_DAYS = TAFFIT.MIN_TRAIN_DAYS
DRAWS = 2000
P0M_SQL = (
    "SELECT station_id, event_date, decision_at_utc, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.taf_observed_tx_c'),"
    " json_extract(lineage_json,'$.observed_hard_excluded_buckets'),"
    " json_extract(lineage_json,'$.p0_root_checkpoint_id')"
    " FROM basket_probability_envelope WHERE stage='P0-M' AND state='COMMITTED'"
    " AND event_date>=? AND event_date<=? ORDER BY decision_at_utc"
)


def collect() -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    replay = sqlite3.connect(f"file:{REPLAY}?mode=ro", uri=True)
    truth = TAFFIT.load_truth_revisions(conn)
    first: dict[tuple, tuple] = {}
    for row in conn.execute(P0M_SQL, ("2026-09-29", "2026-10-24")):
        station, day, at, oj, ivj, tx, excl, root_id = row
        if tx in (None, "", "NONE") or not root_id or (station, day) not in truth:
            continue
        first.setdefault((station, day), row)
    conn.close()
    out = []
    for (station, day), (_s, _d, at, oj, ivj, tx, excl, root_id) in sorted(first.items()):
        hit = replay.execute("SELECT mu_c, sd_c, probabilities_json FROM root WHERE checkpoint_id=?", (root_id,)).fetchone()
        if hit is None:
            continue
        ordinals = json.loads(oj)
        ordered = sorted(ordinals, key=lambda k: ordinals[k])
        intervals = json.loads(ivj)
        lineage = {"bucket_intervals_c_json": ivj}
        labels = TAFFIT._revision_labels(truth[(station, day)], lineage=lineage, ordinals=ordinals)
        axis = _bucket_centers_c(lineage, bucket_ordinals=ordinals)
        if not labels or axis is None:
            continue
        probs = json.loads(hit[2])
        excluded = [b for b in str(excl or "").split(",") if b and b != "NONE" and b in ordinals]
        if labels[-1][3] in excluded:
            continue  # the truth (as revised) below an observed bound: no arm can score it
        out.append({
            "station_id": station, "event_date": day, "decided_at_utc": at, "tx_c": float(tx),
            "c0": float(hit[0]), "sd": float(hit[1]), "ordered": ordered,
            "probabilities": {k: float(probs[k]) for k in ordered},
            "tlo": [intervals[k][0] if intervals[k][0] is not None else -math.inf for k in ordered],
            "thi": [intervals[k][1] if intervals[k][1] is not None else math.inf for k in ordered],
            "centers": dict(zip(axis[0], axis[1])), "excluded": excluded,
            "truth_c": labels[-1][2], "truth_bucket": labels[-1][3],
        })
    return out


def finish(case: dict, vec: dict[str, float]) -> dict[str, float]:
    """Charter 241's observed bound (excluded mass onto the lowest live bucket), then the floor."""

    ordered, excluded = case["ordered"], set(case["excluded"])
    live = [k for k in ordered if k not in excluded]
    total = sum(vec.values())
    v = {k: vec[k] / total for k in ordered}
    if excluded and live:
        lost = sum(v[k] for k in excluded)
        v = {k: (0.0 if k in excluded else v[k]) for k in ordered}
        v[live[0]] += lost
    lifted = {k: (v[k] if k in excluded else max(v[k], FLOOR)) for k in ordered}
    s = sum(lifted.values())
    return {k: lifted[k] / s for k in ordered}


def arm_R(case):
    return finish(case, case["probabilities"])


def arm_C(case, artifact):
    likelihoods, _ = TL.compile_taf_likelihood(
        artifact, station_id=case["station_id"], tx_c=case["tx_c"], root_center_c=case["c0"],
        bucket_centers_c=case["centers"], buckets=case["probabilities"],
        root_mean_c=sum(case["probabilities"][k] * case["centers"][k] for k in case["ordered"]),
    )
    return finish(case, {k: case["probabilities"][k] * float(likelihoods.get(k, 1.0)) for k in case["ordered"]})


def arm_T(case, theta, width: bool):
    a, w = theta[0], theta[1]
    mu = case["c0"] + a + w * (case["tx_c"] - case["c0"])
    sd = case["sd"]
    if width:
        sd = sd * math.exp(theta[2] + theta[3] * math.log(abs(case["tx_c"] - case["c0"]) + 0.2))
    sd = max(sd, 0.05)
    q = {}
    for k, lo, hi in zip(case["ordered"], case["tlo"], case["thi"]):
        q[k] = float(ndtr((hi - mu) / sd) - ndtr((lo - mu) / sd))
    return finish(case, {k: max(v, 0.0) + 1e-300 for k, v in q.items()})


def scores(case, vec):
    t = case["truth_bucket"]
    o = {k: 1.0 if k == t else 0.0 for k in case["ordered"]}
    cq = co = rps = 0.0
    for k in case["ordered"]:
        cq += vec[k]
        co += o[k]
        rps += (cq - co) ** 2
    return {"logscore": -math.log(vec[t]), "brier": sum((vec[k] - o[k]) ** 2 for k in case["ordered"]), "crps": rps}


def fit_T(train, width, warm):
    k = 4 if width else 2
    x0 = np.zeros(k) if warm is None else np.asarray(list(warm)[:k] + [0.0] * (k - len(warm)))

    def obj(theta):
        return statistics.mean(-math.log(arm_T(c, theta, width)[c["truth_bucket"]]) for c in train)

    return minimize(obj, x0, method="Nelder-Mead", options={"maxiter": 600, "xatol": 1e-4, "fatol": 1e-6}).x


def boot(per_day):
    days = sorted(per_day)
    sums = np.array([sum(per_day[d]) for d in days])
    cnts = np.array([len(per_day[d]) for d in days])
    rng = np.random.default_rng(281)
    picks = rng.integers(0, len(days), (DRAWS, len(days)))
    stats = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def compare(rows, hi, lo, keep=lambda r: True):
    out = {}
    for sc in ("logscore", "crps", "brier"):
        per_day = defaultdict(list)
        for r in rows:
            if keep(r):
                per_day[r["day"]].append(r[f"{hi}_{sc}"] - r[f"{lo}_{sc}"])
        allv = [v for vs in per_day.values() for v in vs]
        ci = boot(per_day)
        out[sc] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)]}
        if sc == "logscore":
            out["n_days"], out["n"] = len(per_day), len(allv)
            out["days_better"] = sum(1 for v in per_day.values() if statistics.mean(v) < 0)
    ls = out["logscore"]
    out["verdict"] = ("INSUFFICIENT" if out["n_days"] < 20 else
                      "PASS" if ls["ci"][1] < 0 and out["crps"]["ci"][0] <= 0 and out["brier"]["ci"][0] <= 0 else "REFUSED")
    return out


def main() -> int:
    cases = collect()
    days = sorted({c["event_date"] for c in cases})
    print("cases", len(cases), "days", len(days), "stations", len({c["station_id"] for c in cases}), flush=True)
    rows, params = [], []
    warm1 = warm2 = None
    for day in days:
        if day < SCORE_FROM:
            continue
        train = [c for c in cases if c["event_date"] < day]
        if len({c["event_date"] for c in train}) < MIN_TRAIN_DAYS:
            continue
        line = TAFFIT.fit(train)
        artifact = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **line}
        warm1 = fit_T(train, False, warm1)
        warm2 = fit_T(train, True, warm2 if warm2 is not None else list(warm1) + [0.0, 0.0])
        params.append({"day": day, "pool": line["pool"], "T1": [round(float(x), 4) for x in warm1],
                       "T2": [round(float(x), 4) for x in warm2]})
        for c in (c for c in cases if c["event_date"] == day):
            vecs = {"R": arm_R(c), "C": arm_C(c, artifact), "T1": arm_T(c, warm1, False), "T2": arm_T(c, warm2, True)}
            row = {"day": day, "station": c["station_id"]}
            for arm, v in vecs.items():
                for k, x in scores(c, v).items():
                    row[f"{arm}_{k}"] = x
            rows.append(row)
        print(day, params[-1], flush=True)
    half = sorted({r["day"] for r in rows})[len({r["day"] for r in rows}) // 2]
    out = {"split_day": half, "pairs": {}}
    for hi, lo in (("T1", "C"), ("T2", "C"), ("T2", "T1"), ("C", "R"), ("T1", "R"), ("T2", "R")):
        out["pairs"][f"{hi}-{lo}"] = {
            "all": compare(rows, hi, lo),
            "first_half": compare(rows, hi, lo, lambda r: r["day"] < half),
            "second_half": compare(rows, hi, lo, lambda r: r["day"] >= half),
        }
    by_st = defaultdict(list)
    for r in rows:
        by_st[r["station"]].append(r)
    out["stations_better"] = {p: sum(1 for v in by_st.values() if statistics.mean(x[f"{p.split('-')[0]}_logscore"] - x[f"{p.split('-')[1]}_logscore"] for x in v) < 0)
                              for p in out["pairs"]}
    out["n_stations"] = len(by_st)
    out["mean"] = {a: round(statistics.mean(r[f"{a}_logscore"] for r in rows), 5) for a in ("R", "C", "T1", "T2")}
    out["params"] = params
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps(out["mean"]), out["stations_better"], out["n_stations"])
    for p, sl in out["pairs"].items():
        print(p, " | ".join(f"{k}: {v['logscore']['delta']:+.4f}[{v['logscore']['ci'][0]:+.4f},{v['logscore']['ci'][1]:+.4f}] rps{v['crps']['delta']:+.4f}{v['crps']['ci']} br{v['brier']['delta']:+.4f}{v['brier']['ci']} {v['n_days']}d/{v['days_better']} {v['verdict'][:4]}" for k, v in sl.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
