#!/usr/bin/env python3
"""P0: the width layer's input against its output, by lead, on the same cells.

Cells are the width lane's own (evidence window, one per station-day-3h
block) that carry the input the layer received.  For each lead band: the
modal claim and its hit rate before and after the layer, the bucket NLL, and
the lambda on the lane's own grid that the band's cells would pick
(``center_width_store._cell_nll`` / ``apply_center_width_calibration`` --
the lane's scorer and transform, not a copy).  Read-only; research only.
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from bayes_basket_target.center_width_calibration import apply_center_width_calibration  # noqa: E402
from bayes_basket_target.center_width_store import (  # noqa: E402
    _cell_nll,
    _evidence_floor,
    _truth,
    serving_cells_by_day,
)

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
LAMBDAS = (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2)
LEADS = (("<12h", 0, 12), ("12-24h", 12, 24), ("24-36h", 24, 36), ("36-48h", 36, 48), (">=48h", 48, 999))


def main() -> int:
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    floor = _evidence_floor(conn, stage="P0", caller_bound=None)
    by_day = serving_cells_by_day(conn, _truth(conn), stage="P0", min_decision_at_utc=floor)
    cells = [c for day in sorted(by_day) for c in by_day[day]]
    copies = Counter((c["station_id"], c["event_date"]) for c in cells)
    groups = defaultdict(list)
    for c in cells:
        receipt = c.get("center_width_receipt") or {}
        raw = receipt.get("input_probability") if isinstance(receipt, dict) else None
        if not isinstance(raw, dict) or not set(c["ordered_keys"]) <= set(raw):
            continue
        end = datetime.fromisoformat(c["event_date"]).replace(tzinfo=ZoneInfo(zones[c["station_id"]])) + timedelta(days=1)
        lead = (end - datetime.fromisoformat(c["decision_at_utc"].replace("Z", "+00:00"))).total_seconds() / 3600.0
        band = next(name for name, lo, hi in LEADS if lo <= lead < hi)
        total = sum(float(raw[k]) for k in c["ordered_keys"])
        cell = {**c, "probabilities": {k: float(raw[k]) / total for k in c["ordered_keys"]},
                "w": 1.0 / copies[(c["station_id"], c["event_date"])]}
        groups[band].append(cell)
        groups["all"].append(cell)
    out = {}
    for band in [name for name, _lo, _hi in LEADS] + ["all"]:
        members = groups.get(band, [])
        if not members:
            continue
        w_total = sum(c["w"] for c in members)
        nll = {lam: sum(c["w"] * _cell_nll(c, delta_c=0.0, width_lambda=lam) for c in members) / w_total for lam in LAMBDAS}
        best = min(nll, key=nll.get)

        def modal(lam):
            hits = claims = top_w = top_hit = top_claim = 0.0
            for c in members:
                adjusted, _ = apply_center_width_calibration(
                    c["probabilities"], delta_c=0.0, width_lambda=lam, ordered_keys=c["ordered_keys"],
                    key_centers_c=c["key_centers_c"], excluded_buckets=c["excluded_buckets"],
                    open_edge_bounds_c=c.get("open_edge_bounds_c"),
                ) if lam != 1.0 else (c["probabilities"], None)
                total = sum(adjusted.values())
                key = max(c["ordered_keys"], key=lambda k: adjusted[k])
                claim = adjusted[key] / total
                claims += c["w"] * claim
                hits += c["w"] * (key == c["settled_bucket"])
                if claim >= 0.5:
                    top_w += c["w"]
                    top_claim += c["w"] * claim
                    top_hit += c["w"] * (key == c["settled_bucket"])
            return {
                "modal_claimed": claims / w_total, "modal_happened": hits / w_total,
                "top_share": top_w / w_total,
                "top_claimed": top_claim / top_w if top_w else None,
                "top_happened": top_hit / top_w if top_w else None,
            }

        out[band] = {
            "cells": len(members),
            "station_days": len({(c["station_id"], c["event_date"]) for c in members}),
            "event_days": len({c["event_date"] for c in members}),
            "nll_by_lambda": {str(k): v for k, v in nll.items()},
            "best_lambda": best,
            "input_lambda_1": modal(1.0), "served_lambda_075": modal(0.75), "best": modal(best),
        }
        o = out[band]
        f = lambda m: f"modal {m['modal_claimed']:.3f}->{m['modal_happened']:.3f}; >=0.5 share {m['top_share']:.2f} " + (  # noqa: E731
            f"{m['top_claimed']:.3f}->{m['top_happened']:.3f}" if m["top_claimed"] else "-")
        print(f"{band}: {o['station_days']} sd {o['event_days']} d | NLL λ1 {nll[1.0]:.4f} λ.75 {nll[0.75]:.4f} best λ{best} {nll[best]:.4f}")
        print(f"    input  (λ1)   {f(o['input_lambda_1'])}")
        print(f"    served (λ.75) {f(o['served_lambda_075'])}")
        print(f"    best   (λ{best}) {f(o['best'])}")
    (HERE / "by_lead.json").write_text(json.dumps({"floor": floor, "bands": out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
