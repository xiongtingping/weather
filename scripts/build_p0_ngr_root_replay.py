#!/usr/bin/env python3
"""Rebuild every settled P0 since the calibrated vectors entered the receipts as
charter 281's regression would have served it then, strictly as of its day.

For each event day D: per-product bias and theta fitted (``fit_p0_ngr``) on the
event days before D only (at least ``fit_p0_ngr.MIN_TRAIN_DAYS``), then each of
D's P0 envelopes rebuilt by ``p0_ngr.predict_batch`` from its own receipt
inputs and the deterministic forecasts knowable at its decision.  Days without
enough history are skipped.  One sqlite file, checkpoint -> mu, sd, vector; the
P1 likelihood refit reads its roots from it (``docs/research/root_refit_20261025``;
P1 roots on P0 since charter 283).  SSOT ``mode=ro``, one connection a day.

    python3 scripts/build_p0_ngr_root_replay.py [--since 2026-09-21] [--through 2026-10-24]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_p0_ngr as FIT  # noqa: E402
from bayes_basket_target import p0_ngr as NGR  # noqa: E402
from bayes_basket_target.regional_point_lane import runs_for  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

OUT = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "p0_ngr_root_replay.db"
DAY_SQL = (
    "SELECT station_id, checkpoint_id, decision_at_utc, settlement_timezone, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.native_system_probabilities_json'),"
    " json_extract(lineage_json,'$.system_family_ids_json'),"
    " json_extract(lineage_json,'$.synoptic_cooling_json')"
    " FROM basket_probability_envelope"
    " WHERE event_date=? AND stage='P0' AND state='COMMITTED'"
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS root (
  checkpoint_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT, decision_at_utc TEXT,
  mu_c REAL, sd_c REAL, probabilities_json TEXT, theta_fitted_through TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


P1_ROOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS p1_root (
  checkpoint_id TEXT PRIMARY KEY, stage TEXT, event_date TEXT, probabilities_json TEXT, taf_artifact_sha256 TEXT
);
"""


#: The first event day P0 served the regression (charter 281, P0 v32 at
#: 2026-10-25T06:26Z); from here the store copies what was served.
LIVE_FROM = "2026-10-25"
LIVE_P0_SQL = (
    "SELECT checkpoint_id, station_id, decision_at_utc, probabilities_json,"
    " json_extract(lineage_json,'$.p0_ngr_json')"
    " FROM basket_probability_envelope WHERE event_date=? AND stage='P0' AND state='COMMITTED'"
)
def copy_live(out: sqlite3.Connection, *, days: list[str]) -> int:
    """Days the regression served: each P0 as served (vector, receipt mu/sd), and
    the same vector as the root P1 multiplies -- P1 roots on P0 since charter 283.
    The ``p1_root`` rows of the P0-M-rooted history (charter 281 补记一, built
    before P0-M was retired) stay in the store until they leave the fit window."""

    out.executescript(P1_ROOT_SCHEMA)
    n0 = 0
    for day in days:
        conn = _connect()
        try:
            for ck, station, dec, pj, rj in conn.execute(LIVE_P0_SQL, (day,)).fetchall():
                receipt = FIT._json(rj, {})
                if receipt.get("state") != "SERVED":
                    continue
                out.execute("INSERT OR REPLACE INTO root VALUES (?,?,?,?,?,?,?,?)",
                            (ck, station, day, dec, receipt.get("mu_c"), receipt.get("sd_c"), pj, "served"))
                n0 += 1
            out.execute(
                "INSERT OR REPLACE INTO p1_root SELECT checkpoint_id, 'P0', event_date, probabilities_json, ''"
                " FROM root WHERE event_date=?", (day,),
            )
        finally:
            conn.close()
        out.commit()
    return n0


