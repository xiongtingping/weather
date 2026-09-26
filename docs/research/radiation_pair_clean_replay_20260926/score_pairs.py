#!/usr/bin/env python3
"""PROTOCOL.md: score the radiation pairs on the clean replay; R1 residual by z, R2 two scores.

Fidelity first: every ``none`` vector must equal the band study's pass1
vector for the same update.  Population: updates whose radiation resolved at
the update time.  Station-day weights, three-day block bootstrap (2,000).
Writes results.json.
"""
from __future__ import annotations

import gzip
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

ARMS = ("none", "sym07", "p12_04", "cur", "p18_12", "sym18", "sym12", "p24_08")
HALVES = (("2026-08-16", "2026-09-04"), ("2026-09-05", "2026-09-23"))
Z_BINS = ((-math.inf, -1.0), (-1.0, -0.5), (-0.5, 0.0), (0.0, 0.5), (0.5, 1.0), (1.0, math.inf))
BLOCK, BOOT, SEED = 3, 2000, 20260926
MIN_SPREAD_C = 0.25  # post hoc, R1 readout only


def score(vec, keys, hit):
    floored, _ = floor_probabilities({key: float(value) for key, value in zip(keys, vec)})
    values = [floored[key] for key in keys]
    brier = sum((value - (1.0 if index == hit else 0.0)) ** 2 for index, value in enumerate(values))
    return -math.log(max(values[hit], 1e-12)), brier


def load():
    days, old = [], {}
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            days.extend(json.loads(line) for line in handle)
        band = ROOT / "docs" / "research" / "band_layer_recheck_20260925" / "pass1" / path.name
        with gzip.open(band, "rt") as handle:
            for line in handle:
                sd = json.loads(line)
                for u in sd["updates"]:
                    old[(sd["station"], sd["event_date"], u["at"])] = u["vec"]
    worst, matched = 0.0, 0
    for sd in days:
        for u in sd["updates"]:
            ref = old.get((sd["station"], sd["event_date"], u["at"]))
            if ref is None:
                raise SystemExit(f"fidelity: no pass1 update {sd['station']} {sd['event_date']} {u['at']}")
            worst = max(worst, max(abs(a - b) for a, b in zip(u["vecs"]["none"], ref)))
            matched += 1
    if worst >= 1e-9:
        raise SystemExit(f"fidelity: none arm differs from pass1 by {worst}")
    return days, {"matched_updates": matched, "max_abs_diff": worst}


def rows_of(days, zones):
    rows = []
    for sd in days:
        centers = np.array(sd["centers_c"], dtype=float)
        for u in sd["updates"]:
            if u["rad_state"] != "RESOLVED" or u["z"] is None:
                continue
            hours = hours_to_settlement_if_known(sd["event_date"], as_of_utc=u["at"],
                                                 settlement_timezone=zones.get(sd["station"]))
            none = np.array(u["vecs"]["none"], dtype=float)
            mean_none = float(np.dot(none / none.sum(), centers))
            row = {"station": sd["station"], "event_date": sd["event_date"], "hours": hours, "z": float(u["z"]),
                   "r": (float(sd["actual"]) - mean_none) / float(u["spread_c"]),
                   "r_c": float(sd["actual"]) - mean_none, "spread_c": float(u["spread_c"]),
                   "shift_cur": float(u["shift_sigma"]["cur"] or 0.0)}
            for arm in ARMS:
                row[arm] = score(u["vecs"][arm], sd["keys"], sd["hit"])
            rows.append(row)
    return rows


def weights(rows):
    count = defaultdict(int)
    for r in rows:
        count[(r["station"], r["event_date"])] += 1
    return np.array([1.0 / count[(r["station"], r["event_date"])] for r in rows]), len(count)


def boot_index(rows, rng):
    days = sorted({r["event_date"] for r in rows})
    by_day = defaultdict(list)
    for i, r in enumerate(rows):
        by_day[r["event_date"]].append(i)
    n_blocks = math.ceil(len(days) / BLOCK)
    for _ in range(BOOT):
        yield np.concatenate([by_day[days[(s + j) % len(days)]]
                              for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])


def compare(rows, base, test, rng):
    w, station_days = weights(rows)
    d = np.array([[t - b for t, b in zip(r[test], r[base])] for r in rows])
    delta = (w[:, None] * d).sum(axis=0) / w.sum()
    boot = np.array([(w[i, None] * d[i]).sum(axis=0) / w[i].sum() for i in boot_index(rows, rng)])
    lo, hi = np.percentile(boot, 2.5, axis=0), np.percentile(boot, 97.5, axis=0)
    return {"updates": len(rows), "station_days": station_days,
            "days": len({r["event_date"] for r in rows}),
            "d_logscore": float(delta[0]), "ci_logscore": [float(lo[0]), float(hi[0])],
            "d_brier": float(delta[1]), "ci_brier": [float(lo[1]), float(hi[1])]}


