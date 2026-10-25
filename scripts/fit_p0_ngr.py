#!/usr/bin/env python3
"""Charter 281: fit P0's distributional regression on settled station-days.

One record per station-day x band (>36 h, 12-36 h, <12 h): the band's first P0
decision, its inputs (the seated systems' calibrated vectors and families, the
radiation score, the deterministic day maxima knowable then, the hours to
settlement), the vector the older layers serve without the regression (the
fallback) and the truth.  Writes one JSON artifact the P0 compile reads:

    bias       each deterministic product's fleet bias (truth - forecast)
    theta      the model's parameters (``p0_ngr.THETA_NAMES``), fitted on the
               mean bucket LogScore of every record in the window
    walk_forward  each event day from SCORE_FROM scored under the bias and theta
               fitted on the days before it, against the fallback; charter 240's
               verdict -- serving's precondition

SSOT ``mode=ro``; writes no database.

    python3 scripts/fit_p0_ngr.py [--days 60] [--stations ZUUU,EGLC] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import p0_ngr as NGR  # noqa: E402
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket  # noqa: E402
from bayes_basket_target.regional_point_lane import runs_for  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
WINDOW_DAYS = 60
MIN_TRAIN_DAYS = 8
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 281
ENVELOPES_SQL = (
    "SELECT decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.native_system_probabilities_json'),"
    " json_extract(lineage_json,'$.system_family_ids_json'),"
    " json_extract(lineage_json,'$.synoptic_cooling_json'),"
    " json_extract(lineage_json,'$.p0_ngr_json'),"
    " json_extract(lineage_json,'$.deterministic_consensus_json')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P0'"
    " ORDER BY decision_at_utc"
)
TRUTH_SQL = (
    "SELECT event_date, truth_revision_no, settled_tmax_c FROM basket_station_day_settlement_label"
    " WHERE station_id=? AND event_date>=? AND event_date<=? ORDER BY event_date, truth_revision_no"
)


def _json(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _band(hours: float) -> str | None:
    if hours > 36.0:
        return "far"
    if hours >= 12.0:
        return "near"
    if hours > 0.0:
        return "lt12"
    return None


def _fallback(served: dict, ngr_json, consensus_json) -> dict:
    """The vector the older layers served without the regression."""

    ngr = _json(ngr_json, {})
    if ngr.get("fallback_probabilities"):
        return {k: float(v) for k, v in ngr["fallback_probabilities"].items()}
    # P0 v31 (charter 279 补记七) served the consensus shift on top; its receipt
    # kept the vector it moved.  Ages out of the window by 2026-12-24.
    old = _json(consensus_json, {})
    if old.get("state") == "SERVED" and old.get("pre_shift_probabilities"):
        return {k: float(v) for k, v in old["pre_shift_probabilities"].items()}
    return served


def collect(conn, *, start: str, end: str, stations: list[str] | None) -> tuple[list[dict], dict]:
    roster = stations or [r[0] for r in conn.execute("SELECT station_id FROM calibration_station_registry ORDER BY station_id")]
    records: list[dict] = []
    counts = {"stations": 0, "station_days": 0, "picked": 0, "no_systems": 0, "no_winner": 0}
    for station in roster:
        truth: dict[str, float] = {}
        for day, _rev, tmax in conn.execute(TRUTH_SQL, (station, start, end)):
            if tmax is not None:
                truth[str(day)] = float(tmax)
        if not truth:
            continue
        counts["stations"] += 1
        for day in sorted(truth):
            picked: dict[str, tuple] = {}
            for row in conn.execute(ENVELOPES_SQL, (station, day)):
                try:
                    hours = float(hours_to_settlement(day, as_of_utc=row[0], settlement_timezone=row[1]))
                except Exception:  # noqa: BLE001
                    continue
                band = _band(hours)
                if band is not None and band not in picked:
                    picked[band] = (hours, row)
                if len(picked) == 3:
                    break
            if not picked:
                continue
            counts["station_days"] += 1
            rows = runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            for band, (hours, (dec, tz, pj, oj, ivj, sysj, famj, radj, ngrj, consj)) in picked.items():
                counts["picked"] += 1
                ordinals, intervals = _json(oj, {}), _json(ivj, {})
                ordered = sorted(ordinals, key=lambda k: ordinals[k])
                served = {k: float(v) for k, v in _json(pj, {}).items()}
                geom = NGR.geometry(ordered, intervals)
                moments = NGR.family_moments(_json(sysj, {}), _json(famj, {}), ordered, geom)
                if not moments:
                    counts["no_systems"] += 1
                    continue
                winner = _winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                if winner is None or winner not in served:
                    counts["no_winner"] += 1
                    continue
                fallback = _fallback(served, ngrj, consj)
                z = _json(radj, {}).get("standardised")
                det = NGR.read_deterministic(conn, station_id=station, event_date=day, as_of_utc=dec,
                                             settlement_timezone=tz, rows=rows)
                records.append({
                    "station": station, "day": day, "band": band, "decision_at_utc": dec, "hours": hours,
                    "ordered": ordered, "geom": geom, "moments": moments, "winner": ordered.index(winner),
                    "fallback": [fallback[k] for k in ordered], "y_c": truth[day],
                    "rad": float(z) if isinstance(z, (int, float)) else 0.0,
                    "det": {k: float(v["day_max_c"]) for k, v in det["systems"].items()},
                })
    return records, counts


def fit_bias(records: list[dict]) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for r in records:
        for k, v in r["det"].items():
            acc.setdefault(k, []).append(r["y_c"] - v)
    return {k: round(statistics.mean(v), 4) for k, v in acc.items()}


class Batch:
    """The records with enough inputs, as arrays for ``predict_batch``."""

    def __init__(self, records: list[dict], bias: dict[str, float]):
        keep, feats = [], []
        for r in records:
            f = NGR.det_features(r["det"], bias)
            if f is not None:
                keep.append(r)
                feats.append(f)
        self.records = keep
        n, K, nf = len(keep), max((len(r["ordered"]) for r in keep), default=1), len(NGR.FAMILIES)
        self.m, self.s, self.has = np.zeros((n, nf)), np.ones((n, nf)), np.zeros((n, nf), bool)
        self.tlo, self.thi = np.full((n, K), np.inf), np.full((n, K), np.inf)
        self.mask, self.fb = np.zeros((n, K), bool), np.zeros((n, K))
        self.win = np.array([r["winner"] for r in keep], int)
        self.F = np.array([f[0] for f in feats])
        self.Z = np.array([f[1] for f in feats])
        self.L = np.array([(r["hours"] - 36.0) / 24.0 for r in keep])
        self.rad = np.array([r["rad"] for r in keep])
        for i, r in enumerate(keep):
            k = len(r["ordered"])
            self.tlo[i, :k], self.thi[i, :k], self.mask[i, :k] = r["geom"]["tlo"], r["geom"]["thi"], True
            self.fb[i, :k] = np.asarray(r["fallback"]) / sum(r["fallback"])
            for j, fam in enumerate(NGR.FAMILIES):
                if fam in r["moments"]:
                    self.m[i, j], self.s[i, j] = r["moments"][fam]
                    self.has[i, j] = True

    def predict(self, theta):
        return NGR.predict_batch(theta, m=self.m, s=self.s, has=self.has, F=self.F, Z=self.Z, L=self.L,
                                 rad=self.rad, tlo=self.tlo, thi=self.thi, mask=self.mask)[0]

    def scores(self, q) -> dict[str, np.ndarray]:
        idx = np.arange(len(q))
        o = np.zeros_like(q)
        o[idx, self.win] = 1.0
        return {
            "logscore": -np.log(q[idx, self.win]),
            "brier": ((q - o) ** 2).sum(1),
            "crps": ((np.cumsum(q, 1) - np.cumsum(o, 1)) ** 2 * self.mask).sum(1),
        }


def fit_theta(batch: Batch, warm=None) -> np.ndarray:
    x0 = NGR.initial_theta() if warm is None else np.asarray(warm, float)
    res = minimize(lambda t: float(batch.scores(batch.predict(t))["logscore"].mean()), x0,
                   method="L-BFGS-B", options={"maxiter": 300})
    return res.x


def _boot(per_day: dict[str, list[float]]) -> tuple[float, float]:
    days = sorted(per_day)
    sums = np.array([sum(per_day[d]) for d in days])
    cnts = np.array([len(per_day[d]) for d in days])
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    picks = rng.integers(0, len(days), (BOOTSTRAP_DRAWS, len(days)))
    stats = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def walk_forward(records: list[dict]) -> dict:
    """Each event day from SCORE_FROM under bias and theta fitted on the days before it, against the fallback."""

    days = sorted({r["day"] for r in records})
    per: dict[str, dict[str, list[float]]] = {k: {} for k in ("logscore", "brier", "crps")}
    bands: dict[str, list[float]] = {}
    warm = None
    for day in days:
        test = [r for r in records if r["day"] == day and r["decision_at_utc"] >= NGR.SCORE_FROM_DECISION_UTC]
        train = [r for r in records if r["day"] < day]
        if not test or len({r["day"] for r in train}) < MIN_TRAIN_DAYS:
            continue
        bias = fit_bias(train)
        warm = fit_theta(Batch(train, bias), warm)
        bt = Batch(test, bias)
        if not bt.records:
            continue
        s_ngr, s_fb = bt.scores(bt.predict(warm)), bt.scores(bt.fb)
        for k in per:
            per[k][day] = list(s_ngr[k] - s_fb[k])
        for r, d in zip(bt.records, s_ngr["logscore"] - s_fb["logscore"]):
            bands.setdefault(r["band"], []).append(float(d))
    out: dict = {"n_days": len(per["logscore"]), "n": sum(len(v) for v in per["logscore"].values())}
    if not out["n"]:
        out["verdict"] = "INSUFFICIENT"
        return out
    for k, by_day in per.items():
        allv = [x for v in by_day.values() for x in v]
        lo, hi = _boot(by_day)
        out[k] = {"delta": round(float(np.mean(allv)), 5), "ci": [round(lo, 5), round(hi, 5)]}
    out["days_better"] = sum(1 for v in per["logscore"].values() if np.mean(v) < 0)
    out["by_band"] = {b: {"n": len(v), "delta_logscore": round(float(np.mean(v)), 5)} for b, v in sorted(bands.items())}
    ls = out["logscore"]
    if out["n_days"] < NGR.VERDICT_MIN_EVENT_DAYS:
        out["verdict"] = "INSUFFICIENT"
    elif ls["ci"][1] < 0 and out["crps"]["ci"][0] <= 0 and out["brier"]["ci"][0] <= 0:
        out["verdict"] = "PASS"
    else:
        out["verdict"] = "REFUSED"
    return out


def fit(db: Path, *, now: datetime, stations: list[str] | None, days: int) -> dict:
    started = time.time()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    today = now.astimezone(timezone.utc).date()
    start, end = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
    try:
        records, counts = collect(conn, start=start, end=end, stations=stations)
    finally:
        conn.close()
    collected = time.time() - started
    bias = fit_bias(records)
    batch = Batch(records, bias)
    theta = fit_theta(batch)
    forward = walk_forward(records)
    return {
        "schema": NGR.FIT_SCHEMA, "rule": NGR.RULE_VERSION,
        "fitted_at_utc": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [start, end], "families": list(NGR.FAMILIES), "theta_names": list(NGR.THETA_NAMES),
        "theta": [round(float(t), 6) for t in theta], "bias": bias,
        "n_records": len(batch.records), "n_days": len({r["day"] for r in batch.records}),
        "score_from_decision_utc": NGR.SCORE_FROM_DECISION_UTC, "walk_forward": forward,
        "counts": counts, "elapsed_seconds": {"collect": round(collected, 1), "total": round(time.time() - started, 1)},
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".p0_ngr_fit_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=1, sort_keys=True)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--out", type=Path, default=NGR.FIT_ARTIFACT_PATH)
    parser.add_argument("--stations", default="")
    parser.add_argument("--days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()] or None
    payload = fit(args.db, now=datetime.now(timezone.utc), stations=stations, days=args.days)
    wf = payload["walk_forward"]
    print(json.dumps({"n_records": payload["n_records"], "n_days": payload["n_days"],
                      "theta": dict(zip(NGR.THETA_NAMES, payload["theta"])), "walk_forward": wf,
                      "elapsed": payload["elapsed_seconds"]}, ensure_ascii=False))
    if not args.dry_run:
        _write(args.out, payload)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
