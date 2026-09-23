#!/usr/bin/env python3
"""Where the high-probability buckets over-claim, stage by stage.

Read-only.  The population is the width lane's own serving cells
(``center_width_store.serving_cells_by_day``: one decision per
station-day-stage-3h block, settled truth, pre-band claim, old rows re-served
through today's width function) -- the cells the north star reads.  Each
cell is read twice: as served, and as the width layer received it (the
receipt's ``input_probability``, i.e. after fusion and before the width
layer), so an over-claim can be placed on the side of the layer that made it.

Per stage and window: the reliability diagram in the board's bins
(``_core_murphy``), the top-bucket (modal) claim against its hit rate by
claim band with a 3-day-block bootstrap, and splits of the modal claims
>= 0.5 by lead, unit, served width and station.  Writes results.json.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from bayes_basket_target.center_width_calibration import sigma_floor_lift_v1  # noqa: E402
from bayes_basket_target.center_width_store import (  # noqa: E402
    _evidence_floor,
    _truth,
    serving_cells_by_day,
)
from calibration_dashboard import _core_murphy  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
LONG_FLOOR = "2026-08-15T00:00:00Z"
BANDS = ((0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01))
BOOT = 2000
BLOCK = 3
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def modal(probabilities, keys):
    total = sum(probabilities.get(k, 0.0) for k in keys)
    if total <= 0:
        return None, None
    key = max(keys, key=lambda k: (probabilities.get(k, 0.0), -keys.index(k)))
    return key, probabilities.get(key, 0.0) / total


def input_vector(cell):
    receipt = cell.get("center_width_receipt") or {}
    raw = receipt.get("input_probability") if isinstance(receipt, dict) else None
    if not isinstance(raw, dict):
        return None
    keys = cell["ordered_keys"]
    if not set(keys) <= set(raw):
        return None
    total = sum(float(raw[k]) for k in keys)
    return {k: float(raw[k]) / total for k in keys} if total > 0 else None


def band_table(rows, days):
    """Modal claim vs hit by claim band, station-day weighted, with intervals."""

    out = []
    rng = np.random.default_rng(20260924)
    order = sorted(days)
    n_blocks = max(1, math.ceil(len(order) / BLOCK))
    for lo, hi in BANDS:
        members = [r for r in rows if lo <= r["claim"] < hi]
        if not members:
            continue
        w = np.array([r["w"] for r in members])
        claim = np.array([r["claim"] for r in members])
        hit = np.array([r["hit"] for r in members], float)
        by_day = defaultdict(list)
        for i, r in enumerate(members):
            by_day[r["event_date"]].append(i)
        gaps = []
        for _ in range(BOOT):
            idx = [i for s in rng.integers(0, len(order), n_blocks) for j in range(BLOCK)
                   for i in by_day.get(order[(s + j) % len(order)], [])]
            if not idx:
                continue
            ww = w[idx]
            gaps.append(float((ww * (hit[idx] - claim[idx])).sum() / ww.sum()))
        out.append({
            "band": [lo, min(hi, 1.0)],
            "cells": len(members),
            "station_days": len({(r["station"], r["event_date"]) for r in members}),
            "weight": float(w.sum()),
            "claimed": float((w * claim).sum() / w.sum()),
            "happened": float((w * hit).sum() / w.sum()),
            "gap_ci95": [float(np.percentile(gaps, 2.5)), float(np.percentile(gaps, 97.5))] if gaps else None,
        })
    return out


def split(rows, key_fn, label):
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    out = []
    for key in sorted(groups, key=str):
        members = groups[key]
        w = sum(r["w"] for r in members)
        out.append({
            label: key, "cells": len(members),
            "station_days": len({(r["station"], r["event_date"]) for r in members}),
            "claimed": sum(r["w"] * r["claim"] for r in members) / w,
            "happened": sum(r["w"] * r["hit"] for r in members) / w,
        })
    return out


def analyse(cells_by_day, zones):
    cells = [c for day in sorted(cells_by_day) for c in cells_by_day[day]]
    copies = Counter((c["station_id"], c["event_date"]) for c in cells)
    served_cells, input_cells = [], []
    served_rows, input_rows = [], []
    for c in cells:
        w = 1.0 / copies[(c["station_id"], c["event_date"])]
        keys = c["ordered_keys"]
        served = c["probabilities"]
        total = sum(served.values())
        vector = [served[k] / total for k in keys]
        hit_index = keys.index(c["settled_bucket"])
        served_cells.append((w, {"probabilities": vector, "hit": hit_index}))
        try:
            _lift, sigma_c, rung_c = sigma_floor_lift_v1(
                served, ordered_keys=keys, key_centers_c=c["key_centers_c"], floor_rungs=0.0,
            )
            sigma_rungs = sigma_c / rung_c if rung_c > 0 else None
        except (ValueError, KeyError):
            sigma_rungs = None
        tz = zones.get(c["station_id"])
        lead_h = None
        if tz:
            end = datetime.fromisoformat(c["event_date"]).replace(tzinfo=ZoneInfo(tz)) + timedelta(days=1)
            lead_h = (end - _stamp(c["decision_at_utc"])).total_seconds() / 3600.0
        key, claim = modal(served, keys)
        base = {
            "station": c["station_id"], "event_date": c["event_date"], "w": w,
            "sigma_rungs": sigma_rungs, "lead_h": lead_h,
            "unit": "F" if c["station_id"] in F_STATIONS else "C",
            "width_state": (c.get("center_width_receipt") or {}).get("state"),
        }
        served_rows.append({**base, "claim": claim, "hit": int(key == c["settled_bucket"])})
        raw = input_vector(c)
        if raw is not None:
            input_cells.append((w, {"probabilities": [raw[k] for k in keys], "hit": hit_index}))
            ikey, iclaim = modal(raw, keys)
            input_rows.append({**base, "claim": iclaim, "hit": int(ikey == c["settled_bucket"]),
                               "served_claim_same_cell": claim})
    days = {c["event_date"] for c in cells}
    top = [r for r in served_rows if r["claim"] is not None and r["claim"] >= 0.5]
    lead_band = lambda r: (  # noqa: E731
        "?" if r["lead_h"] is None else "<12h" if r["lead_h"] < 12 else "12-24h" if r["lead_h"] < 24
        else "24-36h" if r["lead_h"] < 36 else "36-48h" if r["lead_h"] < 48 else ">=48h"
    )
    width_band = lambda r: (  # noqa: E731
        "?" if r["sigma_rungs"] is None else "<0.6" if r["sigma_rungs"] < 0.6 else "0.6-0.8"
        if r["sigma_rungs"] < 0.8 else "0.8-1.0" if r["sigma_rungs"] < 1.0 else ">=1.0"
    )
    served_m = _core_murphy(served_cells)
    input_m = _core_murphy(input_cells) if input_cells else None
    stations = split(top, lambda r: r["station"], "station")
    stations = sorted([s for s in stations if s["station_days"] >= 4],
                      key=lambda s: s["happened"] - s["claimed"])
    return {
        "cells": len(cells), "station_days": len(copies), "event_days": len(days),
        "first_day": min(days) if days else None, "last_day": max(days) if days else None,
        "served": {"rel": served_m["reliability"], "res": served_m["resolution"], "diagram": served_m["diagram"]},
        "width_input": None if input_m is None else {
            "cells": len(input_cells), "rel": input_m["reliability"], "res": input_m["resolution"],
            "diagram": input_m["diagram"],
        },
        "modal_served": band_table(served_rows, days),
        "modal_width_input": band_table(input_rows, days) if input_rows else None,
        "top_by_lead": split(top, lead_band, "lead"),
        "top_by_unit": split(top, lambda r: r["unit"], "unit"),
        "top_by_width": split(top, width_band, "served_sigma_rungs"),
        "top_by_width_state": split(top, lambda r: r["width_state"], "width_state"),
        "top_stations_worst": stations[:8],
        "top_stations_best": stations[-5:],
    }


def main() -> int:
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    truth = _truth(conn)
    out = {}
    for stage in ("P0", "P0-M", "P1"):
        out[stage] = {}
        for label, floor in (
            ("evidence_window", _evidence_floor(conn, stage=stage, caller_bound=None)),
            ("since_0815", LONG_FLOOR),
        ):
            cells = serving_cells_by_day(conn, truth, stage=stage, min_decision_at_utc=floor)
            out[stage][label] = {"floor": floor, **analyse(cells, zones)}
            r = out[stage][label]
            print(f"{stage} {label} floor {floor}: {r['cells']} cells {r['station_days']} sd {r['event_days']} d", flush=True)
            for b in r["modal_served"]:
                print(f"   served modal {b['band']}: {b['station_days']} sd claimed {b['claimed']:.3f} happened {b['happened']:.3f} gap CI {[round(x,3) for x in b['gap_ci95']] if b['gap_ci95'] else None}")
            for b in r["modal_width_input"] or []:
                print(f"   input  modal {b['band']}: {b['station_days']} sd claimed {b['claimed']:.3f} happened {b['happened']:.3f}")
    (HERE / "results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
