"""Strict forward read of the realisation layer's P0 shrinkage k.

Locked by PROTOCOL.md before any evidence day (event date >= 2026-10-17)
settled.  Read-only on the SSOT; writes ``reading_<UTC>.json`` next to itself.
Run from the repo root:

    python3 docs/research/realisation_shrink_k_20261017/read.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import realisation_layer as layer  # noqa: E402
from bayes_basket_target import realisation_store as rs  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
FREEZE_UTC = "2026-10-16T15:47:02Z"
EVIDENCE_FROM = "2026-10-17"
POPULATION_FROM = date(2026, 9, 10)
STAGE = "P0"
TRAIN_DAYS = 28
INNER_DAYS = 28
MIN_INNER_DAYS = 5
K_GRID = tuple(round(0.05 * i, 2) for i in range(21))
READY_DAYS, READY_STATION_DAYS = 10, 300
START = np.asarray(layer.START, dtype=float)


def _shrunk(theta: np.ndarray, k: float) -> np.ndarray:
    return START + k * (theta - START)


def _theta_by_day(dec: list[dict]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """theta_d (v2, supported columns) and the market-only theta per day d:
    event days in (d-2-TRAIN_DAYS, d-2], labels known by d's midnight."""

    out = {}
    for day in sorted({d["event_date"] for d in dec}):
        cut = date.fromisoformat(day) - timedelta(days=2)
        floor = (cut - timedelta(days=TRAIN_DAYS)).isoformat()
        midnight = f"{day}T00:00:00Z"
        prefix = [d for d in dec if floor < d["event_date"] <= cut.isoformat()
                  and str(d["label_known_at"]) <= midnight]
        if len({d["event_date"] for d in prefix}) < rs.MIN_PREFIX_DAYS:
            continue
        arrays = layer.stack(prefix)
        theta, _cols = rs.fit_supported(arrays)
        cols = list(rs.MARKET_ONLY_COLUMNS)
        theta_m = layer.fit(rs._columns(arrays, cols), start=[layer.START[c] for c in cols])
        out[day] = (theta, theta_m)
    return out


def _choose_k(day: str, dec: list[dict], by_day: dict[str, list[int]], thetas: dict) -> tuple[float, int]:
    cut = (date.fromisoformat(day) - timedelta(days=2)).isoformat()
    midnight = f"{day}T00:00:00Z"
    inner = sorted(d for d in thetas if d <= cut)[-INNER_DAYS:]
    rows = [(i, d) for d in inner for i in by_day[d] if str(dec[i]["label_known_at"]) <= midnight]
    if len({d for _i, d in rows}) < MIN_INNER_DAYS:
        return 1.0, len({d for _i, d in rows})
    best_k, best = 1.0, math.inf
    for k in K_GRID:
        nll = sum(dec[i]["weight"] * -math.log(layer.belief(dec[i]["terms"], _shrunk(thetas[d][0], k))[dec[i]["y"]])
                  for i, d in rows)
        if nll < best - 1e-12 or (abs(nll - best) <= 1e-12 and k > best_k):
            best_k, best = k, nll
    return best_k, len({d for _i, d in rows})