def residual_by_z(rows, rng, field="r"):
    w, _ = weights(rows)
    r = np.array([x[field] for x in rows])
    by_station = defaultdict(list)
    for i, x in enumerate(rows):
        by_station[x["station"]].append(i)
    demeaned = r.copy()
    for idx in by_station.values():
        demeaned[idx] -= (w[idx] * r[idx]).sum() / w[idx].sum()
    z = np.array([x["z"] for x in rows])
    shift = np.array([x["shift_cur"] for x in rows])
    boots = list(boot_index(rows, rng))
    out = []
    for lo, hi in Z_BINS:
        mask = (z >= lo) & (z < hi)
        if not mask.any():
            continue

        def m(values, index=None):
            sel = mask if index is None else mask[index]
            vals = values if index is None else values[index]
            ww = w if index is None else w[index]
            return float((ww[sel] * vals[sel]).sum() / ww[sel].sum()) if sel.any() else float("nan")

        draws = [m(demeaned, i) for i in boots]
        draws = [v for v in draws if not math.isnan(v)]
        out.append({"z_bin": [lo, hi], "updates": int(mask.sum()),
                    "station_days": len({(rows[i]["station"], rows[i]["event_date"]) for i in np.flatnonzero(mask)}),
                    "realised_sigma": m(demeaned), "ci": [float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))],
                    "served_cur_sigma": m(shift)})
    return out


def main() -> int:
    days, fidelity = load()
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
    conn.close()
    rows = rows_of(days, zones)
    rng = np.random.default_rng(SEED)
    populations = {
        "all": rows,
        "h24_60": [r for r in rows if r["hours"] is not None and 24 <= r["hours"] < 60],
    }
    result = {"protocol": "PROTOCOL.md", "fidelity": fidelity}
    for name, pop in populations.items():
        block = {"vs_cur": {}, "halves": {}}
        for arm in ARMS:
            if arm == "cur":
                continue
            block["vs_cur"][arm] = compare(pop, "cur", arm, rng)
            block["halves"][arm] = [compare([r for r in pop if a <= r["event_date"] <= b], "cur", arm, rng)
                                    for a, b in HALVES]
        block["residual_by_z"] = residual_by_z(pop, rng)
        # Added after the first read (post hoc): KLAX 08-23 put the whole blend in the open top
        # rung (spread 0.02-0.06C) and its sigma-residuals of -60..-230 swamp the sigma readout.
        sane = [r for r in pop if r["spread_c"] >= MIN_SPREAD_C]
        block["residual_by_z_sane_spread"] = residual_by_z(sane, rng)
        block["residual_by_z_celsius"] = residual_by_z(pop, rng, field="r_c")
        block["sane_spread_dropped_updates"] = len(pop) - len(sane)
        result[name] = block
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("fidelity", fidelity)
    for name in populations:
        b = result[name]
        any_arm = next(iter(b["vs_cur"].values()))
        print(f"===== {name}: {any_arm['updates']} updates, {any_arm['station_days']} station-days, {any_arm['days']} days")
        print("R2 arm - cur (negative = arm better)")
        for arm, c in b["vs_cur"].items():
            h = b["halves"][arm]
            print(f"  {arm:7} LogScore {c['d_logscore']:+.5f} [{c['ci_logscore'][0]:+.5f},{c['ci_logscore'][1]:+.5f}]"
                  f"  Brier {c['d_brier']:+.5f} [{c['ci_brier'][0]:+.5f},{c['ci_brier'][1]:+.5f}]"
                  f"  halves LS {h[0]['d_logscore']:+.5f}/{h[1]['d_logscore']:+.5f} Br {h[0]['d_brier']:+.5f}/{h[1]['d_brier']:+.5f}")
        for label, key in (("sigma, all", "residual_by_z"), (f"sigma, spread>={MIN_SPREAD_C} (post hoc, dropped {b['sane_spread_dropped_updates']})", "residual_by_z_sane_spread"), ("CELSIUS (served column still sigma)", "residual_by_z_celsius")):
            print(f"R1 residual (truth - unshifted centre, station-demeaned) by z -- {label}")
            for x in b[key]:
                print(f"  z {x['z_bin'][0]:>5}..{x['z_bin'][1]:<5} n={x['updates']:6} sd={x['station_days']:5}"
                      f"  realised {x['realised_sigma']:+.3f} [{x['ci'][0]:+.3f},{x['ci'][1]:+.3f}]  served cur {x['served_cur_sigma']:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
