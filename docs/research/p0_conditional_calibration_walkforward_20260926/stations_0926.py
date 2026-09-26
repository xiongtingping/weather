#!/usr/bin/env python3
"""Every station, event day 2026-09-26: mean, mode and seat for K1, the basket and the market.

User: 「我要所有站点9月26日的k1、篮子和市场的均值、众数及选桶。」

* Basket: the belief of the station-day's latest committed decision (production
  envelope), and the window production SEATED for it (``basket_candidate_window_
  evidence``), with the execution verdict of ``auto_trade_decisions``.
* Market: the latest committed WS book, production's ``market_reference_v1``.
* K1: the 09-23 fold's coefficients (REPORT section 3) applied to the basket's
  belief; the belief's exact zeros (hard exclusions) stay zero; served floor.
* Mean bucket, skew sign and seat for all three are production's functions
  (``mean_center_bucket_v1``, ``_skew_extension_sign``, ``seat_shape_windows_v1``;
  widest allowed shape whose rungs are all live).  K1's and the market's seats
  are the shape rule alone -- no price, edge or inventory gate.

Read-only; writes stations_0926.json and stations_0926.csv.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import centres as C  # noqa: E402
import walkforward as W  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.basket_kernel.support import mean_center_bucket_v1  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.delta_v2_runtime import _skew_extension_sign, seat_shape_windows_v1  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

DAY = "2026-09-26"
K1 = {"intercept": 0.015, "mode_p": 0.076, "meanbucket_p": -0.020, "mean_m": 0.814,
      "meanbucket_m": -0.063, "mode_m": 0.273}  # 09-23 fold, every term minus the model mean, °C


def seat(probs, ords, keys, live, stage):
    centre = mean_center_bucket_v1(probs, ords, admitted=[b for b in keys if b in live])
    sign = _skew_extension_sign(probs, ords)
    if centre is None:
        return None, None, sign
    by_ord = {ords[b]: b for b in keys}
    _shapes, audit = seat_shape_windows_v1(
        centre_ordinal=ords[centre], sign=sign, stage=stage,
        probability_by_ordinal={ords[b]: float(probs.get(b, 0.0)) for b in keys},
        live_ordinals={ords[b] for b in live})
    for shape in audit["allowed_ordinals"]:
        if all(o in by_ord and by_ord[o] in live for o in shape):
            return centre, [by_ord[o] for o in sorted(shape)], sign
    return centre, None, sign


def describe(vec, keys, centers, ords, live, stage, fahrenheit):
    probs = {k: float(v) for k, v in zip(keys, vec)}
    mean_c = float(np.dot(vec, centers))
    centre, window, sign = seat(probs, ords, keys, live, stage)
    return {"mean": round(mean_c * 9 / 5 + 32, 1) if fahrenheit else round(mean_c, 2),
            "mean_bucket": centre, "mode_bucket": keys[int(np.argmax(vec))],
            "skew": {1: "偏暖", -1: "偏冷", 0: "对称"}[int(sign)], "seat": window}


def main():
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(f"file:{W.ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    stations = sorted(r[0] for r in conn.execute("SELECT station_id FROM calibration_station_market_timezone_binding"))
    out = []
    for st in stations:
        fact = conn.execute(
            "SELECT stage, decision_at_utc, probability_envelope_id, posterior_version FROM basket_policy_training_fact"
            " WHERE station_id=? AND event_date=? ORDER BY decision_at_utc DESC LIMIT 1", (st, DAY)).fetchone()
        if fact is None:
            continue
        env = conn.execute("SELECT stage, decision_at_utc, bucket_ordinals_json, probabilities_json, posterior_version"
                           " FROM basket_probability_envelope WHERE envelope_id=?", (fact[2],)).fetchone()
        # The seat production recorded for THIS belief (same envelope instant), latest cycle first.
        seated = conn.execute(
            "SELECT stage, decision_at_utc, buckets_json, admissible FROM basket_candidate_window_evidence"
            " WHERE station_id=? AND event_date=? AND seated=1 AND decision_at_utc=?"
            " ORDER BY production_decision_at_utc DESC LIMIT 1",
            (st, DAY, env[1])).fetchone()
        verdict = conn.execute(
            "SELECT created_at, decided_state, reason_codes_json, details_json FROM auto_trade_decisions"
            " WHERE station_id=? AND event_date=? AND strategy_lane='basket' ORDER BY created_at DESC LIMIT 1", (st, DAY)).fetchone()
        book = conn.execute(
            "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot INDEXED BY basket_official_ws_snapshot_asof_idx"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
            (st, DAY, now)).fetchone()
        ladder = oos._station_day_ladder(conn, station_id=st, event_date=DAY, as_of_utc=now)
        if ladder is None or book is None:
            out.append({"station": st, "error": "no_ladder" if ladder is None else "no_book"})
            continue
        keys = list(ladder["ordered"])
        centers = np.array([float(c) for c in ladder["centers_c"]])
        ords = {k: int(v) for k, v in json.loads(env[2]).items()}
        p_env = {k: float(v) for k, v in json.loads(env[3]).items()}
        if set(p_env) != set(keys):
            out.append({"station": st, "error": "envelope_keys_differ_from_ladder"})
            continue
        fahrenheit = "°F" in keys[0] or "°F" in keys[-1]
        p = np.array([p_env[k] for k in keys])
        p = p / p.sum()
        live = {k for k in keys if p_env[k] > 0.0}
        market = market_reference_v1(json.loads(book[1]))
        if market["state"] != "complete" or set(market["vector"]) != set(keys):
            out.append({"station": st, "error": f"market_{market['state']}"})
            continue
        m = np.array([float(market["vector"][k]) for k in keys])
        m = m / m.sum()
        row = {"keys": keys, "centers": centers.tolist(), "p": p, "m": m, "open_edges": ladder["open_edge_bounds_c"]}
        positions, _masses, mean_p, _mean_m = C.centre_features(row)
        delta = K1["intercept"] + sum(K1[f] * (positions[f] - mean_p) for f in K1 if f != "intercept")
        q = W.D.as_vec(W.D.shift_tail(dict(zip(keys, p)), delta_c=delta, keys=keys, centers=list(centers),
                                      open_edges=ladder["open_edge_bounds_c"]), keys)
        q = np.array([q[i] if k in live else 0.0 for i, k in enumerate(keys)])
        floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, q / q.sum())}, [k for k in keys if k not in live])
        q = np.array([floored[k] for k in keys])
        q = q / q.sum()
        stage = str(env[0])
        details = {}
        if verdict is not None:
            try:
                details = json.loads(verdict[3] or "{}").get("basket_lifecycle") or {}
            except ValueError:
                details = {}
        out.append({
            "station": st, "stage": stage, "version": env[4], "belief_at": env[1], "book_at": book[0],
            "k1_delta_c": round(delta, 2),
            "basket": {**describe(p, keys, centers, ords, live, stage, fahrenheit),
                       "production_seat": json.loads(seated[2]) if seated else None,
                       "production_seat_stage": seated[0] if seated else None,
                       "production_seat_admissible": bool(seated[3]) if seated else None},
            "k1": describe(q, keys, centers, ords, live, stage, fahrenheit),
            "market": describe(m, keys, centers, ords, live, stage, fahrenheit),
            "execution": {"state": verdict[1] if verdict else None,
                          "reasons": json.loads(verdict[2]) if verdict else None,
                          "lifecycle": details.get("state"), "lifecycle_reason": details.get("reason")},
        })
    conn.close()
    (HERE / "stations_0926.json").write_text(json.dumps({"as_of_utc": now, "rows": out}, ensure_ascii=False, indent=1))
    with open(HERE / "stations_0926.csv", "w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["station", "stage", "basket_mean", "basket_mean_bucket", "basket_mode", "basket_skew", "basket_seat_production",
                    "k1_delta_c", "k1_mean", "k1_mean_bucket", "k1_mode", "k1_skew", "k1_seat",
                    "market_mean", "market_mean_bucket", "market_mode", "market_skew", "market_seat", "execution"])
        for r in out:
            if "error" in r:
                w.writerow([r["station"], r["error"]])
                continue
            b, k, mk = r["basket"], r["k1"], r["market"]
            w.writerow([r["station"], r["stage"], b["mean"], b["mean_bucket"], b["mode_bucket"], b["skew"],
                        "|".join(b["production_seat"] or []), r["k1_delta_c"], k["mean"], k["mean_bucket"], k["mode_bucket"],
                        k["skew"], "|".join(k["seat"] or []), mk["mean"], mk["mean_bucket"], mk["mode_bucket"], mk["skew"],
                        "|".join(mk["seat"] or []), f"{r['execution']['lifecycle'] or r['execution']['state']}"])
    print(now, len(out), "stations")


if __name__ == "__main__":
    main()
