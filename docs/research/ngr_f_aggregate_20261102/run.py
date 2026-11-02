#!/usr/bin/env python3
"""F as median, mean or trimmed mean on the same walk-forward (PROTOCOL.md).

Collects the production daily fit's records once (``scripts/fit_p0_ngr.collect``,
SSOT mode=ro), then runs its walk-forward three times with only F's aggregate
swapped in ``p0_ngr.det_features``, keeping every scored decision's scores so
the arms can be compared decision by decision, clustered by event day.

    python3 docs/research/ngr_f_aggregate_20261102/run.py [--days 60] [--records records.pkl]
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import fit_p0_ngr as FIT  # noqa: E402
from bayes_basket_target import p0_ngr as NGR  # noqa: E402

ORIGINAL = NGR.det_features


def _aggregate(kind: str):
    def features(values, bias):
        corrected = sorted(float(v) + float(bias.get(k, 0.0)) for k, v in values.items() if k in NGR.DET_PRODUCTS)
        if len(corrected) < NGR.MIN_DET:
            return None
        if kind == "MED":
            f = statistics.median(corrected)
        elif kind == "MEAN":
            f = statistics.mean(corrected)
        else:  # TRIM1: drop one highest and one lowest
            f = statistics.mean(corrected[1:-1])
        return f, math.log(float(np.std(corrected)) + 0.2)
    return features


def walk(records: list[dict]) -> dict[tuple, dict[str, float]]:
    """``FIT.walk_forward``'s loop, keeping each scored decision's scores (and the fallback's)."""

    out: dict[tuple, dict[str, float]] = {}
    warm = None
    for day in sorted({r["day"] for r in records}):
        test = [r for r in records if r["day"] == day and r["decision_at_utc"] >= NGR.SCORE_FROM_DECISION_UTC]
        train = [r for r in records if r["day"] < day]
        if not test or len({r["day"] for r in train}) < FIT.MIN_TRAIN_DAYS:
            continue
        bias = FIT.fit_bias(train)
        warm = FIT.fit_theta(FIT.Batch(train, bias), warm)
        bt = FIT.Batch(test, bias)
        if not bt.records:
            continue
        s, fb = bt.scores(bt.predict(warm)), bt.scores(bt.fb)
        for i, r in enumerate(bt.records):
            out[(r["station"], r["day"], r["band"])] = {
                **{k: float(s[k][i]) for k in s}, **{"fb_" + k: float(fb[k][i]) for k in fb}}
    return out


def compare(a: dict, b: dict, key_a: str = "", key_b: str = "") -> dict:
    """Arm a minus arm b on the decisions both scored, clustered by event day; charter 240's verdict for a."""

    common = sorted(set(a) & set(b))
    res: dict = {"n": len(common), "n_days": len({k[1] for k in common})}
    if not common:
        res["verdict"] = "INSUFFICIENT"
        return res
    for score in ("logscore", "crps", "brier"):
        per: dict[str, list[float]] = {}
        for k in common:
            per.setdefault(k[1], []).append(a[k][key_a + score] - b[k][key_b + score])
        allv = [x for v in per.values() for x in v]
        lo, hi = FIT._boot(per)
        res[score] = {"delta": round(float(np.mean(allv)), 5), "ci": [round(lo, 5), round(hi, 5)]}
        if score == "logscore":
            res["days_better"] = sum(1 for v in per.values() if np.mean(v) < 0)
    bands: dict[str, list[float]] = {}
    for k in common:
        bands.setdefault(k[2], []).append(a[k][key_a + "logscore"] - b[k][key_b + "logscore"])
    res["by_band"] = {b_: {"n": len(v), "delta_logscore": round(float(np.mean(v)), 5)} for b_, v in sorted(bands.items())}
    ls = res["logscore"]
    if res["n_days"] < NGR.VERDICT_MIN_EVENT_DAYS:
        res["verdict"] = "INSUFFICIENT"
    elif ls["ci"][1] < 0 and res["crps"]["ci"][0] <= 0 and res["brier"]["ci"][0] <= 0:
        res["verdict"] = "PASS"
    else:
        res["verdict"] = "REFUSED"
    res["reverse_significant"] = ls["ci"][0] > 0  # b better than a, the interval wholly above 0
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=FIT.WINDOW_DAYS)
    ap.add_argument("--records", type=Path, default=HERE / "records.pkl")
    args = ap.parse_args()
    if args.records.exists():
        records, counts, window = pickle.loads(args.records.read_bytes())
    else:
        today = datetime.now(timezone.utc).date()
        window = [(today - timedelta(days=args.days)).isoformat(), (today - timedelta(days=1)).isoformat()]
        conn = sqlite3.connect(f"file:{FIT.DB}?mode=ro", uri=True, timeout=120)
        conn.execute("PRAGMA query_only=ON")
        try:
            records, counts = FIT.collect(conn, start=window[0], end=window[1], stations=None)
        finally:
            conn.close()
        args.records.write_bytes(pickle.dumps((records, counts, window)))
    arms = {}
    for kind in ("MED", "MEAN", "TRIM1"):
        NGR.det_features = _aggregate(kind)
        try:
            arms[kind] = walk(records)
        finally:
            NGR.det_features = ORIGINAL
        print(kind, len(arms[kind]), flush=True)
    result = {
        "window": window, "counts": counts, "n_records": len(records),
        "score_from_decision_utc": NGR.SCORE_FROM_DECISION_UTC,
        "vs_fallback": {k: compare(v, v, "", "fb_") for k, v in arms.items()},
        "MEAN_minus_MED": compare(arms["MEAN"], arms["MED"]),
        "TRIM1_minus_MED": compare(arms["TRIM1"], arms["MED"]),
        "MEAN_minus_TRIM1": compare(arms["MEAN"], arms["TRIM1"]),
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "counts"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
