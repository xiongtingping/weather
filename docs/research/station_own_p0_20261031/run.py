"""Per-station P0 (user 2026-10-31 「所有模型，按照单站点重新优化，重新拟合参数，也就是每个站点一套自己的」, P0 after F).

Read-only on the SSOT.  ``collect`` reads the records production's daily fit
reads (``scripts/fit_p0_ngr.collect``: each settled station-day x band's first
P0 decision, its inputs and the truth) into ``records.pkl``; ``score`` walks
every event day from production's SCORE_FROM forward, fitting on the days
before it exactly as ``fit_p0_ngr.walk_forward`` does, and compares production's
one fleet regression against per-station variants:

  fleet          production: fleet deterministic bias, one theta
  st_bias        each station's own deterministic bias (>= 5 of its records, else
                 the fleet's), one theta refitted on those features
  st_offset_k    fleet theta + the station's own centre and log-sd offsets (a_s,
                 c0_s), fitted on its own records with a ridge k toward zero
  st_full_k      the whole theta fitted on the station's own records, ridge k
                 toward the fleet theta (k = 0: the station alone)
  tuned          each station's pick among the above by its own earlier walk days

    .venv/bin/python -I docs/research/station_own_p0_20261031/run.py collect
    .venv/bin/python -I docs/research/station_own_p0_20261031/run.py score
"""

from __future__ import annotations

import json
import pickle
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fit_p0_ngr as FIT  # noqa: E402
from bayes_basket_target import p0_ngr as NGR  # noqa: E402

START, END = "2026-08-01", "2026-10-29"
MIN_OWN_BIAS = 5
OFFSET_RIDGES = (0.02, 0.2)
FULL_RIDGES = (0.0, 0.05, 0.5)
TUNE_MIN_DAYS, TUNE_DAYS = 7, 21
NF = len(NGR.FAMILIES)
IA, IC0 = NF, NF + 3


