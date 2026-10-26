#!/usr/bin/env python3
"""Charter 282: fit the P1 TX-revision shift on settled station-days.

One record per committed P1 the layer would act on: a TX revision knowable at the
decision (received by then), issued under ``WINDOW_HOURS`` before it, the decision
between ``LOCAL_HOURS`` local.  The vector scored is the one P1 compiled before the
layer (a SERVED receipt's ``pre_shift_probabilities``).  Writes one JSON artifact
the P1 compile reads:

    kappa         fitted on the mean bucket LogScore of every record in the window
    walk_forward  each event day scored under the kappa fitted on the days before it,
                  against the unmoved vector; charter 240's verdict -- serving's
                  precondition

SSOT ``mode=ro``; writes no database.

    python3 scripts/fit_tx_revision_shift.py [--days 60] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import tx_revision_shift as TXR  # noqa: E402
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
WINDOW_DAYS = 60
MIN_TRAIN_DAYS = 7
KAPPAS = [round(0.02 * i, 2) for i in range(51)]
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 282
STATION_DAYS_SQL = (
    "SELECT DISTINCT station_id, event_date FROM calibration_forward_taf_capture"
    " WHERE tx_c IS NOT NULL AND event_date>=? AND event_date<=? ORDER BY station_id, event_date"
)
P1_SQL = (
    "SELECT decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$." + TXR.LINEAGE_KEY + "')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1'"
    " ORDER BY decision_at_utc"
)
TRUTH_SQL = (
    "SELECT settled_tmax_c FROM basket_station_day_settlement_label"
    " WHERE station_id=? AND event_date=? ORDER BY truth_revision_no DESC LIMIT 1"
)


def _json(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def collect(conn, *, start: str, end: str) -> tuple[list[dict], dict]:
    records: list[dict] = []
    counts = {"station_days": 0, "with_revision": 0, "p1_rows": 0, "in_window": 0, "no_winner": 0}
    for station, day in conn.execute(STATION_DAYS_SQL, (start, end)).fetchall():
        truth = conn.execute(TRUTH_SQL, (station, day)).fetchone()
        if truth is None or truth[0] is None:
            continue
        counts["station_days"] += 1
        bulletins = TXR.load_bulletins(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
        if len({b["tx_c"] for b in bulletins}) < 2:
            continue
        counts["with_revision"] += 1
        intervals = None
        for dec, tz, pj, oj, ivj, rj in conn.execute(P1_SQL, (station, day)).fetchall():
            counts["p1_rows"] += 1
            intervals = _json(ivj, {}) or intervals
            revision = TXR.latest_revision(bulletins, decision_at_utc=dec, event_date=day, settlement_timezone=tz)
            hour = TXR.local_hour(dec, event_date=day, settlement_timezone=tz)
            if not TXR.in_window(revision, hour) or not intervals:
                continue
            counts["in_window"] += 1
            ordinals = _json(oj, {})
            receipt = _json(rj, {})
            probs = {k: float(v) for k, v in _json(pj, {}).items()}
            if receipt.get("state") == "SERVED" and receipt.get("pre_shift_probabilities"):
                probs = {k: float(v) for k, v in receipt["pre_shift_probabilities"].items()}
            ordered = sorted(probs, key=lambda k: (ordinals.get(k, 0), k))
            if set(ordered) - set(intervals):
                continue
            try:
                winner = _winning_bucket(official_tmax_c=float(truth[0]), bucket_intervals=intervals)
            except Exception:  # noqa: BLE001
                winner = None
            if winner is None or winner not in probs or probs[winner] <= 0.0:
                counts["no_winner"] += 1
                continue
            records.append({
                "station": station, "day": day, "decision_at_utc": dec, "step_c": revision["step_c"],
                "hours_since_issue": revision["hours_since_issue"], "local_hour": hour,
                "ordered": ordered, "vector": [probs[k] for k in ordered],
                "geom": TXR.geometry(ordered, intervals), "winner": ordered.index(winner),
            })
    return records, counts


def _scores(vector, winner) -> dict[str, float]:
    q = np.asarray(vector, float)
    o = np.zeros_like(q)
    o[winner] = 1.0
    return {"logscore": float(-math.log(q[winner])), "brier": float(((q - o) ** 2).sum()),
            "crps": float(((np.cumsum(q) - np.cumsum(o)) ** 2).sum())}


def score(record: dict, kappa: float) -> dict[str, float]:
    """What serving scores: the moved vector under charter 224's floor (ruled-out buckets stay zero)."""

    moved = TXR.shift_vector(record["vector"], record["geom"], kappa * record["step_c"])
    keys = record["ordered"]
    excluded = {k for k, v in zip(keys, record["vector"]) if v <= 0.0}
    floored, _ = floor_probabilities(dict(zip(keys, moved)), excluded)
    return _scores([floored[k] for k in keys], record["winner"])