def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        window = (datetime.now(timezone.utc).date() - POPULATION_FROM).days
        dec, counts = rs.population(conn, cutoff_utc=now, window_days=window, stage=STAGE)
    finally:
        conn.close()
    by_day: dict[str, list[int]] = {}
    for i, d in enumerate(dec):
        by_day.setdefault(d["event_date"], []).append(i)
    thetas = _theta_by_day(dec)

    def forward(days: list[str], ks: dict[str, float]) -> dict:
        cand, base, mkt = {}, {}, {}
        for day in days:
            theta, theta_m = thetas[day]
            for i in by_day[day]:
                terms = dec[i]["terms"]
                cand[i] = layer.belief(terms, _shrunk(theta, ks[day]))
                base[i] = layer.belief(terms, theta)
                mkt[i] = layer.belief(terms[:, list(rs.MARKET_ONLY_COLUMNS)], theta_m)
        # "without_change" carries the baseline k=1, so the gate's
        # layer_minus_layer_without_change is criterion C (candidate - baseline).
        return {"days": days, "layer": cand, "market_only": mkt, "without_change": base}

    def baseline(days: list[str]) -> dict:
        fw = forward(days, {d: 1.0 for d in days})
        return {**fw, "without_change": fw["layer"]}

    path, sections = [], {}
    ks = {}
    for day in sorted(thetas):
        k, n_inner = _choose_k(day, dec, by_day, thetas)
        ks[day] = k
        path.append({"day": day, "k": k, "inner_days": n_inner,
                     "station_days": round(sum(dec[i]["weight"] for i in by_day[day]), 3)})
    for name, keep in (("self_check_pre_freeze", lambda d: "2026-10-01" <= d < EVIDENCE_FROM),
                       ("evidence", lambda d: d >= EVIDENCE_FROM)):
        days = [d for d in sorted(thetas) if keep(d)]
        station_days = sum(dec[i]["weight"] for d in days for i in by_day[d])
        sec = {"event_days": days, "station_days": round(station_days, 3)}
        if days:
            cand = rs.calibration_gate(dec, forward(days, ks))
            base = rs.calibration_gate(dec, baseline(days))
            zero = rs.calibration_gate(dec, forward(days, {d: 0.0 for d in days}))
            for g in (cand, base, zero):
                g["cells_outside_list"] = [c for c in g.pop("cells", []) if c["outside"]]
            sec["candidate"], sec["baseline_k1"], sec["market_k0"] = cand, base, zero
        if name == "evidence":
            ready = len(days) >= READY_DAYS and station_days >= READY_STATION_DAYS
            sec["ready"] = ready
            if ready:
                ls = sec["candidate"]["logscore"]
                sec["verdict"] = {
                    "A_candidate_gate_admitted": sec["candidate"]["state"] == rs.STATE_ADMITTED,
                    "B_candidate_not_worse_than_market": ls["layer_minus_market"][0] <= 0.0,
                    "C_candidate_not_worse_than_k1": ls["layer_minus_layer_without_change"][0] <= 0.0,
                }
                sec["verdict"]["all_pass"] = all(sec["verdict"].values())
        sections[name] = sec
    reading = {
        "schema": "realisation_shrink_k_reading_v1", "read_at_utc": now, "freeze_utc": FREEZE_UTC,
        "evidence_from": EVIDENCE_FROM, "stage": STAGE, "k_grid": list(K_GRID),
        "code_sha256": rs.code_sha256(), "counts": counts, "k_path": path, **sections,
    }
    out = HERE / f"reading_{now.replace('-', '').replace(':', '')}.json"
    out.write_text(json.dumps(reading, indent=1, default=float))
    print(out)
    for name in ("self_check_pre_freeze", "evidence"):
        sec = sections[name]
        print(f"\n== {name}: {len(sec['event_days'])} days, {sec['station_days']:.0f} station-days, ready={sec.get('ready')}")
        for arm in ("candidate", "baseline_k1", "market_k0"):
            g = sec.get(arm)
            if not g or "calibration_line" not in g:
                print(f"  {arm:12s} {g.get('state') if g else '-'}")
                continue
            s, ls = g["calibration_line"], g["logscore"]
            print(f"  {arm:12s} {g['state']:9s} out={g['cells_outside']}/{g['cells_judged']}(allow {g['cells_allowed']})"
                  f" slope={s['slope'][0]:.3f}[{s['slope'][1]:.3f},{s['slope'][2]:.3f}]"
                  f" icpt={s['intercept'][0]:+.3f}[{s['intercept'][1]:+.3f},{s['intercept'][2]:+.3f}]"
                  f" b-q={ls['layer_minus_market'][0]:+.4f}[{ls['layer_minus_market'][1]:+.4f},{ls['layer_minus_market'][2]:+.4f}]"
                  f" vs_k1={ls['layer_minus_layer_without_change'][0]:+.4f}"
                  f"[{ls['layer_minus_layer_without_change'][1]:+.4f},{ls['layer_minus_layer_without_change'][2]:+.4f}]")
        if "verdict" in sec:
            print("  verdict", sec["verdict"])
    print("\nk path:", " ".join(f"{p['day'][5:]}:{p['k']:.2f}" for p in path))


if __name__ == "__main__":
    main()