def collect() -> None:
    conn = sqlite3.connect(f"file:{FIT.DB}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    records, counts = FIT.collect(conn, start=START, end=END, stations=None)
    print(counts, len(records))
    (HERE / "records.pkl").write_bytes(pickle.dumps(records))


def own_bias(train, fleet_bias):
    by = defaultdict(lambda: defaultdict(list))
    for r in train:
        for k, v in r["det"].items():
            by[r["station"]][k].append(r["y_c"] - v)
    out = {}
    for st, d in by.items():
        out[st] = {k: (statistics.mean(v) if len(v) >= MIN_OWN_BIAS else fleet_bias.get(k, 0.0)) for k, v in d.items()}
    return out


class StationBatch(FIT.Batch):
    """Batch whose deterministic features use each station's own bias."""

    def __init__(self, records, biases, fleet_bias):
        keep, feats = [], []
        for r in records:
            f = NGR.det_features(r["det"], biases.get(r["station"], fleet_bias))
            if f is not None:
                keep.append(r)
                feats.append(f)
        # Reuse the parent's array build on the kept rows, then swap in these features.
        super().__init__(keep, {})
        # Parent recomputed F/Z with no bias and may have dropped none (same count); overwrite.
        self.records = keep
        self.F = np.array([f[0] for f in feats])
        self.Z = np.array([f[1] for f in feats])


def fit_station(batch, base, *, free, ridge, x0=None):
    """Fit ``free`` indices of theta on ``batch``, the rest fixed at ``base``; ridge toward ``base``."""

    base = np.asarray(base, float)
    idx = np.asarray(free)

    def obj(x):
        t = base.copy()
        t[idx] = x
        return float(batch.scores(batch.predict(t))["logscore"].mean()) + ridge * float(((x - base[idx]) ** 2).sum())

    res = minimize(obj, base[idx] if x0 is None else x0, method="L-BFGS-B", options={"maxiter": 200})
    t = base.copy()
    t[idx] = res.x
    return t


def score() -> None:
    records = pickle.loads((HERE / "records.pkl").read_bytes())
    days = sorted({r["day"] for r in records})
    stations = sorted({r["station"] for r in records})
    variants = (["fleet", "st_bias"] + [f"st_offset_{k}" for k in OFFSET_RIDGES]
                + [f"st_full_{k}" for k in FULL_RIDGES])
    rows = []  # one per test record: station, day, band, logscore/crps per variant
    warm_fleet = warm_sb = None
    warm_st: dict[tuple[str, str], np.ndarray] = {}
    for day in days:
        test = [r for r in records if r["day"] == day and r["decision_at_utc"] >= NGR.SCORE_FROM_DECISION_UTC]
        train = [r for r in records if r["day"] < day]
        if not test or len({r["day"] for r in train}) < FIT.MIN_TRAIN_DAYS:
            continue
        fbias = FIT.fit_bias(train)
        warm_fleet = FIT.fit_theta(FIT.Batch(train, fbias), warm_fleet)
        sbias = own_bias(train, fbias)
        warm_sb = FIT.fit_theta(StationBatch(train, sbias, fbias), warm_sb)
        by_st_train = defaultdict(list)
        for r in train:
            by_st_train[r["station"]].append(r)
        for st in sorted({r["station"] for r in test}):
            tst = [r for r in test if r["station"] == st]
            bt = FIT.Batch(tst, fbias)
            if not bt.records:
                continue
            res = {"fleet": bt.scores(bt.predict(warm_fleet))}
            bsb = StationBatch(tst, sbias, fbias)
            res["st_bias"] = bsb.scores(bsb.predict(warm_sb)) if len(bsb.records) == len(bt.records) else res["fleet"]
            own = FIT.Batch(by_st_train.get(st, []), fbias)
            for k in OFFSET_RIDGES:
                name = f"st_offset_{k}"
                if len(own.records) < 20:
                    res[name] = res["fleet"]
                    continue
                t = fit_station(own, warm_fleet, free=[IA, IC0], ridge=k, x0=warm_st.get((st, name)))
                warm_st[(st, name)] = t[[IA, IC0]]
                res[name] = bt.scores(bt.predict(t))
            for k in FULL_RIDGES:
                name = f"st_full_{k}"
                if len(own.records) < 20:
                    res[name] = res["fleet"]
                    continue
                free = list(range(NGR.N_THETA))
                t = fit_station(own, warm_fleet, free=free, ridge=k, x0=warm_st.get((st, name)))
                warm_st[(st, name)] = t
                res[name] = bt.scores(bt.predict(t))
            for i, r in enumerate(bt.records):
                rows.append({"station": st, "day": day, "band": r["band"],
                             "ls": {v: float(res[v]["logscore"][i]) for v in variants},
                             "crps": {v: float(res[v]["crps"][i]) for v in variants},
                             "brier": {v: float(res[v]["brier"][i]) for v in variants}})
        print(day, len(rows), flush=True)
    # Tuned: per station, each day the variant with the lowest mean LogScore over
    # its own earlier walk days (days strictly before, last TUNE_DAYS).
    by_st = defaultdict(list)
    for r in rows:
        by_st[r["station"]].append(r)
    for st, rs in by_st.items():
        sdays = sorted({r["day"] for r in rs})
        for d in sdays:
            prior = [r for r in rs if r["day"] < d and r["day"] >= sdays[max(0, sdays.index(d) - TUNE_DAYS)]]
            pick = "fleet"
            if len({r["day"] for r in prior}) >= TUNE_MIN_DAYS:
                pick = min(variants, key=lambda v: statistics.mean(r["ls"][v] for r in prior))
            for r in rs:
                if r["day"] == d:
                    for m in ("ls", "crps", "brier"):
                        r[m]["tuned"] = r[m][pick]
                    r["pick"] = pick
    (HERE / "walk.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    report(rows, variants + ["tuned"])


def boot(rows, a, b, metric):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[metric][a] - r[metric][b])
    return per, FIT._boot(per)


def report(rows, variants):
    days = sorted({r["day"] for r in rows})
    print(f"walk {len(rows)} records, {len(days)} event days {days[0]}..{days[-1]}, {len({r['station'] for r in rows})} stations")
    print(f"{'variant':16s} {'LogScore':>9s} {'CRPS':>7s}  ΔLogScore vs fleet [95%] halves | ΔCRPS [95%] | ΔBrier [95%]")
    mid = days[len(days) // 2]
    for v in variants:
        ls = statistics.mean(r["ls"][v] for r in rows)
        cr = statistics.mean(r["crps"][v] for r in rows)
        line = f"{v:16s} {ls:9.4f} {cr:7.4f}"
        if v != "fleet":
            per, (lo, hi) = boot(rows, v, "fleet", "ls")
            d = statistics.mean(x for xs in per.values() for x in xs)
            h = [statistics.mean(x for dd, xs in per.items() if (dd < mid) == side for x in xs) for side in (True, False)]
            _p, (clo, chi) = boot(rows, v, "fleet", "crps")
            cd = statistics.mean(r["crps"][v] - r["crps"]["fleet"] for r in rows)
            _p, (blo, bhi) = boot(rows, v, "fleet", "brier")
            bd = statistics.mean(r["brier"][v] - r["brier"]["fleet"] for r in rows)
            line += (f"  {d:+.4f} [{lo:+.4f}, {hi:+.4f}] {h[0]:+.4f}/{h[1]:+.4f}"
                     f" | {cd:+.4f} [{clo:+.4f}, {chi:+.4f}] | {bd:+.4f} [{blo:+.4f}, {bhi:+.4f}]")
        print(line)
    picks = defaultdict(int)
    for r in rows:
        picks[r.get("pick", "fleet")] += 1
    print("tuned picks:", dict(sorted(picks.items(), key=lambda x: -x[1])))
    st = defaultdict(list)
    for r in rows:
        st[r["station"]].append(r)
    better = {v: 0 for v in variants if v != "fleet"}
    print("\nper station LogScore (fleet | tuned | st_offset_0.2 | st_full_0.5):")
    for s, rs in sorted(st.items()):
        m = {v: statistics.mean(r["ls"][v] for r in rs) for v in variants}
        for v in better:
            better[v] += m[v] < m["fleet"]
        print(f"  {s} n={len(rs):3d} {m['fleet']:.3f} | {m['tuned']:.3f} | {m['st_offset_0.2']:.3f} | {m['st_full_0.5']:.3f}")
    print("stations better than fleet:", better, "of", len(st))


if __name__ == "__main__":
    {"collect": collect, "score": score}[sys.argv[1]]()