def _connect():
    conn = sqlite3.connect(f"file:{FIT.DB}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    return conn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-09-21")
    parser.add_argument("--through", default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--live", action="store_true",
                        help="copy the served roots of the days since LIVE_FROM (the daily refits' step)")
    args = parser.parse_args()
    if args.live:
        out = sqlite3.connect(args.out)
        out.executescript(SCHEMA)
        today = datetime.now(timezone.utc).date()
        days = [(date.fromisoformat(LIVE_FROM) + timedelta(days=i)).isoformat()
                for i in range((today - date.fromisoformat(LIVE_FROM)).days + 4)]
        n0 = copy_live(out, days=days)
        out.close()
        print("live P0 roots", n0)
        return 0
    t0 = time.time()
    conn = _connect()
    try:
        records, counts = FIT.collect(conn, start=args.since, end=args.through, stations=None)
    finally:
        conn.close()
    print("training records", len(records), counts, round(time.time() - t0, 1), flush=True)
    out = sqlite3.connect(args.out)
    out.executescript(SCHEMA)
    days = []
    d = date.fromisoformat(args.since)
    while d <= date.fromisoformat(args.through):
        days.append(d.isoformat())
        d += timedelta(days=1)
    warm = None
    total = 0
    for day in days:
        train = [r for r in records if r["day"] < day]
        if len({r["day"] for r in train}) < FIT.MIN_TRAIN_DAYS:
            continue
        bias = FIT.fit_bias(train)
        warm = FIT.fit_theta(FIT.Batch(train, bias), warm)
        conn = _connect()
        try:
            rows = conn.execute(DAY_SQL, (day,)).fetchall()
            det_rows: dict[str, list] = {}
            batch_rows = []
            for station, ck, dec, tz, oj, ivj, sysj, famj, radj in rows:
                ordinals, intervals = FIT._json(oj, {}), FIT._json(ivj, {})
                ordered = sorted(ordinals, key=lambda k: ordinals[k])
                if not ordered or set(ordered) - set(intervals):
                    continue
                geom = NGR.geometry(ordered, intervals)
                moments = NGR.family_moments(FIT._json(sysj, {}), FIT._json(famj, {}), ordered, geom)
                if not moments:
                    continue
                try:
                    hours = float(hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tz))
                except Exception:  # noqa: BLE001
                    continue
                if station not in det_rows:
                    det_rows[station] = runs_for(conn, station_id=station, event_date=day,
                                                 as_of_utc="9999-12-31T23:59:59Z")
                det = NGR.read_deterministic(conn, station_id=station, event_date=day, as_of_utc=dec,
                                             settlement_timezone=tz, rows=det_rows[station])
                values = {k: float(v["day_max_c"]) for k, v in det["systems"].items()}
                feats = NGR.det_features(values, bias)
                if feats is None:
                    continue
                z = FIT._json(radj, {}).get("standardised")
                batch_rows.append((station, ck, dec, ordered, geom, moments, feats, hours,
                                   float(z) if isinstance(z, (int, float)) else 0.0))
        finally:
            conn.close()
        for station, ck, dec, ordered, geom, moments, (F, Z), hours, rad in batch_rows:
            q, mu, sd = NGR.predict_batch(
                warm, m=np.array([[moments.get(f, [0.0, 1.0])[0] for f in NGR.FAMILIES]]),
                s=np.array([[moments.get(f, [0.0, 1.0])[1] for f in NGR.FAMILIES]]),
                has=np.array([[f in moments for f in NGR.FAMILIES]]), F=np.array([F]), Z=np.array([Z]),
                L=np.array([(hours - 36.0) / 24.0]), rad=np.array([rad]), tlo=np.array([geom["tlo"]]),
                thi=np.array([geom["thi"]]), mask=np.ones((1, len(ordered)), bool),
            )
            out.execute(
                "INSERT OR REPLACE INTO root VALUES (?,?,?,?,?,?,?,?)",
                (ck, station, day, dec, float(mu[0]), float(sd[0]),
                 json.dumps({k: float(q[0, i]) for i, k in enumerate(ordered)}), max(r["day"] for r in train)),
            )
        out.commit()
        total += len(batch_rows)
        print(day, len(rows), len(batch_rows), round(time.time() - t0, 1), flush=True)
    out.execute("INSERT OR REPLACE INTO meta VALUES ('built_at_utc', ?)",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    out.commit()
    out.close()
    print("roots", total, "seconds", round(time.time() - t0, 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