def fit_kappa(records: list[dict], table: dict[int, list[float]]) -> float:
    if not records:
        return 0.0
    ids = [id(r) for r in records]
    means = [float(np.mean([table[i][j] for i in ids])) for j in range(len(KAPPAS))]
    return KAPPAS[int(np.argmin(means))]


def _boot(per_day: dict[str, list[float]]) -> tuple[float, float]:
    days = sorted(per_day)
    sums = np.array([sum(per_day[d]) for d in days])
    cnts = np.array([len(per_day[d]) for d in days])
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    picks = rng.integers(0, len(days), (BOOTSTRAP_DRAWS, len(days)))
    stats = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def walk_forward(records: list[dict], table: dict[int, list[float]]) -> dict:
    days = sorted({r["day"] for r in records})
    per: dict[str, dict[str, list[float]]] = {k: {} for k in ("logscore", "brier", "crps")}
    path = []
    for day in days:
        train = [r for r in records if r["day"] < day]
        if len({r["day"] for r in train}) < MIN_TRAIN_DAYS:
            continue
        kappa = fit_kappa(train, table)
        path.append({"day": day, "kappa": kappa})
        for r in (r for r in records if r["day"] == day):
            moved, base = score(r, kappa), score(r, 0.0)
            for k in per:
                per[k].setdefault(day, []).append(moved[k] - base[k])
    out: dict = {"n_days": len(per["logscore"]), "n": sum(len(v) for v in per["logscore"].values()),
                 "kappa_path": path}
    if not out["n"]:
        out["verdict"] = "INSUFFICIENT"
        return out
    for k, by_day in per.items():
        allv = [x for v in by_day.values() for x in v]
        lo, hi = _boot(by_day)
        out[k] = {"delta": round(float(np.mean(allv)), 5), "ci": [round(lo, 5), round(hi, 5)]}
    out["days_better"] = sum(1 for v in per["logscore"].values() if np.mean(v) < 0)
    ls = out["logscore"]
    if out["n_days"] < TXR.VERDICT_MIN_EVENT_DAYS:
        out["verdict"] = "INSUFFICIENT"
    elif ls["ci"][1] < 0 and out["crps"]["ci"][0] <= 0 and out["brier"]["ci"][0] <= 0:
        out["verdict"] = "PASS"
    else:
        out["verdict"] = "REFUSED"
    return out


def fit(db: Path, *, now: datetime, days: int) -> dict:
    started = time.time()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    today = now.astimezone(timezone.utc).date()
    start, end = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
    try:
        records, counts = collect(conn, start=start, end=end)
    finally:
        conn.close()
    collected = time.time() - started
    table = {id(r): [score(r, k)["logscore"] for k in KAPPAS] for r in records}
    kappa = fit_kappa(records, table)
    forward = walk_forward(records, table)
    return {
        "schema": TXR.FIT_SCHEMA, "rule": TXR.RULE_VERSION,
        "fitted_at_utc": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [start, end], "window_hours": TXR.WINDOW_HOURS, "local_hours": list(TXR.LOCAL_HOURS),
        "kappa": kappa, "n_records": len(records), "n_days": len({r["day"] for r in records}),
        "n_stations": len({r["station"] for r in records}), "walk_forward": forward, "counts": counts,
        "elapsed_seconds": {"collect": round(collected, 1), "total": round(time.time() - started, 1)},
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tx_revision_shift_fit_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=1, sort_keys=True)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--out", type=Path, default=TXR.FIT_ARTIFACT_PATH)
    parser.add_argument("--days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = fit(args.db, now=datetime.now(timezone.utc), days=args.days)
    wf = {k: v for k, v in payload["walk_forward"].items() if k != "kappa_path"}
    print(json.dumps({"kappa": payload["kappa"], "n_records": payload["n_records"], "n_days": payload["n_days"],
                      "n_stations": payload["n_stations"], "walk_forward": wf, "counts": payload["counts"],
                      "elapsed": payload["elapsed_seconds"]}, ensure_ascii=False))
    if not args.dry_run:
        _write(args.out, payload)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
